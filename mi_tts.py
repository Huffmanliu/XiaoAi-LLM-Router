"""
【模块 4】mi_tts.py —— 嘴（小米 TTS 回放）
===========================================
职责：把 LLM 的回复送进小爱音箱播放。

当前实现：调用 MiService 的 TTS 接口（aiohttp POST）。

v1.1 修正点：
  - speak() 不再每句新建 aiohttp.ClientSession（旧版每句一次 TCP 握手，
    长回复 5 句就是 5 次握手，白白加延迟）。
    现在 session 由 main.py 创建一次、注入进来（胶水层共享资源，模块保持纯函数）。

二次开发钩子：
  - 换 edge-tts：speak() 里改成 edge_tts.Communicate(text, voice).save(mp3)，
    再走 MiService 的"播放音频"接口。
  - "边想边说"：main.py 消费 router.ask_stream()，每攒够一个句子就 speak() 一次。
    （v1.1 已支持：config llm.stream=true 即开）

注意：speak() 返回时播放"已提交"，不是"已播完"。
      想做语音打断（barge-in）就得在这里接 TTS 完成回调。
"""

from __future__ import annotations

import logging
import re
import ast
import asyncio
import json

import aiohttp

log = logging.getLogger("xiaoai-router.tts")

# MiService 的 TTS 接口路径（不同版本可能不同，在 config miservice.tts_path 里改）
DEFAULT_TTS_PATH = "/api/v1/tts"


class SpeechFailure(Exception):
    """Content-free diagnosis; never retain upstream exception text or responses."""

    STAGES = {'prepare': '准备播报', 'stop_music': '暂停音箱播放', 'device_list': '读取音箱设备',
              'device_prepare': '准备音箱播报参数', 'miot_submit': '提交音箱播报指令',
              'mina_submit': '提交音箱播报指令', 'wait': '等待播报间隔'}
    KINDS = {'request_error': '小米接口未完成请求', 'rejected': '小米接口未接受指令',
             'timeout': '请求超时', 'connection': '网络连接异常',
             'verification': '小米账号需要验证', 'device_missing': '未找到目标音箱',
             'device_metadata': '设备信息不完整', 'invalid_response': '接口返回格式异常'}

    def __init__(self, stage='prepare', kind='request_error', code=None, http_status=None):
        self.stage = stage if isinstance(stage, str) and stage in self.STAGES else 'prepare'
        self.kind = kind if isinstance(kind, str) and kind in self.KINDS else 'request_error'
        self.code = code if type(code) is int and -(2**31) <= code < 2**31 else None
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.chunk_index = self.total_chunks = self.submitted_chunks = 0
        super().__init__(self.STAGES[self.stage] + '失败：' + self.KINDS[self.kind])

    @classmethod
    def from_exception(cls, stage, exc):
        if isinstance(exc, cls):
            return exc
        kind = 'request_error'
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            kind = 'timeout'
        elif isinstance(exc, (aiohttp.ClientConnectionError, OSError)):
            kind = 'connection'
        elif type(exc).__name__ == 'VerificationRequired':
            kind = 'verification'
        code = getattr(exc, 'code', None)
        # MiService wraps responses in a generic Exception. Parse only its known
        # official endpoint envelope in memory; retain solely a top-level integer.
        raw = str(exc)
        if len(raw) <= 16384 and raw.startswith(('Error https://api2.mina.mi.com/', 'Error https://api.io.mi.com/')):
            payload = raw.partition(': ')[2]
            try:
                try:
                    response = json.loads(payload)
                except ValueError:
                    response = ast.literal_eval(payload)
                if type(response) is dict:
                    code = response.get('code')
            except (ValueError, SyntaxError, RecursionError):
                pass
        return cls(stage, kind, code, getattr(exc, 'status', getattr(exc, 'status_code', None)))

    def metadata(self):
        keys = ('stage', 'kind', 'code', 'http_status', 'chunk_index', 'total_chunks', 'submitted_chunks')
        return {key: getattr(self, key) for key in keys}

    @property
    def message(self):
        message = str(self)
        if self.chunk_index:
            message = f'第{self.chunk_index}/{self.total_chunks}段：' + message
        if self.submitted_chunks:
            message += f'；此前{self.submitted_chunks}段指令已提交，不代表已播放完毕'
        if self.code is not None:
            message += f'（小米错误码 {self.code}）'
        if self.http_status is not None:
            message += f'（HTTP {self.http_status}）'
        return message


