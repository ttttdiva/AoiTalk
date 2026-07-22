"""Automatic failure recording helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import traceback
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_RECENT_FAILURES: dict[str, datetime] = {}
_DEDUP_WINDOW = timedelta(minutes=10)
_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|cookie|authorization)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"),
]


def redact_secrets(value: Any) -> Any:
    """Return a JSON-safe value with common secret material removed."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if re.search(r"(?i)(api[_-]?key|token|secret|password|cookie|authorization)", str(key)):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value[:50]]
    if isinstance(value, str):
        text = value[:8000]
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]" if match.groups() else "[REDACTED]", text)
        return text
    return value


def _dedup_key(payload: dict[str, Any]) -> str:
    stable = {
        "source": payload.get("source"),
        "operation": payload.get("operation"),
        "tool_name": payload.get("tool_name"),
        "task_id": payload.get("task_id"),
        "project_id": payload.get("project_id"),
        "error_type": payload.get("error_type"),
        "error_message": payload.get("error_message"),
        "run_id": payload.get("run_id"),
    }
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


async def record_failure_event(
    *,
    source: str,
    operation: str,
    error: BaseException | str,
    tool_name: str | None = None,
    task_id: str | None = None,
    project_id: str | None = None,
    conversation_id: str | None = None,
    run_id: str | None = None,
    input_summary: Any = None,
    include_stack: bool = True,
) -> bool:
    """Record a failed operation into the existing feedback store."""
    if isinstance(error, BaseException):
        error_type = type(error).__name__
        error_message = str(error)
        stack_trace = "".join(traceback.format_exception(error)) if include_stack else None
    else:
        error_type = "Error"
        error_message = str(error)
        stack_trace = None

    payload = redact_secrets(
        {
            "source": source,
            "operation": operation,
            "tool_name": tool_name,
            "task_id": task_id,
            "project_id": project_id,
            "conversation_id": conversation_id,
            "run_id": run_id,
            "error_type": error_type,
            "error_message": error_message,
            "stack_trace": stack_trace,
            "input_summary": input_summary,
            "resolved": False,
        }
    )
    key = _dedup_key(payload)
    now = datetime.utcnow()
    expired = [item for item, at in _RECENT_FAILURES.items() if now - at > _DEDUP_WINDOW]
    for item in expired:
        _RECENT_FAILURES.pop(item, None)
    if key in _RECENT_FAILURES:
        return False
    _RECENT_FAILURES[key] = now

    try:
        from src.services.feedback_store import FeedbackRequest, save_feedback_async

        await save_feedback_async(
            FeedbackRequest(
                message=f"{payload['operation']} failed: {payload['error_message']}",
                category="auto_failure",
                comment=json.dumps(payload, ensure_ascii=False, indent=2),
                session_id=conversation_id or run_id,
            )
        )
        return True
    except Exception as exc:  # pragma: no cover - failure recording must not break callers
        logger.warning("Failed to record automatic failure feedback: %s", exc)
        return False
