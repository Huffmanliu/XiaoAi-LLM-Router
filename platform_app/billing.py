"""Atomic paid entitlements and immutable CNY order snapshots, without chat data."""
import asyncio
from contextlib import suppress
import io
import hashlib
import json
import secrets
import sqlite3
import time

from fastapi import Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, field_validator
import segno

from .payment_gateways import Gateways, PaymentError
from .store import Rejected

SCHEMA = '''
CREATE TABLE IF NOT EXISTS billing_plans (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, price_cents INTEGER NOT NULL CHECK(price_cents>0),
 days INTEGER NOT NULL CHECK(days>0), daily_limit INTEGER NOT NULL CHECK(daily_limit>0),
 enabled INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS billing_orders (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), plan_id TEXT NOT NULL,
 plan_name TEXT NOT NULL, days INTEGER NOT NULL, daily_limit INTEGER NOT NULL,
 amount_cents INTEGER NOT NULL CHECK(amount_cents>0), provider TEXT NOT NULL, profile TEXT NOT NULL,
 state TEXT NOT NULL, client_key TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
 qr BLOB, transaction_id TEXT, paid_at REAL, checked_at REAL NOT NULL DEFAULT 0,
 note TEXT NOT NULL DEFAULT '', terms TEXT NOT NULL, UNIQUE(user_id,client_key), UNIQUE(provider,transaction_id));
CREATE INDEX IF NOT EXISTS billing_owner_time ON billing_orders(user_id,created);
CREATE INDEX IF NOT EXISTS billing_reconcile ON billing_orders(state,checked_at);
CREATE TABLE IF NOT EXISTS billing_grants (
 order_id TEXT PRIMARY KEY REFERENCES billing_orders(id), user_id TEXT NOT NULL, plan_id TEXT NOT NULL,
 plan_name TEXT NOT NULL, starts REAL NOT NULL, expires REAL NOT NULL, daily_limit INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'active');
CREATE INDEX IF NOT EXISTS billing_access ON billing_grants(user_id,state,starts,expires);
CREATE TABLE IF NOT EXISTS billing_refunds (
 order_id TEXT PRIMARY KEY REFERENCES billing_orders(id), requested_by TEXT NOT NULL,
 created REAL NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS billing_audit (
 id INTEGER PRIMARY KEY, order_id TEXT, actor_id TEXT, action TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS billing_notify_events (
 provider TEXT NOT NULL, profile TEXT NOT NULL, notify_id TEXT NOT NULL, order_id TEXT NOT NULL,
 transaction_id TEXT NOT NULL, event_type TEXT NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(provider,profile,notify_id));
'''


def billing_settings(db):
    data = json.loads(db.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])
    return {'enabled': False, 'terms': '', 'support': '', **data.get('billing', {})}


def active_grant(db, uid, now):
    row = db.execute("SELECT * FROM billing_grants WHERE user_id=? AND state='active' AND starts<=? "
                     'AND expires>? ORDER BY starts LIMIT 1', (uid, now, now)).fetchone()
    return dict(row) if row else None


def check_access(db, uid, now):
    user = db.execute('SELECT role,active FROM users WHERE id=?', (uid,)).fetchone()
    if not user or not user['active']:
        raise Rejected('服务已暂停，请联系管理员', 403)
    grant = active_grant(db, uid, now)
    if user['role'] != 'admin' and billing_settings(db)['enabled'] and not grant:
        raise Rejected('服务尚未开通或已到期，请在网页“套餐与订单”查看', 402)
    return grant


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Plan(Input):
    name: str = Field(min_length=2, max_length=40)
    price_cents: int = Field(ge=1, le=1000000, strict=True)
    days: int = Field(ge=1, le=366, strict=True)
    daily_limit: int = Field(ge=1, le=10000, strict=True)
    enabled: bool = False

    @field_validator('name')
    @classmethod
    def name_valid(cls, value):
        value = value.strip()
        if len(value) < 2 or any(ord(c) < 32 for c in value):
            raise ValueError('请填写有效套餐名称')
        return value


class Settings(Input):
    enabled: bool
    terms: str = Field(max_length=4000)
    support: str = Field(max_length=200)


