"""Sanitized context-window snapshots for the composer inspector."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from .openai_model_context_registry import openai_model_context_spec

CHARS_PER_TOKEN_ESTIMATE = 4.0
MAX_PREVIEW_CHARS = 120
MAX_METADATA_CHARS = 240
MAX_COMPONENTS = 128
MAX_REQUESTS = 32
MAX_REQUEST_DEPTH = 2
_SNAPSHOT_FIELDS = {
    "provider",
    "model",
    "captured_at",
    "request_index",
    "request_count",
    "requests_omitted",
    "request_kind",
    "context_window_tokens",
    "context_window_source",
    "response_tokens_reserved",
    "input_tokens",
    "remaining_tokens",
    "usage_percent",
    "measurement",
}
_COMPONENT_FIELDS = {
    "category",
    "label",
    "tokens",
    "percentage",
    "status",
    "measurement",
    "source",
    "preview",
    "selection_reason",
    "duration_ms",
    "retrieved_chars",
    "selected_chars",
    "size_chars",
}


def safe_preview(value: Any, *, limit: int = MAX_PREVIEW_CHARS) -> str:
    """Return a short structural preview without persisting prompt bodies."""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value or "")
    text = " ".join(text.split())
    text = re.sub(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    text = re.sub(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_A-Za-z0-9]{12,}\b", "[REDACTED]", text)
    text = re.sub(r"(?i)\b([A-Z0-9_]*(?:API_KEY|TOKEN|PASSWORD|SECRET))\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", text)
    if not text:
        return ""
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def structural_preview(value: Any) -> str:
    """Describe payload shape; never persist arbitrary prompt text by default."""
    if isinstance(value, str):
        return f"テキスト {len(value):,}文字"
    if isinstance(value, list):
        return f"{len(value):,}項目"
    if isinstance(value, dict):
        keys = [str(key) for key in value.keys()][:8]
        return f"構造化データ（{', '.join(keys)}）"
    return type(value).__name__ if value is not None else ""


def estimated_tokens(value: Any) -> int:
    if value in (None, "", [], {}):
        return 0
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    return max(1, round(len(value) / CHARS_PER_TOKEN_ESTIMATE))


def component(
    category: str,
    label: str,
    value: Any = None,
    *,
    source: str,
    status: str = "active",
    measurement: str = "character_estimate",
    tokens: int | None = None,
    preview: str | None = None,
    selection_reason: str | None = None,
    duration_ms: float | None = None,
    retrieved_chars: int | None = None,
    selected_chars: int | None = None,
) -> dict[str, Any]:
    token_count = tokens if tokens is not None else estimated_tokens(value)
    result = {
        "category": category,
        "label": label,
        "tokens": token_count if measurement != "unavailable" else None,
        "percentage": None,
        "status": status,
        "measurement": measurement,
        "source": source,
        "preview": structural_preview(value) if preview is None else safe_preview(preview),
    }
    if selection_reason:
        result["selection_reason"] = safe_preview(selection_reason)
    if duration_ms is not None:
        result["duration_ms"] = max(0.0, round(float(duration_ms), 3))
    if retrieved_chars is not None:
        result["retrieved_chars"] = max(0, int(retrieved_chars))
    if selected_chars is not None:
        selected = max(0, int(selected_chars))
        result["selected_chars"] = selected
        result["size_chars"] = selected
    return result


_SNAPSHOT_TEXT_FIELDS = {
    "provider",
    "model",
    "captured_at",
    "request_kind",
    "context_window_source",
    "measurement",
}
_SNAPSHOT_NUMBER_FIELDS = _SNAPSHOT_FIELDS - _SNAPSHOT_TEXT_FIELDS
_COMPONENT_TEXT_FIELDS = {
    "category",
    "label",
    "status",
    "measurement",
    "source",
}
_COMPONENT_NUMBER_FIELDS = _COMPONENT_FIELDS - _COMPONENT_TEXT_FIELDS - {
    "preview",
    "selection_reason",
}


def _snapshot_token_value(value: Any) -> int | None:
    """Return a finite non-negative integer token count from metadata."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or value < 0 or not number.is_integer():
        return None
    return int(value)


