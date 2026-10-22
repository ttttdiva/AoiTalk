"""Safe, bounded provider HTTP failure metadata for terminal task outcomes.

Only a structured status, a bounded Retry-After and an allowlisted provider
error category are retained, and only when the exception truly carries them.
Headers, tokens and raw bodies are never copied, and a quota cause is never
inferred from the status alone.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

from ..memory.models.agent_work import USAGE_COUNTER_KEYS as _USAGE_COUNTER_KEYS

MAX_RETRY_AFTER_SECONDS = 86_400.0

_QUOTA_CATEGORIES = frozenset({
    "insufficient_quota",
    "quota_exceeded",
    "billing_hard_limit_reached",
    "credit_balance_exhausted",
    "project_spend_limit_exceeded",
    "organization_spend_limit_exceeded",
})
_RATE_CATEGORIES = frozenset({"rate_limit_exceeded", "rate_limit_error", "too_many_requests"})
_CATEGORY_ALLOWLIST = _QUOTA_CATEGORIES | _RATE_CATEGORIES | frozenset({
    "authentication_error",
    "permission_error",
    "invalid_request_error",
    "server_error",
    "overloaded_error",
    "context_length_exceeded",
})
_CATEGORY_PATTERN = re.compile(r"[a-z][a-z0-9_]{2,63}")


def _status(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(exc, "status", None),
        getattr(response, "status_code", None),
        getattr(exc, "code", None),
    ):
        if isinstance(candidate, bool):
            continue
        if isinstance(candidate, int):
            value = candidate
        elif isinstance(candidate, str) and re.fullmatch(r"[0-9]{3}", candidate.strip()):
            value = int(candidate.strip())
        else:
            continue  # floats such as 429.7 are not valid HTTP statuses
        if 100 <= value <= 599:
            return value
    return None


def _header(exc: BaseException, name: str) -> Any:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    try:
        return headers.get(name)
    except Exception:
        return None


def _retry_after(exc: BaseException) -> float | None:
    raw = _header(exc, "retry-after")
    if raw is None:
        raw = _header(exc, "Retry-After")
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    if text.isascii() and text.isdigit():
        value = float(text)
    else:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        value = (when - datetime.now(timezone.utc)).total_seconds()
        value = max(0.0, value)
    if not math.isfinite(value) or value < 0:
        return None
    return min(value, MAX_RETRY_AFTER_SECONDS)


def _category(exc: BaseException) -> str | None:
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, Mapping) else None
    sources = [error, body] if isinstance(error, Mapping) else [body]
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for key in ("code", "type"):
            value = source.get(key)
            if isinstance(value, str):
                lowered = value.strip().lower()
                if _CATEGORY_PATTERN.fullmatch(lowered) and lowered in _CATEGORY_ALLOWLIST:
                    return lowered
    return None


def extract_provider_failure(exc: BaseException) -> dict[str, Any] | None:
    """Return ``{http_status, retry_after_seconds?, provider_error_category?, quota_indicated?}`` or None."""

    status = _status(exc)
    if status is None:
        return None
    info: dict[str, Any] = {"http_status": status}
    retry_after = _retry_after(exc)
    if retry_after is not None:
        info["retry_after_seconds"] = retry_after
    category = _category(exc)
    if category is not None:
        info["provider_error_category"] = category
        info["quota_indicated"] = category in _QUOTA_CATEGORIES
    return info


_STATES = frozenset({"known", "unknown"})


def terminal_failure_event_fields(metadata: Any) -> dict[str, Any]:
    """Allowlisted subset of an outcome's metadata for the terminal Work event.

    Nothing is blanket-copied: each field is re-validated here.
    """

    if not isinstance(metadata, Mapping):
        return {}
    fields: dict[str, Any] = {}
    failure = metadata.get("provider_failure")
    if isinstance(failure, Mapping):
        status = failure.get("http_status")
        if type(status) is int and 100 <= status <= 599:
            safe: dict[str, Any] = {"http_status": status}
            retry = failure.get("retry_after_seconds")
            if (
                type(retry) in (int, float)
                and math.isfinite(retry)
                and 0 <= retry <= MAX_RETRY_AFTER_SECONDS
            ):
                safe["retry_after_seconds"] = float(retry)
            category = failure.get("provider_error_category")
            if isinstance(category, str) and category in _CATEGORY_ALLOWLIST:
                safe["provider_error_category"] = category
                safe["quota_indicated"] = category in _QUOTA_CATEGORIES
            fields["provider_failure"] = safe
    for key in ("usage_state", "evidence_state"):
        value = metadata.get(key)
        if isinstance(value, str) and value in _STATES:
            fields[key] = value
    for key in ("tool_receipt_count", "tool_receipt_success_count"):
        value = metadata.get(key)
        if (isinstance(value, int) and not isinstance(value, bool) and value >= 0) or value == "unknown":
            fields[key] = value
    return fields


USAGE_COUNTER_KEYS = _USAGE_COUNTER_KEYS


def safe_usage_snapshot(usage: Any) -> dict[str, Any]:
    """Allowlisted numeric usage counters.

    The generic Work JSON sanitizer drops any key containing ``token``, which
    silently erased real ``*_tokens`` usage from terminal events.
    """

    if not isinstance(usage, Mapping):
        return {}
    snapshot: dict[str, Any] = {}
    for key in USAGE_COUNTER_KEYS:
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        if value >= 0:
            snapshot[key] = value
    return snapshot
