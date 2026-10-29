"""Synthetic feedback checks: ownership, privacy, retries and administrative replies."""
from concurrent.futures import ThreadPoolExecutor
import secrets
import sqlite3

import pytest

import test_platform
from platform_app.admin import backup
from platform_app.feedback import Feedback, Handling, Submission
from platform_app.store import Rejected, Store
from test_platform import PASSWORD, invite_user, login

store = test_platform.store
web = test_platform.web


def submission(**values):
    return Submission(**{'submission_id': secrets.token_hex(16), 'category': 'problem',
                         'content': 'Synthetic private support text', **values})


def test_public_help_and_authenticated_routes(web):
    client, app = web
    for path in ('/help', '/static/support.js'):
        assert client.get(path).status_code == 200
    html = client.get('/help').text
    assert html.count('<details ') == 12
    assert '非小米官方产品' in html and '不会自动附带聊天正文' in html
    assert client.get('/api/feedback').status_code == 401
    assert client.post('/api/feedback', json=submission().model_dump()).status_code == 401
    assert client.get('/api/admin/feedback').status_code == 401
    assert client.put('/api/admin/feedback/missing', json={}).status_code == 401


def test_ownership_permissions_csrf_and_no_chat_attachment(web):
    client, app = web
    admin = login(client)
    alice, _ = invite_user(client)
    app.state.store.save_history(alice['user_id'], 'synthetic-device', 'synthetic-history',
                                 'Never attach this question', 'Never attach this answer')
    data = submission().model_dump()
    missing_csrf = client.post('/api/feedback', json=data, headers={'X-CSRF-Token': 'bad'})
    assert missing_csrf.status_code == 403
    assert client.post('/api/feedback', json=data, headers={'Origin': 'https://other.example'}).status_code == 403
    response = client.post('/api/feedback', json=data)
    assert response.status_code == 200
    fid = response.json()['id']
    assert response.json()['device'] is None
    assert 'Never attach' not in response.text
    assert 'user_id' not in response.json() and 'username' not in response.json()
    assert client.get('/api/admin/feedback').status_code == 403
    assert client.put('/api/admin/feedback/' + fid, json={'version': 1, 'status': 'closed', 'reply': ''}).status_code == 403
    login(client)
    inbox = client.get('/api/admin/feedback').json()
    assert inbox['items'][0]['username'] == 'alice'
    assert client.get('/api/feedback').json()['items'] == []
    reply = '<script>synthetic reply</script>'
    response = client.put('/api/admin/feedback/' + fid, json={'version': 1, 'status': 'replied', 'reply': reply})
    assert response.status_code == 200
    assert client.put('/api/admin/feedback/' + fid, json={'version': 1, 'status': 'closed', 'reply': ''}).status_code == 409
    assert client.put('/api/admin/feedback/' + fid, json={'version': 2, 'status': 'replied', 'reply': '  '}).status_code == 400
    login(client, 'alice')
    mine = client.get('/api/feedback').json()['items']
    assert len(mine) == 1 and mine[0]['reply'] == reply and mine[0]['status'] == 'replied'
    login(client)
    bob, _ = invite_user(client, 'bob')
    assert client.get('/api/feedback').json()['items'] == []
    assert client.get('/api/feedback/' + fid).status_code == 404
    assert client.get('/api/feedback?user_id=' + alice['user_id']).json()['items'] == []
    assert bob['user_id'] != admin['user_id']


def test_only_explicitly_selected_owned_device_metadata_is_saved(store):
    feedback = Feedback(store)
    a, b = (store.create_user(name, PASSWORD) for name in ('alice', 'bob'))
    for uid, did in ((a, 'a'), (b, 'b')):
        store.sync_devices(uid, [{'id': did, 'name': 'Private device name', 'hardware': 'OH2', 'supported': True, 'reason': ''}])
    record = feedback.create(a, submission(device_id='a'))
    assert record['device'] == {'id': 'a', 'name': 'Private device name', 'hardware': 'OH2'}
    with pytest.raises(Rejected, match='当前设备'):
        feedback.create(b, submission(device_id='a'))
    with store.db() as db:
        db.execute('UPDATE devices SET present=0 WHERE user_id=?', (a,))
    with pytest.raises(Rejected):
        feedback.create(a, submission(device_id='a'))
    assert feedback.listing(a)['items'][0]['device']['name'] == 'Private device name'


def test_idempotency_is_atomic_and_does_not_consume_extra_slots(store):
    feedback = Feedback(store)
    uid = store.create_user('alice', PASSWORD)
    data = submission()
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda _: feedback.create(uid, data), range(4)))
    assert len({record['id'] for record in records}) == 1
    assert feedback.listing(uid)['total'] == 1
    with pytest.raises(Rejected) as caught:
        feedback.create(uid, submission(submission_id=data.submission_id, content='Changed retry'))
    assert caught.value.status == 409
    for _ in range(4):
        feedback.create(uid, submission())
    with pytest.raises(Rejected) as caught:
        feedback.create(uid, submission())
    assert caught.value.status == 429
    assert feedback.create(uid, data)['id'] == records[0]['id']
    reopened = Feedback(Store(store.root, store.vault))
    with pytest.raises(Rejected):
        reopened.create(uid, submission())
    with store.db() as db:
        db.execute('UPDATE feedback SET created=created-3601')
    assert reopened.create(uid, submission())


