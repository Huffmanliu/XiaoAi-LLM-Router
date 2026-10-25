"""Safe, per-request speech failures; no real Xiaomi, model, or mail calls."""
import asyncio
from copy import deepcopy
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import llm_router
from mi_conversation import MiSpeaker
from mi_tts import SpeechFailure, public_speech_diagnostic
from platform_app.runtime import Runtime
from platform_app.store import Store
from test_platform import (CFG, PASSWORD, bound, response, login, invite_user,
                           store as store_fixture, web as web_fixture)

store = store_fixture
web = web_fixture
PRIVATE = 'PRIVATE-TRANSCRIPT-AND-KEY-DO-NOT-EXPOSE'


@pytest.mark.parametrize('failure,expected_stage,expected_kind', [
    ('stop_false', 'stop_music', 'rejected'),
    ('stop_exception', 'stop_music', 'request_error'),
    ('devices_exception', 'device_list', 'request_error'),
    ('devices_bad', 'device_list', 'invalid_response'),
    ('device_missing', 'device_prepare', 'device_missing'),
    ('metadata_missing', 'device_prepare', 'device_metadata'),
    ('miot_code', 'miot_submit', 'rejected'),
    ('miot_bad_code', 'miot_submit', 'invalid_response'),
    ('miot_exception', 'miot_submit', 'request_error'),
    ('miot_timeout', 'miot_submit', 'timeout'),
    ('mina_false', 'mina_submit', 'rejected'),
])
def test_speech_stage_stops_pipeline_without_retry_or_private_output(
        monkeypatch, caplog, failure, expected_stage, expected_kind):
    async def run():
        stop = AsyncMock(return_value=True)
        devices = AsyncMock(return_value=[{'deviceID': 'qa', 'hardware': 'LX04', 'miotDID': 'fake'}])
        action = AsyncMock(return_value=0)
        mina_tts = AsyncMock(return_value=False)
        if failure == 'stop_false':
            stop.return_value = False
        if failure == 'stop_exception':
            stop.side_effect = Exception(PRIVATE)
        if failure == 'devices_exception':
            devices.side_effect = Exception(PRIVATE)
        if failure == 'devices_bad':
            devices.return_value = {'private': PRIVATE}
        if failure == 'device_missing':
            devices.return_value = []
        if failure == 'metadata_missing':
            devices.return_value[0].pop('miotDID')
        if failure == 'miot_code':
            action.return_value = -704042011
        if failure == 'miot_bad_code':
            action.return_value = False  # bool must not be mistaken for integer zero.
        if failure == 'miot_exception':
            action.side_effect = Exception('Error https://api.io.mi.com/app/miotspec/action: '
                                          "{'code': -3, 'message': '" + PRIVATE + "'}")
        if failure == 'miot_timeout':
            action.side_effect = TimeoutError(PRIVATE)
        monkeypatch.setattr('miservice.MiIOService', lambda account: SimpleNamespace(miot_action=action))
        speaker = MiSpeaker(None, SimpleNamespace(player_stop=stop, device_list=devices,
                                                  text_to_speech=mina_tts),
                            {'tts': {'method': 'mina' if failure == 'mina_false' else 'miot',
                                     'stop_music_before_speech': ['qa']}})
        with pytest.raises(SpeechFailure) as caught:
            await speaker.tts('qa', PRIVATE, raise_on_error=True)
        error = caught.value
        assert (error.stage, error.kind) == (expected_stage, expected_kind)
        assert error.chunk_index == error.total_chunks == 1
        assert error.submitted_chunks == 0
        assert PRIVATE not in str(error) + json.dumps(error.metadata()) + caplog.text
        assert stop.await_count == 1
        assert devices.await_count <= 1 and action.await_count <= 1
        if expected_stage in ('stop_music', 'device_list', 'device_prepare'):
            action.assert_not_awaited()
        if failure == 'miot_code':
            assert error.code == -704042011
        if failure == 'miot_exception':
            assert error.code == -3
    asyncio.run(run())


@pytest.mark.parametrize('payload,code', [
    ('{"code":401,"data":{"secret":"' + PRIVATE + '"},"ok":false}', 401),
    ("{'code': -3, 'message': '" + PRIVATE + "'}", -3),
    ('{"code":"' + PRIVATE + '"}', None),
    ('{"code":true}', None),
    ('{"message":"' + PRIVATE + '","data":{"code":123}}', None),
    ('not-json-' + PRIVATE, None),
])
def test_exception_envelope_extracts_only_top_level_integer(payload, code):
    error = SpeechFailure.from_exception('stop_music', Exception('Error https://api2.mina.mi.com/test: ' + payload))
    assert error.code == code
    assert PRIVATE not in json.dumps(error.metadata()) + error.message
    # Text with a different origin cannot impersonate the library envelope.
    assert SpeechFailure.from_exception('stop_music', Exception('Error https://untrusted.test/test: ' + payload)).code is None


