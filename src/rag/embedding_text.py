"""Text-only handling for model-reserved multimodal placeholders.

User/document text can contain the visible spellings of processor tokens. The
text lane must represent those spellings as ordinary text, never as a request
to synthesize missing media. Diagnostics from this module are counts only and
must not include input text or media bytes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


MEDIA_PLACEHOLDERS = {
    "image": "<|image|>",
    "audio": "<|audio|>",
    "video": "<|video|>",
}

_MODES = frozenset({"document", "SearchQuery", "CodeRetrieval", "media", "query"})
_INPUT_TYPES = ("text", "query_object", "media_object", "other")


def escape_text_only_media_placeholders(value: str) -> str:
    """Break model-reserved token spellings while preserving their meaning."""

    if not isinstance(value, str):
        raise TypeError("embedding text must be a string")
    for marker in MEDIA_PLACEHOLDERS.values():
        value = value.replace(marker, marker.replace("<|", "< |", 1))
    return value


def sanitize_query_embedding_input(value: Any) -> Any:
    """Escape only textual fields; decoded media values remain untouched."""

    if isinstance(value, str):
        return escape_text_only_media_placeholders(value)
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        result = dict(value)
        result["text"] = escape_text_only_media_placeholders(result["text"])
        return result
    return value


def _bounded_count(value: Any) -> int:
    if type(value) is not int:
        return 0
    return min(max(value, 0), 1_000_000)


def safe_embedding_request_diagnostics(value: Any) -> dict[str, Any]:
    """Whitelist low-risk shape/count metadata from an IPC response."""

    raw = value if isinstance(value, Mapping) else {}
    mode = raw.get("mode")
    raw_types = raw.get("input_types")
    raw_types = raw_types if isinstance(raw_types, Mapping) else {}
    raw_special = raw.get("special_token_counts")
    raw_special = raw_special if isinstance(raw_special, Mapping) else {}
    return {
        "mode": mode if isinstance(mode, str) and mode in _MODES else "other",
        "input_types": {key: _bounded_count(raw_types.get(key)) for key in _INPUT_TYPES},
        "image_count": _bounded_count(raw.get("image_count")),
        "special_token_counts": {
            key: _bounded_count(raw_special.get(key)) for key in MEDIA_PLACEHOLDERS
        },
    }


def embedding_request_diagnostics(request: Any) -> dict[str, Any]:
    """Project request shape without retaining its text, paths, or media."""

    raw = request if isinstance(request, Mapping) else {}
    inputs = raw.get("inputs")
    inputs = inputs if isinstance(inputs, list) else []
    input_types = {key: 0 for key in _INPUT_TYPES}
    image_count = 0
    text_values: list[str] = []

    for item in inputs[:32]:
        if isinstance(item, str):
            input_types["text"] += 1
            text_values.append(item)
            continue
        if not isinstance(item, dict):
            input_types["other"] += 1
            continue
        if isinstance(item.get("text"), str):
            input_types["query_object"] += 1
            text_values.append(item["text"])
        elif any(key in item for key in MEDIA_PLACEHOLDERS):
            input_types["media_object"] += 1
        else:
            input_types["other"] += 1
        if "image" in item and item["image"] is not None:
            media = item["image"]
            image_count += min(len(media), 16) if isinstance(media, (list, tuple)) else 1

    special_token_counts = {
        name: sum(text.count(marker) for text in text_values)
        for name, marker in MEDIA_PLACEHOLDERS.items()
    }
    return safe_embedding_request_diagnostics(
        {
            "mode": raw.get("mode"),
            "input_types": input_types,
            "image_count": image_count,
            "special_token_counts": special_token_counts,
        }
    )
