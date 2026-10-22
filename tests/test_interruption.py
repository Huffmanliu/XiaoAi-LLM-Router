"""Synthetic voice interactions: cancellation, accounting, speech and isolation."""
import asyncio
from copy import deepcopy
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import llm_router
from mi_conversation import MiSpeaker
from platform_app.interruption import DeviceTurn, stop_requested
from platform_app.runtime import Runtime
from test_capacity import eventually
from test_platform import store as store_fixture, CFG, PASSWORD, bound, policy, response

store = store_fixture


def record(rid, text='深思模拟问题', **extra):
    return {'request_id': rid, 'text': text, 'ts': time.time() + .001, **extra}


class Rig:
    def __init__(self, store, monkeypatch):
        self.uid = store.create_user('alice', PASSWORD)
        bound(store, self.uid)
        policy(store, self.uid, minute_limit=100, daily_limit=100)
        store.set_dialogue(self.uid, {'routing_mode': 'keyword', 'answer_prefix': ''})
        self.runtime = Runtime(store, deepcopy(CFG))
        self.items = []
        self.polls = 0
        self.sent = []
        self.speaker = MiSpeaker(None, None, {'tts': {'max_chunk_chars': 10, 'chars_per_second': 1000}})

        async def send(did, text, **kwargs):
            self.sent.append((did, text))
            return True

        async def recent(did):
            self.polls += 1
            return self.items

        self.speaker._tts_once = send
        self.speaker.recent_asks = recent
        monkeypatch.setattr(self.runtime, 'context', AsyncMock(return_value={
            'speaker': self.speaker, 'account': SimpleNamespace(token=True)}))
        monkeypatch.setattr('platform_app.runtime.POLL_INTERVAL', .005)
        self.worker = asyncio.create_task(self.runtime.device_loop(self.uid, 'speaker-a'))

    async def publish(self, items):
        self.items = items
        polls = self.polls
        await eventually(lambda: self.polls > polls + 1)

    async def close(self):
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
        assert self.runtime.model_active == self.runtime.model_waiting == self.runtime.speech_pending == 0
        assert self.runtime.semaphore.active == 0


@pytest.mark.parametrize('text,expected', [
    ('不用说了。', True), ('深思，停止回答！', True), ('停止播报', True),
    ('解释一下“停止回答”的意思', False), ('不要说了是什么意思', False),
    ('为什么音乐突然停止', False),
])
def test_stop_commands_match_entire_utterance_only(text, expected):
    assert stop_requested(text, ['深思']) is expected


def test_observation_ignores_replays_answer_updates_bad_dates_and_late_older_queries():
    turn = DeviceTurn()
    now = time.time()
    original = record('one', ts=now)
    assert turn.observe([original], now - 1, now)
    token = turn.token()
    assert not turn.observe([{**original, 'native_answer': '原生回答补充'}], now - 1, now)
    invalid = [record('stale', ts=now - 70), record('future', ts=now + 60),
               record('nan', ts=float('nan')), record('bad', ts='bad'), record('empty', text=''), None]
    assert not turn.observe(invalid, now - 100, now)
    assert token.current()
    # Different identities with tied timestamps are valid, not duplicate answers.
    second = record('two', ts=now)
    assert turn.observe([original, second], now - 1, now)
    assert not token.current() and turn.request_id == 'two'
    assert not turn.observe([record('late', ts=now - .5)], now - 1, now)
    assert turn.request_id == 'two'


def test_native_audio_metadata_arriving_late_invalidates_the_same_query_once():
    turn = DeviceTurn()
    now = time.time()
    item = record('music', ts=now)
    assert turn.observe([item], now - 1, now)
    token = turn.token()
    item['takeover_allowed'] = False
    assert turn.observe([item], now - 1, now)
    assert not token.current()
    assert not turn.observe([item], now - 1, now)


