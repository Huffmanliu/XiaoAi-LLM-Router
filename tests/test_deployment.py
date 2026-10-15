import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import mi_conversation as conv
import llm_router
import main


def test_shared_mi_session_stays_open():
    async def run():
        account = SimpleNamespace(
            login=AsyncMock(return_value=True),
            token_store=SimpleNamespace(load_token=AsyncMock(return_value=None)),
        )
        async with aiohttp.ClientSession() as session:
            with patch("miservice.MiAccount", return_value=account), patch("miservice.MiNAService"):
                speaker = await conv.MiSpeaker.connect(
                    {"mi": {"username": "test", "password": "test"}}, session
                )
                assert not session.closed
                await speaker.close()
                assert not session.closed

    asyncio.run(run())


def test_mina_device_id_and_configured_target():
    async def run():
        mina = SimpleNamespace(
            device_list=AsyncMock(
                return_value=[
                    {"deviceID": "mina-id", "did": "miot-id", "name": "小爱", "hardware": "L06A"}
                ]
            ),
            get_latest_ask=AsyncMock(return_value=[]),
        )
        speaker = conv.MiSpeaker(None, mina, {"mi": {"device_id": "chosen-id"}})
        assert await speaker._first_device() == "mina-id"
        assert (await speaker.list_devices())[0]["did"] == "mina-id"
        await speaker.latest_ask()
        mina.get_latest_ask.assert_awaited_once_with("chosen-id")

    asyncio.run(run())


def test_stream_short_tokens_do_not_spin_forever():
    async def chunks():
        for token in ["你好。", "接下来这是一句足够长的回答。", "结束"]:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=token))])

    async def run():
        with patch.object(llm_router, "acompletion", AsyncMock(return_value=chunks())):
            cfg = {"llm": {"default_provider": "deepseek", "deepseek": {"model": "deepseek-chat"}}}
            result = [part async for part in llm_router.ask_stream(cfg, "", [], "test")]
            assert "".join(result) == "你好。接下来这是一句足够长的回答。结束"

    asyncio.run(run())


def test_multi_device_timestamps_are_independent():
    async def run():
        stop = asyncio.Event()
        now = main.datetime.now(main.timezone.utc).timestamp()

        async def latest(device):
            return {
                "device_id": device,
                "request_id": "same",
                "ts": now + (20 if device == "a" else 10),
                "text": "深思你好",
            }

        sp = SimpleNamespace(cfg={"mi": {"devices": ["a", "b"]}}, latest_ask=latest)
        received = []
        async for item in main._consume_conversation(sp, stop):
            received.append(item["device_id"])
            if len(received) == 2:
                stop.set()
        assert received == ["a", "b"]

    asyncio.run(run())


def test_cloud_history_uses_selected_device_and_latest_record():
    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self, **kwargs):
            return {
                "code": 0,
                "data": json.dumps(
                    {
                        "records": [
                            {"time": 1000, "query": "old", "requestId": "old-id"},
                            {"time": 2000, "query": "深思为什么", "requestId": "new-id"},
                        ]
                    }
                ),
            }

    def get(url, **kwargs):
        assert kwargs["cookies"]["deviceId"] == "chosen"
        assert kwargs["params"]["hardware"] == "LX04"
        return Response()

    async def run():
        account = SimpleNamespace(
            token={"micoapi": ["security", "service"], "userId": "user"},
            _session=SimpleNamespace(get=get),
        )
        sp = conv.MiSpeaker(account, None, {"mi": {"conversation_source": "cloud"}})
        sp._hardware = {"chosen": "LX04"}
        item = await sp.latest_ask("chosen")
        assert item == {
            "device_id": "chosen",
            "request_id": "new-id",
            "ts": 2.0,
            "text": "深思为什么",
        }

    asyncio.run(run())


def test_miot_tts_keeps_reply_as_one_text_argument():
    async def run():
        action = AsyncMock(return_value=0)
        speaker = conv.MiSpeaker(None, None, {"tts": {"method": "miot"}})
        speaker._miot_devices = {"speaker": {"hardware": "L05B", "miotDID": "123"}}
        text = '大模型回答：hello world。\n第二句也要完整。'
        with patch("miservice.MiIOService", return_value=SimpleNamespace(miot_action=action)):
            await speaker.tts("speaker", text)
        action.assert_awaited_once_with("123", (5, 3), [text])

    asyncio.run(run())


def test_touchscreen_stops_music_before_speech_without_unmuting():
    async def run():
        events = []

        async def stop(device):
            events.append(("stop", device))
            return True

        async def action(did, iid, args):
            events.append(("speak", did, iid, args))
            return 0

        mina = SimpleNamespace(player_stop=stop)
        speaker = conv.MiSpeaker(
            None, mina, {"tts": {"method": "miot", "stop_music_before_speech": ["touch"]}}
        )
        speaker._miot_devices = {"touch": {"hardware": "LX04", "miotDID": "123"}}
        with patch("miservice.MiIOService", return_value=SimpleNamespace(miot_action=action)):
            await speaker.tts("touch", "Deepseek 回答：测试。")
        assert events == [("stop", "touch"), ("speak", "123", (5, 1), ["Deepseek 回答：测试。"])]

    asyncio.run(run())
