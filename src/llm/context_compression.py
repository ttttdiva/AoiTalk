"""Lightweight model-context shaping for tool results.

This module intentionally keeps compression deterministic and local. It shapes
the payload sent back to the model while callers keep the original tool result
for UI, logging, retries, and grounding guards.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


DEFAULT_MIN_CHARS = 3000
DEFAULT_MAX_CHARS = 12000
DEFAULT_ERROR_PROTECT_CHARS = 8000

SEARCH_TOOLS = {
    "web_search",
    "grok_x_search",
    "knowledge_search",
    "search_memory",
}
FILE_PREVIEW_TOOLS = {
    "read_workspace_file",
    "view_file",
}
FILE_LISTING_TOOLS = {
    "find_workspace_items",
    "inspect_workspace_tree",
    "search_files",
    "list_directory",
}
PROTECTED_LATEST_TOOLS = {
    "get_project_progress",
}

ERROR_MARKERS = (
    "error:",
    "tool not found:",
    "tool execution error:",
    "traceback",
    "exception",
    "fatal",
)
IMPORTANT_ROW_MARKERS = (
    "error",
    "failed",
    "fatal",
    "warning",
    "deadline",
    "due",
    "priority",
    "status",
    "url",
    "path",
    "date",
)

DATA_URL_RE = re.compile(
    r"(?P<prefix>['\"]data_url['\"]\s*:\s*)(?P<quote>['\"])data:[^'\"]+(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
DIRECT_DATA_URL_RE = re.compile(
    r"data:[a-z0-9.+-]+/[a-z0-9.+-]+;base64,[a-z0-9+/=\r\n]+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ContextCompressionPolicy:
    enabled: bool = False
    min_chars: int = DEFAULT_MIN_CHARS
    max_chars: int = DEFAULT_MAX_CHARS
    preserve_recent_tool_results: int = 2
    ccr_enabled: bool = False
    ccr_ttl_seconds: int = 1800
    strip_data_urls: bool = True
    protect_error_outputs_under_chars: int = DEFAULT_ERROR_PROTECT_CHARS
    protect_latest_project_progress: bool = True
    strategies: Mapping[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class CompressionResult:
    text: str
    original_chars: int
    compressed_chars: int
    strategy: str
    ccr_id: str | None = None
    reversible: bool = False


def context_compression_policy(
    config: Any,
    *,
    max_chars: int | None = None,
) -> ContextCompressionPolicy:
    configured_max = _parse_int(
        _config_get(config, "context_compression.tool_result_max_chars", None)
    )
    if configured_max is None:
        configured_max = _parse_int(
            _config_get(config, "context_compression.max_chars", None)
        )
    effective_max = configured_max or max_chars or DEFAULT_MAX_CHARS
    strategies = {
        "json": _config_bool(config, "context_compression.strategies.json", True),
        "log": _config_bool(config, "context_compression.strategies.log", True),
        "search": _config_bool(config, "context_compression.strategies.search", True),
        "file_preview": _config_bool(
            config,
            "context_compression.strategies.file_preview",
            True,
        ),
        "file_listing": _config_bool(
            config,
            "context_compression.strategies.file_listing",
            True,
        ),
        "text_head_tail": _config_bool(
            config,
            "context_compression.strategies.text_head_tail",
            True,
        ),
    }
    return ContextCompressionPolicy(
        enabled=_config_bool(config, "context_compression.enabled", False),
        min_chars=max(
            0,
            _parse_int(_config_get(config, "context_compression.min_chars", None))
            or DEFAULT_MIN_CHARS,
        ),
        max_chars=max(1, int(effective_max)),
        preserve_recent_tool_results=max(
            0,
            _parse_int(
                _config_get(
                    config,
                    "context_compression.protect.recent_tool_results",
                    None,
                )
            )
            or 2,
        ),
        ccr_enabled=_config_bool(config, "context_compression.ccr_enabled", False),
        ccr_ttl_seconds=max(
            1,
            _parse_int(_config_get(config, "context_compression.ccr_ttl_seconds", None))
            or 1800,
        ),
        strip_data_urls=_config_bool(config, "context_compression.strip_data_urls", True),
        protect_error_outputs_under_chars=max(
            0,
            _parse_int(
                _config_get(
                    config,
                    "context_compression.protect.error_outputs_under_chars",
                    None,
                )
            )
            or DEFAULT_ERROR_PROTECT_CHARS,
        ),
        protect_latest_project_progress=_config_bool(
            config,
            "context_compression.protect.latest_project_progress",
            True,
        ),
        strategies=strategies,
    )


def model_tool_result_payload(
    *,
    tool_name: str,
    output: str,
    user_input: str = "",
    max_chars: int | None = None,
    config: Any = None,
    legacy_clip: Callable[[str, int | None], str] | None = None,
) -> CompressionResult:
    """Build the tool-result payload sent to the model.

    The default-off path delegates to the caller's legacy clipper, so existing
    runtime behavior stays stable until the feature is explicitly enabled.
    """

    policy = context_compression_policy(config, max_chars=max_chars)
    clipper = legacy_clip or _legacy_clip
    raw = str(output or "")
    if not policy.enabled:
        text = clipper(raw, max_chars)
        return CompressionResult(
            text=text,
            original_chars=len(raw),
            compressed_chars=len(text),
            strategy="disabled",
        )
    return compress_tool_result_for_model(
        tool_name=tool_name,
        output=raw,
        user_input=user_input,
        max_chars=max_chars,
        config=config,
        policy=policy,
        legacy_clip=clipper,
    )


def compress_tool_result_for_model(
    *,
    tool_name: str,
    output: str,
    user_input: str,
    max_chars: int | None,
    config: Any = None,
    policy: ContextCompressionPolicy | None = None,
    legacy_clip: Callable[[str, int | None], str] | None = None,
) -> CompressionResult:
    policy = policy or context_compression_policy(config, max_chars=max_chars)
    clipper = legacy_clip or _legacy_clip
    target_chars = max(1, max_chars or policy.max_chars)
    raw = str(output or "")
    source = raw
    stripped_data_url = False

    if policy.strip_data_urls:
        source, stripped_data_url = _strip_data_urls(source)

    fallback = clipper(source, target_chars)
    protected = _protected_reason(tool_name, source, policy)
    if protected:
        return CompressionResult(
            text=fallback,
            original_chars=len(raw),
            compressed_chars=len(fallback),
            strategy=protected,
        )

    if len(source) <= policy.min_chars and len(source) <= target_chars:
        strategy = "data_url_stripped" if stripped_data_url else "below_threshold"
        return CompressionResult(
            text=fallback,
            original_chars=len(raw),
            compressed_chars=len(fallback),
            strategy=strategy,
        )

    strategy = "text_head_tail"
    candidate: str
    if tool_name in FILE_PREVIEW_TOOLS and _strategy_enabled(policy, "file_preview"):
        candidate = _compress_file_preview(source, target_chars)
        strategy = "file_preview"
    elif tool_name in FILE_LISTING_TOOLS and _strategy_enabled(policy, "file_listing"):
        candidate = _compress_json_like(source, user_input, target_chars)
        strategy = "file_listing"
    elif tool_name in SEARCH_TOOLS and _strategy_enabled(policy, "search"):
        candidate = _compress_search_output(source, user_input, target_chars)
        strategy = "search"
    elif _strategy_enabled(policy, "json") and _looks_like_structured(source):
        candidate = _compress_json_like(source, user_input, target_chars)
        strategy = "json"
    elif _strategy_enabled(policy, "log") and _looks_like_log(source):
        candidate = _compress_log_output(source, target_chars)
        strategy = "log"
    else:
        candidate = _compress_head_tail_text(source, target_chars, label="text")

    if stripped_data_url:
        strategy = f"{strategy}+data_url_stripped"
    return _accept_result(
        candidate=candidate,
        fallback=fallback,
        original=raw,
        source=source,
        strategy=strategy,
    )


def _protected_reason(
    tool_name: str,
    source: str,
    policy: ContextCompressionPolicy,
) -> str:
    if (
        policy.protect_latest_project_progress
        and tool_name in PROTECTED_LATEST_TOOLS
    ):
        return "protected_project_progress"
    lowered = source.lstrip().casefold()
    if (
        len(source) <= policy.protect_error_outputs_under_chars
        and any(marker in lowered[:800] for marker in ERROR_MARKERS)
    ):
        return "protected_error"
    return ""


def _compress_file_preview(text: str, max_chars: int) -> str:
    value = _parse_structured(text)
    if not isinstance(value, dict):
        return _compress_head_tail_text(text, max_chars, label="file_preview")

    metadata_keys = (
        "path",
        "type",
        "extension",
        "mime_type",
        "size_bytes",
        "truncated",
        "success",
    )
    lines = ["[AoiTalk context compressed: file_preview]"]
    for key in metadata_keys:
        if key in value:
            lines.append(f"{key}: {value.get(key)}")
    content = value.get("content")
    if isinstance(content, str) and content:
        header = "\n".join(lines) + "\n\ncontent:\n"
        content_budget = max(200, max_chars - len(header))
        return header + _compress_head_tail_text(
            content,
            content_budget,
            label="file_content",
            include_header=False,
        )

    compact = {
        key: value[key]
        for key in metadata_keys
        if key in value
    }
    if "message" in value:
        compact["message"] = value["message"]
    return _limit_text(
        "\n".join(lines) + "\n\n" + _dump_json(compact),
        max_chars,
    )


def _compress_json_like(text: str, user_input: str, max_chars: int) -> str:
    value = _parse_structured(text)
    if value is None:
        return _compress_head_tail_text(text, max_chars, label="structured_text")
    compacted = _compact_structured_value(value, user_input)
    dumped = _dump_json(compacted)
    if len(dumped) <= max_chars:
        return dumped
    return _compress_head_tail_text(dumped, max_chars, label="json")


def _compress_search_output(text: str, user_input: str, max_chars: int) -> str:
    structured = _parse_structured(text)
    if structured is not None:
        return _compress_json_like(text, user_input, max_chars)

    lines = text.splitlines()
    query_terms = _query_terms(user_input)
    selected: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        lowered = line.casefold()
        if "http://" in lowered or "https://" in lowered:
            selected.append((index, line))
        elif query_terms and any(term in lowered for term in query_terms):
            selected.append((index, line))
        elif any(
            marker in lowered for marker in ("published", "date", "title", "source")
        ):
            selected.append((index, line))
    return _selected_lines_summary(
        label="search",
        lines=lines,
        selected=selected,
        max_chars=max_chars,
        first=8,
        last=8,
    )


def _compress_log_output(text: str, max_chars: int) -> str:
    lines = text.splitlines()
    selected = [
        (index, line)
        for index, line in enumerate(lines)
        if any(marker in line.casefold() for marker in ERROR_MARKERS)
    ]
    return _selected_lines_summary(
        label="log",
        lines=lines,
        selected=selected,
        max_chars=max_chars,
        first=10,
        last=20,
    )


def _selected_lines_summary(
    *,
    label: str,
    lines: list[str],
    selected: list[tuple[int, str]],
    max_chars: int,
    first: int,
    last: int,
) -> str:
    if len("\n".join(lines)) <= max_chars:
        return "\n".join(lines)
    keep: list[tuple[int, str]] = []
    keep.extend((index, line) for index, line in enumerate(lines[:first]))
    keep.extend(selected)
    tail_start = max(0, len(lines) - last)
    keep.extend(
        (tail_start + offset, line)
        for offset, line in enumerate(lines[tail_start:])
    )
    deduped = _dedupe_indexed_lines(keep)
    omitted = max(0, len(lines) - len(deduped))
    body = "\n".join(f"{index + 1}: {line}" for index, line in deduped)
    header = (
        f"[AoiTalk context compressed: {label}]\n"
        f"original_lines: {len(lines)}\n"
        f"omitted_lines: {omitted}\n\n"
    )
    return _limit_text(header + body, max_chars)


def _compress_head_tail_text(
    text: str,
    max_chars: int,
    *,
    label: str,
    include_header: bool = True,
) -> str:
    if len(text) <= max_chars:
        return text
    header = ""
    if include_header:
        header = (
            f"[AoiTalk context compressed: {label}]\n"
            f"original_chars: {len(text)}\n\n"
        )
    marker = "\n\n... [middle omitted for model context] ...\n\n"
    keep = max(0, max_chars - len(header) - len(marker))
    if keep <= 0:
        return text[:max_chars].rstrip()
    head = max(1, int(keep * 0.55))
    tail = max(1, keep - head)
    return header + text[:head].rstrip() + marker + text[-tail:].lstrip()


def _compact_structured_value(value: Any, user_input: str) -> Any:
    if isinstance(value, list):
        return _compact_list(value, user_input)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, list):
                compacted = _compact_list(item, user_input)
                result[key] = (
                    compacted.get("items", compacted)
                    if isinstance(compacted, dict)
                    else compacted
                )
                if isinstance(compacted, dict) and "_aoitalk_dropped" in compacted:
                    result[f"{key}_omitted"] = compacted["_aoitalk_dropped"]
            elif isinstance(item, dict):
                result[key] = _compact_structured_value(item, user_input)
            else:
                result[key] = item
        return result
    return value


def _compact_list(items: list[Any], user_input: str, max_items: int = 20) -> dict[str, Any]:
    if len(items) <= max_items:
        return {"items": items}

    query_terms = _query_terms(user_input)
    selected: list[tuple[int, Any]] = []
    selected.extend((index, item) for index, item in enumerate(items[:3]))
    tail_start = max(0, len(items) - 3)
    selected.extend(
        (tail_start + offset, item)
        for offset, item in enumerate(items[tail_start:])
    )
    for index, item in enumerate(items):
        item_text = _item_search_text(item)
        lowered = item_text.casefold()
        if query_terms and any(term in lowered for term in query_terms):
            selected.append((index, item))
        elif any(marker in lowered for marker in ("error", "failed", "fatal", "warning")):
            selected.append((index, item))
    for index, item in enumerate(items):
        item_text = _item_search_text(item)
        lowered = item_text.casefold()
        if any(marker in lowered for marker in IMPORTANT_ROW_MARKERS):
            selected.append((index, item))

    keep = sorted(_dedupe_indexed_items_preserve_order(selected)[:max_items])
    dropped = max(0, len(items) - len(keep))
    return {
        "items": [item for _, item in keep],
        "_aoitalk_dropped": {
            "count": dropped,
            "hint": "Rows omitted for model context.",
        },
    }


def _strip_data_urls(text: str) -> tuple[str, bool]:
    structured = _parse_structured(text)
    if structured is not None:
        stripped, changed = _remove_data_urls(structured)
        if changed:
            return _dump_json(stripped), True

    changed = False

    def replace_keyed(match: re.Match[str]) -> str:
        nonlocal changed
        changed = True
        return (
            f"{match.group('prefix')}{match.group('quote')}"
            "<omitted for model context>"
            f"{match.group('quote')}"
        )

    value = DATA_URL_RE.sub(replace_keyed, text)
    if DIRECT_DATA_URL_RE.search(value):
        changed = True
        value = DIRECT_DATA_URL_RE.sub("<data_url omitted for model context>", value)
    return value, changed


def _remove_data_urls(value: Any) -> tuple[Any, bool]:
    if isinstance(value, dict):
        changed = False
        result: dict[Any, Any] = {}
        for key, item in value.items():
            if str(key) == "data_url":
                result[key] = "<omitted for model context>"
                changed = True
            else:
                result[key], item_changed = _remove_data_urls(item)
                changed = changed or item_changed
        return result, changed
    if isinstance(value, list):
        changed = False
        result = []
        for item in value:
            stripped, item_changed = _remove_data_urls(item)
            result.append(stripped)
            changed = changed or item_changed
        return result, changed
    if (
        isinstance(value, str)
        and value.startswith("data:")
        and ";base64," in value[:120]
    ):
        return "<omitted for model context>", True
    return value, False


def _accept_result(
    *,
    candidate: str,
    fallback: str,
    original: str,
    source: str,
    strategy: str,
) -> CompressionResult:
    text = candidate.strip()
    if not text:
        text = fallback
        strategy = "fallback_empty"
    elif len(text) >= len(source):
        text = fallback
        strategy = "fallback_inflation"
    return CompressionResult(
        text=text,
        original_chars=len(original),
        compressed_chars=len(text),
        strategy=strategy,
    )


def _limit_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return _compress_head_tail_text(
        text,
        max_chars,
        label="text",
        include_header=False,
    )


def _parse_structured(text: str) -> Any:
    value = text.strip()
    if not value:
        return None
    if value.startswith("```"):
        value = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    try:
        return json.loads(value)
    except Exception:
        pass
    try:
        return ast.literal_eval(value)
    except Exception:
        return None


def _looks_like_structured(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def _looks_like_log(text: str) -> bool:
    lowered = text.casefold()
    if "\n" not in text:
        return False
    return (
        any(marker in lowered for marker in ERROR_MARKERS)
        or len(text.splitlines()) > 40
    )


def _query_terms(user_input: str) -> set[str]:
    terms = {
        token.casefold()
        for token in re.findall(r"[\w./:-]{3,}", str(user_input or ""))
    }
    return {term for term in terms if not term.isdigit()}


def _item_search_text(item: Any) -> str:
    if isinstance(item, (dict, list)):
        return _dump_json(item)
    return str(item)


def _dedupe_indexed_lines(items: list[tuple[int, str]]) -> list[tuple[int, str]]:
    seen: set[int] = set()
    result: list[tuple[int, str]] = []
    for index, line in sorted(items, key=lambda entry: entry[0]):
        if index in seen:
            continue
        seen.add(index)
        result.append((index, line))
    return result


def _dedupe_indexed_items_preserve_order(
    items: list[tuple[int, Any]]
) -> list[tuple[int, Any]]:
    seen: set[int] = set()
    result: list[tuple[int, Any]] = []
    for index, item in items:
        if index in seen:
            continue
        seen.add(index)
        result.append((index, item))
    return result


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _strategy_enabled(policy: ContextCompressionPolicy, strategy: str) -> bool:
    return bool(policy.strategies.get(strategy, True))


def _legacy_clip(text: str, max_chars: int | None) -> str:
    if not max_chars or max_chars <= 0 or len(text) <= max_chars:
        return text
    suffix = "\n... (truncated to fit the model context budget)"
    keep = max(0, max_chars - len(suffix))
    return text[:keep].rstrip() + suffix


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    getter = getattr(config, "get", None)
    if callable(getter):
        return getter(key, default)
    return default


def _config_bool(config: Any, key: str, default: bool) -> bool:
    value = _config_get(config, key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value) if value is not None else default


def _parse_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
