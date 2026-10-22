"""Native-first routing, user/admin boundaries, and lossless speech settings."""
import asyncio
from copy import deepcopy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import llm_router
from main import RouterPipeline
from mi_conversation import MiSpeaker
from platform_app.routing import failure_match
from platform_app.runtime import Runtime
from platform_app.security import Vault
from platform_app.store import Rejected, Store
from test_platform import web, store, CFG, PASSWORD, login, invite_user, bound, response  # noqa: F401

FAILURE = '这可把我难住了，看来要更努力学习了'


@pytest.mark.parametrize('answer,expected', [
    (FAILURE, True), ('这可把我难住了! 看来要更努力学习了。', True),
    ('这个我暂时还回答不上来', True), ('对不起，我还在学习中。', True),
    ('今天晴，气温二十度。', False), ('我暂时为你暂停音乐。', False),
    ('我不知道你家的具体位置，请打开定位。', False), ('这个设备不支持蓝牙。', False),
    ('我还在学习，但刚才网络连接失败。', False), ('没听清，请再说一遍。', False),
    ('', False), ('介绍完所有正常内容。' * 20 + '我还在学习', False),
])
def test_conservative_seed_rules(store, answer, expected):
    assert failure_match(answer, store.fallback_rules())['takeover'] is expected


def test_modes_are_mutually_exclusive_and_settings_are_per_user(store):
    a, b = (store.create_user(name, PASSWORD) for name in ('alice', 'bob'))
    runtime = Runtime(store, deepcopy(CFG))
    assert store.dialogue(a)['routing_mode'] == 'fallback'
    assert store.dialogue(a)['answer_prefix'] == 'deepseek回答'
    assert runtime.route(a, '为什么会这样', FAILURE)
    assert not runtime.route(a, '深思为什么会这样', '这是小爱的正常回答')
    store.set_dialogue(a, {'routing_mode': 'keyword', 'wake_words': ['问问AI']})
    assert not runtime.route(a, '为什么会这样', FAILURE)
    assert runtime.route(a, '问问AI为什么会这样', None)
    assert store.dialogue(b)['routing_mode'] == 'fallback'


def test_stable_answers_late_arrival_tied_timestamps_and_dedup(store):
    uid = store.create_user('alice', PASSWORD)
    runtime = Runtime(store, deepcopy(CFG))
    pending, seen = {}, {}
    now = time.time()
    item = {'request_id': 'one', 'ts': now, 'text': '为什么', 'native_answer': ''}
    assert runtime.ready_records(uid, [item], pending, seen, now-1, now) == []
    item = {**item, 'native_answer': FAILURE}
    assert runtime.ready_records(uid, [item], pending, seen, now-1, now+3) == []
    assert runtime.ready_records(uid, [item], pending, seen, now-1, now+6) == [item]
    assert runtime.ready_records(uid, [item], pending, seen, now-1, now+9) == []
    other = {**item, 'request_id': 'two'}
    assert runtime.ready_records(uid, [other], pending, seen, now-1, now+9) == []
    assert runtime.ready_records(uid, [other], pending, seen, now-1, now+12) == [other]


def test_unknown_empty_stale_future_and_pre_switch_records_do_not_dispatch(store):
    uid = store.create_user('alice', PASSWORD)
    runtime = Runtime(store, deepcopy(CFG))
    pending, seen = {}, {}
    now = time.time()
    def item(rid, stamp, answer):
        return {'request_id': rid, 'ts': stamp, 'text': '深思为什么', 'native_answer': answer}
    records = [item('old', now-90, FAILURE), item('future', now+60, FAILURE), item('empty', now, '')]
    assert runtime.ready_records(uid, records, pending, seen, now-100, now) == []
    assert runtime.ready_records(uid, records, pending, seen, now-100, now+16) == []
    assert 'empty' in seen
    store.set_dialogue(uid, {'routing_mode': 'keyword'})
    assert runtime.ready_records(uid, [item('before-switch', now-1, FAILURE)], {}, {}, now-100, now) == []


def test_user_cannot_edit_prompt_memory_or_admin_rulebook(web):
    client, app = web
    login(client)
    alice, _ = invite_user(client)
    for body in ({'system_prompt': 'Override'}, {'ttl_seconds': 300}, {'max_turns': 3}):
        assert client.put('/api/dialogue', json=body).status_code == 422
    assert client.get('/api/admin').status_code == 403
    rule = {'name': 'rule', 'match_type': 'exact', 'content': FAILURE, 'enabled': True}
    assert client.post('/api/admin/fallback-rules', json=rule).status_code == 403
    assert client.put('/api/admin/fallback-rules/seed-0', json=rule).status_code == 403
    assert client.delete('/api/admin/fallback-rules/seed-0').status_code == 403
    assert client.post('/api/admin/fallback-rules/test', json={'answer': FAILURE}).status_code == 403
    profile = client.get('/api/me').json()
    assert 'fallback_rules' not in profile and 'system_prompt' not in profile['policy']
    assert app.state.store.dialogue(alice['user_id'])['routing_mode'] == 'fallback'


