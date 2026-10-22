"""Usage ranking uses synthetic metadata, without models, mail, speech or transcripts."""
from datetime import date, datetime, timedelta
from itertools import count
import json
import time

import pytest

from platform_app.runtime import Runtime
from platform_app.store import CST, Rejected
from platform_app.usage_ranking import period_bounds, ranking
import test_platform
from test_platform import CFG, PASSWORD, invite_user, login

NOW = datetime(2026, 10, 22, 12, tzinfo=CST)
IDS = count()
store = test_platform.store
web = test_platform.web


def request(store, uid, *, at=NOW, total=100, prompt=60, completion=40,
            status='completed', started=1, source='speaker'):
    rid = 'synthetic-' + str(next(IDS))
    with store.db() as db:
        db.execute('INSERT INTO requests(id,user_id,source,request_id,created,day,status,started,'
                   'prompt_tokens,completion_tokens,total_tokens) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (rid, uid, source, rid, at.timestamp(), at.date().isoformat(), status, started,
                    prompt, completion, total))
    return rid


def attempt(store, rid, number, usage, status='completed'):
    with store.db() as db:
        db.execute('INSERT INTO model_attempts(request_id,attempt,service_id,name,provider,model,status,usage,created) '
                   'VALUES(?,?,?,?,?,?,?,?,?)', (rid, number, 'synthetic-service', 'private-service-name',
                                                 'deepseek', 'synthetic-model', status,
                                                 json.dumps(usage) if usage is not None else None, NOW.timestamp()))


@pytest.mark.parametrize('period,anchor,start,end', [
    ('day', date(2026, 10, 22), date(2026, 10, 22), date(2026, 10, 23)),
    ('week', date(2026, 10, 22), date(2026, 10, 19), date(2026, 10, 26)),
    ('week', date(2026, 10, 19), date(2026, 10, 19), date(2026, 10, 26)),
    ('week', date(2025, 12, 31), date(2025, 12, 29), date(2026, 1, 5)),
    ('month', date(2024, 2, 14), date(2024, 2, 1), date(2024, 3, 1)),
    ('month', date(2025, 12, 31), date(2025, 12, 1), date(2026, 1, 1)),
    ('day', date(2024, 2, 29), date(2024, 2, 29), date(2024, 3, 1)),
    ('last_3_months', date(2026, 10, 22), date(2026, 7, 23), date(2026, 10, 23)),
    ('last_6_months', date(2026, 10, 22), date(2026, 4, 23), date(2026, 10, 23)),
    ('last_year', date(2026, 10, 22), date(2025, 10, 23), date(2026, 10, 23)),
    ('last_3_months', date(2026, 3, 31), date(2026, 1, 1), date(2026, 4, 1)),
    ('last_6_months', date(2024, 3, 30), date(2023, 9, 30), date(2024, 3, 31)),
    ('last_year', date(2025, 2, 28), date(2024, 3, 1), date(2025, 3, 1)),
    ('last_year', date(2024, 2, 28), date(2023, 2, 28), date(2024, 2, 29)),
])
def test_calendar_periods(period, anchor, start, end):
    assert period_bounds(period, anchor, NOW) == (start, end)


def test_exact_beijing_boundaries_and_current_period_excludes_future(store):
    uid = store.create_user('alice', PASSWORD)
    start = NOW.replace(hour=0)
    request(store, uid, at=start - timedelta(microseconds=1), total=1)
    request(store, uid, at=start, total=2)
    request(store, uid, at=NOW - timedelta(seconds=1), total=4)
    request(store, uid, at=NOW + timedelta(seconds=1), total=8)
    request(store, uid, at=start + timedelta(days=1), total=16)
    result = ranking(store, now=NOW)
    assert result['summary']['total_tokens'] == 6
    assert result['summary']['requests'] == 2
    result = ranking(store, reference_date=date(2026, 10, 21), now=NOW)
    assert result['summary']['total_tokens'] == 1
    assert result['timezone'] == 'Asia/Shanghai'
    assert result['end_date'] == '2026-10-21'
    with pytest.raises(Rejected, match='未来'):
        ranking(store, reference_date=date(2026, 10, 23), now=NOW)


@pytest.mark.parametrize('period,first_day', [
    ('last_3_months', date(2026, 7, 23)), ('last_6_months', date(2026, 4, 23)),
    ('last_year', date(2025, 10, 23)),
])
def test_rolling_ranges_include_start_and_anchor_not_future(store, period, first_day):
    uid = store.create_user('alice', PASSWORD)
    start = datetime.combine(first_day, datetime.min.time(), CST)
    request(store, uid, at=start - timedelta(microseconds=1), total=1)
    request(store, uid, at=start, total=2)
    request(store, uid, at=NOW - timedelta(seconds=1), total=4)
    request(store, uid, at=NOW + timedelta(seconds=1), total=8)
    result = ranking(store, period=period, now=NOW)
    assert result['summary']['total_tokens'] == 6
    assert result['summary']['requests'] == 2
    assert result['start_date'] == first_day.isoformat()
    assert result['end_date'] == NOW.date().isoformat()
    with pytest.raises(Rejected, match='过早'):
        ranking(store, period=period, reference_date=date(1, 1, 1), now=NOW)


