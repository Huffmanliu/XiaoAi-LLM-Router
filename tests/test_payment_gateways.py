"""Real cryptographic verification with synthetic keys and zero outbound payment calls."""
import asyncio
import base64
import json
import time
from urllib.parse import urlencode
from unittest.mock import AsyncMock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

from platform_app import payment_gateways as pg


@pytest.fixture(scope='module')
def keys():
    return [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]


@pytest.fixture
def adapters(tmp_path, keys):
    merchant, provider = keys
    private = tmp_path / 'merchant.pem'
    private.write_bytes(merchant.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    private.chmod(0o600)
    public = tmp_path / 'provider.pem'
    public.write_bytes(provider.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    api_key = tmp_path / 'v3.key'
    api_key.write_bytes(b'0' * 32)
    api_key.chmod(0o600)
    ali = pg.Alipay({'app_id': '123456', 'seller_id': '208800001', 'private_key_file': str(private),
                     'alipay_public_key_file': str(public)}, 'https://speaker.example.com')
    wx = pg.Wechat({'app_id': 'wxSynthetic', 'mch_id': '190000001', 'merchant_serial': 'ABCD1234',
                    'private_key_file': str(private), 'wechat_public_keys': {'PUB_KEY_ID_TEST': str(public)},
                    'api_v3_key_file': str(api_key)}, 'https://speaker.example.com')
    return ali, wx, merchant, provider


def ali_notify(adapter, key, **changes):
    params = {'app_id': adapter.app_id, 'seller_id': adapter.seller_id, 'out_trade_no': 'MSsynthetic',
              'trade_no': 'synthetic-transaction', 'trade_status': 'TRADE_SUCCESS', 'total_amount': '19.00', **changes}
    content = '&'.join(f'{k}={v}' for k, v in sorted(params.items()) if v)
    params['sign_type'] = 'RSA2'
    params['sign'] = pg.sign(key, content.encode())
    return urlencode(params).encode()


def wx_headers(key, body, **changes):
    stamp, nonce = str(int(time.time())), 'synthetic-nonce'
    return {'wechatpay-timestamp': stamp, 'wechatpay-nonce': nonce, 'wechatpay-serial': 'PUB_KEY_ID_TEST',
            'wechatpay-signature': pg.sign(key, stamp.encode() + b'\n' + nonce.encode() + b'\n' + body + b'\n'), **changes}


def wx_data(adapter, **changes):
    return {'appid': adapter.app_id, 'mchid': adapter.mch_id, 'out_trade_no': 'MSsynthetic',
            'transaction_id': 'synthetic-transaction', 'trade_state': 'SUCCESS',
            'amount': {'total': 1900, 'currency': 'CNY'}, **changes}


def wx_notify(adapter, key, **changes):
    data = wx_data(adapter, **changes)
    nonce, aad = b'012345678901', b'transaction'
    encrypted = AESGCM(adapter.api_key).encrypt(nonce, json.dumps(data).encode(), aad)
    body = json.dumps({'event_type': 'TRANSACTION.SUCCESS', 'resource': {
        'algorithm': 'AEAD_AES_256_GCM', 'nonce': nonce.decode(), 'associated_data': aad.decode(),
        'ciphertext': base64.b64encode(encrypted).decode()}}).encode()
    return wx_headers(key, body), body


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-1', '0.001', 'abc', None])
def test_bad_decimal_amount_rejected(value):
    with pytest.raises(pg.PaymentError):
        pg.cents(value)


def test_decimal_is_exact():
    assert pg.cents('19.01') == 1901
    assert pg.money(1901) == '19.01'


@pytest.mark.parametrize('origin', [
    'http://pay.example.com', 'https://localhost:18765', 'https://127.0.0.1',
    'https://192.168.0.1', 'https://evil.local', 'https://user:secret@pay.example.com',
    'https://pay.example.com/path', 'https://pay.example.com?x=1',
])
def test_real_payment_requires_public_https_origin(origin):
    with pytest.raises(ValueError):
        pg.public_origin(origin)


def test_alipay_signed_notification_validates_merchant_and_amount(adapters):
    ali, _, _, provider = adapters
    result = ali.notification({}, ali_notify(ali, provider))
    assert result['amount_cents'] == 1900 and result['state'] == 'paid'
    for changes in ({'seller_id': 'other'}, {'app_id': 'other'}, {'total_amount': 'NaN'}):
        with pytest.raises(pg.PaymentError):
            ali.notification({}, ali_notify(ali, provider, **changes))
    valid = ali_notify(ali, provider)
    with pytest.raises(pg.PaymentError):
        ali.notification({}, valid.replace(b'19.00', b'99.00'))
    with pytest.raises(pg.PaymentError):
        ali.notification({}, valid + b'&total_amount=0.01')


def test_wechat_notification_signature_decryption_and_identity(adapters):
    _, wx, _, provider = adapters
    headers, body = wx_notify(wx, provider)
    result = wx.notification(headers, body)
    assert result['state'] == 'paid' and result['currency'] == 'CNY' and result['amount_cents'] == 1900
    for change in ({'wechatpay-signature': 'WECHATPAY/SIGNTEST/invalid'}, {'wechatpay-serial': 'unknown'},
                   {'wechatpay-timestamp': str(int(time.time()) - 301)}):
        with pytest.raises(pg.PaymentError):
            wx.notification({**headers, **change}, body)
    with pytest.raises(pg.PaymentError):
        wx.notification(headers, body + b' ')
    for change in ({'mchid': 'other'}, {'appid': 'other'}, {'amount': {'total': 1900, 'currency': 'USD'}},
                   {'amount': {'total': 1900.0, 'currency': 'CNY'}}):
        bad_headers, bad_body = wx_notify(wx, provider, **change)
        with pytest.raises(pg.PaymentError):
            wx.notification(bad_headers, bad_body)
    original = wx.api_key
    wx.api_key = b'1' * 32
    with pytest.raises(pg.PaymentError):
        wx.notification(headers, body)
    wx.api_key = original


def test_alipay_precreate_signs_request_and_verifies_original_response(adapters, monkeypatch):
    ali, _, merchant, provider = adapters
    response_data = {'code': '10000', 'out_trade_no': 'MSsynthetic', 'qr_code': 'https://qr.alipay.com/synthetic'}
    raw_part = json.dumps(response_data, ensure_ascii=False, indent=2)
    body = ('{"alipay_trade_precreate_response": ' + raw_part + ', "sign": "' + pg.sign(provider, raw_part.encode()) + '"}').encode()
    exchange = AsyncMock(return_value=({}, body))
    monkeypatch.setattr(pg, 'exchange', exchange)
    order = {'id': 'MSsynthetic', 'plan_name': 'Synthetic plan', 'amount_cents': 1900}
    assert asyncio.run(ali.create(order)) == response_data['qr_code']
    args, kwargs = exchange.call_args
    assert args == ('POST', 'https://openapi.alipay.com/gateway.do')
    params = kwargs['data']
    content = '&'.join(f'{k}={v}' for k, v in sorted(params.items()) if k != 'sign')
    pg.verify(merchant.public_key(), params['sign'], content.encode())
    assert json.loads(params['biz_content'])['total_amount'] == '19.00'
    assert params['notify_url'].endswith('/api/payments/notify/alipay')
    exchange.return_value = ({}, body.replace(b'MSsynthetic', b'MSdifferent'))
    with pytest.raises(pg.PaymentError):
        asyncio.run(ali.create(order))


def test_wechat_precreate_signature_response_and_no_redirect(adapters, monkeypatch):
    _, wx, merchant, provider = adapters
    raw = b'{"code_url":"weixin://wxpay/bizpayurl?pr=synthetic"}'
    exchange = AsyncMock(return_value=(wx_headers(provider, raw), raw))
    monkeypatch.setattr(pg, 'exchange', exchange)
    order = {'id': 'MSsynthetic', 'plan_name': 'Synthetic plan', 'amount_cents': 1900, 'expires': time.time() + 900}
    assert asyncio.run(wx.create(order)).startswith('weixin://')
    args, kwargs = exchange.call_args
    assert args == ('POST', 'https://api.mch.weixin.qq.com/v3/pay/transactions/native')
    assert json.loads(kwargs['data'])['amount'] == {'total': 1900, 'currency': 'CNY'}
    authorization = kwargs['headers']['Authorization']
    fields = dict(item.split('=', 1) for item in authorization.split(' ', 1)[1].split(','))
    fields = {k: v.strip('"') for k, v in fields.items()}
    content = f"POST\n/v3/pay/transactions/native\n{fields['timestamp']}\n{fields['nonce_str']}\n".encode() + kwargs['data'] + b'\n'
    pg.verify(merchant.public_key(), fields['signature'], content)
    exchange.return_value = ({**wx_headers(provider, raw), 'wechatpay-signature': 'invalid'}, raw)
    with pytest.raises(pg.PaymentError):
        asyncio.run(wx.create(order))


def test_wechat_query_rejects_different_signed_order(adapters, monkeypatch):
    _, wx, _, _ = adapters
    monkeypatch.setattr(wx, 'call', AsyncMock(return_value=wx_data(wx, out_trade_no='another-order')))
    with pytest.raises(pg.PaymentError):
        asyncio.run(wx.query({'id': 'MSsynthetic'}))


def test_refund_verification_uses_exact_order_and_amount(adapters, monkeypatch):
    ali, wx, _, _ = adapters
    order = {'id': 'MSsynthetic', 'transaction_id': 'synthetic-transaction', 'amount_cents': 1900}
    ali_result = {'out_trade_no': order['id'], 'trade_no': order['transaction_id'], 'out_request_no': 'RF' + order['id'],
                  'total_amount': '19.00', 'refund_amount': '19.00'}
    monkeypatch.setattr(ali, 'call', AsyncMock(return_value=ali_result))
    assert asyncio.run(ali.query_refund(order)) is True
    ali_result['refund_amount'] = '1.00'
    assert asyncio.run(ali.query_refund(order)) is False
    wx_result = {'out_trade_no': order['id'], 'transaction_id': order['transaction_id'], 'out_refund_no': 'RF' + order['id'],
                 'status': 'SUCCESS', 'amount': {'total': 1900, 'refund': 1900, 'currency': 'CNY'}}
    assert wx.refund_result(wx_result, order) is True
    wx_result['amount']['refund'] = 1
    with pytest.raises(pg.PaymentError):
        wx.refund_result(wx_result, order)


def test_private_key_permissions_fail_closed(tmp_path, monkeypatch):
    config = tmp_path / 'payments.json'
    config.write_text('{}')
    config.chmod(0o644)
    monkeypatch.setenv('PLATFORM_PAYMENT_CONFIG', str(config))
    with pytest.raises(RuntimeError, match='0600'):
        pg.Gateways('https://pay.example.com')
    config.chmod(0o600)
    gateways = pg.Gateways('https://localhost:18765')
    assert all(not c['ready'] for c in gateways.public())


def test_duplicate_json_fields_rejected():
    with pytest.raises(ValueError):
        pg.strict_json('{"amount":1,"amount":1900}')
