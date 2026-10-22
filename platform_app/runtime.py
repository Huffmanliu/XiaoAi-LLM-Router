"""Account login state machines and independent, sequential device workers."""
import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager
import logging
import secrets
import time

import aiohttp
from miservice import MiAccount, MiNAService
from miservice.miaccount import UA_MINA

import llm_router
from main import RouterPipeline, match_wake
from memory import MemoryStore
from mi_conversation import MiSpeaker, tier_of
from .device_policy import NATIVE_AI_REASON, hardware_code, native_ai
from .capacity import ModelGate
from .interruption import DeviceTurn, stop_requested, valid_records
from .model_services import ModelServices, TEST_RESULTS
from .store import Rejected
from .security import masked_account
from .routing import failure_match

# The dependency can log raw authentication responses. Never forward those logs.
for logger_name in ('miservice', 'LiteLLM', 'LiteLLM Router', 'LiteLLM Proxy', 'httpx', 'httpcore', 'openai'):
    logging.getLogger(logger_name).disabled = True
log = logging.getLogger(__name__)
POLL_INTERVAL = 3


class VerificationRequired(Exception):
    pass


class SafeAccount(MiAccount):
    def __init__(self, *args, interactive=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.interactive = interactive
        self.login_lock = asyncio.Lock()

    async def login(self, sid):
        async with self.login_lock:
            return await super().login(sid)

    async def _verify_otp(self, sid, ntf):
        # Stop BEFORE the library triggers a new SMS/email in unattended mode.
        if not self.interactive:
            raise VerificationRequired('User verification required')
        return await super()._verify_otp(sid, ntf)


class TokenStore:
    def __init__(self, store, uid, username, password, token=None, *, persist=True):
        self.store, self.uid = store, uid
        self.username, self.password, self.token = username, password, token
        self.persist = persist

    async def load_token(self):
        return self.token

    async def save_token(self, token=None):
        # Don't destroy the last encrypted snapshot on a transient login failure.
        if token:
            self.token = token
            # Save only after explicit login completes, or when a bound account refreshes.
            if self.persist and self.store.account(self.uid):
                self.store.save_account(self.uid, self.username, self.password, token)


class Runtime:
    def __init__(self, store, cfg, excluded=()):
        self.store, self.base_cfg = store, cfg
        self.services = ModelServices(store, cfg)
        self.excluded = set(excluded)
        self.jobs, self.accounts, self.workers = {}, {}, {}
        self.memory, self.memory_rules = {}, {}
        self.limits = store.runtime_limits()
        self.semaphore = ModelGate(self.limits['model_concurrency'])
        self.model_active = self.model_waiting = self.speech_pending = 0
        self.capacity_rejected = self.capacity_expired = 0
        self.stopping = False
        self.background = []
        self.notice_at = {}
        self.text_locks = {}
        self.memory_versions = {}
        self.changing, self.checks, self.probe_locks = set(), {}, {}

    async def start(self):
        self.store.cleanup()
        self.background = [asyncio.create_task(self.supervise()), asyncio.create_task(self.clean_loop())]

    async def close(self):
        self.stopping = True
        for job in self.jobs.values():
            job['task'].cancel()
        for task in self.background:
            task.cancel()
        tasks = [*self.workers.values(), *(j['task'] for j in self.jobs.values()), *self.background]
        # Graceful shutdown is bounded; interrupted API calls remain charged/unknown.
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=20)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for ctx in self.accounts.values():
            await ctx['session'].close()
        self.accounts.clear()

    async def clean_loop(self):
        while not self.stopping:
            await asyncio.sleep(3600)
            self.store.cleanup()

    def login_status(self, uid):
        job = self.jobs.get(uid)
        if not job:
            return {'state': 'idle'}
        if job['state'] == 'awaiting_confirmation' and job.get('expires', 0) <= time.time():
            job.pop('candidate', None)
            job.update(state='expired', message='新账号验证已过期，原绑定保持不变，请重新开始')
        return {k: job[k] for k in ('id', 'state', 'mode', 'method', 'message',
                                    'old_label', 'new_label', 'device_count') if k in job}

    @staticmethod
    def _probe_result(status, data):
        if status == 401:
            return 'expired'
        if not isinstance(data, dict):
            return 'unknown'
        code = data.get('code')
        if status == 200 and code == 0:
            return 'valid'
        message = str(data.get('message') or data.get('msg') or '').lower()
        if code in (401, '401') or (code != 0 and any(marker in message for marker in (
                'invalid token', 'token expired', 'auth', 'not logged in', 'login required', '未登录', '登录失效', '登录过期'))):
            return 'expired'
        return 'unknown'

    async def _probe_token(self, uid, token):
        # Use raw, read-only HTTP requests: no MiAccount/MiNA auto-login or token deletion.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
            cookies = {'userId': str(token['userId']), 'serviceToken': token['micoapi'][1]}

            async def read(url, params, request_cookies):
                async with session.get(url, params=params, cookies=request_cookies,
                                       headers={'User-Agent': UA_MINA}, allow_redirects=False) as r:
                    try:
                        data = await r.json(content_type=None)
                    except (ValueError, TypeError):
                        data = None
                    return self._probe_result(r.status, data)
            state = await read('https://api2.mina.mi.com/admin/v2/device_list',
                               {'master': 0, 'requestId': 'app_ios_' + secrets.token_hex(15)}, cookies)
            if state != 'valid':
                return state
            devices = [d for d in self.store.devices(uid) if d['present'] and d['supported']
                       and d['id'] not in self.excluded]
            if not devices:
                return 'valid'
            device = next((d for d in devices if d['enabled']), devices[0])
            return await read('https://userprofile.mina.mi.com/device_profile/v2/conversation',
                              {'source': 'dialogu', 'hardware': device['hardware'],
                               'timestamp': str(int(time.time() * 1000)), 'limit': 1},
                              {**cookies, 'deviceId': device['id']})

    async def check_login(self, uid):
        lock = self.probe_locks.setdefault(uid, asyncio.Lock())
        async with lock:
            saved = self.store.account(uid)
            if not saved:
                raise Rejected('尚未绑定小米账号', 409)
            identity = saved['identity']
            ctx = self.accounts.get(uid)
            token = deepcopy(ctx['account'].token if ctx and ctx['account'].token
                             else saved['credentials']['token'])
            try:
                if not token.get('userId') or not token.get('micoapi'):
                    state = 'expired'
                else:
                    state = await asyncio.wait_for(self._probe_token(uid, token), timeout=20)
            except (asyncio.TimeoutError, aiohttp.ClientError, ValueError, KeyError, TypeError, IndexError):
                state = 'unknown'
            current = self.store.account(uid)
            if not current or current['identity'] != identity:
                raise Rejected('绑定已变化，请重新检查', 409)
            messages = {'valid': '当前小米账号绑定正常，无需重新绑定。',
                        'expired': '当前小米账号绑定已失效，请输入当前账号的密码重新绑定。',
                        'unknown': '暂时无法确认小米账号的绑定状态，请稍后重试；未发起重新绑定或发送验证码。'}
            result = {'state': state, 'checked_at': time.time(), 'message': messages[state]}
            self.checks[uid] = {**result, 'identity': identity}
            if state == 'expired':
                self.store.set_account_state(uid, 'needs_verification')
            elif state == 'valid':
                self.store.set_account_state(uid, 'ready')
                if ctx and not ctx['account'].token:
                    ctx['account'].token = token
                if ctx:
                    ctx['state'] = 'ready'
            return result

    def last_check(self, uid):
        saved = self.store.account(uid)
        check = self.checks.get(uid)
        if not saved or not check or check['identity'] != saved['identity']:
            return None
        return {k: v for k, v in check.items() if k != 'identity'}

    def _begin(self, uid, username, password, mode):
        self.login_status(uid)  # Expire stale confirmation candidates first.
        job = self.jobs.get(uid)
        if uid in self.changing or (job and (not job['task'].done()
                                             or job['state'] == 'awaiting_confirmation')):
            raise Rejected('已有账号操作，请完成或取消', 409)
        if job and time.time() - job['created'] < 60:
            raise Rejected('请间隔60秒后重试绑定', 429)
        saved = self.store.account(uid)
        job = {'id': secrets.token_hex(16), 'state': 'logging_in', 'created': time.time(), 'mode': mode,
               'expected_identity': saved['identity'] if saved else None}
        self.jobs[uid] = job
        job['task'] = asyncio.create_task(self._login(uid, username, password, job))
        return self.login_status(uid)

    def begin_login(self, uid, username, password):
        if self.store.account(uid):
            raise Rejected('已有绑定。请使用确认绑定状态或更换绑定账号入口', 409)
        return self._begin(uid, username, password, 'bind')

    async def begin_reverify(self, uid, password):
        # Recheck on submit as well: neither an altered client nor a stale page can force a valid login.
        result = await self.check_login(uid)
        if result['state'] == 'valid':
            return {'state': 'already_valid', 'message': result['message']}
        if result['state'] != 'expired':
            raise Rejected(result['message'], 503)
        saved = self.store.account(uid)
        return self._begin(uid, saved['credentials']['username'], password, 'reverify')

    def begin_replace(self, uid, username, password):
        if not self.store.account(uid):
            raise Rejected('尚未绑定账号，请使用首次绑定入口', 409)
        return self._begin(uid, username, password, 'replace')

    async def _login(self, uid, username, password, job):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                async def otp(method):
                    job['future'] = asyncio.get_running_loop().create_future()
                    job.update(state='verification_required', method='短信' if method == 'Phone' else '邮箱')
                    return await asyncio.wait_for(job['future'], timeout=180)

                # Staged authentication MUST NOT persist tokens to the existing account.
                tokens = TokenStore(self.store, uid, username, password, persist=False)
                account = SafeAccount(session, username, password, token_store=tokens,
                                      otp_callback=otp, interactive=True)
                ok = await asyncio.wait_for(account.login('micoapi'), timeout=240)
                if not ok:
                    raise Rejected('绑定验证失败；请检查信息，稍后重试绑定。原绑定保持不变。')
                identity = self.store.assert_available(uid, account.token)
                if job['mode'] == 'replace' and identity == job['expected_identity']:
                    raise Rejected('这是当前已绑定的账号，无需更换，请使用确认绑定状态入口')
                if job['mode'] == 'reverify' and identity != job['expected_identity']:
                    raise Rejected('验证身份与当前绑定不一致，原绑定保持不变', 409)
                raw_rows = await asyncio.wait_for(MiNAService(account).device_list(), timeout=30)
                if raw_rows is None:
                    raise Rejected('账号验证成功，但设备列表暂不可用，原绑定保持不变，请稍后重试')
                job['verified_at'] = time.time()
                candidate = {'credentials': {'username': username, 'password': password, 'token': account.token},
                             'devices': self.normalize_devices(raw_rows)}
                job['candidate'] = self.store.vault.seal(candidate, 'candidate:' + uid + ':' + job['id'])
                if job['mode'] == 'replace':
                    old = self.store.account(uid)
                    job.update(state='awaiting_confirmation', expires=time.time() + 300,
                               old_label=masked_account(old['credentials']['username']) if old else '原账号',
                               new_label=masked_account(username), device_count=len(candidate['devices']),
                               message='新账号验证成功，原账号仍在使用。请确认切换，或取消保留原账号。')
                else:
                    await self._apply(uid, job)
        except asyncio.CancelledError:
            job.pop('candidate', None)
            if job['state'] != 'completed':
                job.update(state='cancelled', message='操作已取消，原绑定保持不变')
            raise
        except asyncio.TimeoutError:
            job.pop('candidate', None)
            job.update(state='expired', message='验证超时，原绑定保持不变，请重新开始')
        except Rejected as exc:
            job.pop('candidate', None)
            job.update(state='failed', message=str(exc))
        except Exception:
            job.pop('candidate', None)
            job.update(state='failed', message='连接失败，原绑定保持不变，请稍后重试')
        finally:
            job.pop('future', None)

    async def _apply(self, uid, job):
        if uid in self.changing:
            raise Rejected('已有切换操作进行中', 409)
        self.changing.add(uid)
        job['state'] = 'applying'
        try:
            tasks = [t for (u, _), t in self.workers.items() if u == uid]
            if tasks:
                await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
            text_lock = self.text_locks.setdefault(uid, asyncio.Lock())
            async with text_lock:
                user = self.store.user(uid)
                if not user or not user['active']:
                    raise Rejected('服务已暂停，不能切换账号', 403)
                candidate = self.store.vault.open(job['candidate'], 'candidate:' + uid + ':' + job['id'])
                replacing = job['mode'] == 'replace'
                self.store.install_account(uid, candidate['credentials'], candidate['devices'],
                                           job['expected_identity'], replace=replacing)
                old = self.accounts.pop(uid, None)
                if replacing:
                    self.memory.pop(uid, None)
                    self.memory_rules.pop(uid, None)
                self.checks[uid] = {'identity': self.store.account(uid)['identity'], 'state': 'valid',
                                    'checked_at': job.get('verified_at', time.time()), 'message': '小米账号已绑定'}
                job.pop('candidate', None)
                job.update(state='completed', message='切换成功，请主动选择新账号的音箱并确认。' if replacing
                           else '绑定成功，当前小米账号已绑定。')
                if old:
                    await asyncio.shield(old['session'].close())
        finally:
            self.changing.discard(uid)

    def confirm_replace(self, uid, job_id):
        self.login_status(uid)
        job = self.jobs.get(uid)
        if not job or job['id'] != job_id or job['mode'] != 'replace':
            raise Rejected('切换验证已失效，请重新开始', 409)
        if job['state'] in ('applying', 'completed'):
            return self.login_status(uid)
        if job['state'] != 'awaiting_confirmation' or 'candidate' not in job:
            raise Rejected('请先验证新账号，再确认切换', 409)
        job['state'] = 'applying'
        job['task'] = asyncio.create_task(self._confirm_apply(uid, job))
        return self.login_status(uid)

    async def _confirm_apply(self, uid, job):
        try:
            await self._apply(uid, job)
        except asyncio.CancelledError:
            job.pop('candidate', None)
            if job['state'] != 'completed':
                job.update(state='cancelled', message='切换已取消，原绑定保持不变')
            raise
        except Rejected as exc:
            job.pop('candidate', None)
            job.update(state='failed', message=str(exc))
        except Exception:
            job.pop('candidate', None)
            job.update(state='failed', message='切换未完成，原绑定保持不变，请重试')

    def submit_code(self, uid, job_id, code):
        job = self.jobs.get(uid)
        if not job or job['id'] != job_id or job['state'] != 'verification_required':
            raise Rejected('验证已失效，请重新开始', 409)
        f = job.get('future')
        if not f or f.done():
            raise Rejected('验证码已提交', 409)
        job['state'] = 'verifying'
        f.set_result(code)

    async def cancel_login(self, uid):
        job = self.jobs.get(uid)
        if job:
            if not job['task'].done():
                job['task'].cancel()
                await asyncio.gather(job['task'], return_exceptions=True)
            job.pop('candidate', None)
            if job['state'] != 'completed':
                job.update(state='cancelled', message='操作已取消，原绑定保持不变')
        return self.login_status(uid)

    def normalize_devices(self, rows):
        result = []
        for d in rows:
            hw = hardware_code(d.get('hardware') or d.get('model'))
            tier = tier_of(hw)
            name = str(d.get('name') or d.get('nickname') or hw or '音箱')
            # MiNA returns speakers; defensively omit an explicitly non-speaker product.
            model = str(d.get('model') or '').lower()
            if tier['tier'] == 'unknown' and model and not any(x in model for x in ('speaker', 'xiaoai')):
                continue
            did = str(d.get('deviceID') or d.get('device_id') or d.get('did') or '')
            if not did:
                continue
            supported = tier['tier'] in ('perfect', 'normal')
            reason = '' if supported else (tier.get('note') or '该机型尚未验证兼容，暂不可启用')
            if native_ai(hw):
                supported, reason = False, NATIVE_AI_REASON
            if did in self.excluded:
                supported, reason = False, '此设备分配给其他运行环境，当前环境不可启用'
            result.append({'id': did, 'name': name, 'hardware': hw, 'supported': supported, 'reason': reason,
                           'physical_id': str(d.get('miotDID') or '')})
        return result

    def enabled(self, uid, did):
        if uid in self.changing or did in self.excluded:
            return False
        return any(not native_ai(d['hardware']) for d in self.store.polling_candidates(uid, did))

    def listening_count(self, uid):
        return sum(1 for (owner, did), task in self.workers.items()
                   if owner == uid and not task.done() and self.enabled(uid, did))

    async def context(self, uid):
        if uid in self.accounts:
            return self.accounts[uid]
        saved = self.store.account(uid)
        if not saved:
            raise Rejected('请先绑定小米账号')
        c = saved['credentials']
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        tokens = TokenStore(self.store, uid, c['username'], c['password'], c['token'])
        account = SafeAccount(session, c['username'], c['password'], token_store=tokens)
        account.token = c['token']
        cfg = self.config(uid)
        sp = MiSpeaker(account, MiNAService(account), cfg)
        ctx = {'session': session, 'account': account, 'speaker': sp, 'state': 'ready'}
        self.accounts[uid] = ctx
        return ctx

    async def refresh(self, uid):
        if uid in self.changing:
            raise Rejected('账号切换进行中，请稍后刷新设备', 409)
        ctx = await self.context(uid)
        rows = await asyncio.wait_for(ctx['speaker'].mina.device_list(), timeout=30)
        if ctx['account'].token is None:
            self.store.set_account_state(uid, 'needs_verification')
            raise Rejected('小米账号绑定已失效，请重新绑定', 409)
        if rows is None:
            raise Rejected('设备列表暂不可用，请稍后重试', 502)
        self.store.sync_devices(uid, self.normalize_devices(rows))
        return self.store.devices(uid)

    async def unbind(self, uid):
        if uid in self.changing:
            raise Rejected('账号切换进行中，请稍后再试', 409)
        # First stop new work; let active replies complete before closing the session.
        self.store.select_devices(uid, [])
        await self.cancel_login(uid)
        tasks = [t for (u, _), t in self.workers.items() if u == uid]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        ctx = self.accounts.pop(uid, None)
        if ctx:
            await ctx['session'].close()
        with self.store.db() as db:
            db.execute('DELETE FROM devices WHERE user_id=?', (uid,))
            db.execute('DELETE FROM accounts WHERE user_id=?', (uid,))
            db.execute('DELETE FROM device_claims WHERE user_id=?', (uid,))
        self.memory.pop(uid, None)

    def config(self, uid):
        cfg = deepcopy(self.base_cfg)
        cfg['llm']['stream'] = False
        cfg.pop('usage', None)  # Platform ledger is authoritative; no mixed legacy ledger.
        p = self.store.policy(uid)
        cfg['llm']['max_output_tokens'] = p['max_output_tokens']
        return cfg

    def sessions(self, uid):
        p = self.store.policy(uid)
        rules = (p['ttl_seconds'], p['max_turns'])
        if self.memory_rules.get(uid) != rules:
            self.memory[uid] = MemoryStore(ttl=rules[0], max_turns=rules[1])
            self.memory_rules[uid] = rules
        return self.memory.setdefault(uid, MemoryStore(ttl=rules[0], max_turns=rules[1]))

    def clear_memory(self, uid, did):
        key = (uid, did)
        self.memory_versions[key] = self.memory_versions.get(key, 0) + 1
        self.sessions(uid).get(did).clear()

    def configure_capacity(self, limits):
        self.limits = dict(limits)
        self.semaphore.resize(limits['model_concurrency'])

    def capacity(self):
        return {'limits': self.limits, 'active': self.model_active,
                'waiting': self.model_waiting + self.speech_pending,
                'rejected': self.capacity_rejected, 'expired': self.capacity_expired}

    @asynccontextmanager
    async def model_slot(self, rid, deadline=None, turn=None):
        queued = self.speech_pending + max(0, self.model_active + self.model_waiting - self.limits['model_concurrency'])
        if queued >= self.limits['queue_limit'] and self.model_active + self.model_waiting >= self.limits['model_concurrency']:
            self.capacity_rejected += 1
            self.store.update_request(rid, status='cancelled')
            raise Rejected('AI 服务繁忙，等待队列已满，请稍后再试；本次未调用模型', 429)
        deadline = min(deadline or float('inf'), time.monotonic() + self.limits['queue_timeout'])
        self.model_waiting += 1
        if turn:
            turn.owner.waiting_for_model = True
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                await self.semaphore.acquire()
        except TimeoutError:
            self.capacity_expired += 1
            self.store.update_request(rid, status='cancelled')
            raise Rejected('AI 请求等待超时，请重新提问；本次未调用模型', 429) from None
        except BaseException:
            self.store.update_request(rid, status='cancelled')
            raise
        finally:
            self.model_waiting -= 1
            if turn:
                turn.owner.waiting_for_model = False
        self.model_active += 1
        try:
            yield
        finally:
            self.model_active -= 1
            self.semaphore.release()

    async def supervise(self):
        while not self.stopping:
            for key, task in list(self.workers.items()):
                if task.done():
                    if not task.cancelled() and task.exception():
                        log.warning('device worker stopped with an error')
                    self.workers.pop(key, None)
            # One metadata query for all devices; never decrypt credentials for status checks.
            for d in self.store.polling_candidates():
                key = (d['user_id'], d['id'])
                if (key not in self.workers and key[0] not in self.changing
                        and key[1] not in self.excluded and not native_ai(d['hardware'])):
                    self.workers[key] = asyncio.create_task(self.device_loop(*key))
            await asyncio.sleep(1)

    def route(self, uid, text, native_answer):
        dialogue = self.store.dialogue(uid)
        if stop_requested(text, dialogue['wake_words']):
            return False
        if dialogue['routing_mode'] == 'keyword':
            return bool(match_wake(dialogue['wake_words'], text))
        return failure_match(native_answer or '', self.store.fallback_rules())['takeover']

    def ready_records(self, uid, items, pending, seen, cutoff, now):
        """Wait for stable native replies; never dispatch stale, future, or replayed records."""
        ready = []
        dialogue = self.store.dialogue(uid)
        cutoff = max(cutoff, dialogue['routing_updated'], now - 60)
        for item in sorted(valid_records(items, cutoff, now), key=lambda r: r['ts']):
            rid, stamp = item.get('request_id'), item.get('ts', 0)
            if not rid or stamp <= cutoff or stamp > now + 5 or rid in seen:
                continue
            if item.get('takeover_allowed') is False:
                seen[rid] = stamp
                pending.pop(rid, None)
                continue
            if dialogue['routing_mode'] == 'keyword':
                seen[rid] = stamp
                if self.route(uid, item['text'], item.get('native_answer')):
                    ready.append(item)
                continue
            answer = item.get('native_answer') or ''
            prior = pending.get(rid)
            if not prior:
                prior = pending[rid] = {'first': now, 'answer': answer, 'changed': now}
            elif prior['answer'] != answer:
                prior.update(answer=answer, changed=now)
            if answer and now - prior['changed'] >= 2:
                seen[rid] = stamp
                pending.pop(rid, None)
                if self.route(uid, item['text'], answer):
                    ready.append(item)
            elif now - prior['first'] >= 15:
                seen[rid] = stamp
                pending.pop(rid, None)
        for rid in list(pending):
            if now - pending[rid]['first'] > 60:
                pending.pop(rid)
        for rid in list(seen):
            if seen[rid] < now - 120:
                seen.pop(rid)
        return ready

    async def device_loop(self, uid, did):
        started, seen, pending = time.time(), {}, {}
        # A device retains only the latest interaction, never a backlog of old answers.
        queue = asyncio.Queue(maxsize=1)
        turn = DeviceTurn()
        responder = None
        cancelled = False
        try:
            ctx = await self.context(uid)
            sp = ctx['speaker']
            responder = asyncio.create_task(self.reply_loop(uid, did, sp, queue, turn))
            while not self.stopping and self.enabled(uid, did):
                try:
                    if hasattr(sp, 'recent_asks'):
                        items = await sp.recent_asks(did)
                    else:
                        item = await sp.latest_ask(did)
                        items = [item] if item else []
                    now = time.time()
                    cutoff = max(started, self.store.dialogue(uid)['routing_updated'])
                    if turn.observe(items, cutoff, now):
                        while not queue.empty():
                            queue.get_nowait()
                            self.speech_pending -= 1
                    ready = self.ready_records(uid, items, pending, seen, started, now)
                    for item in ready:
                        if item['request_id'] == turn.request_id and not turn.blocked and self.enabled(uid, did):
                            # A replacement inherits its own cancelled waiter's
                            # queue position budget until that waiter unwinds.
                            obsolete_wait = bool(turn.task and turn.task.cancelling() and turn.waiting_for_model)
                            waiting = self.speech_pending + self.model_waiting - int(obsolete_wait)
                            if queue.full() or waiting >= self.limits['queue_limit']:
                                self.capacity_rejected += 1
                                continue
                            self.speech_pending += 1
                            queue.put_nowait((item, time.monotonic() + self.limits['queue_timeout']))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if ctx['account'].token is None:
                        self.store.set_account_state(uid, 'needs_verification')
                        return
                    ctx['state'] = 'connection_error'
                    await asyncio.sleep(30)
                    continue
                ctx['state'] = 'ready'
                await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            log.warning('device connection unavailable')
        finally:
            while not queue.empty():
                queue.get_nowait()
                self.speech_pending -= 1
            if responder:
                if cancelled:
                    responder.cancel()
                else:
                    queue.put_nowait(None)
                await asyncio.gather(responder, return_exceptions=True)

    async def reply_loop(self, uid, did, speaker, queue, turn=None):
        """Join cancelled turns before starting the next, without stopping polling."""
        while True:
            entry = await queue.get()
            if entry is None:
                return
            self.speech_pending -= 1
            item, deadline = entry
            if not self.enabled(uid, did) or (turn and (item['request_id'] != turn.request_id or turn.blocked)):
                continue
            if time.monotonic() >= deadline:
                self.capacity_expired += 1
                continue
            token = turn.token() if turn else None
            task = asyncio.create_task(self.reply(uid, did, speaker, item, deadline, token))
            if turn:
                turn.task = task
            try:
                await task
            except asyncio.CancelledError:
                # Child cancellation means a new interaction; parent cancellation
                # means shutdown and must still propagate to the device worker.
                if asyncio.current_task().cancelling():
                    raise
            finally:
                if turn and turn.task is task:
                    turn.task = None

    async def reply(self, uid, did, speaker, item, deadline, turn):
        try:
            await self.handle(uid, did, item['text'], 'speaker', item['request_id'], speaker,
                              native_answer=item.get('native_answer'), observed_ts=item['ts'],
                              queue_deadline=deadline, turn=turn)
        except Rejected as exc:
            if exc.status == 429:
                try:
                    key = (uid, did)
                    if self.enabled(uid, did) and time.time() - self.notice_at.get(key, 0) >= 60:
                        self.notice_at[key] = time.time()
                        await self.speak(speaker, did, '现在提问较多，请稍后再试。', turn)
                except Exception:
                    log.warning('busy notice unavailable')
        except Exception:
            log.warning('device reply unavailable')

    @staticmethod
    async def speak(speaker, did, text, turn):
        if turn:
            turn.checkpoint()
            result = await speaker.tts(did, text, is_current=turn.current)
            turn.checkpoint()
            return result
        return await speaker.tts(did, text)

    async def handle(self, uid, did, text, source, request_id, speaker=None, *,
                     native_answer=None, observed_ts=None, queue_deadline=None, turn=None):
        if turn:
            turn.checkpoint()
        cfg = self.config(uid)
        if source == 'speaker':
            dialogue = self.store.dialogue(uid)
            stale = observed_ts is not None and observed_ts <= max(dialogue['routing_updated'], time.time() - 60)
            if not self.route(uid, text, native_answer) or stale:
                return None
            request_id = did + ':' + request_id
        try:
            rid = self.store.reserve(uid, did, source, request_id)
        except Rejected as exc:
            if source != 'speaker':
                raise
            key = (uid, did)
            if exc.status == 429 and time.time() - self.notice_at.get(key, 0) >= 60:
                self.notice_at[key] = time.time()
                await self.speak(speaker, did, str(exc), turn)
            return None
        p = self.store.policy(uid)
        memory = self.sessions(uid).get(did)
        memory_version = self.memory_versions.get((uid, did), 0)
        prompt = RouterPipeline(cfg, None).default_prompt
        saved_account = self.store.account(uid)
        account_label = masked_account(saved_account['credentials']['username']) if saved_account else None
        device_name = next((d['name'] for d in self.store.devices(uid) if d['id'] == did), None)
        t0 = time.monotonic()
        answer, usage, model, finish = '', None, None, None
        async with self.model_slot(rid, queue_deadline, turn):
            # A queued request must not survive a pause/disable action.
            user = self.store.user(uid)
            cancelled = not user or not user['active']
            if source == 'speaker':
                stale = observed_ts is not None and observed_ts <= max(
                    self.store.dialogue(uid)['routing_updated'], time.time() - 60)
                cancelled = cancelled or not self.enabled(uid, did) or stale or not self.route(uid, text, native_answer)
            if cancelled:
                self.store.update_request(rid, status='cancelled')
                raise Rejected('服务或设备已暂停', 409)
            try:
                if turn:
                    turn.checkpoint()
                response, usage, target = await self.services.complete(
                    rid, cfg, llm_router._payload(prompt, memory.to_llm_messages(), text), p['max_output_tokens'])
                choice = response.choices[0]
                model, finish = getattr(response, 'model', target), choice.finish_reason
                answer = (choice.message.content or '').strip()
                if turn:
                    turn.checkpoint()
                if not answer:
                    raise ValueError('Empty model response')
            except Rejected:
                self.store.update_request(rid, status='cancelled')
                if speaker:
                    await self.speak(speaker, did, 'AI 服务暂时不可用，请稍后再试。', turn)
                raise
            except BaseException as exc:
                self.store.finish(rid, usage, model=model, status='interrupted' if isinstance(exc, asyncio.CancelledError)
                                  else 'failed', elapsed_ms=round((time.monotonic() - t0) * 1000))
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if speaker:
                    await self.speak(speaker, did, '刚才网络有点开小差，请稍后再试。', turn)
                raise Rejected('模型请求失败，本次已计入额度，用量可能未知', 502) from None
            self.store.finish(rid, usage, model=model, status='completed', finish_reason=finish,
                              elapsed_ms=round((time.monotonic() - t0) * 1000))
            self.store.save_history(uid, did, rid, text, answer, account_label=account_label, device_name=device_name)
        if speaker:
            self.store.update_request(rid, speech_status='submitting')
            try:
                ok = await self.speak(speaker, did, self.store.dialogue(uid)['answer_prefix'] + answer, turn)
            except asyncio.CancelledError:
                self.store.update_request(rid, speech_status='interrupted')
                raise
            except BaseException:
                self.store.update_request(rid, speech_status='failed')
                raise
            self.store.update_request(rid, speech_status='submitted' if ok else 'failed')
        if (not speaker or ok) and (not turn or turn.current()):
            if (self.sessions(uid) is not None
                    and self.memory[uid].get(did) is memory
                    and self.memory_versions.get((uid, did), 0) == memory_version):
                # Only fully submitted speech enters context; never guess which
                # characters the hardware actually played before interruption.
                memory.append(user=text, assistant=answer)
        return {'id': rid, 'answer': answer, 'finish_reason': finish, 'usage': usage}

    async def text(self, uid, text, request_id):
        """Internal regression helper; no HTTP endpoint exposes arbitrary web prompts."""
        if uid in self.changing:
            raise Rejected('正在切换账号，请稍后再试', 409)
        lock = self.text_locks.setdefault(uid, asyncio.Lock())
        if lock.locked():
            raise Rejected('上一条文字调试仍在处理', 409)
        async with lock:
            return await self.handle(uid, 'web-debug', text, 'web', request_id)

    async def test_model_service(self, uid, ident):
        snapshot = self.services.test_snapshot(ident)
        rid = self.store.reserve(uid, 'model-test:' + ident, 'model_test', secrets.token_hex(16))
        self.services.begin_test(rid, snapshot)
        t0 = time.monotonic()
        result, usage, target = 'not_started', None, None
        try:
            async with self.model_slot(rid):
                user = self.store.user(uid)
                if not user or not user['active'] or user['role'] != 'admin':
                    raise Rejected('管理员账号已暂停或权限已变化', 403)
                result, usage, target = await self.services.test_connection(
                    rid, snapshot, min(64, self.store.policy(uid)['max_output_tokens']))
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError
        except Rejected:
            result = 'not_started'
        except asyncio.CancelledError:
            with self.store.db() as db:
                started = db.execute('SELECT started FROM requests WHERE id=?', (rid,)).fetchone()[0]
            elapsed = round((time.monotonic() - t0) * 1000)
            self.store.finish(rid, usage, model=target, status='interrupted' if started else 'cancelled', elapsed_ms=elapsed)
            self.services.finish_test(rid, 'interrupted' if started else 'not_started', elapsed)
            raise
        except Exception:
            result = 'request_failed'
        elapsed = round((time.monotonic() - t0) * 1000)
        with self.store.db() as db:
            started = db.execute('SELECT started FROM requests WHERE id=?', (rid,)).fetchone()[0]
        self.store.finish(rid, usage, model=target, elapsed_ms=elapsed,
                          status='completed' if result == 'success' else 'failed' if started else 'cancelled')
        self.services.finish_test(rid, result, elapsed)
        return {'ok': result == 'success', 'result': result, 'message': TEST_RESULTS[result],
                'elapsed_ms': elapsed, 'total_tokens': (usage or {}).get('total_tokens'), 'started': bool(started)}
