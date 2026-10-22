"""Direct domestic payment adapters. Fixed HTTPS endpoints; no raw upstream logs."""
import base64
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import parse_qsl, quote, urlsplit
import ipaddress

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .store import CST, Rejected


class PaymentError(Exception):
    """The provider may have accepted the operation; reconcile the same order."""


def cents(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount * 100 != (amount * 100).to_integral_value():
            raise ValueError
        return int(amount * 100)
    except (InvalidOperation, ValueError, TypeError):
        raise PaymentError('支付金额格式无效') from None


def money(value):
    return f'{value // 100}.{value % 100:02d}'


def strict_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate field')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


def sign(key, content):
    return base64.b64encode(key.sign(content, padding.PKCS1v15(), hashes.SHA256())).decode()


def verify(key, signature, content):
    try:
        key.verify(base64.b64decode(signature, validate=True), content, padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        raise PaymentError('支付签名校验失败') from None


def key_file(path, private=False):
    file = Path(path)
    if private and file.stat().st_mode & 0o077:
        raise ValueError('私钥文件权限须为0600')
    raw = file.read_bytes()
    key = serialization.load_pem_private_key(raw, password=None) if private else serialization.load_pem_public_key(raw)
    if not isinstance(key, rsa.RSAPrivateKey if private else rsa.RSAPublicKey) or key.key_size < 2048:
        raise ValueError('支付密钥须为RSA 2048位或以上')
    return key


def public_origin(value):
    parts = urlsplit(value)
    if (parts.scheme != 'https' or not parts.hostname or parts.path not in ('', '/') or parts.query
            or parts.fragment or parts.username or parts.password or parts.port not in (None, 443)):
        raise ValueError('收款需正式公网HTTPS域名')
    host = parts.hostname
    if host == 'localhost' or '.' not in host or host.endswith(('.localhost', '.local')):
        raise ValueError('收款需正式公网HTTPS域名')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return value.rstrip('/')
    raise ValueError('收款请使用正式域名')


async def exchange(method, url, **kwargs):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), trust_env=False) as session:
            async with session.request(method, url, allow_redirects=False, **kwargs) as response:
                body = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise PaymentError('支付通道响应过大')
                if response.status >= 300:
                    raise PaymentError('支付通道暂时无法确认，请查询原订单')
                return {k.lower(): v for k, v in response.headers.items()}, bytes(body)
    except PaymentError:
        raise
    except Exception:
        raise PaymentError('支付结果暂未确认，请查询原订单') from None


