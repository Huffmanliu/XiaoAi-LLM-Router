"""Admin diagnostics never become an ordinary-user general model proxy."""
import asyncio
from copy import deepcopy
import time
from unittest.mock import AsyncMock

import httpx
import pytest

import llm_router
from platform_app.model_services import TEST_MESSAGES
from platform_app.runtime import Runtime
from platform_app.store import Rejected, Store
from test_capacity import eventually
from test_model_services import service, UpstreamError, SECRET, BACKUP
from test_platform import (web as web_fixture, store as store_fixture, CFG, PASSWORD,
                           login, invite_user, response, policy)

web, store = web_fixture, store_fixture


def admin_runtime(store):
    uid = store.create_user('admin', PASSWORD, 'admin')
    return uid, Runtime(store, deepcopy(CFG))


def test_old_prompt_api_retired_and_ordinary_users_cannot_test_any_service(web, monkeypatch):
    client, app = web
    model = AsyncMock(return_value=response())
    monkeypatch.setattr(llm_router, 'acompletion', model)
    assert client.post('/api/debug', json={'text': 'test'}).status_code == 401
    admin = login(client)
    ident = app.state.runtime.services.public()[0]['id']
    assert client.post('/api/debug', json={'text': 'test', 'request_id': 'synthetic-request'}).status_code == 410
    alice, _ = invite_user(client)
    assert client.post('/api/debug', json={'text': 'test', 'request_id': 'synthetic-request'}).status_code == 403
    assert client.post('/api/admin/model-services/' + ident + '/test', json={}).status_code == 403
    assert client.post('/api/admin/model-services/missing/test', json={}).status_code == 403
    assert client.post('/api/dialogue/clear', json={'device_id': 'web-debug'}).status_code == 403
    model.assert_not_called()
    assert app.state.store.summary(admin['user_id'])['today'] == 0
    assert app.state.store.summary(alice['user_id'])['today'] == 0


def test_exact_disabled_service_fixed_input_no_fallback_context_history_or_speech(web, monkeypatch):
    client, app = web
    me = login(client)
    runtime, store = app.state.runtime, app.state.store
    runtime.sessions(me['user_id']).get('web-debug').append('PRIVATE-OLD-QUESTION', 'PRIVATE-OLD-ANSWER')
    ident = client.post('/api/admin/model-services', json=service(enabled=False)).json()['id']
    model = AsyncMock(return_value=response('PRIVATE-UNEXPECTED-PROVIDER-OUTPUT'))
    monkeypatch.setattr(llm_router, 'acompletion', model)
    assert client.post('/api/admin/model-services/' + ident + '/test', json={'text': 'custom prompt'}).status_code == 422
    result = client.post('/api/admin/model-services/' + ident + '/test', json={})
    assert result.status_code == 200 and result.json()['ok']
    kwargs = model.await_args.kwargs
    assert model.await_count == 1 and kwargs['api_key'] == BACKUP and kwargs['model'] == 'openai/qwen-plus'
    assert kwargs['messages'] == TEST_MESSAGES and kwargs['max_tokens'] == 64
    assert kwargs['timeout'] == 15 and kwargs['num_retries'] == 0 and not kwargs['stream']
    assert 'PRIVATE' not in result.text and SECRET not in result.text and BACKUP not in result.text
    assert not store.history(me['user_id']) and runtime.accounts == {} and runtime.workers == {}
    assert runtime.services.health == {}
    metadata = client.get('/api/admin/model-services').json()
    item = next(r for r in metadata['services'] if r['id'] == ident)
    assert not item['enabled'] and item['last_test']['result'] == 'success'
    assert item['last_result'] == 'untested' and item['last_test']['total_tokens'] == 20
    assert metadata['attempts'][0]['source'] == 'model_test'
    assert store.summary(me['user_id'])['today'] == 1
    assert store.summary(me['user_id'])['requests'][0]['speech_status'] == 'not_requested'
    restored = Runtime(Store(store.root, store.vault), deepcopy(CFG))
    assert next(r for r in restored.services.public() if r['id'] == ident)['last_test']['result'] == 'success'
    # Editing invalidates the old result while retaining the cross-version rate limit.
    client.put('/api/admin/model-services/' + ident, json=service(api_key=SECRET, enabled=False))
    changed = next(r for r in client.get('/api/admin/model-services').json()['services'] if r['id'] == ident)
    assert changed['last_test'] is None and changed['next_test_at'] > time.time()


@pytest.mark.parametrize('failure,kind', [
    (UpstreamError(401), 'authentication'), (UpstreamError(403), 'authentication'),
    (UpstreamError(402), 'balance'), (UpstreamError(404), 'model_not_found'),
    (UpstreamError(429), 'rate_limit'), (UpstreamError(503), 'unavailable'),
    (httpx.ConnectError('SYNTHETIC-PRIVATE-ERROR'), 'connection'),
    (TimeoutError('SYNTHETIC-PRIVATE-ERROR'), 'timeout'),
    (RuntimeError('SYNTHETIC-PRIVATE-ERROR'), 'request_failed'),
])
def test_diagnostic_failures_do_not_use_backup_or_echo_raw_errors(web, monkeypatch, caplog, failure, kind):
    client, app = web
    me = login(client)
    ident = app.state.runtime.services.public()[0]['id']
    app.state.runtime.services.save(None, service())
    model = AsyncMock(side_effect=failure)
    monkeypatch.setattr(llm_router, 'acompletion', model)
    result = client.post('/api/admin/model-services/' + ident + '/test', json={})
    assert result.status_code == 200 and result.json()['result'] == kind and not result.json()['ok']
    assert SECRET not in result.text and 'SYNTHETIC-PRIVATE-ERROR' not in result.text + caplog.text
    assert model.await_count == 1 and model.await_args.kwargs['api_key'] == 'fake-key'
    assert app.state.runtime.services.health == {}
    assert app.state.store.summary(me['user_id'])['totals']['unknown_usage'] == 1


