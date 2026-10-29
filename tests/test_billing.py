"""Synthetic payment lifecycle and abuse tests. Never contact payment/model/speaker services."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import secrets
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from platform_app.payment_gateways import PaymentError
from platform_app.store import Rejected
import test_platform
from test_platform import login, invite_user, bound

web = test_platform.web


class FakeAdapter:
    def __init__(self, name):
        self.name, self.profile = name, 'synthetic-profile-' + name
        self.create = AsyncMock(return_value='https://qr.alipay.com/synthetic' if name == 'alipay' else 'weixin://wxpay/bizpayurl?pr=synthetic')
        self.query = AsyncMock()
        self.refund = AsyncMock(return_value=True)
        self.query_refund = AsyncMock(return_value=True)

    def notification(self, headers, body):
        if headers.get('synthetic-signature') != 'valid':
            raise PaymentError('Invalid test signature')
        return json.loads(body)


class FakeGateways:
    def __init__(self):
        self.adapters = {name: FakeAdapter(name) for name in ('alipay', 'wechat')}

    def get(self, name):
        if name not in self.adapters:
            raise Rejected('Not configured', 409)
        return self.adapters[name]

    def public(self):
        return [{'id': name, 'name': name, 'ready': name in self.adapters, 'reason': ''} for name in ('alipay', 'wechat')]


@pytest.fixture
def paid(web):
    client, app = web
    admin = login(client)
    app.state.billing.gateways = FakeGateways()
    payload = {'name': 'Synthetic monthly plan', 'price_cents': 1900, 'days': 30, 'daily_limit': 100, 'enabled': True}
    r = client.post('/api/admin/billing/plans', json=payload)
    assert r.status_code == 200
    plan = app.state.billing.plans(True)[0]
    settings = {'enabled': True, 'terms': 'Synthetic purchase and refund terms', 'support': 'Synthetic support'}
    assert client.put('/api/admin/billing/settings', json=settings).status_code == 200
    owner, _ = invite_user(client)
    bound(app.state.store, owner['user_id'])
    return client, app, owner, admin, plan


def checkout(client, plan, provider='alipay', **changes):
    return client.post('/api/billing/orders', json={
        'plan_id': plan['id'], 'provider': provider, 'client_key': secrets.token_hex(16),
        'accept_terms': True, 'plan_version': plan['updated'],
        'terms_version': hashlib.sha256(b'Synthetic purchase and refund terms').hexdigest(), **changes})


def result(order, **changes):
    return {'id': order['id'], 'transaction_id': 'synthetic-tx-' + order['id'], 'state': 'paid',
            'amount_cents': order['amount_cents'], 'currency': 'CNY', **changes}


def settle(app, order, **changes):
    adapter = app.state.billing.gateways.get(order['provider'])
    app.state.billing.settle(order['provider'], adapter.profile, result(order, **changes))


def test_disabled_by_default_and_unconfigured_cannot_enable(web):
    client, app = web
    login(client)
    info = client.get('/api/billing').json()
    assert not info['enabled'] and all(not c['ready'] for c in info['channels'])
    assert client.put('/api/admin/billing/settings', json={'enabled': True, 'terms': 'terms', 'support': 'support'}).status_code == 409
    assert client.post('/api/admin/billing/plans', json={'name': '  ', 'price_cents': 1, 'days': 30, 'daily_limit': 2}).status_code == 422


@pytest.mark.parametrize('provider', ['alipay', 'wechat'])
def test_payment_grants_access_once_and_does_not_change_devices(paid, provider):
    client, app, owner, _, plan = paid
    before = app.state.store.devices(owner['user_id'])
    with pytest.raises(Rejected) as error:
        app.state.store.reserve(owner['user_id'], 'speaker-a', 'speaker', 'before-payment')
    assert error.value.status == 402
    order = checkout(client, plan, provider).json()
    assert order['state'] == 'pending' and order['amount_cents'] == 1900
    qr = client.get('/api/billing/orders/' + order['id'] + '/qr')
    assert qr.status_code == 200 and '<svg' in qr.text
    settle(app, order)
    for _ in range(3):
        settle(app, order)
    with app.state.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM billing_grants').fetchone()[0] == 1
    rid = app.state.store.reserve(owner['user_id'], 'speaker-a', 'speaker', 'after-payment')
    assert rid and app.state.store.policy(owner['user_id'])['daily_limit'] == 100
    assert app.state.store.devices(owner['user_id']) == before
    snapshot = client.get('/api/billing').json()
    assert snapshot['current']['daily_limit'] == 100
    assert 'transaction_id' not in snapshot['orders'][0] and 'profile' not in snapshot['orders'][0]
    assert 'Synthetic purchase' in snapshot['orders'][0]['terms']


@pytest.mark.parametrize('change', [{'amount_cents': 1}, {'currency': 'USD'}, {'transaction_id': None}, {'amount_cents': 1900.0}])
def test_bad_amount_currency_or_transaction_never_grants(paid, change):
    client, app, _, _, plan = paid
    order = checkout(client, plan).json()
    with pytest.raises(PaymentError):
        settle(app, order, **change)
    assert client.get('/api/billing').json()['current'] is None


def test_idempotent_checkout_immutable_snapshot_and_stale_plan(paid):
    client, app, _, _, plan = paid
    key = secrets.token_hex(16)
    first = checkout(client, plan, client_key=key)
    repeat = checkout(client, plan, client_key=key)
    assert first.json()['id'] == repeat.json()['id']
    assert app.state.billing.gateways.get('alipay').create.call_count == 1
    with app.state.store.db() as db:
        db.execute('UPDATE billing_plans SET price_cents=9900,days=1,updated=?', (time.time() + 1,))
    assert checkout(client, plan).status_code == 409
    settle(app, first.json())
    assert client.get('/api/billing').json()['orders'][0]['days'] == 30
    assert checkout(client, plan, amount_cents=1).status_code == 422


def test_registration_binding_and_terms_prerequisites(paid):
    client, app, owner, _, plan = paid
    assert checkout(client, plan, accept_terms=False).status_code == 400
    with app.state.store.db() as db:
        db.execute('UPDATE devices SET supported=0 WHERE user_id=?', (owner['user_id'],))
    assert checkout(client, plan).status_code == 409
    assert app.state.billing.gateways.get('alipay').create.call_count == 0


def test_creation_timeout_keeps_original_order_for_query(paid):
    client, app, _, _, plan = paid
    adapter = app.state.billing.gateways.get('alipay')
    adapter.create.side_effect = PaymentError('PRIVATE provider details')
    order = checkout(client, plan).json()
    assert order['state'] == 'uncertain' and 'PRIVATE' not in str(order)
    adapter.query.return_value = result(order)
    r = client.post('/api/billing/orders/' + order['id'] + '/refresh')
    assert r.json()['state'] == 'paid'
    assert adapter.create.call_count == 1


def test_late_signed_payment_and_parallel_notifications_grant_once(paid):
    client, app, owner, _, plan = paid
    order = checkout(client, plan).json()
    with app.state.store.db() as db:
        db.execute('UPDATE billing_orders SET expires=0 WHERE id=?', (order['id'],))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: settle(app, order), range(8)))
    with app.state.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM billing_grants WHERE user_id=?', (owner['user_id'],)).fetchone()[0] == 1
    assert client.get('/api/billing').json()['current']


def test_renewals_queue_without_overlap_or_daily_reset(paid):
    client, app, owner, _, plan = paid
    one = checkout(client, plan).json()
    settle(app, one)
    rid = app.state.store.reserve(owner['user_id'], 'speaker-a', 'speaker', 'counted')
    app.state.store.started(rid)
    two = checkout(client, plan).json()
    settle(app, two)
    info = client.get('/api/billing').json()
    assert len(info['queued']) == 1 and info['queued'][0]['starts'] == info['current']['expires']
    assert app.state.store.summary(owner['user_id'])['today'] == 1


def test_unique_provider_transaction_not_reused_for_other_order(paid):
    client, app, _, _, plan = paid
    one, two = checkout(client, plan).json(), checkout(client, plan).json()
    settle(app, one, transaction_id='same-transaction')
    with pytest.raises(PaymentError):
        settle(app, two, transaction_id='same-transaction')
    assert app.state.billing.order(two['id'])['state'] == 'pending'


def test_access_expiry_rechecked_after_queue_but_started_request_can_finish(paid):
    client, app, owner, _, plan = paid
    order = checkout(client, plan).json()
    settle(app, order)
    rid = app.state.store.reserve(owner['user_id'], 'speaker-a', 'speaker', 'queued')
    started = app.state.store.reserve(owner['user_id'], 'speaker-a', 'speaker', 'started')
    app.state.store.started(started)
    with app.state.store.db() as db:
        db.execute('UPDATE billing_grants SET expires=?', (time.time() - 1,))
    with pytest.raises(Rejected):
        app.state.store.started(rid)
    app.state.store.started(started)  # An already-started failover is part of that same paid request.
    with pytest.raises(Rejected):
        app.state.store.reserve(owner['user_id'], 'speaker-a', 'speaker', 'expired')
    assert client.get('/api/history').status_code == 200
    assert client.get('/api/me').status_code == 200


def test_admin_exempt_and_custom_daily_cap_preserved(paid):
    client, app, owner, admin, plan = paid
    assert app.state.store.reserve(admin['user_id'], None, 'web', 'admin-exempt')
    order = checkout(client, plan).json()
    settle(app, order)
    with app.state.store.db() as db:
        db.execute('UPDATE users SET policy=? WHERE id=?', (json.dumps({'daily_limit': 2}), owner['user_id']))
    assert app.state.store.policy(owner['user_id'])['daily_limit'] == 2


def test_other_user_cannot_view_query_qr_or_refund_order(paid):
    client, app, _, _, plan = paid
    order = checkout(client, plan).json()
    assert client.get('/api/admin/billing').status_code == 403
    assert client.post('/api/admin/billing/orders/' + order['id'] + '/refund', json={'reason': 'test reason'}).status_code == 403
    other = TestClient(app)
    assert other.get('/api/billing').status_code == 401
    other.cookies.update(client.cookies)
    # Replace the owner in the synthetic session without granting an admin role.
    uid = app.state.store.create_user('different-user', test_platform.PASSWORD)
    with app.state.store.db() as db:
        db.execute('UPDATE sessions SET user_id=?', (uid,))
        # Current requires a verified email as well.
        email = db.execute('SELECT * FROM user_emails LIMIT 1').fetchone()
        db.execute('UPDATE user_emails SET user_id=? WHERE user_id=?', (uid, email['user_id']))
    assert other.get('/api/billing/orders/' + order['id'] + '/qr').status_code == 404


def test_webhook_origin_exemption_is_narrow_and_verified(paid):
    client, app, _, _, plan = paid
    order = checkout(client, plan).json()
    anonymous = TestClient(app)
    response = anonymous.post('/api/payments/notify/alipay', content=json.dumps(result(order)), headers={'synthetic-signature': 'invalid'})
    assert response.status_code == 400
    assert app.state.billing.order(order['id'])['state'] == 'pending'
    response = anonymous.post('/api/payments/notify/alipay', content=json.dumps(result(order)), headers={'synthetic-signature': 'valid'})
    assert response.status_code == 200 and response.text == 'success'
    assert anonymous.post('/api/billing/orders', json={}).status_code == 403
    assert anonymous.post('/api/payments/notify/unknown', content='x').status_code == 403
    assert anonymous.post('/api/payments/notify/alipay', content='x' * 33000).status_code == 413
    assert app.state.billing.order(order['id'])['state'] == 'paid'


def test_full_refund_once_and_unknown_result_remains_suspended(paid):
    client, app, owner, admin, plan = paid
    order = checkout(client, plan).json()
    settle(app, order)
    adapter = app.state.billing.gateways.get('alipay')
    adapter.refund.side_effect = PaymentError('PRIVATE refund error')
    r = asyncio.run(app.state.billing.request_refund(order['id'], admin['user_id'], 'Synthetic reason'))
    assert r['state'] == 'refunding' and 'PRIVATE' not in str(r)
    assert client.get('/api/billing').json()['current'] is None
    with pytest.raises(Rejected):
        asyncio.run(app.state.billing.request_refund(order['id'], admin['user_id'], 'Synthetic duplicate'))
    assert adapter.refund.call_count == 1
    adapter.query_refund.return_value = True
    final = asyncio.run(app.state.billing.reconcile(order['id']))
    assert final['state'] == 'refunded'
    assert client.get('/api/billing').json()['current'] is None
    assert app.state.store.user(owner['user_id'])['active']


def test_payment_callback_during_refund_does_not_restore_access(paid):
    client, app, _, admin, plan = paid
    order = checkout(client, plan).json()
    settle(app, order)
    asyncio.run(app.state.billing.request_refund(order['id'], admin['user_id'], 'Synthetic refund'))
    settle(app, order)
    assert app.state.billing.order(order['id'])['state'] == 'refunded'
    assert client.get('/api/billing').json()['current'] is None


def test_changed_terms_require_new_consent(paid):
    client, app, _, _, plan = paid
    app.state.store.update_settings({'billing': {'enabled': True, 'terms': 'New refund terms', 'support': 'Support'}})
    assert checkout(client, plan).status_code == 409
    assert app.state.billing.gateways.get('alipay').create.call_count == 0


def test_profile_change_wrong_provider_and_expired_qr_fail_closed(paid):
    client, app, _, _, plan = paid
    order = checkout(client, plan).json()
    with pytest.raises(PaymentError):
        app.state.billing.settle('wechat', 'synthetic-profile-wechat', result(order))
    with pytest.raises(PaymentError):
        app.state.billing.settle('alipay', 'different-profile', result(order))
    with app.state.store.db() as db:
        db.execute('UPDATE billing_orders SET expires=0 WHERE id=?', (order['id'],))
    assert client.get('/api/billing/orders/' + order['id'] + '/qr').status_code == 409
    adapter = app.state.billing.gateways.get('alipay')
    adapter.profile = 'changed-merchant'
    assert client.post('/api/billing/orders/' + order['id'] + '/refresh').status_code == 409


def test_paid_user_pause_and_history_deletion_do_not_grant_free_access(paid):
    client, app, owner, _, plan = paid
    order = checkout(client, plan).json()
    settle(app, order)
    app.state.store.save_history(owner['user_id'], 'speaker-a', 'synthetic-history', 'question', 'answer')
    client.delete('/api/history')
    assert client.get('/api/billing').json()['current']
    with app.state.store.db() as db:
        db.execute('UPDATE users SET active=0 WHERE id=?', (owner['user_id'],))
    with pytest.raises(Rejected) as error:
        app.state.store.reserve(owner['user_id'], 'speaker-a', 'speaker', 'paused')
    assert error.value.status == 403


def test_payments_and_qr_metadata_do_not_contain_plain_credentials(paid):
    client, app, owner, _, plan = paid
    order = checkout(client, plan).json()
    with app.state.store.db() as db:
        dump = '\n'.join(db.iterdump())
    assert 'https://qr.alipay.com/synthetic' not in dump
    info = client.get('/api/billing').json()
    assert 'password' not in str(info) and 'secret' not in str(info)
    assert app.state.billing.order(order['id'])['user_id'] == owner['user_id']


def test_webpay_entry_is_owned_and_has_restricted_form_action(paid):
    client, app, _, _, plan = paid
    adapter = app.state.billing.gateways.get('alipay')
    adapter.page_payment = True
    adapter.create.return_value = ''
    adapter.payment_form = lambda order: '<form method="post" action="https://openapi.alipay.com/gateway.do"></form>'
    order = checkout(client, plan).json()
    assert order['page_payment_available'] and not order['qr_available']
    assert client.get('/api/billing/orders/' + order['id'] + '/qr').status_code == 409
    form = client.get('/api/billing/orders/' + order['id'] + '/pay')
    assert form.status_code == 200 and 'form-action' in form.headers['Content-Security-Policy']
    assert form.headers['Content-Security-Policy'].endswith("form-action 'self' https://openapi.alipay.com")
    login(client)
    invite_user(client, 'second-payer')
    assert client.get('/api/billing/orders/' + order['id'] + '/pay').status_code == 404


def test_webpay_persistent_notice_conflict_and_refund_event_never_grants(paid):
    client, app, _, _, plan = paid
    adapter = app.state.billing.gateways.get('alipay')
    adapter.page_payment = True
    order = checkout(client, plan).json()
    notice = result(order, notify_id='synthetic-notice', event_type='TRADE_SUCCESS:paid')
    app.state.billing.settle('alipay', adapter.profile, notice)
    app.state.billing.settle('alipay', adapter.profile, notice)
    with app.state.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM billing_notify_events').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM billing_grants').fetchone()[0] == 1
    other = checkout(client, plan).json()
    with pytest.raises(PaymentError):
        app.state.billing.settle('alipay', adapter.profile, {**notice, 'id': other['id']})
    app.state.billing.settle('alipay', adapter.profile, result(other, state='refund_review'))
    assert app.state.billing.order(other['id'])['state'] == 'pending'
    app.state.billing.settle('alipay', adapter.profile, result(order, state='closed'))
    assert app.state.billing.order(order['id'])['state'] == 'refund_review'
    assert client.get('/api/billing').json()['current'] is None


def test_payment_return_shell_public_but_no_unverified_order_lookup(paid):
    client, app, _, _, _ = paid
    adapter = app.state.billing.gateways.get('alipay')
    adapter.page_payment = True

    def reject(params):
        raise PaymentError('Synthetic signature rejection')

    adapter.verify_params = reject
    assert client.get('/payments/alipay/return').status_code == 200
    response = client.get('/api/billing/alipay-return?out_trade_no=untrusted&trade_status=TRADE_SUCCESS')
    assert response.status_code == 400 and '签名' in response.json()['error']
    client.cookies.clear()
    assert client.get('/payments/alipay/return').status_code == 200
    assert client.get('/api/billing/alipay-return?out_trade_no=untrusted').status_code == 401
