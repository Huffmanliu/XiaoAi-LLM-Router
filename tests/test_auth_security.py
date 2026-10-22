"""Authentication abuse cases; all email, locations, accounts, and devices are synthetic."""
from copy import deepcopy
import time

from fastapi.testclient import TestClient
import pytest

from platform_app.app import create_app
from platform_app.security import digest, password_matches
from test_platform import (web, PASSWORD, CFG, QuietRuntime, FakeMailer, login, invite_user, allow_mail)  # noqa: F401


def begin(client, remember=False):
    return client.post('/api/login', json={'username': 'admin', 'password': PASSWORD, 'remember': remember})


def complete(client, challenge):
    return client.post('/api/auth/verify', json={'challenge_id': challenge.json()['challenge_id'], 'code': client.mailer.code()})


def test_password_alone_and_legacy_cookie_cannot_read_history(web):
    client, app = web
    with app.state.store.db() as db:
        uid = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (digest('old-password-only'), uid, 'csrf', time.time()+1000))
    app.state.store.save_history(uid, 'device', 'private', 'SECRET question', 'SECRET answer')
    client.cookies.set('speaker_session', 'old-password-only')
    challenge = begin(client)
    assert challenge.status_code == 200 and 'challenge_id' in challenge.json()
    assert 'set-cookie' not in challenge.headers
    assert client.get('/api/history').status_code == 401
    assert complete(client, challenge).status_code == 200
    assert client.get('/api/history').json()[0]['question'] == 'SECRET question'


def test_email_missing_cannot_be_claimed_by_password_owner(web):
    client, app = web
    app.state.store.create_user('legacy', PASSWORD)
    r = client.post('/api/login', json={'username': 'legacy', 'password': PASSWORD})
    assert r.status_code == 403
    assert client.post('/api/login', json={'username': 'legacy', 'password': PASSWORD, 'email': 'attacker@example.com'}).status_code == 422
    assert client.get('/api/me').status_code == 401


def test_registration_requires_email_and_single_use_code(web):
    client, app = web
    login(client)
    invitation = client.post('/api/admin/invites').json()['token']
    client.post('/api/logout')
    body = {'username': 'new-user', 'password': PASSWORD, 'token': invitation, 'email': 'new@example.com'}
    challenge = client.post('/api/redeem', json=body)
    assert challenge.status_code == 200
    with app.state.store.db() as db:
        assert not db.execute("SELECT 1 FROM users WHERE username='new-user'").fetchone()
    assert client.get('/api/me').status_code == 401
    assert complete(client, challenge).status_code == 200
    assert complete(client, challenge).status_code == 400
    assert client.get('/api/me').json()['username'] == 'new-user'
    assert client.post('/api/redeem', json={**body, 'username': 'another'}).status_code == 400


def test_verification_five_guesses_persist_and_expiry_fails(web):
    client, app = web
    challenge = begin(client)
    body = {'challenge_id': challenge.json()['challenge_id'], 'code': '000000' if client.mailer.code() != '000000' else '111111'}
    for _ in range(5):
        assert client.post('/api/auth/verify', json=body).status_code == 400
    assert complete(client, challenge).status_code == 400
    with app.state.store.db() as db:
        assert db.execute('SELECT attempts FROM auth_challenges WHERE id=?', (body['challenge_id'],)).fetchone()[0] == 5
    allow_mail(client)
    new = begin(client)
    with app.state.store.db() as db:
        db.execute('UPDATE auth_challenges SET expires=0')
    assert complete(client, new).status_code == 400


def test_mail_cooldown_is_shared_and_survives_new_auth_instance(web):
    client, app = web
    assert begin(client).status_code == 200
    assert begin(client).status_code == 429
    from platform_app.auth import Auth
    other = Auth(app.state.store, FakeMailer())
    with pytest.raises(Exception, match='频繁'):
        other.limit('mail-minute:admin@example.com', 1, 60)


