"""Same-origin web application. No Xiaomi secrets or transcript APIs for admins."""
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date
import fcntl
import asyncio
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit
from typing import Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError
import yaml

from .auth import Auth
from .billing import Billing
from .feedback import Feedback
from .routing import failure_match, normalized
from .runtime import Runtime
from .security import Vault, digest, masked_account
from .store import Rejected, Store
from .usage_ranking import ranking


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Login(Input):
    username: str = Field(min_length=3, max_length=32, pattern=r'^[A-Za-z0-9_.-]+$')
    password: str = Field(min_length=1, max_length=256)


class Redeem(Login):
    token: str = Field(min_length=20, max_length=128)


class Binding(Input):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class Reverify(Input):
    password: str = Field(min_length=1, max_length=256)


class ConfirmSwitch(Input):
    job_id: str = Field(min_length=32, max_length=32)


class Code(Input):
    job_id: str = Field(min_length=32, max_length=32)
    code: str = Field(min_length=4, max_length=12, pattern=r'^\d+$')


class Selection(Input):
    devices: list[str] = Field(max_length=50)


class Dialogue(Input):
    routing_mode: Literal['fallback', 'keyword'] | None = None
    answer_prefix: str | None = Field(default=None, max_length=12)
    wake_words: list[str] | None = Field(default=None, min_length=1, max_length=5)

    @field_validator('answer_prefix', mode='before')
    @classmethod
    def clean_prefix(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError('前缀不允许换行或控制字符')
        return value

    @field_validator('wake_words')
    @classmethod
    def words(cls, values):
        if values is None:
            return values
        values = list(dict.fromkeys(v.strip() for v in values))
        if any(not 2 <= len(v) <= 20 or any(ord(c) < 32 for c in v) for v in values):
            raise ValueError('每个触发词须为2至20字')
        return values


RULE_ERRORS = {
    'rule_exact_length': '完整回复须为4至200个字符（忽略标点和空格）',
    'rule_phrase_count': '组合短语请每行填写一条，最多8条，至少填写1条',
    'rule_phrase_length': '每行短语须为2至60个字符（忽略标点和空格）',
    'rule_phrase_broad': '仅填写一条短语时，至少4个字符，且不能使用过于宽泛的词；可改为多条组合短语',
    'rule_empty': '规则名称和回复或短语不能为空',
}


class FailureRule(Input):
    name: str = Field(min_length=1, max_length=60)
    match_type: Literal['exact', 'phrases']
    content: str = Field(min_length=1, max_length=500)
    enabled: bool = True

    @field_validator('name', 'content')
    @classmethod
    def trim(cls, value):
        value = value.strip()
        if not value:
            raise PydanticCustomError('rule_empty', RULE_ERRORS['rule_empty'])
        return value

    @field_validator('content')
    @classmethod
    def check_content(cls, value, info):
        if info.data.get('match_type') == 'exact':
            if not 4 <= len(normalized(value)) <= 200:
                raise PydanticCustomError('rule_exact_length', RULE_ERRORS['rule_exact_length'])
        else:
            parts = [normalized(p) for p in value.splitlines() if normalized(p)]
            broad = {'暂时', '不会', '不支持', '不知道', '我不知道', '没听懂', '还在学习', '我不会'}
            if not 1 <= len(parts) <= 8:
                raise PydanticCustomError('rule_phrase_count', RULE_ERRORS['rule_phrase_count'])
            if any(not 2 <= len(p) <= 60 for p in parts):
                raise PydanticCustomError('rule_phrase_length', RULE_ERRORS['rule_phrase_length'])
            if len(parts) == 1 and (len(parts[0]) < 4 or parts[0] in broad):
                raise PydanticCustomError('rule_phrase_broad', RULE_ERRORS['rule_phrase_broad'])
        return value


class FailureSample(Input):
    answer: str = Field(min_length=1, max_length=4000)


class Policy(Input):
    daily_limit: int | None = Field(default=None, ge=0, le=100000, strict=True)
    minute_limit: int | None = Field(default=None, ge=1, le=120, strict=True)
    max_output_tokens: int | None = Field(default=None, ge=128, le=8192, strict=True)
    ttl_seconds: int | None = Field(default=None, ge=60, le=86400, strict=True)
    max_turns: int | None = Field(default=None, ge=1, le=30, strict=True)


class Defaults(Policy):
    retention_days: int | None = Field(default=None, ge=1, le=3650, strict=True)


class UserPolicy(Policy):
    active: bool | None = None
    inherit_memory: bool = False


class Capacity(Input):
    model_concurrency: int = Field(ge=1, le=128, strict=True)
    queue_limit: int = Field(ge=1, le=5000, strict=True)
    queue_timeout: int = Field(ge=1, le=60, strict=True)


class SpeechSettings(Input):
    max_chunk_chars: int = Field(ge=10, le=100, strict=True)
    chars_per_second: float = Field(ge=1, le=10, strict=True, allow_inf_nan=False)
    safety_margin_seconds: float = Field(ge=0, le=10, strict=True, allow_inf_nan=False)


class ModelService(Input):
    name: str = Field(min_length=1, max_length=60)
    provider: Literal['deepseek', 'qwen', 'qwen_intl', 'openai']
    model: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_.:-]+$')
    api_key: str | None = Field(default=None, max_length=512)
    enabled: bool = True
    priority: int = Field(ge=0, le=999, strict=True)

    @field_validator('name', 'api_key', mode='before')
    @classmethod
    def clean(cls, value):
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError('请输入文字')
        value = value.strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('不允许控制字符')
        return value

    @field_validator('api_key')
    @classmethod
    def key_length(cls, value):
        if value and (len(value) < 8 or any(c.isspace() for c in value)):
            raise ValueError('API Key 格式有误')
        return value or None


