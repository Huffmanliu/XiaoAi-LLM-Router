"""Voluntary support feedback, isolated from chat history and credentials."""
import math
import secrets
import time
from typing import Literal

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .store import Rejected

Category = Literal['problem', 'suggestion', 'device', 'other']
Status = Literal['pending', 'processing', 'replied', 'closed']
PAGE_SIZE = 20


class Submission(BaseModel):
    model_config = ConfigDict(extra='forbid')
    submission_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    category: Category
    content: str = Field(min_length=1, max_length=2000)
    device_id: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator('content')
    @classmethod
    def clean_content(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('请填写反馈内容')
        return value


class Handling(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1, strict=True)
    status: Status
    reply: str = Field(max_length=2000)

    @field_validator('reply')
    @classmethod
    def clean_reply(cls, value):
        return value.strip()


class Feedback:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, submission_id TEXT NOT NULL,
                    category TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    created REAL NOT NULL, updated REAL NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    content BLOB NOT NULL, reply BLOB NOT NULL,
                    UNIQUE(user_id,submission_id));
                CREATE INDEX IF NOT EXISTS feedback_owner_time ON feedback(user_id,created DESC,id DESC);
                CREATE INDEX IF NOT EXISTS feedback_status_time ON feedback(status,created DESC,id DESC);
            ''')

    def public(self, row, administrator=False):
        value = {key: row[key] for key in ('id', 'category', 'status', 'created', 'updated', 'version')}
        value.update(self.store.vault.open(row['content'], 'feedback:' + row['id']))
        value['reply'] = self.store.vault.open(row['reply'], 'feedback-reply:' + row['id'])
        if administrator:
            value['username'] = row['username']
        return value

    def create(self, uid, data):
        now = time.time()
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM feedback WHERE user_id=? AND submission_id=?',
                             (uid, data.submission_id)).fetchone()
            if old:
                value = self.public(old)
                if (value['category'], value['content'], (value['device'] or {}).get('id')) != (
                        data.category, data.content, data.device_id):
                    raise Rejected('该次提交内容已变化，请重新提交', 409)
                return value
            count = db.execute('SELECT COUNT(*) FROM feedback WHERE user_id=? AND created>?',
                               (uid, now - 3600)).fetchone()[0]
            if count >= 5:
                raise Rejected('每小时最多提交5条反馈，请稍后再试', 429)
            device = None
            if data.device_id:
                row = db.execute('SELECT id,name,hardware FROM devices WHERE user_id=? AND id=? AND present=1',
                                 (uid, data.device_id)).fetchone()
                if not row:
                    raise Rejected('请选择你当前设备列表中的设备', 403)
                device = dict(row)
            fid = secrets.token_hex(16)
            content = self.store.vault.seal({'content': data.content, 'device': device}, 'feedback:' + fid)
            reply = self.store.vault.seal('', 'feedback-reply:' + fid)
            db.execute('INSERT INTO feedback(id,user_id,submission_id,category,created,updated,content,reply) '
                       'VALUES(?,?,?,?,?,?,?,?)', (fid, uid, data.submission_id, data.category, now, now, content, reply))
            return self.public(db.execute('SELECT * FROM feedback WHERE id=?', (fid,)).fetchone())

    def listing(self, uid=None, *, status=None, page=1):
        conditions, args = [], []
        if uid is not None:
            conditions.append('f.user_id=?')
            args.append(uid)
        if status is not None:
            conditions.append('f.status=?')
            args.append(status)
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        with self.store.db() as db:
            db.execute('BEGIN')
            total = db.execute('SELECT COUNT(*) FROM feedback f' + where, args).fetchone()[0]
            pages = max(1, math.ceil(total / PAGE_SIZE))
            page = min(page, pages)
            rows = db.execute('SELECT f.*,u.username FROM feedback f JOIN users u ON u.id=f.user_id' + where
                              + ' ORDER BY f.created DESC,f.id DESC LIMIT ? OFFSET ?',
                              (*args, PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
            return {'items': [self.public(row, uid is None) for row in rows], 'total': total,
                    'page': page, 'pages': pages}

    def handle(self, fid, data):
        if data.status == 'replied' and not data.reply:
            raise Rejected('标记为已回复前，请填写给用户的回复')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM feedback WHERE id=?', (fid,)).fetchone()
            if not row:
                raise Rejected('反馈不存在', 404)
            if row['version'] != data.version:
                raise Rejected('这条反馈已被更新，请刷新列表后核对；当前输入已保留', 409)
            db.execute('UPDATE feedback SET status=?,reply=?,updated=?,version=version+1 WHERE id=?',
                       (data.status, self.store.vault.seal(data.reply, 'feedback-reply:' + fid), time.time(), fid))
        return {'ok': True}

    def install(self, app, current, admin):
        @app.get('/api/feedback')
        def mine(page: int = Query(default=1, ge=1, le=1000000), user=Depends(current)):
            return self.listing(user['user_id'], page=page)

        @app.post('/api/feedback')
        def submit(data: Submission, user=Depends(current)):
            return self.create(user['user_id'], data)

        @app.get('/api/admin/feedback')
        def inbox(status: Status | None = None, page: int = Query(default=1, ge=1, le=1000000),
                  user=Depends(admin)):
            return self.listing(status=status, page=page)

        @app.put('/api/admin/feedback/{fid}')
        def handle(fid: str, data: Handling, user=Depends(admin)):
            return self.handle(fid, data)