def test_trusted_browser_skips_email_only_with_password_and_known_network(web):
    client, app = web
    challenge = begin(client, remember=True)
    assert complete(client, challenge).status_code == 200
    assert client.cookies.get('speaker_trust')
    client.headers['X-CSRF-Token'] = client.get('/api/me').json()['csrf']
    assert client.post('/api/logout').status_code == 200
    count = len(client.mailer.messages)
    assert begin(client, remember=True).json() == {'ok': True}
    assert len(client.mailer.messages) == count
    assert client.post('/api/login', json={'username': 'admin', 'password': 'wrong', 'remember': True}).status_code == 401
    # A new browser on the same IP still requires email, even if it spoofs X-Forwarded-For.
    allow_mail(client)
    new = TestClient(app)
    new.headers.update({'Origin': 'http://testserver', 'X-Forwarded-For': 'testclient'})
    assert 'challenge_id' in begin(new).json()


@pytest.mark.parametrize('field,age', [('last_login', 604801), ('verified_at', 2592001)])
def test_trust_does_not_slide_forever(web, field, age):
    client, app = web
    assert complete(client, begin(client, True)).status_code == 200
    allow_mail(client)
    with app.state.store.db() as db:
        db.execute('UPDATE trusted_browsers SET ' + field + '=?', (time.time()-age,))
    assert 'challenge_id' in begin(client, True).json()


def test_same_city_requires_trust_and_unknown_or_changed_city_requires_email(web, monkeypatch):
    client, app = web
    monkeypatch.setattr(app.state.auth, 'city', lambda address: 'CN:123')
    assert complete(client, begin(client, True)).status_code == 200
    client._transport.client = ('203.0.113.20', 1234)
    assert begin(client, True).json() == {'ok': True}
    allow_mail(client)
    monkeypatch.setattr(app.state.auth, 'city', lambda address: None)
    client._transport.client = ('203.0.113.21', 1234)
    assert 'challenge_id' in begin(client, True).json()


def test_polling_does_not_extend_browser_trust(web):
    client, app = web
    assert complete(client, begin(client, True)).status_code == 200
    with app.state.store.db() as db:
        before = tuple(db.execute('SELECT verified_at,last_login,expires FROM trusted_browsers').fetchone())
    for _ in range(3):
        assert client.get('/api/me').status_code == 200
    with app.state.store.db() as db:
        assert tuple(db.execute('SELECT verified_at,last_login,expires FROM trusted_browsers').fetchone()) == before


def test_forgot_is_generic_and_reset_rejects_replay_and_revokes_everything(web):
    client, app = web
    assert complete(client, begin(client, True)).status_code == 200
    allow_mail(client)
    missing = client.post('/api/password/forgot', json={'email': 'missing@example.com'})
    present = client.post('/api/password/forgot', json={'email': 'admin@example.com'})
    assert missing.status_code == present.status_code == 200 and missing.json() == present.json()
    token = client.mailer.reset_token()
    r = client.post('/api/password/reset', json={'token': token, 'password': 'New-password-for-recovery'})
    assert r.status_code == 200 and not client.cookies.get('speaker_session')
    assert client.get('/api/history').status_code == 401
    with app.state.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM trusted_browsers').fetchone()[0] == 0
    assert client.post('/api/password/reset', json={'token': token, 'password': 'New-password-for-recovery'}).status_code == 400
    allow_mail(client)
    assert begin(client).status_code == 401


def test_reset_cannot_reactivate_paused_user_and_wrong_secrets_fail(web):
    client, app = web
    login(client)
    allow_mail(client)
    client.post('/api/password/forgot', json={'email': 'admin@example.com'})
    token = client.mailer.reset_token()
    wrong = token[:-1] + ('a' if token[-1] != 'a' else 'b')
    assert client.post('/api/password/reset', json={'token': wrong, 'password': 'Another-valid-password'}).status_code == 400
    with app.state.store.db() as db:
        db.execute("UPDATE users SET active=0 WHERE username='admin'")
    assert client.post('/api/password/reset', json={'token': token, 'password': 'Another-valid-password'}).status_code == 400


