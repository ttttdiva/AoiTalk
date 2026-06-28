"""Context-window based prompt budgeting for local OpenAI-compatible servers."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_CONTEXT_WINDOW_TOKENS = 8192
DEFAULT_REMOTE_CONTEXT_WINDOW_TOKENS = 32768
DEFAULT_RESPONSE_TOKENS = 1024
DEFAULT_CHARS_PER_TOKEN = 1.15
MIN_CONTEXT_WINDOW_TOKENS = 1024
MAX_CONTEXT_WINDOW_TOKENS = 1048576
PROBE_CACHE_TTL_SECONDS = 60.0
PROBE_TIMEOUT_SECONDS = 0.2

_CONTEXT_FIELD_NAMES = {
    "n_ctx",
    "ctx_size",
    "context_size",
    "context_length",
    "context_window",
    "context_window_tokens",
    "max_context_length",
    "max_model_len",
    "max_position_embeddings",
    "num_ctx",
}
_PROBE_CACHE: dict[tuple[str, str, str], tuple[float, int | None, str]] = {}


@dataclass(frozen=True)
class ContextBudget:
    context_window_tokens: int
    source: str
    response_tokens: int
    input_tokens: int
    message_budget_chars: int
    context_bundle_chars: int
    tool_hint_context_chars: int
    tool_result_chars: int
    history_messages: int
    history_message_chars: int
    suppress_parent_tools_after_tool_hints: bool


def resolve_context_budget(
    *,
    config: Any,
    provider_key: str,
    base_url: str,
    model_name: str | None,
    api_key: str | None = None,
    requested_max_tokens: int | None = None,
    override_context_window_tokens: int | None = None,
) -> ContextBudget:
    """Resolve the active context window and convert it to character budgets."""
    if override_context_window_tokens:
        return build_context_budget(
            context_window_tokens=_bounded_context_tokens(
                override_context_window_tokens
            ),
            source="adaptive-overflow",
            config=config,
            provider_key=provider_key,
            requested_max_tokens=requested_max_tokens,
        )

    explicit = _configured_context_window_tokens(config, provider_key)
    if explicit:
        return build_context_budget(
            context_window_tokens=explicit,
            source="config",
            config=config,
            provider_key=provider_key,
            requested_max_tokens=requested_max_tokens,
        )

    if _probe_enabled(config, provider_key):
        probed_tokens, probed_source = _probe_context_window_tokens(
            base_url=base_url,
            model_name=model_name,
            api_key=api_key,
            provider_key=provider_key,
        )
        if probed_tokens:
            return build_context_budget(
                context_window_tokens=probed_tokens,
                source=probed_source,
                config=config,
                provider_key=provider_key,
                requested_max_tokens=requested_max_tokens,
            )

    return build_context_budget(
        context_window_tokens=_fallback_context_window_tokens(provider_key),
        source="fallback",
        config=config,
        provider_key=provider_key,
        requested_max_tokens=requested_max_tokens,
    )


def build_context_budget(
    *,
    context_window_tokens: int,
    source: str,
    config: Any = None,
    provider_key: str = "openai_compatible_local",
    requested_max_tokens: int | None = None,
) -> ContextBudget:
    window = _bounded_context_tokens(context_window_tokens)
    response_tokens = _response_tokens(
        config=config,
        provider_key=provider_key,
        requested_max_tokens=requested_max_tokens,
        context_window_tokens=window,
    )
    safety_tokens = max(384, int(window * 0.06))
    input_tokens = max(1024, window - response_tokens - safety_tokens)
    chars_per_token = _chars_per_token(
        config,
        provider_key,
        context_window_tokens=window,
    )
    input_chars = max(2048, int(input_tokens * chars_per_token))

    context_bundle_chars = _clamp_int(int(input_chars * 0.36), 2500, 48000)
    tool_hint_chars = _clamp_int(int(input_chars * 0.42), 3000, 64000)
    tool_chars = _clamp_int(int(input_chars * 0.42), 3000, 64000)
    history_messages = _clamp_int(window // 2048, 2, 24)
    history_message_chars = _clamp_int(int(input_chars * 0.13), 1000, 8000)
    suppress_parent_tools = window <= 16384 or input_chars <= 32000

    return ContextBudget(
        context_window_tokens=window,
        source=source,
        response_tokens=response_tokens,
        input_tokens=input_tokens,
        message_budget_chars=input_chars,
        context_bundle_chars=context_bundle_chars,
        tool_hint_context_chars=tool_hint_chars,
        tool_result_chars=tool_chars,
        history_messages=history_messages,
        history_message_chars=history_message_chars,
        suppress_parent_tools_after_tool_hints=suppress_parent_tools,
    )


def clip_text(text: str, max_chars: int | None) -> str:
    if not max_chars or max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text
    suffix = "\n... (truncated to fit the local model context budget)"
    keep = max(0, max_chars - len(suffix))
    return text[:keep].rstrip() + suffix


def clip_text_preserve_tail(text: str, max_chars: int | None) -> str:
    if not max_chars or max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = "\n... (middle truncated to fit the local model context budget) ...\n"
    keep = max(0, max_chars - len(marker))
    head = max(0, int(keep * 0.6))
    tail = max(0, keep - head)
    return text[:head].rstrip() + marker + text[-tail:].lstrip()


def reduced_context_window_after_overflow(current_tokens: int) -> int:
    return max(MIN_CONTEXT_WINDOW_TOKENS, int(current_tokens * 0.75))


def extract_context_window_tokens(
    payload: Any,
    model_name: str | None = None,
) -> int | None:
    """Extract a context window from common OpenAI-compatible metadata shapes."""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            model_payload = _matching_model_payload(data, model_name)
            if model_payload is not None:
                found = _find_context_field(model_payload)
                if found:
                    return found
        return _find_context_field(payload)
    if isinstance(payload, list):
        model_payload = _matching_model_payload(payload, model_name)
        if model_payload is not None:
            found = _find_context_field(model_payload)
            if found:
                return found
    return None


def _configured_context_window_tokens(config: Any, provider_key: str) -> int | None:
    env_keys = [
        f"{provider_key.upper()}_CONTEXT_WINDOW_TOKENS",
        f"{provider_key.upper()}_CONTEXT_WINDOW",
        f"{provider_key.upper()}_CTX_SIZE",
    ]
    if provider_key == "openai_compatible_local":
        env_keys.extend(
            [
                "OPENAI_COMPATIBLE_LOCAL_CONTEXT_WINDOW_TOKENS",
                "OPENAI_COMPATIBLE_LOCAL_CONTEXT_WINDOW",
                "OPENAI_COMPATIBLE_LOCAL_CTX_SIZE",
            ]
        )
    for key in dict.fromkeys(env_keys):
        value = _parse_int(os.getenv(key))
        if value:
            return _bounded_context_tokens(value)

    config_keys = [
        f"{provider_key}.context_window_tokens",
        f"{provider_key}.context_window",
        f"{provider_key}.ctx_size",
        f"{provider_key}.context_size",
        f"{provider_key}.max_context_tokens",
        f"{provider_key}.context_budget.tokens",
        f"{provider_key}.context_budget.context_window_tokens",
        f"{provider_key}.context_budget.context_window",
    ]
    if provider_key == "sglang":
        config_keys.append("sglang.max_model_len")
    if provider_key == "openai_compatible_local":
        config_keys.extend(
            [
                "openai_compatible_local.qwopus.ctx_size",
                "openai_compatible_local.qwopus.context_window",
                "openai_compatible_local.qwopus.context_window_tokens",
            ]
        )
    for key in config_keys:
        value = _parse_int(_config_get(config, key))
        if value:
            return _bounded_context_tokens(value)
    return None


def _probe_context_window_tokens(
    *,
    base_url: str,
    model_name: str | None,
    api_key: str | None,
    provider_key: str,
) -> tuple[int | None, str]:
    normalized_base = str(base_url or "").rstrip("/")
    cache_key = (provider_key, normalized_base, str(model_name or ""))
    cached = _PROBE_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] < PROBE_CACHE_TTL_SECONDS:
        return cached[1], cached[2]

    for endpoint_name, url in _probe_urls(normalized_base):
        try:
            payload = _read_json(url, api_key=api_key)
        except Exception as exc:
            logger.debug("[ContextBudget] Probe %s failed: %s", url, exc)
            continue
        tokens = extract_context_window_tokens(payload, model_name=model_name)
        if tokens:
            bounded = _bounded_context_tokens(tokens)
            result = (bounded, f"server:{endpoint_name}")
            _PROBE_CACHE[cache_key] = (now, result[0], result[1])
            return result

    _PROBE_CACHE[cache_key] = (now, None, "server:unavailable")
    return None, "server:unavailable"


def _probe_urls(base_url: str) -> list[tuple[str, str]]:
    if not base_url:
        return []
    root = base_url[:-3] if base_url.endswith("/v1") else base_url
    return [
        ("props", f"{root}/props"),
        (
            "models",
            f"{base_url}/models"
            if base_url.endswith("/v1")
            else f"{base_url}/v1/models",
        ),
    ]


def _read_json(url: str, *, api_key: str | None) -> Any:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _probe_enabled(config: Any, provider_key: str) -> bool:
    if provider_key not in {"openai_compatible_local", "sglang"}:
        return False
    value = _config_get(config, f"{provider_key}.context_budget.probe_server", True)
    return _config_bool(value, True)


def _response_tokens(
    *,
    config: Any,
    provider_key: str,
    requested_max_tokens: int | None,
    context_window_tokens: int,
) -> int:
    configured = _parse_int(
        _config_get(config, f"{provider_key}.context_budget.response_reserve_tokens")
    )
    requested = _parse_int(requested_max_tokens)
    raw = requested or configured or DEFAULT_RESPONSE_TOKENS
    upper = max(256, min(4096, int(context_window_tokens * 0.35)))
    return _clamp_int(raw, 256, upper)


def _chars_per_token(
    config: Any,
    provider_key: str,
    *,
    context_window_tokens: int,
) -> float:
    value = _config_get(config, f"{provider_key}.context_budget.chars_per_token")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_CHARS_PER_TOKEN
    bounded = min(4.0, max(1.0, parsed))
    if provider_key in {"openai_compatible_local", "sglang"}:
        if context_window_tokens <= 16384:
            return min(bounded, DEFAULT_CHARS_PER_TOKEN)
        if context_window_tokens <= 32768:
            return min(bounded, 1.4)
    return bounded


def _fallback_context_window_tokens(provider_key: str) -> int:
    if provider_key in {"openai_compatible_local", "sglang"}:
        return DEFAULT_LOCAL_CONTEXT_WINDOW_TOKENS
    return DEFAULT_REMOTE_CONTEXT_WINDOW_TOKENS


def _matching_model_payload(items: list[Any], model_name: str | None) -> Any | None:
    if not model_name:
        return items[0] if items else None
    normalized = str(model_name).casefold()
    for item in items:
        if not isinstance(item, dict):
            continue
        candidate = str(item.get("id") or item.get("name") or item.get("model") or "")
        if candidate.casefold() == normalized:
            return item
    return items[0] if items else None


def _find_context_field(value: Any) -> int | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in _CONTEXT_FIELD_NAMES:
                parsed = _parse_int(item)
                if parsed:
                    return _bounded_context_tokens(parsed)
        for item in value.values():
            found = _find_context_field(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_context_field(item)
            if found:
                return found
    return None


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    if hasattr(config, "get"):
        return config.get(key, default)
    return default


def _config_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() not in {"0", "false", "no", "off"}


def _parse_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _bounded_context_tokens(value: int) -> int:
    return _clamp_int(value, MIN_CONTEXT_WINDOW_TOKENS, MAX_CONTEXT_WINDOW_TOKENS)


def _clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(int(value), maximum))
