"""Sanitized context-window snapshots for the composer inspector."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable

from .openai_model_context_registry import openai_model_context_spec
from ..services.secret_patterns import HIGH_CONFIDENCE_API_TOKEN_RE

logger = logging.getLogger(__name__)

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
    text = HIGH_CONFIDENCE_API_TOKEN_RE.sub("[REDACTED]", text)
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


# ---------------------------------------------------------------------------
# Work Intelligence Plane WS1: sanitized ContextManifest shadow projection
# ---------------------------------------------------------------------------

CONTEXT_MANIFEST_SCHEMA_VERSION = "1.0"
CONTEXT_MANIFEST_PRODUCER_VERSION = "ws1-shadow-1"
CONTEXT_MANIFEST_SANITIZER_VERSION = "1.0"
# Work Intelligence remains a one-way observation.  Keep the original WS1
# producer accepted for historical rows and switch only manifests that carry
# the optional typed compiler sidecar to this explicit marker.
CONTEXT_MANIFEST_WORK_INTELLIGENCE_PRODUCER_VERSION = "wi-core-1"
CONTEXT_MANIFEST_SUPPORTED_PRODUCER_VERSIONS = frozenset(
    {
        CONTEXT_MANIFEST_PRODUCER_VERSION,
        CONTEXT_MANIFEST_WORK_INTELLIGENCE_PRODUCER_VERSION,
    }
)

MAX_MANIFEST_RESOURCES = 128
MAX_MANIFEST_EVIDENCE = 128
MAX_MANIFEST_REQUESTS = 32

_MANIFEST_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MANIFEST_TOKEN_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,239}$"
)
_MANIFEST_VERSION_RE = re.compile(r"^[0-9TZ:+.\-]{1,64}$")
_CONTEXT_MANIFEST_CAPTURE_FIELD = "_context_manifest_shadow_capture"

_MANIFEST_LAYER_SOURCES = {
    "project_context": "ContextBundle.project_context_block",
    "session_summary": "ContextBundle.session_context_block",
    "context_memory": "ContextBundle.memory_context_block",
    "project_knowledge_index": "ContextBundle.project_knowledge_index",
    "accessible_knowledge_index": "ContextBundle.accessible_knowledge_index",
    "project_information": "ContextBundle.project_information_block",
    "active_task_context": "ContextBundle.task_context_block",
    "work_intelligence": "ContextBundle.work_intelligence_block",
}
_PROJECT_CONTEXT_LAYER_CATEGORIES = frozenset(
    {
        "project_context",
        "project_knowledge_index",
        "accessible_knowledge_index",
        "project_information",
        "active_task_context",
        "work_intelligence",
    }
)

_MANIFEST_COMPONENT_CATEGORIES = frozenset(
    {
        "system_instructions",
        "current_user_message",
        "conversation_history",
        "tool_results",
        "native_tool_schemas",
        "attachments",
        "prompt_scaffolding",
        "past_conversation_recall",
        "tool_hints",
        "project_context",
        "project_information",
        "project_knowledge_index",
        "accessible_knowledge_index",
        "active_task_context",
        "session_summary",
        "context_memory",
        "work_intelligence",
        "provider_overhead",
        "provider_managed",
        "cli_tool_descriptions",
        "cli_native_session",
        "dynamic_context",
    }
)

_MANIFEST_SELECTION_REASONS = frozenset(
    {
        "selected_project_context",
        "project_context_disabled_or_unavailable",
        "selected_project_knowledge_index",
        "selected_accessible_knowledge_index",
        "explicit_or_current_message_requires_details",
        "deferred_until_relevant_turn",
        "explicit_task_or_current_message_requires_details",
        "current_session_summary",
        "scoped_relevance_search",
        "retrieval_failed",
        "duplicate_context",
        "context_budget_exceeded",
        "no_context_selected",
        "selected_with_budget_truncation",
        "selected_for_current_turn",
        "selected_live_work_projection",
        "rollout_gate_disabled",
        "project_context_disabled",
        "project_scope_unavailable",
        "insufficient_expert_evidence",
        "advisory_conflict",
        "budget_omitted",
    }
)

_MANIFEST_RELATIONS = frozenset(
    {
        "canonical",
        "related",
        "reference",
        "personal",
        "global",
        "user",
        "project",
        "task",
        "session",
        "explicit",
        "resolved_scope",
        "turn",
        "work",
        "activity",
        "approval",
        "owner",
        "assignee",
        "creator",
        "editor",
        "participant",
    }
)
_MANIFEST_EVIDENCE_KINDS = frozenset(
    {
        "project_knowledge_node",
        "docs_scope_node",
        "memory_source",
        "memory_evidence",
        "work_project",
        "work_task",
        "work_task_assignee",
        "work_task_activity",
        "work_docs_node",
        "work_docs_revision",
        "work_session_participant",
        "work_agent_run",
        "work_project_app",
        "work_app_job",
    }
)
_MANIFEST_POLICY_AUTHORITIES = frozenset(
    {
        "TurnContext",
        "ProjectContextResolver",
        "ConversationSession",
        "Task",
        "ProjectKnowledgeService",
        "DocsScope",
    }
)
_MANIFEST_POLICY_SCOPES = frozenset(
    {
        "project",
        "session",
        "task",
        "project_knowledge",
        "accessible_knowledge",
    }
)

_MANIFEST_SCOPE_KINDS = frozenset(
    {"global", "user", "project", "task", "session"}
)
_MANIFEST_RESOURCE_KINDS = frozenset(
    {
        "user",
        "session",
        "project",
        "task",
        "message",
        "client_message",
        "tool_call",
        "knowledge_node",
        "scoped_memory",
        "docs",
        "app",
        "file",
        "chat_session",
        "task_activity",
        "docs_node",
        "docs_revision",
        "session_participant",
        "agent_run",
        "project_app",
        "app_job",
    }
)


@dataclass(frozen=True)
class ResourceRef:
    """Non-authoritative persisted reference to an already-authorized resource."""

    kind: str
    ref_hash: str
    relation: str | None = None
    source: str | None = None
    version: str | int | None = None
    freshness: str | None = None
    supersedes_ref_hash: str | None = None


@dataclass(frozen=True)
class EvidenceRef:
    """Opaque evidence locator; never a reusable source identifier."""

    kind: str
    locator_hash: str
    source_type: str | None = None
    version: str | int | None = None


@dataclass(frozen=True)
class PolicyDecision:
    """Observation of an existing authority's decision, not an authority."""

    authority: str
    scope: str
    decision: str
    resource_ref_hash: str | None
    decision_ref_hash: str


@dataclass(frozen=True)
class ContextLayer:
    """Sanitized ContextBundle selection metadata."""

    category: str
    source: str
    status: str
    inclusion_reason: str | None
    retrieved_chars: int | None
    selected_chars: int | None
    truncated: bool
    transform: str | None


@dataclass(frozen=True)
class SubjectContext:
    """Sanitized projection of the immutable TurnContext and resolved scopes."""

    actor: ResourceRef | None
    turn_ref: ResourceRef | None
    scopes: tuple[ResourceRef, ...]
    include_project_context: bool | None
    strict_project_scope: bool
    automatic_context_suppressed: bool
    verified_project_attachment: bool


@dataclass(frozen=True)
class ContextRequest:
    """Provider-request observation stripped of prompt/model bodies."""

    request_index: int | None
    request_kind: str | None
    observed_provider: str | None
    observed_model: str | None
    context_window_tokens: int | None
    response_tokens_reserved: int | None
    input_tokens: int | None
    remaining_tokens: int | None
    measurement: str | None
    component_categories: tuple[str, ...]
    request_hash: str


@dataclass(frozen=True)
class ContextManifest:
    """WS1 shadow-only, sanitized Work Intelligence observation artifact."""

    schema_version: str
    producer_version: str
    sanitizer_version: str
    mode: str
    subject: SubjectContext
    resources: tuple[ResourceRef, ...]
    evidence: tuple[EvidenceRef, ...]
    policy_decisions: tuple[PolicyDecision, ...]
    layers: tuple[ContextLayer, ...]
    requests: tuple[ContextRequest, ...]
    bundle_char_budget: int | None
    omission_reason_counts: dict[str, int]
    reproducibility_hashes: dict[str, str]
    manifest_hash: str
    # Optional typed Work Intelligence sidecar.  ``None`` preserves the exact
    # legacy WS1 serialized shape; the serializer emits this field only for
    # the explicit ``wi-core-1`` producer.
    work_intelligence: dict[str, Any] | None = None


