"""
【模块 1b】mi_conversation.py —— 机型无关收音 + 设备自检（基于 MiService Python 库）
=================================================================================

为什么单独一个模块？
  v1.1 押注 MiService 的 MQTT Bridge（可选插件，topic 版本漂移、默认不开）。
  v1.2 改用整个生态（xiaogpt / mi-gpt）验证过的最通用方案：
  直接依赖 PyPI 上的 MiService Python 库，用它现成的：
    - MiNAService.device_list()    列出账号下所有小爱音箱（含 hardware 型号）
    - MiNAService.get_latest_ask() 拦截小爱最近一轮"用户 query + 小爱回复"
    - MiNAService.text_to_speech() 让指定音箱念出 LLM 回复
  三个能力串起来 = 机型无关的收音源 + TTS 嘴。

与 miservice_glue.py（模块 1）的关系：
  - 本模块（conversation）是默认收音源，依赖 MiService 库
  - 模块 1（poll/mqtt）是 fallback，不依赖 MiService 库（纯 HTTP/MQTT）
  - config miservice.ingest: conversation | poll | auto（auto = 先 conversation，挂了落 poll）

机型指令表 HARDWARE_COMMAND_DICT（v1.2 内置）：
  来源：mi-gpt 官方兼容表 + xiaogpt config.py，两者互相印证。
  用途：启动时查 device_list 拿到 hardware 字段，对号入座判断档位；
  查不到的机型走默认指令 + warning，提示用户贡献回仓库（一行配置）。

二次开发：
  - 加新机型：在 HARDWARE_COMMAND_DICT 里加一行 "LXXX": ("5-1","5-5") 即可
  - 换 TTS 后端（edge-tts 生成 mp3 再 play_by_url）：改 MiSpeaker.tts() 一处
"""

from __future__ import annotations

import logging
import os
import json
import time
import asyncio
from mi_tts import speech_chunks, SpeechFailure

log = logging.getLogger("xiaoai-router.mina")

# ---------------------------------------------------------------------------
# 机型指令表：hardware → (tts_command, wakeup_command, supports_stream)
#   tts_command      : 播报 TTS 的 MIoT 指令（如 "5-1" / "7-3" / "3-1"）
#   wakeup_command   : 唤醒/打断指令（部分机型用）
#   supports_stream  : 是否支持"连续对话"（streamResponse，L06A 这档为 False）
# 数据核对：mi-gpt docs/compatibility.md + xiaogpt config.py HARDWARE_COMMAND_DICT
# ---------------------------------------------------------------------------
HARDWARE_COMMAND_DICT: dict[str, tuple[str, str, bool]] = {
    # ---------- ✅ 完美支持（含实验性连续对话） ----------
    "OH2P": ("7-3", "7-1", True),
    "OH2": ("5-3", "5-1", True),
    "LX06": ("5-1", "5-3", True),
    "S12": ("5-1", "5-3", True),
    "S12A": ("5-1", "5-5", True),
    "L15A": ("7-3", "7-1", True),
    "LX5A": ("5-1", "5-5", True),
    "LX05": ("5-1", "5-3", True),
    "LX05A": ("5-1", "5-5", True),
    "X10A": ("7-3", "7-1", True),
    "L17A": ("7-3", "7-1", True),
    # ---------- 🚗 正常支持（TTS 问答链路完整；关 streamResponse） ----------
    "L06A": ("5-1", "5-5", False),  # 小爱音箱 / Redmi 小爱音箱（零售 L607/L607A）
    "LX01": ("5-1", "5-5", False),
    "L05B": ("5-3", "5-1", False),
    "L05C": ("5-3", "5-4", False),
    "L07A": ("5-1", "5-5", False),
    "L09A": ("3-1", "3-2", False),
    "LX04": ("5-1", "5-4", False),
    "X4B": ("5-3", "5-1", False),
    "ASX4B": ("5-3", "5-1", False),
    "X6A": ("7-3", "7-1", False),
    "X08E": ("7-3", "7-1", False),
    "X8F": ("7-3", "7-1", False),
}

# 完全不支持（云协议层不开放 / 纯蓝牙无云小爱）—— 启动自检时直接提示换机型
UNSUPPORTED_HARDWARE: dict[str, str] = {
    "SM4": "小米小爱音箱 HD：云协议层不开放",
    # 纯蓝牙随身版没有固定 hardware 编码，靠"不在 device_list 里"识别
}

