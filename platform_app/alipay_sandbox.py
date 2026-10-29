"""Loopback-only Alipay web sandbox. Separate ledger; never starts speaker/model services."""
import argparse
from contextlib import contextmanager
import fcntl
from pathlib import Path
import secrets
import sqlite3
import time

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .alipay_webpay import PROJECT_ROOT, WebAlipay
from .payment_gateways import PaymentError


def create_sandbox_app(origin='http://127.0.0.1:8766', adapter=None, directory=None):
    if origin != 'http://127.0.0.1:8766':
        raise ValueError('沙箱只允许固定本机回环入口')
    gateway = adapter or WebAlipay({}, origin, sandbox=True)
    root = Path(directory or PROJECT_ROOT / 'deploy' / 'alipay-webpay-sandbox')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    ledger = root / 'orders.sqlite3'
    static = Path(__file__).parent / 'static'

    @contextmanager
    def db():
        with sqlite3.connect(ledger, timeout=10) as connection:
            connection.row_factory = sqlite3.Row
            yield connection

    with db() as connection:
        connection.executescript('''
        CREATE TABLE IF NOT EXISTS orders (
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, profile TEXT NOT NULL, plan_name TEXT NOT NULL,
          amount_cents INTEGER NOT NULL, state TEXT NOT NULL, transaction_id TEXT,
          created REAL NOT NULL, checked REAL NOT NULL DEFAULT 0, refund_requested REAL,
          UNIQUE(profile,transaction_id));
        CREATE TABLE IF NOT EXISTS notices (
          profile TEXT NOT NULL, notify_id TEXT NOT NULL, order_id TEXT NOT NULL,
          transaction_id TEXT NOT NULL, event_type TEXT NOT NULL, PRIMARY KEY(profile,notify_id));
        ''')
    ledger.chmod(0o600)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.gateway = gateway

    @app.exception_handler(PaymentError)
    async def payment_error(request, exc):
        return JSONResponse({'error': str(exc)}, status_code=409)

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        return JSONResponse({'error': '操作暂未完成，请查询原订单，勿重复支付'}, status_code=409)

    @app.middleware('http')
    async def boundary(request, call_next):
        if request.headers.get('host') != '127.0.0.1:8766':
            return PlainTextResponse('请求域名不匹配', status_code=400)
        if request.method == 'POST' and request.headers.get('origin') != origin and request.url.path != '/api/payments/notify/alipay':
            return PlainTextResponse('请求来源不匹配', status_code=403)
        try:
            size = int(request.headers.get('content-length', '0'))
        except ValueError:
            size = -1
        if size < 0 or size > 32768 or request.headers.get('transfer-encoding'):
            return PlainTextResponse('请求无效', status_code=413)
        response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                                 'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
                                 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://openapi-sandbox.dl.alipaydev.com"})
        return response

    def owner(request):
        value = request.cookies.get('sandbox_owner', '')
        if len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            raise PaymentError('请先打开沙箱付款首页')
        return value

    def order(oid, request=None):
        with db() as connection:
            row = connection.execute('SELECT * FROM orders WHERE id=? AND profile=?' + (' AND owner=?' if request else ''),
                                     (oid, gateway.profile, owner(request)) if request else (oid, gateway.profile)).fetchone()
        if not row:
            raise PaymentError('订单不存在或不属于当前浏览器')
        return dict(row)

    def public(row):
        return {k: row[k] for k in ('id', 'amount_cents', 'state', 'created')}

    def settle(result):
        with db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM orders WHERE id=? AND profile=?', (result['id'], gateway.profile)).fetchone()
            tx = result.get('transaction_id')
            if (not row or result['amount_cents'] != row['amount_cents'] or result['currency'] != 'CNY'
                    or row['transaction_id'] and tx and row['transaction_id'] != tx):
                raise PaymentError('订单或支付金额不匹配')
            if result.get('notify_id'):
                key = (gateway.profile, result['notify_id'])
                event = (row['id'], tx or '', result['event_type'])
                if not connection.execute('INSERT OR IGNORE INTO notices VALUES(?,?,?,?,?)', key + event).rowcount:
                    prior = connection.execute('SELECT order_id,transaction_id,event_type FROM notices WHERE profile=? AND notify_id=?', key).fetchone()
                    if tuple(prior) != event:
                        raise PaymentError('支付通知标识冲突')
                    return
            status = result['state']
            if status == 'paid' and not tx:
                raise PaymentError('支付交易号缺失')
            if row['state'] in ('refunded', 'refunding', 'refund_review'):
                return
            if row['state'] == 'paid' and status in ('closed', 'pending', 'unknown'):
                return
            if status == 'unknown':
                status = 'uncertain'
            if status in ('paid', 'closed', 'pending', 'refund_review', 'uncertain'):
                connection.execute('UPDATE orders SET state=?,transaction_id=COALESCE(?,transaction_id) WHERE id=?',
                                   (status, tx, row['id']))

    @app.get('/')
    async def home(request: Request):
        response = FileResponse(static / 'alipay-sandbox.html')
        if not request.cookies.get('sandbox_owner'):
            response.set_cookie('sandbox_owner', secrets.token_hex(32), httponly=True, samesite='strict', max_age=86400 * 7)
        return response

    @app.post('/api/orders')
    async def create(request: Request):
        now, uid = time.time(), owner(request)
        with db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute("SELECT id FROM orders WHERE owner=? AND profile=? AND state IN ('pending','uncertain') ORDER BY created DESC LIMIT 1",
                                     (uid, gateway.profile)).fetchone()
            if row:
                oid = row['id']  # Reopen the original unpaid order instead of blindly creating another.
            else:
                if connection.execute('SELECT COUNT(*) FROM orders WHERE owner=? AND created>?', (uid, now - 60)).fetchone()[0] >= 5:
                    raise PaymentError('创建订单过于频繁，请稍后再试')
                oid = 'SB' + secrets.token_hex(14)
                connection.execute("INSERT INTO orders(id,owner,profile,plan_name,amount_cents,state,created) VALUES(?,?,?,?,1,'pending',?)",
                                   (oid, uid, gateway.profile, '小爱 AI 沙箱体验（无真实权益）', now))
        return public(order(oid, request))

    @app.get('/pay/{oid}')
    async def pay(oid: str, request: Request):
        row = order(oid, request)
        if row['state'] != 'pending' or row['created'] < time.time() - 900:
            raise PaymentError('订单不可继续付款，请核对原订单')
        return HTMLResponse(gateway.payment_form(row))

    async def refresh(row):
        if row['state'] in ('refunded', 'closed'):
            return public(row)
        now = time.time()
        with db() as connection:
            if not connection.execute('UPDATE orders SET checked=? WHERE id=? AND checked<?', (now, row['id'], now - 10)).rowcount:
                return public(order(row['id']))
        if row['state'] == 'refunding':
            if await gateway.query_refund(row):
                with db() as connection:
                    connection.execute("UPDATE orders SET state='refunded' WHERE id=?", (row['id'],))
        else:
            settle(await gateway.query(row))
        return public(order(row['id']))

    @app.post('/api/orders/{oid}/query')
    async def query(oid: str, request: Request):
        return await refresh(order(oid, request))

    @app.post('/api/orders/{oid}/close')
    async def close(oid: str, request: Request):
        row = order(oid, request)
        if row['state'] not in ('pending', 'uncertain'):
            raise PaymentError('仅待支付订单可关闭')
        settle(await gateway.close(row))
        return public(order(oid, request))

    @app.post('/api/orders/{oid}/refund')
    async def refund(oid: str, request: Request):
        row = order(oid, request)
        with db() as connection:
            if not connection.execute("UPDATE orders SET state='refunding',refund_requested=? WHERE id=? AND state='paid' AND refund_requested IS NULL",
                                      (time.time(), oid)).rowcount:
                raise PaymentError('仅已付款且未申请退款的订单可发起全额退款，请查询原退款')
        if await gateway.refund(row):
            with db() as connection:
                connection.execute("UPDATE orders SET state='refunded' WHERE id=?", (oid,))
        return public(order(oid, request))

    @app.get('/payments/alipay/return')
    async def result_page():
        return FileResponse(static / 'alipay-return.html')

    @app.get('/api/billing/alipay-return')
    async def result(request: Request):
        pairs = list(request.query_params.multi_items())
        params = dict(pairs)
        if len(params) != len(pairs):
            raise PaymentError('回跳参数重复')
        gateway.verify_params(params)
        return await refresh(order(params.get('out_trade_no', ''), request))

    @app.post('/api/payments/notify/alipay')
    async def notify(request: Request):
        try:
            settle(gateway.notification(dict(request.headers), await request.body()))
            return PlainTextResponse('success')
        except Exception:
            return PlainTextResponse('fail', status_code=400)

    app.mount('/static', StaticFiles(directory=static), name='static')
    return app


def main():
    parser = argparse.ArgumentParser(description='本机支付宝网页沙箱，无真实收款或音箱监听')
    parser.parse_args()
    root = PROJECT_ROOT / 'deploy' / 'alipay-webpay-sandbox'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(root / 'instance.lock', 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        import uvicorn
        uvicorn.run(create_sandbox_app(), host='127.0.0.1', port=8766, access_log=False, log_level='warning')


if __name__ == '__main__':
    main()
