"""Platform boundaries and failure modes. No real accounts, SMS, speech, or model calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

import llm_router
from platform_app.admin import backup, import_usage
from platform_app.app import create_app
from platform_app.runtime import Runtime, SafeAccount, VerificationRequired
from platform_app.security import Vault, password_matches
from platform_app.store import CST, Rejected, Store

CFG = {'llm': {'default_provider': 'deepseek', 'deepseek': {'model': 'deepseek-chat', 'api_key': 'fake-key'}},
       'trigger': {'wake_words': ['问问AI', '深思']}, 'tts': {'answer_prefix': 'Deepseek 回答：'}}
PASSWORD = 'Test-platform-password-123'


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'data', Vault(tmp_path / 'keys' / 'master.key'))


def bound(store, uid, did='speaker-a', identity=None):
    store.save_account(uid, 'private-xiaomi-account', 'private-xiaomi-password',
                       {'userId': identity or uid, 'micoapi': ['secret-one', 'secret-two']})
    store.sync_devices(uid, [{'id': did, 'name': 'Play', 'hardware': 'L05B', 'supported': True, 'reason': ''}])
    store.select_devices(uid, [did])


def policy(store, uid, **values):
    with store.db() as db:
        row = db.execute('SELECT policy FROM users WHERE id=?', (uid,)).fetchone()
        value = {**json.loads(row[0]), **values}
        db.execute('UPDATE users SET policy=? WHERE id=?', (json.dumps(value), uid))


class QuietRuntime(Runtime):
    async def start(self):
        self.store.cleanup()


class FakeMailer:
    def __init__(self):
        self.messages = []

    async def send(self, recipient, subject, body):
        self.messages.append((recipient, subject, body))

    def code(self):
        return re.search(r'邮箱验证码：(\d{6})', self.messages[-1][2]).group(1)

    def reset_token(self):
        return re.search(r'/#reset=([A-Za-z0-9_.-]+)', self.messages[-1][2]).group(1)


def prepare_mail(client, app):
    client.mailer = app.state.auth.mailer
    with app.state.store.db() as db:
        owner = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    if not app.state.auth.email(owner[0]):
        app.state.auth.set_legacy_email(owner[0], 'admin@example.com')


def allow_mail(client):
    # Other tests exercise persistent throttles directly; helpers model separate login occasions.
    with client.app.state.store.db() as db:
        db.execute('UPDATE auth_limits SET start=0')


@pytest.fixture
def web(tmp_path, monkeypatch):
    for key in ('PLATFORM_KEY_FILE', 'PLATFORM_PUBLIC_ORIGIN', 'PLATFORM_COOKIE_SECURE', 'PLATFORM_EXCLUDED_DEVICES'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('PLATFORM_ADMIN_PASSWORD', PASSWORD)
    monkeypatch.setenv('PLATFORM_PUBLIC_ORIGIN', 'http://testserver')
    app = create_app(tmp_path / 'web', deepcopy(CFG), runtime_factory=QuietRuntime, mailer=FakeMailer())
    with TestClient(app) as client:
        client.headers['Origin'] = 'http://testserver'
        prepare_mail(client, app)
        yield client, app


def login(client, username='admin', password=PASSWORD):
    allow_mail(client)
    r = client.post('/api/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    if 'challenge_id' in r.json():
        r = client.post('/api/auth/verify', json={'challenge_id': r.json()['challenge_id'], 'code': client.mailer.code()})
        assert r.status_code == 200, r.text
    me = client.get('/api/me').json()
    client.headers['X-CSRF-Token'] = me['csrf']
    return me


def invite_user(client, username='alice'):
    token = client.post('/api/admin/invites').json()['token']
    allow_mail(client)
    r = client.post('/api/redeem', json={'token': token, 'username': username, 'password': PASSWORD, 'email': username+'@example.com'})
    assert r.status_code == 200, r.text
    r = client.post('/api/auth/verify', json={'challenge_id': r.json()['challenge_id'], 'code': client.mailer.code()})
    assert r.status_code == 200, r.text
    me = client.get('/api/me').json()
    client.headers['X-CSRF-Token'] = me['csrf']
    return me, token


def response(text='测试回答', usage=True):
    return SimpleNamespace(model='test-model', id='response-id', choices=[SimpleNamespace(
        finish_reason='stop', message=SimpleNamespace(content=text))], usage={
            'prompt_tokens': 8, 'completion_tokens': 12, 'total_tokens': 20} if usage else None)


def test_cipher_binds_owner_and_keeps_disk_private(store):
    a = store.create_user('alice', PASSWORD)
    b = store.create_user('bob', PASSWORD)
    bound(store, a)
    store.save_history(a, 'speaker-a', 'history-id', 'private question XYZ', 'private answer XYZ')
    assert store.history(a)[0]['question'] == 'private question XYZ'
    assert store.history(b) == []
    for path in (store.path, store.history_path):
        data = path.read_bytes()
        for secret in (b'private question XYZ', b'private answer XYZ', b'private-xiaomi-password', PASSWORD.encode()):
            assert secret not in data
        assert path.stat().st_mode & 0o777 == 0o600
    with store.history_db() as db:
        cipher = db.execute('SELECT content FROM history').fetchone()[0]
    with pytest.raises(Exception):
        store.vault.open(cipher, 'history:' + b + ':history-id')


def test_same_xiaomi_account_cannot_be_bound_by_second_user(store):
    a, b = (store.create_user(name, PASSWORD) for name in ('alice', 'bob'))
    bound(store, a, identity='same-xiaomi-id')
    with pytest.raises(Rejected, match='已绑定'):
        bound(store, b, identity='same-xiaomi-id')


def test_devices_are_owned_and_unsupported_cannot_be_enabled(store):
    a, b = (store.create_user(name, PASSWORD) for name in ('alice', 'bob'))
    bound(store, a)
    with pytest.raises(Rejected):
        store.select_devices(b, ['speaker-a'])
    store.sync_devices(a, [{'id': 'bad', 'name': 'HD', 'hardware': 'SM4', 'supported': False, 'reason': '不支持'}])
    assert not next(d for d in store.devices(a) if d['id'] == 'speaker-a')['present']
    with pytest.raises(Rejected):
        store.select_devices(a, ['bad'])


def test_device_normalization_hides_non_speakers_and_blocks_unknown_and_excluded(store):
    runtime = Runtime(store, CFG, {'production'})
    rows = runtime.normalize_devices([
        {'deviceID': 'good', 'hardware': 'L05B', 'name': 'Play'},
        {'deviceID': 'production', 'hardware': 'LX04', 'name': '触屏'},
        {'deviceID': 'unknown', 'hardware': 'NEW', 'model': 'xiaomi.wifispeaker.new'},
        {'deviceID': 'bad', 'hardware': 'SM4'},
        {'deviceID': 'lamp', 'model': 'xiaomi.light.foo'}])
    assert {r['id'] for r in rows} == {'good', 'production', 'unknown', 'bad'}
    assert next(r for r in rows if r['id'] == 'good')['supported']
    assert all(r['reason'] and not r['supported'] for r in rows if r['id'] != 'good')
    uid = store.create_user('alice', PASSWORD)
    store.sync_devices(uid, rows)
    # Selection is a UI default, not a side effect of fetching a device list.
    assert not any(r['enabled'] for r in store.devices(uid))


def test_quota_reservation_is_atomic_across_concurrent_requests(store):
    uid = store.create_user('alice', PASSWORD)
    policy(store, uid, daily_limit=5, minute_limit=120)
    def request(n):
        try:
            return store.reserve(uid, 'a', 'web', str(n))
        except Rejected:
            return None
    with ThreadPoolExecutor(max_workers=12) as pool:
        result = list(pool.map(request, range(20)))
    assert sum(bool(r) for r in result) == 5


def test_quota_reset_uses_beijing_midnight_and_failure_still_counts(store):
    uid = store.create_user('alice', PASSWORD)
    policy(store, uid, daily_limit=1, minute_limit=120)
    before = datetime(2026, 10, 17, 23, 59, 59, tzinfo=CST).timestamp()
    rid = store.reserve(uid, 'a', 'web', 'one', now=before)
    store.started(rid)
    store.finish(rid, None, status='failed')
    with pytest.raises(Rejected):
        store.reserve(uid, 'a', 'web', 'two', now=before)
    assert store.reserve(uid, 'a', 'web', 'three', now=before + 2)
    assert store.summary(uid)['totals']['unknown_usage'] == 1


def test_duplicate_and_cancelled_request_do_not_bypass_rules(store):
    uid = store.create_user('alice', PASSWORD)
    policy(store, uid, daily_limit=1)
    rid = store.reserve(uid, None, 'web', 'same')
    with pytest.raises(Rejected, match='已处理'):
        store.reserve(uid, None, 'web', 'same')
    store.update_request(rid, status='cancelled')
    assert store.reserve(uid, None, 'web', 'new')


def test_opening_store_does_not_cancel_running_reservations(store):
    uid = store.create_user('alice', PASSWORD)
    rid = store.reserve(uid, None, 'web', 'request')
    Store(store.root, store.vault)
    assert store.summary(uid)['requests'][0]['status'] == 'reserved'
    store.recover_requests()
    assert store.summary(uid)['requests'][0]['status'] == 'cancelled'
    r2 = store.reserve(uid, None, 'web', 'second')
    store.started(r2)
    store.recover_requests()
    assert store.summary(uid)['requests'][0]['status'] == 'interrupted'


def test_retention_deletes_oldest_expired_only_and_user_deletion_is_scoped(store):
    a, b = (store.create_user(n, PASSWORD) for n in ('alice', 'bob'))
    store.save_history(a, 'a', 'old', 'old question', 'old answer')
    store.save_history(a, 'a', 'new', 'new question', 'new answer')
    store.save_history(b, 'b', 'bob-history', 'bob question', 'bob answer')
    with store.history_db() as db:
        db.execute('UPDATE history SET created=0 WHERE id=?', ('old',))
    assert store.cleanup() == 1
    assert [r['id'] for r in store.history(a)] == ['new']
    store.delete_history(a, 'bob-history')
    assert store.history(b)
    store.delete_history(a)
    assert not store.history(a) and store.history(b)


def test_import_is_idempotent_and_backup_excludes_history_and_sessions(store, tmp_path):
    uid = store.create_user('alice', PASSWORD)
    source = tmp_path / 'usage.jsonl'
    source.write_text(json.dumps({'timestamp': '2026-10-17T00:00:00+00:00', 'status': 'completed',
                                  'usage_available': True, 'total_tokens': 5}) + '\ninvalid-json\n')
    assert import_usage(store, 'alice', source) == {'imported': 1, 'skipped': 1}
    assert import_usage(store, 'alice', source) == {'imported': 0, 'skipped': 2}
    store.save_history(uid, 'a', 'history', 'hidden body', 'hidden answer')
    with store.db() as db:
        db.execute('INSERT INTO sessions VALUES(?,?,?,?)', ('hash', uid, 'csrf', 9999999999))
    path = backup(store, tmp_path / 'backup')
    import sqlite3
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == 1
        assert not db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='history'").fetchone()
    assert len(list(path.parent.iterdir())) == 1


def test_auth_origin_csrf_and_validation_redaction(web):
    client, app = web
    assert client.get('/api/me').status_code == 401
    login(client)
    bad = client.put('/api/dialogue', json={'system_prompt': 'X'}, headers={'Origin': 'https://evil.example'})
    assert bad.status_code == 403
    assert client.post('/api/admin/invites', headers={'X-CSRF-Token': ''}).status_code == 403
    r = client.post('/api/xiaomi/login', json={'username': 'a', 'password': 'SECRET-' * 100})
    assert r.status_code == 422 and 'SECRET-' not in r.text
    assert client.get('/').headers['content-security-policy'].startswith("default-src 'self'")
    allow_mail(client)
    r = client.post('/api/login', json={'username': 'admin', 'password': PASSWORD})
    r = client.post('/api/auth/verify', json={'challenge_id': r.json()['challenge_id'], 'code': client.mailer.code()})
    assert 'HttpOnly' in r.headers['set-cookie']


def test_invitation_single_use_and_ordinary_user_cannot_admin(web):
    client, _ = web
    login(client)
    alice, token = invite_user(client)
    assert alice['role'] == 'user'
    assert client.get('/api/admin').status_code == 403
    assert client.post('/api/redeem', json={'token': token, 'username': 'bob', 'password': PASSWORD, 'email': 'bob@example.com'}).status_code == 400


def test_history_is_never_available_to_other_user_or_admin(web):
    client, app = web
    admin = login(client)
    alice, _ = invite_user(client)
    store = app.state.store
    store.save_history(alice['user_id'], 'dev', 'private', 'alice private question', 'alice private answer')
    assert client.get('/api/history').json()[0]['question'] == 'alice private question'
    login(client)
    assert client.get('/api/history', params={'user_id': alice['user_id']}).json() == []
    assert 'alice private' not in client.get('/api/admin').text
    client.delete('/api/history/private')
    assert store.history(alice['user_id'])
    assert admin['user_id'] != alice['user_id']


def test_suspension_revokes_sessions_and_stops_new_requests(web):
    client, app = web
    login(client)
    alice, _ = invite_user(client)
    token = client.cookies.get('speaker_session')
    login(client)
    r = client.put('/api/admin/users/' + alice['user_id'], json={'active': False})
    assert r.status_code == 200
    client.cookies.set('speaker_session', token, domain='testserver.local', path='/')
    assert client.get('/api/me').status_code == 401
    with pytest.raises(Rejected, match='暂停'):
        app.state.store.reserve(alice['user_id'], None, 'web', 'request')


def test_user_cannot_write_quota_or_another_users_device(web):
    client, app = web
    login(client)
    alice, _ = invite_user(client)
    assert client.put('/api/dialogue', json={'daily_limit': 99999}).status_code == 422
    bound(app.state.store, alice['user_id'])
    assert client.put('/api/devices', json={'devices': ['not-owned']}).status_code == 403
    assert client.post('/api/dialogue/clear', json={'device_id': 'not-owned'}).status_code == 403


def test_password_change_invalidates_old_session_and_removes_bootstrap(web):
    client, app = web
    login(client)
    old = client.cookies.get('speaker_session')
    assert (app.state.store.root / 'bootstrap-admin.txt').exists()
    r = client.post('/api/password', json={'username': 'admin', 'password': 'New-long-platform-password'})
    assert r.status_code == 200
    assert not (app.state.store.root / 'bootstrap-admin.txt').exists()
    with app.state.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 1
    assert client.cookies.get('speaker_session') != old
    assert password_matches('New-long-platform-password', app.state.store.user(client.get('/api/me').json()['user_id'])['password'])


def test_missing_original_key_refuses_to_open_existing_database(web, monkeypatch):
    _, app = web
    monkeypatch.setenv('PLATFORM_KEY_FILE', str(app.state.store.root / 'missing.key'))
    with pytest.raises(RuntimeError, match='密钥缺失'):
        create_app(app.state.store.root, deepcopy(CFG), runtime_factory=QuietRuntime)


def test_model_usage_limits_history_and_no_automatic_retry(store, monkeypatch):
    async def run():
        uid = store.create_user('alice', PASSWORD)
        runtime = Runtime(store, deepcopy(CFG))
        call = AsyncMock(return_value=response())
        monkeypatch.setattr(llm_router, 'acompletion', call)
        result = await runtime.text(uid, 'private question', 'request-12345678')
        assert result['answer'] == '测试回答'
        kwargs = call.await_args.kwargs
        assert kwargs['max_tokens'] == 1024 and kwargs['num_retries'] == 0
        assert store.summary(uid)['requests'][0]['total_tokens'] == 20
        assert store.history(uid)[0]['question'] == 'private question'
        assert runtime.sessions(uid).get('web-debug').to_llm_messages()
        runtime.clear_memory(uid, 'web-debug')
        assert not runtime.sessions(uid).get('web-debug').to_llm_messages()
        assert store.history(uid)
    asyncio.run(run())


def test_model_failure_counts_unknown_and_does_not_pollute_context(store, monkeypatch):
    async def run():
        uid = store.create_user('alice', PASSWORD)
        runtime = Runtime(store, deepcopy(CFG))
        monkeypatch.setattr(llm_router, 'acompletion', AsyncMock(side_effect=RuntimeError('SECRET raw response')))
        with pytest.raises(Rejected) as exc:
            await runtime.text(uid, 'question', 'request-12345678')
        assert 'SECRET' not in str(exc.value)
        totals = store.summary(uid)['totals']
        assert totals['unknown_usage'] == 1 and totals['failures'] == 1
        assert not runtime.sessions(uid).get('web-debug').to_llm_messages()
    asyncio.run(run())


def test_clear_during_model_request_prevents_context_reappearing(store, monkeypatch):
    async def run():
        uid = store.create_user('alice', PASSWORD)
        runtime = Runtime(store, deepcopy(CFG))
        waiting, proceed = asyncio.Event(), asyncio.Event()
        async def model(**kwargs):
            waiting.set()
            await proceed.wait()
            return response()
        monkeypatch.setattr(llm_router, 'acompletion', model)
        task = asyncio.create_task(runtime.text(uid, 'question', 'request-12345678'))
        await waiting.wait()
        runtime.clear_memory(uid, 'web-debug')
        proceed.set()
        await task
        assert not runtime.sessions(uid).get('web-debug').to_llm_messages()
    asyncio.run(run())


def test_cancelled_queued_request_releases_reserved_quota(store):
    async def run():
        uid = store.create_user('alice', PASSWORD)
        runtime = Runtime(store, deepcopy(CFG))
        runtime.semaphore = asyncio.Semaphore(0)
        task = asyncio.create_task(runtime.text(uid, 'question', 'request-12345678'))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        row = store.summary(uid)['requests'][0]
        assert row['status'] == 'cancelled' and not row['started']
    asyncio.run(run())


def test_background_login_never_triggers_otp_send():
    async def run():
        account = SafeAccount(SimpleNamespace(), 'user', 'password')
        with pytest.raises(VerificationRequired):
            await account._verify_otp('micoapi', 'unused-url')
    asyncio.run(run())


def test_web_otp_state_machine_and_device_fetch_do_not_start_listening(store, monkeypatch):
    import platform_app.runtime as module
    async def run():
        uid = store.create_user('alice', PASSWORD)
        runtime = Runtime(store, deepcopy(CFG))
        class FakeAccount:
            def __init__(self, session, username, password, token_store, otp_callback, **kwargs):
                self.callback = otp_callback
                self.token = {'userId': 'xiaomi-user-id', 'micoapi': ['secret', 'secret']}
            async def login(self, sid):
                assert await self.callback('Phone') == '123456'
                return True
        monkeypatch.setattr(module, 'SafeAccount', FakeAccount)
        monkeypatch.setattr(module, 'MiNAService', lambda account: SimpleNamespace(device_list=AsyncMock(
            return_value=[{'deviceID': 'play', 'hardware': 'L05B', 'name': 'Play'}])))
        job = runtime.begin_login(uid, 'private-user', 'private-password')
        for _ in range(10):
            await asyncio.sleep(0)
            if runtime.login_status(uid)['state'] == 'verification_required':
                break
        runtime.submit_code(uid, job['id'], '123456')
        await runtime.jobs[uid]['task']
        assert runtime.login_status(uid)['state'] == 'completed'
        assert store.devices(uid)[0]['supported'] and not store.devices(uid)[0]['enabled']
        assert 'private-password' not in json.dumps(runtime.login_status(uid))
    asyncio.run(run())


def test_independent_device_workers_do_not_wait_for_other_device_speech(store, monkeypatch):
    async def run():
        uid = store.create_user('alice', PASSWORD)
        bound(store, uid, 'a')
        store.sync_devices(uid, [{'id': d, 'name': d, 'hardware': 'L05B', 'supported': True, 'reason': ''}
                                for d in ('a', 'b')])
        store.select_devices(uid, ['a', 'b'])
        store.set_dialogue(uid, {'routing_mode': 'keyword'})
        runtime = Runtime(store, deepcopy(CFG))
        started_a, completed_b, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        sent = set()
        async def latest(did):
            if did in sent:
                return None
            sent.add(did)
            return {'request_id': did, 'ts': time.time() + 0.01, 'text': '深思 test'}
        async def context(uid):
            return {'speaker': SimpleNamespace(latest_ask=latest), 'account': SimpleNamespace(token=True)}
        async def handle(uid, did, *args, **kwargs):
            if did == 'a':
                started_a.set()
                await release.wait()
            else:
                await started_a.wait()
                completed_b.set()
        monkeypatch.setattr(runtime, 'context', context)
        monkeypatch.setattr(runtime, 'handle', handle)
        tasks = [asyncio.create_task(runtime.device_loop(uid, d)) for d in ('a', 'b')]
        await asyncio.wait_for(completed_b.wait(), 2)
        assert not tasks[0].done()
        runtime.stopping = True
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(run())


def test_failed_verification_and_cancel_do_not_save_credentials(store, monkeypatch):
    import platform_app.runtime as module
    async def run():
        uid = store.create_user('alice', PASSWORD)
        runtime = Runtime(store, deepcopy(CFG))
        class FailedAccount:
            def __init__(self, *args, **kwargs):
                pass
            async def login(self, sid):
                return False
        monkeypatch.setattr(module, 'SafeAccount', FailedAccount)
        runtime.begin_login(uid, 'private-username', 'private-password')
        await runtime.jobs[uid]['task']
        assert runtime.login_status(uid)['state'] == 'failed'
        assert store.account(uid) is None
        runtime.jobs.clear()
        class WaitingAccount:
            def __init__(self, *args, otp_callback, **kwargs):
                self.callback = otp_callback
            async def login(self, sid):
                await self.callback('Phone')
        monkeypatch.setattr(module, 'SafeAccount', WaitingAccount)
        runtime.begin_login(uid, 'private-username', 'private-password')
        await asyncio.sleep(0)
        await runtime.cancel_login(uid)
        assert runtime.login_status(uid)['state'] == 'cancelled'
        assert store.account(uid) is None
    asyncio.run(run())


def test_verification_timeout_is_reported_without_storing_code(store, monkeypatch):
    import platform_app.runtime as module
    async def run():
        uid = store.create_user('alice', PASSWORD)
        runtime = Runtime(store, deepcopy(CFG))
        class TimedOutAccount:
            def __init__(self, *args, **kwargs):
                pass
            async def login(self, sid):
                raise asyncio.TimeoutError
        monkeypatch.setattr(module, 'SafeAccount', TimedOutAccount)
        runtime.begin_login(uid, 'private-username', 'private-password')
        await runtime.jobs[uid]['task']
        assert runtime.login_status(uid)['state'] == 'expired'
        assert store.account(uid) is None
    asyncio.run(run())


def test_reset_link_revokes_sessions_and_cannot_be_reused(web):
    client, app = web
    login(client)
    alice, _ = invite_user(client)
    login(client)
    allow_mail(client)
    response = client.post('/api/admin/users/' + alice['user_id'] + '/reset')
    assert response.status_code == 200 and 'token' not in response.json()
    token = client.mailer.reset_token()
    new_password = 'Reset-user-platform-password'
    r = client.post('/api/password/reset', json={'token': token, 'password': new_password})
    assert r.status_code == 200
    with app.state.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM sessions WHERE user_id=?', (alice['user_id'],)).fetchone()[0] == 0
    assert client.post('/api/password/reset', json={'token': token, 'password': new_password}).status_code == 400
    login(client, 'alice', new_password)


def test_model_and_speech_logs_do_not_contain_transcripts(store, monkeypatch, caplog):
    import logging
    import mi_conversation
    async def run():
        uid = store.create_user('alice', PASSWORD)
        bound(store, uid)
        runtime = Runtime(store, deepcopy(CFG))
        monkeypatch.setattr(llm_router, 'acompletion', AsyncMock(return_value=response('PRIVATE-ANSWER-ZZZ')))
        speaker = mi_conversation.MiSpeaker(None, SimpleNamespace(text_to_speech=AsyncMock(return_value=True)), {})
        with caplog.at_level(logging.INFO):
            await runtime.handle(uid, 'speaker-a', '深思 PRIVATE-QUESTION-ZZZ', 'speaker', 'req-private', speaker, native_answer='这可把我难住了，看来要更努力学习了')
        assert 'PRIVATE-ANSWER-ZZZ' not in caplog.text
        assert 'PRIVATE-QUESTION-ZZZ' not in caplog.text
        assert store.summary(uid)['requests'][0]['speech_status'] == 'submitted'
    asyncio.run(run())


def test_minute_rate_limit_and_empty_response_usage_are_preserved(store, monkeypatch):
    async def run():
        uid = store.create_user('alice', PASSWORD)
        policy(store, uid, minute_limit=1)
        runtime = Runtime(store, deepcopy(CFG))
        call = AsyncMock(return_value=response(''))
        monkeypatch.setattr(llm_router, 'acompletion', call)
        with pytest.raises(Rejected):
            await runtime.text(uid, 'question', 'request-empty-response')
        assert store.summary(uid)['requests'][0]['total_tokens'] == 20
        with pytest.raises(Rejected, match='太快'):
            await runtime.text(uid, 'another question', 'request-another-response')
        assert call.await_count == 1
    asyncio.run(run())


def test_new_reset_link_invalidates_previous_link(web):
    client, _ = web
    login(client)
    alice, _ = invite_user(client)
    login(client)
    allow_mail(client)
    assert client.post('/api/admin/users/' + alice['user_id'] + '/reset').status_code == 200
    first = client.mailer.reset_token()
    allow_mail(client)
    assert client.post('/api/admin/users/' + alice['user_id'] + '/reset').status_code == 200
    second = client.mailer.reset_token()
    body = {'password': 'Another-long-password', 'token': first}
    assert client.post('/api/password/reset', json=body).status_code == 400
    body['token'] = second
    assert client.post('/api/password/reset', json=body).status_code == 200


def test_login_rate_limit_has_no_password_echo(web):
    client, _ = web
    for _ in range(10):
        assert client.post('/api/login', json={'username': 'admin', 'password': 'wrong-private-value'}).status_code == 401
    r = client.post('/api/login', json={'username': 'admin', 'password': 'wrong-private-value'})
    assert r.status_code == 429 and 'wrong-private-value' not in r.text


def test_recovery_marks_unfinished_speech_as_interrupted(store):
    uid = store.create_user('alice', PASSWORD)
    rid = store.reserve(uid, 'a', 'speaker', 'request')
    store.started(rid)
    store.finish(rid, {}, status='completed', speech_status='submitting')
    store.recover_requests()
    row = store.summary(uid)['requests'][0]
    assert row['status'] == 'completed' and row['speech_status'] == 'interrupted'


def test_incremental_history_cursors_include_ties_without_duplicates_or_cross_user_leaks(store):
    a, b = (store.create_user(n, PASSWORD) for n in ('alice', 'bob'))
    for rid in ('a', 'b', 'c'):
        store.save_history(a, 'device', rid, 'question ' + rid, 'answer ' + rid)
    store.save_history(b, 'device', 'other', 'private question', 'private answer')
    import time
    timestamp = time.time()
    with store.history_db() as db:
        db.execute('UPDATE history SET created=?', (timestamp,))
    assert [r['id'] for r in store.history(a)] == ['c', 'b', 'a']
    assert [r['id'] for r in store.history(a, after=timestamp, after_id='a')] == ['b', 'c']
    assert [r['id'] for r in store.history(a, before=timestamp, before_id='b')] == ['a']
    assert store.history(a, after=timestamp, after_id='c') == []


def test_incremental_history_catches_up_across_multiple_pages(store):
    uid = store.create_user('alice', PASSWORD)
    import time
    timestamp = time.time()
    for n in range(65):
        store.save_history(uid, 'device', f'{n:03}', 'question', 'answer')
    with store.history_db() as db:
        db.execute('UPDATE history SET created=?', (timestamp,))
    received, after_id = [], ''
    while True:
        rows = store.history(uid, after=timestamp, after_id=after_id)
        if not rows:
            break
        received.extend(r['id'] for r in rows)
        after_id = rows[-1]['id']
    assert received == [f'{n:03}' for n in range(65)]


def test_history_api_incremental_query_is_scoped_to_logged_in_user(web):
    client, app = web
    login(client)
    alice, _ = invite_user(client)
    store = app.state.store
    store.save_history(alice['user_id'], 'dev', 'new', 'private alice question', 'answer')
    row = store.history(alice['user_id'])[0]
    assert client.get('/api/history', params={'after': row['created'], 'after_id': ''}).json()[0]['id'] == 'new'
    assert client.get('/api/history', params={'after': row['created'], 'after_id': 'new'}).json() == []
    assert client.get('/api/history', params={'after': 1, 'before': 2}).status_code == 400
    login(client)
    assert client.get('/api/history', params={'after': 0, 'user_id': alice['user_id']}).json() == []


@pytest.mark.parametrize('identity,expected', [
    ('13812345678', '138••••5678'),
    ('alice@example.com', 'a••••@e••••'),
    ('xiaomi-user-123', 'xi••••23'),
    ('abc', '•••'),
    ('', '•'),
])
def test_connected_account_label_never_returns_full_identity(identity, expected):
    from platform_app.security import masked_account
    assert masked_account(identity) == expected


def test_profile_exposes_only_masked_xiaomi_identity(web):
    client, app = web
    me = login(client)
    app.state.store.save_account(me['user_id'], '13812345678', 'private-password-value',
                                 {'userId': 'synthetic-owner', 'micoapi': ['private-token-one', 'private-token-two']})
    r = client.get('/api/me')
    assert r.json()['account_label'] == '138••••5678'
    for secret in ('13812345678', 'private-password-value', 'private-token-one', 'private-token-two'):
        assert secret not in r.text



def test_device_claim_cannot_be_taken_by_refresh_or_pause(store):
    a, b = (store.create_user(n, PASSWORD) for n in ('alice', 'bob'))
    bound(store, a)
    rows = [{'id': 'speaker-a', 'name': 'Shared Play', 'hardware': 'L05B', 'supported': True, 'reason': ''}]
    store.select_devices(a, [])
    store.sync_devices(a, [])
    store.sync_devices(b, rows)
    assert not store.devices(b)[0]['supported']
    assert '其他平台账号' in store.devices(b)[0]['reason']
    with pytest.raises(Rejected):
        store.select_devices(b, ['speaker-a'])
    assert store.devices(a)[0]['enabled'] == 0


def test_physical_device_alias_blocks_different_mina_ids(store):
    a, b = (store.create_user(n, PASSWORD) for n in ('alice', 'bob'))
    def rows(did):
        return [{'id': did, 'physical_id': 'same-miot-device', 'name': 'Play',
                 'hardware': 'L05B', 'supported': True, 'reason': ''}]
    store.sync_devices(a, rows('mina-one'))
    store.sync_devices(b, rows('mina-two'))
    assert not store.devices(b)[0]['supported']
    with pytest.raises(Rejected):
        store.select_devices(b, ['mina-two'])
