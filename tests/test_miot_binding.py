"""Read and speech authorizations must both be usable in MIoT mode."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from platform_app.runtime import Runtime
from test_account_lifecycle import account_store as account_store_fixture, complete
from test_platform import CFG

account_store = account_store_fixture


@pytest.mark.parametrize('status,code,expected', [(401, 3, 'expired'), (503, 3, 'unknown'), (200, 0, 'valid')])
def test_read_channel_success_does_not_hide_speech_authorization_failure(account_store, monkeypatch, status, code, expected):
    store, uid = account_store
    calls = []
    class Response:
        def __init__(self, status, code):
            self.status, self.code = status, code
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def json(self, **kwargs):
            return {'code': self.code}
    class Session:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def get(self, url, **kwargs):
            calls.append(('GET', url))
            assert not kwargs['allow_redirects']
            return Response(200, 0)
        def post(self, url, **kwargs):
            calls.append(('POST', url))
            assert not kwargs['allow_redirects'] and kwargs['data']['signature']
            assert kwargs['cookies']['serviceToken'] == 'synthetic-miot-session'
            return Response(status, code)
    monkeypatch.setattr('platform_app.runtime.aiohttp.ClientSession', Session)
    monkeypatch.setattr('platform_app.runtime.SafeAccount', lambda *a, **k: pytest.fail('Unexpected login'))
    async def run():
        cfg = deepcopy(CFG)
        cfg['tts']['method'] = 'miot'
        runtime = Runtime(store, cfg)
        token = store.account(uid)['credentials']['token']
        token.update(deviceId='synthetic-client', xiaomiio=['c2VjdXJpdHk=', 'synthetic-miot-session'])
        assert await runtime._probe_token(uid, token) == expected
        assert calls == [('GET', 'https://api2.mina.mi.com/admin/v2/device_list'),
                         ('GET', 'https://userprofile.mina.mi.com/device_profile/v2/conversation'),
                         ('POST', 'https://api.io.mi.com/app/home/device_list')]
        assert not runtime.jobs
    asyncio.run(run())


def test_missing_speech_token_does_not_attempt_login_or_hardware_action(account_store):
    store, uid = account_store
    async def run():
        runtime = Runtime(store, deepcopy(CFG))
        assert await runtime._probe_miot_token(None, {'userId': 'synthetic'}) == 'expired'
        assert not runtime.jobs
    asyncio.run(run())


@pytest.mark.parametrize('result,identity,expected', [
    ('valid', 'original-xiaomi-id', 'completed'),
    ('unknown', 'original-xiaomi-id', 'failed'),
    ('expired', 'original-xiaomi-id', 'failed'),
    ('valid', 'different-xiaomi-id', 'failed'),
])
def test_rebind_validates_both_services_and_keeps_old_binding_on_failure(
        account_store, monkeypatch, result, identity, expected):
    store, uid = account_store
    before = store.account(uid)['credentials']
    services = []
    class Account:
        def __init__(self, session, username, password, **kwargs):
            self.token = {'userId': 'original-xiaomi-id', 'deviceId': 'synthetic-client'}
        async def login(self, sid):
            services.append(sid)
            self.token[sid] = ['c2VjdXJpdHk=', 'synthetic-session']
            if sid == 'xiaomiio':
                self.token['userId'] = identity
            return True
    monkeypatch.setattr('platform_app.runtime.SafeAccount', Account)
    monkeypatch.setattr('platform_app.runtime.MiNAService', lambda account: SimpleNamespace(device_list=AsyncMock(
        return_value=[{'deviceID': 'speaker-a', 'hardware': 'L05B', 'name': 'Synthetic Play'}])))
    async def run():
        cfg = deepcopy(CFG)
        cfg['tts']['method'] = 'miot'
        runtime = Runtime(store, cfg)
        monkeypatch.setattr(runtime, '_probe_token', AsyncMock(return_value='expired'))
        probe = AsyncMock(return_value=result)
        monkeypatch.setattr(runtime, '_probe_miot_token', probe)
        await runtime.begin_reverify(uid, 'synthetic-new-password')
        assert (await complete(runtime, uid))['state'] == expected
        assert services == ['micoapi', 'xiaomiio']
        assert store.devices(uid)[0]['enabled']
        if expected == 'completed':
            assert store.account(uid)['credentials']['token'].get('xiaomiio')
            assert store.account(uid)['credentials']['token'].get('micoapi')
        else:
            assert store.account(uid)['credentials'] == before
        if identity != 'original-xiaomi-id':
            probe.assert_not_awaited()
    asyncio.run(run())
