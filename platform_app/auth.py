"""Email ownership, recovery, and revocable browser trust. No password-only sessions."""
import asyncio
import hmac
import ipaddress
import os
import secrets
import time
from datetime import datetime
from urllib.parse import urlsplit

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .mailer import Mailer
from .security import digest, masked_account, normalize_email, password_hash, password_matches
from .store import CST, Rejected


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Login(Input):
    username: str = Field(min_length=3, max_length=32, pattern=r'^[A-Za-z0-9_.-]+$')
    password: str = Field(min_length=1, max_length=256)
    remember: bool = False


class Register(Login):
    token: str = Field(min_length=20, max_length=128)
    email: str = Field(min_length=3, max_length=254)


class Verify(Input):
    challenge_id: str = Field(min_length=43, max_length=43)
    code: str = Field(min_length=6, max_length=6, pattern=r'^\d{6}$')


class Email(Input):
    email: str = Field(min_length=3, max_length=254)


class Reset(Input):
    token: str = Field(min_length=87, max_length=87)
    password: str = Field(min_length=12, max_length=256)


class Password(Input):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=12, max_length=256)


class Auth:
    def __init__(self, store, mailer=None):
        self.store = store
        self.vault = store.vault
        self.mailer = mailer if mailer is not None else Mailer()
        self.production = os.environ.get('PLATFORM_DEPLOYMENT') == 'production'
        self.origin = os.environ.get('PLATFORM_PUBLIC_ORIGIN', '').rstrip('/')
        self.secure = self.production or os.environ.get('PLATFORM_COOKIE_SECURE') == '1'
        parts = urlsplit(self.origin)
        if self.production and (parts.scheme != 'https' or not parts.hostname or parts.path
                                or parts.query or parts.fragment or parts.username):
            raise RuntimeError('生产环境必须设置正式 HTTPS 来源 PLATFORM_PUBLIC_ORIGIN')
        self.geo = None
        path = os.environ.get('PLATFORM_CITY_DB')
        if path:
            import maxminddb
            self.geo = maxminddb.open_database(path)
        self.dummy = password_hash(secrets.token_urlsafe(24))

    def email(self, uid, db=None):
        if db is None:
            with self.store.db() as connection:
                return self.email(uid, connection)
        row = db.execute('SELECT * FROM user_emails WHERE user_id=?', (uid,)).fetchone()
        return {**dict(row), 'email': self.vault.open(row['address'], 'email:' + uid)} if row else None

    def set_legacy_email(self, uid, address):
        """Trusted private maintenance only; passwords cannot claim an existing account's email."""
        address = normalize_email(address)
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.email(uid, db):
                raise Rejected('该用户已有安全邮箱，请使用更换邮箱流程')
            db.execute('INSERT INTO user_emails VALUES(?,?,?,0)',
                       (uid, self.vault.fingerprint(address), self.vault.seal(address, 'email:' + uid)))
            db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))

    def city(self, address):
        if not self.geo:
            return None
        try:
            result = self.geo.get(str(ipaddress.ip_address(address))) or {}
            country, city = result.get('country', {}).get('iso_code'), result.get('city', {}).get('geoname_id')
            return f'{country}:{city}' if country and city else None
        except (ValueError, TypeError, OSError):
            return None

    def location(self, request):
        # ASGI client is rewritten only by explicitly trusted proxy configuration.
        address = request.client.host
        return {'ip': address, 'city': self.city(address), 'agent': digest(request.headers.get('user-agent', ''))}

    def limit(self, name, maximum=6, seconds=3600, message='操作太频繁，请稍后再试'):
        now = time.time()
        key = self.vault.fingerprint(name)
        rejected = False
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM auth_limits WHERE key=?', (key,)).fetchone()
            if row and row['start'] > now - seconds:
                rejected = row['count'] >= maximum
                if not rejected:
                    db.execute('UPDATE auth_limits SET count=count+1 WHERE key=?', (key,))
            else:
                db.execute('INSERT OR REPLACE INTO auth_limits VALUES(?,?,1)', (key, now))
        if rejected:
            raise Rejected(message, 429)

    async def issue(self, purpose, address, payload, uid=None):
        self.limit('mail-minute:' + address, 1, 60)
        self.limit('mail-hour:' + address)
        cid = secrets.token_urlsafe(32)
        code = secrets.token_urlsafe(32) if purpose == 'reset' else f'{secrets.randbelow(1000000):06}'
        target = self.vault.fingerprint(address)
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # A resend invalidates earlier codes for this purpose and recipient.
            db.execute('UPDATE auth_challenges SET used=1 WHERE purpose=? AND target=?', (purpose, target))
            db.execute('INSERT INTO auth_challenges VALUES(?,?,?,?,?,?,?,0,0)',
                       (cid, purpose, uid, target, self.vault.fingerprint(cid + ':' + code),
                        self.vault.seal({**payload, 'email': address}, 'challenge:' + cid), time.time() + 600))
        subject = '小爱 AI · 安全验证'
        if purpose == 'reset':
            if not self.origin:
                with self.store.db() as db:
                    db.execute('DELETE FROM auth_challenges WHERE id=?', (cid,))
                raise Rejected('找回密码暂未配置，请联系管理员', 503)
            body = f'平台账号：{payload["username"]}\n\n设置新密码：\n{self.origin}/#reset={cid}.{code}\n\n链接10分钟内有效，只能使用一次。重置后所有设备需要重新登录。\n如非本人操作，请忽略此邮件。'
            subject = '小爱 AI · 重置平台密码'
        else:
            labels = {'register': '注册平台账号', 'login': '登录平台', 'stepup': '确认敏感操作', 'email': '更换安全邮箱'}
            body = f'你正在{labels[purpose]}。\n\n邮箱验证码：{code}\n\n10分钟内有效，最多尝试5次。请勿将验证码告知他人。\n本邮件来自独立的小爱 AI 服务，并非小米官方。'
        try:
            await self.mailer.send(address, subject, body)
        except Exception:
            with self.store.db() as db:
                db.execute('DELETE FROM auth_challenges WHERE id=?', (cid,))
            raise Rejected('邮件暂时无法发送，请稍后重试或联系管理员', 503) from None
        return {'challenge_id': cid, 'email_hint': masked_account(address), 'expires_in': 600}

    def consume(self, db, cid, code, purposes, uid=None, session=None):
        row = db.execute('SELECT * FROM auth_challenges WHERE id=?', (cid,)).fetchone()
        if (not row or row['purpose'] not in purposes or row['used'] or row['expires'] <= time.time()
                or row['attempts'] >= 5 or (uid is not None and row['user_id'] != uid)):
            return None
        payload = self.vault.open(row['payload'], 'challenge:' + cid)
        if session is not None and payload.get('session') != session:
            return None
        db.execute('UPDATE auth_challenges SET attempts=attempts+1 WHERE id=?', (cid,))
        if not hmac.compare_digest(row['code_hash'], self.vault.fingerprint(cid + ':' + code)):
            return None  # Caller commits attempts before returning an error.
        db.execute('UPDATE auth_challenges SET used=1 WHERE id=?', (cid,))
        return dict(row), payload

    def notify(self, db, address, message):
        ident = secrets.token_hex(16)
        payload = self.vault.seal({'email': address, 'message': message}, 'outbox:' + ident)
        db.execute('INSERT INTO mail_outbox VALUES(?,?,0,?)', (ident, payload, time.time()))

    def login_notice(self, db, uid, address, request, method):
        username = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()[0]
        introductions = {'email': '平台账号完成了一次邮箱验证登录。',
                         'trusted': '平台账号完成了一次可信设备免验证码登录。',
                         'register': '平台账号已注册并完成首次登录。'}
        methods = {'email': '平台密码＋邮箱验证码', 'trusted': '平台密码＋可信设备验证',
                   'register': '注册邮箱验证'}
        timestamp = datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')
        body = (f'{introductions[method]}\n平台账号：{username}\n登录方式：{methods[method]}'
                f'\n时间：{timestamp}（北京时间 UTC+08:00）\n登录IP：{request.client.host}'
                '\n如非本人操作，请尽快重置平台密码。')
        self.notify(db, address, body)

    async def deliver_notifications(self):
        while True:
            with self.store.db() as db:
                rows = db.execute('SELECT * FROM mail_outbox WHERE next_attempt<? AND attempts<5 LIMIT 10',
                                  (time.time(),)).fetchall()
            for row in rows:
                data = self.vault.open(row['payload'], 'outbox:' + row['id'])
                try:
                    await self.mailer.send(data['email'], '小爱 AI · 账号安全通知', data['message'])
                except Exception:
                    with self.store.db() as db:
                        db.execute('UPDATE mail_outbox SET attempts=attempts+1,next_attempt=? WHERE id=?',
                                   (time.time() + 300, row['id']))
                else:
                    with self.store.db() as db:
                        db.execute('DELETE FROM mail_outbox WHERE id=?', (row['id'],))
            await asyncio.sleep(30)

    def revoke(self, db, uid):
        db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
        db.execute('DELETE FROM trusted_browsers WHERE user_id=?', (uid,))
        db.execute('UPDATE auth_challenges SET used=1 WHERE user_id=?', (uid,))

    def recent(self, user):
        if user['verified_at'] < time.time() - 600:
            raise Rejected('请先在“账号与安全”完成邮箱验证，再进行此操作', 428)

    def trusted(self, db, uid, request):
        row = db.execute('SELECT * FROM trusted_browsers WHERE token=? AND user_id=?',
                         (digest(request.cookies.get('speaker_trust', '')), uid)).fetchone()
        now = time.time()
        if not row or row['expires'] <= now or row['last_login'] <= now - 604800 or row['verified_at'] <= now - 2592000:
            return None
        old = self.vault.open(row['location'], 'trust:' + row['token'])
        new = self.location(request)
        known = old['ip'] == new['ip'] or (old['city'] and old['city'] == new['city'])
        return row['verified_at'] if known and old['agent'] == new['agent'] else None

    def session(self, db, uid, request, verified_at, remember=False):
        # Replace the browser's previous session, rather than silently accumulating sessions.
        db.execute('DELETE FROM sessions WHERE token=?', (digest(request.cookies.get('speaker_session', '')),))
        now = time.time()
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (digest(token), uid, csrf, min(now + 604800, verified_at + 2592000)))
        db.execute('INSERT INTO session_security VALUES(?,?,?,?,?)',
                   (digest(token), verified_at, now, request.headers.get('user-agent', '')[:200], request.client.host))
        response = JSONResponse({'ok': True})
        response.set_cookie('speaker_session', token, httponly=True, samesite='strict', max_age=604800, secure=self.secure)
        db.execute('DELETE FROM trusted_browsers WHERE token=?', (digest(request.cookies.get('speaker_trust', '')),))
        if remember:
            trust = secrets.token_urlsafe(32)
            key = digest(trust)
            db.execute('INSERT INTO trusted_browsers VALUES(?,?,?,?,?,?,?)',
                       (key, uid, verified_at, now, verified_at + 2592000,
                        self.vault.seal(self.location(request), 'trust:' + key), request.headers.get('user-agent', '')[:200]))
            response.set_cookie('speaker_trust', trust, httponly=True, samesite='strict', max_age=max(1, int(verified_at + 2592000 - now)), secure=self.secure)
        else:
            response.delete_cookie('speaker_trust')
        return response

    def install(self, app, current, rate):
        store = self.store

        @app.post('/api/login')
        async def login(data: Login, request: Request):
            rate(request, 'login')
            self.limit('login:' + data.username.lower(), 10, 600,
                       '平台登录尝试次数过多，请10分钟后再试；请确认填写的是平台注册用户名。')
            with store.db() as db:
                row = db.execute('SELECT * FROM users WHERE username=?', (data.username,)).fetchone()
            if not password_matches(data.password, row['password'] if row else self.dummy) or not row or not row['active']:
                raise Rejected('平台账号、密码有误或服务已暂停。请使用平台注册用户名与平台密码。', 401)
            mail = self.email(row['id'])
            if not mail:
                raise Rejected('此账号需由管理员确认安全邮箱后再登录，请联系管理员', 403)
            with store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                attempts = db.execute('SELECT count FROM auth_limits WHERE key=?',
                                      (self.vault.fingerprint('login:' + data.username.lower()),)).fetchone()
                valid = self.trusted(db, row['id'], request) if mail['verified'] and attempts[0] <= 5 else None
                owner = db.execute('SELECT password,active FROM users WHERE id=?', (row['id'],)).fetchone()
                if not owner or owner['password'] != row['password'] or not owner['active']:
                    raise Rejected('账号状态已变化，请重新登录', 401)
                if valid:
                    response = self.session(db, row['id'], request, valid, data.remember)
                    self.login_notice(db, row['id'], mail['email'], request, 'trusted')
                    return response
            return await self.issue('login', mail['email'], {'password': row['password'], 'remember': data.remember}, row['id'])

        @app.post('/api/redeem')
        async def register(data: Register, request: Request):
            rate(request, 'redeem', 5)
            try:
                address = normalize_email(data.email)
                encoded = password_hash(data.password)
            except ValueError as exc:
                raise Rejected(str(exc)) from None
            with store.db() as db:
                invitation = db.execute("SELECT 1 FROM invites WHERE token=? AND kind='invite' AND used=0 AND expires>?",
                                        (digest(data.token), time.time())).fetchone()
                taken = db.execute('SELECT 1 FROM users WHERE username=?', (data.username,)).fetchone()
                email_taken = db.execute('SELECT 1 FROM user_emails WHERE email_key=?', (self.vault.fingerprint(address),)).fetchone()
            if not invitation or taken or email_taken:
                raise Rejected('注册未完成，请检查邀请和账号信息；如曾注册，请使用忘记密码')
            payload = {'username': data.username, 'password': encoded, 'invite': digest(data.token),
                       'remember': data.remember}
            return await self.issue('register', address, payload)

        @app.post('/api/auth/verify')
        async def verify(data: Verify, request: Request):
            rate(request, 'auth-code', 10)
            result = None
            with store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                challenge = self.consume(db, data.challenge_id, data.code, ('register', 'login'))
                if challenge:
                    row, p = challenge
                    if row['purpose'] == 'register':
                        invite = db.execute("SELECT 1 FROM invites WHERE token=? AND kind='invite' AND used=0 AND expires>?",
                                            (p['invite'], time.time())).fetchone()
                        taken = db.execute('SELECT 1 FROM users WHERE username=?', (p['username'],)).fetchone()
                        email_taken = db.execute('SELECT 1 FROM user_emails WHERE email_key=?', (row['target'],)).fetchone()
                        if invite and not taken and not email_taken:
                            uid = secrets.token_hex(16)
                            db.execute('INSERT INTO users(id,username,password,created) VALUES(?,?,?,?)',
                                       (uid, p['username'], p['password'], time.time()))
                            db.execute('INSERT INTO user_emails VALUES(?,?,?,1)',
                                       (uid, row['target'], self.vault.seal(p['email'], 'email:' + uid)))
                            db.execute('UPDATE invites SET used=1 WHERE token=?', (p['invite'],))
                            result = self.session(db, uid, request, time.time(), p['remember'])
                            self.login_notice(db, uid, p['email'], request, 'register')
                    else:
                        uid = row['user_id']
                        owner = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
                        mail = self.email(uid, db)
                        if owner and owner['active'] and owner['password'] == p['password'] and mail and mail['email_key'] == row['target']:
                            db.execute('UPDATE user_emails SET verified=1 WHERE user_id=?', (uid,))
                            result = self.session(db, uid, request, time.time(), p['remember'])
                            self.login_notice(db, uid, p['email'], request, 'email')
            if result is None:
                raise Rejected('验证码有误、已失效或账号信息已变化，请重新开始')
            return result

        @app.post('/api/password/forgot')
        async def forgot(data: Email, request: Request):
            rate(request, 'forgot', 5)
            try:
                address = normalize_email(data.email)
            except ValueError:
                raise Rejected('请填写有效的邮箱地址') from None
            # Identical response even for unknown, unverified, paused, rate-limited recipients or mail failure.
            with store.db() as db:
                row = db.execute('SELECT u.* FROM user_emails e JOIN users u ON u.id=e.user_id WHERE e.email_key=? AND e.verified=1 AND u.active=1',
                                 (self.vault.fingerprint(address),)).fetchone()

            async def send():
                try:
                    self.limit('forgot-target:' + address, 3, 3600)
                    if row:
                        await self.issue('reset', address, {'password': row['password'], 'username': row['username']}, row['id'])
                except Rejected:
                    pass
            # BackgroundTasks schedules delivery after the generic response; SMTP timing reveals no existence.
            from fastapi import BackgroundTasks
            tasks = BackgroundTasks()
            tasks.add_task(send)
            return JSONResponse({'message': '如果该邮箱对应已验证的平台账号，我们会发送密码重置邮件，请检查收件箱和垃圾邮件。'}, background=tasks)

        @app.post('/api/password/reset')
        async def reset(data: Reset, request: Request):
            rate(request, 'reset', 5)
            encoded = password_hash(data.password)
            cid, separator, secret = data.token.partition('.')
            if not separator:
                raise Rejected('重置链接已失效，请重新申请')
            done = False
            with store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                challenge = self.consume(db, cid, secret, ('reset',))
                if challenge:
                    row, p = challenge
                    owner = db.execute('SELECT password,active FROM users WHERE id=?', (row['user_id'],)).fetchone()
                    mail = self.email(row['user_id'], db)
                    if owner and owner['active'] and owner['password'] == p['password'] and mail and mail['verified'] and mail['email_key'] == row['target']:
                        db.execute('UPDATE users SET password=? WHERE id=?', (encoded, row['user_id']))
                        self.revoke(db, row['user_id'])
                        self.notify(db, p['email'], '平台密码已重置，所有旧登录会话和可信设备已撤销。请使用新密码重新登录。如非本人操作，请联系管理员。')
                        done = True
            if not done:
                raise Rejected('重置链接已失效，请重新申请')
            response = JSONResponse({'message': '密码已重置，请使用新密码登录并验证邮箱。'})
            response.delete_cookie('speaker_session')
            response.delete_cookie('speaker_trust')
            return response

        @app.post('/api/logout')
        async def logout(request: Request, user=Depends(current)):
            with store.db() as db:
                db.execute('DELETE FROM sessions WHERE token=?', (user['session_key'],))
            response = JSONResponse({'ok': True})
            response.delete_cookie('speaker_session')
            return response

        @app.post('/api/security/challenge')
        async def stepup(request: Request, user=Depends(current)):
            rate(request, 'stepup', 3)
            mail = self.email(user['user_id'])
            return await self.issue('stepup', mail['email'], {'session': user['session_key']}, user['user_id'])

        @app.post('/api/security/verify')
        async def stepup_verify(data: Verify, request: Request, user=Depends(current)):
            rate(request, 'auth-code', 10)
            done = False
            with store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                challenge = self.consume(db, data.challenge_id, data.code, ('stepup',), user['user_id'], user['session_key'])
                if challenge:
                    db.execute('UPDATE session_security SET verified_at=? WHERE token=?', (time.time(), user['session_key']))
                    done = True
            if not done:
                raise Rejected('验证码有误或已失效，请重新申请')
            return {'ok': True}

        @app.post('/api/password')
        async def password(data: Password, request: Request, user=Depends(current)):
            rate(request, 'password', 5)
            self.recent(user)
            if data.username != user['username']:
                raise Rejected('平台账号不匹配')
            encoded = password_hash(data.password)
            with store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                mail = self.email(user['user_id'], db)
                db.execute('UPDATE users SET password=? WHERE id=?', (encoded, user['user_id']))
                self.revoke(db, user['user_id'])
                self.notify(db, mail['email'], '平台密码已修改，其他登录会话及全部可信设备已撤销。如非本人操作，请联系管理员。')
                response = self.session(db, user['user_id'], request, time.time())
            bootstrap = store.root / 'bootstrap-admin.txt'
            if user['role'] == 'admin' and bootstrap.exists():
                bootstrap.unlink()
            return response

        @app.post('/api/security/email')
        async def change_email(data: Email, request: Request, user=Depends(current)):
            rate(request, 'email-change', 3)
            self.recent(user)
            try:
                address = normalize_email(data.email)
            except ValueError as exc:
                raise Rejected(str(exc)) from None
            with store.db() as db:
                taken = db.execute('SELECT 1 FROM user_emails WHERE email_key=?', (self.vault.fingerprint(address),)).fetchone()
            if taken:
                raise Rejected('该邮箱无法用于本次更换，请检查邮箱或使用账号找回')
            return await self.issue('email', address, {'session': user['session_key']}, user['user_id'])

        @app.post('/api/security/email/verify')
        async def email_verify(data: Verify, request: Request, user=Depends(current)):
            rate(request, 'auth-code', 10)
            self.recent(user)
            done = False
            with store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                challenge = self.consume(db, data.challenge_id, data.code, ('email',), user['user_id'], user['session_key'])
                if challenge:
                    row, p = challenge
                    taken = db.execute('SELECT 1 FROM user_emails WHERE email_key=?', (row['target'],)).fetchone()
                    if not taken:
                        old = self.email(user['user_id'], db)
                        db.execute('UPDATE user_emails SET email_key=?,address=?,verified=1 WHERE user_id=?',
                                   (row['target'], self.vault.seal(p['email'], 'email:' + user['user_id']), user['user_id']))
                        self.revoke(db, user['user_id'])
                        for address in (old['email'], p['email']):
                            self.notify(db, address, '平台安全邮箱已更换，所有登录会话及可信设备已撤销。如非本人操作，请联系管理员。')
                        done = True
            if not done:
                raise Rejected('验证码有误、已失效或邮箱不可用')
            response = JSONResponse({'message': '安全邮箱已更换，请重新登录。'})
            response.delete_cookie('speaker_session')
            response.delete_cookie('speaker_trust')
            return response

        @app.get('/api/security')
        async def security(request: Request, user=Depends(current)):
            with store.db() as db:
                sessions = [dict(r) for r in db.execute(
                    'SELECT s.token AS id,m.created,m.agent,m.address FROM sessions s JOIN session_security m ON m.token=s.token WHERE s.user_id=? AND s.expires>?',
                    (user['user_id'], time.time()))]
                trusted = [dict(r) for r in db.execute(
                    'SELECT token AS id,agent,verified_at,last_login,expires FROM trusted_browsers WHERE user_id=? AND expires>?',
                    (user['user_id'], time.time()))]
            for session in sessions:
                session['current'] = session['id'] == user['session_key']
            return {'email_hint': masked_account(self.email(user['user_id'])['email']), 'sessions': sessions, 'trusted': trusted,
                    'recently_verified': user['verified_at'] >= time.time() - 600}

        @app.delete('/api/security/sessions')
        async def revoke_others(user=Depends(current)):
            with store.db() as db:
                db.execute('DELETE FROM sessions WHERE user_id=? AND token!=?', (user['user_id'], user['session_key']))
                db.execute('DELETE FROM trusted_browsers WHERE user_id=?', (user['user_id'],))
            return {'ok': True}

        @app.delete('/api/security/trusted/{ident}')
        async def forget_device(ident: str, user=Depends(current)):
            with store.db() as db:
                db.execute('DELETE FROM trusted_browsers WHERE user_id=? AND token=?', (user['user_id'], ident))
            return {'ok': True}
