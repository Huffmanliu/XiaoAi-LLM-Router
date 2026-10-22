"""Transactional SMTP mail. Secrets and message contents never enter application logs."""
import asyncio
from email.message import EmailMessage
import json
import os
from pathlib import Path
import smtplib
import ssl

from .store import Rejected


class Mailer:
    def __init__(self):
        path = Path(os.environ.get('PLATFORM_MAIL_CONFIG', 'deploy/local/mail.json'))
        self.config = json.loads(path.read_text()) if path.exists() else {}

    async def send(self, recipient, subject, body):
        try:
            await asyncio.to_thread(self._send, recipient, subject, body)
        except Exception:
            raise Rejected('邮件暂时无法发送，请稍后重试或联系管理员', 503) from None

    def _send(self, recipient, subject, body):
        c = self.config
        if not all(c.get(k) for k in ('host', 'port', 'username', 'password', 'sender')):
            raise ValueError('Mail not configured')
        message = EmailMessage()
        message['From'], message['To'], message['Subject'] = c['sender'], recipient, subject
        message.set_content(body)
        context = ssl.create_default_context()
        transport = smtplib.SMTP_SSL if c.get('ssl', True) else smtplib.SMTP
        kwargs = {'timeout': 15}
        if c.get('ssl', True):
            kwargs['context'] = context
        with transport(c['host'], int(c['port']), **kwargs) as smtp:
            if not c.get('ssl', True):
                # Unencrypted authentication is never allowed.
                smtp.starttls(context=context)
            smtp.login(c['username'], c['password'])
            if smtp.send_message(message):
                raise ValueError('Mail rejected')
