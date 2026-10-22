"""Encrypted key management, bounded failover and conservative usage accounting."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import llm_router
from platform_app.runtime import Runtime
from platform_app.store import Rejected
from test_platform import web, store, CFG, PASSWORD, login, invite_user, response  # noqa: F401

SECRET = 'sk-synthetic-secret-123456789'
BACKUP = 'sk-synthetic-backup-987654321'


def service(name='备用千问', priority=20, **extra):
    return {'name': name, 'provider': 'qwen', 'model': 'qwen-plus', 'api_key': BACKUP,
            'priority': priority, 'enabled': True, **extra}


class UpstreamError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__('Synthetic upstream error with credential ' + SECRET)


def test_key_management_masks_encrypts_preserves_and_requires_new_key_on_provider_change(web):
    client, app = web
    login(client)
    values = service(api_key=SECRET)
    result = client.post('/api/admin/model-services', json=values)
    assert result.status_code == 200
    ident = result.json()['id']
    metadata = client.get('/api/admin/model-services')
    assert SECRET not in metadata.text and 'api_key' not in metadata.text and 'secret' not in metadata.text
    row = next(r for r in metadata.json()['services'] if r['id'] == ident)
    assert row['key_hint'].endswith(SECRET[-4:]) and row['last_result'] == 'untested'
    with app.state.store.db() as db:
        encrypted = db.execute('SELECT secret FROM model_services WHERE id=?', (ident,)).fetchone()[0]
    assert SECRET.encode() not in encrypted
    assert app.state.store.vault.open(encrypted, 'model-service:'+ident) == SECRET
    values.pop('api_key')
    assert client.put('/api/admin/model-services/'+ident, json={**values, 'name': '新备注'}).status_code == 200
    with app.state.store.db() as db:
        assert db.execute('SELECT secret FROM model_services WHERE id=?', (ident,)).fetchone()[0] == encrypted
    changed = client.put('/api/admin/model-services/'+ident, json={**values, 'provider': 'deepseek'})
    assert changed.status_code == 400 and '对应的 API Key' in changed.json()['error']
    assert client.post('/api/admin/model-services', json={**values, 'api_key': 'short'}).status_code == 422
    assert client.post('/api/admin/model-services', json={**values, 'provider': 'arbitrary-endpoint'}).status_code == 422
    assert client.post('/api/admin/model-services', json={**values, 'api_key': SECRET, 'base_url': 'http://localhost'}).status_code == 422
    assert client.post('/api/admin/model-services', json={**values, 'api_key': SECRET}).status_code == 400  # duplicate priority
    assert client.delete('/api/admin/model-services/'+ident).status_code == 200
    assert not any(r['id'] == ident for r in client.get('/api/admin/model-services').json()['services'])


def test_model_service_requires_admin_and_recent_verification_and_does_not_leak_key_in_validation(web):
    client, app = web
    login(client)
    with app.state.store.db() as db:
        db.execute('UPDATE session_security SET verified_at=1')
    assert client.post('/api/admin/model-services', json=service()).status_code == 428
    login(client)
    malformed = client.post('/api/admin/model-services', json=service(api_key=SECRET+'\nINVALID'))
    assert malformed.status_code == 422 and SECRET not in malformed.text
    invite_user(client)
    assert client.get('/api/admin/model-services').status_code == 403
    assert client.post('/api/admin/model-services', json=service()).status_code == 403
    assert client.put('/api/admin/model-services/any', json=service()).status_code == 403
    assert client.delete('/api/admin/model-services/any').status_code == 403
    assert 'model_services' not in client.get('/api/me').json()


@pytest.mark.parametrize('failure', [UpstreamError(401), UpstreamError(429), UpstreamError(503), httpx.ConnectError('offline')])
def test_failover_uses_backup_once_marks_unknown_and_cools_failed_primary(store, monkeypatch, failure):
    async def run():
        uid = store.create_user('alice', PASSWORD)
        runtime = Runtime(store, deepcopy(CFG))
        runtime.services.save(None, service())
        call = AsyncMock(side_effect=[failure, response('备用回复'), response('再次回复')])
        monkeypatch.setattr(llm_router, 'acompletion', call)
        result = await runtime.text(uid, 'question', 'synthetic-request-111')
        assert result['answer'] == '备用回复' and call.await_count == 2
        assert call.await_args_list[0].kwargs['api_key'] == 'fake-key'
        assert call.await_args_list[1].kwargs['api_key'] == BACKUP
        assert call.await_args_list[1].kwargs['model'] == 'openai/qwen-plus'
        assert call.await_args_list[1].kwargs['api_base'] == 'https://dashscope.aliyuncs.com/compatible-mode/v1'
        stats = store.summary(uid)
        assert stats['today'] == 1 and stats['totals']['unknown_usage'] == 1
        assert stats['requests'][0]['total_tokens'] is None and stats['requests'][0]['status'] == 'completed'
        attempts = runtime.services.recent_attempts()
        assert [a['status'] for a in reversed(attempts)] == ['failed', 'completed']
        assert SECRET not in str(attempts) and BACKUP not in str(attempts)
        await runtime.text(uid, 'next', 'synthetic-request-222')
        assert call.await_count == 3 and call.await_args.kwargs['api_key'] == BACKUP
    asyncio.run(run())


class ContentPolicyViolationError(UpstreamError):
    pass


@pytest.mark.parametrize('failure', [TimeoutError(), UpstreamError(400), UpstreamError(504), ContentPolicyViolationError(403)])
def test_ambiguous_timeout_parameter_and_content_failures_do_not_reissue(store, monkeypatch, failure):
    async def run():
        uid = store.create_user('alice', PASSWORD)
        runtime = Runtime(store, deepcopy(CFG))
        runtime.services.save(None, service())
        call = AsyncMock(side_effect=failure)
        monkeypatch.setattr(llm_router, 'acompletion', call)
        with pytest.raises(Rejected):
            await runtime.text(uid, 'question', 'synthetic-no-retry-123')
        assert call.await_count == 1 and len(runtime.services.recent_attempts()) == 1
        assert store.summary(uid)['today'] == 1
    asyncio.run(run())


def test_attempt_cap_and_disabled_all_never_fall_back_to_environment_key(store, monkeypatch):
    async def run():
        uid = store.create_user('alice', PASSWORD)
        runtime = Runtime(store, deepcopy(CFG))
        for n in range(3):
            runtime.services.save(None, service(name=str(n), priority=20+n))
        call = AsyncMock(side_effect=UpstreamError(503))
        monkeypatch.setattr(llm_router, 'acompletion', call)
        with pytest.raises(Rejected):
            await runtime.text(uid, 'question', 'synthetic-max-attempts')
        assert call.await_count == 3
        for row in runtime.services.public():
            runtime.services.delete(row['id'])
        assert runtime.services.managed()
        with pytest.raises(Rejected, match='没有可用'):
            await runtime.text(uid, 'question', 'synthetic-no-services')
        assert call.await_count == 3 and store.summary(uid)['today'] == 1
    asyncio.run(run())


def test_service_edit_resets_cooldown_bootstrap_is_once_and_settings_preserve_services(store, monkeypatch):
    async def run():
        uid = store.create_user('alice', PASSWORD)
        runtime = Runtime(store, deepcopy(CFG))
        rows = runtime.services.public()
        assert len(rows) == 1
        primary = rows[0]
        call = AsyncMock(side_effect=[UpstreamError(401), response()])
        monkeypatch.setattr(llm_router, 'acompletion', call)
        with pytest.raises(Rejected):
            await runtime.text(uid, 'question', 'synthetic-key-failed')
        assert runtime.services.public()[0]['cooldown_seconds'] > 0
        runtime.services.save(primary['id'], service(name='原服务已更新', provider='deepseek', model='deepseek-chat',
                                                    priority=primary['priority'], api_key=SECRET))
        assert runtime.services.public()[0]['last_result'] == 'untested'
        await runtime.text(uid, 'question', 'synthetic-key-replaced')
        assert call.await_args.kwargs['api_key'] == SECRET
        store.update_settings({'daily_limit': 51})
        assert runtime.services.managed()
        assert len(Runtime(store, deepcopy(CFG)).services.public()) == 1
    asyncio.run(run())


def test_model_cancellation_records_one_interrupted_attempt_and_does_not_retry(store, monkeypatch):
    async def run():
        uid = store.create_user('alice', PASSWORD)
        runtime = Runtime(store, deepcopy(CFG))
        runtime.services.save(None, service())
        call = AsyncMock(side_effect=asyncio.CancelledError())
        monkeypatch.setattr(llm_router, 'acompletion', call)
        with pytest.raises(asyncio.CancelledError):
            await runtime.text(uid, 'question', 'synthetic-cancelled-model')
        assert call.await_count == 1
        assert runtime.services.recent_attempts()[0]['status'] == 'interrupted'
        assert runtime.model_active == 0 and runtime.semaphore.active == 0
    asyncio.run(run())