DEFAULT_COMMAND: tuple[str, str, bool] = ("5-1", "5-5", False)


def tier_of(hardware: str) -> dict:
    """返回机型档位信息（供 --list-devices 打印 / 启动自检用）。

    返回: {"hardware":..., "tier": "perfect|normal|unknown|unsupported",
           "tts":..., "wakeup":..., "stream":..., "note":...}
    """
    hw = (hardware or "").upper().strip()
    if hw in UNSUPPORTED_HARDWARE:
        return {"hardware": hw, "tier": "unsupported", "note": UNSUPPORTED_HARDWARE[hw]}
    if hw in HARDWARE_COMMAND_DICT:
        tts, wk, stream = HARDWARE_COMMAND_DICT[hw]
        tier = "perfect" if stream else "normal"
        return {
            "hardware": hw,
            "tier": tier,
            "tts": tts,
            "wakeup": wk,
            "stream": stream,
            "note": "",
        }
    tts, wk, _ = DEFAULT_COMMAND
    return {
        "hardware": hw,
        "tier": "unknown",
        "tts": tts,
        "wakeup": wk,
        "stream": False,
        "note": "指令表未收录，按默认 5-1 走；可贡献 PR 补进 HARDWARE_COMMAND_DICT",
    }


# ---------------------------------------------------------------------------
# MiNA 封装：把 MiService 库的 MiAccount + MiNAService 粘成"音箱控制器"
# ---------------------------------------------------------------------------
class MiSpeaker:
    """一个小米账号 + 目标音箱，负责 收音(get_latest_ask) + 播报(text_to_speech)。

    用法（main.py 注入）：
        sp = await MiSpeaker.connect(cfg)
        msg = await sp.latest_ask()          # → {"text","device_id","request_id","ts"} | None
        await sp.tts(device_id, "回复文本")

    所有方法都是 async（MiService 库本身是 asyncio 实现）。
    二次开发：换 TTS 后端（edge-tts 生成 mp3 + play_by_url）只改 tts() 一处。
    """

    def __init__(self, account, mina, cfg: dict):
        self.account = account
        self.mina = mina
        self.cfg = cfg
        self._hardware = {}
        self._miot_devices = {}

    @classmethod
    async def connect(cls, cfg: dict, session=None) -> "MiSpeaker":
        """登录小米账号，建 MiNAService。失败抛 RuntimeError（main 兜底）。"""
        import miservice  # 延迟 import：poll-only 部署不必装 miservice
        import aiohttp

        mi = cfg.get("mi", {}) or {}
        user = mi.get("username") or ""
        pwd = mi.get("password") or ""
        if not user or not pwd:
            raise RuntimeError(
                "小米账号未配置：请设置 MI_USER / MI_PASS（.env 或 config.yaml 的 mi 段）。"
                "跑 --list-devices 前需要能登录小米云。"
            )

        owned_session = session is None
        if owned_session:
            session = aiohttp.ClientSession()
        try:
            token_path = os.path.expanduser(mi.get("token_path", "~/.mi.token"))
            account = miservice.MiAccount(session, user, pwd, token_store=token_path)
            # 登录小爱云（sid=micoapi），serviceToken 落 ~/.mi.token 可复用
            account.token = await account.token_store.load_token()
            ok = bool(account.token and account.token.get("micoapi"))
            if not ok:
                ok = await account.login("micoapi")
            if not ok:
                raise RuntimeError("小米账号登录失败（检查账号密码 / 是否需 OTP）")
            mina = miservice.MiNAService(account)
            speaker = cls(account, mina, cfg)
            speaker._owned_session = session if owned_session else None
            return speaker
        except BaseException:
            if owned_session:
                await session.close()
            raise

    async def close(self):
        if getattr(self, "_owned_session", None) is not None:
            await self._owned_session.close()

    # ---- 收音：拦截小爱最近一轮问答 ----
    async def latest_ask(self, device_id: str | None = None) -> dict | None:
        """Legacy CLI contract; the platform uses rich batches via recent_asks."""
        items = await self.recent_asks(device_id)
        if not items:
            return None
        latest = max(items, key=lambda item: item['ts'])
        return {key: latest[key] for key in ('text', 'device_id', 'request_id', 'ts')}

    async def recent_asks(self, device_id: str | None = None) -> list[dict]:
        """Recent user queries and native answer text; never log or persist the raw records."""
        did = device_id or self.cfg.get('mi', {}).get('device_id') or await self._first_device()
        if not did:
            return []
        if self.cfg.get('mi', {}).get('conversation_source') == 'cloud':
            return await self._cloud_recent_asks(did)
        messages = await self.mina.get_latest_ask(did) or []
        result = []
        for message in messages:
            answers = (message.get('response') or {}).get('answer') or []
            question = next((a.get('question') for a in answers if a.get('question')), '')
            if question:
                result.append({'text': question.strip(), 'device_id': did,
                               'request_id': message.get('request_id', ''),
                               'ts': message.get('timestamp_ms', 0) / 1000.0,
                               'native_answer': ''.join(a.get('content', '') for a in answers
                                                        if isinstance(a.get('content'), str))})
        return result

    async def _cloud_latest_ask(self, device_id):
        items = await self._cloud_recent_asks(device_id)
        return max(items, key=lambda item: item['ts']) if items else None

    async def _cloud_recent_asks(self, device_id):
        """Read the app's conversation history on devices with empty NLP buffers."""
        import aiohttp

        if device_id not in self._hardware:
            rows = await self.mina.device_list() or []
            self._hardware = {
                d["deviceID"]: d["hardware"]
                for d in rows
                if d.get("deviceID") and d.get("hardware")
            }
        hardware = self._hardware.get(device_id)
        if not hardware:
            raise RuntimeError("Configured speaker not found in Xiaomi account")
        for attempt in range(2):
            token = self.account.token
            cookies = {
                "deviceId": device_id,
                "serviceToken": token["micoapi"][1],
                "userId": str(token["userId"]),
            }
            async with self.account._session.get(
                "https://userprofile.mina.mi.com/device_profile/v2/conversation",
                params={
                    "source": "dialogu",
                    "hardware": hardware,
                    "timestamp": str(int(time.time() * 1000)),
                    "limit": 10,
                },
                cookies=cookies,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except (ValueError, TypeError):
                    data = {}
                valid = response.status == 200 and data.get("code") == 0
            if valid:
                break
            if attempt == 0 and await self.account.login("micoapi"):
                continue
            raise RuntimeError("Xiaomi conversation API authentication failed")
        body = data.get("data") or {}
        body = json.loads(body) if isinstance(body, str) else body
        records = body.get("records") or []
        result = []
        for record in records:
            text = (record.get('query') or '').strip()
            if not text or not isinstance(record.get('time'), (int, float)):
                continue
            answers = record.get('answers') or []
            native = ''.join(a[k]['text'] for a in answers if isinstance(a, dict)
                             for k in ('tts', 'llm') if isinstance(a.get(k), dict)
                             and isinstance(a[k].get('text'), str))
            # Audio/action/unknown typed responses belong to native functions,
            # even when accompanied by text that happens to match a failure rule.
            eligible = all(isinstance(a, dict) and str(a.get('type') or '').upper() in ('', 'TTS', 'LLM')
                           and not any(k in a for k in ('audio', 'action')) for a in answers)
            result.append({'text': text, 'device_id': device_id,
                           'request_id': record.get('requestId') or f"{device_id}:{record['time']}",
                           'ts': record['time'] / 1000.0, 'native_answer': native,
                           'takeover_allowed': eligible})
        return result

    async def _first_device(self) -> str | None:
        """device_list 里找第一台小爱音箱（name 含 '小爱' 或 model 属 speaker）。"""
        devices = await self.mina.device_list() or []
        for d in devices:
            name = d.get("name") or d.get("nickname") or ""
            model = d.get("model") or d.get("hardware") or ""
            did = str(d.get("deviceID") or d.get("device_id") or d.get("did") or "")
            if did and ("小爱" in name or "speaker" in model.lower() or "xiaoai" in model.lower()):
                return did
        if devices:
            return str(
                devices[0].get("deviceID")
                or devices[0].get("device_id")
                or devices[0].get("did")
                or ""
            )
        return None

    # ---- 自检：列出账号下所有小爱音箱 + 档位（--list-devices）----
    async def list_devices(self) -> list[dict]:
        devices = await self.mina.device_list() or []
        out = []
        for d in devices:
            hw = (d.get("hardware") or d.get("model") or "").upper()
            name = d.get("name") or d.get("nickname") or "?"
            did = str(d.get("deviceID") or d.get("device_id") or d.get("did") or "")
            tier = tier_of(hw)
            out.append({"name": name, "did": did, **tier})
        return out

    # ---- 播报：让音箱念出 LLM 回复（v1.2 嘴）----
    async def tts(self, device_id: str, text: str, *, is_current=None, raise_on_error=False,
                  speech_settings=None) -> bool:
        stage, index, submitted, chunks = 'prepare', 0, 0, []
        try:
            text = (text or "").strip()
            # Snapshot once: updates must not change timing midway through an answer.
            settings = dict(self.cfg.get('tts', {}))
            settings.update(speech_settings or {})
            max_chars = max(10, int(settings.get("max_chunk_chars", 60)))
            cps = max(1.0, float(settings.get("chars_per_second", 3.5)))
            margin = max(0.0, float(settings.get('safety_margin_seconds', 1.0)))
            chunks = [text] if text and len(text) <= max_chars else speech_chunks(text, max_chars)
            for index, chunk in enumerate(chunks, 1):
                self._check_current(is_current)
                stage = 'prepare'
                kwargs = {'is_current': is_current} if is_current is not None else {}
                if not await self._tts_once(device_id, chunk, **kwargs):
                    raise SpeechFailure('prepare', 'rejected')
                submitted += 1
                self._check_current(is_current)
                if index < len(chunks) or is_current is not None:
                    # Submitted is not hardware-confirmed playback. Keep the final
                    # estimate interruptible, just as the intervals between chunks.
                    stage = 'wait'
                    await asyncio.sleep(margin + len(chunk) / cps)
            self._check_current(is_current)
            return bool(chunks)
        except Exception as exc:
            error = SpeechFailure.from_exception(stage, exc)
            error.chunk_index, error.total_chunks, error.submitted_chunks = index, len(chunks), submitted
            if raise_on_error:
                raise error from None
            log.error("speech failure diagnosis=%s", json.dumps(error.metadata(), sort_keys=True))
            return False

    @staticmethod
    def _check_current(is_current):
        if asyncio.current_task().cancelling() or (is_current is not None and not is_current()):
            raise asyncio.CancelledError

    async def _tts_once(self, device_id: str, text: str, *, is_current=None) -> bool:
        """Submit one chunk; raise sanitized stage metadata to the per-call handler."""
        text = (text or "").strip()
        if not text:
            return False
        stage = 'prepare'
        try:
            self._check_current(is_current)
            if device_id in self.cfg.get("tts", {}).get("stop_music_before_speech", []):
                stage = 'stop_music'
                if not await self.mina.player_stop(device_id):
                    raise SpeechFailure(stage, 'rejected')
                self._check_current(is_current)
            if self.cfg.get("tts", {}).get("method") == "miot":
                import miservice

                if device_id not in self._miot_devices:
                    stage = 'device_list'
                    rows = await self.mina.device_list()
                    if not isinstance(rows, list):
                        raise SpeechFailure(stage, 'invalid_response')
                    self._miot_devices = {d["deviceID"]: d for d in rows
                                          if isinstance(d, dict) and d.get("deviceID")}
                self._check_current(is_current)
                stage = 'device_prepare'
                device = self._miot_devices.get(device_id)
                if device is None:
                    raise SpeechFailure(stage, 'device_missing')
                if not device.get('hardware') or not device.get('miotDID'):
                    raise SpeechFailure(stage, 'device_metadata')
                command = tier_of(device["hardware"]).get("tts")
                if not command:
                    raise SpeechFailure(stage, 'device_metadata')
                iid = tuple(int(part) for part in command.split("-"))
                stage = 'miot_submit'
                code = await miservice.MiIOService(self.account).miot_action(
                    str(device["miotDID"]), iid, [text]
                )
                if type(code) is not int:
                    raise SpeechFailure(stage, 'invalid_response')
                if code != 0:
                    raise SpeechFailure(stage, 'rejected', code=code)
                log.info("miot tts accepted → %s", device_id)
                return True
            stage = 'mina_submit'
            ok = await self.mina.text_to_speech(device_id, text)
            if not ok:
                raise SpeechFailure(stage, 'rejected')
            log.info("tts submitted → %s", device_id)
            return True
        except Exception as exc:
            raise SpeechFailure.from_exception(stage, exc) from None