def test_historical_rolling_range_includes_whole_end_day(store):
    uid = store.create_user('alice', PASSWORD)
    request(store, uid, at=datetime(2026, 9, 30, 23, 59, 59, tzinfo=CST), total=10)
    request(store, uid, at=datetime(2026, 10, 1, tzinfo=CST), total=20)
    result = ranking(store, period='last_3_months', reference_date=date(2026, 9, 30), now=NOW)
    assert result['start_date'] == '2026-07-01'
    assert result['end_date'] == '2026-09-30'
    assert result['summary']['total_tokens'] == 10


def test_all_accounts_sources_and_statuses_and_history_independence(store, monkeypatch):
    alice = store.create_user('alice', PASSWORD)
    bob = store.create_user('bob', PASSWORD)
    admin = store.create_user('admin', PASSWORD, role='admin')
    store.create_user('zero', PASSWORD)
    with store.db() as db:
        db.execute('UPDATE users SET active=0 WHERE id=?', (bob,))
    for uid, status, source in [(alice, 'failed', 'speaker'), (alice, 'interrupted', 'speaker'),
                                (bob, 'completed', 'debug'), (admin, 'completed', 'model_test')]:
        request(store, uid, at=NOW - timedelta(seconds=1), status=status, source=source)
    request(store, alice, total=None, status='cancelled', started=0)
    request(store, alice, total=None, status='reserved', started=0)
    # No history read or credential decryption is required by the board.
    store.save_history(alice, None, 'synthetic-history', 'private question', 'private answer')
    before = ranking(store, now=NOW)
    store.delete_history(alice)
    monkeypatch.setattr(store.vault, 'open', lambda *args: pytest.fail('Must not decrypt private data'))
    after = ranking(store, now=NOW)
    assert before == after
    assert after['summary'] == {'requests': 4, 'total_tokens': 400, 'unknown_usage': 0, 'pending_usage': 0}
    rows = {r['username']: r for r in after['rows']}
    assert rows['alice']['total_tokens'] == 200
    assert rows['bob']['active'] is False
    assert rows['admin']['role'] == 'admin'
    assert rows['zero']['requests'] == rows['zero']['total_tokens'] == 0
    assert 'private' not in json.dumps(after)
    assert 'password' not in json.dumps(after)


def test_failover_known_partial_unknown_pending_and_no_double_count(store):
    Runtime(store, CFG)  # Creates the existing attempts schema, without starting workers.
    uid = store.create_user('alice', PASSWORD)
    when = NOW - timedelta(seconds=1)
    rid = request(store, uid, at=when, total=None, prompt=6000, completion=4000)
    attempt(store, rid, 1, None, 'failed')
    usage = {'prompt_tokens': 60, 'completion_tokens': 40, 'total_tokens': 100,
             'prompt_cache_hit_tokens': 50, 'completion_tokens_details': {'reasoning_tokens': 10}}
    attempt(store, rid, 2, usage)
    rid = request(store, uid, at=when, total=9999)
    attempt(store, rid, 1, {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}, 'failed')
    attempt(store, rid, 2, {'prompt_tokens': 6, 'completion_tokens': 4, 'total_tokens': 10})
    rid = request(store, uid, at=when, total=None, status='reserved')
    attempt(store, rid, 1, None, 'failed')
    attempt(store, rid, 2, None, 'started')
    request(store, uid, at=when, total=None, prompt=None, completion=None, status='interrupted')
    request(store, uid, at=when, total=None, prompt=None, completion=None, status='reserved')
    request(store, uid, at=when, total=0, prompt=0, completion=0)
    result = ranking(store, now=NOW)
    assert result['summary'] == {'requests': 6, 'total_tokens': 115, 'unknown_usage': 3, 'pending_usage': 2}
    assert result['rows'][0]['prompt_tokens'] == 69
    assert result['rows'][0]['completion_tokens'] == 46
    assert 'private-service-name' not in json.dumps(result)


def test_missing_or_malformed_usage_is_not_silently_zero(store):
    Runtime(store, CFG)
    uid = store.create_user('alice', PASSWORD)
    for total in (True, -10, '100', None, 0):
        rid = request(store, uid, at=NOW - timedelta(seconds=1), total=999)
        attempt(store, rid, 1, {'total_tokens': total})
    rid = request(store, uid, at=NOW - timedelta(seconds=1), total=999)
    attempt(store, rid, 1, None)
    with store.db() as db:
        db.execute("UPDATE model_attempts SET usage='not json' WHERE request_id=?", (rid,))
    result = ranking(store, now=NOW)
    assert result['summary']['unknown_usage'] == 5
    assert result['summary']['total_tokens'] == 0


def test_attempt_finishing_next_day_stays_in_request_period(store):
    Runtime(store, CFG)
    uid = store.create_user('alice', PASSWORD)
    rid = request(store, uid, at=NOW.replace(day=21, hour=23, minute=59), total=None)
    attempt(store, rid, 1, {'prompt_tokens': 6, 'completion_tokens': 4, 'total_tokens': 10})
    assert ranking(store, now=NOW)['summary']['total_tokens'] == 0
    yesterday = ranking(store, reference_date=date(2026, 10, 21), now=NOW)
    assert yesterday['summary']['requests'] == 1
    assert yesterday['summary']['total_tokens'] == 10


