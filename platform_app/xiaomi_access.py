"""Persistent account-wide Xiaomi rate-limit pauses; never store provider payloads."""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import secrets
import time
from urllib.parse import urlsplit

from .store import Rejected

BACKOFF = (1800, 3600, 7200)
LEASE_SECONDS = 90


def retry_delay(value, now):
    """Retry-After accepts seconds or an HTTP date. Invalid values use our fallback."""
    if not isinstance(value, str):
        return 0
    value = value.strip()
    if value.isascii() and value.isdigit() and len(value) <= 12:
        return int(value)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0, date.timestamp() - now)
    except (ValueError, TypeError, OverflowError):
        return 0


class RateLimited(Rejected):
    def __init__(self, status):
        self.cooldown = status
        wait = status['retry_after']
        message = (f'小米请求受限，相关访问已暂停；还需等待约{math.ceil(wait / 60)}分钟。' if wait else
                   '小米请求仍处于保护暂停，请主动检查一次后再操作。')
        if status['manual_required']:
            message += '连续受限后已停止自动检查。'
        super().__init__(message + '原账号和设备选择保留，无需因此重新输入密码。', 429)


class XiaomiAccess:
    def __init__(self, store):
        self.store = store
        self.permit = ContextVar('xiaomi_recovery_permit', default=None)

    def row(self, uid):
        with self.store.db() as db:
            row = db.execute('SELECT * FROM xiaomi_cooldowns WHERE user_id=?', (uid,)).fetchone()
        return dict(row) if row else None

    def status(self, uid):
        row = self.row(uid)
        if not row or not (row['until'] or row['manual_required'] or row['lease']):
            return None
        return self.public_status(row)

    @staticmethod
    def public_status(row):
        now = time.time()
        wait = max(0, row['until'] - now, row['lease_until'] - now if row['lease'] else 0)
        return {'state': 'rate_limited', 'retry_after': math.ceil(wait), 'retry_at': max(row['until'], row['lease_until']),
                'manual_required': bool(row['manual_required']), 'recovering': bool(row['lease'] and row['lease_until'] > now),
                'failures': row['failures'], 'http_status': row['http_status'], 'upstream_code': row['upstream_code'],
                'can_reverify': False, 'message': '小米请求受限，已暂停相关访问，原绑定保留。'}

    def ensure(self, uid):
        row = self.row(uid)
        if not row or not (row['until'] or row['manual_required'] or row['lease']):
            return
        permit = self.permit.get()
        if (permit and permit['uid'] == uid and permit['lease'] == row['lease'] and row['lease']
                and row['lease_until'] > time.time() and row['until'] <= time.time()):
            return
        raise RateLimited(self.public_status(row))

    def limited(self, uid, retry_after=None, code=None):
        now = time.time()
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM xiaomi_cooldowns WHERE user_id=?', (uid,)).fetchone()
            # Already in-flight responses from the same burst must not count as new recovery attempts.
            burst = old and old['until'] > now and not old['lease']
            failures = old['failures'] if burst else (old['failures'] if old else 0) + 1
            delay = max(BACKOFF[min(failures - 1, 2)], retry_delay(retry_after, now))
            until = max(old['until'] if old else 0, now + delay)
            safe_code = code if type(code) is int and abs(code) <= 999999999 else None
            db.execute('INSERT INTO xiaomi_cooldowns VALUES(?,?,?,?,?,?,?,?,?,?) '
                       'ON CONFLICT(user_id) DO UPDATE SET failures=excluded.failures,until=excluded.until,'
                       'manual_required=excluded.manual_required,lease=excluded.lease,lease_until=excluded.lease_until,'
                       'revision=excluded.revision,http_status=excluded.http_status,upstream_code=excluded.upstream_code,'
                       'updated=excluded.updated',
                       (uid, failures, until, int(failures >= 3), '', 0, secrets.token_hex(16), 429, safe_code, now))
        return self.status(uid)

    def enter_probe(self, uid, manual=False, lease_seconds=LEASE_SECONDS):
        """One atomic recovery lease; manual checking cannot bypass an unexpired pause."""
        now = time.time()
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM xiaomi_cooldowns WHERE user_id=?', (uid,)).fetchone()
            paused = row and (row['until'] or row['manual_required'] or row['lease'])
            if paused:
                if (row['until'] > now or row['lease_until'] > now or (row['manual_required'] and not manual)):
                    raise RateLimited(self.public_status(row))
                lease = secrets.token_hex(16)
                db.execute('UPDATE xiaomi_cooldowns SET lease=?,lease_until=? WHERE user_id=?',
                           (lease, now + lease_seconds, uid))
            else:
                lease = None
            return {'uid': uid, 'revision': row['revision'] if row else None, 'lease': lease}

    def finish_probe(self, permit, result):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM xiaomi_cooldowns WHERE user_id=?', (permit['uid'],)).fetchone()
            if not row or row['revision'] != permit['revision'] or row['lease'] != (permit['lease'] or ''):
                return  # A concurrent 429 always wins over an older successful check.
            if result == 'valid':
                db.execute('UPDATE xiaomi_cooldowns SET failures=0,until=0,manual_required=0,lease=\'\','
                           'lease_until=?,revision=?,updated=? WHERE user_id=?',
                           (time.time(), secrets.token_hex(16), time.time(), permit['uid']))
            elif result == 'expired':
                # An explicit authorization failure needs user repair, not a rate-limit label.
                # Retain the failure count until a full successful check to prevent rebind resets.
                db.execute('UPDATE xiaomi_cooldowns SET until=0,manual_required=0,lease=\'\',lease_until=0 '
                           'WHERE user_id=?', (permit['uid'],))
            elif permit['lease']:
                db.execute('UPDATE xiaomi_cooldowns SET until=MAX(until,?),lease=\'\',lease_until=0 WHERE user_id=?',
                           (time.time() + 300, permit['uid']))

    async def pace(self, uid):
        # For two minutes after recovery, spread requests across all devices/entry points.
        now = time.time()
        wait = 0
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM xiaomi_cooldowns WHERE user_id=?', (uid,)).fetchone()
            if row and not row['until'] and not row['manual_required'] and not row['lease'] and not row['failures']:
                if now >= row['updated'] + 120:
                    db.execute('DELETE FROM xiaomi_cooldowns WHERE user_id=?', (uid,))
                else:
                    slot = max(now, row['lease_until'])
                    wait = max(0, slot - now)
                    db.execute('UPDATE xiaomi_cooldowns SET lease_until=? WHERE user_id=?', (slot + 1, uid))
        if wait:
            await asyncio.sleep(wait)
        self.ensure(uid)

    @asynccontextmanager
    async def probe(self, uid, manual=False, lease_seconds=LEASE_SECONDS):
        permit = self.enter_probe(uid, manual, lease_seconds)
        token = self.permit.set(permit)
        try:
            yield permit
        finally:
            self.finish_probe(permit, 'unknown')
            self.permit.reset(token)


