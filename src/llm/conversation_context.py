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


DEFAULT_HISTORY_TOOL_RESULT_MAX_CHARS = 2400
DEFAULT_HISTORY_TOOL_RESULT_TOTAL_CHARS = 24000
DEFAULT_HISTORY_PRESERVE_RECENT_TOOL_RESULTS = 2


def _history_config_value(config: Any, key: str, default: Any) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        current: Any = config
        for part in key.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return default
            current = current[part]
        return current
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
        except TypeError:
            value = getter(key)
        if value is not None:
            return value
    return default


def _history_config_int(
    config: Any,
    key: str,
    default: int,
    *,
    minimum: int = 0,
) -> int:
    value = _history_config_value(config, key, default)
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def _history_config_bool(config: Any, key: str, default: bool) -> bool:
    value = _history_config_value(config, key, default)
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def compact_model_transcript_for_history(
    model_messages: list[dict[str, Any]],
    config: Any = None,
) -> list[dict[str, Any]]:
    """Apply configured history compaction to a model-input copy."""

    if not _history_config_bool(
        config,
        "context_compression.history_compaction_enabled",
        True,
    ):
        return [dict(message) for message in model_messages]
    return compact_model_transcript_tool_results(
        model_messages,
        preserve_recent_tool_results=_history_config_int(
            config,
            "context_compression.protect.recent_tool_results",
            DEFAULT_HISTORY_PRESERVE_RECENT_TOOL_RESULTS,
        ),
        max_total_chars=_history_config_int(
            config,
            "context_compression.history_tool_result_total_chars",
            DEFAULT_HISTORY_TOOL_RESULT_TOTAL_CHARS,
            minimum=1,
        ),
        max_result_chars=_history_config_int(
            config,
            "context_compression.history_tool_result_max_chars",
            DEFAULT_HISTORY_TOOL_RESULT_MAX_CHARS,
            minimum=1,
        ),
    )


def compact_model_transcript_tool_results(
    model_messages: list[dict[str, Any]],
    *,
    preserve_recent_tool_results: int = DEFAULT_HISTORY_PRESERVE_RECENT_TOOL_RESULTS,
    max_total_chars: int = DEFAULT_HISTORY_TOOL_RESULT_TOTAL_CHARS,
    max_result_chars: int = DEFAULT_HISTORY_TOOL_RESULT_MAX_CHARS,
) -> list[dict[str, Any]]:
    """Compact old tool payloads while keeping the authoritative transcript intact.

    The persisted model transcript is a cumulative provider checkpoint and must
    remain lossless for deduplication, audit, and UI replay.  Only the copy
    installed into the active provider history is shaped here.  Recent tool
    results stay verbatim because they are the most likely evidence for the
    next continuation; older results are compressed with the same deterministic
    local compressor used for new tool payloads.
    """

    copied = [dict(message) for message in model_messages]
    tool_indexes = [
        index
        for index, message in enumerate(copied)
        if str(message.get("role") or "") == "tool"
        and isinstance(message.get("content"), str)
    ]
    if not tool_indexes:
        return copied

    older_indexes = tool_indexes[: max(0, len(tool_indexes) - max(0, preserve_recent_tool_results))]
    older_total = sum(len(str(copied[index].get("content") or "")) for index in older_indexes)
    if older_total <= max(0, max_total_chars):
        return copied

    # Runtime tool messages carry only `tool_call_id`; recover the original
    # function name from the preceding assistant tool-call payload so that
    # search/file/progress-specific compression policies still apply.
    tool_names_by_call_id: dict[str, str] = {}
    for message in copied:
        if str(message.get("role") or "") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                call_id = str(call.get("id") or call.get("tool_call_id") or "")
                function = call.get("function")
                tool_name = (
                    str(function.get("name") or "")
                    if isinstance(function, dict)
                    else str(call.get("name") or "")
                )
            else:
                call_id = str(getattr(call, "id", "") or "")
                function = getattr(call, "function", None)
                tool_name = str(
                    getattr(function, "name", "")
                    if function is not None
                    else getattr(call, "name", "")
                )
            if call_id and tool_name:
                tool_names_by_call_id[call_id] = tool_name

    compression_config = {
        "context_compression": {
            "enabled": True,
            "min_chars": 0,
            "tool_result_max_chars": max(1, int(max_result_chars)),
            "protect": {
                "error_outputs_under_chars": 8000,
                "latest_project_progress": True,
            },
        }
    }
    try:
        from .context_compression import model_tool_result_payload
    except Exception:  # pragma: no cover - import failures should not drop history
        model_tool_result_payload = None

    per_result_budget = max(
        1,
        min(
            max(1, int(max_result_chars)),
            max(1, int(max_total_chars)) // max(1, len(older_indexes)),
        ),
    )

    for index in older_indexes:
        message = copied[index]
        content = str(message.get("content") or "")
        if not content:
            continue
        if model_tool_result_payload is not None:
            try:
                compressed = model_tool_result_payload(
                    tool_name=tool_names_by_call_id.get(
                        str(message.get("tool_call_id") or ""),
                        str(message.get("name") or "tool"),
                    ),
                    output=content,
                    user_input="",
                    max_chars=per_result_budget,
                    config=compression_config,
                ).text
            except Exception:
                compressed = content[:per_result_budget].rstrip() + "\n... (history compacted)"
        else:
            compressed = content[:per_result_budget].rstrip() + "\n... (history compacted)"
        message["content"] = compressed

    return copied


