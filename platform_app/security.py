"""Local key custody, authenticated encryption, and platform passwords."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import re
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def password_hash(password):
    if not 12 <= len(password) <= 256:
        raise ValueError('密码长度须为12至256个字符')
    salt = secrets.token_bytes(16)
    value = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return base64.b64encode(salt + value).decode()


def password_matches(password, encoded):
    try:
        raw = base64.b64decode(encoded)
        value = hashlib.scrypt(password.encode(), salt=raw[:16], n=16384, r=8, p=1)
        return hmac.compare_digest(value, raw[16:])
    except (ValueError, TypeError):
        return False


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Vault:
    def __init__(self, key_path):
        path = Path(key_path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(AESGCM.generate_key(bit_length=256))
        path.chmod(0o600)
        key = path.read_bytes()
        self.cipher = AESGCM(key)
        self.mac_key = hashlib.sha256(b'platform-auth-v1\0' + key).digest()

    def fingerprint(self, value):
        return hmac.new(self.mac_key, value.encode(), hashlib.sha256).hexdigest()

    def seal(self, value, context):
        nonce = secrets.token_bytes(12)
        data = json.dumps(value, ensure_ascii=False).encode()
        return nonce + self.cipher.encrypt(nonce, data, context.encode())

    def open(self, value, context):
        return json.loads(self.cipher.decrypt(value[:12], value[12:], context.encode()))


def masked_account(value):
    """Display-only account label; never return full identity, password, or token."""
    value = str(value).strip()
    if len(value) <= 4:
        return '•' * max(1, len(value))
    if value.isdigit() and len(value) >= 8:
        return value[:3] + '••••' + value[-4:]
    if '@' in value:
        local, domain = value.rsplit('@', 1)
        return local[:1] + '••••@' + domain[:1] + '••••'
    return value[:2] + '••••' + value[-2:]


def normalize_email(value):
    value = value.strip()
    if len(value) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value):
        raise ValueError('请填写有效的邮箱地址')
    local, domain = value.rsplit('@', 1)
    if len(local) > 64 or '.' not in domain or '..' in value or local.startswith('.') or local.endswith('.'):
        raise ValueError('请填写有效的邮箱地址')
    # Deliberately do not strip dots or + aliases: provider-specific rules are not universal.
    return local.lower() + '@' + domain.lower()