class Clear(Input):
    device_id: str = Field(min_length=1, max_length=128)


def create_app(root=None, cfg=None, excluded=(), runtime_factory=Runtime, mailer=None, payment_gateways=None):
    root = Path(root or os.environ.get('PLATFORM_STATE_DIR', 'deploy/local/platform'))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_handle = None
    key_path = Path(os.environ.get('PLATFORM_KEY_FILE', str(root.parent / 'platform.key')))
    if (root / 'platform.sqlite3').exists() and not key_path.exists():
        raise RuntimeError('已有数据库但加密密钥缺失，请恢复原密钥，不要生成替代密钥')
    vault = Vault(key_path)
    store = Store(root, vault)
    if cfg is None:
        load_dotenv(override=False)
        config_path = Path(os.environ.get('PLATFORM_ROUTER_CONFIG', 'config.yaml'))
        if not config_path.exists():
            config_path = Path('config.example.yaml')
        cfg = yaml.safe_load(config_path.read_text())
        cfg.setdefault('llm', {}).setdefault('deepseek', {})['api_key'] = os.environ.get('DEEPSEEK_API_KEY', '')
        cfg['llm'].setdefault('openai', {})['api_key'] = os.environ.get('OPENAI_API_KEY', '')
    excluded = set(excluded) | set(filter(None, os.environ.get('PLATFORM_EXCLUDED_DEVICES', '').split(',')))
    runtime = runtime_factory(store, cfg, excluded)
    rates = defaultdict(deque)
    auth = Auth(store, mailer)
    billing = Billing(store, auth.origin, payment_gateways)

    @asynccontextmanager
    async def lifespan(app):
        nonlocal lock_handle
        lock_handle = open(root / 'instance.lock', 'a')
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_handle.close()
            raise RuntimeError('该数据目录已有运行实例，请勿启动多个 worker') from None
        store.recover_requests()
        with store.db() as db:
            exists = db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone()
        if not exists:
            password = os.environ.get('PLATFORM_ADMIN_PASSWORD') or secrets.token_urlsafe(18)
            store.create_user('admin', password, 'admin')
            path = root / 'bootstrap-admin.txt'
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write('平台账号：admin（英文 admin）\n密码：' + password + '\n首次登录后请修改密码。\n')
        await runtime.start()
        mail_task = asyncio.create_task(auth.deliver_notifications())
        payment_task = asyncio.create_task(billing.background())
        try:
            yield
        finally:
            mail_task.cancel()
            payment_task.cancel()
            await asyncio.gather(mail_task, return_exceptions=True)
            await asyncio.gather(payment_task, return_exceptions=True)
            if auth.geo:
                auth.geo.close()
            await runtime.close()
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
            lock_handle.close()

    app = FastAPI(title='小爱 AI 控制台', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.runtime, app.state.auth = store, runtime, auth
    app.state.billing = billing

    @app.exception_handler(Rejected)
    async def rejected(request, exc):
        return JSONResponse({'error': str(exc)}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        # FastAPI's default validation details echo raw password/code input.
        if request.url.path.startswith('/api/admin/fallback-rules'):
            for error in exc.errors():
                if error['type'] in RULE_ERRORS:
                    return JSONResponse({'error': RULE_ERRORS[error['type']]}, status_code=422)
        return JSONResponse({'error': '输入格式有误，请检查字段长度、数值范围或必填信息'}, status_code=422)

    @app.exception_handler(Exception)
    async def internal(request, exc):
        return JSONResponse({'error': '操作暂未完成，请稍后重试'}, status_code=500)

    @app.middleware('http')
    async def boundaries(request, call_next):
        if auth.production and request.url.scheme != 'https':
            return JSONResponse({'error': '生产环境仅接受 HTTPS 请求'}, status_code=400)
        if auth.origin and request.headers.get('host') != urlsplit(auth.origin).netloc:
            return JSONResponse({'error': '请求域名不匹配'}, status_code=400)
        write = request.method not in ('GET', 'HEAD', 'OPTIONS')
        payment_notify = request.method == 'POST' and request.url.path in (
            '/api/payments/notify/alipay', '/api/payments/notify/wechat')
        if write:
            origin = request.headers.get('origin')
            expected = os.environ.get('PLATFORM_PUBLIC_ORIGIN') or str(request.base_url).rstrip('/')
            if not payment_notify and origin != expected:
                return JSONResponse({'error': '请求来源不匹配'}, status_code=403)
            try:
                size = int(request.headers.get('content-length', '0'))
            except ValueError:
                return JSONResponse({'error': '请求无效'}, status_code=400)
            if size > 32768 or size < 0:
                return JSONResponse({'error': '请求过大'}, status_code=413)
            # Require a finite body; prevents chunked uploads from bypassing this bound.
            if request.headers.get('transfer-encoding'):
                return JSONResponse({'error': '不支持流式提交'}, status_code=400)
        response = await call_next(request)
        if auth.production:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if request.url.path.startswith('/api/billing/orders/') and request.url.path.endswith('/pay') and response.status_code == 200:
            response.headers['Content-Security-Policy'] += ' https://openapi.alipay.com'
        return response

    def rate(request, action, maximum=10):
        # No X-Forwarded-For trust: only an explicitly configured trusted proxy may rewrite client IP.
        now = time.time()
        key = (request.client.host, action)
        q = rates[key]
        while q and q[0] <= now - 60:
            q.popleft()
        if len(q) >= maximum:
            raise Rejected('操作太频繁，请稍后再试', 429)
        q.append(now)
        if len(rates) > 10000:
            for k in list(rates):
                if not rates[k] or rates[k][-1] < now - 60:
                    rates.pop(k, None)

    def current(request: Request):
        token = request.cookies.get('speaker_session', '')
        with store.db() as db:
            row = db.execute('SELECT s.user_id,s.csrf,u.role,u.active,u.username,m.verified_at,s.token AS session_key FROM sessions s '
                             'JOIN users u ON s.user_id=u.id JOIN session_security m ON m.token=s.token '
                             'JOIN user_emails e ON e.user_id=u.id AND e.verified=1 '
                             'WHERE s.token=? AND s.expires>?',
                             (digest(token), time.time())).fetchone()
        if not row:
            raise Rejected('请先登录平台', 401)
        if not row['active']:
            raise Rejected('服务已暂停，请联系管理员', 403)
        if request.method not in ('GET', 'HEAD') and request.headers.get('x-csrf-token') != row['csrf']:
            raise Rejected('会话校验失败，请刷新页面', 403)
        return dict(row)

    def admin(user=Depends(current)):
        if user['role'] != 'admin':
            raise Rejected('需要管理员权限', 403)
        return user

    auth.install(app, current, rate)
    billing.install(app, current, admin, auth, rate)
    Feedback(store).install(app, current, admin)

    @app.get('/api/help/settings')
    def help_settings():
        defaults = store.defaults()
        return {key: defaults[key] for key in ('ttl_seconds', 'max_turns', 'retention_days')}

    @app.get('/api/me')
    async def me(user=Depends(current)):
        saved = store.account(user['user_id'])
        return {**{k: v for k, v in user.items() if k not in ('session_key', 'verified_at')},
                'email_hint': masked_account(auth.email(user['user_id'])['email']),
                'environment': os.environ.get('PLATFORM_ENVIRONMENT', '本机调试'),
                'account_state': saved['state'] if saved else 'unbound',
                'account_label': masked_account(saved['credentials']['username']) if saved else None,
                'login_check': runtime.last_check(user['user_id']),
                'connection_state': runtime.connection_state(user['user_id']), 'policy': store.policy(user['user_id']),
                'retention_days': store.defaults()['retention_days'], 'dialogue': store.dialogue(user['user_id'])}

    @app.get('/api/usage')
    async def usage(user=Depends(current)):
        return store.summary(user['user_id'])

    @app.get('/api/devices')
    async def devices(user=Depends(current)):
        return store.devices(user['user_id'])

    @app.post('/api/devices/refresh')
    async def refresh(request: Request, user=Depends(current)):
        rate(request, 'refresh', 6)
        try:
            return await runtime.refresh(user['user_id'])
        except Rejected:
            raise
        except Exception:
            raise Rejected('暂时无法获取设备，后台会自动检查绑定状态；原绑定保留，请稍后重试', 502) from None

    @app.put('/api/devices')
    async def selection(data: Selection, user=Depends(current)):
        if user['user_id'] in runtime.changing:
            raise Rejected('正在切换账号，请稍后修改设备', 409)
        saved = store.account(user['user_id'])
        if not saved or saved['state'] != 'ready':
            raise Rejected('请先完成小米账号绑定', 409)
        if any(d in runtime.excluded for d in data.devices):
            raise Rejected('该设备保留给其他环境，禁止启用', 403)
        store.select_devices(user['user_id'], data.devices)
        return {'ok': True}

    @app.post('/api/xiaomi/login')
    async def xiaomi_login(data: Binding, request: Request, user=Depends(current)):
        rate(request, 'xiaomi', 3)
        return runtime.begin_login(user['user_id'], data.username, data.password)

    @app.post('/api/xiaomi/check')
    async def check_login(request: Request, user=Depends(current)):
        rate(request, 'xiaomi-check', 6)
        return await runtime.check_login(user['user_id'])

    @app.post('/api/xiaomi/recover')
    async def recover_xiaomi(request: Request, user=Depends(current)):
        rate(request, 'xiaomi-recovery', 3)
        if not runtime.xiaomi.status(user['user_id']):
            raise Rejected('当前没有小米限流暂停，无需重复检查', 409)
        return await runtime.check_login(user['user_id'], manual_recovery=True)

    @app.post('/api/xiaomi/reverify')
    async def reverify(data: Reverify, request: Request, user=Depends(current)):
        rate(request, 'xiaomi', 3)
        return await runtime.begin_reverify(user['user_id'], data.password)

    @app.post('/api/xiaomi/replace')
    async def replace_account(data: Binding, request: Request, user=Depends(current)):
        auth.recent(user)
        rate(request, 'xiaomi', 3)
        return runtime.begin_replace(user['user_id'], data.username, data.password)

    @app.post('/api/xiaomi/replace/confirm')
    async def confirm_switch(data: ConfirmSwitch, user=Depends(current)):
        auth.recent(user)
        return runtime.confirm_replace(user['user_id'], data.job_id)

    @app.get('/api/xiaomi/login')
    async def login_status(user=Depends(current)):
        return runtime.login_status(user['user_id'])

    @app.post('/api/xiaomi/code')
    async def submit(data: Code, request: Request, user=Depends(current)):
        rate(request, 'code', 5)
        runtime.submit_code(user['user_id'], data.job_id, data.code)
        return {'ok': True}

    @app.delete('/api/xiaomi/login')
    async def cancel(user=Depends(current)):
        return await runtime.cancel_login(user['user_id'])

    @app.delete('/api/xiaomi')
    async def unbind(user=Depends(current)):
        auth.recent(user)
        await runtime.unbind(user['user_id'])
        return {'ok': True}

    @app.put('/api/dialogue')
    async def dialogue(data: Dialogue, user=Depends(current)):
        store.set_dialogue(user['user_id'], data.model_dump(exclude_none=True))
        return {'ok': True}

    @app.post('/api/dialogue/clear')
    async def clear(data: Clear, user=Depends(current)):
        owned = {d['id'] for d in store.devices(user['user_id'])}
        if data.device_id not in owned:
            raise Rejected('设备不属于当前用户', 403)
        runtime.clear_memory(user['user_id'], data.device_id)
        return {'ok': True}

    @app.post('/api/debug')
    async def debug(user=Depends(admin)):
        raise Rejected('文字调试已下线，请在管理后台的模型服务中测试连接', 410)

    @app.get('/api/history')
    async def history(before: float | None = None, after: float | None = None,
                      before_id: str | None = None, after_id: str = '', user=Depends(current)):
        if before is not None and after is not None:
            raise Rejected('请分别查询更早或更新的记录')
        return store.history(user['user_id'], before=before, after=after,
                             before_id=before_id, after_id=after_id)

    @app.delete('/api/history')
    async def delete_history(user=Depends(current)):
        store.delete_history(user['user_id'])
        return {'ok': True}

    @app.delete('/api/history/{rid}')
    async def delete_one(rid: str, user=Depends(current)):
        store.delete_history(user['user_id'], rid)
        return {'ok': True}

    @app.get('/api/admin')
    async def dashboard(user=Depends(admin)):
        with store.db() as db:
            users = [dict(r) for r in db.execute('SELECT id,username,role,active,policy,created FROM users ORDER BY created')]
            for row in users:
                row['memory_inherited'] = not any(k in json.loads(row['policy']) for k in ('ttl_seconds', 'max_turns'))
                row['policy'] = store.policy(row['id'])
                row['today'] = store.summary(row['id'])['today']
                row['device_count'] = db.execute('SELECT COUNT(*) FROM devices WHERE user_id=? AND present=1', (row['id'],)).fetchone()[0]
                row['listening_count'] = runtime.listening_count(row['id'])
        return {'users': users, 'usage': store.summary(), 'defaults': store.defaults(),
                'fallback_rules': store.fallback_rules(), 'capacity': runtime.capacity(),
                'speech_settings': store.speech_settings(runtime.base_cfg.get('tts'))}

    @app.get('/api/admin/usage-ranking')
    async def usage_ranking(period: Literal['day', 'week', 'month', 'last_3_months',
                                            'last_6_months', 'last_year'] = 'day',
                            reference_date: date | None = None, search: str = Query(default='', max_length=32),
                            only_unknown: bool = False, page: int = Query(default=1, ge=1, le=1000000),
                            user=Depends(admin)):
        return await run_in_threadpool(ranking, store, period=period, reference_date=reference_date, search=search,
                                       only_unknown=only_unknown, page=page)

    @app.get('/api/admin/capacity')
    async def capacity_status(user=Depends(admin)):
        return runtime.capacity()

    @app.get('/api/admin/speech-settings')
    async def speech_settings_status(user=Depends(admin)):
        return store.speech_settings(runtime.base_cfg.get('tts'))

    @app.put('/api/admin/speech-settings')
    async def update_speech_settings(data: SpeechSettings, user=Depends(admin)):
        store.update_settings({'speech_settings': data.model_dump()})
        return store.speech_settings(runtime.base_cfg.get('tts'))

    @app.get('/api/admin/model-services')
    async def model_services(user=Depends(admin)):
        return {'services': runtime.services.public(), 'managed': runtime.services.managed(),
                'attempts': runtime.services.recent_attempts()}

    @app.post('/api/admin/model-services')
    async def create_model_service(data: ModelService, user=Depends(admin)):
        auth.recent(user)
        return {'id': runtime.services.save(None, data.model_dump())}

    @app.post('/api/admin/model-services/{ident}/test')
    async def test_model_service(ident: str, data: Input, user=Depends(admin)):
        return await runtime.test_model_service(user['user_id'], ident)

    @app.put('/api/admin/model-services/{ident}')
    async def update_model_service(ident: str, data: ModelService, user=Depends(admin)):
        auth.recent(user)
        return {'id': runtime.services.save(ident, data.model_dump())}

    @app.delete('/api/admin/model-services/{ident}')
    async def delete_model_service(ident: str, user=Depends(admin)):
        auth.recent(user)
        runtime.services.delete(ident)
        return {'ok': True}

    @app.put('/api/admin/capacity')
    async def capacity_limits(data: Capacity, user=Depends(admin)):
        limits = data.model_dump()
        store.update_settings({'runtime_limits': limits})
        runtime.configure_capacity(limits)
        return runtime.capacity()

    @app.post('/api/admin/fallback-rules/test')
    async def test_failure(data: FailureSample, user=Depends(admin)):
        return failure_match(data.answer, store.fallback_rules())

    @app.post('/api/admin/fallback-rules')
    async def add_failure(data: FailureRule, user=Depends(admin)):
        ident = secrets.token_hex(16)
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT COUNT(*) FROM fallback_rules').fetchone()[0] >= 200:
                raise Rejected('最多维护200条话术，请先整理已有规则')
            db.execute('INSERT INTO fallback_rules VALUES(?,?,?,?,?)',
                       (ident, data.name, data.match_type, data.content, int(data.enabled)))
        return {'id': ident}

    @app.put('/api/admin/fallback-rules/{ident}')
    async def edit_failure(ident: str, data: FailureRule, user=Depends(admin)):
        with store.db() as db:
            changed = db.execute('UPDATE fallback_rules SET name=?,match_type=?,content=?,enabled=? WHERE id=?',
                                 (data.name, data.match_type, data.content, int(data.enabled), ident)).rowcount
        if not changed:
            raise Rejected('话术不存在', 404)
        return {'ok': True}

    @app.delete('/api/admin/fallback-rules/{ident}')
    async def delete_failure(ident: str, user=Depends(admin)):
        with store.db() as db:
            db.execute('DELETE FROM fallback_rules WHERE id=?', (ident,))
        return {'ok': True}

    @app.post('/api/admin/invites')
    async def invite(user=Depends(admin)):
        return {'token': store.invite(), 'expires_in': 86400}

    @app.post('/api/admin/users/{uid}/reset')
    async def reset(uid: str, user=Depends(admin)):
        target = store.user(uid)
        if not target or target['role'] == 'admin':
            raise Rejected('只能重置普通用户', 400)
        auth.recent(user)
        mail = auth.email(uid)
        if not mail or not mail['verified']:
            raise Rejected('该用户尚未验证安全邮箱，请先完成邮箱归属确认')
        await auth.issue('reset', mail['email'], {'password': target['password'], 'username': target['username']}, uid)
        return {'message': '密码重置邮件已发送至用户安全邮箱，管理员不会获得重置链接。'}

    @app.put('/api/admin/users/{uid}')
    async def update_user(uid: str, data: UserPolicy, user=Depends(admin)):
        target = store.user(uid)
        if not target or target['role'] == 'admin':
            raise Rejected('请选择普通用户', 400)
        values = data.model_dump(exclude_none=True)
        inherit = values.pop('inherit_memory')
        if inherit:
            values.pop('ttl_seconds', None)
            values.pop('max_turns', None)
        with store.db() as db:
            old = json.loads(target['policy'])
            if inherit:
                old.pop('ttl_seconds', None)
                old.pop('max_turns', None)
            active = values.pop('active', target['active'])
            old.update(values)
            db.execute('UPDATE users SET active=?,policy=? WHERE id=?', (int(active), json.dumps(old), uid))
            if not active:
                db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
        return {'ok': True}

    @app.put('/api/admin/defaults')
    async def defaults(data: Defaults, user=Depends(admin)):
        store.update_settings(data.model_dump(exclude_none=True))
        return {'ok': True}

    static = Path(__file__).parent / 'static'
    app.mount('/static', StaticFiles(directory=static), name='static')

    @app.get('/')
    async def index():
        return FileResponse(static / 'index.html')

    @app.get('/help')
    async def help_page():
        return FileResponse(static / 'help.html')

    return app