def test_later_chunk_failure_keeps_usage_history_and_submitted_count(store, monkeypatch, caplog):
    async def run():
        uid = store.create_user('alice', PASSWORD)
        bound(store, uid)
        cfg = deepcopy(CFG)
        cfg['tts']['max_chunk_chars'] = 10
        runtime = Runtime(store, cfg)
        answer = '第一段用于模拟播报。第二段用于模拟失败。第三段不应再次发出。'
        model = AsyncMock(return_value=response(answer))
        monkeypatch.setattr(llm_router, 'acompletion', model)
        action = AsyncMock(side_effect=[0, -3])
        monkeypatch.setattr('miservice.MiIOService', lambda account: SimpleNamespace(miot_action=action))
        speaker = MiSpeaker(None, None, {'tts': {'method': 'miot', 'max_chunk_chars': 10}})
        speaker._miot_devices = {'speaker-a': {'hardware': 'L05B', 'miotDID': 'fake'}}
        # Only bypass the timing estimate, not the actual submission path.
        monkeypatch.setattr('mi_conversation.asyncio.sleep', AsyncMock())
        with caplog.at_level(logging.WARNING), pytest.raises(SpeechFailure):
            await runtime.handle(uid, 'speaker-a', '模拟问题', 'speaker', 'qa-later-chunk', speaker,
                                 native_answer='这个问题把我难住了，我还需要学习。')
        row = store.summary(uid)['requests'][0]
        assert row['status'] == 'completed' and row['speech_status'] == 'failed'
        assert row['total_tokens'] == 20 and model.await_count == 1 and action.await_count == 2
        assert row['speech_error']['stage'] == 'miot_submit' and row['speech_error']['code'] == -3
        assert row['speech_error']['chunk_index'] == 2 and row['speech_error']['submitted_chunks'] == 1
        assert '不代表已播放完毕' in row['speech_error']['message']
        assert not runtime.sessions(uid).get('speaker-a').to_llm_messages()
        assert len(store.history(uid)) == 1
        assert 'qa-later-chunk' not in caplog.text  # Only internal request ID correlates logs.
        assert row['id'] in caplog.text and answer not in caplog.text
        store.delete_history(uid)
        assert store.summary(uid)['requests'][0]['speech_error'] == row['speech_error']
    asyncio.run(run())


def test_legacy_bool_contract_and_parallel_diagnoses_are_isolated(monkeypatch, caplog):
    async def run():
        async def submit(did, text):
            if did == 'bad':
                raise TimeoutError(PRIVATE)
            return True
        speaker = MiSpeaker(None, SimpleNamespace(text_to_speech=submit), {})
        results = await asyncio.gather(speaker.tts('bad', PRIVATE, raise_on_error=True),
                                       speaker.tts('good', '模拟成功', raise_on_error=True), return_exceptions=True)
        assert isinstance(results[0], SpeechFailure) and results[0].kind == 'timeout'
        assert results[1] is True
        assert await speaker.tts('bad', PRIVATE) is False
        assert PRIVATE not in caplog.text and 'mina_submit' in caplog.text
    asyncio.run(run())


def test_cancellation_is_never_reported_as_speech_failure(monkeypatch, caplog):
    async def run():
        speaker = MiSpeaker(None, SimpleNamespace(text_to_speech=AsyncMock(side_effect=asyncio.CancelledError)), {})
        with pytest.raises(asyncio.CancelledError):
            await speaker.tts('qa', PRIVATE, raise_on_error=True)
        assert not caplog.text
    asyncio.run(run())


def test_diagnostic_migration_is_idempotent_and_old_failure_stays_unknown(store):
    uid = store.create_user('alice', PASSWORD)
    rid = store.reserve(uid, 'qa', 'speaker', 'qa-old')
    store.finish(rid, {}, status='completed', speech_status='failed')
    # Model a database created before this column existed.
    with store.db() as db:
        db.execute('ALTER TABLE requests DROP COLUMN speech_error')
    for _ in range(2):
        upgraded = Store(store.root, store.vault)
        row = upgraded.summary(uid)['requests'][0]
        assert row['speech_status'] == 'failed' and row['speech_error'] is None


def test_diagnostic_is_metadata_only_with_owner_and_admin_permissions(web):
    client, app = web
    login(client)
    admin_uid = client.get('/api/me').json()['user_id']
    rid = app.state.store.reserve(admin_uid, 'qa', 'speaker', 'qa-private')
    app.state.store.finish(rid, {'total_tokens': 20}, status='completed', speech_status='failed')
    app.state.store.update_request(rid, speech_error={
        **SpeechFailure('stop_music', 'request_error', code=-3).metadata(), 'secret': PRIVATE, 'message': PRIVATE})
    row = client.get('/api/usage').json()['requests'][0]
    assert row['speech_error']['code'] == -3 and PRIVATE not in json.dumps(row)
    assert client.get('/api/admin').json()['usage']['requests'][0]['speech_error'] == row['speech_error']
    invite_user(client)
    assert not client.get('/api/usage').json()['requests']
    assert client.get('/api/admin').status_code == 403
    client.cookies.clear()
    assert client.get('/api/usage').status_code == 401


@pytest.mark.parametrize('data', [None, 'invalid-json', {'stage': [], 'kind': []},
                                  {'stage': PRIVATE, 'kind': PRIVATE}])
def test_malformed_stored_data_cannot_become_display_text(data):
    assert public_speech_diagnostic(data) is None