def test_empty_response_keeps_known_usage_and_marks_test_failure(store, monkeypatch):
    async def run():
        uid, runtime = admin_runtime(store)
        ident = runtime.services.public()[0]['id']
        monkeypatch.setattr(llm_router, 'acompletion', AsyncMock(return_value=response('')))
        result = await runtime.test_model_service(uid, ident)
        assert result['result'] == 'invalid_response' and result['total_tokens'] == 20
        assert store.summary(uid)['totals']['unknown_usage'] == 0
        assert not store.history(uid)
    asyncio.run(run())


def test_persistent_rate_limits_same_service_busy_admin_and_hourly_total(store, monkeypatch):
    async def run():
        uid, runtime = admin_runtime(store)
        policy(store, uid, minute_limit=100, daily_limit=100)
        ident = runtime.services.public()[0]['id']
        monkeypatch.setattr(llm_router, 'acompletion', AsyncMock(return_value=response()))
        await runtime.test_model_service(uid, ident)
        restored = Runtime(Store(store.root, store.vault), deepcopy(CFG))
        with pytest.raises(Rejected, match='60秒'):
            await restored.test_model_service(uid, ident)
        now = time.time()
        # Seed only synthetic completed metadata at separate past times.
        for index in range(19):
            rid = store.reserve(uid, 'model-test:synthetic-' + str(index), 'model_test', 'test-' + str(index),
                                now=now - (index + 1) * 80)
            store.started(rid)
            store.finish(rid, {}, status='completed')
        backup = runtime.services.save(None, service())
        with pytest.raises(Rejected, match='每小时'):
            await runtime.test_model_service(uid, backup)
    asyncio.run(run())


@pytest.mark.parametrize('delete', [False, True])
def test_edit_or_delete_while_queued_never_tests_old_revision(store, monkeypatch, delete):
    async def run():
        uid, runtime = admin_runtime(store)
        ident = runtime.services.public()[0]['id']
        runtime.configure_capacity({'model_concurrency': 1, 'queue_limit': 4, 'queue_timeout': 5})
        await runtime.semaphore.acquire()
        runtime.model_active += 1
        model = AsyncMock(return_value=response())
        monkeypatch.setattr(llm_router, 'acompletion', model)
        task = asyncio.create_task(runtime.test_model_service(uid, ident))
        await eventually(lambda: runtime.model_waiting == 1)
        with pytest.raises(Rejected, match='正在处理'):
            await runtime.test_model_service(uid, ident)
        if delete:
            runtime.services.delete(ident)
        else:
            runtime.services.save(ident, service(provider='deepseek', model='deepseek-chat', priority=10))
        runtime.model_active -= 1
        runtime.semaphore.release()
        result = await task
        assert result['result'] == 'configuration_changed' and not result['started']
        model.assert_not_called()
        assert store.summary(uid)['today'] == 0 and runtime.semaphore.active == 0
    asyncio.run(run())


@pytest.mark.parametrize('started', [False, True])
def test_cancelled_test_releases_permits_and_accounts_for_actual_submission(store, monkeypatch, started):
    async def run():
        uid, runtime = admin_runtime(store)
        ident = runtime.services.public()[0]['id']
        entered = asyncio.Event()

        async def model(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(llm_router, 'acompletion', model)
        runtime.configure_capacity({'model_concurrency': 1, 'queue_limit': 4, 'queue_timeout': 5})
        if not started:
            await runtime.semaphore.acquire()
            runtime.model_active += 1
        task = asyncio.create_task(runtime.test_model_service(uid, ident))
        if started:
            await entered.wait()
        else:
            await eventually(lambda: runtime.model_waiting == 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if not started:
            runtime.model_active -= 1
            runtime.semaphore.release()
        assert runtime.model_active == runtime.model_waiting == runtime.semaphore.active == 0
        row = store.summary(uid)['requests'][0]
        assert bool(row['started']) == started
        assert row['status'] == ('interrupted' if started else 'cancelled')
        assert store.summary(uid)['today'] == int(started)
        assert runtime.services.public()[0]['last_test']['result'] == ('interrupted' if started else 'not_started')
    asyncio.run(run())


def test_recovery_clears_inflight_test_and_ordinary_role_cannot_bypass_store_guard(store):
    uid, runtime = admin_runtime(store)
    ident = runtime.services.public()[0]['id']
    rid = store.reserve(uid, 'model-test:' + ident, 'model_test', 'synthetic-recovery')
    runtime.services.begin_test(rid, runtime.services.test_snapshot(ident))
    store.started(rid)
    store.recover_requests()
    assert runtime.services.public()[0]['last_test']['result'] == 'interrupted'
    assert store.summary(uid)['requests'][0]['status'] == 'interrupted'
    alice = store.create_user('alice', PASSWORD)
    with pytest.raises(Rejected) as error:
        store.reserve(alice, 'model-test:' + ident, 'model_test', 'bypass')
    assert error.value.status == 403
