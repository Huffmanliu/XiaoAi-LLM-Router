"""Bounded, resizable admission and polling independent of sequential replies."""
import asyncio
from copy import deepcopy
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from platform_app.capacity import ModelGate
from platform_app.runtime import Runtime
from platform_app.store import Rejected, Store
from test_platform import store, web, CFG, PASSWORD, login, invite_user, bound  # noqa: F401


async def eventually(condition):
    async with asyncio.timeout(2):
        while not condition():
            await asyncio.sleep(.001)


def test_gate_live_resize_fifo_and_cancel_after_grant():
    async def run():
        gate = ModelGate(1)
        await gate.acquire()
        first = asyncio.create_task(gate.acquire())
        second = asyncio.create_task(gate.acquire())
        await eventually(lambda: len(gate.waiters) == 2)
        gate.resize(2)
        await first
        assert not second.done()
        gate.resize(1)
        gate.release()
        assert gate.active == 1 and not second.done()
        gate.release()
        await second
        assert gate.active == 1
        waiting = asyncio.create_task(gate.acquire())
        await eventually(lambda: len(gate.waiters) == 1)
        gate.release()  # Grant, then cancel before the waiter resumes.
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        assert gate.active == 0 and not gate.waiters
        await gate.acquire()
        gate.release()
    asyncio.run(run())


def test_global_queue_bound_expiry_releases_quota_and_permits(store):
    async def run():
        uid = store.create_user('alice', PASSWORD)
        runtime = Runtime(store, deepcopy(CFG))
        runtime.configure_capacity({'model_concurrency': 1, 'queue_limit': 1, 'queue_timeout': 1})
        held, release = asyncio.Event(), asyncio.Event()
        rid = store.reserve(uid, 'web-debug', 'web', 'held')
        async def hold():
            async with runtime.model_slot(rid):
                held.set()
                await release.wait()
        first = asyncio.create_task(hold())
        await held.wait()
        queued_rid = store.reserve(uid, 'web-debug', 'web', 'queued')
        async def queued():
            async with runtime.model_slot(queued_rid, time.monotonic()+.04):
                pytest.fail('Expired request cannot start')
        waiting = asyncio.create_task(queued())
        await eventually(lambda: runtime.model_waiting == 1)
        overflow = store.reserve(uid, 'web-debug', 'web', 'overflow')
        with pytest.raises(Rejected, match='队列已满'):
            async with runtime.model_slot(overflow):
                pytest.fail('Queue overflow cannot start')
        with pytest.raises(Rejected, match='等待超时'):
            await waiting
        snapshot = runtime.capacity()
        assert snapshot['active'] == 1 and snapshot['waiting'] == 0
        assert snapshot['rejected'] == snapshot['expired'] == 1
        with store.db() as db:
            assert db.execute("SELECT COUNT(*) FROM requests WHERE status='cancelled' AND started=0").fetchone()[0] == 2
        release.set()
        await first
        assert runtime.capacity()['active'] == 0 and runtime.semaphore.active == 0
    asyncio.run(run())


def test_capacity_api_admin_only_bounds_persistence_and_independence_from_defaults(web):
    client, app = web
    login(client)
    limits = {'model_concurrency': 8, 'queue_limit': 100, 'queue_timeout': 20}
    assert client.put('/api/admin/capacity', json=limits).status_code == 200
    assert app.state.runtime.semaphore.limit == 8
    assert client.get('/api/admin/capacity').json()['limits'] == limits
    assert client.put('/api/admin/defaults', json={'daily_limit': 55}).status_code == 200
    assert app.state.store.runtime_limits() == limits
    assert Store(app.state.store.root, app.state.store.vault).runtime_limits() == limits
    assert client.put('/api/admin/capacity', json={**limits, 'model_concurrency': 0}).status_code == 422
    assert client.put('/api/admin/capacity', json={**limits, 'queue_timeout': 61}).status_code == 422
    assert client.put('/api/admin/capacity', json={**limits, 'queue_limit': True}).status_code == 422
    invite_user(client)
    assert client.get('/api/admin/capacity').status_code == 403
    assert client.put('/api/admin/capacity', json=limits).status_code == 403
    assert 'runtime_limits' not in client.get('/api/me').json()['policy']


def test_polling_continues_and_pause_allows_current_answer_to_finish(store, monkeypatch):
    async def run():
        uid = store.create_user('alice', PASSWORD)
        bound(store, uid)
        store.set_dialogue(uid, {'routing_mode': 'keyword'})
        runtime = Runtime(store, deepcopy(CFG))
        polls, spoken = [], []
        entered, release = asyncio.Event(), asyncio.Event()
        async def recent(did):
            number = len(polls)
            polls.append(number)
            if number == 0:
                return [{'request_id': '0', 'ts': time.time()+.001, 'text': '深思 test', 'native_answer': ''}]
            return []
        async def handle(uid, did, text, source, rid, speaker, **kwargs):
            spoken.append(rid)
            if rid == '0':
                entered.set()
                await release.wait()
        ctx = {'speaker': SimpleNamespace(recent_asks=recent), 'account': SimpleNamespace(token=True)}
        monkeypatch.setattr(runtime, 'context', AsyncMock(return_value=ctx))
        monkeypatch.setattr(runtime, 'handle', handle)
        monkeypatch.setattr('platform_app.runtime.POLL_INTERVAL', .01)
        polling = asyncio.create_task(runtime.device_loop(uid, 'speaker-a'))
        await entered.wait()
        await eventually(lambda: len(polls) >= 3)
        assert spoken == ['0'] and runtime.speech_pending == 0
        store.select_devices(uid, [])
        release.set()
        await asyncio.wait_for(polling, 2)
        assert spoken == ['0'] and runtime.speech_pending == 0
    asyncio.run(run())