class XiaomiSession:
    """Guard the dependency and direct cloud reads through the same persistent pause."""
    def __init__(self, session, access, uid):
        self.session, self.access, self.uid = session, access, uid

    def __getattr__(self, name):
        return getattr(self.session, name)

    def get(self, url, **kwargs):
        return self._request('GET', url, **kwargs)

    def post(self, url, **kwargs):
        return self._request('POST', url, **kwargs)

    def request(self, method, url, **kwargs):
        return self._request(method, url, **kwargs)

    @asynccontextmanager
    async def _request(self, method, url, **kwargs):
        host = (urlsplit(str(url)).hostname or '').lower()
        xiaomi = host.endswith(('.mi.com', '.xiaomi.com')) or host in ('mi.com', 'xiaomi.com')
        if xiaomi:
            self.access.ensure(self.uid)
            await self.access.pace(self.uid)
        request = getattr(self.session, method.lower(), None)
        pending = request(url, **kwargs) if request else self.session.request(method, url, **kwargs)
        async with pending as response:
            if xiaomi and response.status == 429:
                retry_after = getattr(response, 'headers', {}).get('Retry-After')
                status = self.access.limited(self.uid, retry_after)
                # Pause on headers even if the error body is malformed, slow or cancelled.
                try:
                    body = await response.json(content_type=None)
                    code = body.get('code') if isinstance(body, dict) else None
                    if type(code) is int and abs(code) <= 999999999:
                        with self.access.store.db() as db:
                            db.execute('UPDATE xiaomi_cooldowns SET upstream_code=? WHERE user_id=?', (code, self.uid))
                        status = self.access.status(self.uid)
                except Exception:
                    pass
                raise RateLimited(status)
            yield response
