"""Rate limits are shared across entry points and survive runtime/process replacement."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from platform_app.app import create_app
from platform_app.runtime import Runtime
from platform_app.store import Store, Rejected
from platform_app.xiaomi_access import XiaomiAccess, XiaomiSession, RateLimited, retry_delay
from test_account_lifecycle import account_store as account_store_fixture, mock_auth, complete
from test_platform import CFG, PASSWORD, QuietRuntime, FakeMailer, prepare_mail, login, bound

account_store = account_store_fixture


@pytest.fixture
def clock(monkeypatch):
    value = [1000.0]
    monkeypatch.setattr('platform_app.xiaomi_access.time', SimpleNamespace(time=lambda: value[0]))
    return value


class Response:

    def __init__(self, status=200, code=0, headers=None, broken=False):
        self.status, self.code, self.headers, self.broken = status, code, headers or {}, broken

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self, **kwargs):
        if self.broken:
            raise ValueError('PRIVATE-UPSTREAM-BODY')
        return {'code': self.code, 'data': [], 'message': 'PRIVATE-UPSTREAM-BODY'}


class Session:

    def __init__(self, response):
        self.response, self.calls = response, []

    def get(self, url, **kwargs):
        self.calls.append(('GET', url))
        return self.response

    def post(self, url, **kwargs):
        self.calls.append(('POST', url))
        return self.response

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        return self.response


@pytest.mark.parametrize('header,expected', [('7200', 7200), ('0', 0), ('bad', 0), ('-1', 0), (None, 0)])
def test_retry_after_values(header, expected):
    assert retry_delay(header, 1000) == expected


def test_retry_after_date_is_utc_and_respects_long_server_delay(account_store, clock):
    store, uid = account_store
    access = XiaomiAccess(store)
    header = format_datetime(datetime.fromtimestamp(10000, timezone.utc), usegmt=True)
    assert retry_delay(header, 1000) == 9000
    assert access.limited(uid, header)['retry_after'] == 9000


@pytest.mark.parametrize('broken', [True, False])
def test_429_pauses_dependency_and_direct_requests_without_leaking_bodies(account_store, clock, broken):
    store, uid = account_store
    access = XiaomiAccess(store)
    raw = Session(Response(429, -127, broken=broken))
    guarded = XiaomiSession(raw, access, uid)

    async def run():
        with pytest.raises(RateLimited) as first:
            async with guarded.post('https://api.io.mi.com/app/home/device_list'):
                pytest.fail('429 reached caller as an ordinary response')
        assert first.value.cooldown['retry_after'] == 1800
        assert 'PRIVATE' not in str(first.value)
        for method, url in (('get', 'https://userprofile.mina.mi.com/device_profile/v2/conversation'),
                            ('request', 'https://account.xiaomi.com/pass/serviceLoginAuth2')):
            with pytest.raises(RateLimited):
                pending = guarded.request('POST', url) if method == 'request' else guarded.get(url)
                async with pending:
                    pytest.fail('Cooldown was bypassed')
        assert len(raw.calls) == 1
        assert access.status(uid)['upstream_code'] == (None if broken else -127)
    asyncio.run(run())


def test_pause_is_written_before_error_body_is_read(account_store, clock):
    store, uid = account_store
    access = XiaomiAccess(store)

    class CancelledBody(Response):

        async def json(self, **kwargs):
            assert access.status(uid)['http_status'] == 429
            raise asyncio.CancelledError

    async def run():
        with pytest.raises(asyncio.CancelledError):
            async with XiaomiSession(Session(CancelledBody(429)), access, uid).get('https://api.io.mi.com/app/test'):
                pass
        assert access.status(uid)['retry_after'] == 1800
    asyncio.run(run())


def test_restart_and_credential_changes_cannot_reset_pause(account_store, clock):
    store, uid = account_store
    first = XiaomiAccess(store)
    first.limited(uid, code=-127)
    credentials = store.account(uid)['credentials']
    store.save_account(uid, credentials['username'], 'new-synthetic-password', credentials['token'])
    reopened = Store(store.root, store.vault)
    second = Runtime(reopened, deepcopy(CFG))
    assert second.last_check(uid)['state'] == 'rate_limited'
    assert second.connection_state(uid) == 'rate_limited'
    with pytest.raises(RateLimited):
        second.begin_replace(uid, 'synthetic-other', 'synthetic')
    assert second.xiaomi.status(uid)['retry_after'] == 1800


def test_cooldown_is_per_account_and_inflight_bursts_do_not_escalate(account_store, clock):
    store, uid = account_store
    access = XiaomiAccess(store)
    access.limited(uid)
    clock[0] += 5
    access.limited(uid)
    assert access.status(uid)['failures'] == 1
    access.ensure('synthetic-unrelated-user')
    with pytest.raises(RateLimited):
        access.ensure(uid)


def test_single_recovery_lease_is_shared_between_gate_instances(account_store, clock):
    store, uid = account_store
    first, second = XiaomiAccess(store), XiaomiAccess(store)
    first.limited(uid)
    clock[0] += 1800
    permit = first.enter_probe(uid)
    with pytest.raises(RateLimited):
        second.enter_probe(uid, manual=True)
    with pytest.raises(RateLimited):
        first.ensure(uid)  # Device workers never acquire the recovery permit.
    first.finish_probe(permit, 'valid')
    assert first.status(uid) is None
    second.ensure(uid)


def test_backoff_escalates_then_requires_manual_check(account_store, clock):
    store, uid = account_store
    access = XiaomiAccess(store)

    async def run():
        assert access.limited(uid)['retry_after'] == 1800
        for expected in (3600, 7200):
            clock[0] = access.status(uid)['retry_at']
            async with access.probe(uid):
                access.ensure(uid)
                assert access.limited(uid)['retry_after'] == expected
        assert access.status(uid)['manual_required']
        clock[0] = access.status(uid)['retry_at']
        with pytest.raises(RateLimited):
            access.enter_probe(uid)
        async with access.probe(uid, manual=True) as permit:
            access.ensure(uid)
            access.finish_probe(permit, 'valid')
        assert access.status(uid) is None
    asyncio.run(run())


def test_stale_valid_probe_cannot_clear_concurrent_429(account_store, clock):
    store, uid = account_store
    access = XiaomiAccess(store)
    old = access.enter_probe(uid)
    access.limited(uid)
    access.finish_probe(old, 'valid')
    assert access.status(uid)['failures'] == 1


def test_cancelled_or_failed_recovery_is_not_retried_every_second(account_store, clock):
    store, uid = account_store
    access = XiaomiAccess(store)

    async def run():
        access.limited(uid)
        clock[0] += 1800
        with pytest.raises(asyncio.CancelledError):
            async with access.probe(uid):
                raise asyncio.CancelledError
        assert access.status(uid)['retry_after'] == 300
    asyncio.run(run())


def test_auth_expiration_does_not_reset_backoff_history(account_store, clock):
    store, uid = account_store
    access = XiaomiAccess(store)
    access.limited(uid)
    clock[0] += 1800
    permit = access.enter_probe(uid)
    access.finish_probe(permit, 'expired')
    assert access.status(uid) is None
    access.ensure(uid)
    assert access.limited(uid)['failures'] == 2


def test_runtime_cooldown_blocks_refresh_rebind_model_and_automatic_work(account_store, clock, monkeypatch):
    store, uid = account_store

    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        runtime.xiaomi.limited(uid)
        probe = AsyncMock(return_value='valid')
        monkeypatch.setattr(runtime, '_probe_token', probe)
        for operation in (runtime.refresh(uid), runtime.begin_reverify(uid, 'PRIVATE-PASSWORD')):
            with pytest.raises(RateLimited):
                await operation
        assert (await runtime.check_login(uid))['state'] == 'rate_limited'
        runtime.schedule_binding_checks()
        assert not runtime.binding_probes and not probe.await_count
        assert await runtime.handle(uid, 'speaker-a', 'synthetic', 'speaker', 'synthetic') is None
        assert not store.summary(uid)['requests']
        clock[0] += 1800
        runtime.schedule_binding_checks()
        await asyncio.gather(*runtime.binding_probes.values())
        assert probe.await_count == 1 and runtime.xiaomi.status(uid) is None
    asyncio.run(run())


def test_explicit_login_429_is_not_masked_as_password_failure(account_store, monkeypatch, clock):
    store, uid = account_store
    before = store.account(uid)['credentials']
    raw = Session(Response(429, -127))

    class Account:

        def __init__(self, session, *args, **kwargs):
            self.session, self.token = session, before['token']

        async def login(self, sid):
            try:
                async with self.session.get('https://account.xiaomi.com/pass/serviceLogin'):
                    return True
            except Exception:
                return False  # Reproduce dependency swallowing authentication exceptions.

    class ContextSession(Session):

        async def __aenter__(self):
            return raw

        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr('platform_app.runtime.aiohttp.ClientSession', lambda **kwargs: ContextSession(Response()))
    monkeypatch.setattr('platform_app.runtime.SafeAccount', Account)

    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        monkeypatch.setattr(runtime, '_probe_token', AsyncMock(return_value='expired'))
        await runtime.begin_reverify(uid, 'synthetic')
        result = await complete(runtime, uid)
        assert result['state'] == 'failed' and result['diagnostic']['kind'] == 'rate_limited'
        assert result['diagnostic']['http_status'] == 429 and result['diagnostic']['upstream_code'] == -127
        assert '小米请求受限' in result['message']
        assert store.account(uid)['credentials'] == before and store.devices(uid)[0]['enabled']
    asyncio.run(run())


def test_unbound_account_can_only_retry_explicitly_after_pause(account_store, clock, monkeypatch):
    store, uid = account_store
    with store.db() as db:
        db.execute('DELETE FROM accounts WHERE user_id=?', (uid,))
    captured = mock_auth(monkeypatch, identity='synthetic-new')

    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        runtime.xiaomi.limited(uid)
        with pytest.raises(RateLimited):
            runtime.begin_login(uid, 'synthetic', 'synthetic')
        assert not captured
        clock[0] += 1800
        runtime.begin_login(uid, 'synthetic', 'synthetic')
        result = await complete(runtime, uid)
        assert result['state'] == 'completed' and runtime.xiaomi.status(uid) is None
    asyncio.run(run())


def test_recovery_api_requires_auth_and_does_not_bypass_cooldown(tmp_path, monkeypatch, clock):
    monkeypatch.setenv('PLATFORM_ADMIN_PASSWORD', PASSWORD)
    for key in ('PLATFORM_KEY_FILE', 'PLATFORM_PUBLIC_ORIGIN', 'PLATFORM_COOKIE_SECURE'):
        monkeypatch.delenv(key, raising=False)
    app = create_app(tmp_path / 'web', deepcopy(CFG), runtime_factory=QuietRuntime, mailer=FakeMailer())
    probe = AsyncMock(return_value='valid')
    monkeypatch.setattr(app.state.runtime, '_probe_token', probe)
    with TestClient(app) as client:
        client.headers['Origin'] = 'http://testserver'
        assert client.post('/api/xiaomi/recover').status_code == 401
        prepare_mail(client, app)
        me = login(client)
        bound(app.state.store, me['user_id'])
        app.state.runtime.xiaomi.limited(me['user_id'])
        assert client.post('/api/xiaomi/recover').json()['state'] == 'rate_limited'
        assert not probe.await_count
        assert client.post('/api/devices/refresh').status_code == 429
        assert client.post('/api/xiaomi/reverify', json={'password': 'synthetic'}).status_code == 429
        clock[0] += 1800
        assert client.post('/api/xiaomi/recover').json()['state'] == 'valid'
        assert probe.await_count == 1
        assert client.post('/api/xiaomi/recover').status_code == 409


def test_speech_check_429_has_safe_codes_and_preserves_binding(account_store, monkeypatch, clock):
    store, uid = account_store
    before = store.account(uid)['credentials']
    raw = Session(Response(429, -127))

    class Account:

        def __init__(self, *args, **kwargs):
            self.token = {**before['token'], 'deviceId': 'synthetic', 'xiaomiio': ['c2VjdXJpdHk=', 'synthetic']}

        async def login(self, sid):
            return True

    class ContextSession(Session):

        async def __aenter__(self):
            return raw

        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr('platform_app.runtime.aiohttp.ClientSession', lambda **kwargs: ContextSession(Response()))
    monkeypatch.setattr('platform_app.runtime.SafeAccount', Account)
    monkeypatch.setattr('platform_app.runtime.MiNAService', lambda account: SimpleNamespace(
        device_list=AsyncMock(return_value=[])))

    async def run():
        cfg = deepcopy(CFG)
        cfg['tts'] = {'method': 'miot'}
        runtime = Runtime(store, cfg)
        monkeypatch.setattr(runtime, '_probe_token', AsyncMock(return_value='expired'))
        await runtime.begin_reverify(uid, 'synthetic')
        result = await complete(runtime, uid)
        assert result['diagnostic'] == {'stage': 'speech_check', 'kind': 'rate_limited',
                                        'http_status': 429, 'upstream_code': -127}
        assert len(raw.calls) == 1 and 'PRIVATE' not in str(result)
        assert store.account(uid)['credentials'] == before and store.devices(uid)[0]['enabled']
    asyncio.run(run())


def test_inflight_valid_check_cannot_hide_new_rate_limit(account_store, monkeypatch, clock):
    store, uid = account_store
    before = store.account(uid)

    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        entered, released = asyncio.Event(), asyncio.Event()

        async def probe(*args):
            entered.set()
            await released.wait()
            return 'valid'
        monkeypatch.setattr(runtime, '_probe_token', probe)
        pending = asyncio.create_task(runtime.check_login(uid))
        await entered.wait()
        runtime.xiaomi.limited(uid)
        released.set()
        assert (await pending)['state'] == 'rate_limited'
        assert runtime.connection_state(uid) == 'rate_limited'
        assert store.account(uid) == before
    asyncio.run(run())


def test_expired_recovery_lease_cannot_be_cleared_by_old_process(account_store, clock):
    store, uid = account_store
    access = XiaomiAccess(store)
    access.limited(uid)
    clock[0] += 1800
    old = access.enter_probe(uid)
    clock[0] += 91
    replacement = XiaomiAccess(store)
    new = replacement.enter_probe(uid)
    access.finish_probe(old, 'valid')
    assert replacement.status(uid)['recovering']
    replacement.finish_probe(new, 'valid')
    assert replacement.status(uid) is None


def test_recovery_spreads_requests_then_ends_warmup(account_store, clock):
    store, uid = account_store
    access = XiaomiAccess(store)
    access.limited(uid)
    clock[0] += 1800
    permit = access.enter_probe(uid)
    access.finish_probe(permit, 'valid')
    raw = Session(Response())
    guarded = XiaomiSession(raw, access, uid)

    async def run():
        for _ in range(2):
            async with guarded.get('https://api.io.mi.com/app/test'):
                pass
        assert access.row(uid)['lease_until'] == clock[0] + 2
        clock[0] += 121
        async with guarded.get('https://api.io.mi.com/app/test'):
            pass
        assert len(raw.calls) == 3 and access.row(uid) is None
    asyncio.run(run())


def test_simultaneous_manual_check_reuses_completed_recovery(account_store, monkeypatch, clock):
    store, uid = account_store

    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        runtime.xiaomi.limited(uid)
        clock[0] += 1800
        entered, released = asyncio.Event(), asyncio.Event()

        async def probe(*args):
            entered.set()
            await released.wait()
            return 'valid'

        probe_mock = AsyncMock(side_effect=probe)
        monkeypatch.setattr(runtime, '_probe_token', probe_mock)
        automatic = asyncio.create_task(runtime.check_login(uid))
        await entered.wait()
        manual = asyncio.create_task(runtime.check_login(uid, manual_recovery=True))
        await asyncio.sleep(0)
        released.set()
        assert (await automatic)['state'] == 'valid'
        assert (await manual)['state'] == 'valid'
        assert probe_mock.await_count == 1
    asyncio.run(run())


def test_queued_speaker_request_is_cancelled_before_model_when_rate_limited(account_store, monkeypatch, clock):
    from test_capacity import eventually
    store, uid = account_store

    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        runtime.semaphore.resize(1)
        await runtime.semaphore.acquire()
        monkeypatch.setattr(runtime, 'route', lambda *args: True)
        complete_model = AsyncMock(side_effect=AssertionError('Model must not be called'))
        monkeypatch.setattr(runtime.services, 'complete', complete_model)
        speaker = SimpleNamespace(tts=AsyncMock(return_value=True))
        pending = asyncio.create_task(runtime.handle(uid, 'speaker-a', 'synthetic', 'speaker', 'queued', speaker))
        await eventually(lambda: runtime.model_waiting == 1)
        runtime.xiaomi.limited(uid)
        runtime.semaphore.release()
        with pytest.raises(Rejected):
            await pending
        assert not complete_model.await_count and not speaker.tts.await_count
        with store.db() as db:
            row = db.execute('SELECT status,started FROM requests').fetchone()
            assert tuple(row) == ('cancelled', 0)
    asyncio.run(run())