def public_speech_diagnostic(value):
    """Validate stored metadata again before exposing it to a browser."""
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return None
    if (not isinstance(data, dict) or not isinstance(data.get('stage'), str)
            or not isinstance(data.get('kind'), str) or data['stage'] not in SpeechFailure.STAGES
            or data['kind'] not in SpeechFailure.KINDS):
        return None
    error = SpeechFailure(data['stage'], data['kind'], data.get('code'), data.get('http_status'))
    for key in ('chunk_index', 'total_chunks', 'submitted_chunks'):
        n = data.get(key, 0)
        setattr(error, key, n if type(n) is int and 0 <= n <= 10000 else 0)
    return {**error.metadata(), 'message': error.message}


def split_sentences(text: str, max_chars: int = 60) -> list[str]:
    """把长回复切成"短句"，逐句播 → 模拟真人换气停顿。

    规则（极简）：
      1. 先按句号 / 问号 / 感叹号 / 换行切
      2. 切完的单句仍 > max_chars → 再按逗号切
      3. 空串丢弃
    二次开发：接个分句库（jieba + 标点规则）效果更好。
    """
    text = (text or "").strip()
    if not text:
        return []

    out: list[str] = []
    for p in re.split(r"(?<=[。？！\n])", text):
        p = p.strip()
        if not p:
            continue
        if len(p) <= max_chars:
            out.append(p)
            continue
        while len(p) > max_chars:
            cut = max(p.rfind(mark, 0, max_chars) for mark in "，、；, ") + 1
            cut = cut if cut > max_chars // 2 else max_chars
            out.append(p[:cut].strip())
            p = p[cut:].strip()
        if p:
            out.append(p)
    return out


def speech_chunks(text: str, max_chars: int = 60) -> list[str]:
    """Group sentence fragments into bounded commands, avoiding one-character chunks."""
    max_chars = max(10, int(max_chars))
    chunks = []
    for sentence in split_sentences(text, max_chars):
        if chunks and len(chunks[-1]) + len(sentence) + 1 <= max_chars:
            chunks[-1] += " " + sentence
        else:
            chunks.append(sentence)
    return chunks


class Speaker:
    """TTS 播放器：持有一个共享 aiohttp.ClientSession（由 main.py 注入）。

    用法：
        sp = Speaker(cfg, http_session)
        await sp.speak(device_id, "你好")
    """

    def __init__(self, cfg: dict, session: aiohttp.ClientSession):
        self.cfg = cfg
        self.session = session

    def _url(self) -> str:
        m = self.cfg.get("miservice", {}) or {}
        base = (m.get("base_url", "http://localhost:8080")).rstrip("/")
        return f"{base}{m.get('tts_path', DEFAULT_TTS_PATH)}"

    async def speak(self, device_id: str, text: str) -> None:
        """让指定设备念出 text。失败只打日志（不抛），
        因为"念不出来"不该中断整个对话流程。"""
        text = (text or "").strip()
        if not text:
            return
        payload = {"device": device_id, "text": text}
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with self.session.post(self._url(), json=payload, timeout=timeout) as resp:
                if resp.status >= 400:
                    log.error("TTS failed: HTTP %s", resp.status)
                else:
                    log.info(
                        "tts submitted → %s",
                        device_id,
                    )
        except aiohttp.ClientError as e:
            log.error("TTS unreachable (%s)", type(e).__name__)

    async def speak_chunks(self, device_id: str, answer: str, max_chars: int = 60) -> None:
        """把完整回复切成短句逐句播（main.py 非流式路径走这里）。"""
        for chunk in split_sentences(answer, max_chars):
            await self.speak(device_id, chunk)