def test_sensitive_operations_require_recent_email_and_bind_stepup_to_session(web):
    client, app = web
    login(client)
    with app.state.store.db() as db:
        db.execute('UPDATE session_security SET verified_at=?', (time.time()-601,))
    assert client.post('/api/xiaomi/replace', json={'username': 'x', 'password': 'y'}).status_code == 428
    assert client.delete('/api/xiaomi').status_code == 428
    assert client.post('/api/password', json={'username': 'admin', 'password': 'Another-valid-password'}).status_code == 428
    allow_mail(client)
    challenge = client.post('/api/security/challenge')
    body = {'challenge_id': challenge.json()['challenge_id'], 'code': client.mailer.code()}
    assert client.post('/api/auth/verify', json=body).status_code == 400
    assert client.post('/api/security/verify', json=body).status_code == 200
    assert client.get('/api/security').json()['recently_verified']


def test_change_email_verifies_new_address_and_revokes_old_access(web):
    client, app = web
    me = login(client)
    challenge = client.post('/api/security/email', json={'email': 'next@example.com'})
    assert challenge.status_code == 200
    assert app.state.auth.email(me['user_id'])['email'] == 'admin@example.com'
    r = client.post('/api/security/email/verify', json={'challenge_id': challenge.json()['challenge_id'], 'code': client.mailer.code()})
    assert r.status_code == 200
    assert client.get('/api/me').status_code == 401
    assert app.state.auth.email(me['user_id'])['email'] == 'next@example.com'
    with app.state.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM mail_outbox').fetchone()[0] >= 2


def test_revoke_trusted_device_forces_next_login_verification(web):
    client, app = web
    assert complete(client, begin(client, True)).status_code == 200
    client.headers['X-CSRF-Token'] = client.get('/api/me').json()['csrf']
    ident = client.get('/api/security').json()['trusted'][0]['id']
    assert client.delete('/api/security/trusted/' + ident).status_code == 200
    allow_mail(client)
    assert 'challenge_id' in begin(client, True).json()


def test_mail_failure_never_grants_session_or_leaves_valid_challenge(web, monkeypatch):
    client, app = web
    async def fail(*args):
        raise RuntimeError('PRIVATE-SMTP-PASSWORD')
    monkeypatch.setattr(client.mailer, 'send', fail)
    response = begin(client)
    assert response.status_code == 503 and 'PRIVATE-' not in response.text
    assert client.get('/api/me').status_code == 401
    with app.state.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM auth_challenges').fetchone()[0] == 0


def test_production_rejects_http_and_wrong_host_and_uses_secure_cookies(tmp_path, monkeypatch):
    monkeypatch.setenv('PLATFORM_DEPLOYMENT', 'production')
    monkeypatch.setenv('PLATFORM_PUBLIC_ORIGIN', 'http://insecure.example')
    monkeypatch.delenv('PLATFORM_KEY_FILE', raising=False)
    with pytest.raises(RuntimeError, match='HTTPS'):
        create_app(tmp_path / 'prod', deepcopy(CFG), runtime_factory=QuietRuntime, mailer=FakeMailer())
    monkeypatch.setenv('PLATFORM_PUBLIC_ORIGIN', 'https://secure.example')
    app = create_app(tmp_path / 'prod', deepcopy(CFG), runtime_factory=QuietRuntime, mailer=FakeMailer())
    c = TestClient(app, base_url='http://secure.example')
    assert c.get('/').status_code == 400
    c = TestClient(app, base_url='https://secure.example')
    assert c.get('/').headers['strict-transport-security'] == 'max-age=31536000'
    assert c.get('/', headers={'Host': 'evil.example'}).status_code == 400
    assert app.state.auth.secure


def test_concurrent_registration_cannot_consume_invite_twice(web):
    client, app = web
    login(client)
    token = client.post('/api/admin/invites').json()['token']
    payload = {'token': token, 'password': PASSWORD}
    a = client.post('/api/redeem', json={**payload, 'username': 'one', 'email': 'one@example.com'})
    code_a = client.mailer.code()
    b = client.post('/api/redeem', json={**payload, 'username': 'two', 'email': 'two@example.com'})
    code_b = client.mailer.code()
    assert client.post('/api/auth/verify', json={'challenge_id': a.json()['challenge_id'], 'code': code_a}).status_code == 200
    assert client.post('/api/auth/verify', json={'challenge_id': b.json()['challenge_id'], 'code': code_b}).status_code == 400
    with app.state.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM users WHERE username IN ('one','two')").fetchone()[0] == 1