def merge_model_transcript_snapshot(
    model_messages: list[dict[str, Any]],
    previous_transcript: list[dict[str, Any]],
    fallback_start: int,
    transcript: Any,
    *,
    allowed_roles: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Merge one cumulative provider transcript without duplicating checkpoints."""

    if not isinstance(transcript, list) or not transcript:
        return model_messages, previous_transcript, fallback_start

    filtered_transcript = [
        dict(item)
        for item in transcript
        if isinstance(item, dict) and item.get("role") in allowed_roles
    ]
    if not filtered_transcript:
        return model_messages, previous_transcript, fallback_start

    snapshot_start = None
    if previous_transcript and len(previous_transcript) <= len(filtered_transcript):
        last_start = len(filtered_transcript) - len(previous_transcript)
        for start in range(last_start, -1, -1):
            if (
                filtered_transcript[start : start + len(previous_transcript)]
                == previous_transcript
            ):
                snapshot_start = start
                break

    if snapshot_start is not None:
        new_messages = filtered_transcript[
            snapshot_start + len(previous_transcript) :
        ]
        if new_messages:
            # Display messages since the last checkpoint are fallbacks.  Once
            # the provider advances, replace the entire interval with its
            # authoritative suffix so failed users are not duplicated.
            model_messages = model_messages[:fallback_start]
            model_messages.extend(new_messages)
            fallback_start = len(model_messages)
    else:
        # The first or a disjoint transcript is an authoritative checkpoint,
        # for example after a branch or provider-state reset.
        model_messages = filtered_transcript
        fallback_start = len(model_messages)

    return (
        model_messages,
        [dict(item) for item in filtered_transcript],
        fallback_start,
    )


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
    current = getattr(value, name, default)
    if current is not default:
        return current
    model_extra = getattr(value, "model_extra", None)
    if isinstance(model_extra, Mapping):
        return model_extra.get(name, default)
    return current


def _as_mapping(value: Any) -> dict[str, Any] | None:
    """Coerce provider payload fragments (dict / pydantic / SimpleNamespace) into a dict."""

    if value is None:
        return None
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    for attr in ("model_dump", "dict", "to_dict"):
        method = getattr(value, attr, None)
        if callable(method):
            try:
                dumped = method()
            except Exception:  # noqa: BLE001
                continue
            if isinstance(dumped, Mapping):
                return {str(key): item for key, item in dumped.items()}
    raw = getattr(value, "__dict__", None)
    if isinstance(raw, Mapping) and raw:
        return {
            str(key): item
            for key, item in raw.items()
            if not str(key).startswith("_")
        }
    return None


def _tool_invocation_counts(usage: Any) -> dict[str, int] | None:
    """Extract per-tool invocation counts when the provider reports them."""

    counts: dict[str, int] = {}
    declared = _as_mapping(_field(usage, "tool_invocations"))
    if declared:
        for name, value in declared.items():
            count = _as_non_negative_int(value)
            if count:
                counts[str(name)] = count
    for key in ("num_search_queries", "web_search_requests", "search_count"):
        count = _as_non_negative_int(_field(usage, key))
        if count:
            counts["web_search"] = max(counts.get("web_search", 0), count)
    return counts or None


def normalize_usage(
    usage: Any,
    *,
    provider: str = "",
    resolved_model: str | None = None,
) -> dict[str, Any]:
    """Normalize heterogeneous OpenAI/local usage payloads.

    Missing fields remain ``None`` so an unavailable metric is not confused
    with a measured zero.  Billing-relevant extras (provider reported cost,
    resolved model, tool invocations) are only added when actually reported,
    so callers can compare the returned key set.
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
    prompt_cache_hit_tokens = _as_non_negative_int(
        _field(usage, "prompt_cache_hit_tokens")
    )
    if prompt_cache_hit_tokens is not None:
        if cached_tokens is None:
            cached_tokens = prompt_cache_hit_tokens
        if cache_read_tokens is None:
            cache_read_tokens = prompt_cache_hit_tokens
    cache_write_raw = _field(usage, "cache_write_tokens")
    if cache_write_raw is None:
        cache_write_raw = _field(usage, "cache_creation_input_tokens")
    if cache_write_raw is None:
        # OpenAI/OpenRouter は prompt_tokens_details 配下に書き込みトークンを載せる。
        cache_write_raw = _field(input_details, "cache_write_tokens")
    if cache_write_raw is None:
        cache_write_raw = _field(input_details, "cache_creation_tokens")
    cache_write_tokens = _as_non_negative_int(cache_write_raw)
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

    # --- 課金用の追加情報（取得できたときだけ入れる） ---------------------------
    # 既存呼び出し側が返却キー集合を比較しているため、値が無いキーは追加しない。
    reported_cost = _field(usage, "cost")
    if reported_cost is None:
        reported_cost = _field(usage, "provider_reported_cost")
    if reported_cost is not None and not isinstance(reported_cost, bool):
        result["provider_reported_cost"] = str(reported_cost)
    cost_details = _as_mapping(_field(usage, "cost_details"))
    if cost_details is None:
        cost_details = _as_mapping(_field(usage, "provider_reported_cost_details"))
    estimated_cost = _field(usage, "estimated_cost")
    if estimated_cost is not None and not isinstance(estimated_cost, bool):
        # DeepInfra reports an estimate, not a provider-confirmed charge.
        # Preserve it as details so billing logic does not treat it as final.
        cost_details = dict(cost_details or {})
        cost_details.setdefault("estimated_cost", estimated_cost)
        cost_details.setdefault("cost_type", "provider_estimate")
    if cost_details:
        result["provider_reported_cost_details"] = cost_details
    resolved = resolved_model
    if resolved is None:
        resolved = _field(usage, "resolved_model") or _field(usage, "model_version")
    if resolved:
        result["resolved_model"] = str(resolved)
    tool_invocations = _tool_invocation_counts(usage)
    if tool_invocations:
        result["tool_invocations"] = tool_invocations

    if result["input_tokens"] is not None or result["output_tokens"] is not None:
        result["total_tokens"] = (result["input_tokens"] or 0) + (result["output_tokens"] or 0)
    return result


def persist_usage_sync(
    client: Any,
    *,
    provider: str,
    model: str,
    usage: Mapping[str, Any] | None,
    requested_model: str | None = None,
    resolved_model: str | None = None,
    request_type: str = "chat",
    latency_ms: int = 0,
    is_streaming: bool = False,
) -> bool:
    """Persist normalized usage from a synchronous provider client.

    ``model`` historically represented the requested model.  Ephemeral and
    provider-routed calls also need to preserve the model name selected by the
    caller separately from the model that the provider actually served, so
    callers may pass explicit ``requested_model``/``resolved_model`` values.
    Existing callers can continue passing only ``model`` and a normalized
    ``usage["resolved_model"]``.
    """
    if not usage:
        return False
    try:
        from ..services.token_tracking_service import get_token_tracking_service

        requested = str(requested_model or model or "")
        resolved = resolved_model or usage.get("resolved_model")

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
                model=requested,
                requested_model=requested,
                resolved_model=resolved,
                provider_reported_cost=usage.get("provider_reported_cost"),
                provider_reported_cost_details=usage.get(
                    "provider_reported_cost_details"
                ),
                tool_invocations=usage.get("tool_invocations"),
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
