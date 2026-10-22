"""Real-time login probes and staged, owner-bound account transitions."""
import asyncio
from copy import deepcopy
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import aiohttp
from fastapi.testclient import TestClient

from platform_app.app import create_app
from platform_app.runtime import Runtime, TokenStore
from platform_app.security import Vault, masked_account
from platform_app.store import Rejected, Store
from test_platform import CFG, PASSWORD, QuietRuntime, bound, login, FakeMailer, prepare_mail


@pytest.fixture
def account_store(tmp_path):
    store = Store(tmp_path / 'data', Vault(tmp_path / 'keys' / 'master.key'))
    uid = store.create_user('alice', PASSWORD)
    bound(store, uid, identity='original-xiaomi-id')
    return store, uid


def mock_auth(monkeypatch, identity='new-xiaomi-id', *, success=True, otp=False, fail_devices=False):
    import platform_app.runtime as module
    captured = []
    class Account:
        def __init__(self, session, username, password, token_store, otp_callback, **kwargs):
            captured.append((username, password))
            self.callback, self.tokens = otp_callback, token_store
            self.token = {'userId': identity, 'micoapi': ['synthetic-one', 'synthetic-two']}
        async def login(self, sid):
            if otp:
                assert await self.callback('Phone') == '123456'
            if success:
                await self.tokens.save_token(self.token)
            return success
    async def devices():
        if fail_devices:
            raise aiohttp.ClientConnectionError
        return [{'deviceID': 'new-device', 'hardware': 'L05B', 'name': 'New Play'}]
    monkeypatch.setattr(module, 'SafeAccount', Account)
    monkeypatch.setattr(module, 'MiNAService', lambda account: SimpleNamespace(device_list=devices))
    return captured


async def complete(runtime, uid):
    await runtime.jobs[uid]['task']
    return runtime.login_status(uid)


def test_valid_probe_is_read_only_and_never_constructs_login_account(account_store, monkeypatch):
    import platform_app.runtime as module
    store, uid = account_store
    before = store.account(uid)['credentials']
    calls = []
    class Response:
        status = 200
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def json(self, **kwargs):
            return {'code': 0, 'data': []}
    class Session:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()
    monkeypatch.setattr(module.aiohttp, 'ClientSession', Session)
    monkeypatch.setattr(module, 'SafeAccount', lambda *args, **kwargs: pytest.fail('Probe attempted login'))
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        result = await runtime.check_login(uid)
        assert result['state'] == 'valid'
        assert (await runtime.begin_reverify(uid, 'should-not-be-used'))['state'] == 'already_valid'
        assert not runtime.jobs
    asyncio.run(run())
    assert len(calls) == 4  # device metadata + cloud read for each explicit probe
    assert all(not kwargs['allow_redirects'] for _, kwargs in calls)
    assert all('login' not in url.lower() and 'ticket' not in url.lower() for url, _ in calls)
    assert store.account(uid)['credentials'] == before
    assert store.devices(uid)[0]['enabled']


@pytest.mark.parametrize('failure', [asyncio.TimeoutError, aiohttp.ClientConnectionError])
def test_probe_network_failure_keeps_binding_and_monitoring(account_store, monkeypatch, failure):
    store, uid = account_store
    before = store.account(uid)['credentials']
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        monkeypatch.setattr(runtime, '_probe_token', AsyncMock(side_effect=failure))
        assert (await runtime.check_login(uid))['state'] == 'unknown'
        with pytest.raises(Rejected) as exc:
            await runtime.begin_reverify(uid, 'unused-password')
        assert exc.value.status == 503
        assert not runtime.jobs
    asyncio.run(run())
    assert store.account(uid)['state'] == 'ready'
    assert store.account(uid)['credentials'] == before
    assert store.devices(uid)[0]['enabled']


@pytest.mark.parametrize('status,body,expected', [
    (401, None, 'expired'), (200, {'code': 401}, 'expired'),
    (200, {'code': 0}, 'valid'), (403, {'code': 'waf'}, 'unknown'),
    (503, None, 'unknown'), (200, {'code': 9, 'message': 'invalid token'}, 'expired'),
])
def test_probe_distinguishes_authentication_failure_from_connectivity(status, body, expected):
    assert Runtime._probe_result(status, body) == expected