@pytest.mark.parametrize('answers,expected,eligible', [
    ([{'type': 'LLM', 'llm': {'text': '原生模型答复'}}], '原生模型答复', True),
    ([{'type': 'TTS', 'tts': {'text': '准备播放'}}, {'type': 'Audio', 'audio': {}}], '准备播放', False),
    ([{'type': 'Action', 'tts': {'text': '控制失败'}}], '控制失败', False),
])
def test_cloud_message_types_preserve_text_without_taking_over_audio_or_actions(answers, expected, eligible):
    async def run():
        class Reply:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def json(self, **kwargs):
                return {'code': 0, 'data': {'records': [
                    {'query': '模拟问题', 'time': 1000, 'requestId': 'synthetic', 'answers': answers}]}}

        account = SimpleNamespace(token={'userId': 'qa', 'micoapi': ['fake', 'fake']},
                                  _session=SimpleNamespace(get=lambda *a, **k: Reply()))
        speaker = MiSpeaker(account, None, {'mi': {'conversation_source': 'cloud'}})
        speaker._hardware = {'synthetic': 'L05B'}
        item = (await speaker.recent_asks('synthetic'))[0]
        assert item['native_answer'] == expected and item['takeover_allowed'] is eligible
    asyncio.run(run())


def test_new_question_cancels_model_and_only_latest_gets_context_and_speech(store, monkeypatch):
    async def run():
        rig = Rig(store, monkeypatch)
        entered, cancelled = asyncio.Event(), asyncio.Event()
        calls = []

        async def model(**kwargs):
            calls.append(kwargs['messages'])
            if len(calls) == 1:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return response('新回答')

        monkeypatch.setattr(llm_router, 'acompletion', model)
        try:
            await rig.publish([record('old')])
            await asyncio.wait_for(entered.wait(), 2)
            await rig.publish([record('new', text='深思新问题')])
            await eventually(lambda: bool(rig.sent))
            assert cancelled.is_set() and len(calls) == 2
            assert [part for _, part in rig.sent] == ['新回答']
            assert len(calls[1]) == 2  # Fixed system prompt + new query, no old answer.
            rows = store.summary(rig.uid)['requests']
            old = next(r for r in rows if r['request_id'].endswith(':old'))
            assert old['status'] == 'interrupted' and old['started'] and old['total_tokens'] is None
            assert store.summary(rig.uid)['today'] == 2
            await eventually(lambda: bool(rig.runtime.sessions(rig.uid).get('speaker-a').to_llm_messages()))
            assert len(store.history(rig.uid)) == 1
        finally:
            await rig.close()
    asyncio.run(run())


@pytest.mark.parametrize('native', [False, True])
def test_stop_or_native_question_cancels_waiting_model_without_charge(store, monkeypatch, native):
    async def run():
        rig = Rig(store, monkeypatch)
        runtime = rig.runtime
        runtime.configure_capacity({'model_concurrency': 1, 'queue_limit': 8, 'queue_timeout': 5})
        model = AsyncMock(return_value=response())
        monkeypatch.setattr(llm_router, 'acompletion', model)
        await runtime.semaphore.acquire()
        runtime.model_active += 1
        try:
            await rig.publish([record('queued')])
            await eventually(lambda: runtime.model_waiting == 1)
            await rig.publish([record('control', text='明天天气怎么样' if native else '深思，不用说了！',
                                      native_answer='晴天' if native else '')])
            await eventually(lambda: runtime.model_waiting == 0)
            assert not model.called and not rig.sent
            row = store.summary(rig.uid)['requests'][0]
            assert row['status'] == 'cancelled' and not row['started']
            assert store.summary(rig.uid)['today'] == 0
        finally:
            runtime.model_active -= 1
            runtime.semaphore.release()
            await rig.close()
    asyncio.run(run())


def test_new_query_stops_old_chunks_even_before_native_failure_decision(store, monkeypatch):
    async def run():
        rig = Rig(store, monkeypatch)
        model = AsyncMock(return_value=response('旧回答第一段很长。旧回答第二段也很长。'))
        monkeypatch.setattr(llm_router, 'acompletion', model)
        try:
            await rig.publish([record('old')])
            await eventually(lambda: bool(rig.sent))
            # Native reply isn't available yet: interruption must not wait two seconds.
            store.set_dialogue(rig.uid, {'routing_mode': 'fallback'})
            await rig.publish([record('pending', text='新的复杂问题', native_answer='')])
            await eventually(lambda: store.summary(rig.uid)['requests'][0]['speech_status'] == 'interrupted')
            assert len(rig.sent) == 1 and model.await_count == 1
            assert rig.runtime.sessions(rig.uid).get('speaker-a').to_llm_messages() == []
            assert len(store.history(rig.uid)) == 1  # Generated answer retained, not a claim of full playback.
            assert store.summary(rig.uid)['requests'][0]['status'] == 'completed'
            # The same record later gains native text: it must not acquire a new turn.
            await rig.publish([{**rig.items[0], 'native_answer': '这是正常原生回答'}])
            assert len(rig.sent) == 1 and model.await_count == 1
        finally:
            await rig.close()
    asyncio.run(run())


