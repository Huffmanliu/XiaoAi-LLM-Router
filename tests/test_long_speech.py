import asyncio
from unittest.mock import AsyncMock, patch

from mi_tts import speech_chunks
from mi_conversation import MiSpeaker


def test_long_speech_stays_bounded_and_preserves_all_characters():
    text = "皇帝评价没有标点" * 35 + "。下一句，继续说明。"
    chunks = speech_chunks(text, 60)
    assert len(chunks) > 1
    assert all(1 <= len(s) <= 60 for s in chunks)
    assert "".join(chunks).replace(" ", "") == text


def test_long_speech_paces_commands_and_stops_on_failure():
    async def run():
        speaker = MiSpeaker(None, None, {"tts": {"max_chunk_chars": 30}})
        text = "这是第一位皇帝的评价。" * 10
        chunks = speech_chunks(text, 30)
        events = []

        async def send(device, part):
            events.append(("send", part))
            return True

        async def wait(delay):
            events.append(("wait", delay))

        with (
            patch.object(speaker, "_tts_once", side_effect=send),
            patch("mi_conversation.asyncio.sleep", side_effect=wait),
        ):
            assert await speaker.tts("device", text)
        assert [e[1] for e in events if e[0] == "send"] == chunks
        assert len(events) == 2 * len(chunks) - 1
        assert all(events[i][0] == ("send" if i % 2 == 0 else "wait") for i in range(len(events)))
        with (
            patch.object(speaker, "_tts_once", AsyncMock(return_value=False)) as failed,
            patch("mi_conversation.asyncio.sleep", AsyncMock()) as delay,
        ):
            assert not await speaker.tts("device", text)
            assert failed.await_count == 1
            delay.assert_not_awaited()

    asyncio.run(run())
