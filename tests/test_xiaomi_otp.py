"""Synthetic identity responses only: no real OTP delivery or credentials."""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miservice import MiAccount

from platform_app.runtime import Runtime, SafeAccount, VerificationRequired
from platform_app.xiaomi_otp import masked_recipient, observe_response, recipient_method
from test_platform import CFG, PASSWORD, store as store_fixture

store = store_fixture


@pytest.mark.parametrize('data,method,expected', [
    ({'phone': '+86 19900001234'}, 'Phone', '尾号 1234 的手机'),
    ({'data': {'maskedPhone': '199****5678'}}, 'Phone', '尾号 5678 的手机'),
    ({'maskedEmail': 't****@example.com'}, 'Email', 't***@example.com'),
    ({'data': {'email': 'synthetic@example.com'}}, 'Email', 's***@example.com'),
    ({'phone': '199****12'}, 'Phone', ''),
    ({'userId': '19900001234', 'message': 'Phone: 19900001234'}, 'Phone', ''),
    ({'phone': '19900001234'}, 'Email', ''),
    ({'email': '<script>@example.com'}, 'Email', ''),
    ({'phone': ['19900001234']}, 'Phone', ''),
    ({'data': None}, 'Phone', ''),
])
def test_only_explicit_fields_are_masked(data, method, expected):
    assert masked_recipient(data, method) == expected


@pytest.mark.parametrize('url,method', [
    ('https://example.com/identity/list', 'GET'),
    ('http://account.xiaomi.com/identity/list', 'GET'),
    ('https://account.xiaomi.com.evil.test/identity/list', 'GET'),
    ('https://account.xiaomi.com/identity/auth/verifyPhone', 'POST'),
    ('https://account.xiaomi.com/other', 'GET'),
])
def test_no_identity_guessing_from_unrelated_endpoints(url, method):
    assert recipient_method(url, method) is None


@pytest.mark.parametrize('status,raw', [
    (401, b'{"phone":"19900001234"}'),
    (200, b'{"code":1,"phone":"19900001234"}'),
    (200, b'<html>19900001234</html>'),
    (200, b'{"userId":"19900001234"}'),
    (200, b'{"data":{"unknownPhoneField":"19900001234"}}'),
    (200, b' ' * 65537),
])
def test_unavailable_or_unreliable_destination_stays_unknown(status, raw):
    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def read(self):
            return raw

    async def run():
        response = Response()
        response.status = status
        recipients = {}
        async with observe_response(response, recipients, 'Phone') as observed:
            assert await observed.read() == raw
        assert recipients == {}
    asyncio.run(run())


@pytest.mark.parametrize('channel', ['Phone', 'Email'])
def test_existing_dependency_flow_captures_hint_without_extra_send(monkeypatch, channel):
    calls, seen = [], []

    class Response:
        status = 200

        def __init__(self, data):
            self.raw = json.dumps(data).encode()
            self.reads = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def read(self):
            self.reads += 1
            assert self.reads == 1  # The observer must not consume a second copy.
            return self.raw

    responses = []

    def request(self, url, method='GET', **kwargs):
        calls.append((url, method))
        if '/identity/list?' in url:
            data = {'flag': 4 if channel == 'Phone' else 8}
        elif method == 'GET' and f'/verify{channel}?' in url:
            # The second service challenge has no recipient: do not reuse the first hint.
            data = {'code': 0, 'data': {'phone': '19900001234', 'email': 'test@example.com'}
                    if len(seen) == 0 else {}}
        elif method == 'POST' and f'/verify{channel}?' in url:
            data = {'location': '/resume'}
        else:
            data = {'code': 0}
        response = Response(data)
        responses.append(response)
        return response

    monkeypatch.setattr(MiAccount, 'request', request)

    async def run():
        async def otp(method):
            seen.append((method, account.otp_recipients.copy()))
            return '123456'
        account = SafeAccount(SimpleNamespace(), 'different-login', 'synthetic', otp_callback=otp, interactive=True)
        account.token = {'deviceId': 'synthetic-device'}
        account._serviceLogin = AsyncMock(return_value={'code': 0})
        for sid in ('micoapi', 'xiaomiio'):
            assert await account._verify_otp(sid, '/start?context=synthetic') == {'code': 0}
            assert account.otp_recipients is None
        assert seen[0][1][channel] == ('尾号 1234 的手机' if channel == 'Phone' else 't***@example.com')
        assert seen[1][1] == {}
    asyncio.run(run())
    assert len(calls) == (12 if channel == 'Phone' else 10)
    assert sum('/sendPhoneTicket' in url for url, _ in calls) == (2 if channel == 'Phone' else 0)
    assert all(response.reads == 1 for response in responses)


def test_cancel_resets_recipient_and_unattended_never_sends(monkeypatch):
    async def cancelled(self, sid, ntf):
        assert self.otp_recipients == {}
        self.otp_recipients['Phone'] = '尾号 1234 的手机'
        raise asyncio.CancelledError
    monkeypatch.setattr(MiAccount, '_verify_otp', cancelled)

    async def run():
        account = SafeAccount(SimpleNamespace(), 'synthetic', 'synthetic', interactive=True)
        with pytest.raises(asyncio.CancelledError):
            await account._verify_otp('micoapi', '/start')
        assert account.otp_recipients is None
        account.interactive = False
        with pytest.raises(VerificationRequired):
            await account._verify_otp('micoapi', '/start')
    asyncio.run(run())


def test_status_transmits_only_current_masked_destination(store, monkeypatch):
    import platform_app.runtime as module

    class Account:
        def __init__(self, *args, otp_callback, **kwargs):
            self.callback = otp_callback
            self.otp_recipients = {'Phone': '尾号 1234 的手机'}
            self.token = {'userId': 'synthetic-id', 'micoapi': ['synthetic', 'synthetic']}

        async def login(self, sid):
            await self.callback('Phone')
            return True

    monkeypatch.setattr(module, 'SafeAccount', Account)
    monkeypatch.setattr(module, 'MiNAService', lambda a: SimpleNamespace(device_list=AsyncMock(return_value=[])))

    async def run():
        uid = store.create_user('otp-user', PASSWORD)
        runtime = Runtime(store, deepcopy(CFG))
        job = runtime.begin_login(uid, 'different-login', 'synthetic-password')
        for _ in range(20):
            await asyncio.sleep(0)
            if runtime.login_status(uid)['state'] == 'verification_required':
                break
        status = runtime.login_status(uid)
        assert status['recipient'] == '尾号 1234 的手机'
        assert 'different-login' not in json.dumps(status)
        assert 'synthetic-password' not in json.dumps(status)
        runtime.submit_code(uid, job['id'], '123456')
        await runtime.jobs[uid]['task']
        assert 'recipient' not in runtime.login_status(uid)
    asyncio.run(run())