def test_stop_during_final_chunk_estimate_still_marks_interrupted_and_keeps_context_clean(store, monkeypatch):
    async def run():
        rig = Rig(store, monkeypatch)
        monkeypatch.setattr(llm_router, 'acompletion', AsyncMock(return_value=response('很短的回答')))
        try:
            await rig.publish([record('old')])
            await eventually(lambda: bool(rig.sent))
            assert not rig.runtime.sessions(rig.uid).get('speaker-a').to_llm_messages()
            await rig.publish([record('stop', text='停止回答')])
            await eventually(lambda: store.summary(rig.uid)['requests'][0]['speech_status'] == 'interrupted')
            assert not rig.runtime.sessions(rig.uid).get('speaker-a').to_llm_messages()
        finally:
            await rig.close()
    asyncio.run(run())


def test_burst_keeps_latest_and_discards_new_question_superseded_by_native_control(store, monkeypatch):
    async def run():
        rig = Rig(store, monkeypatch)
        entered = asyncio.Event()
        calls = []

        async def model(**kwargs):
            calls.append(kwargs['messages'][-1]['content'])
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(llm_router, 'acompletion', model)
        try:
            await rig.publish([record('first')])
            await asyncio.wait_for(entered.wait(), 2)
            stamp = time.time() + .001
            await rig.publish([record('newest', text='深思最后一个问题', ts=stamp + .001),
                               record('middle', text='深思中间问题', ts=stamp)])
            await eventually(lambda: len(calls) == 2)
            assert calls[-1] == '深思最后一个问题'
            await rig.publish([record('unused', ts=stamp + .002),
                               record('music', text='播放音乐', ts=stamp + .003, takeover_allowed=False)])
            await eventually(lambda: rig.runtime.model_active == 0)
            assert len(calls) == 2 and not rig.sent and rig.runtime.speech_pending == 0
        finally:
            await rig.close()
    asyncio.run(run())


