"""Shared conversation, prompt-cache and provider-state primitives.

The web UI stores display messages.  Model requests may contain additional
turn-local context and tool items, so those two representations must not be
collapsed into one string or persisted as if they were user-authored text.
"""

from __future__ import annotations

import hashlib
import json
import asyncio
import concurrent.futures
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)


class PromptMessages(list):
    """List of role messages with a small compatibility affordance for tests."""

    def __contains__(self, item: object) -> bool:
        if isinstance(item, str):
            return any(item in str(message.get("content") or "") for message in self)
        return super().__contains__(item)


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if hasattr(config, "get"):
        try:
            value = config.get(key, None)
        except TypeError:
            value = config.get(key)
        if value is not None:
            return value
    if isinstance(config, Mapping):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, Mapping) or part not in value:
                return default
            value = value[part]
        return value
    return default


def _as_non_negative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, value)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def normalize_usage(usage: Any, *, provider: str = "") -> dict[str, Any]:
    """Normalize heterogeneous OpenAI/local usage payloads.

    Missing fields remain ``None`` so an unavailable metric is not confused
    with a measured zero.
    """

    if usage is None:
        return {}
    input_details = _field(usage, "input_tokens_details") or _field(
        usage, "prompt_tokens_details"
    ) or {}
    output_details = _field(usage, "output_tokens_details") or _field(
        usage, "completion_tokens_details"
    ) or {}
    input_tokens = _as_non_negative_int(
        _field(usage, "input_tokens")
        if _field(usage, "input_tokens") is not None
        else _field(usage, "prompt_tokens")
    )
    output_tokens = _as_non_negative_int(
        _field(usage, "output_tokens")
        if _field(usage, "output_tokens") is not None
        else _field(usage, "completion_tokens")
    )
    cached_tokens = _as_non_negative_int(
        _field(input_details, "cached_tokens")
        if _field(input_details, "cached_tokens") is not None
        else _field(usage, "cached_tokens")
    )
    cache_read_tokens = _as_non_negative_int(
        _field(usage, "cache_read_tokens")
        if _field(usage, "cache_read_tokens") is not None
        else _field(usage, "cache_read_input_tokens")
    )
    cache_write_tokens = _as_non_negative_int(
        _field(usage, "cache_write_tokens")
        if _field(usage, "cache_write_tokens") is not None
        else _field(usage, "cache_creation_input_tokens")
    )
    if cache_read_tokens is None:
        cache_read_tokens = cached_tokens
    reasoning_tokens = _as_non_negative_int(
        _field(output_details, "reasoning_tokens")
        if _field(output_details, "reasoning_tokens") is not None
        else _field(usage, "reasoning_tokens")
    )
    prompt_eval_tokens = _as_non_negative_int(
        _field(usage, "prompt_eval_count")
        if _field(usage, "prompt_eval_count") is not None
        else _field(usage, "prompt_eval_tokens")
    )
    prompt_eval_ms = _field(usage, "prompt_eval_duration")
    if prompt_eval_ms is None:
        prompt_eval_ms = _field(usage, "prompt_eval_ms")
    try:
        if prompt_eval_ms is not None:
            prompt_eval_ms = float(prompt_eval_ms)
            # llama.cpp reports nanoseconds for *_duration fields.
            if prompt_eval_ms > 100_000:
                prompt_eval_ms /= 1_000_000
    except (TypeError, ValueError):
        prompt_eval_ms = None

    result: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cached_tokens": cached_tokens,
        "prompt_eval_tokens": prompt_eval_tokens,
        "prompt_eval_ms": prompt_eval_ms,
        "cache_hit_rate": _field(usage, "cache_hit_rate"),
        "cache_evictions": _as_non_negative_int(_field(usage, "cache_evictions")),
        "cache_provider": _field(usage, "cache_provider") or provider or None,
        "cache_mode": _field(usage, "cache_mode"),
        "cache_key": _field(usage, "cache_key"),
        "cache_supported": _field(usage, "cache_supported"),
        "cache_active": _field(usage, "cache_active"),
        "metrics_source": _field(usage, "metrics_source"),
    }
    for key in ("cache_n", "prompt_n", "prompt_ms", "prompt_eval_count", "prompt_eval_duration"):
        value = _field(usage, key)
        if value is not None:
            result[key] = value
    if result["input_tokens"] is not None or result["output_tokens"] is not None:
        result["total_tokens"] = (result["input_tokens"] or 0) + (result["output_tokens"] or 0)
    return result