def test_feedback_encryption_and_configuration_backup_excludes_submissions(store, tmp_path):
    feedback = Feedback(store)
    uid = store.create_user('alice', PASSWORD)
    row = feedback.create(uid, submission())
    feedback.handle(row['id'], Handling(version=1, status='replied', reply='Private support reply marker'))
    raw = store.path.read_bytes()
    assert b'Synthetic private support text' not in raw
    assert b'Private support reply marker' not in raw
    with store.db() as db:
        cipher = db.execute('SELECT content FROM feedback').fetchone()[0]
    with pytest.raises(Exception):
        store.vault.open(cipher, 'feedback:wrong-id')
    with sqlite3.connect(backup(store, tmp_path / 'backup')) as db:
        assert db.execute('SELECT COUNT(*) FROM feedback').fetchone()[0] == 0
    assert feedback.listing(uid)['items'][0]['reply'] == 'Private support reply marker'


def test_pagination_filters_missing_and_conflicting_updates(store):
    feedback = Feedback(store)
    uid = store.create_user('alice', PASSWORD)
    for i in range(23):
        feedback.create(uid, submission(content=str(i)))
        with store.db() as db:
            db.execute('UPDATE feedback SET created=created-3601')
    first = feedback.listing(uid)
    second = feedback.listing(uid, page=2)
    assert first['total'] == 23 and first['pages'] == 2 and len(first['items']) == 20
    assert len(second['items']) == 3
    assert {r['id'] for r in first['items']}.isdisjoint(r['id'] for r in second['items'])
    fid = first['items'][0]['id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        def handle(_):
            try:
                return feedback.handle(fid, Handling(version=1, status='processing', reply=''))
            except Rejected as error:
                return error.status
        results = list(pool.map(handle, range(2)))
    assert 409 in results and {'ok': True} in results
    assert feedback.listing(status='processing')['total'] == 1
    assert feedback.listing(status='closed')['items'] == []
    assert feedback.listing(page=999)['page'] == 2
    with pytest.raises(Rejected) as caught:
        feedback.handle('missing', Handling(version=1, status='closed', reply=''))
    assert caught.value.status == 404


@pytest.mark.parametrize('change', [
    {'content': '   '}, {'content': 'x' * 2001}, {'category': 'invalid'},
    {'submission_id': 'invalid'}, {'user_id': 'other'}, {'chat_history': 'do not attach'},
])
def test_submission_validation(web, change):
    client, _ = web
    login(client)
    assert client.post('/api/feedback', json={**submission().model_dump(), **change}).status_code == 422


def test_admin_validation_filters_and_account_deactivation(web):
    client, app = web
    admin = login(client)
    fid = client.post('/api/feedback', json=submission().model_dump()).json()['id']
    assert client.get('/api/feedback?page=0').status_code == 422
    assert client.get('/api/admin/feedback?status=invalid').status_code == 422
    assert client.put('/api/admin/feedback/' + fid, json={'version': True, 'status': 'closed', 'reply': ''}).status_code == 422
    assert client.put('/api/admin/feedback/' + fid, json={'version': 1, 'status': 'closed', 'reply': 'x' * 2001}).status_code == 422
    assert client.put('/api/admin/feedback/' + fid, json={'version': 1, 'status': 'closed', 'reply': '', 'content': 'tamper'}).status_code == 422
    with app.state.store.db() as db:
        db.execute('UPDATE users SET active=0 WHERE id=?', (admin['user_id'],))
    assert client.get('/api/feedback').status_code == 403
    assert client.post('/api/feedback', json=submission().model_dump()).status_code == 403


def test_help_settings_expose_only_live_defaults_and_preserve_personal_overrides(web):
    client, app = web
    keys = ('ttl_seconds', 'max_turns', 'retention_days')
    expected = {key: app.state.store.defaults()[key] for key in keys}
    assert client.get('/api/help/settings').json() == expected
    app.state.store.update_settings({'ttl_seconds': 95, 'max_turns': 7, 'retention_days': 21,
                                     'private_help_marker': 'must-not-appear-in-public-settings'})
    expected = {'ttl_seconds': 95, 'max_turns': 7, 'retention_days': 21}
    response = client.get('/api/help/settings')
    assert response.status_code == 200 and response.json() == expected
    admin = login(client)
    test_platform.policy(app.state.store, admin['user_id'], ttl_seconds=120, max_turns=2)
    me = client.get('/api/me').json()
    assert me['policy']['ttl_seconds'] == 120 and me['policy']['max_turns'] == 2
    assert me['retention_days'] == 21
    assert client.get('/api/help/settings').json() == expected
    updated = client.put('/api/admin/defaults',
                         json={'ttl_seconds': 180, 'max_turns': 9, 'retention_days': 30})
    assert updated.status_code == 200
    assert client.get('/api/help/settings').json() == {'ttl_seconds': 180, 'max_turns': 9, 'retention_days': 30}
    assert client.get('/api/me').json()['policy']['max_turns'] == 2