def test_late_model_result_after_swallowed_cancellation_never_speaks_or_enters_memory(store, monkeypatch):
    async def run():
        rig = Rig(store, monkeypatch)
        entered = asyncio.Event()

        async def model(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return response('已经作废的结果')

        monkeypatch.setattr(llm_router, 'acompletion', model)
        try:
            await rig.publish([record('old')])
            await asyncio.wait_for(entered.wait(), 2)
            await rig.publish([record('stop', text='不用说了')])
            await eventually(lambda: rig.runtime.model_active == 0)
            assert not rig.sent and not store.history(rig.uid)
            assert not rig.runtime.sessions(rig.uid).get('speaker-a').to_llm_messages()
            row = store.summary(rig.uid)['requests'][0]
            assert row['status'] == 'interrupted' and row['total_tokens'] == 20
        finally:
            await rig.close()
    asyncio.run(run())


def test_chunk_guard_rechecks_before_next_hardware_command():
    async def run():
        speaker = MiSpeaker(None, None, {'tts': {'max_chunk_chars': 10}})
        current = True

        async def send(did, text, **kwargs):
            nonlocal current
            current = False
            return True

        speaker._tts_once = AsyncMock(side_effect=send)
        with pytest.raises(asyncio.CancelledError):
            await speaker.tts('synthetic-device', '第一段回答需要分成好几个片段。这是后面的文字。',
                              is_current=lambda: current)
        assert speaker._tts_once.await_count == 1
    asyncio.run(run())


def test_speech_preparation_finishing_after_invalidation_never_sends_hardware_action(monkeypatch):
    async def run():
        import miservice

        current = True

        async def devices():
            nonlocal current
            current = False  # Simulate a newer query observed during this await.
            return [{'deviceID': 'synthetic', 'miotDID': 'fake', 'hardware': 'L05B'}]

        action = AsyncMock(return_value=0)
        monkeypatch.setattr(miservice, 'MiIOService', lambda account: SimpleNamespace(miot_action=action))
        speaker = MiSpeaker(None, SimpleNamespace(device_list=devices), {'tts': {'method': 'miot'}})
        with pytest.raises(asyncio.CancelledError):
            await speaker.tts('synthetic', '模拟回答', is_current=lambda: current)
        action.assert_not_awaited()
    asyncio.run(run())


def test_replacement_in_full_queue_reuses_cancelled_waiter_budget(store, monkeypatch):
    async def run():
        rig = Rig(store, monkeypatch)
        runtime = rig.runtime
        runtime.configure_capacity({'model_concurrency': 1, 'queue_limit': 1, 'queue_timeout': 5})
        await runtime.semaphore.acquire()
        runtime.model_active += 1
        model = AsyncMock(return_value=response('替换后的回答'))
        monkeypatch.setattr(llm_router, 'acompletion', model)
        try:
            await rig.publish([record('old')])
            await eventually(lambda: runtime.model_waiting == 1)
            await rig.publish([record('new', text='深思替换后的问题')])
            await eventually(lambda: len(store.summary(rig.uid)['requests']) == 2)
            assert runtime.capacity_rejected == 0 and not model.called
            runtime.model_active -= 1
            runtime.semaphore.release()
            await eventually(lambda: bool(rig.sent))
            assert model.await_args.kwargs['messages'][-1]['content'] == '深思替换后的问题'
            assert store.summary(rig.uid)['today'] == 1
        finally:
            if runtime.model_active and not model.called:
                runtime.model_active -= 1
                runtime.semaphore.release()
            await rig.close()
    asyncio.run(run())


def test_cancellation_translated_to_retryable_error_does_not_use_backup(store, monkeypatch):
    async def run():
        rig = Rig(store, monkeypatch)
        rig.runtime.services.save(None, {'name': '备用', 'provider': 'deepseek', 'model': 'deepseek-chat',
                                         'api_key': 'synthetic-key-backup', 'enabled': True, 'priority': 20})
        entered = asyncio.Event()
        calls = []

        async def model(**kwargs):
            calls.append(kwargs['model'])
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise ConnectionError('synthetic transport cancellation') from None

        monkeypatch.setattr(llm_router, 'acompletion', model)
        try:
            await rig.publish([record('old')])
            await asyncio.wait_for(entered.wait(), 2)
            await rig.publish([record('stop', text='停止回答')])
            await eventually(lambda: rig.runtime.model_active == 0)
            assert len(calls) == 1 and not rig.sent
            assert rig.runtime.services.health == {}
            assert rig.runtime.services.recent_attempts()[0]['status'] == 'interrupted'
        finally:
            await rig.close()
    asyncio.run(run())


@pytest.mark.parametrize('other_user', [False, True])
def test_interruption_isolated_from_other_device_and_other_user(store, monkeypatch, other_user):
    async def run():
        rig = Rig(store, monkeypatch)
        uid = store.create_user('bob', PASSWORD) if other_user else rig.uid
        if other_user:
            bound(store, uid, did='speaker-b')
        else:
            store.sync_devices(uid, [{'id': did, 'name': did, 'hardware': 'L05B', 'supported': True, 'reason': ''}
                                     for did in ('speaker-a', 'speaker-b')])
            store.select_devices(uid, ['speaker-a', 'speaker-b'])
        store.set_dialogue(uid, {'routing_mode': 'keyword'})
        entered = asyncio.Event()
        other_release = asyncio.Event()

        async def model(**kwargs):
            if kwargs['messages'][-1]['content'] == '深思另一个设备':
                entered.set()
                await other_release.wait()
                return response('另一设备正常回答')
            await asyncio.Event().wait()

        monkeypatch.setattr(llm_router, 'acompletion', model)
        other_speaker = SimpleNamespace(tts=AsyncMock(return_value=True))
        other = asyncio.create_task(rig.runtime.handle(uid, 'speaker-b', '深思另一个设备', 'speaker', 'other', other_speaker))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await rig.publish([record('old')])
            await eventually(lambda: rig.runtime.model_active == 2)
            await rig.publish([record('stop', text='停止回答')])
            await eventually(lambda: rig.runtime.model_active == 1)
            assert not other.done()
            other_release.set()
            await other
            other_speaker.tts.assert_awaited_once()
            assert not rig.sent
        finally:
            other.cancel()
            await asyncio.gather(other, return_exceptions=True)
            await rig.close()
    asyncio.run(run())