def _snapshot_json_safe(value: Any) -> Any:
    """Copy arbitrary legacy metadata without emitting non-JSON values."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            safe[str(key)] = _snapshot_json_safe(item)
        return safe
    if isinstance(value, (list, tuple)):
        return [_snapshot_json_safe(item) for item in value]
    try:
        return str(value)
    except Exception:  # pragma: no cover - defensive for hostile objects
        return None


_SNAPSHOT_NUMERIC_FIELDS = {
    "request_index",
    "request_count",
    "requests_omitted",
    "input_tokens",
    "remaining_tokens",
    "usage_percent",
    "response_tokens_reserved",
    "max_output_tokens",
}


def _enrich_openai_snapshot_item(value: dict[str, Any]) -> dict[str, Any]:
    """Backfill one legacy OpenAI snapshot without changing its measurement."""

    raw_input = value.get("input_tokens")
    raw_response = value.get("response_tokens_reserved")
    input_tokens = _snapshot_token_value(raw_input)
    response_tokens = _snapshot_token_value(raw_response)
    input_valid = raw_input is None or input_tokens is not None
    response_valid = raw_response is None or response_tokens is not None

    result = _snapshot_json_safe(value)
    if not isinstance(result, dict):  # pragma: no cover - value is typed dict
        return {}

    # Existing scalar numeric fields are retained when finite and removed when
    # malformed.  This mirrors ``sanitize_context_snapshot`` while preserving
    # legacy fields not in its public allow-list.
    for key in _SNAPSHOT_NUMERIC_FIELDS:
        if key not in value or value[key] is None:
            continue
        normalized = _safe_metadata_number(value[key])
        if normalized is None:
            result.pop(key, None)
        else:
            result[key] = normalized

    raw_window = value.get("context_window_tokens")
    explicit_window = _snapshot_token_value(raw_window)
    if explicit_window is None or explicit_window <= 0:
        result.pop("context_window_tokens", None)
    else:
        result["context_window_tokens"] = explicit_window

    # Compatibility aliases are safe to retain only as positive integer token
    # values.  They are candidates for a missing canonical window below.
    alias_windows: dict[str, int] = {}
    for alias in ("context_length", "context_window", "max_context_length"):
        raw_alias = value.get(alias)
        if raw_alias is None:
            continue
        alias_window = _snapshot_token_value(raw_alias)
        if alias_window is None or alias_window <= 0:
            result.pop(alias, None)
        else:
            alias_windows[alias] = alias_window
            result[alias] = alias_window

    if result.get("provider") != "openai":
        return result
    spec = openai_model_context_spec(result.get("model"))
    if spec is None:
        return result

    # A valid persisted canonical value remains authoritative.  Only a
    # missing/invalid canonical window is eligible for alias or registry
    # backfill, and only that path recomputes derived totals.
    backfilled = False
    window = result.get("context_window_tokens")
    if window is None:
        for alias in ("context_length", "context_window", "max_context_length"):
            if alias in alias_windows:
                window = alias_windows[alias]
                result["context_window_tokens"] = window
                backfilled = True
                break
    if window is None:
        window = spec.context_window_tokens
        result["context_window_tokens"] = window
        result["context_window_source"] = "official-registry"
        backfilled = True

    if not backfilled or not input_valid or not response_valid:
        return result
    if input_tokens is None:
        return result
    response_tokens = response_tokens or 0
    window_tokens = _snapshot_token_value(window)
    if window_tokens is None or window_tokens <= 0:
        return result
    result["remaining_tokens"] = max(
        0,
        window_tokens - input_tokens - response_tokens,
    )
    result["usage_percent"] = round(input_tokens / window_tokens * 100, 1)
    return result


def enrich_persisted_context_snapshot(
    value: Any,
    *,
    _depth: int = 0,
) -> dict[str, Any] | None:
    """Enrich legacy exact OpenAI snapshots at response time.

    This is intentionally a non-destructive response projection: database
    metadata is never rewritten, unknown models remain unchanged, and existing
    measurement/source fields are preserved unless an official registry window
    is newly added.
    """

    if not isinstance(value, dict):
        return None
    result = _enrich_openai_snapshot_item(value)
    if _depth >= MAX_REQUEST_DEPTH:
        return result
    for key in ("main", "requests"):
        raw = result.get(key)
        if isinstance(raw, dict):
            result[key] = enrich_persisted_context_snapshot(raw, _depth=_depth + 1)
        elif isinstance(raw, list):
            result[key] = [
                (
                    enrich_persisted_context_snapshot(item, _depth=_depth + 1)
                    if isinstance(item, dict)
                    else item
                )
                for item in raw
            ]
    return result


def _safe_metadata_text(value: Any) -> str | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    return safe_preview(value, limit=MAX_METADATA_CHARS)


def _safe_metadata_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0 or number > 1_000_000_000:
        return None
    return int(value) if isinstance(value, int) else round(number, 3)


def sanitize_context_snapshot(
    value: Any,
    *,
    _depth: int = 0,
) -> dict[str, Any] | None:
    """Copy only public observation fields; never retain model or prompt bodies."""
    if not isinstance(value, dict):
        return None

    clean: dict[str, Any] = {}
    for key in _SNAPSHOT_TEXT_FIELDS:
        if key in value and (text := _safe_metadata_text(value[key])) is not None:
            clean[key] = text
    for key in _SNAPSHOT_NUMBER_FIELDS:
        if key in value and (
            number := _safe_metadata_number(value[key])
        ) is not None:
            clean[key] = number

    components: list[dict[str, Any]] = []
    raw_components = value.get("components") or value.get("categories") or []
    if not isinstance(raw_components, (list, tuple)):
        raw_components = []
    for item in raw_components[:MAX_COMPONENTS]:
        if not isinstance(item, dict):
            continue
        part: dict[str, Any] = {}
        for key in _COMPONENT_TEXT_FIELDS:
            if key in item and (
                text := _safe_metadata_text(item[key])
            ) is not None:
                part[key] = text
        for key in _COMPONENT_NUMBER_FIELDS:
            if key in item and (
                number := _safe_metadata_number(item[key])
            ) is not None:
                part[key] = number
        for key in ("preview", "selection_reason"):
            if key in item and isinstance(item[key], (str, int, float)):
                part[key] = safe_preview(item[key])
        if part:
            components.append(part)
    if components:
        clean["components"] = components

    if _depth < MAX_REQUEST_DEPTH:
        raw_requests = value.get("requests") or []
        if not isinstance(raw_requests, (list, tuple)):
            raw_requests = []
        requests = [
            sanitized
            for item in raw_requests[-MAX_REQUESTS:]
            if (
                sanitized := sanitize_context_snapshot(
                    item,
                    _depth=_depth + 1,
                )
            )
        ]
        if requests:
            clean["requests"] = requests
    return clean or None


def sanitized_snapshot_series(
    snapshots: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build bounded persistence metadata while retaining the latest requests."""
    values = [dict(item) for item in snapshots if isinstance(item, dict)]
    if not values:
        return None
    latest = dict(values[-1])
    latest["request_count"] = len(values)
    latest["requests_omitted"] = max(0, len(values) - MAX_REQUESTS)
    latest["requests"] = values
    return sanitize_context_snapshot(latest)