class Alipay:
    name = 'alipay'

    def __init__(self, cfg, origin):
        self.app_id, self.seller_id = str(cfg['app_id']), str(cfg['seller_id'])
        if not self.app_id.isdigit() or not self.seller_id.isdigit():
            raise ValueError('支付宝应用ID或商户ID格式有误')
        self.private = key_file(cfg['private_key_file'], True)
        self.public = key_file(cfg['alipay_public_key_file'])
        self.notify_url = origin + '/api/payments/notify/alipay'
        self.profile = hashlib.sha256(('alipay:' + self.app_id + ':' + self.seller_id).encode()).hexdigest()

    async def call(self, method, biz, notify=False):
        params = {'app_id': self.app_id, 'method': method, 'format': 'JSON', 'charset': 'utf-8',
                  'sign_type': 'RSA2', 'timestamp': datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S'),
                  'version': '1.0', 'biz_content': json.dumps(biz, ensure_ascii=False, separators=(',', ':'))}
        if notify:
            params['notify_url'] = self.notify_url
        params['sign'] = sign(self.private, '&'.join(f'{k}={v}' for k, v in sorted(params.items())).encode())
        _, body = await exchange('POST', 'https://openapi.alipay.com/gateway.do', data=params)
        try:
            raw = body.decode('utf-8')
            data = strict_json(raw)
            name = method.replace('.', '_') + '_response'
            match = re.search('"' + re.escape(name) + r'"\s*:\s*', raw)
            if not match or not isinstance(data.get(name), dict):
                raise ValueError
            _, length = json.JSONDecoder().raw_decode(raw[match.end():])
            verify(self.public, data['sign'], raw[match.end():match.end() + length].encode())
            result = data[name]
            if result.get('code') != '10000':
                raise PaymentError('支付通道未确认成功，请查询原订单或联系管理员')
            return result
        except PaymentError:
            raise
        except Exception:
            raise PaymentError('支付宝响应校验失败') from None

    async def create(self, order):
        result = await self.call('alipay.trade.precreate', {'out_trade_no': order['id'], 'seller_id': self.seller_id,
                                 'subject': order['plan_name'], 'total_amount': money(order['amount_cents']),
                                 'timeout_express': '15m'}, True)
        if result.get('out_trade_no') != order['id']:
            raise PaymentError('支付订单不匹配')
        link = result.get('qr_code', '')
        parsed = urlsplit(link)
        if parsed.scheme != 'https' or parsed.hostname != 'qr.alipay.com' or parsed.username:
            raise PaymentError('支付二维码格式无效')
        return link

    def outcome(self, data):
        state = {'TRADE_SUCCESS': 'paid', 'TRADE_FINISHED': 'paid', 'TRADE_CLOSED': 'closed',
                 'WAIT_BUYER_PAY': 'pending'}.get(data.get('trade_status'))
        if not state:
            raise PaymentError('支付状态暂无法识别')
        return {'id': data['out_trade_no'], 'state': state, 'transaction_id': data.get('trade_no'),
                'amount_cents': cents(data['total_amount']), 'currency': 'CNY'}

    async def query(self, order):
        data = await self.call('alipay.trade.query', {'out_trade_no': order['id']})
        if data.get('out_trade_no') != order['id'] or data.get('seller_id') != self.seller_id:
            raise PaymentError('支付商户或订单不匹配')
        return self.outcome(data)

    def notification(self, headers, body):
        try:
            pairs = parse_qsl(body.decode('utf-8'), keep_blank_values=True, strict_parsing=True, max_num_fields=64)
            params = dict(pairs)
            if len(params) != len(pairs) or params.get('sign_type') != 'RSA2':
                raise ValueError
            content = '&'.join(f'{k}={v}' for k, v in sorted(params.items()) if k not in ('sign', 'sign_type') and v)
            verify(self.public, params['sign'], content.encode())
            if params.get('app_id') != self.app_id or params.get('seller_id') != self.seller_id:
                raise ValueError
            return self.outcome(params)
        except PaymentError:
            raise
        except Exception:
            raise PaymentError('支付宝通知校验失败') from None

    async def refund(self, order):
        result = await self.call('alipay.trade.refund', {'out_trade_no': order['id'], 'trade_no': order['transaction_id'],
                                 'out_request_no': 'RF' + order['id'], 'refund_amount': money(order['amount_cents'])})
        if (result.get('out_trade_no') != order['id'] or result.get('trade_no') != order['transaction_id']
                or cents(result.get('refund_fee')) != order['amount_cents']):
            raise PaymentError('退款结果校验失败')
        return result.get('fund_change') == 'Y'

    async def query_refund(self, order):
        result = await self.call('alipay.trade.fastpay.refund.query',
                                 {'out_trade_no': order['id'], 'out_request_no': 'RF' + order['id']})
        if (result.get('out_trade_no') != order['id'] or result.get('trade_no') != order['transaction_id']
                or result.get('out_request_no') != 'RF' + order['id']):
            raise PaymentError('退款订单不匹配')
        return (result.get('refund_status', 'REFUND_SUCCESS') == 'REFUND_SUCCESS'
                and cents(result.get('refund_amount')) == order['amount_cents']
                and cents(result.get('total_amount')) == order['amount_cents'])


