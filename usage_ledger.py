"""Persist provider-reported usage, without storing conversation text or keys."""

import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("xiaoai-router.usage")


def _mapping(value):
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return vars(value) if value is not None and hasattr(value, "__dict__") else {}


def record(
    cfg, usage, *, model, response_id=None, context=None, status="completed", elapsed_ms=None
):
    path = cfg.get("usage", {}).get("log_path")
    if not path:
        return
    values = _mapping(usage)
    details = _mapping(values.get("completion_tokens_details"))
    prompt_details = _mapping(values.get("prompt_tokens_details"))
    context = context or {}
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "usage_available": bool(values),
        "model": model,
        "response_id": response_id,
        "device_id": context.get("device_id"),
        "request_id": context.get("request_id"),
        "prompt_tokens": values.get("prompt_tokens"),
        "completion_tokens": values.get("completion_tokens"),
        "total_tokens": values.get("total_tokens"),
        "prompt_cache_hit_tokens": values.get(
            "prompt_cache_hit_tokens", prompt_details.get("cached_tokens")
        ),
        "prompt_cache_miss_tokens": values.get("prompt_cache_miss_tokens"),
        "reasoning_tokens": details.get("reasoning_tokens"),
        "elapsed_ms": elapsed_ms,
    }
    try:
        data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if os.write(fd, data) != len(data):
                raise OSError("Incomplete usage ledger write")
        finally:
            os.close(fd)
        log.info(
            "usage device=%s input=%s output=%s total=%s status=%s",
            row["device_id"],
            row["prompt_tokens"],
            row["completion_tokens"],
            row["total_tokens"],
            status,
        )
    except OSError:
        log.exception("Could not persist token usage")