def test_reverify_uses_saved_username_and_preserves_device_selection(account_store, monkeypatch):
    store, uid = account_store
    username = store.account(uid)['credentials']['username']
    captured = mock_auth(monkeypatch, identity='original-xiaomi-id')
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        monkeypatch.setattr(runtime, '_probe_token', AsyncMock(return_value='expired'))
        # The same hardware list keeps the user's selected devices.
        import platform_app.runtime as module
        monkeypatch.setattr(module, 'MiNAService', lambda account: SimpleNamespace(device_list=AsyncMock(return_value=[
            {'deviceID': 'speaker-a', 'hardware': 'L05B', 'name': 'Play'}])))
        await runtime.begin_reverify(uid, 'new-current-password')
        assert (await complete(runtime, uid))['state'] == 'completed'
        assert store.devices(uid)[0]['enabled']
    asyncio.run(run())
    assert captured == [(username, 'new-current-password')]
    assert store.account(uid)['credentials']['password'] == 'new-current-password'


def test_reverify_identity_mismatch_does_not_overwrite_original(account_store, monkeypatch):
    store, uid = account_store
    before = store.account(uid)['credentials']
    mock_auth(monkeypatch, identity='unexpected-id')
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        monkeypatch.setattr(runtime, '_probe_token', AsyncMock(return_value='expired'))
        await runtime.begin_reverify(uid, 'password')
        assert (await complete(runtime, uid))['state'] == 'failed'
        assert store.account(uid)['credentials'] == before
        assert store.devices(uid)[0]['enabled']
    asyncio.run(run())


def test_replace_is_staged_until_confirmation_and_preserves_history_and_quota(account_store, monkeypatch):
    store, uid = account_store
    old = store.account(uid)['credentials']
    rid = store.reserve(uid, 'speaker-a', 'speaker', 'old-request')
    store.started(rid)
    store.finish(rid, {'total_tokens': 12}, status='completed')
    store.save_history(uid, 'speaker-a', rid, 'old question', 'old answer', account_label=masked_account(old['username']))
    mock_auth(monkeypatch)
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        runtime.sessions(uid).get('speaker-a').append('old user', 'old assistant')
        job = runtime.begin_replace(uid, 'new-xiaomi-login', 'new-secret-password')
        status = await complete(runtime, uid)
        assert status['state'] == 'awaiting_confirmation'
        assert 'new-secret-password' not in str(status)
        assert store.account(uid)['credentials'] == old
        assert store.devices(uid)[0]['enabled']
        assert isinstance(runtime.jobs[uid]['candidate'], bytes)
        runtime.confirm_replace(uid, job['id'])
        assert (await complete(runtime, uid))['state'] == 'completed'
        assert store.account(uid)['credentials']['username'] == 'new-xiaomi-login'
        assert all(not d['enabled'] for d in store.devices(uid))
        assert {d['id'] for d in store.devices(uid)} == {'new-device'}
        assert not runtime.sessions(uid).get('speaker-a').to_llm_messages()
        assert store.history(uid)[0]['question'] == 'old question'
        assert store.summary(uid)['totals']['total_tokens'] == 12
        assert store.summary(uid)['today'] == 1
    asyncio.run(run())


def test_cancel_pending_replacement_wipes_candidate_and_leaves_old_binding(account_store, monkeypatch):
    store, uid = account_store
    before = store.account(uid)['credentials']
    mock_auth(monkeypatch)
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        runtime.begin_replace(uid, 'new-xiaomi-login', 'secret')
        await complete(runtime, uid)
        await runtime.cancel_login(uid)
        assert runtime.login_status(uid)['state'] == 'cancelled'
        assert 'candidate' not in runtime.jobs[uid]
        assert store.account(uid)['credentials'] == before
        assert store.devices(uid)[0]['enabled']
    asyncio.run(run())


