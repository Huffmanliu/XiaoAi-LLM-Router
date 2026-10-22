"""Case-insensitive platform identity, without changing passwords or account ownership."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from platform_app.security import digest
from platform_app.store import Store
from test_auth_security import begin, complete
import test_platform
from test_platform import PASSWORD, login, allow_mail

web = test_platform.web


@pytest.mark.parametrize('username', ['ADMIN', 'AdMiN'])
def test_username_case_does_not_skip_password_or_email_checks(web, username):
    client, app = web
    assert client.post('/api/login', json={'username': username, 'password': PASSWORD.lower()}).status_code == 401
    challenge = client.post('/api/login', json={'username': username, 'password': PASSWORD, 'remember': True})
    assert challenge.status_code == 200 and 'challenge_id' in challenge.json()
    assert client.get('/api/me').status_code == 401
    assert complete(client, challenge).status_code == 200
    me = client.get('/api/me').json()
    assert me['username'] == 'admin'
    client.headers['X-CSRF-Token'] = me['csrf']
    assert client.post('/api/logout').status_code == 200
    assert begin(client, True).json() == {'ok': True}
    assert client.get('/api/me').json()['user_id'] == me['user_id']


def test_registration_rejects_case_variant_before_sending_email(web):
    client, app = web
    invitation = app.state.store.invite()
    count = len(client.mailer.messages)
    response = client.post('/api/redeem', json={
        'username': 'ADMIN', 'password': PASSWORD, 'token': invitation, 'email': 'other@example.com'})
    assert response.status_code == 400
    assert len(client.mailer.messages) == count
    with app.state.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 1
        assert db.execute('SELECT used FROM invites WHERE token=?', (digest(invitation),)).fetchone()[0] == 0


def test_pending_case_variant_registrations_only_create_one_account(web):
    client, app = web
    challenges = []
    for username, email in [('Case.User-1', 'first@example.com'), ('cASE.uSER-1', 'second@example.com')]:
        challenge = client.post('/api/redeem', json={
            'username': username, 'password': PASSWORD, 'token': app.state.store.invite(), 'email': email})
        assert challenge.status_code == 200
        challenges.append({'challenge_id': challenge.json()['challenge_id'], 'code': client.mailer.code()})
    assert client.post('/api/auth/verify', json=challenges[0]).status_code == 200
    owner = client.get('/api/me').json()
    assert owner['username'] == 'Case.User-1'
    assert client.post('/api/auth/verify', json=challenges[1]).status_code == 400
    with app.state.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM users WHERE username=? COLLATE NOCASE', ('case.user-1',)).fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM user_emails WHERE user_id=?', (owner['user_id'],)).fetchone()[0] == 1
    client.headers['X-CSRF-Token'] = owner['csrf']
    client.post('/api/logout')
    allow_mail(client)
    challenge = client.post('/api/login', json={'username': 'CASE.USER-1', 'password': PASSWORD})
    assert complete(client, challenge).status_code == 200
    assert client.get('/api/me').json()['user_id'] == owner['user_id']


def test_username_unique_index_blocks_concurrent_maintenance_creates(web):
    _, app = web
    store = app.state.store

    def create(username):
        try:
            return store.create_user(username, PASSWORD)
        except sqlite3.IntegrityError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ['CaseName', 'casename']))
    assert sum(uid is not None for uid in results) == 1
    with store.db() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE users SET username='CASENAME' WHERE username='admin'")


def test_username_index_upgrade_preserves_existing_identity(web):
    _, app = web
    store = app.state.store
    uid = store.create_user('Mixed.User-1', PASSWORD)
    with store.db() as db:
        before = dict(db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone())
        db.execute('DROP INDEX users_username_nocase')
    for _ in range(2):
        upgraded = Store(store.root, store.vault)
        assert upgraded.user(uid) == before
        with pytest.raises(sqlite3.IntegrityError):
            upgraded.create_user('mixed.user-1', PASSWORD)


def test_legacy_case_collision_stops_upgrade_without_merging_users(web):
    _, app = web
    store = app.state.store
    first = store.create_user('LegacyName', PASSWORD)
    with store.db() as db:
        db.execute('DROP INDEX users_username_nocase')
    second = store.create_user('legacyname', PASSWORD)
    before = [store.user(uid) for uid in (first, second)]
    with pytest.raises(RuntimeError, match='不会自动合并账号'):
        Store(store.root, store.vault)
    assert [store.user(uid) for uid in (first, second)] == before


def test_password_change_accepts_username_case_variant(web):
    client, app = web
    login(client)
    updated = 'New-case-sensitive-password'
    assert client.post('/api/password', json={'username': 'ADMIN', 'password': updated}).status_code == 200
    allow_mail(client)
    assert begin(client).status_code == 401
    challenge = client.post('/api/login', json={'username': 'AdMiN', 'password': updated})
    assert complete(client, challenge).status_code == 200
