"""Best-effort recipient hints from existing Xiaomi verification responses only."""
import re
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from miservice.miaccount import parse_resp


def masked_recipient(data, method):
    """Accept explicit recipient fields, never arbitrary response text or identities."""
    if not isinstance(data, dict):
        return ''
    fields = ('maskedPhone', 'phone') if method == 'Phone' else ('maskedEmail', 'email')
    for item in (data, data.get('data')):
        if not isinstance(item, dict):
            continue
        for field in fields:
            value = item.get(field)
            if not isinstance(value, str) or len(value) > 254:
                continue
            value = value.strip()
            if method == 'Phone':
                # Full or masked phone numbers; only the final four visible digits leave here.
                if re.fullmatch(r'\+?[\d*•xX() -]{7,32}', value) and re.search(r'[0-9]{4}$', value):
                    return '尾号 ' + value[-4:] + ' 的手机'
            elif method == 'Email':
                match = re.fullmatch(r'([A-Za-z0-9_*.+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)', value)
                if match:
                    return match[1][0] + '***@' + match[2].lower()
    return ''


def recipient_method(url, method):
    """Only exact, existing Xiaomi identity metadata endpoints may contribute."""
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != 'account.xiaomi.com':
        return None
    if method == 'GET' and parsed.path == '/identity/list':
        return 'list'
    if method == 'GET' and parsed.path == '/identity/auth/verifyPhone':
        return 'Phone'
    if method == 'GET' and parsed.path == '/identity/auth/verifyEmail':
        return 'Email'
    if method == 'POST' and parsed.path == '/identity/auth/sendPhoneTicket':
        return 'Phone'
    return None


@asynccontextmanager
async def observe_response(request, recipients, method):
    """Observe the dependency's existing read, without an extra request or stream read."""
    async with request as response:
        class ObservedResponse:
            def __getattr__(self, name):
                return getattr(response, name)

            async def read(self):
                raw = await response.read()
                if response.status == 200 and isinstance(raw, bytes) and len(raw) <= 65536:
                    try:
                        data = parse_resp(raw)
                    except (ValueError, UnicodeError, TypeError):
                        data = None
                    if isinstance(data, dict) and data.get('code') in (None, 0):
                        for channel in (('Phone', 'Email') if method == 'list' else (method,)):
                            hint = masked_recipient(data, channel)
                            if hint:
                                recipients[channel] = hint
                return raw
        yield ObservedResponse()