class Checkout(Input):
    plan_id: str = Field(min_length=16, max_length=32, pattern=r'^[a-f0-9]+$')
    provider: str = Field(pattern=r'^(alipay|wechat)$')
    client_key: str = Field(min_length=32, max_length=64, pattern=r'^[A-Za-z0-9-]+$')
    accept_terms: bool
    plan_version: float = Field(gt=0, allow_inf_nan=False)
    terms_version: str = Field(pattern=r'^[a-f0-9]{64}$')


class Refund(Input):
    reason: str = Field(min_length=4, max_length=80)


class Billing:
    def __init__(self, store, origin, gateways=None):
        self.store = store
        self.gateways = gateways if gateways is not None else Gateways(origin)
        self.network = asyncio.Semaphore(4)
        with store.db() as db:
            db.executescript(SCHEMA)
        store.billing = self

    def audit(self, db, action, order=None, actor=None):
        db.execute('INSERT INTO billing_audit(order_id,actor_id,action,created) VALUES(?,?,?,?)',
                   (order, actor, action, time.time()))

    def plans(self, admin=False):
        with self.store.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM billing_plans' + ('' if admin else ' WHERE enabled=1') + ' ORDER BY price_cents,id')]

    def overview(self, uid):
        now = time.time()
        with self.store.db() as db:
            settings = billing_settings(db)
            grant = active_grant(db, uid, now)
            queued = [dict(r) for r in db.execute("SELECT * FROM billing_grants WHERE user_id=? AND state='active' AND starts>? ORDER BY starts", (uid, now))]
            orders = [self.public_order(dict(r), db=db) for r in db.execute('SELECT * FROM billing_orders WHERE user_id=? ORDER BY created DESC LIMIT 50', (uid,))]
            role = db.execute('SELECT role FROM users WHERE id=?', (uid,)).fetchone()[0]
        return {'enabled': settings['enabled'], 'terms': settings['terms'], 'support': settings['support'],
                'terms_version': hashlib.sha256(settings['terms'].encode()).hexdigest(),
                'admin_exempt': role == 'admin', 'current': grant, 'queued': queued, 'plans': self.plans(),
                'channels': self.gateways.public(), 'orders': orders}

    def public_order(self, order, db=None):
        keys = ('id', 'user_id', 'plan_id', 'plan_name', 'days', 'daily_limit', 'amount_cents', 'provider',
                'state', 'created', 'expires', 'paid_at', 'checked_at', 'note', 'terms')
        result = {key: order[key] for key in keys}
        result['qr_available'] = bool(order['qr'] and order['state'] == 'pending' and order['expires'] > time.time())
        try:
            page_payment = bool(getattr(self.adapter(order), 'page_payment', False))
        except Rejected:
            page_payment = False
        result['page_payment_available'] = bool(page_payment and order['state'] == 'pending' and order['expires'] > time.time())
        if page_payment:
            result['qr_available'] = False
        result['payment_expired'] = order['expires'] <= time.time() and order['state'] in ('creating', 'pending', 'uncertain')
        if db is not None:
            grant = db.execute('SELECT starts,expires,state FROM billing_grants WHERE order_id=?', (order['id'],)).fetchone()
            result['entitlement'] = dict(grant) if grant else None
        return result

    def order(self, oid, uid=None):
        with self.store.db() as db:
            row = db.execute('SELECT * FROM billing_orders WHERE id=?' + (' AND user_id=?' if uid else ''),
                             (oid, uid) if uid else (oid,)).fetchone()
        if not row:
            raise Rejected('订单不存在', 404)
        return dict(row)

    def adapter(self, order):
        adapter = self.gateways.get(order['provider'])
        if adapter.profile != order['profile']:
            raise Rejected('该订单所属商户配置已变化，请联系管理员核对', 409)
        return adapter

    async def checkout(self, uid, data):
        now = time.time()
        adapter = self.gateways.get(data.provider)
        if not data.accept_terms:
            raise Rejected('请阅读并同意购买和退款说明')
        existing = False
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM billing_orders WHERE user_id=? AND client_key=?', (uid, data.client_key)).fetchone()
            if row:
                if row['plan_id'] != data.plan_id or row['provider'] != data.provider:
                    raise Rejected('请刷新订单后重试', 409)
                existing, order = True, dict(row)
            else:
                settings = billing_settings(db)
                if not settings['enabled'] or not settings['terms'].strip() or not settings['support'].strip():
                    raise Rejected('付费服务暂未开通', 409)
                if data.terms_version != hashlib.sha256(settings['terms'].encode()).hexdigest():
                    raise Rejected('购买与退款说明已更新，请刷新并重新确认', 409)
                if not db.execute("SELECT 1 FROM accounts a JOIN devices d ON a.user_id=d.user_id WHERE a.user_id=? "
                                  "AND a.state='ready' AND d.present=1 AND d.supported=1 LIMIT 1", (uid,)).fetchone():
                    raise Rejected('请先绑定小米账号并确认有支持的音箱，再购买套餐', 409)
                plan = db.execute('SELECT * FROM billing_plans WHERE id=? AND enabled=1', (data.plan_id,)).fetchone()
                if not plan:
                    raise Rejected('套餐已下架，请刷新后重试', 409)
                if plan['updated'] != data.plan_version:
                    raise Rejected('套餐价格或权益已更新，请刷新后重新确认', 409)
                if db.execute('SELECT COUNT(*) FROM billing_orders WHERE user_id=? AND created>?', (uid, now - 60)).fetchone()[0] >= 5:
                    raise Rejected('创建订单太频繁，请稍后再试', 429)
                if db.execute("SELECT COUNT(*) FROM billing_orders WHERE user_id=? AND expires>? AND state IN ('creating','pending','uncertain')", (uid, now)).fetchone()[0] >= 3:
                    raise Rejected('已有待支付订单，请先查看原订单', 429)
                oid = 'MS' + secrets.token_hex(14)
                db.execute('INSERT INTO billing_orders(id,user_id,plan_id,plan_name,days,daily_limit,amount_cents,provider,profile,state,client_key,created,expires,terms) '
                           "VALUES(?,?,?,?,?,?,?,?,?,'creating',?,?,?,?)",
                           (oid, uid, plan['id'], plan['name'], plan['days'], plan['daily_limit'], plan['price_cents'],
                            data.provider, adapter.profile, data.client_key, now, now + 900, settings['terms']))
                self.audit(db, 'order_created', oid, uid)
                order = dict(db.execute('SELECT * FROM billing_orders WHERE id=?', (oid,)).fetchone())
        if not existing:
            try:
                async with self.network:
                    link = await adapter.create(order)
                with self.store.db() as db:
                    db.execute("UPDATE billing_orders SET state='pending',qr=? WHERE id=? AND state='creating'",
                               (self.store.vault.seal(link, 'payment-qr:' + order['id']), order['id']))
            except Exception:
                with self.store.db() as db:
                    db.execute("UPDATE billing_orders SET state='uncertain',note=? WHERE id=? AND state='creating'",
                               ('下单结果暂未确认，请查询原订单，勿重复付款', order['id']))
        return self.public_order(self.order(order['id'], uid))

    def settle(self, provider, profile, result):
        now = time.time()
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM billing_orders WHERE id=?', (result['id'],)).fetchone()
            if (not row or row['provider'] != provider or row['profile'] != profile or result.get('currency') != 'CNY'
                    or type(result.get('amount_cents')) is not int or result['amount_cents'] != row['amount_cents']):
                raise PaymentError('支付订单金额或归属不匹配')
            status = result['state']
            tx = result.get('transaction_id')
            if row['transaction_id'] and tx and row['transaction_id'] != tx:
                raise PaymentError('支付交易号不匹配')
            if result.get('notify_id'):
                # Insert in the same transaction as fulfillment. A failed settlement rolls back both.
                if not db.execute('INSERT OR IGNORE INTO billing_notify_events VALUES(?,?,?,?,?,?,?)',
                                  (provider, profile, result['notify_id'], row['id'], tx or '',
                                   result['event_type'], now)).rowcount:
                    prior = db.execute('SELECT order_id,transaction_id,event_type FROM billing_notify_events '
                                       'WHERE provider=? AND profile=? AND notify_id=?',
                                       (provider, profile, result['notify_id'])).fetchone()
                    if tuple(prior) != (row['id'], tx or '', result['event_type']):
                        raise PaymentError('支付通知标识冲突')
                    return
            if status == 'paid':
                if not isinstance(tx, str) or not 1 <= len(tx) <= 80:
                    raise PaymentError('缺少有效支付交易号')
                if row['state'] in ('paid', 'refunding', 'refunded', 'refund_review'):
                    return
                end = db.execute("SELECT MAX(expires) FROM billing_grants WHERE user_id=? AND state='active'", (row['user_id'],)).fetchone()[0]
                start = max(now, end or now)
                try:
                    db.execute("UPDATE billing_orders SET state='paid',transaction_id=?,paid_at=?,note='' WHERE id=?", (tx, now, row['id']))
                except sqlite3.IntegrityError:
                    raise PaymentError('支付交易号已用于其他订单') from None
                db.execute('INSERT INTO billing_grants(order_id,user_id,plan_id,plan_name,starts,expires,daily_limit) VALUES(?,?,?,?,?,?,?)',
                           (row['id'], row['user_id'], row['plan_id'], row['plan_name'], start, start + row['days'] * 86400, row['daily_limit']))
                self.audit(db, 'payment_confirmed', row['id'])
            elif status == 'closed':
                if row['state'] in ('paid', 'refunding', 'refund_review') and provider == 'alipay' and not getattr(self.adapter(dict(row)), 'page_payment', False):
                    self._refund_done(db, row['id'])
                elif row['state'] == 'paid' and provider == 'alipay':
                    db.execute("UPDATE billing_orders SET state='refund_review',note=? WHERE id=?", ('商户侧交易已关闭，请管理员核对退款金额', row['id']))
                    db.execute("UPDATE billing_grants SET state='suspended' WHERE order_id=?", (row['id'],))
                    self.audit(db, 'external_refund_review', row['id'])
                elif row['state'] not in ('paid', 'refunding', 'refunded', 'refund_review'):
                    db.execute("UPDATE billing_orders SET state='closed',qr=NULL WHERE id=?", (row['id'],))
            elif status == 'refund_review' and row['state'] == 'paid':
                db.execute("UPDATE billing_orders SET state='refund_review',note=? WHERE id=?", ('商户侧存在退款，请管理员核对退款金额', row['id']))
                db.execute("UPDATE billing_grants SET state='suspended' WHERE order_id=?", (row['id'],))
                self.audit(db, 'external_refund_review', row['id'])
            elif status == 'unknown' and row['state'] in ('creating', 'pending', 'uncertain'):
                db.execute("UPDATE billing_orders SET state='uncertain',note=? WHERE id=?",
                           ('支付结果未知，请核对原订单，勿重复付款', row['id']))

    def _refund_done(self, db, oid):
        db.execute("UPDATE billing_orders SET state='refunded',qr=NULL,note='' WHERE id=?", (oid,))
        db.execute("UPDATE billing_grants SET state='revoked' WHERE order_id=?", (oid,))
        db.execute("UPDATE billing_refunds SET state='succeeded' WHERE order_id=?", (oid,))
        self.audit(db, 'refund_confirmed', oid)

    async def reconcile(self, oid, uid=None, refund=False):
        order = self.order(oid, uid)
        if order['state'] in ('closed', 'refunded'):
            return self.public_order(order)
        now = time.time()
        with self.store.db() as db:
            if not db.execute('UPDATE billing_orders SET checked_at=? WHERE id=? AND checked_at<?',
                              (now, oid, now - 10)).rowcount:
                return self.public_order(self.order(oid, uid))
        adapter = self.adapter(order)
        try:
            async with self.network:
                if order['state'] == 'refunding' or refund:
                    if await adapter.query_refund(order):
                        with self.store.db() as db:
                            db.execute('BEGIN IMMEDIATE')
                            self._refund_done(db, oid)
                else:
                    result = await adapter.query(order)
                    self.settle(order['provider'], adapter.profile, result)
        except (PaymentError, Rejected):
            with self.store.db() as db:
                db.execute('UPDATE billing_orders SET note=? WHERE id=?', ('通道暂未确认最新结果，请稍后查询或联系管理员核对', oid))
        with self.store.db() as db:
            return self.public_order(self.order(oid, uid), db=db)

    async def request_refund(self, oid, admin_uid, reason):
        order = self.order(oid)
        adapter = self.adapter(order)
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM billing_refunds WHERE order_id=?', (oid,)).fetchone():
                raise Rejected('该订单已发起退款，请查询退款结果，勿重复提交', 409)
            order = dict(db.execute('SELECT * FROM billing_orders WHERE id=?', (oid,)).fetchone())
            if order['state'] != 'paid':
                raise Rejected('仅已支付订单可申请全额退款', 409)
            db.execute("INSERT INTO billing_refunds VALUES(?,?,?,'processing',?)", (oid, admin_uid, time.time(), reason))
            db.execute("UPDATE billing_orders SET state='refunding',note=? WHERE id=?", ('退款处理中，对应套餐服务已暂停', oid))
            db.execute("UPDATE billing_grants SET state='suspended' WHERE order_id=?", (oid,))
            self.audit(db, 'refund_requested', oid, admin_uid)
        try:
            async with self.network:
                success = await adapter.refund(order)
            if success:
                with self.store.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    self._refund_done(db, oid)
        except Exception:
            # Never report money returned or retry a financial mutation blindly.
            pass
        return self.public_order(self.order(oid))

    async def background(self):
        while True:
            await asyncio.sleep(30)
            with self.store.db() as db:
                rows = db.execute("SELECT id FROM billing_orders WHERE state IN ('creating','pending','uncertain','refunding','refund_review') "
                                  'AND checked_at<? AND created>? ORDER BY checked_at LIMIT 10', (time.time() - 30, time.time() - 86400 * 7)).fetchall()
            for row in rows:
                with suppress(Exception):
                    await self.reconcile(row['id'])

    def install(self, app, current, admin, auth, rate):
        @app.get('/payments/alipay/return')
        async def alipay_return():
            # The public shell displays no order details. Authenticated API verifies signed params.
            return FileResponse(Path(__file__).parent / 'static' / 'alipay-return.html')

        @app.get('/api/billing/alipay-return')
        async def alipay_return_status(request: Request, user=Depends(current)):
            pairs = list(request.query_params.multi_items())
            params = dict(pairs)
            adapter = self.gateways.get('alipay')
            if len(params) != len(pairs) or not getattr(adapter, 'page_payment', False):
                raise Rejected('支付回跳参数无效', 400)
            try:
                adapter.verify_params(params)
            except PaymentError:
                raise Rejected('支付回跳签名校验失败，请从订单列表核对', 400) from None
            return await self.reconcile(params.get('out_trade_no', ''), user['user_id'])

        @app.get('/api/billing/orders/{oid}/pay')
        async def page_pay(oid: str, user=Depends(current)):
            order = self.order(oid, user['user_id'])
            if not self.public_order(order)['page_payment_available']:
                raise Rejected('该订单暂不能前往付款，请先核对原订单', 409)
            return HTMLResponse(self.adapter(order).payment_form(order))

        @app.post('/api/billing/orders/{oid}/close')
        async def close_order(oid: str, request: Request, user=Depends(current)):
            rate(request, 'payment-close', 5)
            order = self.order(oid, user['user_id'])
            adapter = self.adapter(order)
            if not getattr(adapter, 'page_payment', False) or order['state'] not in ('pending', 'uncertain', 'creating'):
                raise Rejected('该订单暂不能关闭，请先查询订单结果', 409)
            try:
                async with self.network:
                    self.settle(order['provider'], adapter.profile, await adapter.close(order))
            except PaymentError:
                raise Rejected('关单结果待核对，请查询原订单', 409) from None
            return self.public_order(self.order(oid, user['user_id']))

        @app.get('/api/billing')
        async def overview(user=Depends(current)):
            return self.overview(user['user_id'])

        @app.post('/api/billing/orders')
        async def checkout(data: Checkout, request: Request, user=Depends(current)):
            rate(request, 'checkout', 5)
            return await self.checkout(user['user_id'], data)

        @app.post('/api/billing/orders/{oid}/refresh')
        async def refresh(oid: str, request: Request, user=Depends(current)):
            rate(request, 'payment-query', 10)
            return await self.reconcile(oid, user['user_id'])

        @app.get('/api/billing/orders/{oid}/qr')
        async def qr(oid: str, user=Depends(current)):
            order = self.order(oid, user['user_id'])
            if not self.public_order(order)['qr_available']:
                raise Rejected('订单二维码已失效，请查询订单结果', 409)
            link = self.store.vault.open(order['qr'], 'payment-qr:' + oid)
            output = io.BytesIO()
            segno.make_qr(link).save(output, kind='svg', scale=6, border=4)
            return Response(output.getvalue(), media_type='image/svg+xml')

        @app.post('/api/payments/notify/{provider}')
        async def notify(provider: str, request: Request):
            try:
                adapter = self.gateways.get(provider)
                body = await request.body()
                if len(body) > 32768:
                    raise PaymentError('通知过大')
                result = adapter.notification(dict(request.headers), body)
                self.settle(provider, adapter.profile, result)
            except Exception:
                return PlainTextResponse('fail' if provider == 'alipay' else 'failure', status_code=400)
            return PlainTextResponse('success') if provider == 'alipay' else Response(status_code=204)

        @app.get('/api/admin/billing')
        async def admin_overview(page: int = 1, user=Depends(admin)):
            page = max(1, min(page, 1000000))
            with self.store.db() as db:
                orders = [self.public_order(dict(r), db=db) for r in db.execute('SELECT o.*,u.username FROM billing_orders o JOIN users u ON u.id=o.user_id ORDER BY o.created DESC LIMIT 25 OFFSET ?', ((page - 1) * 25,))]
                usernames = {r['id']: r['username'] for r in db.execute('SELECT id,username FROM users')}
                count = db.execute('SELECT COUNT(*) FROM billing_orders').fetchone()[0]
                settings = billing_settings(db)
                summary = dict(db.execute("SELECT COUNT(*) AS orders,COALESCE(SUM(CASE WHEN state='paid' THEN amount_cents ELSE 0 END),0) AS paid_cents,"
                                          "COALESCE(SUM(CASE WHEN state='refunded' THEN amount_cents ELSE 0 END),0) AS refunded_cents,"
                                          "COALESCE(SUM(CASE WHEN state IN ('uncertain','refunding','refund_review') THEN 1 ELSE 0 END),0) AS needs_review FROM billing_orders").fetchone())
            return {'plans': self.plans(True), 'channels': self.gateways.public(), 'settings': settings,
                    'orders': [{**o, 'username': usernames.get(o['user_id'], '')} for o in orders],
                    'page': page, 'pages': max(1, (count + 24) // 25), 'summary': summary}

        @app.put('/api/admin/billing/settings')
        async def settings(data: Settings, user=Depends(admin)):
            auth.recent(user)
            if data.enabled and (not any(c['ready'] for c in self.gateways.public()) or not data.terms.strip()
                                 or not data.support.strip() or not self.plans()):
                raise Rejected('开启收费前请配置商户、上架套餐，并填写购买/退款说明和联系渠道', 409)
            self.store.update_settings({'billing': data.model_dump()})
            with self.store.db() as db:
                self.audit(db, 'billing_enabled' if data.enabled else 'billing_disabled', actor=user['user_id'])
            return {'ok': True}

        @app.post('/api/admin/billing/plans')
        async def create_plan(data: Plan, user=Depends(admin)):
            auth.recent(user)
            ident = secrets.token_hex(12)
            with self.store.db() as db:
                if db.execute('SELECT COUNT(*) FROM billing_plans').fetchone()[0] >= 20:
                    raise Rejected('最多可配置20个套餐')
                db.execute('INSERT INTO billing_plans VALUES(?,?,?,?,?,?,?)', (ident, data.name.strip(), data.price_cents,
                           data.days, data.daily_limit, data.enabled, time.time()))
                self.audit(db, 'plan_created', actor=user['user_id'])
            return {'id': ident}

        @app.put('/api/admin/billing/plans/{ident}')
        async def update_plan(ident: str, data: Plan, user=Depends(admin)):
            auth.recent(user)
            with self.store.db() as db:
                if not db.execute('UPDATE billing_plans SET name=?,price_cents=?,days=?,daily_limit=?,enabled=?,updated=? WHERE id=?',
                                  (data.name.strip(), data.price_cents, data.days, data.daily_limit, data.enabled, time.time(), ident)).rowcount:
                    raise Rejected('套餐不存在', 404)
                self.audit(db, 'plan_updated', actor=user['user_id'])
            return {'ok': True}

        @app.post('/api/admin/billing/orders/{oid}/refresh')
        async def admin_refresh(oid: str, request: Request, user=Depends(admin)):
            rate(request, 'admin-payment-query', 10)
            return await self.reconcile(oid)

        @app.post('/api/admin/billing/orders/{oid}/refund')
        async def refund(oid: str, data: Refund, request: Request, user=Depends(admin)):
            auth.recent(user)
            rate(request, 'refund', 3)
            return await self.request_refund(oid, user['user_id'], data.reason.strip())
