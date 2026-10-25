"""Admin timing controls and immutable per-answer snapshots; no real speech/model."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

import llm_router
from mi_conversation import MiSpeaker
from platform_app.runtime import Runtime
from platform_app.speech_settings import DEFAULT_SPEECH
from platform_app.store import Store
from test_platform import (CFG, PASSWORD, bound, invite_user, login, response,
                           store as store_fixture, web as web_fixture)

store = store_fixture
web = web_fixture
NEW = {'max_chunk_chars': 10, 'chars_per_second': 5.0, 'safety_margin_seconds': 2.0}


def test_admin_access_persistence_and_independence(web):
    client, app = web
    assert client.get('/api/admin/speech-settings').status_code == 401
    assert client.put('/api/admin/speech-settings', json=NEW).status_code == 401
    login(client)
    assert client.get('/api/admin/speech-settings').json() == DEFAULT_SPEECH
    before = app.state.store.defaults()
    assert client.put('/api/admin/speech-settings', json=NEW).json() == NEW
    assert client.get('/api/admin').json()['speech_settings'] == NEW
    assert Store(app.state.store.root, app.state.store.vault).speech_settings() == NEW
    assert app.state.store.defaults() == before
    invite_user(client)
    assert client.get('/api/admin/speech-settings').status_code == 403
    assert client.put('/api/admin/speech-settings', json=NEW).status_code == 403
    assert 'speech_settings' not in client.get('/api/me').json()['policy']


@pytest.mark.parametrize('key,value', [
    ('max_chunk_chars', 9), ('max_chunk_chars', 101), ('max_chunk_chars', 10.5),
    ('max_chunk_chars', True), ('chars_per_second', .9), ('chars_per_second', 10.1),
    ('chars_per_second', True), ('chars_per_second', '3.5'),
    ('safety_margin_seconds', -1), ('safety_margin_seconds', 11),
    ('safety_margin_seconds', 'NaN'), ('safety_margin_seconds', True),
])
def test_invalid_settings_do_not_change_saved_values(web, key, value):
    client, app = web
    login(client)
    assert client.put('/api/admin/speech-settings', json={**NEW, key: value}).status_code == 422
    assert app.state.store.speech_settings() == DEFAULT_SPEECH


def test_zero_margin_and_boundary_values_allowed(web):
    client, app = web
    login(client)
    values = {'max_chunk_chars': 100, 'chars_per_second': 10, 'safety_margin_seconds': 0}
    assert client.put('/api/admin/speech-settings', json=values).json() == values
    assert client.put('/api/admin/speech-settings', json={**NEW, 'method': 'mina'}).status_code == 422


def test_existing_config_and_bad_stored_values(store):
    base = {'max_chunk_chars': 80, 'chars_per_second': 4.0, 'method': 'miot'}
    assert store.speech_settings(base) == {**DEFAULT_SPEECH, 'max_chunk_chars': 80, 'chars_per_second': 4}
    store.update_settings({'speech_settings': {'max_chunk_chars': False, 'chars_per_second': -1,
                                               'safety_margin_seconds': 'invalid'}})
    assert store.speech_settings(base) == {**DEFAULT_SPEECH, 'max_chunk_chars': 80, 'chars_per_second': 4}


def test_formula_snapshotted_through_last_chunk_without_mutating_shared_speaker(monkeypatch):
    async def run():
        settings = dict(NEW)
        speaker = MiSpeaker(None, None, {'tts': {'max_chunk_chars': 60, 'chars_per_second': 3.5}})
        original = deepcopy(speaker.cfg)
        chunks, delays = [], []

        async def submit(did, chunk, **kwargs):
            chunks.append(chunk)
            return True

        async def wait(delay):
            delays.append(delay)
            settings.update(max_chunk_chars=100, chars_per_second=1, safety_margin_seconds=10)

        monkeypatch.setattr(speaker, '_tts_once', submit)
        monkeypatch.setattr('mi_conversation.asyncio.sleep', wait)
        assert await speaker.tts('synthetic', '甲' * 25, is_current=lambda: True, speech_settings=settings)
        assert [len(c) for c in chunks] == [10, 10, 5]
        assert delays == [4, 4, 3]  # Includes the final interruptible estimate.
        assert speaker.cfg == original
    asyncio.run(run())


def test_saved_settings_only_apply_to_next_answer_and_cached_speaker(store, monkeypatch):
    async def run():
        uid = store.create_user('speaker-settings-user', PASSWORD)
        bound(store, uid)
        runtime = Runtime(store, deepcopy(CFG))
        speaker = MiSpeaker(None, None, deepcopy(CFG))
        chunks = []

        async def submit(did, chunk, **kwargs):
            chunks.append(chunk)
            return True

        async def model(**kwargs):
            # An administrator saves while this answer is already generating.
            store.update_settings({'speech_settings': NEW})
            return response('甲' * 25)

        monkeypatch.setattr(llm_router, 'acompletion', model)
        monkeypatch.setattr(speaker, '_tts_once', submit)
        monkeypatch.setattr('mi_conversation.asyncio.sleep', AsyncMock())
        args = (uid, 'speaker-a', '模拟问题', 'speaker')
        await runtime.handle(*args, 'timing-one', speaker, native_answer='这个问题把我难住了，我还需要学习。')
        assert len(chunks) == 1  # Old 60-character rule, despite the save during generation.
        chunks.clear()
        await runtime.handle(*args, 'timing-two', speaker, native_answer='这个问题把我难住了，我还需要学习。')
        assert len(chunks) > 1 and all(len(c) <= 10 for c in chunks)
        assert 'safety_margin_seconds' not in speaker.cfg['tts']
        assert runtime.base_cfg == CFG
    asyncio.run(run())