def test_pending_login_code_cannot_survive_password_reset(web):
    client, app = web
    me = login(client)
    allow_mail(client)
    pending = begin(client)
    code = client.mailer.code()
    with app.state.store.db() as db:
        from platform_app.security import password_hash
        db.execute('UPDATE users SET password=? WHERE id=?', (password_hash('Different-current-password'), me['user_id']))
    assert client.post('/api/auth/verify', json={'challenge_id': pending.json()['challenge_id'], 'code': code}).status_code == 400
    assert password_matches('Different-current-password', app.state.store.user(me['user_id'])['password'])


def test_trust_record_id_cannot_be_used_as_browser_cookie(web):
    client, app = web
    assert complete(client, begin(client, True)).status_code == 200
    client.headers['X-CSRF-Token'] = client.get('/api/me').json()['csrf']
    record = client.get('/api/security').json()['trusted'][0]['id']
    client.cookies.clear()
    client.cookies.set('speaker_trust', record)
    allow_mail(client)
    assert 'challenge_id' in begin(client, True).json()


def test_many_password_failures_require_email_even_on_trusted_browser(web):
    client, app = web
    assert complete(client, begin(client, True)).status_code == 200
    allow_mail(client)
    for _ in range(6):
        assert client.post('/api/login', json={'username': 'admin', 'password': 'incorrect'}).status_code == 401
    assert 'challenge_id' in begin(client, True).json()


def test_forwarded_ip_and_scheme_only_accepted_from_configured_proxy(tmp_path, monkeypatch):
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    monkeypatch.setenv('PLATFORM_DEPLOYMENT', 'production')
    monkeypatch.setenv('PLATFORM_PUBLIC_ORIGIN', 'https://secure.example')
    monkeypatch.delenv('PLATFORM_KEY_FILE', raising=False)
    app = create_app(tmp_path / 'proxy', deepcopy(CFG), runtime_factory=QuietRuntime, mailer=FakeMailer())
    from fastapi import Request
    @app.get('/test-proxy')
    async def probe(request: Request):
        return {'address': request.client.host}
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=['127.0.0.1'])
    headers = {'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '198.51.100.12'}
    c = TestClient(wrapped, base_url='http://secure.example', client=('203.0.113.12', 1234))
    assert c.get('/test-proxy', headers=headers).status_code == 400
    c = TestClient(wrapped, base_url='http://secure.example', client=('127.0.0.1', 1234))
    assert c.get('/test-proxy', headers=headers).json()['address'] == '198.51.100.12'


def test_every_successful_login_queues_one_notice_but_pending_failure_and_refresh_do_not(web):
    client, app = web
    def notices():
        with app.state.store.db() as db:
            rows = db.execute('SELECT id,payload FROM mail_outbox ORDER BY rowid').fetchall()
        return [app.state.store.vault.open(row['payload'], 'outbox:' + row['id'])['message'] for row in rows]
    challenge = begin(client, True)
    assert notices() == []
    assert complete(client, challenge).status_code == 200
    first = notices()
    assert len(first) == 1 and '邮箱验证登录' in first[0]
    assert '北京时间 UTC+08:00' in first[0] and '平台账号：admin' in first[0]
    assert '登录IP：testclient' in first[0]
    assert client.get('/api/me').status_code == 200
    assert client.get('/').status_code == 200
    assert client.post('/api/login', json={'username': 'admin', 'password': 'wrong'}).status_code == 401
    assert notices() == first
    assert begin(client, True).json() == {'ok': True}
    second = notices()
    assert len(second) == 2 and '可信设备免验证码登录' in second[-1]
    assert '平台密码＋可信设备验证' in second[-1]
    assert '北京时间 UTC+08:00' in second[-1]