class Wechat:
    name = 'wechat'

    def __init__(self, cfg, origin):
        self.app_id, self.mch_id = str(cfg['app_id']), str(cfg['mch_id'])
        self.serial = str(cfg['merchant_serial'])
        if not re.fullmatch(r'[A-Za-z0-9]+', self.app_id + self.mch_id + self.serial):
            raise ValueError('微信商户参数格式有误')
        self.private = key_file(cfg['private_key_file'], True)
        self.public_keys = {serial: key_file(path) for serial, path in cfg['wechat_public_keys'].items()}
        if not self.public_keys:
            raise ValueError('缺少微信支付公钥')
        api_key = Path(cfg['api_v3_key_file'])
        if api_key.stat().st_mode & 0o077:
            raise ValueError('APIv3密钥文件权限须为0600')
        self.api_key = api_key.read_bytes().strip()
        if len(self.api_key) != 32:
            raise ValueError('APIv3密钥须为32字节')
        self.notify_url = origin + '/api/payments/notify/wechat'
        self.profile = hashlib.sha256(('wechat:' + self.app_id + ':' + self.mch_id).encode()).hexdigest()

    def verify_message(self, headers, body):
        try:
            stamp, nonce = headers['wechatpay-timestamp'], headers['wechatpay-nonce']
            if abs(time.time() - int(stamp)) > 300 or not 1 <= len(nonce) <= 128:
                raise ValueError
            key = self.public_keys[headers['wechatpay-serial']]
            verify(key, headers['wechatpay-signature'], stamp.encode() + b'\n' + nonce.encode() + b'\n' + body + b'\n')
        except PaymentError:
            raise
        except Exception:
            raise PaymentError('微信支付签名或时间校验失败') from None

    async def call(self, method, path, data=None):
        body = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode() if data is not None else b''
        stamp, nonce = str(int(time.time())), secrets.token_hex(16)
        signature = sign(self.private, f'{method}\n{path}\n{stamp}\n{nonce}\n'.encode() + body + b'\n')
        auth = (f'WECHATPAY2-SHA256-RSA2048 mchid="{self.mch_id}",nonce_str="{nonce}",'
                f'timestamp="{stamp}",serial_no="{self.serial}",signature="{signature}"')
        headers, raw = await exchange(method, 'https://api.mch.weixin.qq.com' + path, data=body,
                                      headers={'Authorization': auth, 'Accept': 'application/json', 'Content-Type': 'application/json'})
        self.verify_message(headers, raw)
        try:
            return strict_json(raw) if raw else {}
        except Exception:
            raise PaymentError('微信支付响应格式无效') from None

    async def create(self, order):
        data = await self.call('POST', '/v3/pay/transactions/native', {
            'appid': self.app_id, 'mchid': self.mch_id, 'description': order['plan_name'], 'out_trade_no': order['id'],
            'notify_url': self.notify_url, 'time_expire': datetime.fromtimestamp(order['expires'], CST).isoformat(timespec='seconds'),
            'amount': {'total': order['amount_cents'], 'currency': 'CNY'}})
        link = data.get('code_url', '')
        if not link.startswith(('weixin://wxpay/bizpayurl?', 'weixin://pay.weixin.qq.com/')) or len(link) > 2048:
            raise PaymentError('微信支付二维码格式无效')
        return link

    def outcome(self, data):
        if data.get('appid') != self.app_id or data.get('mchid') != self.mch_id:
            raise PaymentError('微信应用或商户不匹配')
        amount = data['amount']
        if type(amount.get('total')) is not int or amount.get('currency') != 'CNY':
            raise PaymentError('微信支付金额或币种无效')
        state = {'SUCCESS': 'paid', 'CLOSED': 'closed', 'REVOKED': 'closed', 'PAYERROR': 'pending',
                 'NOTPAY': 'pending', 'USERPAYING': 'pending', 'REFUND': 'refund_review'}.get(data.get('trade_state'))
        if not state:
            raise PaymentError('微信支付状态暂无法识别')
        return {'id': data['out_trade_no'], 'state': state, 'transaction_id': data.get('transaction_id'),
                'amount_cents': amount['total'], 'currency': amount['currency']}

    async def query(self, order):
        data = await self.call('GET', '/v3/pay/transactions/out-trade-no/' + quote(order['id'], safe='') + '?mchid=' + self.mch_id)
        if data.get('out_trade_no') != order['id']:
            raise PaymentError('微信支付查询订单不匹配')
        return self.outcome(data)

    def notification(self, headers, body):
        self.verify_message(headers, body)
        try:
            data = strict_json(body)
            resource = data['resource']
            if data.get('event_type') != 'TRANSACTION.SUCCESS' or resource.get('algorithm') != 'AEAD_AES_256_GCM':
                raise ValueError
            ciphertext = base64.b64decode(resource['ciphertext'], validate=True)
            aad = resource.get('associated_data', '').encode()
            plain = AESGCM(self.api_key).decrypt(resource['nonce'].encode(), ciphertext, aad)
            result = self.outcome(strict_json(plain))
            if result['state'] != 'paid':
                raise ValueError
            return result
        except PaymentError:
            raise
        except Exception:
            raise PaymentError('微信支付通知解密或状态校验失败') from None

    def refund_result(self, result, order):
        if (result.get('out_trade_no') != order['id'] or result.get('out_refund_no') != 'RF' + order['id']
                or result.get('transaction_id') != order['transaction_id']):
            raise PaymentError('微信退款订单不匹配')
        amount = result.get('amount', {})
        if (type(amount.get('refund')) is not int or amount['refund'] != order['amount_cents']
                or amount.get('total') != order['amount_cents'] or amount.get('currency') != 'CNY'):
            raise PaymentError('微信退款金额不匹配')
        return result.get('status') == 'SUCCESS'

    async def refund(self, order):
        result = await self.call('POST', '/v3/refund/domestic/refunds', {
            'out_trade_no': order['id'], 'out_refund_no': 'RF' + order['id'],
            'amount': {'refund': order['amount_cents'], 'total': order['amount_cents'], 'currency': 'CNY'}})
        return self.refund_result(result, order)

    async def query_refund(self, order):
        result = await self.call('GET', '/v3/refund/domestic/refunds/' + 'RF' + order['id'])
        return self.refund_result(result, order)


