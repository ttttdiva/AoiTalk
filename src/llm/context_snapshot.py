"""Sanitized context-window snapshots for the composer inspector."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

CHARS_PER_TOKEN_ESTIMATE = 4.0
MAX_PREVIEW_CHARS = 120


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
) -> dict[str, Any]:
    token_count = tokens if tokens is not None else estimated_tokens(value)
    return {
        "category": category,
        "label": label,
        "tokens": token_count if measurement != "unavailable" else None,
        "percentage": None,
        "status": status,
        "measurement": measurement,
        "source": source,
        "preview": structural_preview(value) if preview is None else safe_preview(preview),
    }


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
        )
        for item in trace
    ]
    return rendered, result


def without_text(messages: Iterable[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    copied = [dict(item) for item in messages]
    if not text:
        return copied
    for message in copied:
        content = message.get("content")
        if isinstance(content, str) and text in content:
            message["content"] = content.replace(text, "", 1)
            break
    return copied


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
