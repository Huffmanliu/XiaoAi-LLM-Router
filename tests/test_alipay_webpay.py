"""Official SDK signatures, durable events and browser ownership; no payment network calls."""
import asyncio
import base64
import json
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl, urlencode, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import pytest

from platform_app.alipay_sandbox import create_sandbox_app
from platform_app.alipay_webpay import PaymentForm, WebAlipay
from platform_app.payment_gateways import PaymentError, sign, verify


@pytest.fixture(scope='module')
def keys():
    return [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]


@pytest.fixture
def adapter(tmp_path, keys):
    merchant, provider = keys
    private = base64.b64encode(merchant.private_bytes(serialization.Encoding.DER,
                               serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())).decode()
    public = base64.b64encode(provider.public_key().public_bytes(serialization.Encoding.DER,
                              serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
    config = tmp_path / 'sdk.json'
    data = {'appId': '123456', 'sellerId': '208800001', 'appPrivatePkcsKey': private, 'alipayPublicKey': public,
            'appPublicKey': base64.b64encode(merchant.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()}
    config.write_text(json.dumps(data))
    config.chmod(0o600)
    return WebAlipay({'sdk_config_file': str(config)}, 'https://speaker.example.com')


def order(**changes):
    return {'id': 'SyntheticWebpay', 'plan_name': '合成套餐 <safe> & "quote"', 'amount_cents': 19,
            'transaction_id': 'SyntheticTransaction', **changes}


def params(adapter, key, **changes):
    values = {'app_id': adapter.app_id, 'seller_id': adapter.seller_id, 'notify_id': 'SyntheticNotify',
              'notify_type': 'trade_status_sync', 'trade_no': 'SyntheticTransaction',
              'out_trade_no': 'SyntheticWebpay', 'total_amount': '0.19', 'trade_status': 'TRADE_SUCCESS', **changes}
    content = '&'.join(f'{k}={v}' for k, v in sorted(values.items()) if v)
    return {**values, 'sign_type': 'RSA2', 'sign': sign(key, content.encode())}


def test_sdk_form_is_post_exact_signed_return_url_and_fixed_product(adapter, keys):
    html = adapter.payment_form(order())
    parser = PaymentForm()
    parser.feed(html)
    values = dict(parse_qsl(urlsplit(parser.action).query)) | dict(parser.fields)
    signature = values.pop('sign')
    verify(keys[0].public_key(), signature, '&'.join(f'{k}={v}' for k, v in sorted(values.items()) if v).encode())
    assert values['method'] == 'alipay.trade.page.pay'
    assert values['return_url'] == 'https://speaker.example.com/payments/alipay/return'
    assert values['notify_url'] == 'https://speaker.example.com/api/payments/notify/alipay'
    expected = {'out_trade_no': 'SyntheticWebpay', 'total_amount': '0.19', 'subject': order()['plan_name'],
                'product_code': 'FAST_INSTANT_TRADE_PAY', 'timeout_express': '15m'}
    assert json.loads(values['biz_content']) == expected
    assert 'document.forms' not in html and '/static/alipay-submit.js' in html
    assert adapter.config.skip_sign is False
    assert adapter.check_configuration()


def test_sdk_notify_authentication_rejects_tamper_duplicate_and_refund_grant(adapter, keys):
    valid = params(adapter, keys[1])
    assert adapter.notification({}, urlencode(valid).encode())['state'] == 'paid'
    for changes in ({'app_id': 'other'}, {'seller_id': 'other'}, {'notify_id': ''}, {'notify_type': 'other'}):
        with pytest.raises(PaymentError):
            adapter.notification({}, urlencode(params(adapter, keys[1], **changes)).encode())
    with pytest.raises(PaymentError):
        adapter.notification({}, urlencode(valid).encode() + b'&total_amount=0.01')
    with pytest.raises(PaymentError):
        adapter.notification({}, urlencode({**valid, 'total_amount': '1.19'}).encode())
    for feature in ('out_biz_no', 'gmt_refund', 'refund_fee'):
        assert adapter.notification({}, urlencode(params(adapter, keys[1], **{feature: 'synthetic'})).encode())['state'] == 'refund_review'


def test_unknown_query_and_refund_need_explicit_verified_success(adapter, monkeypatch):
    data = {'code': '10000', 'out_trade_no': order()['id'], 'seller_id': adapter.seller_id,
            'trade_no': order()['transaction_id'], 'total_amount': '0.19', 'trade_status': 'UNKNOWN'}
    monkeypatch.setattr(adapter, 'call', AsyncMock(return_value=data))
    assert asyncio.run(adapter.query(order()))['state'] == 'unknown'
    data.update({'out_request_no': 'RFSyntheticWebpay', 'refund_amount': '0.19', 'refund_fee': '0.19'})
    assert asyncio.run(adapter.query_refund(order())) is False
    assert asyncio.run(adapter.refund(order())) is False
    data['refund_status'] = 'REFUND_SUCCESS'
    assert asyncio.run(adapter.query_refund(order())) is True
    data['total_amount'] = '9.99'
    assert asyncio.run(adapter.query_refund(order())) is False


def test_close_queries_before_financial_mutation(adapter, monkeypatch):
    query = AsyncMock(return_value={'id': 'SyntheticWebpay', 'state': 'paid'})
    call = AsyncMock(return_value={'out_trade_no': 'SyntheticWebpay'})
    monkeypatch.setattr(adapter, 'query', query)
    monkeypatch.setattr(adapter, 'call', call)
    assert asyncio.run(adapter.close(order()))['state'] == 'paid'
    call.assert_not_called()
    query.return_value = {'id': 'SyntheticWebpay', 'state': 'pending'}
    assert asyncio.run(adapter.close(order()))['state'] == 'closed'
    assert call.call_args.args[0] == 'alipay.trade.close'


def test_sandbox_persistence_owner_origin_and_notify_idempotency(adapter, tmp_path, monkeypatch, keys):
    app = create_sandbox_app(adapter=adapter, directory=tmp_path / 'ledger')
    with TestClient(app, base_url='http://127.0.0.1:8766') as client:
        client.headers['Origin'] = 'http://127.0.0.1:8766'
        assert client.get('/payments/alipay/return').status_code == 200
        assert client.post('/api/orders').status_code == 409
        assert client.get('/').status_code == 200
        first = client.post('/api/orders').json()
        assert first['amount_cents'] == 1 and first['state'] == 'pending'
        assert client.post('/api/orders').json()['id'] == first['id']
        other = TestClient(app, base_url='http://127.0.0.1:8766')
        other.headers['Origin'] = 'http://127.0.0.1:8766'
        other.get('/')
        assert other.get('/pay/' + first['id']).status_code == 409
        assert client.post('/api/orders', headers={'Origin': 'https://evil.example'}).status_code == 403
        notification = params(adapter, keys[1], out_trade_no=first['id'], total_amount='0.01')
        for _ in range(2):
            assert client.post('/api/payments/notify/alipay', content=urlencode(notification)).text == 'success'
        notification['total_amount'] = '9.99'
        assert client.post('/api/payments/notify/alipay', content=urlencode(notification)).text == 'fail'
        monkeypatch.setattr(adapter, 'query', AsyncMock(return_value={'id': first['id'], 'state': 'paid',
                            'transaction_id': 'SyntheticTransaction', 'amount_cents': 1, 'currency': 'CNY'}))
        assert client.post('/api/orders/' + first['id'] + '/query').json()['state'] == 'paid'
        # A restart uses the same persistent ledger and browser ownership.
        restart = create_sandbox_app(adapter=adapter, directory=tmp_path / 'ledger')
        with TestClient(restart, base_url='http://127.0.0.1:8766') as again:
            again.cookies.update(client.cookies)
            again.headers['Origin'] = 'http://127.0.0.1:8766'
            assert again.post('/api/orders/' + first['id'] + '/query').json()['state'] == 'paid'


def test_sandbox_return_never_trusts_unsigned_order(adapter, tmp_path):
    app = create_sandbox_app(adapter=adapter, directory=tmp_path / 'ledger')
    with TestClient(app, base_url='http://127.0.0.1:8766') as client:
        response = client.get('/api/billing/alipay-return?out_trade_no=SyntheticWebpay&trade_status=TRADE_SUCCESS')
        assert response.status_code == 409
