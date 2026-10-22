"""Encrypted provider registry and bounded failover; raw upstream errors stay private."""
import asyncio
import json
import secrets
import time

import httpx

import llm_router
from .store import Rejected

PROVIDERS = {
    'deepseek': {'label': 'DeepSeek', 'base_url': 'https://api.deepseek.com', 'prefix': 'deepseek'},
    'qwen': {'label': '千问（北京）', 'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1', 'prefix': 'openai'},
    'qwen_intl': {'label': '千问（新加坡）', 'base_url': 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1', 'prefix': 'openai'},
    'openai': {'label': 'OpenAI', 'base_url': 'https://api.openai.com/v1', 'prefix': 'openai'},
}

TEST_MESSAGES = [{'role': 'user', 'content': '这是一次连接测试，请只回复：连接成功。'}]
TEST_RESULTS = {
    'success': '连接测试成功，指定服务已返回有效文字。',
    'queued': '正在等待测试名额。', 'started': '正在测试指定服务。',
    'authentication': '鉴权失败：请检查 Key、所选地区及模型访问权限。',
    'balance': '供应商报告余额或账单不足，请到供应商控制台检查。',
    'model_not_found': '供应商未找到该模型或接口，请检查模型名称及所选地区。',
    'rate_limit': '供应商限流或额度不足：请检查供应商控制台后稍后重试。',
    'unavailable': '供应商服务暂时异常，请稍后重试。',
    'connection': '未能连接供应商，请检查服务器网络。',
    'timeout': '测试超时；未重试或切换备用，供应商可能已产生消耗。',
    'content_rejected': '供应商拒绝了测试请求，请检查模型服务设置。',
    'request_failed': '供应商拒绝或未完成请求，请检查模型名称、权限及供应商控制台。',
    'invalid_response': '服务未返回有效文字，请检查模型是否适用于文字回答。',
    'interrupted': '测试已中断，未切换备用；已发出请求的用量可能未知。',
    'configuration_changed': '配置已变化或已删除，本次未调用模型，请重新测试。',
    'not_started': '测试未获得处理名额，本次未调用模型，请稍后再试。',
}


def failure_kind(exc):
    if isinstance(exc, asyncio.CancelledError):
        return 'interrupted', False
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)) or 'timeout' in type(exc).__name__.lower():
        return 'timeout', False
    if 'contentpolicy' in type(exc).__name__.lower() or 'safety' in type(exc).__name__.lower():
        return 'content_rejected', False
    status = getattr(exc, 'status_code', None)
    if status in (401, 403):
        return 'authentication', True
    if status == 429:
        return 'rate_limit', True
    if status in (500, 502, 503):
        return 'unavailable', True
    cause = exc
    for _ in range(4):
        if isinstance(cause, (httpx.ConnectError, ConnectionRefusedError)):
            return 'connection', True
        cause = getattr(cause, '__cause__', None)
        if cause is None:
            break
    return 'request_failed', False