def test_prefix_limit_empty_unicode_and_user_isolation(web):
    client, app = web
    admin = login(client)
    alice, _ = invite_user(client)
    for value in ('123456789012', '你好' * 6, '😀' * 12, '', '   '):
        assert client.put('/api/dialogue', json={'answer_prefix': value}).status_code == 200
        assert client.get('/api/me').json()['dialogue']['answer_prefix'] == value.strip()
    assert app.state.store.dialogue(admin['user_id'])['answer_prefix'] == 'deepseek回答'
    for value in ('a'*13, '😀'*13, 'a\nb'):
        assert client.put('/api/dialogue', json={'answer_prefix': value}).status_code == 422
    assert client.put('/api/dialogue', json={'wake_words': []}).status_code == 422
    assert client.put('/api/dialogue', json={'wake_words': [' ']}).status_code == 422
    assert client.put('/api/dialogue', json={'routing_mode': 'all'}).status_code == 422
    assert app.state.store.dialogue(alice['user_id'])['answer_prefix'] == ''


def test_admin_rule_crud_preview_disable_and_validation(web):
    client, app = web
    login(client)
    body = {'name': '测试精确规则', 'match_type': 'exact', 'content': '这是一条模拟失败回复', 'enabled': True}
    ident = client.post('/api/admin/fallback-rules', json=body).json()['id']
    preview = client.post('/api/admin/fallback-rules/test', json={'answer': body['content']}).json()
    assert preview['takeover'] and preview['matched'][0]['id'] == ident
    assert client.put('/api/admin/fallback-rules/'+ident, json={**body, 'enabled': False}).status_code == 200
    assert not client.post('/api/admin/fallback-rules/test', json={'answer': body['content']}).json()['takeover']
    assert client.put('/api/admin/fallback-rules/'+ident, json={**body, 'content': '新的模拟失败回复'}).status_code == 200
    assert client.post('/api/admin/fallback-rules/test', json={'answer': '新的模拟失败回复'}).json()['takeover']
    assert client.delete('/api/admin/fallback-rules/'+ident).status_code == 200
    assert not client.post('/api/admin/fallback-rules/test', json={'answer': '新的模拟失败回复'}).json()['takeover']
    assert client.post('/api/admin/fallback-rules', json={**body, 'match_type': 'phrases', 'content': '不会'}).status_code == 422
    assert client.put('/api/admin/defaults', json={'system_prompt': 'override'}).status_code == 422


@pytest.mark.parametrize('content,message', [
    ('2\n3', '每行短语须为2至60个字符'),
    ('不会', '仅填写一条短语时，至少4个字符'),
    ('abcd\n' * 9, '最多8条'),
])
def test_rule_validation_explains_error_without_echoing_input(web, content, message):
    client, _ = web
    login(client)
    result = client.post('/api/admin/fallback-rules', json={
        'name': '1', 'match_type': 'phrases', 'content': content})
    assert result.status_code == 422
    assert message in result.json()['error']
    assert 'input' not in result.json()


def test_rule_preview_only_matches_text_and_never_calls_runtime(web, monkeypatch):
    client, app = web
    login(client)
    handle = AsyncMock(side_effect=AssertionError('Preview cannot dispatch a conversation'))
    monkeypatch.setattr(app.state.runtime, 'handle', handle)
    result = client.post('/api/admin/fallback-rules', json={
        'name': '1', 'match_type': 'phrases', 'content': '22\n33', 'enabled': True})
    assert result.status_code == 200
    ident = result.json()['id']
    sample = client.post('/api/admin/fallback-rules/test', json={'answer': '22和33'}).json()
    assert sample['takeover'] and any(rule['id'] == ident for rule in sample['matched'])
    handle.assert_not_called()
    with app.state.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == 0


def test_admin_only_memory_rules_and_inheritance(web):
    client, app = web
    login(client)
    alice, _ = invite_user(client)
    login(client)
    assert client.put('/api/admin/defaults', json={'ttl_seconds': 900, 'max_turns': 8}).status_code == 200
    assert app.state.store.policy(alice['user_id'])['max_turns'] == 8
    path = '/api/admin/users/'+alice['user_id']
    assert client.put(path, json={'ttl_seconds': 300, 'max_turns': 2}).status_code == 200
    assert app.state.store.policy(alice['user_id'])['max_turns'] == 2
    assert client.put(path, json={'inherit_memory': True}).status_code == 200
    assert app.state.store.policy(alice['user_id'])['ttl_seconds'] == 900
    assert app.state.store.policy(alice['user_id'])['max_turns'] == 8