def test_device_newest_question_replaces_old_answer_without_overlap(store, monkeypatch):
    async def run():
        uid = store.create_user('alice', PASSWORD)
        bound(store, uid)
        store.set_dialogue(uid, {'routing_mode': 'keyword'})
        runtime = Runtime(store, deepcopy(CFG))
        entered, release = asyncio.Event(), asyncio.Event()
        polls, spoken = [], []
        in_reply = 0
        async def recent(did):
            number = len(polls)
            polls.append(number)
            if number in (0, 1):
                return [{'request_id': str(number), 'ts': time.time()+.001, 'text': '深思 test'}]
            return []
        async def handle(uid, did, text, source, rid, speaker, **kwargs):
            nonlocal in_reply
            in_reply += 1
            assert in_reply == 1
            spoken.append(rid)
            try:
                if rid == '0':
                    entered.set()
                    await release.wait()
            finally:
                in_reply -= 1
        monkeypatch.setattr(runtime, 'context', AsyncMock(return_value={
            'speaker': SimpleNamespace(recent_asks=recent), 'account': SimpleNamespace(token=True)}))
        monkeypatch.setattr(runtime, 'handle', handle)
        monkeypatch.setattr('platform_app.runtime.POLL_INTERVAL', .01)
        polling = asyncio.create_task(runtime.device_loop(uid, 'speaker-a'))
        await entered.wait()
        await eventually(lambda: len(spoken) == 2 and in_reply == 0)
        assert not release.is_set()  # New question cancelled the old wait.
        assert spoken == ['0', '1'] and runtime.speech_pending == 0
        runtime.stopping = True
        await asyncio.wait_for(polling, 2)
        assert runtime.speech_pending == 0
        # An expired item in the per-device queue never reaches model admission.
        queue = asyncio.Queue()
        runtime.speech_pending = 1
        queue.put_nowait(({'request_id': 'expired'}, time.monotonic()-1))
        queue.put_nowait(None)
        await runtime.reply_loop(uid, 'speaker-a', SimpleNamespace(), queue)
        assert runtime.capacity_expired == 1
    asyncio.run(run())


def test_synthetic_thousand_requests_never_exceed_capacity_or_queue(store):
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        runtime.configure_capacity({'model_concurrency': 8, 'queue_limit': 32, 'queue_timeout': 1})
        # Synthetic reservations isolate scheduling from SQLite throughput; no models or network.
        runtime.store = SimpleNamespace(update_request=lambda *args, **kwargs: None)
        release = asyncio.Event()
        accepted, rejected, peak = [], [], []
        async def request(index):
            try:
                async with runtime.model_slot(str(index)):
                    accepted.append(index)
                    peak.append(runtime.model_active)
                    await release.wait()
            except Rejected:
                rejected.append(index)
        tasks = [asyncio.create_task(request(i)) for i in range(1000)]
        await eventually(lambda: runtime.model_active == 8 and len(rejected) == 960)
        assert runtime.model_waiting == 32
        runtime.configure_capacity({'model_concurrency': 16, 'queue_limit': 32, 'queue_timeout': 1})
        await eventually(lambda: runtime.model_active == 16)
        release.set()
        await asyncio.gather(*tasks)
        assert len(accepted) == 40 and max(peak) == 16
        assert runtime.model_active == runtime.model_waiting == runtime.semaphore.active == 0
    asyncio.run(run())


def test_cancelled_poller_drains_reply_tasks_queue_and_model_permit(store, monkeypatch):
    async def run():
        uid = store.create_user('alice', PASSWORD)
        bound(store, uid)
        store.set_dialogue(uid, {'routing_mode': 'keyword'})
        runtime = Runtime(store, deepcopy(CFG))
        entered = asyncio.Event()
        never = asyncio.Event()
        count = 0
        async def recent(did):
            nonlocal count
            count += 1
            if count == 1:
                return [{'request_id': 'one', 'ts': time.time()+.001, 'text': '深思 test'}]
            return []
        async def model(**kwargs):
            entered.set()
            await never.wait()
        monkeypatch.setattr('platform_app.runtime.POLL_INTERVAL', .01)
        monkeypatch.setattr('llm_router.acompletion', model)
        monkeypatch.setattr(runtime, 'context', AsyncMock(return_value={
            'speaker': SimpleNamespace(recent_asks=recent, tts=AsyncMock()),
            'account': SimpleNamespace(token=True)}))
        polling = asyncio.create_task(runtime.device_loop(uid, 'speaker-a'))
        await entered.wait()
        await eventually(lambda: count > 1)
        polling.cancel()
        await asyncio.gather(polling, return_exceptions=True)
        assert runtime.model_active == runtime.model_waiting == runtime.speech_pending == 0
        assert runtime.semaphore.active == 0
        row = store.summary(uid)['requests'][0]
        assert row['status'] == 'interrupted' and row['started']
    asyncio.run(run())


def test_polling_metadata_checks_do_not_decrypt_account_secrets(store, monkeypatch):
    uid = store.create_user('alice', PASSWORD)
    bound(store, uid)
    runtime = Runtime(store, deepcopy(CFG))
    def cannot_read_credentials(*args):
        pytest.fail('Device status must not decrypt account credentials')
    monkeypatch.setattr(store, 'account', cannot_read_credentials)
    assert runtime.enabled(uid, 'speaker-a')
    assert len(store.polling_candidates()) == 1
    store.select_devices(uid, [])
    assert not runtime.enabled(uid, 'speaker-a')