def test_search_stable_ranks_pagination_and_period_summary(store):
    with store.db() as db:
        db.executemany('INSERT INTO users(id,username,password,created) VALUES(?,?,?,?)',
                       [(f'id-{i}', f'user{i:02}', 'synthetic-hash', 0) for i in range(52)])
    request(store, 'id-51', at=NOW - timedelta(seconds=1), total=10)
    request(store, 'id-50', at=NOW - timedelta(seconds=1), total=None, prompt=None, completion=None)
    first = ranking(store, now=NOW)
    assert first['users_total'] == 52 and first['pages'] == 3
    assert first['rows'][0]['username'] == 'user51'
    assert first['rows'][1]['username'] == 'user00'
    second = ranking(store, page=2, now=NOW)
    assert second['rows'][0]['rank'] == 26
    assert not {r['user_id'] for r in first['rows']} & {r['user_id'] for r in second['rows']}
    assert ranking(store, page=999, now=NOW)['page'] == 3
    match = ranking(store, search='USER50', now=NOW)
    assert match['users_total'] == 1 and match['rows'][0]['rank'] == 52
    assert match['summary']['total_tokens'] == 10  # Summary ignores list-only filters.
    assert ranking(store, only_unknown=True, now=NOW)['rows'][0]['username'] == 'user50'
    assert ranking(store, search='%', now=NOW)['users_total'] == 0  # Literal search, not LIKE wildcards.
    empty = ranking(store, search="' OR 1=1 --", now=NOW)
    assert empty['rows'] == [] and empty['page'] == 1 and empty['pages'] == 1


def test_empty_database(store):
    result = ranking(store, now=NOW)
    assert result['rows'] == [] and result['users_total'] == 0
    assert result['summary'] == {'requests': 0, 'total_tokens': 0, 'unknown_usage': 0, 'pending_usage': 0}


def test_admin_endpoint_validation_permissions_and_safe_metadata(web):
    client, app = web
    assert client.get('/api/admin/usage-ranking').status_code == 401
    login(client)
    for params in ({'period': 'year'}, {'reference_date': 'invalid'}, {'page': 0},
                   {'search': 'a' * 33}, {'only_unknown': 'invalid'}):
        assert client.get('/api/admin/usage-ranking', params=params).status_code == 422
    assert client.get('/api/admin/usage-ranking', params={'reference_date': '2099-01-01'}).status_code == 400
    result = client.get('/api/admin/usage-ranking')
    assert result.status_code == 200
    assert result.json()['rows'][0]['username'] == 'admin'
    fields = {'rank', 'user_id', 'username', 'role', 'active', 'requests', 'prompt_tokens',
              'completion_tokens', 'total_tokens', 'unknown_usage', 'pending_usage'}
    assert set(result.json()['rows'][0]) == fields
    for period in ('last_3_months', 'last_6_months', 'last_year'):
        response = client.get('/api/admin/usage-ranking', params={'period': period})
        assert response.status_code == 200
        assert response.json()['period'] == period
    invite_user(client)
    assert client.get('/api/admin/usage-ranking').status_code == 403


def test_thousand_users_aggregate_query_and_time_index(store):
    Runtime(store, CFG)
    with store.db() as db:
        db.executemany('INSERT INTO users(id,username,password,created) VALUES(?,?,?,?)',
                       [(f'u{i}', f'user{i:04}', 'synthetic-hash', 0) for i in range(1000)])
        db.executemany('INSERT INTO requests(id,user_id,source,request_id,created,day,status,started,'
                       'prompt_tokens,completion_tokens,total_tokens) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       [(f'r{i}', f'u{i % 1000}', 'speaker', f'q{i}', NOW.timestamp() - 1, '2026-10-22',
                         'completed', 1, 60, 40, 100) for i in range(20000)])
        db.executemany('INSERT INTO model_attempts(request_id,attempt,service_id,name,provider,model,status,usage,created) '
                       'VALUES(?,1,?,?,?,?,?,?,?)',
                       [(f'r{i}', 'test', 'test', 'deepseek', 'test', 'completed',
                         '{"prompt_tokens":60,"completion_tokens":40,"total_tokens":100}', NOW.timestamp() - 1)
                        for i in range(10000)])
        plan = db.execute('EXPLAIN QUERY PLAN SELECT * FROM requests WHERE started=1 AND created>=? AND created<?',
                          (NOW.timestamp() - 86400, NOW.timestamp())).fetchall()
    assert 'request_usage_time' in str([tuple(r) for r in plan])
    started = time.monotonic()
    result = ranking(store, now=NOW)
    elapsed = time.monotonic() - started
    assert result['summary']['total_tokens'] == 2000000
    assert result['summary']['requests'] == 20000
    assert result['users_total'] == 1000 and len(result['rows']) == 25
    assert elapsed < 5  # Bounded aggregate smoke check, not a production capacity promise.
