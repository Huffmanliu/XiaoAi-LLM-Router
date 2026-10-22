"""Single-instance SQLite state; content kept in a separate, unbacked-up database."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
import sqlite3
import time

from .device_policy import NATIVE_AI_MODELS, NATIVE_AI_REASON, native_ai
from .capacity import DEFAULT_LIMITS
from .security import digest, password_hash
from .routing import DEFAULT_DIALOGUE, SEED_RULES

CST = timezone(timedelta(hours=8))
DEFAULTS = {'daily_limit': 50, 'minute_limit': 5, 'max_output_tokens': 1024,
            'retention_days': 60, 'ttl_seconds': 600, 'max_turns': 10}


class Rejected(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Store:
    def __init__(self, root, vault):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.vault = vault
        self.path = self.root / 'platform.sqlite3'
        self.history_path = self.root / 'history.sqlite3'
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user', active INTEGER NOT NULL DEFAULT 1,
                policy TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS invites (
                token TEXT PRIMARY KEY, kind TEXT NOT NULL, user_id TEXT, expires REAL NOT NULL,
                used INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY, user_id TEXT NOT NULL, csrf TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts (
                user_id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, secret BLOB NOT NULL,
                state TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS devices (
                user_id TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, hardware TEXT NOT NULL,
                supported INTEGER NOT NULL, reason TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
                present INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(user_id,id));
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, device_id TEXT,
                source TEXT NOT NULL, request_id TEXT NOT NULL, created REAL NOT NULL,
                day TEXT NOT NULL, status TEXT NOT NULL, started INTEGER NOT NULL DEFAULT 0,
                model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
                cache_hit_tokens INTEGER, cache_miss_tokens INTEGER, elapsed_ms INTEGER,
                finish_reason TEXT, speech_status TEXT NOT NULL DEFAULT 'not_requested',
                UNIQUE(user_id,source,request_id));
            CREATE INDEX IF NOT EXISTS request_owner_time ON requests(user_id,created);
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS imports (fingerprint TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS user_emails (
                user_id TEXT PRIMARY KEY REFERENCES users(id), email_key TEXT UNIQUE NOT NULL,
                address BLOB NOT NULL, verified INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS auth_challenges (
                id TEXT PRIMARY KEY, purpose TEXT NOT NULL, user_id TEXT, target TEXT NOT NULL,
                code_hash TEXT NOT NULL, payload BLOB NOT NULL, expires REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, used INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS auth_limits (key TEXT PRIMARY KEY, start REAL NOT NULL, count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS session_security (
                token TEXT PRIMARY KEY REFERENCES sessions(token) ON DELETE CASCADE,
                verified_at REAL NOT NULL, created REAL NOT NULL, agent TEXT NOT NULL, address TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trusted_browsers (
                token TEXT PRIMARY KEY, user_id TEXT NOT NULL, verified_at REAL NOT NULL,
                last_login REAL NOT NULL, expires REAL NOT NULL, location BLOB NOT NULL, agent TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS mail_outbox (
                id TEXT PRIMARY KEY, payload BLOB NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS device_claims (
                device_key TEXT PRIMARY KEY, user_id TEXT NOT NULL, device_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS dialogue_preferences (user_id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fallback_rules (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, match_type TEXT NOT NULL, content TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY);
            INSERT OR IGNORE INTO settings VALUES (1,'{}');
            ''')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute("SELECT 1 FROM migrations WHERE name='device-claims-v1'").fetchone():
                for row in db.execute('SELECT id,COUNT(DISTINCT user_id) AS owners,MIN(user_id) AS owner FROM devices GROUP BY id').fetchall():
                    owner = row['owner'] if row['owners'] == 1 else '__conflict__'
                    db.execute('INSERT INTO device_claims VALUES(?,?,?)', ('mina:' + row['id'], owner, row['id']))
                    if row['owners'] > 1:
                        db.execute("UPDATE devices SET enabled=0,supported=0,reason=? WHERE id=?",
                                   ('设备归属存在冲突，请联系管理员确认', row['id']))
                db.execute("INSERT INTO migrations VALUES('device-claims-v1')")
            if not db.execute("SELECT 1 FROM migrations WHERE name='fixed-dialogue-v1'").fetchone():
                defaults = json.loads(db.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])
                defaults.pop('system_prompt', None)
                db.execute('UPDATE settings SET data=? WHERE id=1', (json.dumps(defaults),))
                for row in db.execute('SELECT id,policy FROM users').fetchall():
                    policy = json.loads(row['policy'])
                    for key in ('system_prompt', 'ttl_seconds', 'max_turns'):
                        policy.pop(key, None)
                    db.execute('UPDATE users SET policy=? WHERE id=?', (json.dumps(policy), row['id']))
                for index, (name, kind, content) in enumerate(SEED_RULES):
                    db.execute('INSERT INTO fallback_rules VALUES(?,?,?,?,1)', (f'seed-{index}', name, kind, content))
                db.execute("INSERT INTO migrations VALUES('fixed-dialogue-v1')")
        with self.db() as db:
            for hardware in NATIVE_AI_MODELS:
                db.execute("UPDATE devices SET enabled=0,supported=0,reason=? WHERE UPPER(TRIM(hardware)) IN (?,?)",
                           (NATIVE_AI_REASON, hardware, 'XIAOMI.WIFISPEAKER.' + hardware))
        with self.history_db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS history (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, device_id TEXT,
                created REAL NOT NULL, content BLOB NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS history_age ON history(created)')
        for p in (self.path, self.history_path):
            p.chmod(0o600)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA foreign_keys=ON')
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def history_db(self):
        connection = sqlite3.connect(self.history_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA secure_delete=ON')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def recover_requests(self):
        # Called only after the instance lock is acquired.
        with self.db() as db:
            db.execute("UPDATE requests SET status='interrupted' WHERE status='reserved' AND started=1")
            db.execute("UPDATE requests SET status='cancelled' WHERE status='reserved' AND started=0")
            db.execute("UPDATE requests SET speech_status='interrupted' WHERE speech_status='submitting'")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_attempts'").fetchone():
                db.execute("UPDATE model_attempts SET status='interrupted',error_kind='interrupted',finished=? WHERE status='started'",
                           (time.time(),))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_tests'").fetchone():
                db.execute("UPDATE model_tests SET result=CASE WHEN EXISTS (SELECT 1 FROM requests r "
                           "WHERE r.id=model_tests.request_id AND r.started=1) THEN 'interrupted' ELSE 'not_started' END,"
                           "finished=? WHERE result IN ('queued','started')", (time.time(),))

    def user(self, uid):
        with self.db() as db:
            r = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        return dict(r) if r else None

    def create_user(self, username, password, role='user'):
        uid = secrets.token_hex(16)
        with self.db() as db:
            db.execute('INSERT INTO users(id,username,password,role,created) VALUES(?,?,?,?,?)',
                       (uid, username, password_hash(password), role, time.time()))
        return uid

    def defaults(self):
        with self.db() as db:
            data = json.loads(db.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])
        return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}

    def policy(self, uid):
        u = self.user(uid)
        return {**self.defaults(), **{k: v for k, v in json.loads(u['policy']).items() if k in DEFAULTS}}

    def runtime_limits(self):
        with self.db() as db:
            data = json.loads(db.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])
        return {**DEFAULT_LIMITS, **data.get('runtime_limits', {})}

    def update_settings(self, values):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            data = json.loads(db.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])
            db.execute('UPDATE settings SET data=? WHERE id=1', (json.dumps({**data, **values}),))

    def dialogue(self, uid):
        with self.db() as db:
            row = db.execute('SELECT data FROM dialogue_preferences WHERE user_id=?', (uid,)).fetchone()
        return {**DEFAULT_DIALOGUE, **(json.loads(row[0]) if row else {})}

    def set_dialogue(self, uid, values):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM dialogue_preferences WHERE user_id=?', (uid,)).fetchone()
            old = {**DEFAULT_DIALOGUE, **(json.loads(row[0]) if row else {})}
            if any(key in values and values[key] != old[key] for key in ('routing_mode', 'wake_words')):
                values = {**values, 'routing_updated': time.time()}
            db.execute('INSERT INTO dialogue_preferences VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data',
                       (uid, json.dumps({**old, **values})))

    def fallback_rules(self):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM fallback_rules ORDER BY rowid')]

    def invite(self, uid=None):
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            if uid:
                db.execute('DELETE FROM invites WHERE kind=? AND user_id=?', ('reset', uid))
            db.execute('INSERT INTO invites VALUES(?,?,?,?,0)',
                       (digest(token), 'reset' if uid else 'invite', uid, time.time() + 86400))
        return token

    def redeem(self, token, username, password):
        hashed = password_hash(password)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM invites WHERE token=? AND used=0 AND expires>?',
                             (digest(token), time.time())).fetchone()
            if not row:
                raise Rejected('链接已失效或已使用')
            if row['kind'] == 'reset':
                uid = row['user_id']
                owner = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
                if not owner or owner[0] != username:
                    raise Rejected('平台账号与重置链接不匹配')
                db.execute('UPDATE users SET password=? WHERE id=?', (hashed, uid))
                db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
            else:
                uid = secrets.token_hex(16)
                db.execute('INSERT INTO users(id,username,password,created) VALUES(?,?,?,?)',
                           (uid, username, hashed, time.time()))
            db.execute('UPDATE invites SET used=1 WHERE token=?', (digest(token),))
        return uid

    def account(self, uid):
        with self.db() as db:
            row = db.execute('SELECT * FROM accounts WHERE user_id=?', (uid,)).fetchone()
        if not row:
            return None
        row = dict(row)
        row['credentials'] = self.vault.open(row.pop('secret'), 'account:' + uid)
        return row

    def save_account(self, uid, username, password, token, state='ready'):
        # Never expose Xiaomi username, even in metadata tables.
        identity = digest(str(token.get('userId') or username).strip().lower())
        encrypted = self.vault.seal({'username': username, 'password': password, 'token': token},
                                    'account:' + uid)
        with self.db() as db:
            old = db.execute('SELECT identity FROM accounts WHERE user_id=?', (uid,)).fetchone()
            if old and old[0] != identity:
                raise Rejected('请先解绑原小米账号，再绑定新账号')
            try:
                db.execute('INSERT INTO accounts VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET '
                           'identity=excluded.identity,secret=excluded.secret,state=excluded.state,updated=excluded.updated',
                           (uid, identity, encrypted, state, time.time()))
            except sqlite3.IntegrityError:
                raise Rejected('该小米账号已绑定其他用户的平台账号。如曾注册，请找回原平台账号或联系管理员', 409) from None

    def set_account_state(self, uid, state):
        with self.db() as db:
            db.execute('UPDATE accounts SET state=?,updated=? WHERE user_id=?', (state, time.time(), uid))

    def devices(self, uid):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM devices WHERE user_id=? ORDER BY name', (uid,))]

    def polling_candidates(self, uid=None, did=None):
        query = ("SELECT d.user_id,d.id,d.hardware FROM devices d JOIN users u ON u.id=d.user_id "
                 "JOIN accounts a ON a.user_id=d.user_id WHERE u.active=1 AND a.state='ready' "
                 "AND d.present=1 AND d.supported=1 AND d.enabled=1")
        args = []
        if uid is not None:
            query += ' AND d.user_id=?'
            args.append(uid)
        if did is not None:
            query += ' AND d.id=?'
            args.append(did)
        with self.db() as db:
            return [dict(r) for r in db.execute(query, args)]

    def assert_available(self, uid, token):
        if not token.get('userId'):
            raise Rejected('小米账号身份信息不完整，请重新绑定')
        identity = digest(str(token['userId']).strip().lower())
        with self.db() as db:
            other = db.execute('SELECT user_id FROM accounts WHERE identity=?', (identity,)).fetchone()
        if other and other[0] != uid:
            raise Rejected('该小米账号已绑定其他用户的平台账号。如曾注册，请找回原平台账号或联系管理员', 409)
        return identity

    def install_account(self, uid, credentials, rows, expected_identity, replace=False):
        """Compare-and-swap binding and device selection in one SQLite transaction."""
        token = credentials['token']
        identity = self.assert_available(uid, token)
        encrypted = self.vault.seal(credentials, 'account:' + uid)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT identity FROM accounts WHERE user_id=?', (uid,)).fetchone()
            if (current[0] if current else None) != expected_identity:
                raise Rejected('当前绑定已变化，请重新开始', 409)
            if not replace and expected_identity and identity != expected_identity:
                raise Rejected('验证身份与当前绑定不一致，原账号保持不变', 409)
            try:
                db.execute('INSERT INTO accounts VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET '
                           'identity=excluded.identity,secret=excluded.secret,state=excluded.state,updated=excluded.updated',
                           (uid, identity, encrypted, 'ready', time.time()))
            except sqlite3.IntegrityError:
                raise Rejected('该小米账号已绑定其他用户的平台账号。如曾注册，请找回原平台账号或联系管理员', 409) from None
            if replace:
                db.execute('DELETE FROM devices WHERE user_id=?', (uid,))
                db.execute('DELETE FROM device_claims WHERE user_id=?', (uid,))
            self._sync_devices(db, uid, rows)

    def _sync_devices(self, db, uid, rows):
        placeholders = ','.join('?' for _ in rows)
        db.execute('UPDATE devices SET present=0,enabled=0 WHERE user_id=? AND id NOT IN ('
                   + placeholders + ')', (uid, *(r['id'] for r in rows)))
        for source in rows:
            r = dict(source)
            if native_ai(r['hardware']):
                r.update(supported=False, reason=NATIVE_AI_REASON)
            keys = ['mina:' + r['id']]
            if r.get('physical_id'):
                keys.append('miot:' + r['physical_id'])
            conflict = any(db.execute('SELECT 1 FROM device_claims WHERE device_key=? AND user_id!=?',
                                      (key, uid)).fetchone() for key in keys)
            if conflict:
                r.update(supported=False, reason='该音箱已绑定其他平台账号，请先解除原绑定或联系管理员')
            else:
                for key in keys:
                    db.execute('INSERT OR IGNORE INTO device_claims VALUES(?,?,?)', (key, uid, r['id']))
            db.execute("""INSERT INTO devices(user_id,id,name,hardware,supported,reason,present)
                VALUES(?,?,?,?,?,?,1) ON CONFLICT(user_id,id) DO UPDATE SET
                name=excluded.name,hardware=excluded.hardware,supported=excluded.supported,
                reason=excluded.reason,present=1,enabled=CASE WHEN excluded.supported=0
                THEN 0 ELSE devices.enabled END""",
                       (uid, r['id'], r['name'], r['hardware'], int(r['supported']), r['reason']))

    def sync_devices(self, uid, rows):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._sync_devices(db, uid, rows)

    def select_devices(self, uid, selected):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            owned = {r['id'] for r in db.execute(
                "SELECT id,hardware FROM devices WHERE user_id=? AND present=1 AND supported=1 AND EXISTS (SELECT 1 FROM device_claims c WHERE c.device_key='mina:'||devices.id AND c.user_id=devices.user_id)", (uid,)) if not native_ai(r['hardware'])}
            if not set(selected) <= owned:
                raise Rejected('设备不属于当前账号、已消失或尚不支持', 403)
            db.execute('UPDATE devices SET enabled=0 WHERE user_id=?', (uid,))
            for did in selected:
                db.execute('UPDATE devices SET enabled=1 WHERE user_id=? AND id=?', (uid, did))

    def reserve(self, uid, did, source, request_id, now=None):
        now = time.time() if now is None else now
        day = datetime.fromtimestamp(now, CST).date().isoformat()
        policy = self.policy(uid)
        rid = secrets.token_hex(16)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            u = db.execute('SELECT active,role FROM users WHERE id=?', (uid,)).fetchone()
            if not u or not u[0]:
                raise Rejected('服务已暂停', 403)
            if source == 'model_test':
                if u['role'] != 'admin':
                    raise Rejected('仅管理员可测试模型服务', 403)
                if db.execute("SELECT 1 FROM requests WHERE user_id=? AND source='model_test' AND status='reserved'",
                              (uid,)).fetchone():
                    raise Rejected('已有连接测试正在处理，请等待完成', 429)
                if db.execute("SELECT 1 FROM requests WHERE source='model_test' AND device_id=? AND created>?",
                              (did, now - 60)).fetchone():
                    raise Rejected('同一服务每60秒可测试一次，请稍后再试', 429)
                if db.execute("SELECT COUNT(*) FROM requests WHERE source='model_test' AND created>?",
                              (now - 3600,)).fetchone()[0] >= 20:
                    raise Rejected('全平台每小时最多测试20次，请稍后再试', 429)
            if db.execute('SELECT 1 FROM requests WHERE user_id=? AND source=? AND request_id=?',
                          (uid, source, request_id)).fetchone():
                raise Rejected('该请求已处理', 409)
            count = db.execute("SELECT COUNT(*) FROM requests WHERE user_id=? AND day=? "
                               "AND (started=1 OR status='reserved')", (uid, day)).fetchone()[0]
            recent = db.execute("SELECT COUNT(*) FROM requests WHERE user_id=? AND created>? "
                                "AND (started=1 OR status='reserved')", (uid, now - 60)).fetchone()[0]
            if count >= policy['daily_limit']:
                raise Rejected('今日提问次数已用完，北京时间零点恢复', 429)
            if recent >= policy['minute_limit']:
                raise Rejected('提问太快，请稍后再试', 429)
            db.execute('INSERT INTO requests(id,user_id,device_id,source,request_id,created,day,status) '
                       "VALUES(?,?,?,?,?,?,?,'reserved')", (rid, uid, did, source, request_id, now, day))
        return rid

    def started(self, rid):
        with self.db() as db:
            db.execute('UPDATE requests SET started=1 WHERE id=?', (rid,))

    def finish(self, rid, usage, **metadata):
        usage = usage or {}
        keys = ('model', 'elapsed_ms', 'finish_reason', 'speech_status', 'status')
        updates = {k: v for k, v in metadata.items() if k in keys}
        for field in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            updates[field] = usage.get(field)
        details = usage.get('prompt_tokens_details') or {}
        updates['cache_hit_tokens'] = usage.get('prompt_cache_hit_tokens', details.get('cached_tokens'))
        updates['cache_miss_tokens'] = usage.get('prompt_cache_miss_tokens')
        with self.db() as db:
            db.execute('UPDATE requests SET ' + ','.join(k + '=?' for k in updates) + ' WHERE id=?',
                       (*updates.values(), rid))

    def update_request(self, rid, **updates):
        assert set(updates) <= {'status', 'speech_status'}
        with self.db() as db:
            db.execute('UPDATE requests SET ' + ','.join(k + '=?' for k in updates) + ' WHERE id=?',
                       (*updates.values(), rid))

    def save_history(self, uid, did, rid, question, answer, *, account_label=None, device_name=None):
        content = self.vault.seal({'question': question, 'answer': answer, 'account_label': account_label,
                                   'device_name': device_name}, 'history:' + uid + ':' + rid)
        with self.history_db() as db:
            db.execute('INSERT OR REPLACE INTO history VALUES(?,?,?,?,?)', (rid, uid, did, time.time(), content))

    def history(self, uid, before=None, limit=30, *, after=None, after_id='', before_id=None):
        cutoff = time.time() - self.defaults()['retention_days'] * 86400
        conditions = ['user_id=?', 'created>?']
        args = [uid, cutoff]
        if after is not None:
            conditions.append('(created>? OR (created=? AND id>?))')
            args.extend((after, after, after_id))
        if before is not None:
            if before_id is None:
                conditions.append('created<?')
                args.append(before)
            else:
                conditions.append('(created<? OR (created=? AND id<?))')
                args.extend((before, before, before_id))
        order = 'ASC' if after is not None else 'DESC'
        query = ('SELECT * FROM history WHERE ' + ' AND '.join(conditions)
                 + f' ORDER BY created {order},id {order} LIMIT ?')
        with self.history_db() as db:
            rows = db.execute(query, (*args, limit)).fetchall()
        result = []
        for row in rows:
            row = dict(row)
            row.update(self.vault.open(row.pop('content'), 'history:' + uid + ':' + row['id']))
            result.append(row)
        return result

    def delete_history(self, uid, rid=None):
        with self.history_db() as db:
            db.execute('DELETE FROM history WHERE user_id=?' + (' AND id=?' if rid else ''),
                       (uid, rid) if rid else (uid,))

    def cleanup(self, now=None):
        cutoff = (time.time() if now is None else now) - self.defaults()['retention_days'] * 86400
        total = 0
        while True:
            with self.history_db() as db:
                n = db.execute('DELETE FROM history WHERE id IN (SELECT id FROM history WHERE created<? '
                               'ORDER BY created LIMIT 500)', (cutoff,)).rowcount
            total += n
            if n < 500:
                break
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
            db.execute('DELETE FROM invites WHERE expires<? OR used=1', (time.time(),))
            db.execute('DELETE FROM auth_challenges WHERE expires<? OR used=1', (time.time(),))
            db.execute('DELETE FROM auth_limits WHERE start<?', (time.time() - 86400,))
            db.execute('DELETE FROM trusted_browsers WHERE expires<?', (time.time(),))
            db.execute('DELETE FROM mail_outbox WHERE attempts>=5 AND next_attempt<?', (time.time() - 86400,))
        return total

    def summary(self, uid=None, days=7):
        day = datetime.now(CST).date().isoformat()
        args = [uid] if uid else []
        where = ' WHERE user_id=?' if uid else ''
        conjunction = ' AND ' if where else ' WHERE '
        totals_sql = (
            'SELECT COUNT(*) requests,COALESCE(SUM(total_tokens),0) total_tokens,'
            'COALESCE(SUM(prompt_tokens),0) prompt_tokens,COALESCE(SUM(completion_tokens),0) completion_tokens,'
            'COALESCE(SUM(cache_hit_tokens),0) cache_hit_tokens,COALESCE(SUM(cache_miss_tokens),0) cache_miss_tokens,'
            'SUM(CASE WHEN total_tokens IS NULL AND started=1 THEN 1 ELSE 0 END) unknown_usage,'
            "SUM(CASE WHEN status IN ('failed','interrupted') THEN 1 ELSE 0 END) failures,"
            'AVG(elapsed_ms) elapsed_ms FROM requests'
        )
        daily_sql = (
            'SELECT day, COUNT(*) requests, SUM(total_tokens) total_tokens, '
            'SUM(prompt_tokens) prompt_tokens,SUM(completion_tokens) completion_tokens '
            'FROM requests'
        )
        with self.db() as db:
            requests = [dict(r) for r in db.execute(
                'SELECT * FROM requests' + where + ' ORDER BY created DESC LIMIT 100', args)]
            totals = dict(db.execute(totals_sql + where, args).fetchone())
            today = db.execute(
                'SELECT COUNT(*) FROM requests' + where + conjunction
                + "day=? AND (started=1 OR status='reserved')", (*args, day)).fetchone()[0]
            daily = [dict(r) for r in db.execute(
                daily_sql + where + conjunction + 'created>? GROUP BY day ORDER BY day',
                (*args, time.time() - days * 86400))]
        return {'totals': totals, 'today': today, 'daily': daily, 'requests': requests}