@pytest.mark.parametrize('success,fail_devices', [(False, False), (True, True)])
def test_new_account_login_or_device_fetch_failure_does_not_modify_old(account_store, monkeypatch, success, fail_devices):
    store, uid = account_store
    before = store.account(uid)['credentials']
    mock_auth(monkeypatch, success=success, fail_devices=fail_devices)
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        runtime.begin_replace(uid, 'new-account', 'secret')
        assert (await complete(runtime, uid))['state'] == 'failed'
        assert store.account(uid)['credentials'] == before
        assert store.devices(uid)[0]['enabled']
    asyncio.run(run())


def test_new_account_owned_by_another_user_is_rejected(account_store, monkeypatch):
    store, uid = account_store
    other = store.create_user('bob', PASSWORD)
    bound(store, other, did='other-speaker', identity='occupied-xiaomi-id')
    mock_auth(monkeypatch, identity='occupied-xiaomi-id')
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        runtime.begin_replace(uid, 'occupied-account', 'secret')
        status = await complete(runtime, uid)
        assert status['state'] == 'failed' and '其他用户' in status['message']
        assert store.account(uid)['credentials']['token']['userId'] == 'original-xiaomi-id'
    asyncio.run(run())


def test_candidate_expiration_cannot_be_confirmed(account_store, monkeypatch):
    store, uid = account_store
    mock_auth(monkeypatch)
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        job = runtime.begin_replace(uid, 'new-account', 'secret')
        await complete(runtime, uid)
        runtime.jobs[uid]['expires'] = time.time() - 1
        with pytest.raises(Rejected):
            runtime.confirm_replace(uid, job['id'])
        assert 'candidate' not in runtime.jobs[uid]
        assert store.devices(uid)[0]['enabled']
    asyncio.run(run())


def test_confirm_switch_waits_for_current_reply_and_blocks_new_work(account_store, monkeypatch):
    store, uid = account_store
    mock_auth(monkeypatch)
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        job = runtime.begin_replace(uid, 'new-account', 'secret')
        await complete(runtime, uid)
        release = asyncio.Event()
        worker = asyncio.create_task(release.wait())
        runtime.workers[(uid, 'speaker-a')] = worker
        runtime.confirm_replace(uid, job['id'])
        await asyncio.sleep(0)
        assert uid in runtime.changing
        assert not runtime.enabled(uid, 'speaker-a')
        assert store.account(uid)['credentials']['token']['userId'] == 'original-xiaomi-id'
        with pytest.raises(Rejected):
            await runtime.text(uid, 'question', 'new-text-request')
        release.set()
        await complete(runtime, uid)
        assert store.account(uid)['credentials']['token']['userId'] == 'new-xiaomi-id'
    asyncio.run(run())


def test_binding_and_devices_roll_back_together_on_storage_failure(account_store, monkeypatch):
    store, uid = account_store
    before = store.account(uid)['credentials']
    credentials = {'username': 'new-account', 'password': 'new-password',
                   'token': {'userId': 'new-account-id', 'micoapi': ['a', 'b']}}
    monkeypatch.setattr(store, '_sync_devices', lambda *args: (_ for _ in ()).throw(RuntimeError('storage failure')))
    with pytest.raises(RuntimeError):
        store.install_account(uid, credentials, [], store.account(uid)['identity'], replace=True)
    assert store.account(uid)['credentials'] == before
    assert store.devices(uid)[0]['enabled']


def test_reverify_api_rejects_client_supplied_username(tmp_path, monkeypatch):
    monkeypatch.setenv('PLATFORM_ADMIN_PASSWORD', PASSWORD)
    for key in ('PLATFORM_KEY_FILE', 'PLATFORM_PUBLIC_ORIGIN', 'PLATFORM_COOKIE_SECURE'):
        monkeypatch.delenv(key, raising=False)
    app = create_app(tmp_path / 'web', deepcopy(CFG), runtime_factory=QuietRuntime, mailer=FakeMailer())
    with TestClient(app) as client:
        client.headers['Origin'] = 'http://testserver'
        prepare_mail(client, app)
        me = login(client)
        bound(app.state.store, me['user_id'])
        assert client.post('/api/xiaomi/reverify', json={'username': 'another-account', 'password': 'secret'}).status_code == 422
        assert client.post('/api/xiaomi/login', json={'username': 'another-account', 'password': 'secret'}).status_code == 409
        mock_probe = AsyncMock(return_value='valid')
        monkeypatch.setattr(app.state.runtime, '_probe_token', mock_probe)
        assert client.post('/api/xiaomi/check').json()['state'] == 'valid'
        assert client.post('/api/xiaomi/reverify', json={'password': 'unused'}).json()['state'] == 'already_valid'
        assert not app.state.runtime.jobs