def stable_manifest_json(value: Any) -> str:
    """Return the repository's stable compact JSON form for Manifest hashing."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def stable_manifest_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        stable_manifest_json(value).encode("utf-8")
    ).hexdigest()


def _observation_value(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    return str(value or "")


def _safe_manifest_token(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    if not text or not _MANIFEST_TOKEN_RE.fullmatch(text):
        return None
    return text


def observation_ref_hash(kind: Any, value: Any) -> str | None:
    """Hash a resource/evidence locator without persisting the locator itself."""

    normalized_kind = _safe_manifest_token(kind)
    if not normalized_kind or value is None:
        return None
    text = _observation_value(value)
    if not text:
        return None
    payload = (
        "aoitalk.context_manifest.ref.v1"
        + "\0"
        + normalized_kind.casefold()
        + "\0"
        + text
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_hash(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().casefold()
    return text if _MANIFEST_HASH_RE.fullmatch(text) else None


def _safe_version(value: Any) -> str | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 1_000_000_000 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _MANIFEST_VERSION_RE.fullmatch(text) else None


def _safe_non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        not math.isfinite(number)
        or number < 0
        or number > 1_000_000_000
        or not number.is_integer()
    ):
        return None
    return int(number)


def _config_value(config: Any, key: str, default: Any = None) -> Any:
    try:
        if isinstance(config, Mapping):
            current: Any = config
            for part in key.split("."):
                if not isinstance(current, Mapping) or part not in current:
                    break
                current = current[part]
            else:
                return current
        getter = getattr(config, "get", None)
        if callable(getter):
            return getter(key, default)
    except Exception:
        return default
    return default


def _literal_config_bool(config: Any, key: str, default: bool = False) -> bool:
    value = _config_value(config, key, None)
    return value if type(value) is bool else default


def context_manifest_persistence_enabled(config: Any) -> bool:
    """Malformed/missing config always selects the legacy path."""

    return bool(
        _literal_config_bool(
            config,
            "work_intelligence.context_manifest.enabled",
            False,
        )
        and _literal_config_bool(
            config,
            "work_intelligence.context_manifest.shadow_mode",
            False,
        )
        and _literal_config_bool(
            config,
            "work_intelligence.context_manifest.persist_metadata",
            False,
        )
    )


def _safe_resource_kind(value: Any) -> str | None:
    kind = _safe_manifest_token(value)
    if kind is None:
        return None
    normalized = kind.casefold()
    return normalized if normalized in _MANIFEST_RESOURCE_KINDS else None


def _resource(
    *,
    kind: Any,
    ref_hash: Any,
    relation: Any = None,
    source: Any = None,
    version: Any = None,
    freshness: Any = None,
    supersedes_ref_hash: Any = None,
) -> ResourceRef | None:
    safe_kind = _safe_resource_kind(kind)
    safe_ref_hash = _safe_hash(ref_hash)
    if not safe_kind or not safe_ref_hash:
        return None
    safe_relation = (
        str(relation).strip().casefold()
        if relation is not None
        and str(relation).strip().casefold() in _MANIFEST_RELATIONS
        else None
    )
    safe_source = _safe_manifest_token(source)
    safe_freshness = _safe_version(freshness)
    return ResourceRef(
        kind=safe_kind,
        ref_hash=safe_ref_hash,
        relation=safe_relation,
        source=safe_source,
        version=_safe_version(version),
        freshness=(
            str(safe_freshness) if safe_freshness is not None else None
        ),
        supersedes_ref_hash=_safe_hash(supersedes_ref_hash),
    )


def _resource_from_raw(
    *,
    kind: Any,
    value: Any,
    relation: Any = None,
    source: Any = None,
    version: Any = None,
    freshness: Any = None,
) -> ResourceRef | None:
    safe_kind = _safe_resource_kind(kind)
    if safe_kind is None:
        return None
    return _resource(
        kind=safe_kind,
        ref_hash=observation_ref_hash(safe_kind, value),
        relation=relation,
        source=source,
        version=version,
        freshness=freshness,
    )


def _manifest_debug(bundle: Any) -> dict[str, Any]:
    debug = getattr(bundle, "debug", None)
    return debug if isinstance(debug, dict) else {}


def _project_context_observation_enabled(turn_context: Any) -> bool:
    value = getattr(turn_context, "include_project_context", None)
    return value is None or (type(value) is bool and value)


def _trusted_subject(
    bundle: Any,
    turn_context: Any,
) -> tuple[SubjectContext, bool, dict[str, ResourceRef], bool]:
    debug = _manifest_debug(bundle)
    trusted_user_id = str(getattr(turn_context, "user_id", None) or "").strip()
    debug_user_id = str(debug.get("user_id") or "").strip()
    actor = _resource_from_raw(
        kind="user",
        value=trusted_user_id,
        relation="turn",
        source="TurnContext",
    ) if trusted_user_id else None

    preliminary_bundle_trusted = bool(
        bundle is not None
        and actor is not None
        and bool(debug_user_id)
        and debug_user_id == trusted_user_id
    )
    trusted_project = str(
        getattr(turn_context, "project_id", None) or ""
    ).strip()
    debug_project = str(debug.get("project_id") or "").strip()
    project_context_enabled = _project_context_observation_enabled(turn_context)
    trusted_session = str(
        getattr(turn_context, "session_id", None) or ""
    ).strip()
    debug_session = str(debug.get("session_id") or "").strip()
    trusted_task = str(
        getattr(turn_context, "task_id", None) or ""
    ).strip()
    debug_task = str(debug.get("task_id") or "").strip()
    debug_task_project = str(
        debug.get("task_project_id") or ""
    ).strip()

    def authorization_value(key: str) -> tuple[bool | None, bool]:
        if key not in debug:
            return None, False
        value = debug.get(key)
        if type(value) is bool:
            return value, False
        return None, True

    project_authorized, project_authorization_malformed = (
        authorization_value("project_scope_authorized")
    )
    session_authorized, session_authorization_malformed = (
        authorization_value("session_scope_authorized")
    )
    task_authorized, task_authorization_malformed = authorization_value(
        "task_scope_authorized"
    )
    task_scope_matches = bool(
        trusted_task
        and debug_task == trusted_task
        and not task_authorization_malformed
        and task_authorized is not False
    )
    task_project_binding_valid = bool(
        task_scope_matches
        and debug_project
        and debug_task_project
        and debug_task_project == debug_project
    )
    direct_project_mismatch = bool(
        preliminary_bundle_trusted
        and trusted_project
        and (
            not debug_project
            or trusted_project != debug_project
        )
    )
    extra_project_mismatch = bool(
        preliminary_bundle_trusted
        and debug_project
        and not trusted_project
        and not task_project_binding_valid
    )
    task_identity_mismatch = bool(
        preliminary_bundle_trusted
        and (
            (trusted_task and not task_scope_matches)
            or (debug_task and not trusted_task)
        )
    )
    task_project_mismatch = bool(
        preliminary_bundle_trusted
        and task_scope_matches
        and not task_project_binding_valid
    )
    session_identity_mismatch = bool(
        preliminary_bundle_trusted
        and (
            bool(trusted_session) != bool(debug_session)
            or (
                trusted_session
                and debug_session
                and trusted_session != debug_session
            )
        )
    )
    authorization_mismatch = bool(
        preliminary_bundle_trusted
        and (
            project_authorization_malformed
            or session_authorization_malformed
            or task_authorization_malformed
            or (
                (trusted_project or debug_project)
                and project_authorized is not True
            )
            or session_authorized is False
            or task_authorized is False
        )
    )
    scope_mismatch = bool(
        direct_project_mismatch
        or extra_project_mismatch
        or task_identity_mismatch
        or task_project_mismatch
        or session_identity_mismatch
        or authorization_mismatch
    )
    bundle_trusted = preliminary_bundle_trusted and not scope_mismatch

    scope_refs: dict[str, ResourceRef] = {}

    if bundle_trusted:
        if (
            trusted_session
            and debug_session == trusted_session
            and session_authorized is not False
        ):
            ref = _resource_from_raw(
                kind="session",
                value=debug_session,
                relation="resolved_scope",
                source="ConversationSession",
            )
            if ref:
                scope_refs["session"] = ref

        if task_scope_matches:
            ref = _resource_from_raw(
                kind="task",
                value=debug_task,
                relation="resolved_scope",
                source="Task",
            )
            if ref:
                scope_refs["task"] = ref

        project_matches_turn = bool(
            trusted_project and debug_project == trusted_project
        )
        project_derived_from_trusted_task = bool(
            debug_project
            and "task" in scope_refs
            and debug_task_project
            and debug_task_project == debug_project
        )
        if project_context_enabled and project_authorized is True and (
            project_matches_turn or project_derived_from_trusted_task
        ):
            ref = _resource_from_raw(
                kind="project",
                value=debug_project,
                relation="resolved_scope",
                source="ProjectContextResolver",
            )
            if ref:
                scope_refs["project"] = ref

    raw_turn_id = (
        getattr(turn_context, "message_id", None)
        or getattr(turn_context, "client_message_id", None)
    )
    turn_kind = (
        "message"
        if getattr(turn_context, "message_id", None)
        else "client_message"
    )
    turn_ref = (
        _resource_from_raw(
            kind=turn_kind,
            value=raw_turn_id,
            relation="turn",
            source="TurnContext",
        )
        if raw_turn_id
        else None
    )

    subject = SubjectContext(
        actor=actor,
        turn_ref=turn_ref,
        scopes=tuple(
            scope_refs[key]
            for key in ("session", "project", "task")
            if key in scope_refs
        ),
        include_project_context=(
            getattr(turn_context, "include_project_context", None)
            if type(
                getattr(turn_context, "include_project_context", None)
            ) is bool
            else None
        ),
        strict_project_scope=bool(
            getattr(turn_context, "strict_project_scope", False)
        ),
        automatic_context_suppressed=bool(
            getattr(turn_context, "suppress_automatic_context", False)
        ),
        verified_project_attachment=bool(
            getattr(turn_context, "verified_project_attachment", False)
        ),
    )
    return subject, bundle_trusted, scope_refs, scope_mismatch


def _manifest_layers(
    bundle: Any,
    *,
    trusted: bool,
    project_context_enabled: bool = True,
) -> tuple[tuple[ContextLayer, ...], dict[str, int]]:
    if not trusted or bundle is None or not hasattr(bundle, "render_with_trace"):
        return (), {}

    _rendered, trace = bundle.render_with_trace()
    if not isinstance(trace, list):
        return (), {}

    layers: list[ContextLayer] = []
    omitted: dict[str, int] = {}
    for item in trace[:32]:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip()
        source = _MANIFEST_LAYER_SOURCES.get(category)
        if not source:
            continue
        if not project_context_enabled and category in _PROJECT_CONTEXT_LAYER_CATEGORIES:
            omitted["project_context_disabled"] = (
                omitted.get("project_context_disabled", 0) + 1
            )
            continue
        status = str(item.get("status") or "").strip().casefold()
        if status not in {"active", "deferred", "failed"}:
            continue
        raw_reason = str(item.get("selection_reason") or "").strip()
        reason = (
            raw_reason if raw_reason in _MANIFEST_SELECTION_REASONS else None
        )
        retrieved = _safe_non_negative_int(item.get("retrieved_chars"))
        selected = _safe_non_negative_int(item.get("selected_chars"))
        truncated = bool(
            status == "active"
            and retrieved is not None
            and selected is not None
            and selected < retrieved
        )
        layers.append(
            ContextLayer(
                category=category,
                source=source,
                status=status,
                inclusion_reason=reason,
                retrieved_chars=retrieved,
                selected_chars=selected,
                truncated=truncated,
                transform="budget_clip" if truncated else None,
            )
        )
        if status != "active" and reason:
            omitted[reason] = omitted.get(reason, 0) + 1
    return tuple(layers), omitted


def _dedupe_resources(values: Iterable[ResourceRef]) -> tuple[ResourceRef, ...]:
    by_key: dict[
        tuple[str, str, str, str],
        ResourceRef,
    ] = {}
    for item in values:
        key = (
            item.kind,
            item.ref_hash,
            item.relation or "",
            item.source or "",
        )
        by_key[key] = item
    return tuple(
        by_key[key]
        for key in sorted(by_key)
    )[:MAX_MANIFEST_RESOURCES]


def _dedupe_evidence(values: Iterable[EvidenceRef]) -> tuple[EvidenceRef, ...]:
    by_key: dict[tuple[str, str, str], EvidenceRef] = {}
    for item in values:
        key = (
            item.kind,
            item.locator_hash,
            item.source_type or "",
        )
        by_key[key] = item
    return tuple(
        by_key[key]
        for key in sorted(by_key)
    )[:MAX_MANIFEST_EVIDENCE]


def _manifest_index_provenance_hashes(
    bundle: Any,
    *,
    key: str,
    project_ref: ResourceRef,
    group: str,
) -> set[str]:
    provenance = _manifest_debug(bundle).get(key)
    if not isinstance(provenance, dict):
        return set()
    if _safe_hash(provenance.get("project_ref_hash")) != project_ref.ref_hash:
        return set()
    grouped = provenance.get("node_ref_hashes")
    if not isinstance(grouped, dict):
        return set()
    values = grouped.get(group)
    if not isinstance(values, list):
        return set()
    return {
        digest
        for raw in values[:64]
        if (digest := _safe_hash(raw))
    }


def _project_knowledge_projection(
    bundle: Any,
    *,
    project_ref: ResourceRef | None,
) -> tuple[list[ResourceRef], list[EvidenceRef]]:
    if project_ref is None:
        return [], []
    value = getattr(bundle, "project_knowledge_index", None)
    if not isinstance(value, dict):
        return [], []

    resources: list[ResourceRef] = []
    evidence: list[EvidenceRef] = []
    for key, default_relation in (
        ("canonical_nodes", "canonical"),
        ("related_nodes", "related"),
    ):
        allowed_hashes = _manifest_index_provenance_hashes(
            bundle,
            key="project_knowledge_manifest_provenance",
            project_ref=project_ref,
            group=key,
        )
        if not allowed_hashes:
            continue
        rows = value.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows[:64]:
            if not isinstance(row, dict):
                continue
            node_id = row.get("id") or row.get("node_id")
            node_ref_hash = observation_ref_hash("knowledge_node", node_id)
            if node_ref_hash not in allowed_hashes:
                continue
            relation = str(
                row.get("relation_type") or default_relation
            ).strip().casefold()
            if relation not in {"canonical", "related", "reference"}:
                continue
            updated_at = _safe_version(row.get("updated_at"))
            ref = _resource(
                kind="knowledge_node",
                ref_hash=node_ref_hash,
                relation=relation,
                source="ProjectKnowledgeService",
                version=updated_at,
                freshness=updated_at,
            )
            if ref is None:
                continue
            resources.append(ref)
            evidence.append(
                EvidenceRef(
                    kind="project_knowledge_node",
                    locator_hash=ref.ref_hash,
                    source_type="ProjectKnowledgeService",
                    version=updated_at,
                )
            )
    return resources, evidence


def _accessible_knowledge_projection(
    bundle: Any,
    *,
    project_ref: ResourceRef | None,
) -> tuple[list[ResourceRef], list[EvidenceRef]]:
    if project_ref is None:
        return [], []
    value = getattr(bundle, "accessible_knowledge_index", None)
    if not isinstance(value, dict):
        return [], []

    resources: list[ResourceRef] = []
    evidence: list[EvidenceRef] = []
    for group in ("project", "personal"):
        allowed_hashes = _manifest_index_provenance_hashes(
            bundle,
            key="accessible_knowledge_manifest_provenance",
            project_ref=project_ref,
            group=group,
        )
        if not allowed_hashes:
            continue
        rows = value.get(group)
        if not isinstance(rows, list):
            continue
        for row in rows[:64]:
            if not isinstance(row, dict):
                continue
            node_ref_hash = observation_ref_hash(
                "knowledge_node",
                row.get("id"),
            )
            if node_ref_hash not in allowed_hashes:
                continue
            relation = (
                str(row.get("relation") or "").strip().casefold()
                if group == "project"
                else "personal"
            )
            if relation not in {"canonical", "related", "personal"}:
                continue
            ref = _resource(
                kind="knowledge_node",
                ref_hash=node_ref_hash,
                relation=relation,
                source="DocsScope",
            )
            if ref is None:
                continue
            resources.append(ref)
            evidence.append(
                EvidenceRef(
                    kind="docs_scope_node",
                    locator_hash=ref.ref_hash,
                    source_type="DocsScope",
                    version=None,
                )
            )
    return resources, evidence


def _memory_projection(
    bundle: Any,
    *,
    subject: SubjectContext,
    scope_refs: dict[str, ResourceRef],
    trusted: bool,
) -> tuple[list[ResourceRef], list[EvidenceRef]]:
    if not trusted:
        return [], []
    rows = _manifest_debug(bundle).get("context_memory_manifest_lineage")
    if not isinstance(rows, list):
        return [], []

    allowed_scope_hashes = {
        key: value.ref_hash for key, value in scope_refs.items()
    }
    if subject.actor:
        allowed_scope_hashes["user"] = subject.actor.ref_hash

    resources: list[ResourceRef] = []
    evidence: list[EvidenceRef] = []
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        scope = str(row.get("scope_type") or "").strip().casefold()
        if scope not in _MANIFEST_SCOPE_KINDS:
            continue
        scope_hash = _safe_hash(row.get("scope_ref_hash"))
        if scope != "global":
            if (
                scope not in allowed_scope_hashes
                or scope_hash != allowed_scope_hashes[scope]
            ):
                continue

        ref = _resource(
            kind="scoped_memory",
            ref_hash=row.get("memory_ref_hash"),
            relation=scope,
            source="ScopedMemoryService",
            version=row.get("version"),
            freshness=row.get("updated_at"),
            supersedes_ref_hash=row.get("supersedes_ref_hash"),
        )
        if ref is None:
            continue
        resources.append(ref)

        source_type = _safe_manifest_token(row.get("source_type"))
        source_ref_hash = _safe_hash(row.get("source_ref_hash"))
        if source_ref_hash:
            evidence.append(
                EvidenceRef(
                    kind="memory_source",
                    locator_hash=source_ref_hash,
                    source_type=source_type,
                    version=_safe_version(row.get("version")),
                )
            )
        raw_evidence = row.get("evidence_ref_hashes")
        if isinstance(raw_evidence, list):
            for raw_hash in raw_evidence[:16]:
                evidence_hash = _safe_hash(raw_hash)
                if evidence_hash:
                    evidence.append(
                        EvidenceRef(
                            kind="memory_evidence",
                            locator_hash=evidence_hash,
                            source_type=source_type,
                            version=_safe_version(row.get("version")),
                        )
                    )
    return resources, evidence


def _manifest_work_intelligence_projection(
    bundle: Any,
    *,
    trusted: bool,
    project_context_enabled: bool,
) -> dict[str, Any] | None:
    """Sanitize the transient compiler sidecar for historical observation.

    This function is intentionally independent from prompt rendering.  It
    accepts only hashed references, bounded counters, allowlisted relation and
    resource kinds, and source version/freshness metadata.  Titles, bodies,
    raw IDs, advisory memory text, and arbitrary provider data are discarded.
    """

    if not trusted or not project_context_enabled or bundle is None:
        return None
    value = getattr(bundle, "work_intelligence", None)
    if value is None:
        return None
    try:
        raw = value.to_dict(hashed=True) if hasattr(value, "to_dict") else value
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None

    def safe_hash(value: Any) -> str | None:
        return _safe_hash(value)

    def safe_token(value: Any) -> str | None:
        return _safe_manifest_token(value)

    def safe_version(value: Any) -> str | int | None:
        return _safe_version(value)

    def safe_count(value: Any) -> int | None:
        return _safe_non_negative_int(value)

    def safe_bool(value: Any) -> bool | None:
        return value if type(value) is bool else None

    items: list[dict[str, Any]] = []
    for row in (raw.get("items") if isinstance(raw.get("items"), list) else [])[:64]:
        if not isinstance(row, dict):
            continue
        ref_hash = safe_hash(row.get("ref_hash"))
        kind = safe_token(row.get("kind"))
        if ref_hash is None or kind is None:
            continue
        safe_item: dict[str, Any] = {
            "kind": kind,
            "ref_hash": ref_hash,
            "status": safe_token(row.get("status")),
            "priority": (
                safe_count(row.get("priority"))
                if isinstance(row.get("priority"), (int, float))
                else safe_token(row.get("priority"))
            ),
            "score": row.get("score") if isinstance(row.get("score"), (int, float)) and math.isfinite(float(row.get("score"))) else 0,
            "version": safe_version(row.get("version")),
            "freshness": safe_version(row.get("freshness")),
            "uncertain": safe_bool(row.get("uncertain")) is True,
            "advisory_conflict": safe_bool(row.get("advisory_conflict")) is True,
        }
        evidence_rows = row.get("evidence")
        if isinstance(evidence_rows, list):
            refs: list[str] = []
            for evidence in evidence_rows[:16]:
                if not isinstance(evidence, dict):
                    continue
                evidence_hash = safe_hash(evidence.get("ref_hash"))
                if evidence_hash:
                    refs.append(evidence_hash)
            if refs:
                safe_item["evidence_ref_hashes"] = list(dict.fromkeys(refs))
        items.append(safe_item)

    evidence: list[dict[str, Any]] = []
    for row in (raw.get("evidence") if isinstance(raw.get("evidence"), list) else [])[:MAX_MANIFEST_EVIDENCE]:
        if not isinstance(row, dict):
            continue
        locator = safe_hash(row.get("ref_hash") or row.get("locator_hash"))
        kind = safe_token(row.get("kind"))
        if not locator or not kind:
            continue
        evidence.append(
            {
                "kind": kind,
                "locator_hash": locator,
                "source_type": safe_token(row.get("source" ) or row.get("source_type")),
                "version": safe_version(row.get("version")),
                "freshness": safe_version(row.get("freshness")),
                "uncertain": safe_bool(row.get("uncertain")) is True,
            }
        )

    relations: list[dict[str, Any]] = []
    for row in (raw.get("relations") if isinstance(raw.get("relations"), list) else [])[:MAX_MANIFEST_EVIDENCE]:
        if not isinstance(row, dict):
            continue
        relation = safe_token(row.get("relation_type"))
        subject = safe_hash(row.get("subject_ref_hash"))
        target = safe_hash(row.get("target_ref_hash"))
        target_kind = safe_token(row.get("target_kind"))
        if relation not in _MANIFEST_RELATIONS or not subject or not target or not target_kind:
            continue
        refs = [
            digest
            for raw_ref in (row.get("evidence_ref_hashes") if isinstance(row.get("evidence_ref_hashes"), list) else [])[:16]
            if (digest := safe_hash(raw_ref))
        ]
        relations.append(
            {
                "relation_type": relation,
                "subject_ref_hash": subject,
                "target_kind": target_kind,
                "target_ref_hash": target,
                "evidence_ref_hashes": list(dict.fromkeys(refs)),
                "confidence": row.get("confidence") if isinstance(row.get("confidence"), (int, float)) and math.isfinite(float(row.get("confidence"))) else 0,
                "uncertain": safe_bool(row.get("uncertain")) is True,
            }
        )

    people: list[dict[str, Any]] = []
    for row in (raw.get("people") if isinstance(raw.get("people"), list) else [])[:32]:
        if not isinstance(row, dict):
            continue
        person_ref = safe_hash(row.get("person_ref_hash"))
        if not person_ref:
            continue
        people.append(
            {
                "person_ref_hash": person_ref,
                "evidence_count": safe_count(row.get("evidence_count")) or 0,
                "score": row.get("score") if isinstance(row.get("score"), (int, float)) and math.isfinite(float(row.get("score"))) else 0,
                "uncertain": safe_bool(row.get("uncertain")) is True,
            }
        )

    freshness_raw = raw.get("freshness")
    freshness: dict[str, str | int] = {}
    if isinstance(freshness_raw, dict):
        for key, item in list(freshness_raw.items())[:8]:
            token = safe_token(key)
            version = safe_version(item)
            if token and version is not None:
                freshness[token] = version
    omissions: dict[str, int] = {}
    if isinstance(raw.get("omissions"), dict):
        for key, count in raw["omissions"].items():
            token = safe_token(key)
            number = safe_count(count)
            if token and number is not None and number > 0:
                omissions[token] = number
    # ``trace`` is converted to counts/offset-free structural layer metadata;
    # no text or source IDs are persisted.
    layers: list[dict[str, Any]] = []
    if items or people or evidence or relations:
        transient_block = getattr(bundle, "work_intelligence_block", "") or ""
        if not transient_block:
            transient = getattr(bundle, "work_intelligence", None)
            transient_block = getattr(transient, "block", "") if transient is not None else ""
        layers.append(
            {
                "category": "work_intelligence",
                "source": "ContextBundle.work_intelligence_block",
                "status": "active",
                "selected_chars": safe_count(len(transient_block)) or 0,
                "item_count": len(items),
                "evidence_count": len(evidence),
            }
        )
    if not (items or people or evidence or relations or omissions):
        return None
    return {
        "schema_version": "1.0",
        "producer_version": "wi-core-1",
        "items": items,
        "people": people,
        "evidence": evidence,
        "relations": relations,
        "freshness": freshness,
        "omissions": dict(sorted(omissions.items())),
        "layers": layers,
    }


def _policy_ref(
    *,
    authority: str,
    scope: str,
    decision: str,
    resource_ref_hash: str | None,
) -> PolicyDecision:
    decision_ref = observation_ref_hash(
        "policy_decision",
        {
            "authority": authority,
            "scope": scope,
            "decision": decision,
            "resource_ref_hash": resource_ref_hash,
        },
    )
    assert decision_ref is not None
    return PolicyDecision(
        authority=authority,
        scope=scope,
        decision=decision,
        resource_ref_hash=resource_ref_hash,
        decision_ref_hash=decision_ref,
    )


def _policy_projection(
    bundle: Any,
    *,
    scope_refs: dict[str, ResourceRef],
    project_mismatch: bool,
    layers: tuple[ContextLayer, ...],
    project_context_enabled: bool = True,
) -> tuple[PolicyDecision, ...]:
    debug = _manifest_debug(bundle)
    decisions: list[PolicyDecision] = []

    if project_context_enabled and project_mismatch:
        decisions.append(
            _policy_ref(
                authority="TurnContext",
                scope="project",
                decision="deny",
                resource_ref_hash=None,
            )
        )
    elif project_context_enabled and type(debug.get("project_scope_authorized")) is bool:
        project_ref = scope_refs.get("project")
        decisions.append(
            _policy_ref(
                authority="ProjectContextResolver",
                scope="project",
                decision=(
                    "allow"
                    if debug["project_scope_authorized"] is True
                    and project_ref is not None
                    else "deny"
                ),
                resource_ref_hash=(
                    project_ref.ref_hash if project_ref else None
                ),
            )
        )

    for scope, authority, debug_key in (
        ("session", "ConversationSession", "session_scope_authorized"),
        ("task", "Task", "task_scope_authorized"),
    ):
        ref = scope_refs.get(scope)
        if type(debug.get(debug_key)) is bool or ref is not None:
            decisions.append(
                _policy_ref(
                    authority=authority,
                    scope=scope,
                    decision=(
                        "allow"
                        if ref is not None
                        and debug.get(debug_key) is not False
                        else "deny"
                    ),
                    resource_ref_hash=ref.ref_hash if ref else None,
                )
            )

    active_categories = {
        item.category for item in layers if item.status == "active"
    }
    project_ref = scope_refs.get("project")
    if project_ref and "project_knowledge_index" in active_categories:
        decisions.append(
            _policy_ref(
                authority="ProjectKnowledgeService",
                scope="project_knowledge",
                decision="allow",
                resource_ref_hash=project_ref.ref_hash,
            )
        )
    if project_ref and "accessible_knowledge_index" in active_categories:
        decisions.append(
            _policy_ref(
                authority="DocsScope",
                scope="accessible_knowledge",
                decision="allow",
                resource_ref_hash=project_ref.ref_hash,
            )
        )

    return tuple(
        sorted(
            decisions,
            key=lambda item: (
                item.authority,
                item.scope,
                item.decision,
                item.decision_ref_hash,
            ),
        )
    )


def _request_projection(
    snapshots: Iterable[dict[str, Any]],
    *,
    project_context_enabled: bool = True,
) -> tuple[ContextRequest, ...]:
    clean = sanitized_snapshot_series(snapshots)
    if not clean:
        return ()
    raw_requests = clean.get("requests")
    values = (
        raw_requests[-MAX_MANIFEST_REQUESTS:]
        if isinstance(raw_requests, list) and raw_requests
        else [clean]
    )
    requests: list[ContextRequest] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        categories: set[str] = set()
        components = item.get("components")
        if isinstance(components, list):
            for component_value in components[:MAX_COMPONENTS]:
                if not isinstance(component_value, dict):
                    continue
                category = str(
                    component_value.get("category") or ""
                ).strip()
                if category not in _MANIFEST_COMPONENT_CATEGORIES:
                    continue
                if (
                    not project_context_enabled
                    and category in _PROJECT_CONTEXT_LAYER_CATEGORIES
                ):
                    continue
                categories.add(category)

        request_payload = {
            "request_index": _safe_non_negative_int(
                item.get("request_index")
            ),
            "request_kind": _safe_manifest_token(
                item.get("request_kind")
            ),
            "observed_provider": _safe_manifest_token(
                item.get("provider")
            ),
            "observed_model": _safe_manifest_token(item.get("model")),
            "context_window_tokens": _safe_non_negative_int(
                item.get("context_window_tokens")
            ),
            "response_tokens_reserved": _safe_non_negative_int(
                item.get("response_tokens_reserved")
            ),
            "input_tokens": _safe_non_negative_int(
                item.get("input_tokens")
            ),
            "remaining_tokens": _safe_non_negative_int(
                item.get("remaining_tokens")
            ),
            "measurement": _safe_manifest_token(
                item.get("measurement")
            ),
            "component_categories": tuple(sorted(categories)),
        }
        requests.append(
            ContextRequest(
                **request_payload,
                request_hash=stable_manifest_hash(request_payload),
            )
        )
    return tuple(requests)


def _explicit_turn_resources(
    turn_context: Any,
) -> tuple[list[ResourceRef], int]:
    resources: list[ResourceRef] = []
    unsupported_kind_count = 0
    for raw in list(
        getattr(turn_context, "explicit_references", ()) or ()
    )[:64]:
        identifier = getattr(raw, "id", None)
        if not identifier:
            continue
        kind = _safe_resource_kind(getattr(raw, "kind", None))
        if kind is None:
            unsupported_kind_count += 1
            continue
        ref = _resource_from_raw(
            kind=kind,
            value=identifier,
            relation="explicit",
            source="TurnContext",
        )
        if ref:
            resources.append(ref)

    tool_call_id = getattr(turn_context, "tool_call_id", None)
    if tool_call_id:
        ref = _resource_from_raw(
            kind="tool_call",
            value=tool_call_id,
            relation="turn",
            source="TurnContext",
        )
        if ref:
            resources.append(ref)
    return resources, unsupported_kind_count


def build_context_manifest(
    client: Any,
    *,
    turn_context: Any = None,
) -> ContextManifest:
    """Build one inert sanitized Manifest from already-resolved observations."""

    if turn_context is None:
        from ..services.turn_context import get_turn_context

        turn_context = get_turn_context()

    bundle = getattr(client, "_current_context_bundle", None)
    snapshots = list(
        getattr(client, "_last_context_snapshots", None) or []
    )
    subject, bundle_trusted, scope_refs, project_mismatch = _trusted_subject(
        bundle,
        turn_context,
    )
    project_context_enabled = _project_context_observation_enabled(turn_context)
    layers, omission_counts = _manifest_layers(
        bundle,
        trusted=bundle_trusted,
        project_context_enabled=project_context_enabled,
    )

    resources, unsupported_resource_kinds = _explicit_turn_resources(
        turn_context
    )
    evidence: list[EvidenceRef] = []
    if unsupported_resource_kinds:
        omission_counts["unsupported_resource_kind"] = (
            unsupported_resource_kinds
        )

    if bundle_trusted:
        project_resources, project_evidence = (
            _project_knowledge_projection(
                bundle,
                project_ref=scope_refs.get("project"),
            )
        )
        resources.extend(project_resources)
        evidence.extend(project_evidence)

        docs_resources, docs_evidence = _accessible_knowledge_projection(
            bundle,
            project_ref=scope_refs.get("project"),
        )
        resources.extend(docs_resources)
        evidence.extend(docs_evidence)

        memory_resources, memory_evidence = _memory_projection(
            bundle,
            subject=subject,
            scope_refs=scope_refs,
            trusted=bundle_trusted,
        )
        resources.extend(memory_resources)
        evidence.extend(memory_evidence)

    if project_mismatch:
        omission_counts["scope_mismatch"] = (
            omission_counts.get("scope_mismatch", 0) + 1
        )

    policy_decisions = (
        _policy_projection(
            bundle,
            scope_refs=scope_refs,
            project_mismatch=project_mismatch,
            layers=layers,
            project_context_enabled=project_context_enabled,
        )
        if bundle_trusted
        else ()
    )
    denied = sum(
        1 for item in policy_decisions if item.decision == "deny"
    )
    if denied:
        omission_counts["policy_denied"] = (
            omission_counts.get("policy_denied", 0) + denied
        )

    safe_resources = _dedupe_resources(resources)
    safe_evidence = _dedupe_evidence(evidence)
    bundle_observation_safe = bundle is None or bundle_trusted
    requests = (
        _request_projection(
            snapshots,
            project_context_enabled=project_context_enabled,
        )
        if bundle_observation_safe
        else ()
    )
    bundle_char_budget = (
        _safe_non_negative_int(getattr(bundle, "max_chars", None))
        if bundle_trusted and project_context_enabled
        else None
    )
    work_intelligence = _manifest_work_intelligence_projection(
        bundle,
        trusted=bundle_trusted,
        project_context_enabled=project_context_enabled,
    )
    if work_intelligence:
        # Keep the sidecar outside provider request fields, but include it in
        # the reproducibility hash so observers can correlate a live compile.
        wi_omissions = work_intelligence.get("omissions")
        if isinstance(wi_omissions, dict):
            for key, count in wi_omissions.items():
                if isinstance(count, int) and count > 0:
                    omission_counts[key] = omission_counts.get(key, 0) + count

    context_projection = {
        "resources": [asdict(item) for item in safe_resources],
        "evidence": [asdict(item) for item in safe_evidence],
        "policy_decisions": [
            asdict(item) for item in policy_decisions
        ],
        "layers": [asdict(item) for item in layers],
        "bundle_char_budget": bundle_char_budget,
        "omission_reason_counts": dict(sorted(omission_counts.items())),
    }
    if work_intelligence:
        context_projection["work_intelligence"] = work_intelligence
    request_projection = [asdict(item) for item in requests]
    hashes = {
        "turn": stable_manifest_hash(asdict(subject)),
        "context": stable_manifest_hash(context_projection),
        "requests": stable_manifest_hash(request_projection),
    }

    manifest = ContextManifest(
        schema_version=CONTEXT_MANIFEST_SCHEMA_VERSION,
        producer_version=(
            CONTEXT_MANIFEST_WORK_INTELLIGENCE_PRODUCER_VERSION
            if work_intelligence
            else CONTEXT_MANIFEST_PRODUCER_VERSION
        ),
        sanitizer_version=CONTEXT_MANIFEST_SANITIZER_VERSION,
        mode="shadow",
        subject=subject,
        resources=safe_resources,
        evidence=safe_evidence,
        policy_decisions=policy_decisions,
        layers=layers,
        requests=requests,
        bundle_char_budget=bundle_char_budget,
        omission_reason_counts=dict(sorted(omission_counts.items())),
        reproducibility_hashes=hashes,
        manifest_hash="",
        work_intelligence=work_intelligence,
    )
    hash_payload = asdict(manifest)
    hash_payload.pop("manifest_hash", None)
    if hash_payload.get("work_intelligence") is None:
        hash_payload.pop("work_intelligence", None)
    return replace(
        manifest,
        manifest_hash=stable_manifest_hash(hash_payload),
    )


def is_supported_context_manifest(value: Any) -> bool:
    """Future consumers must explicitly recognize all three versions."""

    return bool(
        isinstance(value, dict)
        and value.get("schema_version")
        == CONTEXT_MANIFEST_SCHEMA_VERSION
        and value.get("producer_version")
        in CONTEXT_MANIFEST_SUPPORTED_PRODUCER_VERSIONS
        and value.get("sanitizer_version")
        == CONTEXT_MANIFEST_SANITIZER_VERSION
        and value.get("mode") == "shadow"
    )


def _canonical_manifest_hash(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if value != value.strip() or not _MANIFEST_HASH_RE.fullmatch(value):
        return None
    return value


def _manifest_exact_keys(value: Any, keys: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _manifest_optional_token(value: Any) -> bool:
    return value is None or _safe_manifest_token(value) == value


def _manifest_optional_hash(value: Any) -> bool:
    return value is None or _canonical_manifest_hash(value) is not None


def _manifest_optional_version(value: Any) -> bool:
    return value is None or _safe_version(value) == value


def _manifest_optional_count(value: Any) -> bool:
    return value is None or (
        type(value) is int and _safe_non_negative_int(value) == value
    )


def _valid_manifest_resource(value: Any) -> bool:
    keys = {
        "kind",
        "ref_hash",
        "relation",
        "source",
        "version",
        "freshness",
        "supersedes_ref_hash",
    }
    if not _manifest_exact_keys(value, keys):
        return False
    if _safe_resource_kind(value.get("kind")) != value.get("kind"):
        return False
    if _canonical_manifest_hash(value.get("ref_hash")) is None:
        return False
    relation = value.get("relation")
    if relation is not None and relation not in _MANIFEST_RELATIONS:
        return False
    return bool(
        _manifest_optional_token(value.get("source"))
        and _manifest_optional_version(value.get("version"))
        and _manifest_optional_version(value.get("freshness"))
        and _manifest_optional_hash(value.get("supersedes_ref_hash"))
    )


def _valid_manifest_evidence(value: Any) -> bool:
    keys = {"kind", "locator_hash", "source_type", "version"}
    return bool(
        _manifest_exact_keys(value, keys)
        and isinstance(value.get("kind"), str)
        and value.get("kind") in _MANIFEST_EVIDENCE_KINDS
        and _canonical_manifest_hash(value.get("locator_hash")) is not None
        and _manifest_optional_token(value.get("source_type"))
        and _manifest_optional_version(value.get("version"))
    )


def _valid_manifest_policy(value: Any) -> bool:
    keys = {
        "authority",
        "scope",
        "decision",
        "resource_ref_hash",
        "decision_ref_hash",
    }
    if not _manifest_exact_keys(value, keys):
        return False
    authority = value.get("authority")
    scope = value.get("scope")
    decision = value.get("decision")
    resource_ref_hash = value.get("resource_ref_hash")
    decision_ref_hash = value.get("decision_ref_hash")
    if (
        not isinstance(authority, str)
        or authority not in _MANIFEST_POLICY_AUTHORITIES
        or not isinstance(scope, str)
        or scope not in _MANIFEST_POLICY_SCOPES
        or decision not in {"allow", "deny"}
        or not _manifest_optional_hash(resource_ref_hash)
        or _canonical_manifest_hash(decision_ref_hash) is None
    ):
        return False
    return observation_ref_hash(
        "policy_decision",
        {
            "authority": authority,
            "scope": scope,
            "decision": decision,
            "resource_ref_hash": resource_ref_hash,
        },
    ) == decision_ref_hash


def _valid_manifest_layer(value: Any) -> bool:
    keys = {
        "category",
        "source",
        "status",
        "inclusion_reason",
        "retrieved_chars",
        "selected_chars",
        "truncated",
        "transform",
    }
    if not _manifest_exact_keys(value, keys):
        return False
    category = value.get("category")
    truncated = value.get("truncated")
    transform = value.get("transform")
    reason = value.get("inclusion_reason")
    return bool(
        category in _MANIFEST_LAYER_SOURCES
        and value.get("source") == _MANIFEST_LAYER_SOURCES[category]
        and value.get("status") in {"active", "deferred", "failed"}
        and (reason is None or reason in _MANIFEST_SELECTION_REASONS)
        and _manifest_optional_count(value.get("retrieved_chars"))
        and _manifest_optional_count(value.get("selected_chars"))
        and type(truncated) is bool
        and transform == ("budget_clip" if truncated else None)
    )


def _valid_manifest_request(value: Any) -> bool:
    keys = {
        "request_index",
        "request_kind",
        "observed_provider",
        "observed_model",
        "context_window_tokens",
        "response_tokens_reserved",
        "input_tokens",
        "remaining_tokens",
        "measurement",
        "component_categories",
        "request_hash",
    }
    if not _manifest_exact_keys(value, keys):
        return False
    categories = value.get("component_categories")
    if (
        not isinstance(categories, list)
        or len(categories) > MAX_COMPONENTS
        or categories != sorted(set(categories))
        or any(item not in _MANIFEST_COMPONENT_CATEGORIES for item in categories)
    ):
        return False
    for key in (
        "request_kind",
        "observed_provider",
        "observed_model",
        "measurement",
    ):
        if not _manifest_optional_token(value.get(key)):
            return False
    for key in (
        "request_index",
        "context_window_tokens",
        "response_tokens_reserved",
        "input_tokens",
        "remaining_tokens",
    ):
        if not _manifest_optional_count(value.get(key)):
            return False
    request_hash = _canonical_manifest_hash(value.get("request_hash"))
    if request_hash is None:
        return False
    request_payload = dict(value)
    request_payload.pop("request_hash", None)
    return stable_manifest_hash(request_payload) == request_hash


def _valid_manifest_work_intelligence(value: Any) -> bool:
    """Validate the optional hashed Work Intelligence sidecar shape."""

    if not isinstance(value, dict):
        return False
    required = {
        "schema_version",
        "producer_version",
        "items",
        "people",
        "evidence",
        "relations",
        "freshness",
        "omissions",
        "layers",
    }
    if set(value) != required:
        return False
    if value.get("schema_version") != "1.0" or value.get("producer_version") != CONTEXT_MANIFEST_WORK_INTELLIGENCE_PRODUCER_VERSION:
        return False
    items = value.get("items")
    if not isinstance(items, list) or len(items) > 64:
        return False
    for row in items:
        if not isinstance(row, dict):
            return False
        allowed = {
            "kind",
            "ref_hash",
            "status",
            "priority",
            "score",
            "version",
            "freshness",
            "uncertain",
            "advisory_conflict",
            "evidence_ref_hashes",
        }
        if not set(row).issubset(allowed) or not set(row).issuperset({"kind", "ref_hash", "status", "priority", "score", "version", "freshness", "uncertain", "advisory_conflict"}):
            return False
        if not _manifest_optional_token(row.get("kind")) or not _canonical_manifest_hash(row.get("ref_hash")):
            return False
        for key in ("status", "version", "freshness"):
            checker = _manifest_optional_token if key == "status" else _manifest_optional_version
            if not checker(row.get(key)):
                return False
        priority = row.get("priority")
        if priority is not None and not (_manifest_optional_token(priority) or _manifest_optional_count(priority)):
            return False
        score = row.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(float(score)):
            return False
        if type(row.get("uncertain")) is not bool or type(row.get("advisory_conflict")) is not bool:
            return False
        refs = row.get("evidence_ref_hashes", [])
        if not isinstance(refs, list) or len(refs) > 16 or any(_canonical_manifest_hash(item) is None for item in refs):
            return False

    people = value.get("people")
    if not isinstance(people, list) or len(people) > 32:
        return False
    for row in people:
        if not isinstance(row, dict) or set(row) != {"person_ref_hash", "evidence_count", "score", "uncertain"}:
            return False
        if _canonical_manifest_hash(row.get("person_ref_hash")) is None:
            return False
        if not _manifest_optional_count(row.get("evidence_count")):
            return False
        if not isinstance(row.get("score"), (int, float)) or isinstance(row.get("score"), bool) or not math.isfinite(float(row.get("score"))):
            return False
        if type(row.get("uncertain")) is not bool:
            return False

    evidence = value.get("evidence")
    if not isinstance(evidence, list) or len(evidence) > MAX_MANIFEST_EVIDENCE:
        return False
    for row in evidence:
        if not isinstance(row, dict):
            return False
        keys = {"kind", "locator_hash", "source_type", "version", "freshness", "uncertain"}
        if set(row) != keys:
            return False
        if not _manifest_optional_token(row.get("kind")) or _canonical_manifest_hash(row.get("locator_hash")) is None:
            return False
        if not _manifest_optional_token(row.get("source_type")) or not _manifest_optional_version(row.get("version")) or not _manifest_optional_version(row.get("freshness")) or type(row.get("uncertain")) is not bool:
            return False

    relations = value.get("relations")
    if not isinstance(relations, list) or len(relations) > MAX_MANIFEST_EVIDENCE:
        return False
    for row in relations:
        if not isinstance(row, dict):
            return False
        keys = {"relation_type", "subject_ref_hash", "target_kind", "target_ref_hash", "evidence_ref_hashes", "confidence", "uncertain"}
        if set(row) != keys:
            return False
        if row.get("relation_type") not in _MANIFEST_RELATIONS or _canonical_manifest_hash(row.get("subject_ref_hash")) is None or _canonical_manifest_hash(row.get("target_ref_hash")) is None or not _manifest_optional_token(row.get("target_kind")):
            return False
        refs = row.get("evidence_ref_hashes")
        if not isinstance(refs, list) or len(refs) > 16 or any(_canonical_manifest_hash(item) is None for item in refs):
            return False
        if not isinstance(row.get("confidence"), (int, float)) or isinstance(row.get("confidence"), bool) or not math.isfinite(float(row.get("confidence"))) or type(row.get("uncertain")) is not bool:
            return False

    freshness = value.get("freshness")
    if not isinstance(freshness, dict) or len(freshness) > 8:
        return False
    for key, item in freshness.items():
        if not _manifest_optional_token(key) or not _manifest_optional_version(item):
            return False
    omissions = value.get("omissions")
    if not isinstance(omissions, dict):
        return False
    for key, count in omissions.items():
        if not _manifest_optional_token(key) or not _manifest_optional_count(count) or count <= 0:
            return False
    layers = value.get("layers")
    if not isinstance(layers, list) or len(layers) > 8:
        return False
    for row in layers:
        if not isinstance(row, dict) or set(row) != {"category", "source", "status", "selected_chars", "item_count", "evidence_count"}:
            return False
        if row.get("category") != "work_intelligence" or not _manifest_optional_token(row.get("source")) or row.get("status") != "active":
            return False
        if any(not _manifest_optional_count(row.get(key)) for key in ("selected_chars", "item_count", "evidence_count")):
            return False
    return True


def _validate_context_manifest_metadata(value: Any) -> dict[str, Any] | None:
    """Accept only the complete canonical sanitized Manifest JSON shape."""

    if not isinstance(value, dict) or not is_supported_context_manifest(value):
        return None
    try:
        payload = json.loads(stable_manifest_json(value))
    except (TypeError, ValueError):
        return None
    if payload != value:
        return None
    top_keys = {
        "schema_version",
        "producer_version",
        "sanitizer_version",
        "mode",
        "subject",
        "resources",
        "evidence",
        "policy_decisions",
        "layers",
        "requests",
        "bundle_char_budget",
        "omission_reason_counts",
        "reproducibility_hashes",
        "manifest_hash",
    }
    is_work_intelligence_manifest = (
        payload.get("producer_version")
        == CONTEXT_MANIFEST_WORK_INTELLIGENCE_PRODUCER_VERSION
    )
    if is_work_intelligence_manifest:
        top_keys.add("work_intelligence")
    if not _manifest_exact_keys(payload, top_keys):
        return None

    work_intelligence = payload.get("work_intelligence")
    if is_work_intelligence_manifest and not _valid_manifest_work_intelligence(
        work_intelligence
    ):
        return None
    if not is_work_intelligence_manifest and "work_intelligence" in payload:
        return None

    subject = payload.get("subject")
    subject_keys = {
        "actor",
        "turn_ref",
        "scopes",
        "include_project_context",
        "strict_project_scope",
        "automatic_context_suppressed",
        "verified_project_attachment",
    }
    if not _manifest_exact_keys(subject, subject_keys):
        return None
    actor = subject.get("actor")
    turn_ref = subject.get("turn_ref")
    scopes = subject.get("scopes")
    if actor is not None and (
        not _valid_manifest_resource(actor) or actor.get("kind") != "user"
    ):
        return None
    if turn_ref is not None and (
        not _valid_manifest_resource(turn_ref)
        or turn_ref.get("kind") not in {"message", "client_message"}
    ):
        return None
    if (
        not isinstance(scopes, list)
        or len(scopes) > 3
        or any(
            not _valid_manifest_resource(item)
            or item.get("kind") not in {"session", "project", "task"}
            for item in scopes
        )
    ):
        return None
    if subject.get("include_project_context") is not None and type(
        subject.get("include_project_context")
    ) is not bool:
        return None
    for key in (
        "strict_project_scope",
        "automatic_context_suppressed",
        "verified_project_attachment",
    ):
        if type(subject.get(key)) is not bool:
            return None

    resources = payload.get("resources")
    evidence = payload.get("evidence")
    policies = payload.get("policy_decisions")
    layers = payload.get("layers")
    requests = payload.get("requests")
    if (
        not isinstance(resources, list)
        or len(resources) > MAX_MANIFEST_RESOURCES
        or any(not _valid_manifest_resource(item) for item in resources)
        or not isinstance(evidence, list)
        or len(evidence) > MAX_MANIFEST_EVIDENCE
        or any(not _valid_manifest_evidence(item) for item in evidence)
        or not isinstance(policies, list)
        or len(policies) > 64
        or any(not _valid_manifest_policy(item) for item in policies)
        or not isinstance(layers, list)
        or len(layers) > 32
        or any(not _valid_manifest_layer(item) for item in layers)
        or not isinstance(requests, list)
        or len(requests) > MAX_MANIFEST_REQUESTS
        or any(not _valid_manifest_request(item) for item in requests)
    ):
        return None

    bundle_char_budget = payload.get("bundle_char_budget")
    if not _manifest_optional_count(bundle_char_budget):
        return None
    omission_counts = payload.get("omission_reason_counts")
    if not isinstance(omission_counts, dict):
        return None
    for key, count in omission_counts.items():
        if (
            _safe_manifest_token(key) != key
            or _safe_non_negative_int(count) != count
            or count <= 0
        ):
            return None

    reproducibility = payload.get("reproducibility_hashes")
    if not _manifest_exact_keys(reproducibility, {"turn", "context", "requests"}):
        return None
    if any(
        _canonical_manifest_hash(reproducibility.get(key)) is None
        for key in ("turn", "context", "requests")
    ):
        return None
    context_projection = {
        "resources": resources,
        "evidence": evidence,
        "policy_decisions": policies,
        "layers": layers,
        "bundle_char_budget": bundle_char_budget,
        "omission_reason_counts": dict(sorted(omission_counts.items())),
    }
    if is_work_intelligence_manifest:
        context_projection["work_intelligence"] = work_intelligence
    expected_reproducibility = {
        "turn": stable_manifest_hash(subject),
        "context": stable_manifest_hash(context_projection),
        "requests": stable_manifest_hash(requests),
    }
    if reproducibility != expected_reproducibility:
        return None

    manifest_hash = _canonical_manifest_hash(payload.get("manifest_hash"))
    if manifest_hash is None:
        return None
    hash_payload = dict(payload)
    hash_payload.pop("manifest_hash", None)
    if stable_manifest_hash(hash_payload) != manifest_hash:
        return None
    return value


def validate_context_manifest_metadata(value: Any) -> dict[str, Any] | None:
    """Validate a persisted Manifest without ever raising on hostile input."""

    try:
        return _validate_context_manifest_metadata(value)
    except Exception:
        # Metadata is an optional observation.  Treat malformed provider or
        # persisted values as absent so availability never depends on them.
        return None


def serialize_context_manifest(
    manifest: ContextManifest,
) -> dict[str, Any] | None:
    try:
        payload = json.loads(stable_manifest_json(asdict(manifest)))
    except (TypeError, ValueError):
        return None
    # ``ContextManifest.work_intelligence`` is an optional dataclass field for
    # source compatibility.  Omit it from legacy WS1 rows so their exact
    # historical shape and validator contract remain unchanged.
    if payload.get("work_intelligence") is None:
        payload.pop("work_intelligence", None)
    return validate_context_manifest_metadata(payload)


def context_snapshot_without_manifest_capture(
    value: Any,
) -> dict[str, Any]:
    clean = dict(value) if isinstance(value, dict) else {}
    clean.pop(_CONTEXT_MANIFEST_CAPTURE_FIELD, None)
    return clean


def _turn_context_identity_fingerprint(turn_context: Any) -> str | None:
    def ref_hash(kind: str, field: str) -> str | None:
        return observation_ref_hash(
            kind,
            getattr(turn_context, field, None),
        )

    # Private capture reuse requires a stable turn reference. CPython may
    # reuse id() after an immutable TurnContext is released, so process-local
    # object identity cannot safely distinguish otherwise-identical turns.
    message_ref_hash = ref_hash("message", "message_id")
    client_message_ref_hash = ref_hash(
        "client_message",
        "client_message_id",
    )
    if message_ref_hash is None and client_message_ref_hash is None:
        return None

    explicit_reference_hashes: list[str] = []
    for raw in list(
        getattr(turn_context, "explicit_references", ()) or ()
    )[:64]:
        kind = str(getattr(raw, "kind", None) or "").strip().casefold()
        identifier = str(getattr(raw, "id", None) or "").strip()
        if not kind and not identifier:
            continue
        explicit_reference_hashes.append(
            stable_manifest_hash({"kind": kind, "id": identifier})
        )

    include_project_context = getattr(
        turn_context,
        "include_project_context",
        None,
    )
    identity = {
        "user_ref_hash": ref_hash("user", "user_id"),
        "project_ref_hash": ref_hash("project", "project_id"),
        "session_ref_hash": ref_hash("session", "session_id"),
        "task_ref_hash": ref_hash("task", "task_id"),
        "message_ref_hash": message_ref_hash,
        "client_message_ref_hash": client_message_ref_hash,
        "tool_call_ref_hash": ref_hash("tool_call", "tool_call_id"),
        "explicit_reference_hashes": sorted(set(explicit_reference_hashes)),
        "include_project_context": (
            include_project_context
            if type(include_project_context) is bool
            else None
        ),
        "verified_project_attachment": bool(
            getattr(turn_context, "verified_project_attachment", False)
        ),
        "suppress_automatic_context": bool(
            getattr(turn_context, "suppress_automatic_context", False)
        ),
        "strict_project_scope": bool(
            getattr(turn_context, "strict_project_scope", False)
        ),
    }
    if not any(
        identity[key]
        for key in (
            "user_ref_hash",
            "project_ref_hash",
            "session_ref_hash",
            "task_ref_hash",
            "message_ref_hash",
            "client_message_ref_hash",
            "tool_call_ref_hash",
            "explicit_reference_hashes",
        )
    ):
        return None
    return stable_manifest_hash(identity)


def _current_turn_identity_fingerprint() -> str | None:
    try:
        from ..services.turn_context import get_turn_context

        return _turn_context_identity_fingerprint(get_turn_context())
    except Exception:
        return None


def _captured_context_manifest(
    snapshots: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    snapshot_values = list(snapshots)
    current_identity_hash = _current_turn_identity_fingerprint()
    if current_identity_hash is None:
        for snapshot_value in snapshot_values:
            if isinstance(snapshot_value, dict):
                snapshot_value.pop(_CONTEXT_MANIFEST_CAPTURE_FIELD, None)
        return None
    for snapshot_value in reversed(snapshot_values):
        if not isinstance(snapshot_value, dict):
            continue
        capture = snapshot_value.get(_CONTEXT_MANIFEST_CAPTURE_FIELD)
        if not isinstance(capture, dict):
            continue
        if _safe_hash(capture.get("turn_identity_hash")) != current_identity_hash:
            snapshot_value.pop(_CONTEXT_MANIFEST_CAPTURE_FIELD, None)
            continue
        raw = capture.get("manifest")
        payload = validate_context_manifest_metadata(raw)
        if payload is not None:
            return payload
        snapshot_value.pop(_CONTEXT_MANIFEST_CAPTURE_FIELD, None)
    return None


def capture_context_manifest_before_context_clear(
    client: Any,
) -> dict[str, Any] | None:
    """Attach one sanitized Manifest to the current turn's existing snapshot."""

    config = getattr(client, "config", None)
    if not context_manifest_persistence_enabled(config):
        return None
    snapshots = getattr(client, "_last_context_snapshots", None)
    if (
        not isinstance(snapshots, list)
        or not snapshots
        or getattr(client, "_current_context_bundle", None) is None
        or not isinstance(snapshots[-1], dict)
    ):
        return None
    try:
        turn_identity_hash = _current_turn_identity_fingerprint()
        if turn_identity_hash is None:
            return None
        manifest = serialize_context_manifest(build_context_manifest(client))
        if manifest is None:
            return None
        snapshots[-1][_CONTEXT_MANIFEST_CAPTURE_FIELD] = {
            "turn_identity_hash": turn_identity_hash,
            "manifest": manifest,
        }
        return manifest
    except Exception:
        logger.warning(
            "ContextManifest generation failed; continuing legacy turn path"
        )
        return None


def context_manifest_metadata(
    client: Any,
    *,
    allow_snapshot_only: bool = False,
) -> dict[str, Any] | None:
    """Return persistable Manifest metadata without affecting the turn result."""

    config = getattr(client, "config", None)
    if not context_manifest_persistence_enabled(config):
        return None
    snapshots = list(
        getattr(client, "_last_context_snapshots", None) or []
    )
    if not snapshots:
        # In particular this keeps pre-generation user-message persistence from
        # receiving the prior turn's/empty observation.
        return None
    captured = _captured_context_manifest(snapshots)
    if captured is not None:
        return captured
    if (
        not allow_snapshot_only
        and getattr(client, "_current_context_bundle", None) is None
    ):
        return None
    try:
        return serialize_context_manifest(build_context_manifest(client))
    except Exception:
        # Do not persist or log exception text: ContextBuilder debug/errors and
        # provider exceptions may themselves contain source or secret material.
        logger.warning(
            "ContextManifest generation failed; continuing legacy turn path"
        )
        return None


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