class ModelServices:
    def __init__(self, store, cfg):
        self.store, self.cfg = store, cfg
        self.health = {}
        with store.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS model_services (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                secret BLOB NOT NULL, key_hint TEXT NOT NULL, enabled INTEGER NOT NULL,
                priority INTEGER NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS model_attempts (
                request_id TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
                attempt INTEGER NOT NULL, service_id TEXT NOT NULL, name TEXT NOT NULL,
                provider TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
                error_kind TEXT, usage TEXT, created REAL NOT NULL, finished REAL,
                PRIMARY KEY(request_id,attempt));
            CREATE INDEX IF NOT EXISTS model_attempt_time ON model_attempts(created);
            CREATE TABLE IF NOT EXISTS model_tests (
                request_id TEXT PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
                service_id TEXT NOT NULL, service_version REAL NOT NULL,
                result TEXT NOT NULL, created REAL NOT NULL, finished REAL, elapsed_ms INTEGER);
            CREATE INDEX IF NOT EXISTS model_test_version ON model_tests(service_id,service_version,created);
            CREATE INDEX IF NOT EXISTS model_test_rate ON requests(source,device_id,created);
            ''')
        self.bootstrap()

    def managed(self):
        with self.store.db() as db:
            data = json.loads(db.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])
        return bool(data.get('model_services_managed'))

    def bootstrap(self):
        if self.managed():
            return
        llm = self.cfg.get('llm', {})
        provider = llm.get('default_provider', 'deepseek')
        conf = llm.get(provider, {})
        if provider not in PROVIDERS or not conf.get('api_key') or not conf.get('model'):
            return
        base = str(conf.get('base_url') or PROVIDERS[provider]['base_url']).rstrip('/')
        if base not in (PROVIDERS[provider]['base_url'], PROVIDERS[provider]['base_url'] + '/v1'):
            return
        self.save(None, {'name': PROVIDERS[provider]['label'] + ' 主用（原配置）', 'provider': provider,
                         'model': conf['model'], 'api_key': conf['api_key'], 'priority': 10, 'enabled': True})

    def public(self):
        with self.store.db() as db:
            rows = [dict(r) for r in db.execute(
                'SELECT s.id,s.name,s.provider,s.model,s.key_hint,s.enabled,s.priority,s.updated,'
                't.result AS test_result,t.created AS test_created,t.finished AS test_finished,'
                't.elapsed_ms AS test_elapsed_ms,r.total_tokens AS test_tokens,r.started AS test_started,'
                "(SELECT MAX(created)+60 FROM requests WHERE source='model_test' AND device_id='model-test:'||s.id) AS next_test_at "
                'FROM model_services s LEFT JOIN model_tests t ON t.request_id=('
                'SELECT request_id FROM model_tests WHERE service_id=s.id AND service_version=s.updated '
                'ORDER BY created DESC LIMIT 1) LEFT JOIN requests r ON r.id=t.request_id ORDER BY s.priority,s.id')]
        for row in rows:
            health = self.health.get(row['id'], {})
            if health.get('version') != row['updated']:
                health = {}
            row['cooldown_seconds'] = max(0, int(health.get('until', 0) - time.monotonic()))
            row['last_result'] = health.get('result', 'untested')
            row['provider_label'] = PROVIDERS[row['provider']]['label']
            result = row.pop('test_result')
            row['last_test'] = ({'result': result, 'message': TEST_RESULTS[result],
                                 'created': row['test_created'], 'finished': row['test_finished'],
                                 'elapsed_ms': row['test_elapsed_ms'], 'total_tokens': row['test_tokens'],
                                 'started': bool(row['test_started'])} if result else None)
            for key in ('test_created', 'test_finished', 'test_elapsed_ms', 'test_tokens', 'test_started'):
                row.pop(key)
        return rows

    def save(self, ident, values):
        is_new = ident is None
        ident = ident or secrets.token_hex(16)
        values = dict(values)
        key = values.pop('api_key', None)
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM model_services WHERE id=?', (ident,)).fetchone()
            if not is_new and not old:
                raise Rejected('模型服务不存在', 404)
            if is_new and db.execute('SELECT COUNT(*) FROM model_services').fetchone()[0] >= 20:
                raise Rejected('最多配置20条模型服务，请先整理已有配置')
            if db.execute('SELECT 1 FROM model_services WHERE priority=? AND id!=?',
                          (values['priority'], ident)).fetchone():
                raise Rejected('该优先级已被其他服务使用，请设置不同的数字')
            if not key and (not old or old['provider'] != values['provider']):
                raise Rejected('新增服务或更换供应商时，请填写对应的 API Key')
            secret = self.store.vault.seal(key, 'model-service:' + ident) if key else old['secret']
            hint = '••••••••' + key[-4:] if key else old['key_hint']
            db.execute('INSERT INTO model_services VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                       'name=excluded.name,provider=excluded.provider,model=excluded.model,secret=excluded.secret,'
                       'key_hint=excluded.key_hint,enabled=excluded.enabled,priority=excluded.priority,updated=excluded.updated',
                       (ident, values['name'], values['provider'], values['model'], secret, hint,
                        int(values['enabled']), values['priority'], time.time()))
            settings = json.loads(db.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])
            settings['model_services_managed'] = True
            db.execute('UPDATE settings SET data=? WHERE id=1', (json.dumps(settings),))
        self.health.pop(ident, None)
        return ident

    def delete(self, ident):
        with self.store.db() as db:
            if not db.execute('DELETE FROM model_services WHERE id=?', (ident,)).rowcount:
                raise Rejected('模型服务不存在', 404)
        self.health.pop(ident, None)

    def recent_attempts(self):
        with self.store.db() as db:
            return [dict(r) for r in db.execute(
                'SELECT a.name,a.provider,a.model,a.status,a.error_kind,a.created,a.finished,a.attempt,r.source '
                'FROM model_attempts a JOIN requests r ON r.id=a.request_id ORDER BY a.created DESC LIMIT 20')]

    def test_snapshot(self, ident):
        with self.store.db() as db:
            row = db.execute('SELECT id,updated FROM model_services WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise Rejected('模型服务不存在', 404)
        return dict(row)

    def begin_test(self, rid, snapshot):
        with self.store.db() as db:
            db.execute('INSERT INTO model_tests(request_id,service_id,service_version,result,created) '
                       "VALUES(?,?,?,'queued',?)", (rid, snapshot['id'], snapshot['updated'], time.time()))

    def finish_test(self, rid, result, elapsed_ms):
        with self.store.db() as db:
            db.execute('UPDATE model_tests SET result=?,finished=?,elapsed_ms=? WHERE request_id=?',
                       (result, time.time(), elapsed_ms, rid))

    async def test_connection(self, rid, snapshot, max_tokens):
        """One call to a saved revision, including disabled/cooling services.

        Diagnostics neither fall back nor change production health. No generated
        text, key, or upstream error body escapes this method.
        """
        with self.store.db() as db:
            row = db.execute('SELECT * FROM model_services WHERE id=? AND updated=?',
                             (snapshot['id'], snapshot['updated'])).fetchone()
        if row is None:
            return 'configuration_changed', None, None
        key = self.store.vault.open(row['secret'], 'model-service:' + row['id'])
        provider = PROVIDERS[row['provider']]
        target = provider['prefix'] + '/' + row['model']
        self.store.started(rid)
        with self.store.db() as db:
            db.execute("UPDATE model_tests SET result='started' WHERE request_id=?", (rid,))
            db.execute('INSERT INTO model_attempts(request_id,attempt,service_id,name,provider,model,status,created) '
                       "VALUES(?,1,?,?,?,?,'started',?)", (rid, row['id'], row['name'], row['provider'], target, time.time()))
        usage = None
        try:
            async with asyncio.timeout(15):
                response = await llm_router.acompletion(
                    model=target, messages=[dict(m) for m in TEST_MESSAGES], max_tokens=max_tokens,
                    num_retries=0, stream=False, api_key=key, api_base=provider['base_url'], timeout=15)
            raw = getattr(response, 'usage', None)
            usage = raw.model_dump() if hasattr(raw, 'model_dump') else raw
            content = response.choices[0].message.content if response.choices else None
            result = 'success' if isinstance(content, str) and content.strip() else 'invalid_response'
            if asyncio.current_task().cancelling():
                self.finish_attempt(rid, 1, 'interrupted', 'interrupted', usage)
                return 'interrupted', usage, target
        except BaseException as exc:
            result = 'interrupted' if asyncio.current_task().cancelling() else failure_kind(exc)[0]
            if result != 'interrupted':
                result = {402: 'balance', 404: 'model_not_found'}.get(getattr(exc, 'status_code', None), result)
            self.finish_attempt(rid, 1, 'interrupted' if result == 'interrupted' else 'failed', result, usage)
            if result == 'interrupted':
                raise asyncio.CancelledError from None
            return result, usage, target
        self.finish_attempt(rid, 1, 'completed' if result == 'success' else 'failed',
                            None if result == 'success' else result, usage)
        return result, usage, target

    def finish_attempt(self, rid, number, status, kind=None, usage=None):
        with self.store.db() as db:
            db.execute('UPDATE model_attempts SET status=?,error_kind=?,usage=?,finished=? WHERE request_id=? AND attempt=?',
                       (status, kind, json.dumps(usage) if usage is not None else None, time.time(), rid, number))

    async def complete(self, rid, cfg, messages, max_tokens):
        if not self.managed():
            # Preserve legacy local/custom setups until the first web-managed service is saved.
            target, extra = llm_router._litellm_target(cfg)
            self.store.started(rid)
            response = await llm_router.acompletion(
                model=target, messages=messages, max_tokens=max_tokens, num_retries=0, stream=False, **extra)
            raw = getattr(response, 'usage', None)
            return response, raw.model_dump() if hasattr(raw, 'model_dump') else raw, target
        candidates = []
        for row in self.public():
            health = self.health.get(row['id'], {})
            cooling = health.get('version') == row['updated'] and health.get('until', 0) > time.monotonic()
            if row['enabled'] and not cooling:
                candidates.append(row)
        if not candidates:
            raise Rejected('没有可用的模型服务，请联系管理员检查主备配置或等待冷却结束', 503)
        failed = False
        attempted = 0
        deadline = time.monotonic() + float(cfg.get('llm', {}).get('timeout_seconds', 60))
        for row in candidates:
            # A cancelled device turn must never start a backup even if a
            # transport catches cancellation and translates it into an error.
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            if attempted >= 3:
                break
            # Re-read before each attempt so a disabled/deleted/edited backup is never used from an old snapshot.
            with self.store.db() as db:
                current = db.execute('SELECT * FROM model_services WHERE id=? AND enabled=1 AND updated=?',
                                     (row['id'], row['updated'])).fetchone()
            if current is None:
                continue
            attempted += 1
            key = self.store.vault.open(current['secret'], 'model-service:' + row['id'])
            provider = PROVIDERS[row['provider']]
            target = provider['prefix'] + '/' + row['model']
            self.store.started(rid)
            with self.store.db() as db:
                db.execute('INSERT INTO model_attempts(request_id,attempt,service_id,name,provider,model,status,created) '
                           "VALUES(?,?,?,?,?,?,'started',?)", (rid, attempted, row['id'], row['name'], row['provider'], target, time.time()))
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(remaining):
                    response = await llm_router.acompletion(
                        model=target, messages=messages, max_tokens=max_tokens, num_retries=0, stream=False,
                        api_key=key, api_base=provider['base_url'], timeout=remaining)
                raw = getattr(response, 'usage', None)
                usage = raw.model_dump() if hasattr(raw, 'model_dump') else raw
            except BaseException as exc:
                if asyncio.current_task().cancelling():
                    self.finish_attempt(rid, attempted, 'interrupted', 'interrupted')
                    raise asyncio.CancelledError from None
                kind, retryable = failure_kind(exc)
                self.finish_attempt(rid, attempted, 'interrupted' if kind == 'interrupted' else 'failed', kind)
                if kind == 'interrupted':
                    raise
                self.health[row['id']] = {'version': row['updated'], 'result': kind,
                                          'until': time.monotonic() + (300 if kind == 'authentication' else 30)}
                if not retryable:
                    raise
                failed = True
                continue
            valid = bool(response.choices and (response.choices[0].message.content or '').strip())
            self.finish_attempt(rid, attempted, 'completed' if valid else 'failed',
                                None if valid else 'invalid_response', usage=usage)
            self.health[row['id']] = {'version': row['updated'], 'result': 'completed' if valid else 'invalid_response',
                                      'until': 0 if valid else time.monotonic() + 30}
            if failed and usage is not None:
                # Earlier failures did not report usage; never present the final success as the full bill.
                usage = {**usage, 'total_tokens': None}
            return response, usage, target
        if attempted == 0:
            raise Rejected('模型服务配置已变化，请重新提问', 503)
        raise RuntimeError('All selected model services failed')
