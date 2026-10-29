"""Official SDK web payment adapter; sandbox credentials stay in their source file."""
import asyncio
import hashlib
from html import escape
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlsplit

from .payment_gateways import PaymentError, cents, money, strict_json

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SANDBOX_GATEWAY = 'https://openapi-sandbox.dl.alipaydev.com/gateway.do'
PRODUCTION_GATEWAY = 'https://openapi.alipay.com/gateway.do'


class PaymentForm(HTMLParser):
    """Preserve decoded signed values, while emitting only the SDK's form and hidden fields."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.action, self.fields = None, []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'form':
            if self.action is not None or values.get('method', '').lower() != 'post':
                raise ValueError('Invalid payment form')
            self.action = values.get('action')
        elif tag == 'input' and values.get('type') == 'hidden':
            self.fields.append((values['name'], values['value']))


class WebAlipay:
    name = 'alipay'
    page_payment = True

    def __init__(self, cfg, origin, *, sandbox=False):
        from alipay.aop.api.AlipayClientConfig import AlipayClientConfig
        from alipay.aop.api.DefaultAlipayClient import DefaultAlipayClient

        self.sandbox = sandbox
        self.config_path = PROJECT_ROOT / '.alipay-sandbox.json' if sandbox else Path(cfg['sdk_config_file']).resolve()
        if self.config_path.stat().st_mode & 0o077:
            raise ValueError('支付配置文件权限须为0600')
        raw = strict_json(self.config_path.read_text())
        app = raw['appIds'][0] if sandbox else raw
        self.app_id = str(app['appId'])
        # The runtime sandbox merchant identity is authoritative, never the notification.
        self.seller_id = str(raw['sandboxAccounts']['partner']['userId']) if sandbox else str(app['sellerId'])
        private, public = app['appPrivatePkcsKey'], app['alipayPublicKey']
        self.application_public_key = app['appPublicKey']
        if (not self.app_id.isdigit() or not self.seller_id.isdigit()
                or not all(isinstance(v, str) and v and not any(c.isspace() for c in v) for v in (private, public))):
            raise ValueError('支付配置无效')
        self.gateway = SANDBOX_GATEWAY if sandbox else PRODUCTION_GATEWAY
        config = AlipayClientConfig()
        config.server_url, config.app_id = self.gateway, self.app_id
        config.app_private_key, config.alipay_public_key = private, public
        config.charset, config.sign_type = 'utf-8', 'RSA2'
        config.timeout = 12
        self.config = config
        self.client = DefaultAlipayClient(alipay_client_config=config)
        self.return_url = origin.rstrip('/') + '/payments/alipay/return'
        self.notify_url = None if sandbox else origin.rstrip('/') + '/api/payments/notify/alipay'
        self.profile = hashlib.sha256(('alipay:webpay:' + ('sandbox:' if sandbox else 'production:')
                                      + self.app_id + ':' + self.seller_id).encode()).hexdigest()
        self.check_configuration()

    def check_configuration(self):
        """Sign and verify locally with the configured application key pair; no payment call."""
        from alipay.aop.api.util.SignatureUtils import verify_with_rsa
        parsed = PaymentForm()
        parsed.feed(self.payment_form({'id': 'CONFIGURATION_CHECK_ONLY', 'plan_name': '配置校验', 'amount_cents': 1}))
        values = dict(parse_qsl(urlsplit(parsed.action).query)) | dict(parsed.fields)
        signature = values.pop('sign')
        content = '&'.join(f'{k}={v}' for k, v in sorted(values.items()) if v).encode('utf-8')
        if (not verify_with_rsa(self.application_public_key, content, signature)
                or values.get('app_id') != self.app_id or values.get('return_url') != self.return_url
                or self.config.skip_sign or self.config.server_url != self.gateway):
            raise PaymentError('支付配置与SDK映射校验失败')
        return True

    def request(self, method, biz):
        # Import actual SDK request classes, never infer a dynamic class or SDK method.
        from alipay.aop.api.request.AlipayTradePagePayRequest import AlipayTradePagePayRequest
        from alipay.aop.api.request.AlipayTradeQueryRequest import AlipayTradeQueryRequest
        from alipay.aop.api.request.AlipayTradeRefundRequest import AlipayTradeRefundRequest
        from alipay.aop.api.request.AlipayTradeFastpayRefundQueryRequest import AlipayTradeFastpayRefundQueryRequest
        from alipay.aop.api.request.AlipayTradeCloseRequest import AlipayTradeCloseRequest

        classes = {'alipay.trade.page.pay': AlipayTradePagePayRequest, 'alipay.trade.query': AlipayTradeQueryRequest,
                   'alipay.trade.refund': AlipayTradeRefundRequest,
                   'alipay.trade.fastpay.refund.query': AlipayTradeFastpayRefundQueryRequest,
                   'alipay.trade.close': AlipayTradeCloseRequest}
        request = classes[method]()
        request.biz_content = biz
        return request

    async def call(self, method, biz):
        try:
            response = await asyncio.to_thread(self.client.execute, self.request(method, biz))
            result = strict_json(response)
            # DefaultAlipayClient verifies the original signed response and returns its inner object.
            if result.get('code') != '10000':
                raise PaymentError('支付宝暂未确认结果，请核对原订单')
            return result
        except PaymentError:
            raise
        except Exception:
            raise PaymentError('支付宝结果暂未确认，请稍后查询原订单') from None

    async def create(self, order):
        # Page payment only signs the form; no API order is created until the browser submits it.
        return ''

    def payment_form(self, order):
        if type(order['amount_cents']) is not int or not 1 <= order['amount_cents'] <= 10000000000:
            raise PaymentError('支付金额无效')
        request = self.request('alipay.trade.page.pay', {'out_trade_no': order['id'],
                               'total_amount': money(order['amount_cents']), 'subject': order['plan_name'],
                               'product_code': 'FAST_INSTANT_TRADE_PAY', 'timeout_express': '15m'})
        request.return_url = self.return_url
        if self.notify_url:
            request.notify_url = self.notify_url
        try:
            form = self.client.page_execute(request, http_method='POST')
            # The SDK inline auto-submit conflicts with the app's CSP. Use a local script instead.
            # The SDK emits raw '&notify_url' in its action attribute, which HTML parsers
            # interpret as the '&not' entity. Preserve that signed URL before escaping it.
            match = re.search(r'<form\b[^>]*\baction="([^"]+)"', form, re.I)
            action_value = match.group(1) if match else ''
            action = urlsplit(action_value)
            expected = urlsplit(self.gateway)
            biz_content = request.get_params()['biz_content']
            if (action.scheme, action.netloc, action.path) != (expected.scheme, expected.netloc, expected.path) or not biz_content:
                raise ValueError
            form = '<form method="post" action="' + escape(action_value, quote=True) + '">'
            form += '<input type="hidden" name="biz_content" value="' + escape(biz_content, quote=True) + '">'
            form += '<button type="submit">前往支付宝付款</button></form>'
            return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>前往支付宝</title>' + form + '<script src="/static/alipay-submit.js"></script></html>'
        except Exception:
            raise PaymentError('支付页面暂时无法生成') from None

    def verify_params(self, params):
        from alipay.aop.api.util.SignatureUtils import verify_with_rsa
        if params.get('app_id') != self.app_id or params.get('sign_type') != 'RSA2' or not params.get('sign'):
            raise PaymentError('支付宝签名或应用校验失败')
        content = '&'.join(f'{k}={v}' for k, v in sorted(params.items()) if k not in ('sign', 'sign_type') and v)
        try:
            if not verify_with_rsa(self.config.alipay_public_key, content.encode('utf-8'), params['sign']):
                raise ValueError
        except Exception:
            raise PaymentError('支付宝签名校验失败') from None

    def outcome(self, data, *, notification=False):
        # trade.query does not promise seller_id. Its app-scoped SDK request, signed response,
        # fixed HTTPS transport and exact local order/amount are the query ownership boundary.
        if (notification or 'seller_id' in data) and data.get('seller_id') != self.seller_id:
            raise PaymentError('支付宝收款商户不匹配')
        if 'app_id' in data and data['app_id'] != self.app_id:
            raise PaymentError('支付宝应用不匹配')
        return {'id': data['out_trade_no'], 'state': {'TRADE_SUCCESS': 'paid', 'TRADE_FINISHED': 'paid',
                'WAIT_BUYER_PAY': 'pending', 'TRADE_CLOSED': 'closed'}.get(data.get('trade_status'), 'unknown'),
                'transaction_id': data.get('trade_no'), 'amount_cents': cents(data['total_amount']), 'currency': 'CNY'}

    async def query(self, order):
        result = self.outcome(await self.call('alipay.trade.query', {'out_trade_no': order['id']}))
        if result['id'] != order['id'] or result['amount_cents'] != order['amount_cents']:
            raise PaymentError('支付宝订单或金额不匹配')
        return result

    def notification(self, headers, body):
        try:
            pairs = parse_qsl(body.decode('utf-8'), keep_blank_values=True, strict_parsing=True, max_num_fields=64)
            params = dict(pairs)
            if len(params) != len(pairs):
                raise ValueError
            self.verify_params(params)
            if not params.get('notify_id') or params.get('notify_type') != 'trade_status_sync':
                raise ValueError
            result = self.outcome(params, notification=True)
            if any(params.get(k) for k in ('out_biz_no', 'gmt_refund', 'refund_fee')):
                result['state'] = 'refund_review'  # Never grant access from a refund event.
            result['notify_id'] = params['notify_id']
            result['event_type'] = params['trade_status'] + ':' + result['state']
            return result
        except PaymentError:
            raise
        except Exception:
            raise PaymentError('支付宝通知校验失败') from None

    async def close(self, order):
        # Confirm unpaid immediately before closing; an uncertain result stays unresolved.
        current = await self.query(order)
        if current['state'] != 'pending':
            return current
        result = await self.call('alipay.trade.close', {'out_trade_no': order['id']})
        if result.get('out_trade_no') != order['id']:
            raise PaymentError('支付宝关单结果不匹配')
        return {**current, 'state': 'closed'}

    async def refund(self, order):
        # Billing persists one full refund per order before sending this request.
        result = await self.call('alipay.trade.refund', {'out_trade_no': order['id'],
                                 'out_request_no': 'RF' + order['id'], 'refund_amount': money(order['amount_cents'])})
        if (result.get('out_trade_no') != order['id'] or result.get('trade_no') != order['transaction_id']
                or cents(result.get('refund_fee')) != order['amount_cents']):
            raise PaymentError('支付宝退款订单或金额不匹配')
        return result.get('fund_change') == 'Y'

    async def query_refund(self, order):
        result = await self.call('alipay.trade.fastpay.refund.query',
                                 {'out_trade_no': order['id'], 'out_request_no': 'RF' + order['id']})
        if (result.get('out_trade_no') != order['id'] or result.get('trade_no') != order['transaction_id']
                or result.get('out_request_no') != 'RF' + order['id']):
            raise PaymentError('支付宝退款查询订单不匹配')
        return (result.get('refund_status') == 'REFUND_SUCCESS'
                and cents(result.get('refund_amount')) == order['amount_cents']
                and cents(result.get('total_amount')) == order['amount_cents'])