def message_components(messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        role = str(message.get("role") or "unknown")
        content = message.get("content")
        if role == "system":
            category, label = "system_instructions", "System instructions"
        elif role == "user" and index == len(messages) - 1:
            category, label = "current_user_message", "Current user message"
        elif role == "tool":
            category, label = "tool_results", "Tool results"
        else:
            category, label = "conversation_history", "Conversation history"
        result.append(component(category, label, content, source=f"messages[{index}] ({role})"))
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}
            for part in content
        ):
            result.append(component(
                "attachments", "添付ファイル・画像由来の入力", source=f"messages[{index}] image parts",
                measurement="unavailable", preview="画像入力（バイナリ・URLは保存しません）",
            ))
    return result


def tool_components(tools: Iterable[dict[str, Any]], *, source: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = (function or {}).get("name") if isinstance(function, dict) else tool.get("name")
        result.append(component(
            "native_tool_schemas", "Native tool schemas", tool,
            source=source, preview=str(name or "tool schema"),
        ))
    return result


def openai_compatible_request_components(
    messages: Iterable[dict[str, Any]],
    tools: Iterable[dict[str, Any]],
    *,
    provider: str,
    dynamic_context: Iterable[tuple[str, str]] = (),
    dynamic_context_metadata: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Classify the exact chat payload while preserving injected provenance."""
    observed_messages: list[Any] = list(messages)
    injected: list[dict[str, Any]] = []

    for label, raw_text in dynamic_context:
        text = str(raw_text or "").strip()
        if not text:
            continue
        rendered = f"[{label}]\n{text}"
        if not last_role_contains_text(
            observed_messages,
            rendered,
            role="user",
        ):
            continue
        observed_messages = without_text_from_last_role(
            observed_messages,
            rendered,
            role="user",
        )
        normalized_label = str(label or "").casefold()
        if "project" in normalized_label:
            category = "project_context"
            source = "resolved project context"
        elif "memory" in normalized_label:
            category = "past_conversation_recall"
            source = "memory search"
        elif "tool" in normalized_label:
            category = "tool_hints"
            source = "runtime tool registry"
        else:
            category = "dynamic_context"
            source = "prompt composer"
        injected.append(
            component(
                category,
                str(label or "Dynamic context"),
                rendered,
                source=source,
                selection_reason="selected for current provider request",
                duration_ms=(
                    (dynamic_context_metadata or {})
                    .get(str(label), {})
                    .get("duration_ms")
                ),
                selected_chars=len(rendered),
            )
        )
    return [
        *message_components(observed_messages),
        *injected,
        *tool_components(
            tools,
            source=f"{provider} chat.completions tools payload",
        ),
    ]


def context_bundle_components(bundle: Any) -> tuple[str, list[dict[str, Any]]]:
    if bundle is None or not hasattr(bundle, "render_with_trace"):
        return "", []
    rendered, trace = bundle.render_with_trace()
    result = [
        component(
            item["category"], item["label"], item.get("text", ""),
            source=item["source"], status=item.get("status", "active"),
            tokens=0 if item.get("status") == "deferred" else None,
            preview=item.get("preview"),
            selection_reason=item.get("selection_reason"),
            duration_ms=item.get("duration_ms"),
            retrieved_chars=item.get("retrieved_chars"),
            selected_chars=item.get("selected_chars"),
        )
        for item in trace
    ]
    return rendered, result


def without_text(messages: Iterable[Any], text: str) -> list[Any]:
    def remove(value: Any) -> tuple[Any, bool]:
        if isinstance(value, str):
            return (
                value.replace(text, "", 1),
                text in value,
            )
        if isinstance(value, list):
            copied_parts = []
            removed = False
            for part in value:
                if removed:
                    copied_parts.append(part)
                    continue
                copied_part, removed = remove(part)
                copied_parts.append(copied_part)
            return copied_parts, removed
        if isinstance(value, dict):
            copied_value = dict(value)
            for key in ("content", "text", "input_text", "output"):
                if key not in copied_value:
                    continue
                copied_value[key], removed = remove(copied_value[key])
                if removed:
                    return copied_value, True
            return copied_value, False
        return value, False

    copied = [
        dict(item) if isinstance(item, dict) else item
        for item in messages
    ]
    if not text:
        return copied
    for index, message in enumerate(copied):
        copied[index], removed = remove(message)
        if removed:
            break
    return copied


def without_text_from_last_role(
    messages: Iterable[Any],
    text: str,
    *,
    role: str,
) -> list[Any]:
    """Remove text only from the last message with the requested role.

    Turn-local dynamic context is composed into the current user message. A
    global first-match removal can instead mutate an older history/tool item
    when it contains the same text, causing snapshot double-counting.
    """

    copied = [
        dict(item) if isinstance(item, dict) else item
        for item in messages
    ]
    if not text:
        return copied
    normalized_role = str(role or "").casefold()
    for index in range(len(copied) - 1, -1, -1):
        message = copied[index]
        message_role = (
            message.get("role")
            if isinstance(message, dict)
            else getattr(message, "role", None)
        )
        if str(message_role or "").casefold() != normalized_role:
            continue
        copied[index] = without_text([message], text)[0]
        break
    return copied


def last_role_contains_text(
    messages: Iterable[Any],
    text: str,
    *,
    role: str,
) -> bool:
    """Return whether text exists in the last message with the requested role."""

    def contains(value: Any) -> bool:
        if isinstance(value, str):
            return text in value
        if isinstance(value, (list, tuple)):
            return any(contains(item) for item in value)
        if isinstance(value, dict):
            return any(contains(item) for item in value.values())
        for attribute in ("content", "text", "input_text", "output"):
            if hasattr(value, attribute) and contains(getattr(value, attribute)):
                return True
        return False

    if not text:
        return False
    normalized_role = str(role or "").casefold()
    items = list(messages)
    for message in reversed(items):
        message_role = (
            message.get("role")
            if isinstance(message, dict)
            else getattr(message, "role", None)
        )
        if str(message_role or "").casefold() == normalized_role:
            return contains(message)
    return False


def snapshot(
    *,
    provider: str,
    model: str,
    components: Iterable[dict[str, Any]],
    context_window_tokens: int | None = None,
    response_tokens: int | None = None,
    request_index: int = 0,
    request_kind: str = "model_request",
    input_tokens: int | None = None,
    window_source: str | None = None,
) -> dict[str, Any]:
    parts = [dict(item) for item in components if item]
    active_estimate = sum(
        int(item.get("tokens") or 0)
        for item in parts
        if item.get("status") == "active" and item.get("tokens") is not None
    )
    total = input_tokens if input_tokens is not None else active_estimate or None
    measurement = "measured" if input_tokens is not None else (
        "character_estimate" if total is not None else "unavailable"
    )
    if input_tokens is not None and active_estimate > input_tokens and active_estimate:
        scale = input_tokens / active_estimate
        for item in parts:
            if item.get("status") == "active" and item.get("tokens") is not None:
                item["tokens"] = round(int(item["tokens"]) * scale)
        rounding_delta = input_tokens - sum(
            int(item.get("tokens") or 0) for item in parts
            if item.get("status") == "active" and item.get("tokens") is not None
        )
        if rounding_delta and parts:
            adjustable = next((item for item in parts if item.get("status") == "active" and item.get("tokens") is not None), None)
            if adjustable:
                adjustable["tokens"] = max(0, int(adjustable["tokens"]) + rounding_delta)
    elif input_tokens is not None and input_tokens > active_estimate:
        parts.append(component(
            "provider_overhead",
            "Provider overhead / unattributed",
            source="provider usage difference",
            measurement="measured",
            tokens=input_tokens - active_estimate,
            preview="カテゴリ別推定とProvider実測入力の差分",
        ))
    denominator = total or 0
    for item in parts:
        tokens = item.get("tokens")
        item["percentage"] = (
            round(tokens / denominator * 100, 1)
            if denominator and tokens is not None and item.get("status") == "active"
            else 0.0 if item.get("status") == "deferred" else None
        )
    remaining = (
        max(0, context_window_tokens - (total or 0) - int(response_tokens or 0))
        if context_window_tokens is not None and total is not None
        else None
    )
    percent = (
        round((total or 0) / context_window_tokens * 100, 1)
        if context_window_tokens and total is not None
        else None
    )
    return {
        "provider": provider,
        "model": model,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "request_index": request_index,
        "request_kind": request_kind,
        "context_window_tokens": context_window_tokens,
        "context_window_source": window_source,
        "response_tokens_reserved": response_tokens,
        "input_tokens": total,
        "remaining_tokens": remaining,
        "usage_percent": percent,
        "measurement": measurement,
        "components": parts,
    }


def reconcile_snapshot(item: dict[str, Any], input_tokens: int | None) -> dict[str, Any]:
    if input_tokens is None:
        return item
    return snapshot(
        provider=str(item.get("provider") or "unknown"),
        model=str(item.get("model") or "unknown"),
        components=[
            part for part in item.get("components", [])
            if part.get("category") != "provider_overhead"
        ],
        context_window_tokens=item.get("context_window_tokens"),
        response_tokens=item.get("response_tokens_reserved"),
        request_index=int(item.get("request_index") or 0),
        request_kind=str(item.get("request_kind") or "model_request"),
        input_tokens=input_tokens,
        window_source=item.get("context_window_source"),
    )