def test_ephemeral_token_store_cannot_overwrite_bound_credentials(account_store):
    store, uid = account_store
    before = store.account(uid)['credentials']
    async def run():
        tokens = TokenStore(store, uid, 'another-user', 'another-password', persist=False)
        await tokens.save_token({'userId': 'another-id', 'micoapi': ['a', 'b']})
    asyncio.run(run())
    assert store.account(uid)['credentials'] == before


def test_same_account_alias_is_not_a_replacement(account_store, monkeypatch):
    store, uid = account_store
    mock_auth(monkeypatch, identity='original-xiaomi-id')
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        runtime.begin_replace(uid, 'different-login-alias', 'password')
        status = await complete(runtime, uid)
        assert status['state'] == 'failed' and '当前已绑定' in status['message']
        assert store.devices(uid)[0]['enabled']
    asyncio.run(run())


def test_cancel_after_atomic_commit_reports_completed_instead_of_old_binding(account_store, monkeypatch):
    store, uid = account_store
    mock_auth(monkeypatch)
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        job = runtime.begin_replace(uid, 'new-account', 'secret')
        await complete(runtime, uid)
        runtime.confirm_replace(uid, job['id'])
        await complete(runtime, uid)
        result = await runtime.cancel_login(uid)
        assert result['state'] == 'completed'
        assert store.account(uid)['credentials']['token']['userId'] == 'new-xiaomi-id'
    asyncio.run(run())


def test_reverify_otp_is_bound_to_original_account_and_not_a_new_identity(account_store, monkeypatch):
    store, uid = account_store
    username = store.account(uid)['credentials']['username']
    captured = mock_auth(monkeypatch, identity='original-xiaomi-id', otp=True)
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        monkeypatch.setattr(runtime, '_probe_token', AsyncMock(return_value='expired'))
        job = await runtime.begin_reverify(uid, 'current-password')
        for _ in range(10):
            await asyncio.sleep(0)
            if runtime.login_status(uid)['state'] == 'verification_required':
                break
        assert runtime.login_status(uid)['mode'] == 'reverify'
        runtime.submit_code(uid, job['id'], '123456')
        assert (await complete(runtime, uid))['state'] == 'completed'
    asyncio.run(run())
    assert captured == [(username, 'current-password')]


def test_stale_confirmation_cannot_replace_a_changed_binding(account_store, monkeypatch):
    store, uid = account_store
    mock_auth(monkeypatch)
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        job = runtime.begin_replace(uid, 'candidate-account', 'secret')
        await complete(runtime, uid)
        with store.db() as db:
            db.execute('DELETE FROM accounts WHERE user_id=?', (uid,))
        bound(store, uid, identity='changed-between-steps')
        runtime.confirm_replace(uid, job['id'])
        assert (await complete(runtime, uid))['state'] == 'failed'
        assert store.account(uid)['credentials']['token']['userId'] == 'changed-between-steps'
    asyncio.run(run())


def test_cancel_while_waiting_for_old_speech_does_not_interrupt_that_speech(account_store, monkeypatch):
    store, uid = account_store
    mock_auth(monkeypatch)
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        job = runtime.begin_replace(uid, 'new-account', 'secret')
        await complete(runtime, uid)
        release = asyncio.Event()
        worker = asyncio.create_task(release.wait())
        runtime.workers[(uid, 'speaker-a')] = worker
        runtime.confirm_replace(uid, job['id'])
        await asyncio.sleep(0)
        result = await runtime.cancel_login(uid)
        assert result['state'] == 'cancelled'
        assert not worker.cancelled() and not worker.done()
        assert runtime.enabled(uid, 'speaker-a')
        assert store.account(uid)['credentials']['token']['userId'] == 'original-xiaomi-id'
        release.set()
        await worker
    asyncio.run(run())