class Gateways:
    """Only a private file enables real payment. No fake payment mode in the app."""
    def __init__(self, platform_origin):
        self.adapters, self.errors = {}, {}
        path = os.environ.get('PLATFORM_PAYMENT_CONFIG')
        if not path:
            self.errors = {name: '尚未配置商户收款信息' for name in ('alipay', 'wechat')}
            return
        file = Path(path)
        if file.stat().st_mode & 0o077:
            raise RuntimeError('支付配置文件权限须为0600')
        try:
            cfg = strict_json(file.read_text())
            origin = public_origin(platform_origin)
            for name, adapter in (('alipay', Alipay), ('wechat', Wechat)):
                if name not in cfg:
                    self.errors[name] = '尚未配置商户收款信息'
                    continue
                try:
                    self.adapters[name] = adapter(cfg[name], origin)
                except Exception:
                    self.errors[name] = '商户参数、密钥或公钥配置未通过检查'
        except ValueError:
            self.errors = {name: '需要正式公网HTTPS域名和有效商户配置' for name in ('alipay', 'wechat')}

    def public(self):
        return [{'id': name, 'name': label, 'ready': name in self.adapters,
                 'reason': self.errors.get(name, '')} for name, label in (('alipay', '支付宝'), ('wechat', '微信支付'))]

    def get(self, name):
        if name not in self.adapters:
            raise Rejected('该支付方式暂未开通，请选择其他方式或联系管理员', 409)
        return self.adapters[name]