def persist_usage_sync(
    client: Any,
    *,
    provider: str,
    model: str,
    usage: Mapping[str, Any] | None,
    request_type: str = "chat",
    latency_ms: int = 0,
    is_streaming: bool = False,
) -> bool:
    """Persist normalized usage from a synchronous provider client."""
    if not usage:
        return False
    try:
        from ..services.token_tracking_service import get_token_tracking_service

        def _uuid_or_none(value: Any):
            try:
                return uuid.UUID(str(value)) if value else None
            except (TypeError, ValueError, AttributeError):
                return None

        async def _record() -> Any:
            cache_read = int(
                usage.get("cache_read_tokens")
                or usage.get("cached_tokens")
                or 0
            )
            return await get_token_tracking_service().record_usage(
                provider=provider,
                model=model,
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                cached_tokens=cache_read,
                cache_read_tokens=cache_read,
                cache_write_tokens=int(usage.get("cache_write_tokens") or 0),
                reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
                prompt_eval_tokens=int(usage.get("prompt_eval_tokens") or 0),
                prompt_eval_ms=int(usage.get("prompt_eval_ms") or 0),
                cache_hit_rate=usage.get("cache_hit_rate"),
                cache_evictions=int(usage.get("cache_evictions") or 0),
                cache_provider=usage.get("cache_provider") or provider,
                cache_mode=usage.get("cache_mode"),
                cache_key=usage.get("cache_key") or getattr(client, "_cache_key", None),
                cache_supported=usage.get("cache_supported"),
                cache_active=usage.get("cache_active"),
                metrics_source=usage.get("metrics_source"),
                session_id=_uuid_or_none(getattr(client, "current_session_id", None)),
                user_id=(
                    client._get_session_user_id()
                    if callable(getattr(client, "_get_session_user_id", None))
                    else None
                ),
                project_id=_uuid_or_none(getattr(client, "current_project_id", None)),
                agent_name=getattr(client, "character_name", None),
                request_type=request_type,
                latency_ms=latency_ms,
                is_streaming=is_streaming,
            )

        runner = getattr(client, "_run_async_sync", None)
        if callable(runner):
            runner(_record())
        else:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(_record())
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(asyncio.run, _record()).result()
        return True
    except Exception:
        logger.debug("usage persistence failed", exc_info=True)
        return False


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def stable_tool_schemas(tools: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return tool schemas with deterministic ordering and JSON keys."""

    copied = [json.loads(stable_json(tool)) for tool in tools if isinstance(tool, Mapping)]

    def name(tool: dict[str, Any]) -> str:
        function = tool.get("function")
        return str((function or {}).get("name") if isinstance(function, Mapping) else tool.get("name") or "")

    return sorted(copied, key=lambda item: (name(item), stable_json(item)))


def stable_cache_key(
    *,
    user_id: str | None,
    session_id: str | None,
    project_id: str | None,
    character: str | None,
    model: str,
    system_prompt: str,
    tool_schemas: Iterable[dict[str, Any]],
    provider: str,
    system_version: str | None = None,
    tool_version: str | None = None,
    branch_fingerprint: str | None = None,
    summary_version: int | None = None,
    server_instance: str | None = None,
) -> str:
    """Build a scoped, stable cache key without including prompt contents."""

    payload = {
        "user": str(user_id or "default_user"),
        "session": str(session_id or "no-session"),
        "project": str(project_id or "no-project"),
        "character": str(character or "default"),
        "model": str(model or ""),
        "provider": str(provider or ""),
        "system_version": system_version or hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:16],
        "tool_version": tool_version or hashlib.sha256(stable_json(stable_tool_schemas(tool_schemas)).encode("utf-8")).hexdigest()[:16],
        "branch": str(branch_fingerprint or "default-branch"),
        "summary_version": int(summary_version or 0),
        "server_instance": str(server_instance or "default-instance"),
    }
    return "aoitalk-" + hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()[:40]


def conversation_state_mode(config: Any, provider: str) -> str:
    raw = (
        _config_get(config, "conversation_state.mode")
        or _config_get(config, "openai.conversation_state_mode")
        or _config_get(config, "openai.responses_state_mode")
        or "stateless"
    )
    normalized = str(raw).strip().lower().replace("_", "-")
    if normalized in {"provider-managed", "provider", "managed"} and provider == "openai":
        return "provider-managed"
    return "stateless"


def build_prompt_messages(
    history: Iterable[Mapping[str, Any]],
    *,
    summary: str = "",
    current_user_input: str,
    dynamic_context: Iterable[tuple[str, str]] = (),
) -> PromptMessages:
    """Build the canonical role transcript plus a turn-local user message."""

    messages = PromptMessages()
    if summary.strip():
        messages.append(
            {
                "role": "system",
                "content": "[AoiTalk summary checkpoint]\n" + summary.strip(),
                "_checkpoint": True,
            }
        )
    for raw in history:
        role = str(raw.get("role") or "")
        if role not in {"system", "user", "assistant", "tool"}:
            continue
        content = raw.get("content")
        item = {"role": role, "content": content if content is not None else ""}
        for key in ("tool_call_id", "tool_calls", "name"):
            if key in raw:
                item[key] = raw[key]
        messages.append(item)

    blocks = [
        f"[{label}]\n{text.strip()}"
        for label, text in dynamic_context
        if str(text or "").strip()
    ]
    current = str(current_user_input or "")
    if blocks:
        current = "\n\n".join(
            [
                *blocks,
                "[Current user input]\nCurrent user request:\n" + current,
            ]
        )
    messages.append({"role": "user", "content": current})
    return messages


def prompt_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(item.get("text") or item.get("content") or "") for item in value if isinstance(item, Mapping))
    return str(value or "")


@dataclass
class ProviderState:
    mode: str = "stateless"
    previous_response_id: str | None = None
    fingerprint: str | None = None

    def reset(self) -> None:
        self.previous_response_id = None
        self.fingerprint = None
