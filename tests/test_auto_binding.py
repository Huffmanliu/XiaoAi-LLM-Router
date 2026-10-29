"""Automatic saved-token checks, bounded work, and no unsolicited login/OTP."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from platform_app.runtime import Runtime
from platform_app.store import Rejected
from test_account_lifecycle import account_store as account_store_fixture
from test_account_lifecycle import complete, mock_auth
from test_capacity import eventually
from test_platform import CFG, PASSWORD, bound

account_store = account_store_fixture


async def settle(runtime):
    await asyncio.gather(*runtime.binding_probes.values())


def test_monitor_runs_without_browser_or_enabled_speaker(account_store, monkeypatch):
    store, uid = account_store
    store.select_devices(uid, [])
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        monkeypatch.setattr(runtime, '_probe_token', AsyncMock(return_value='expired'))
        monkeypatch.setattr('platform_app.runtime.SafeAccount', lambda *a, **k: pytest.fail('Unexpected login'))
        await runtime.start()
        try:
            await eventually(lambda: store.account(uid)['state'] == 'needs_verification')
            assert not runtime.workers and not runtime.jobs and not store.summary(uid)['requests']
        finally:
            await runtime.close()
        assert all(t.done() for t in runtime.background)
    asyncio.run(run())


def test_expiration_is_confirmed_without_login_or_codes_and_not_retried_forever(account_store, monkeypatch):
    store, uid = account_store
    before = store.account(uid)['credentials']
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        probe = AsyncMock(return_value='expired')
        monkeypatch.setattr(runtime, '_probe_token', probe)
        monkeypatch.setattr('platform_app.runtime.SafeAccount', lambda *a, **k: pytest.fail('Unexpected login'))
        runtime.schedule_binding_checks()
        await settle(runtime)
        assert store.account(uid)['state'] == 'needs_verification'
        assert store.account(uid)['credentials'] == before
        assert store.devices(uid)[0]['enabled']
        assert runtime.last_check(uid)['state'] == 'expired' and not runtime.jobs
        runtime.schedule_binding_checks()
        assert not runtime.binding_probes and probe.await_count == 1
        assert 'version' not in runtime.last_check(uid) and 'identity' not in runtime.last_check(uid)
    asyncio.run(run())


@pytest.mark.parametrize('failure', [TimeoutError, aiohttp.ClientConnectionError])
def test_transient_failure_keeps_binding_retries_after_one_minute_and_recovers(account_store, monkeypatch, failure):
    store, uid = account_store
    before = store.account(uid)
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        probe = AsyncMock(side_effect=[failure('synthetic network failure'), 'valid'])
        monkeypatch.setattr(runtime, '_probe_token', probe)
        runtime.schedule_binding_checks()
        await settle(runtime)
        assert store.account(uid)['state'] == 'ready'
        assert store.account(uid)['updated'] == before['updated']
        assert runtime.connection_state(uid) == 'connection_error'
        runtime.schedule_binding_checks()
        assert not runtime.binding_probes and probe.await_count == 1
        runtime.checks[uid]['checked_at'] -= 61
        runtime.schedule_binding_checks()
        await settle(runtime)
        assert probe.await_count == 2 and runtime.connection_state(uid) != 'connection_error'
        assert store.account(uid)['credentials'] == before['credentials'] and not runtime.jobs
    asyncio.run(run())


def test_normal_checks_are_low_frequency_and_polling_error_shortens_interval(account_store, monkeypatch):
    store, uid = account_store
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        probe = AsyncMock(return_value='valid')
        monkeypatch.setattr(runtime, '_probe_token', probe)
        runtime.schedule_binding_checks()
        await settle(runtime)
        runtime.checks[uid]['checked_at'] -= 61
        runtime.schedule_binding_checks()
        assert not runtime.binding_probes
        runtime.accounts[uid] = {'state': 'connection_error', 'account': SimpleNamespace(token=None)}
        runtime.schedule_binding_checks()
        await settle(runtime)
        assert runtime.connection_state(uid) == 'ready'
        assert runtime.accounts[uid]['account'].token is not None
        runtime.checks[uid]['checked_at'] -= 301
        runtime.schedule_binding_checks()
        await settle(runtime)
        assert probe.await_count == 3
    asyncio.run(run())


def test_result_for_old_password_cannot_overwrite_same_account_rebind(account_store, monkeypatch):
    store, uid = account_store
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        entered, release = asyncio.Event(), asyncio.Event()
        async def probe(*args):
            entered.set()
            await release.wait()
            return 'expired'
        monkeypatch.setattr(runtime, '_probe_token', probe)
        runtime.schedule_binding_checks()
        await entered.wait()
        old = store.account(uid)['credentials']
        store.save_account(uid, old['username'], 'new-synthetic-password', old['token'])
        release.set()
        await settle(runtime)
        assert store.account(uid)['state'] == 'ready'
        assert store.account(uid)['credentials']['password'] == 'new-synthetic-password'
        assert runtime.last_check(uid) is None
    asyncio.run(run())


def test_unbind_during_probe_discards_old_result(account_store, monkeypatch):
    store, uid = account_store
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        entered, release = asyncio.Event(), asyncio.Event()
        async def probe(*args):
            entered.set()
            await release.wait()
            return 'expired'
        monkeypatch.setattr(runtime, '_probe_token', probe)
        runtime.schedule_binding_checks()
        await entered.wait()
        await runtime.unbind(uid)
        release.set()
        await settle(runtime)
        assert store.account(uid) is None and runtime.last_check(uid) is None
    asyncio.run(run())


def test_probe_work_is_bounded_four_and_does_not_decrypt_all_accounts(account_store, monkeypatch):
    store, uid = account_store
    for i in range(5):
        user = store.create_user(f'user{i}', PASSWORD)
        bound(store, user, did=f'device{i}', identity=f'account{i}')
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        release = asyncio.Event()
        async def probe(*args):
            await release.wait()
            return 'valid'
        probe_mock = AsyncMock(side_effect=probe)
        monkeypatch.setattr(runtime, '_probe_token', probe_mock)
        original = store.account
        reads = []
        def account(user):
            reads.append(user)
            return original(user)
        monkeypatch.setattr(store, 'account', account)
        runtime.schedule_binding_checks()
        runtime.schedule_binding_checks()
        await eventually(lambda: probe_mock.await_count == 4)
        assert len(runtime.binding_probes) == 4 and len(set(reads)) == 4
        release.set()
        await settle(runtime)
        runtime.schedule_binding_checks()
        await settle(runtime)
        assert probe_mock.await_count == 6
        runtime.schedule_binding_checks()
        assert not runtime.binding_probes
    asyncio.run(run())


def test_manual_inflight_probe_is_not_duplicated_by_monitor(account_store, monkeypatch):
    store, uid = account_store
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        release, entered = asyncio.Event(), asyncio.Event()
        async def probe(*args):
            entered.set()
            await release.wait()
            return 'valid'
        probe_mock = AsyncMock(side_effect=probe)
        monkeypatch.setattr(runtime, '_probe_token', probe_mock)
        task = asyncio.create_task(runtime.check_login(uid))
        await entered.wait()
        runtime.schedule_binding_checks()
        assert not runtime.binding_probes
        release.set()
        await task
        runtime.schedule_binding_checks()
        assert not runtime.binding_probes and probe_mock.await_count == 1
    asyncio.run(run())


def test_paused_accounts_are_skipped_and_probe_shutdown_is_clean(account_store, monkeypatch):
    store, uid = account_store
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        probe = AsyncMock(side_effect=lambda *args: None)
        monkeypatch.setattr(runtime, '_probe_token', probe)
        with store.db() as db:
            db.execute('UPDATE users SET active=0 WHERE id=?', (uid,))
        runtime.schedule_binding_checks()
        assert not runtime.binding_probes
        with store.db() as db:
            db.execute('UPDATE users SET active=1 WHERE id=?', (uid,))
        wait = asyncio.Event()
        async def blocked(*args):
            await wait.wait()
            return 'valid'
        monkeypatch.setattr(runtime, '_probe_token', blocked)
        runtime.schedule_binding_checks()
        await asyncio.sleep(0)
        tasks = list(runtime.binding_probes.values())
        await runtime.close()
        assert all(t.cancelled() for t in tasks) and store.account(uid)['state'] == 'ready'
    asyncio.run(run())


@pytest.mark.parametrize('status,body', [
    (403, {'code': -1, 'message': 'authorization denied'}),
    (200, {'code': -1, 'message': 'authoritative service unavailable'}),
    (503, {'code': 401}),
    (429, {'code': -1, 'message': 'invalid token'}),
    (200, {'code': False}),
])
def test_permission_limit_and_server_errors_do_not_force_password_entry(status, body):
    assert Runtime._probe_result(status, body) == 'unknown'


def test_unexpected_probe_error_is_unknown_and_throttled(account_store, monkeypatch):
    store, uid = account_store
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        probe = AsyncMock(side_effect=RuntimeError('PRIVATE-UPSTREAM-BODY'))
        monkeypatch.setattr(runtime, '_probe_token', probe)
        runtime.schedule_binding_checks()
        await settle(runtime)
        assert runtime.last_check(uid)['state'] == 'unknown'
        assert 'PRIVATE' not in str(runtime.last_check(uid))
        assert store.account(uid)['state'] == 'ready'
        runtime.schedule_binding_checks()
        assert not runtime.binding_probes and probe.await_count == 1
    asyncio.run(run())


def test_missing_in_memory_token_is_not_itself_proof_of_expiration(account_store, monkeypatch):
    store, uid = account_store
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        ctx = {'account': SimpleNamespace(token=None), 'state': 'ready', 'speaker': SimpleNamespace(
            recent_asks=AsyncMock(side_effect=RuntimeError('synthetic poll error')),
            mina=SimpleNamespace(device_list=AsyncMock(return_value=[])))}
        monkeypatch.setattr(runtime, 'context', AsyncMock(return_value=ctx))
        with pytest.raises(Rejected) as caught:
            await runtime.refresh(uid)
        assert caught.value.status == 503 and store.account(uid)['state'] == 'ready'
        ctx['state'] = 'ready'
        task = asyncio.create_task(runtime.device_loop(uid, 'speaker-a'))
        await eventually(lambda: ctx['speaker'].recent_asks.await_count > 0)
        await eventually(lambda: ctx['state'] == 'connection_error')
        assert store.account(uid)['state'] == 'ready'
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize('unexpected', [False, True])
def test_continuous_failure_slows_after_two_minutes_and_recovers(account_store, monkeypatch, unexpected):
    store, uid = account_store
    clock = [1000.0]
    monkeypatch.setattr('platform_app.runtime.time', SimpleNamespace(time=lambda: clock[0]))
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        probe = AsyncMock(side_effect=RuntimeError('PRIVATE') if unexpected else None, return_value='unknown')
        monkeypatch.setattr(runtime, '_probe_token', probe)
        for at in (1000, 1060):
            clock[0] = at
            runtime.schedule_binding_checks()
            await settle(runtime)
            assert runtime.last_check(uid)['retry_interval'] == 60
            assert not runtime.last_check(uid)['can_reverify']
        clock[0] = 1120
        runtime.schedule_binding_checks()
        assert not runtime.binding_probes and probe.await_count == 2
        check = runtime.last_check(uid)
        assert check['unknown_since'] == 1000 and check['can_reverify']
        assert check['retry_interval'] == 300 and check['next_check_at'] == 1360
        clock[0] = 1359
        runtime.schedule_binding_checks()
        assert not runtime.binding_probes
        clock[0] = 1360
        runtime.schedule_binding_checks()
        await settle(runtime)
        assert probe.await_count == 3 and runtime.last_check(uid)['unknown_since'] == 1000
        probe.side_effect, probe.return_value = None, 'valid'
        clock[0] = 1660
        runtime.schedule_binding_checks()
        await settle(runtime)
        assert runtime.last_check(uid)['state'] == 'valid'
        assert not runtime.last_check(uid)['can_reverify']
        assert 'unknown_since' not in runtime.last_check(uid)
        probe.return_value = 'unknown'
        clock[0] = 1960
        await runtime.check_login(uid)
        assert runtime.last_check(uid)['unknown_since'] == 1960
        assert runtime.last_check(uid)['retry_interval'] == 60
        assert not runtime.jobs and store.devices(uid)[0]['enabled']
    asyncio.run(run())


@pytest.mark.parametrize('success', [True, False])
def test_user_can_rebind_persistent_unknown_without_losing_selected_devices(account_store, monkeypatch, success):
    store, uid = account_store
    before = store.account(uid)['credentials']
    captured = mock_auth(monkeypatch, identity=before['token']['userId'], success=success)
    monkeypatch.setattr('platform_app.runtime.MiNAService', lambda account: SimpleNamespace(
        device_list=AsyncMock(return_value=[{'deviceID': 'speaker-a', 'hardware': 'L05B', 'name': 'Play'},
                                          {'deviceID': 'new-device', 'hardware': 'L05B', 'name': 'New'}])))
    clock = [1000.0]
    monkeypatch.setattr('platform_app.runtime.time', SimpleNamespace(time=lambda: clock[0]))
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        monkeypatch.setattr(runtime, '_probe_token', AsyncMock(return_value='unknown'))
        await runtime.check_login(uid)
        with pytest.raises(Rejected) as exc:
            await runtime.begin_reverify(uid, 'synthetic-new-password')
        assert exc.value.status == 503 and not captured
        clock[0] += 120
        await runtime.begin_reverify(uid, 'synthetic-new-password')
        assert (await complete(runtime, uid))['state'] == ('completed' if success else 'failed')
        assert next(d for d in store.devices(uid) if d['id'] == 'speaker-a')['enabled']
        if success:
            assert not next(d for d in store.devices(uid) if d['id'] == 'new-device')['enabled']
            assert store.account(uid)['credentials']['password'] == 'synthetic-new-password'
            assert runtime.last_check(uid)['state'] == 'valid'
        else:
            assert store.account(uid)['credentials'] == before
    asyncio.run(run())
    assert captured == [(before['username'], 'synthetic-new-password')]


def test_credential_change_does_not_inherit_unknown_backoff(account_store, monkeypatch):
    store, uid = account_store
    clock = [1000.0]
    monkeypatch.setattr('platform_app.runtime.time', SimpleNamespace(time=lambda: clock[0]))
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        monkeypatch.setattr(runtime, '_probe_token', AsyncMock(return_value='unknown'))
        await runtime.check_login(uid)
        clock[0] += 120
        assert runtime.last_check(uid)['can_reverify']
        saved = store.account(uid)['credentials']
        store.save_account(uid, saved['username'], 'synthetic-new-password', saved['token'])
        await runtime.check_login(uid)
        assert not runtime.last_check(uid)['can_reverify']
        assert runtime.last_check(uid)['unknown_since'] == clock[0]
    asyncio.run(run())


def test_device_reads_back_off_and_pause_can_end_on_rebind(account_store, monkeypatch):
    store, uid = account_store
    clock, delays = [1000.0], []
    monkeypatch.setattr('platform_app.runtime.time', SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]))
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        reads = AsyncMock(side_effect=RuntimeError('synthetic outage'))
        ctx = {'state': 'ready', 'speaker': SimpleNamespace(recent_asks=reads)}
        monkeypatch.setattr(runtime, 'context', AsyncMock(return_value=ctx))
        async def pause(user, device, delay):
            delays.append(delay)
            clock[0] += delay
            if delay == 300:
                runtime.stopping = True
        monkeypatch.setattr(runtime, '_poll_pause', pause)
        await runtime.device_loop(uid, 'speaker-a')
        assert delays == [30, 30, 30, 30, 300] and reads.await_count == 5
        runtime.stopping = False
        runtime.changing.add(uid)
        # The real wait exits immediately when the current account is being rebound.
        await asyncio.wait_for(Runtime._poll_pause(runtime, uid, 'speaker-a', 300), timeout=0.5)
        assert store.devices(uid)[0]['enabled']
    asyncio.run(run())


def test_background_library_relogin_never_submits_password(monkeypatch):
    from miservice import MiAccount
    from platform_app.runtime import SafeAccount, VerificationRequired
    login = AsyncMock(return_value=True)
    monkeypatch.setattr(MiAccount, 'login', login)
    async def run():
        account = SafeAccount(SimpleNamespace(), 'synthetic', 'synthetic')
        with pytest.raises(VerificationRequired):
            await account.login('micoapi')
        assert not login.await_count
        account.interactive = True
        assert await account.login('micoapi')
        assert login.await_count == 1
    asyncio.run(run())