def test_migration_discards_old_user_memory_and_all_custom_prompts_once(store):
    uid = store.create_user('legacy', PASSWORD)
    with store.db() as db:
        db.execute("DELETE FROM migrations WHERE name='fixed-dialogue-v1'")
        db.execute('DELETE FROM fallback_rules')
        db.execute('UPDATE settings SET data=?', (json.dumps({'system_prompt': 'old-platform', 'ttl_seconds': 1200}),))
        db.execute('UPDATE users SET policy=? WHERE id=?', (json.dumps({'system_prompt': 'old-user', 'ttl_seconds': 300, 'max_turns': 3}), uid))
    migrated = Store(store.root, store.vault)
    assert migrated.policy(uid)['ttl_seconds'] == 1200 and migrated.policy(uid)['max_turns'] == 10
    assert 'system_prompt' not in migrated.policy(uid)
    with migrated.db() as db:
        assert json.loads(db.execute('SELECT policy FROM users WHERE id=?', (uid,)).fetchone()[0]) == {}
    assert len(Store(store.root, store.vault).fallback_rules()) == len(migrated.fallback_rules())


def test_native_records_preserve_answers_and_batches_without_affecting_legacy_contract():
    async def run():
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def json(self, **kwargs):
                return {'code': 0, 'data': {'records': [
                    {'query': '天气', 'time': 1000, 'requestId': 'one', 'answers': [{'tts': {'text': '晴天'}}]},
                    {'query': '复杂问题', 'time': 1000, 'requestId': 'two', 'answers': [{'tts': {'text': FAILURE}}]}]}}
        account = SimpleNamespace(token={'userId': 'qa', 'micoapi': ['fake', 'fake']},
                                  _session=SimpleNamespace(get=lambda *a, **k: Response()))
        speaker = MiSpeaker(account, None, {'mi': {'conversation_source': 'cloud'}})
        speaker._hardware = {'speaker': 'L05B'}
        records = await speaker.recent_asks('speaker')
        assert len(records) == 2 and records[1]['native_answer'] == FAILURE
        assert 'native_answer' not in await speaker.latest_ask('speaker')
    asyncio.run(run())


def test_normal_native_reply_costs_nothing_and_fallback_speaks_user_prefix(store, monkeypatch):
    async def run():
        uid = store.create_user('alice', PASSWORD)
        bound(store, uid)
        runtime = Runtime(store, deepcopy(CFG))
        model = AsyncMock(return_value=response('模拟回答'))
        monkeypatch.setattr(llm_router, 'acompletion', model)
        speaker = SimpleNamespace(tts=AsyncMock(return_value=True))
        assert await runtime.handle(uid, 'speaker-a', '天气', 'speaker', 'weather', speaker, native_answer='晴天') is None
        assert not model.called and store.summary(uid)['today'] == 0
        store.set_dialogue(uid, {'answer_prefix': ''})
        await runtime.handle(uid, 'speaker-a', '复杂问题', 'speaker', 'one', speaker, native_answer=FAILURE)
        speaker.tts.assert_awaited_with('speaker-a', '模拟回答')
        assert store.summary(uid)['today'] == 1
        # Same request must not repeat or charge twice.
        await runtime.handle(uid, 'speaker-a', '复杂问题', 'speaker', 'one', speaker, native_answer=FAILURE)
        assert model.await_count == 1
        assert model.await_args.kwargs['messages'][0]['content'] == RouterPipeline(CFG, None).default_prompt
        store.set_dialogue(uid, {'answer_prefix': '我来回答'})
        await runtime.handle(uid, 'speaker-a', '另一问题', 'speaker', 'two', speaker, native_answer=FAILURE)
        speaker.tts.assert_awaited_with('speaker-a', '我来回答模拟回答')
    asyncio.run(run())


def test_mode_change_while_model_queued_cancels_without_cost(store, monkeypatch):
    async def run():
        uid = store.create_user('alice', PASSWORD)
        bound(store, uid)
        runtime = Runtime(store, deepcopy(CFG))
        runtime.semaphore = asyncio.Semaphore(0)
        model = AsyncMock(return_value=response())
        monkeypatch.setattr(llm_router, 'acompletion', model)
        speaker = SimpleNamespace(tts=AsyncMock(return_value=True))
        task = asyncio.create_task(runtime.handle(uid, 'speaker-a', '复杂问题', 'speaker', 'queued', speaker,
                                                 native_answer=FAILURE, observed_ts=time.time()))
        await asyncio.sleep(0)
        store.set_dialogue(uid, {'routing_mode': 'keyword'})
        runtime.semaphore.release()
        with pytest.raises(Rejected):
            await task
        assert not model.called and store.summary(uid)['today'] == 0
    asyncio.run(run())
