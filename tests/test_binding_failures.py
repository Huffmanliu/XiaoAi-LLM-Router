"""Synthetic failure paths: clear outcomes, safe diagnostics, no provider calls."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miservice import MiAccount

from platform_app.runtime import Runtime
from platform_app.store import Rejected
from test_account_lifecycle import account_store as account_store_fixture, complete, mock_auth
from test_capacity import eventually
from test_platform import CFG

account_store = account_store_fixture


def test_upstream_rejection_keeps_only_safe_error_code(account_store, monkeypatch, caplog):
    store, uid = account_store
    before = store.account(uid)['credentials']
    responses = [{'code': 70016, 'qs': 'synthetic', 'sid': 'micoapi', '_sign': 'synthetic', 'callback': 'synthetic'},
                 {'code': 4321, 'description': 'PRIVATE-UPSTREAM-BODY', 'passToken': 'PRIVATE-TOKEN'}]
    monkeypatch.setattr(MiAccount, '_serviceLogin', AsyncMock(side_effect=responses))
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        monkeypatch.setattr(runtime, '_probe_token', AsyncMock(return_value='expired'))
        await runtime.begin_reverify(uid, 'PRIVATE-ENTERED-PASSWORD')
        result = await complete(runtime, uid)
        assert result['state'] == 'failed' and result['stage'] == 'read_auth'
        assert result['diagnostic'] == {'stage': 'read_auth', 'kind': 'not_completed', 'upstream_code': 4321}
        assert '小米账号读取授权未完成' in result['message']
        assert 'PRIVATE' not in str(result) and 'PRIVATE' not in caplog.text
        assert 'code=4321' in caplog.text and not runtime.binding_probes
        assert store.account(uid)['credentials'] == before and store.devices(uid)[0]['enabled']
        monkeypatch.setattr('platform_app.runtime.time', SimpleNamespace(time=lambda: 1000))
        runtime.jobs[uid]['created'] = 950
        assert runtime.login_status(uid)['retry_after'] == 10
        with pytest.raises(Rejected, match='等待10秒'):
            runtime._begin(uid, 'synthetic', 'synthetic', 'reverify')
    asyncio.run(run())


def test_sms_submission_then_device_failure_is_not_reported_as_password_error(account_store, monkeypatch, caplog):
    store, uid = account_store
    before = store.account(uid)['credentials']
    mock_auth(monkeypatch, identity=before['token']['userId'], otp=True, fail_devices=True)
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        monkeypatch.setattr(runtime, '_probe_token', AsyncMock(return_value='expired'))
        job = await runtime.begin_reverify(uid, 'PRIVATE-ENTERED-PASSWORD')
        await eventually(lambda: runtime.login_status(uid)['state'] == 'verification_required')
        runtime.submit_code(uid, job['id'], '123456')
        result = await complete(runtime, uid)
        assert result['state'] == 'failed' and result['diagnostic']['stage'] == 'devices'
        assert '获取设备列表未完成' in result['message'] and '验证码已提交' in result['message']
        assert result['diagnostic']['kind'] == 'network'
        assert '123456' not in str(result) + caplog.text
        assert 'PRIVATE' not in str(result) + caplog.text
        assert store.account(uid)['credentials'] == before and store.devices(uid)[0]['enabled']
        assert len(runtime.jobs) == 1 and runtime.jobs[uid]['task'].done()
    asyncio.run(run())


def test_second_authorization_failure_has_separate_stage(account_store, monkeypatch):
    store, uid = account_store
    before = store.account(uid)['credentials']
    class Account:
        def __init__(self, *args, **kwargs):
            self.token = before['token']
            self.auth_diagnostic = {'upstream_code': 8765, 'http_status': 403, 'body': 'PRIVATE'}
        async def login(self, sid):
            return sid == 'micoapi'
    monkeypatch.setattr('platform_app.runtime.SafeAccount', Account)
    monkeypatch.setattr('platform_app.runtime.MiNAService', lambda account: SimpleNamespace(
        device_list=AsyncMock(return_value=[{'deviceID': 'speaker-a', 'hardware': 'L05B', 'name': 'Play'}])))
    async def run():
        cfg = deepcopy(CFG)
        cfg['tts'] = {'method': 'miot'}
        runtime = Runtime(store, cfg)
        monkeypatch.setattr(runtime, '_probe_token', AsyncMock(return_value='expired'))
        await runtime.begin_reverify(uid, 'synthetic')
        result = await complete(runtime, uid)
        assert result['stage'] == 'speech_auth'
        assert result['diagnostic']['upstream_code'] == 8765
        assert result['diagnostic']['http_status'] == 403
        assert '音箱播报授权未完成' in result['message'] and 'PRIVATE' not in str(result)
        assert store.account(uid)['credentials'] == before
    asyncio.run(run())


def test_diagnostic_rejects_non_numeric_provider_fields(account_store, caplog):
    store, _ = account_store
    runtime = Runtime(store, deepcopy(CFG))
    job = {'stage': 'read_auth', 'candidate': b'PRIVATE-CANDIDATE'}
    account = SimpleNamespace(auth_diagnostic={'http_status': True, 'upstream_code': 'PRIVATE-CODE', 'token': 'PRIVATE'})
    runtime._binding_failure(job, account, 'failed', '连接未完成。', 'unexpected')
    assert job['diagnostic'] == {'stage': 'read_auth', 'kind': 'unexpected'}
    assert 'candidate' not in job and 'PRIVATE' not in str(job) + caplog.text
