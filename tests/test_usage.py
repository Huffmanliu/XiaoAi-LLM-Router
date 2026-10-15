import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import llm_router


def config(path):
    return {
        "usage": {"log_path": str(path)},
        "llm": {"default_provider": "deepseek", "deepseek": {"model": "deepseek-chat"}},
    }


def test_completed_response_saves_reported_usage_without_content(tmp_path):
    path = tmp_path / "usage.jsonl"
    response = SimpleNamespace(
        model="actual-model",
        id="response-1",
        choices=[SimpleNamespace(message=SimpleNamespace(content="private reply"))],
        usage={
            "prompt_tokens": 100,
            "completion_tokens": 23,
            "total_tokens": 123,
            "prompt_cache_hit_tokens": 64,
            "prompt_cache_miss_tokens": 36,
            "completion_tokens_details": {"reasoning_tokens": 5},
        },
    )

    async def run():
        with patch.object(llm_router, "acompletion", AsyncMock(return_value=response)):
            assert (
                await llm_router.ask(
                    config(path),
                    "private system",
                    [],
                    "private question",
                    {"device_id": "speaker", "request_id": "query-1"},
                )
                == "private reply"
            )

    asyncio.run(run())
    row = json.loads(path.read_text())
    assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == (100, 23, 123)
    assert row["prompt_cache_hit_tokens"] == 64 and row["reasoning_tokens"] == 5
    assert row["model"] == "actual-model" and row["device_id"] == "speaker"
    assert "private" not in path.read_text()


def test_failed_request_keeps_unknown_usage_instead_of_zero(tmp_path):
    path = tmp_path / "usage.jsonl"

    async def run():
        with patch.object(llm_router, "acompletion", AsyncMock(side_effect=TimeoutError)):
            try:
                await llm_router.ask(config(path), "", [], "test")
            except TimeoutError:
                pass

    asyncio.run(run())
    row = json.loads(path.read_text())
    assert row["status"] == "failed_or_interrupted"
    assert row["usage_available"] is False and row["total_tokens"] is None


def test_stream_records_usage_only_chunk(tmp_path):
    path = tmp_path / "usage.jsonl"

    async def chunks():
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="测试回答。"))]
        )
        yield SimpleNamespace(
            choices=[], usage={"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
        )

    async def run():
        with patch.object(llm_router, "acompletion", AsyncMock(return_value=chunks())):
            assert (
                "".join([s async for s in llm_router.ask_stream(config(path), "", [], "test")])
                == "测试回答。"
            )

    asyncio.run(run())
    row = json.loads(path.read_text())
    assert row["total_tokens"] == 13 and row["status"] == "completed"
