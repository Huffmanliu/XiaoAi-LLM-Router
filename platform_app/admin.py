"""Private maintenance CLI: idempotent legacy metadata import and content-free backup."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from .auth import Auth
from .security import Vault
from .store import CST, Store


def import_usage(store, username, path):
    with store.db() as db:
        user = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
    if not user:
        raise ValueError('平台用户不存在，请先通过邀请创建')
    imported = skipped = 0
    with Path(path).open() as source:
        for line in source:
            try:
                row = json.loads(line)
                timestamp = datetime.fromisoformat(row['timestamp'])
                if timestamp.tzinfo is None:
                    raise ValueError('Timestamp must include timezone')
                created = timestamp.timestamp()
                fingerprint = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
                day = datetime.fromtimestamp(created, CST).date().isoformat()
                known = row.get('usage_available') or row.get('total_tokens') is not None
                with store.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if not db.execute('INSERT OR IGNORE INTO imports VALUES(?)', (fingerprint,)).rowcount:
                        skipped += 1
                        continue
                    status = 'completed' if row.get('status') == 'completed' else 'failed'
                    db.execute('''INSERT INTO requests(id,user_id,device_id,source,request_id,created,
                        day,status,started,model,prompt_tokens,completion_tokens,total_tokens,
                        cache_hit_tokens,cache_miss_tokens,elapsed_ms,finish_reason,speech_status)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                               ('import-' + fingerprint, user[0], row.get('device_id'), 'legacy', fingerprint,
                                created, day, status, 1, row.get('model'), row.get('prompt_tokens') if known else None,
                                row.get('completion_tokens') if known else None, row.get('total_tokens') if known else None,
                                row.get('prompt_cache_hit_tokens'), row.get('prompt_cache_miss_tokens'),
                                row.get('elapsed_ms'), row.get('finish_reason'), 'unknown'))
                imported += 1
            except (ValueError, KeyError, TypeError):
                skipped += 1
    return {'imported': imported, 'skipped': skipped}


def backup(store, directory):
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.chmod(0o700)
    path = target / ('platform-' + str(time.time_ns()) + '.sqlite3')
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with store.db() as src, sqlite3.connect(path) as dst:
        src.backup(dst)
        dst.execute('DELETE FROM sessions')
        dst.execute('DELETE FROM invites')
        for table in ('session_security', 'auth_challenges', 'auth_limits', 'mail_outbox', 'trusted_browsers'):
            dst.execute('DELETE FROM ' + table)
        dst.commit()
    return path


def main():
    parser = argparse.ArgumentParser(description='平台私有维护工具，不输出凭据或问答内容')
    parser.add_argument('--state-dir', default='deploy/local/platform')
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('import-usage')
    command.add_argument('--username', required=True)
    command.add_argument('--path', required=True)
    command = commands.add_parser('set-recovery-email', help='线下确认旧账号邮箱归属后预设；下次登录仍需验证邮箱')
    command.add_argument('--username', required=True)
    command.add_argument('--email', required=True)
    command = commands.add_parser('backup')
    command.add_argument('--directory', required=True)
    args = parser.parse_args()
    root = Path(args.state_dir)
    key = Path(os.environ.get('PLATFORM_KEY_FILE', str(root.parent / 'platform.key')))
    if not key.exists() or not (root / 'platform.sqlite3').exists():
        parser.error('平台尚未初始化，或加密密钥不存在')
    store = Store(root, Vault(key))
    if args.command == 'import-usage':
        print(json.dumps(import_usage(store, args.username, args.path)))
    elif args.command == 'set-recovery-email':
        with store.db() as db:
            user = db.execute('SELECT id FROM users WHERE username=?', (args.username,)).fetchone()
        if not user:
            parser.error('平台用户不存在')
        Auth(store).set_legacy_email(user['id'], args.email)
        print('已预设安全邮箱；用户下次登录须完成邮件验证，不会直接获得访问权限。')
    else:
        print('已创建不含问答历史的配置与用量备份：' + str(backup(store, args.directory)))


if __name__ == '__main__':
    main()
