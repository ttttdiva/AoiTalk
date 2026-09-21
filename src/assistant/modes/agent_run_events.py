"""AgentRun イベント整形・記録ユーティリティ

terminal_mode.py から挙動不変で切り出した AgentRun 関連ヘルパ群と、
`_process_user_message_web` 内のネスト関数（イベント payload 構築・安全送出）を
まとめた `AgentRunEventEmitter` を提供する。ロジック・例外処理・送出内容は
イベント移設時の互換性を保ちつつ、完了判定では成功したmutation実績も扱う。
"""

import hashlib
import inspect
import json
import re
import time
from datetime import datetime
from typing import Any, Optional

from ...llm.agentic_completion import (
    _simple_task_has_unexpected_successful_mutation,
    _simple_task_post_create_has_blocked_work,
    _simple_task_request_is_explicit,
    requested_deterministic_task_mutation_tools,
    response_looks_like_unfinished_work,
)
from ...llm.context_snapshot import sanitize_context_snapshot
from ...llm.tool_policy import (
    DOCS_MUTATION_TOOL_NAMES,
    FILESYSTEM_MUTATION_TOOL_NAMES,
    PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES,
    mutation_execution_forbidden,
)
from ...services.agent_run_service import (
    AgentRunService,
    sanitize_assistant_display_text,
    sanitize_durable_error_text,
    sanitize_stream_display_payload,
    sanitize_tool_audit_payload,
)
from ...services.agent_team_service import config_get
from ...services.agent_team_v3 import (
    AGENT_TEAM_SUBAGENT_CATALOG,
    agent_team_v3_teams,
    agent_team_v3_subagents,
    resolve_agent_team_v3_route,
)
from ...services.turn_context import get_turn_context


_SEARCH_TOOL_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"'、。]+")
_DOCS_SEARCH_HIT_RE = re.compile(r"^\s*([0-9a-fA-F]{8,36})\s*\|", re.MULTILINE)
_SEARCH_URL_LIMIT = 20
_MUTATION_TOOL_NAMES = (
    PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES
    | DOCS_MUTATION_TOOL_NAMES
    | FILESYSTEM_MUTATION_TOOL_NAMES
)
# A failed mutation may leave no successful tool record at all.  In that
# situation the terminal completion check still needs to distinguish an
# imperative update request from ordinary prose.  Keep this fallback narrow:
# it only fires for explicit imperative forms and a response that is visibly a
# plan, so the command-context trust boundary in ``response_looks_like...``
# remains intact for normal turns.
_PLAIN_MUTATION_REQUEST_HINTS = (
    "更新して",
    "更新してください",
    "登録して",
    "登録してください",
    "作成して",
    "作成してください",
    "削除して",
    "削除してください",
    "移動して",
    "移動してください",
    "反映して",
    "反映してください",
    "編集して",
    "編集してください",
    "変更して",
    "変更してください",
    "closedにして",
    "closeして",
    "クローズして",
    "完了にして",
    "update it",
    "update this",
    "create it",
    "delete it",
    "move it",
    "edit it",
    "modify it",
)
_PLAIN_MUTATION_EXPLANATION_HINTS = (
    "説明",
    "方法",
    "とは",
    "について",
    "how to",
    "what is",
)
_PLAIN_INCOMPLETE_REPLY_HINTS = (
    "次は",
    "これから",
    "確認する",
    "確認します",
    "調査する",
    "調査します",
    "実行する",
    "実行します",
    "必要があります",
    "必要です",
    "UIで対応してください",
    "UIで対応",
    "next,",
    "let me",
    "i will",
)
_SHARED_INTEGRATION_TOOLS = {
    "media_assistant": "media",
}
AGENT_RUN_DELEGATION_TOOL_SUBAGENTS = {
    "agent_team_delegate": "agent_team",
}
_SPOTIFY_TOOL_NAMES = frozenset(
    {
        "setup_spotify_auth", "set_spotify_auth_code", "search_spotify_activity",
        "get_spotify_activity_stats", "get_recent_spotify_activity",
        "get_spotify_listening_patterns", "search_spotify_music",
        "play_spotify_track", "play_song_now", "queue_song", "pause_spotify",
        "skip_spotify_track", "previous_track", "get_spotify_status", "show_queue",
        "clear_spotify_queue", "remove_from_queue", "get_spotify_user_playlists",
        "create_playlist", "create_playlist_from_queue", "add_tracks_to_playlist",
        "add_queue_to_playlist", "add_playlist_to_queue", "remove_tracks_from_playlist",
        "play_playlist",
    }
)
_PROVIDER_MODEL_KEYS = {
    "openai": ("openai.model",),
    "gemini": ("gemini.model",),
    "openai_compatible_local": (
        "openai_compatible_local.model",
        "openai_compatible_local_model",
    ),
    "sglang": ("sglang.model", "sglang_model"),
    "openrouter": ("openrouter.model", "openrouter_model"),
    "deepseek": ("deepseek.model", "deepseek_model"),
    "deepinfra": ("deepinfra.model", "deepinfra_model"),
    "ollama": ("ollama.model", "ollama_model"),
    "codex-cli": ("codex_cli.model",),
    "claude-cli": ("claude_cli.model",),
    "antigravity-cli": ("antigravity_cli.model",),
    "grok-cli": ("grok_cli.model",),
}


def _extract_search_tool_urls(output_text: str) -> list[str]:
    urls: list[str] = []
    for match in _SEARCH_TOOL_URL_RE.finditer(str(output_text or "")):
        url = match.group(0).rstrip(".,;:!?")
        if url not in urls:
            urls.append(url)
        if len(urls) >= _SEARCH_URL_LIMIT:
            break
    return urls


def _config_text(config: Any, key: str, default: str = "") -> str:
    return str(config_get(config, key, default) or "").strip()


def _main_agent_run_provider(config: Any) -> str:
    provider = _config_text(config, "llm_provider", "openai").lower()
    return provider or "openai"


def _main_agent_run_model(config: Any, provider: str) -> str:
    selected = _config_text(config, "llm_model")
    if selected:
        return selected
    for key in _PROVIDER_MODEL_KEYS.get(provider, ()):
        value = _config_text(config, key)
        if value:
            return value
    return ""


def _agent_run_subagent_context(
    config: Any,
    subagent_id: str,
    *,
    team_id: str | None = None,
) -> dict[str, Any]:
    provider = ""
    model = ""
    subagent = next(
        (
            item
            for item in agent_team_v3_subagents(config)
            if str(item.get("subagent_id") or "") == str(subagent_id)
        ),
        None,
    )
    route = resolve_agent_team_v3_route(config, subagent_id) or {}
    if route:
        provider = str(route.get("provider") or "").strip()
        model = str(route.get("model") or "").strip()
    if not provider:
        provider = _main_agent_run_provider(config)
    if not model:
        model = _main_agent_run_model(config, provider)
    return {
        "actor_type": "agent_team",
        "actor_key": subagent_id,
        "actor_label": str(
            (subagent or {}).get("name")
            or AGENT_TEAM_SUBAGENT_CATALOG.get(subagent_id, {}).get("name")
            or subagent_id
        ),
        "team_id": team_id or None,
        "subagent_id": subagent_id,
        "llm_profile_id": None,
        "execution_profile_id": str(route.get("execution_profile_id") or "") or None,
        "provider": provider,
        "model": model,
        "route_source": str(route.get("route_source") or "main_inherit").strip()
        or "main_inherit",
    }


def _agent_run_tool_context(config: Any, data: dict[str, Any]) -> dict[str, str]:
    tool_result = data.get("tool_result")
    tool_name = str(data.get("tool") or data.get("tool_name") or "").strip()
    if not tool_name and isinstance(tool_result, dict):
        tool_name = str(tool_result.get("tool") or tool_result.get("name") or "").strip()
    if not tool_name:
        return {}

    if tool_name == "agent_team_delegate":
        tool_args = (
            data.get("tool_args") if isinstance(data.get("tool_args"), dict) else {}
        )
        requested_team = str(tool_args.get("team") or "").strip()
        configured_team = next(
            (
                item
                for item in agent_team_v3_teams(config)
                if str(item.get("team_id") or "") == requested_team
                or str(item.get("name") or "").casefold() == requested_team.casefold()
            ),
            None,
        )
        requested_team_id = str(
            (configured_team or {}).get("team_id") or requested_team
        ).strip()
        requested_id = str(tool_args.get("subagent") or "").strip()
        subagent = next(
            (
                item
                for item in agent_team_v3_subagents(config, include_disabled=False)
                if str(item.get("subagent_id") or "") == requested_id
                or str(item.get("name") or "").casefold() == requested_id.casefold()
            ),
            None,
        )
        if not subagent:
            return {}
        subagent_id = str(subagent.get("subagent_id") or requested_id).strip()
        if configured_team and subagent_id not in {
            str(item).strip() for item in (configured_team.get("subagent_ids") or [])
        }:
            return {}
        route = resolve_agent_team_v3_route(config, subagent_id) or {}
        provider = str(route.get("provider") or "").strip()
        provider = provider or _main_agent_run_provider(config)
        model = str(route.get("model") or "").strip()
        model = model or _main_agent_run_model(config, provider)
        label = str(subagent.get("name") or AGENT_TEAM_SUBAGENT_CATALOG.get(subagent_id, {}).get("name") or subagent_id)
        return {
            "actor_type": "agent_team",
            "actor_key": subagent_id,
            "actor_label": label,
            "team_id": requested_team_id or None,
            "provider": provider,
            "model": model,
            "subagent_id": subagent_id,
            "llm_profile_id": None,
        "execution_profile_id": str(route.get("execution_profile_id") or "") or None,
            "mode": str(route.get("effort") or route.get("reasoning_effort") or "").strip(),
            "route_source": str(route.get("route_source") or "main_inherit").strip()
            or "main_inherit",
        }

    integration_key = _SHARED_INTEGRATION_TOOLS.get(tool_name)
    if not integration_key and tool_name in _SPOTIFY_TOOL_NAMES:
        integration_key = "spotify"
    if integration_key:
        return {
            "actor_type": "integration",
            "actor_key": integration_key,
            "actor_label": {
                "utility": "補助機能",
                "media": "メディア連携",
                "spotify": "Spotify連携",
            }.get(integration_key, integration_key),
            "provider": _main_agent_run_provider(config),
            "model": _main_agent_run_model(config, _main_agent_run_provider(config)),
            "route_source": "main_inherit",
        }

    return {}


def _enrich_agent_run_event_payload(
    config: Any,
    data: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(data or {})
    context = _agent_run_tool_context(config, payload)
    if not context:
        return payload

    payload.setdefault("actor_type", context.get("actor_type"))
    payload.setdefault("actor_key", context.get("actor_key"))
    if context.get("subagent_id"):
        payload.setdefault("subagent_id", context.get("subagent_id"))
    if context.get("team_id"):
        payload.setdefault("team_id", context.get("team_id"))
    payload.setdefault("actor_label", context.get("actor_label"))
    payload.setdefault("agent_label", context.get("actor_label"))
    payload.setdefault("provider", context.get("provider"))
    payload.setdefault("model", context.get("model"))
    payload.setdefault("route_source", context.get("route_source"))
    payload.setdefault("mode", context.get("mode"))
    payload.setdefault("reasoning_effort", context.get("mode"))
    if context.get("execution_profile_id"):
        payload.setdefault("execution_profile_id", context.get("execution_profile_id"))

    tool_result = payload.get("tool_result")
    if isinstance(tool_result, dict):
        tool_result = dict(tool_result)
        tool_result.setdefault("actor_type", context.get("actor_type"))
        tool_result.setdefault("actor_key", context.get("actor_key"))
        tool_result.setdefault("actor_label", context.get("actor_label"))
        tool_result.setdefault("provider", context.get("provider"))
        tool_result.setdefault("model", context.get("model"))
        tool_result.setdefault("route_source", context.get("route_source"))
        tool_result.setdefault("mode", context.get("mode"))
        tool_result.setdefault("reasoning_effort", context.get("mode"))
        if context.get("subagent_id"):
            tool_result.setdefault("subagent_id", context.get("subagent_id"))
        if context.get("team_id"):
            tool_result.setdefault("team_id", context.get("team_id"))
        if context.get("execution_profile_id"):
            tool_result.setdefault("execution_profile_id", context.get("execution_profile_id"))
        payload["tool_result"] = tool_result
    return payload


def _agent_run_tool_operation_signature(data: dict[str, Any]) -> str:
    tool_result = data.get("tool_result")
    tool_name = str(data.get("tool") or data.get("tool_name") or "").strip()
    if not tool_name and isinstance(tool_result, dict):
        tool_name = str(tool_result.get("tool") or tool_result.get("name") or "").strip()
    arguments = data.get("tool_args") or data.get("arguments") or data.get("args")
    if not isinstance(arguments, dict) and isinstance(tool_result, dict):
        arguments = tool_result.get("arguments") or tool_result.get("args")
    if not isinstance(arguments, dict):
        arguments = {}
    return f"{tool_name}\0{json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)}"


def _is_isolated_controller_turn() -> bool:
    """Whether this AgentRun belongs to a Guide-only controller turn."""

    try:
        return bool(get_turn_context().suppress_automatic_context)
    except Exception:
        return False


def _client_tool_calls(client) -> list[Any]:
    # chat completions 経路は `_last_tool_calls`、Responses API 経路（native runtime）は
    # `_last_turn_tool_records` に積む。前者しか見ていなかったため Responses 経路の
    # ツール実行が agent_run_tool_calls に1件も残らず、実行内容を後から追えなかった。
    if _is_isolated_controller_turn():
        return []
    for attribute in ("_last_tool_calls", "_last_turn_tool_records"):
        calls = getattr(client, attribute, None)
        if calls:
            return list(calls)
    return []


async def _peek_client_agent_run_state(
    client,
    run_id: str | None,
) -> tuple[list[Any], dict[str, int] | None, str] | None:
    """Peek at provider run state so DB failures remain retryable."""

    if _is_isolated_controller_turn():
        # Never let a coincidentally reused cancellation/run id expose an
        # ordinary turn's completed tool ledger or usage to Help.
        return None
    getter = getattr(client, "peek_completed_agent_run_state", None)
    if not callable(getter):
        return None
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return [], None, ""
    try:
        state = getter(normalized_run_id)
        if inspect.isawaitable(state):
            state = await state
    except Exception:
        return [], None, ""
    if not isinstance(state, dict):
        return [], None, ""
    raw_tool_calls = state.get("tool_calls")
    tool_calls = list(raw_tool_calls) if isinstance(raw_tool_calls, (list, tuple)) else []
    raw_usage = state.get("usage")
    if not isinstance(raw_usage, dict):
        return tool_calls, None, str(state.get("failure") or "")
    usage: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens"):
        value = raw_usage.get(key)
        if value is None:
            continue
        try:
            usage[key] = max(0, int(value))
        except (TypeError, ValueError):
            continue
    if usage:
        usage.setdefault(
            "total_tokens",
            usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        )
    return tool_calls, usage or None, str(state.get("failure") or "")


async def _ack_client_agent_run_state(client, run_id: str | None) -> None:
    acknowledger = getattr(client, "ack_completed_agent_run_state", None)
    if not callable(acknowledger):
        return
    result = acknowledger(run_id)
    if inspect.isawaitable(result):
        await result


async def _discard_client_generation_run(client, run_id: str | None) -> None:
    if _is_isolated_controller_turn():
        return
    discard = getattr(client, "discard_generation_run", None)
    if not callable(discard):
        return
    try:
        result = discard(run_id)
        if inspect.isawaitable(result):
            await result
    except Exception:
        return


def _client_tool_rounds_exhausted(client) -> bool:
    """ツールループ上限で打ち切られたターンかどうか。"""
    if _is_isolated_controller_turn():
        return False
    return bool(getattr(client, "_last_turn_tool_rounds_exhausted", False))


def _client_tool_loop_failed(client) -> bool:
    """CLI tool loop ended unsuccessfully for any reason.

    ``_last_turn_tool_rounds_exhausted`` predates follow-up failures and only
    covered the max-rounds case.  Keep max-rounds clients compatible while
    also honoring the broader failure flag exposed by CLI clients.
    """
    if _is_isolated_controller_turn():
        return False
    return bool(
        getattr(client, "_last_turn_tool_loop_failed", False)
        or _client_tool_rounds_exhausted(client)
    )


def _client_context_snapshot(client) -> dict[str, Any] | None:
    """Read generation observation without letting metadata failures stop a run."""
    if _is_isolated_controller_turn():
        return None
    getter = getattr(client, "get_generation_metadata", None)
    if not callable(getter):
        return None
    try:
        metadata = getter() or {}
        if not isinstance(metadata, dict):
            return None
        return sanitize_context_snapshot(metadata.get("context_snapshot"))
    except Exception:
        return None


def _client_context_manifest_ref(client) -> dict[str, Any] | None:
    """Return only the validated, hash-based ContextManifest projection."""

    if _is_isolated_controller_turn():
        return None

    getter = getattr(client, "get_generation_metadata", None)
    if not callable(getter):
        return None
    try:
        metadata = getter() or {}
        if not isinstance(metadata, dict):
            return None
        from ...llm.context_snapshot import validate_context_manifest_metadata

        manifest = validate_context_manifest_metadata(metadata.get("context_manifest"))
        if not isinstance(manifest, dict):
            return None
        evidence_hashes: list[str] = []
        for item in manifest.get("evidence") or []:
            if not isinstance(item, dict):
                continue
            value = item.get("locator_hash") or item.get("ref_hash")
            if value:
                evidence_hashes.append(str(value))
        return {
            "schema_version": str(manifest.get("schema_version") or ""),
            "manifest_hash": str(manifest.get("manifest_hash") or ""),
            "reproducibility_hashes": {
                str(key): str(value)
                for key, value in (manifest.get("reproducibility_hashes") or {}).items()
                if str(key) and str(value)
            },
            "evidence_hashes": sorted(set(evidence_hashes))[:512],
            "evidence_count": len(evidence_hashes),
        }
    except Exception:
        return None
def _client_agent_run_usage(client) -> dict[str, int] | None:
    """Return the client-side confirmed usage accumulated for this AgentRun."""
    if _is_isolated_controller_turn():
        return None
    getter = getattr(client, "get_generation_metadata", None)
    if not callable(getter):
        return None
    try:
        metadata = getter() or {}
        if not isinstance(metadata, dict):
            return None
        raw_usage = metadata.get("agent_run_usage")
        if not isinstance(raw_usage, dict):
            return None

        usage: dict[str, int] = {}
        for key in (
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "total_tokens",
        ):
            value = raw_usage.get(key)
            if value is None:
                continue
            try:
                usage[key] = max(0, int(value))
            except (TypeError, ValueError):
                continue
        if not usage:
            return None
        usage.setdefault(
            "total_tokens",
            usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        )
        return usage
    except Exception:
        return None


def _agent_run_tool_call_payload(call: Any) -> dict[str, Any]:
    """Normalize provider tool records without dropping audit correlation.

    Native/CLI adapters do not all use the same record class.  In particular,
    some expose ``operation_id``/``tool_call_id`` and an explicit ``error``
    while older records only expose ``successful`` and ``result``.  Keep the
    durable AgentRun shape additive and derive success only when the provider
    did not supply an explicit value.
    """

    dumped: dict[str, Any] = {}
    if isinstance(call, dict):
        dumped = dict(call)
    else:
        model_dump = getattr(call, "model_dump", None)
        if callable(model_dump):
            try:
                candidate = model_dump()
                if isinstance(candidate, dict):
                    dumped = dict(candidate)
            except Exception:
                dumped = {}

    def value(*keys: str, default: Any = None) -> Any:
        for key in keys:
            if key in dumped and dumped[key] is not None:
                return dumped[key]
            candidate = getattr(call, key, None)
            if candidate is not None:
                return candidate
        return default

    raw_arguments = value("arguments", "args", default={}) or {}
    arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
    raw_result = value(
        "result",
        "output",
        "model_output",
        default="",
    )
    result_object = raw_result if isinstance(raw_result, dict) else None
    if isinstance(raw_result, (dict, list, tuple)):
        result: Any = json.dumps(
            raw_result,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    else:
        result = str(raw_result or "")
    if result_object is None and isinstance(result, str) and result.lstrip().startswith("{"):
        try:
            parsed_result = json.loads(result)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed_result = None
        if isinstance(parsed_result, dict):
            result_object = parsed_result
    error = value("error", "failure", "error_message", default=None)
    if error is not None:
        error = str(error)

    explicit_success = value("successful", "success", default=None)
    if isinstance(result_object, dict):
        if explicit_success is None:
            explicit_success = result_object.get("success")
        if error is None and result_object.get("error"):
            error = str(result_object.get("error"))
    if isinstance(explicit_success, bool):
        successful = explicit_success
    elif explicit_success is not None:
        successful = str(explicit_success).strip().lower() in {
            "1",
            "true",
            "yes",
            "ok",
            "success",
            "succeeded",
        }
    elif error:
        successful = False
    else:
        lowered = str(result).strip().lower()
        successful = not (
            lowered.startswith("error:")
            or lowered.startswith("tool execution error:")
            or lowered.startswith("tool not found:")
            or "delegation error" in lowered
        )
    if error and str(error).strip():
        # Never let a contradictory provider marker (successful=True plus an
        # error/failure field) become authoritative completion evidence.
        successful = False
    if isinstance(result_object, dict):
        result_success = result_object.get("success")
        if result_success is not None and str(result_success).strip().casefold() not in {
            "1",
            "true",
            "yes",
            "ok",
            "success",
            "succeeded",
        }:
            successful = False

    tool_call_id = value(
        "tool_call_id",
        "operation_id",
        "call_id",
        "id",
        default=None,
    )
    normalized_tool_call_id = str(tool_call_id or "").strip() or None
    metadata = value("metadata", "result_metadata", "meta", default={})
    if not isinstance(metadata, dict):
        metadata = {}
    else:
        metadata = dict(metadata)
    if error:
        metadata.setdefault("error", error)

    safe_payload = sanitize_tool_audit_payload(
        {
            "tool": str(value("tool", "name", default="") or ""),
            "arguments": dict(arguments),
            "result": result,
            "successful": bool(successful),
            "error": error,
            "tool_call_id": normalized_tool_call_id,
        }
    )
    safe_arguments = safe_payload.get("arguments")
    if not isinstance(safe_arguments, dict):
        safe_arguments = {"_redacted": "[REDACTED_UNAVAILABLE]"}
    safe_result = safe_payload.get("result")
    if isinstance(safe_result, (dict, list, tuple)):
        safe_result = json.dumps(
            safe_result,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    elif safe_result is None:
        safe_result = ""
    else:
        safe_result = str(safe_result)
    payload: dict[str, Any] = {
        "tool": str(safe_payload.get("tool") or value("tool", "name", default="") or ""),
        "arguments": safe_arguments,
        "result": safe_result,
        "successful": bool(successful),
        "error": safe_payload.get("error"),
        "metadata": sanitize_tool_audit_payload(metadata),
        "tool_call_id": str(safe_payload.get("tool_call_id") or normalized_tool_call_id or "") or None,
        "already_recorded": bool(
            value("tool_result_already_recorded", "already_recorded", default=False)
        ),
    }
    for key in ("event_id", "started_at", "ended_at", "duration_ms"):
        item = value(key, default=None)
        if item is not None:
            payload[key] = item
    return payload


# A deterministic mutation may be followed by a provider-side finalization
# failure (for example, the final text request can hit a quota limit).  Keep
# this allowlist deliberately small: only operations whose result is a durable
# object with a machine-checkable postcondition may be used as authoritative
# completion evidence.  Complex/project-review work continues through the
# normal verifier path.
_AUTHORITATIVE_MUTATION_TOOL_NAMES = frozenset({"create_task"})


def _decode_tool_result_payload(value: Any) -> Any:
    """Decode one provider tool result without trusting arbitrary wrappers."""

    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _task_result_object(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return the bounded task projection emitted by ``create_task``."""

    decoded = _decode_tool_result_payload(payload.get("result"))
    if not isinstance(decoded, dict):
        return None
    # Approved-mutation replay adapters may wrap the original task under a
    # ``task``/``data`` key.  Accept only a dictionary wrapper; never infer a
    # successful task from prose or an arbitrary truthy value.
    if not (decoded.get("id") or decoded.get("task_id")):
        for key in ("task", "data"):
            nested = decoded.get(key)
            if isinstance(nested, dict) and (nested.get("id") or nested.get("task_id")):
                decoded = nested
                break
    success_marker = decoded.get("success")
    if success_marker is not None and str(success_marker).strip().casefold() not in {
        "1",
        "true",
        "yes",
        "ok",
        "success",
        "succeeded",
    }:
        return None
    if str(decoded.get("error") or "").strip():
        return None
    task_id = decoded.get("id") or decoded.get("task_id")
    title = decoded.get("title")
    if not str(task_id or "").strip() or not str(title or "").strip():
        return None
    return decoded


def _task_schedule_field_present(task: dict[str, Any], key: str) -> bool:
    value = task.get(key)
    if isinstance(value, bool):
        return value
    return bool(str(value or "").strip())


def _canonical_task_datetime(value: Any) -> str:
    """Normalize the wall-clock ISO shape used by ``Task.to_dict``."""

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text[:-1] if text.endswith("Z") else text)
    except (TypeError, ValueError):
        return " ".join(text.split()).casefold()
    # Task scheduling deliberately stores wall-clock values without a timezone
    # offset.  Match the tool's normalization instead of comparing raw input
    # spelling (``19:00`` vs ``19:00:00``).
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed.isoformat()


def _task_boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _create_task_postcondition_holds(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the fields required for an authoritative task-create result.

    ``create_task`` returns ``Task.to_dict()``.  Validate every non-empty
    structured argument that the caller supplied, rather than treating a
    generic ``{"success": true}`` response as proof that the requested task
    exists.  Date-only ``due_date`` is normalized by the tool into ``start_at``.
    """

    if (
        payload.get("tool", "").casefold() != "create_task"
        or payload.get("successful") is not True
    ):
        return None
    task = _task_result_object(payload)
    if task is None:
        return None
    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}

    requested_title = str(arguments.get("title") or "").strip()
    if not requested_title:
        return None
    if str(task.get("title") or "").strip() != requested_title:
        return None
    # Keep the fallback limited to the flat Task.to_dict fields we can prove
    # without another provider/database read.  Nested recurrence/assignee and
    # opt-in auto-close requests stay on the normal verification path.
    if (
        bool(arguments.get("auto_close_on_due"))
        or str(arguments.get("assignee_ids") or "").strip()
        or str(arguments.get("recurrence_rrule") or "").strip()
    ):
        return None

    # Required identity fields are always checked above.  Optional fields are
    # checked only when the provider actually received a non-empty request for
    # them, preserving fail-closed behavior for missing schedule/description
    # data while allowing ordinary title-only tasks.
    requested_description = str(arguments.get("description") or "")
    if requested_description.strip():
        if not _task_schedule_field_present(task, "description"):
            return None
        if str(task.get("description") or "") != requested_description:
            return None
    requested_priority = str(arguments.get("priority") or "").strip()
    if requested_priority:
        if not _task_schedule_field_present(task, "priority"):
            return None
        if str(task.get("priority") or "").strip().casefold() != requested_priority.casefold():
            return None
    task_start_present = (
        _task_schedule_field_present(task, "start_at")
        or _task_schedule_field_present(task, "due_date")
    )
    if (
        str(arguments.get("start_at") or "").strip()
        or str(arguments.get("due_date") or "").strip()
    ) and not task_start_present:
        return None
    requested_start = str(arguments.get("start_at") or arguments.get("due_date") or "").strip()
    if requested_start and _canonical_task_datetime(requested_start) != _canonical_task_datetime(
        task.get("start_at") or task.get("due_date")
    ):
        return None
    if str(arguments.get("end_at") or "").strip() and not _task_schedule_field_present(
        task, "end_at"
    ):
        return None
    requested_end = str(arguments.get("end_at") or "").strip()
    if requested_end and _canonical_task_datetime(requested_end) != _canonical_task_datetime(
        task.get("end_at")
    ):
        return None
    requested_project_id = str(arguments.get("project_id") or "").strip()
    requested_project = str(arguments.get("project") or "").strip()
    if requested_project_id:
        observed_project_id = str(task.get("project_id") or "").strip()
        if not observed_project_id or observed_project_id.casefold() != requested_project_id.casefold():
            return None
    elif requested_project:
        observed_project_id = str(task.get("project_id") or "").strip()
        observed_project_name = str(
            task.get("project_name") or task.get("project") or ""
        ).strip()
        if not observed_project_id and not observed_project_name:
            return None
        if requested_project.casefold() not in {
            observed_project_id.casefold(),
            observed_project_name.casefold(),
        }:
            return None
    if str(arguments.get("parent_task_id") or "").strip() and not _task_schedule_field_present(
        task, "parent_task_id"
    ):
        return None
    if str(arguments.get("parent_task_id") or "").strip() and str(
        task.get("parent_task_id") or ""
    ).strip().casefold() != str(arguments.get("parent_task_id") or "").strip().casefold():
        return None
    requested_all_day = _task_boolean(arguments.get("all_day")) or bool(
        str(arguments.get("due_date") or "").strip()
    )
    if requested_all_day and not _task_boolean(task.get("all_day")):
        return None
    return task


def _search_result_is_empty(payload: dict[str, Any]) -> bool:
    """Return True only for a successful, unambiguous empty task search."""

    if payload.get("tool", "").casefold() not in {"search_task_candidates", "list_tasks"}:
        return False
    if payload.get("successful") is not True:
        return False
    decoded = _decode_tool_result_payload(payload.get("result"))
    if isinstance(decoded, list):
        return len(decoded) == 0
    if not isinstance(decoded, dict):
        return False
    indicators: list[bool] = []
    for key in ("items", "tasks", "candidates", "results"):
        if key in decoded and not isinstance(decoded.get(key), list):
            return False
        value = decoded.get(key)
        if isinstance(value, list):
            indicators.append(len(value) == 0)
    for key in ("count", "total"):
        if key in decoded and (
            isinstance(decoded.get(key), bool)
            or not isinstance(decoded.get(key), int)
        ):
            return False
        value = decoded.get(key)
        if isinstance(value, int):
            indicators.append(value == 0)
    return bool(indicators) and all(item == indicators[0] for item in indicators)


def _request_requires_task_create(user_input: str | None) -> bool:
    """Keep a mutation result tied to an explicit task-create request."""

    if user_input is None:
        # Callers that do not carry request text (legacy integrations) already
        # own the required-operation decision and may use the structured
        # ledger directly.
        return True
    raw_text = str(user_input).strip()
    text = raw_text.casefold()
    if not text:
        return False
    if not _simple_task_request_is_explicit(raw_text):
        return False
    if mutation_execution_forbidden(raw_text):
        return False
    question_markers = (
        "方法",
        "使い方",
        "教えて",
        "できますか",
        "できるか",
        "確認して",
        "確認したい",
        "かどうか",
        "?",
        "？",
    )
    confirmation_markers = (
        "してもいい",
        "してもよい",
        "しても大丈夫",
        "作ってもいい",
        "作ってもよい",
        "作っていい",
        "作って良い",
        "作成していい",
        "作成してよい",
        "作成して良い",
        "登録していい",
        "登録してよい",
        "登録して良い",
        "追加していい",
        "追加してよい",
        "追加して良い",
        "許可",
        "承認",
        "is it okay",
        "may i",
        "can i",
        "should i",
    )
    imperative_markers = (
        "作って",
        "作ってください",
        "作成して",
        "作成してください",
        "作成お願いします",
        "作成をお願いします",
        "登録して",
        "登録してください",
        "登録お願いします",
        "追加して",
        "追加してください",
        "入れて",
        "create ",
        "create_task",
        "register a task",
        "make a task",
        "add a task",
    )
    conditional_markers = (
        "作成予定",
        "作る予定",
        "作成するつもり",
        "作るつもり",
        "作成を検討",
        "作成を考えて",
    )
    if any(marker in text for marker in conditional_markers) and not any(
        marker in text for marker in imperative_markers
    ):
        return False
    if any(marker in text for marker in confirmation_markers):
        return False
    if any(marker in text for marker in question_markers) and not any(
        marker in text for marker in imperative_markers
    ):
        return False
    if any(marker in text for marker in ("?", "？")) and any(
        marker in text
        for marker in ("いい", "良い", "大丈夫", "可能", "できますか")
    ):
        return False
    try:
        required = requested_deterministic_task_mutation_tools(raw_text)
    except Exception:
        return False
    required.discard("schedule_task")
    return required == {"create_task"}


def authoritative_mutation_completion_evidence(
    tool_calls: list[Any] | None,
    *,
    user_input: str | None = None,
) -> dict[str, Any] | None:
    """Return safe completion evidence for a required deterministic mutation.

    The evidence is intentionally independent from the final assistant text.
    A successful ``create_task`` is authoritative only when the latest
    duplicate-candidate search *before that create* succeeded and was empty,
    and the returned task projection contains the requested fields.  Failures
    after the create remain in the audit ledger but do not erase this evidence.
    """

    if not _request_requires_task_create(user_input):
        return None
    records = [
        _agent_run_tool_call_payload(call)
        for call in (tool_calls or [])
    ]
    if _simple_task_has_unexpected_successful_mutation(records):
        return None
    if _simple_task_post_create_has_blocked_work(records):
        return None
    create_indexes = [
        index
        for index, payload in enumerate(records)
        if payload.get("tool", "").casefold() in _AUTHORITATIVE_MUTATION_TOOL_NAMES
        and payload.get("successful")
    ]
    # Multiple successful creates are ambiguous and must never be collapsed
    # into one user-facing success summary.
    if len(create_indexes) != 1:
        return None
    create_index = create_indexes[0]
    if any(
        not payload.get("successful")
        for index, payload in enumerate(records)
        if payload.get("tool", "").casefold() == "create_task"
        and index > create_index
    ):
        # A later failed create is a required-operation failure, not an
        # optional diagnostic.  Do not let the earlier success hide it.
        return None
    task = _create_task_postcondition_holds(records[create_index])
    if task is None:
        return None

    searches = [
        payload
        for index, payload in enumerate(records)
        if index < create_index
        and payload.get("tool", "").casefold()
        in {"search_task_candidates", "list_tasks"}
    ]
    if not searches or not _search_result_is_empty(searches[-1]):
        return None

    task_id = str(task.get("id") or task.get("task_id") or "").strip()
    title = " ".join(str(task.get("title") or "").split())
    safe_title = sanitize_assistant_display_text(title).strip()
    start_at = " ".join(
        str(task.get("start_at") or task.get("due_date") or "").split()
    )
    end_at = " ".join(str(task.get("end_at") or "").split())
    if start_at and end_at:
        schedule = f"{start_at}〜{end_at}"
    else:
        schedule = start_at or end_at
    safe_schedule = sanitize_assistant_display_text(schedule).strip()
    summary = f"タスク「{safe_title}」を作成しました。"
    if safe_schedule:
        summary += f"日時: {safe_schedule}"
    return {
        "tool": "create_task",
        "tool_call_id": records[create_index].get("tool_call_id"),
        "task_id": task_id,
        "title": safe_title,
        "start_at": sanitize_assistant_display_text(start_at).strip() or None,
        "end_at": sanitize_assistant_display_text(end_at).strip() or None,
        "summary": sanitize_assistant_display_text(summary),
    }


def _agent_run_tool_call_id(
    run_id: str | None,
    index: int,
    payload: dict[str, Any],
) -> str:
    existing = str(payload.get("tool_call_id") or "").strip()
    if existing:
        return existing
    canonical = json.dumps(
        {
            "run_id": str(run_id or ""),
            "index": index,
            "tool": payload["tool"],
            "arguments": payload["arguments"],
            "result": payload["result"],
            "successful": payload["successful"],
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"agent-run-terminal:{index}:{digest}"


def _successful_mutation_tool_call_count(tool_calls: list[Any]) -> int:
    count = 0
    for call in tool_calls:
        payload = _agent_run_tool_call_payload(call)
        if payload["successful"] and payload["tool"] in _MUTATION_TOOL_NAMES:
            count += 1
    return count


def _plain_mutation_request_is_incomplete(
    user_input: str | None,
    reply: Optional[str],
    *,
    successful_mutation_tool_count: int,
) -> bool:
    """Detect a failed explicit mutation without trusting arbitrary prose.

    The normal completion detector intentionally requires a server-provided
    command capability header.  Terminal AgentRun completion also receives
    turns where a mutation tool failed before a capability-bearing prompt was
    assembled, so a small imperative-only fallback is needed to avoid marking
    a plan-only answer as successful.
    """

    if successful_mutation_tool_count > 0:
        return False
    request = str(user_input or "").strip().casefold()
    if not request or any(
        hint.casefold() in request for hint in _PLAIN_MUTATION_EXPLANATION_HINTS
    ):
        return False
    if not any(hint.casefold() in request for hint in _PLAIN_MUTATION_REQUEST_HINTS):
        return False
    response = str(reply or "").strip().casefold()
    return bool(response) and any(
        hint.casefold() in response for hint in _PLAIN_INCOMPLETE_REPLY_HINTS
    )


def _agent_run_completion_result(
    *,
    reply: Optional[str],
    search_tool_results: list[dict[str, Any]],
    tool_calls: list[Any],
) -> dict[str, Any]:
    result_payload: dict[str, Any] = {
        # Keep the raw reply local for completion classification only; the
        # durable AgentRun projection must never carry provider credentials.
        "assistant_response": sanitize_assistant_display_text(reply),
        "tool_result_count": len(search_tool_results) + len(tool_calls),
        "successful_mutation_tool_count": _successful_mutation_tool_call_count(
            tool_calls
        ),
    }
    if tool_calls:
        result_payload["tool_calls"] = [
            _agent_run_tool_call_payload(call) for call in tool_calls
        ]
    return result_payload


def _should_fail_agent_run_completion(
    *,
    user_input: str,
    reply: Optional[str],
    search_tool_result_count: int = 0,
    successful_mutation_tool_count: int = 0,
    completion_confirmed: bool = False,
    tool_rounds_exhausted: bool = False,
    tool_loop_failed: bool = False,
) -> bool:
    if tool_rounds_exhausted or tool_loop_failed:
        return True
    if _looks_like_cli_execution_error(reply):
        return True
    if completion_confirmed and str(reply or "").strip():
        return response_looks_like_unfinished_work(
            user_input,
            reply,
            completion_confirmed=True,
        )
    if _plain_mutation_request_is_incomplete(
        user_input,
        reply,
        successful_mutation_tool_count=successful_mutation_tool_count,
    ):
        return True
    if (
        (search_tool_result_count > 0 or successful_mutation_tool_count > 0)
        and str(reply or "").strip()
    ):
        return False
    return response_looks_like_unfinished_work(user_input, reply)


def _looks_like_cli_execution_error(reply: Optional[str]) -> bool:
    text = str(reply or "").strip()
    if not text:
        return False
    lowered = text.lower()
    cli_markers = (
        "cli error:",
        "cli execution failed",
        "cli returned no output",
        "returned no output from print mode",
        "codex cli error",
        "codex cli failed",
        "antigravity cli returned no output",
        "antigravity cli error",
        "antigravity cli failed",
        "gemini cli returned no output",
        "gemini cli error",
        "gemini cli failed",
    )
    if any(marker in lowered for marker in cli_markers):
        return True
    return lowered.startswith("エラーが発生しました:") and "cli" in lowered


def _agent_run_completion_failure_message(
    *,
    user_input: str,
    reply: Optional[str],
    search_tool_result_count: int = 0,
    successful_mutation_tool_count: int = 0,
    completion_confirmed: bool = False,
    tool_rounds_exhausted: bool = False,
    tool_loop_failed: bool = False,
) -> Optional[str]:
    if tool_rounds_exhausted:
        # ツールループ上限で最終応答を作れずに打ち切ったターン。
        # 以前は succeeded のまま記録され、失敗が可視化されなかった。
        return "Tool loop hit the max round limit before producing a final answer"
    if tool_loop_failed:
        return "CLI tool loop failed before producing a final answer"
    if _looks_like_cli_execution_error(reply):
        return "CLI execution failed"
    if completion_confirmed and str(reply or "").strip():
        if response_looks_like_unfinished_work(
            user_input,
            reply,
            completion_confirmed=True,
        ):
            return "Assistant response did not complete the requested work"
        return None
    if _plain_mutation_request_is_incomplete(
        user_input,
        reply,
        successful_mutation_tool_count=successful_mutation_tool_count,
    ):
        return "Assistant response did not complete the requested work"
    if (
        (search_tool_result_count > 0 or successful_mutation_tool_count > 0)
        and str(reply or "").strip()
    ):
        return None
    if response_looks_like_unfinished_work(user_input, reply):
        return "Assistant response did not complete the requested work"
    return None


class AgentRunEventEmitter:
    """`_process_user_message_web` の AgentRun イベント送出ネスト関数群を集約したクラス。

    移設前はメソッド内クロージャで参照していた変数（agent_run_service /
    agent_run_id / session 情報 / 共有 search_tool_results / user_input など）を
    コンストラクタで明示的に受け取る。`search_tool_results` は呼び出し側と同一の
    リスト参照を保持し、ストリーム中の append が complete() 時に反映される点も
    移設前と同一。`finished` フラグは旧 nonlocal `agent_run_finished` に相当する。
    """

    def __init__(
        self,
        *,
        agent_run_service: Optional[AgentRunService],
        agent_run_id: Optional[str],
        session_id: Optional[str],
        project_id: Optional[str],
        generation_profile: Any,
        include_project_context: Any,
        command_capabilities: Any,
        search_tool_results: list[dict[str, Any]],
        user_input: str,
        log_prefix: str = "TerminalMode",
    ):
        self._agent_run_service = agent_run_service
        self._agent_run_id = agent_run_id
        self._session_id = session_id
        self._project_id = project_id
        self._generation_profile = generation_profile
        self._include_project_context = include_project_context
        self._command_capabilities = command_capabilities
        self._search_tool_results = search_tool_results
        self._user_input = user_input
        self._log_prefix = log_prefix
        self.finished = False
        # Command handlers can persist a tool result while the stream event is
        # being emitted.  Keep the explicit marker/correlation in this
        # emitter so terminal completion does not issue a second mutation.
        self._already_recorded_tool_ids: set[str] = set()
        self._already_recorded_tool_signatures: set[str] = set()
        self._event_tool_call_ids_by_signature: dict[str, str] = {}

    @staticmethod
    def _model_context(client) -> dict[str, Any]:
        provider = None
        route_source = "main_inherit"
        backend = getattr(client, "cli_backend", None)
        if backend and hasattr(backend, "get_provider_name"):
            try:
                provider = backend.get_provider_name()
            except Exception:
                provider = None
        provider = provider or getattr(client, "provider", None)
        provider = provider or getattr(client, "provider_label", None)
        model = getattr(backend, "_model", None) if backend else None
        model = (
            model
            or getattr(client, "model_name", None)
            or getattr(client, "model", None)
        )
        if str(model or "").strip().lower() == "default":
            model = None
        # The generation client already records the additive effective-route
        # projection.  Reuse it for AgentRun start metadata instead of reading
        # provider-specific defaults a second time.
        try:
            metadata_getter = getattr(client, "get_generation_metadata", None)
            generation_metadata = metadata_getter() if callable(metadata_getter) else {}
            route = (
                generation_metadata.get("route")
                if isinstance(generation_metadata, dict)
                else None
            )
            if isinstance(route, dict):
                route_source = str(route.get("route_source") or route_source).strip() or route_source
                provider = provider or route.get("provider")
                model = model or route.get("model")
        except Exception:
            # Route metadata is diagnostics-only; a malformed getter must not
            # prevent an otherwise valid AgentRun from starting.
            pass
        return {
            "provider": str(provider) if provider else None,
            "model": str(model) if model else None,
            "route_source": route_source,
        }

    # payload の text 本文をそのまま AgentRunEvent.message へ写すストリームイベント。
    # タイムラインが message を本文として表示するため、message 未設定でも本文を残す。
    TEXT_MESSAGE_EVENT_TYPES = frozenset(
        {
            "stream.assistant_text",
            "stream.thinking",
        }
    )

    @classmethod
    def _resolve_message_text(
        cls,
        event_type: str,
        payload: dict[str, Any],
        message_text: str | None,
    ) -> str | None:
        if message_text:
            return message_text
        if event_type not in cls.TEXT_MESSAGE_EVENT_TYPES:
            return message_text
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            return text
        return message_text

    @staticmethod
    def _event_payload(
        event_type: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        payload = dict(data or {})
        if event_type in {"stream.tool_start", "stream.tool_end"}:
            tool_result = payload.get("tool_result")
            if isinstance(tool_result, dict):
                tool_result = dict(tool_result)
                arguments = payload.get("tool_args")
                if not isinstance(arguments, dict):
                    arguments = payload.get("arguments") or payload.get("args")
                if isinstance(arguments, dict):
                    payload.setdefault("tool_args", dict(arguments))
                    tool_result.setdefault("arguments", dict(arguments))
                correlation = ""
                for source in (payload, tool_result):
                    if not isinstance(source, dict):
                        continue
                    correlation = str(
                        source.get("operation_id")
                        or source.get("tool_call_id")
                        or source.get("call_id")
                        or source.get("id")
                        or ""
                    ).strip()
                    if correlation:
                        break
                if correlation:
                    payload.setdefault("operation_id", correlation)
                    payload.setdefault("tool_call_id", correlation)
                    tool_result.setdefault("tool_call_id", correlation)
                if "success" not in tool_result and "error" in tool_result:
                    tool_result["success"] = not bool(tool_result.get("error"))
                if payload.get("error") and not tool_result.get("error"):
                    tool_result["error"] = payload.get("error")
                payload["tool_result"] = tool_result
            else:
                arguments = payload.get("arguments") or payload.get("args")
                if isinstance(arguments, dict):
                    payload.setdefault("tool_args", dict(arguments))
        for key in ("content", "delta", "text", "output"):
            value = payload.get(key)
            if (
                event_type != "stream.stream_token"
                and isinstance(value, str)
                and len(value) > 4000
            ):
                payload[key] = value[:4000].rstrip() + "\n... (truncated)"
        tool_result = payload.get("tool_result")
        if isinstance(tool_result, dict):
            tool_result = dict(tool_result)
            for key in ("output", "result", "error", "stderr"):
                value = tool_result.get(key)
                if isinstance(value, str) and len(value) > 20000:
                    tool_result[key] = (
                        value[:20000].rstrip() + "\n... (truncated)"
                    )
            payload["tool_result"] = tool_result
        payload = sanitize_stream_display_payload(event_type, payload)
        return payload

    async def record_event(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        *,
        status: str | None = None,
        message_text: str | None = None,
    ) -> None:
        if not self._agent_run_service:
            return
        payload = self._event_payload(event_type, data or {})
        if event_type == "stream.tool_end":
            tool_result = payload.get("tool_result")
            if isinstance(tool_result, dict):
                operation_id = str(
                    payload.get("operation_id")
                    or payload.get("tool_call_id")
                    or tool_result.get("tool_call_id")
                    or ""
                ).strip()
                signature = _agent_run_tool_operation_signature(payload)
                if operation_id:
                    self._event_tool_call_ids_by_signature[signature] = operation_id
                already_recorded = bool(
                    payload.get("tool_result_already_recorded")
                )
                if already_recorded:
                    if operation_id:
                        self._already_recorded_tool_ids.add(operation_id)
                    self._already_recorded_tool_signatures.add(signature)
        try:
            safe_message_text = (
                sanitize_assistant_display_text(message_text)
                if message_text is not None
                else None
            )
            await self._agent_run_service.record_event(
                self._agent_run_id,
                event_type,
                status=status,
                message=self._resolve_message_text(
                    event_type, payload, safe_message_text
                ),
                payload=payload,
            )
        except Exception as exc:
            print(f"[{self._log_prefix}] AgentRun event record failed: {exc}")

    async def _persist_tool_calls(self, tool_calls: list[Any]) -> None:
        """Persist provider tool records exactly once for this emitter.

        Event-driven command handlers may already have written a durable row;
        explicit ``tool_result_already_recorded`` markers are authoritative.
        Other adapters still receive the same correlation ID when one exists,
        allowing AgentRunService's run/idempotency constraint to collapse a
        repeated terminal observation.
        """

        if not self._agent_run_service:
            return
        for index, call in enumerate(tool_calls):
            payload = _agent_run_tool_call_payload(call)
            if not payload["tool"]:
                continue
            signature = _agent_run_tool_operation_signature(payload)
            event_tool_call_id = self._event_tool_call_ids_by_signature.get(signature)
            if not payload["tool_call_id"] and event_tool_call_id:
                payload["tool_call_id"] = event_tool_call_id
            tool_call_id = _agent_run_tool_call_id(
                self._agent_run_id,
                index,
                payload,
            )
            if payload["already_recorded"] or (
                tool_call_id in self._already_recorded_tool_ids
                or signature in self._already_recorded_tool_signatures
            ):
                continue
            persisted_tool_call = await self._agent_run_service.record_tool_call(
                self._agent_run_id,
                tool_name=payload["tool"],
                arguments=payload["arguments"],
                result=payload["result"],
                success=payload["successful"],
                mutation_confirmed=payload["tool"] in _MUTATION_TOOL_NAMES,
                tool_call_id=tool_call_id,
                event_id=payload.get("event_id"),
                metadata=payload.get("metadata") or None,
                started_at=payload.get("started_at"),
                ended_at=payload.get("ended_at"),
                duration_ms=payload.get("duration_ms"),
            )
            if persisted_tool_call is None:
                raise RuntimeError("AgentRun tool audit was not persisted")
            self._already_recorded_tool_ids.add(tool_call_id)

    async def mark_running(self, client) -> None:
        if not self._agent_run_service:
            return
        model_context = self._model_context(client)
        try:
            await self._agent_run_service.mark_running(
                self._agent_run_id,
                message="Assistant generation started",
                metadata={
                    "session_id": self._session_id,
                    "project_id": self._project_id,
                    "generation_profile": self._generation_profile,
                    "include_project_context": self._include_project_context,
                    "command_capabilities": list(self._command_capabilities),
                    "route_source": model_context["route_source"],
                },
                provider=model_context["provider"],
                model=model_context["model"],
            )
        except Exception as exc:
            print(f"[{self._log_prefix}] AgentRun start update failed: {exc}")

    async def authoritative_mutation_completion(self, client=None) -> dict[str, Any] | None:
        """Return this run's proven mutation result, if one exists.

        The lookup is run-keyed whenever the provider exposes its completion
        ledger.  That prevents a failed finalization from accidentally using a
        different conversation's stale ``_last_*`` fields as success evidence.
        """

        profile = getattr(self._generation_profile, "value", self._generation_profile)
        if str(profile or "").strip().casefold() == "review":
            return None
        # Only the task-update capability is allowed to share this narrow
        # create receipt path.  Workspace/Docs/DB/WBS/media/help/review
        # capabilities have independent verification contracts and must never
        # be promoted by a task-shaped sentence in the same turn.
        non_task_capabilities = set(self._command_capabilities or ()) - {
            "task_update"
        }
        if non_task_capabilities:
            return None

        run_state = await _peek_client_agent_run_state(client, self._agent_run_id)
        tool_calls = (
            run_state[0]
            if run_state is not None
            else _client_tool_calls(client)
        )
        evidence = authoritative_mutation_completion_evidence(
            tool_calls,
            user_input=self._user_input,
        )
        if evidence is not None and run_state is not None and run_state[2]:
            evidence["provider_finalization_failure"] = sanitize_durable_error_text(
                run_state[2]
            )
        return evidence

    async def complete(
        self,
        reply: Optional[str],
        client=None,
        *,
        completion_confirmed: bool = False,
        allow_authoritative_mutation: bool = False,
    ) -> None:
        if self.finished:
            return
        run_state = await _peek_client_agent_run_state(
            client,
            self._agent_run_id,
        )
        if not self._agent_run_service:
            await _discard_client_generation_run(client, self._agent_run_id)
            self.finished = True
            return
        try:
            tool_calls = (
                run_state[0]
                if run_state is not None
                else _client_tool_calls(client)
            )
            await self._persist_tool_calls(tool_calls)

            result_payload = _agent_run_completion_result(
                reply=reply,
                search_tool_results=self._search_tool_results,
                tool_calls=tool_calls,
            )
            successful_mutation_tool_count = _successful_mutation_tool_call_count(
                tool_calls
            )
            mutation_evidence = (
                authoritative_mutation_completion_evidence(
                    tool_calls,
                    user_input=self._user_input,
                )
                if allow_authoritative_mutation
                else None
            )
            context_snapshot = _client_context_snapshot(client)
            if context_snapshot:
                result_payload["context_snapshot"] = context_snapshot
            context_manifest_ref = _client_context_manifest_ref(client)
            if context_manifest_ref:
                result_payload["context_manifest"] = context_manifest_ref
            usage = (
                run_state[1]
                if run_state is not None
                else _client_agent_run_usage(client)
            )
            completion_metadata = {"usage": usage} if usage else None
            if usage:
                result_payload["usage"] = usage
            if mutation_evidence is not None:
                # Keep the provider-side finalization failure observable while
                # allowing the already-proven mutation to complete the run.
                # Tool-level failed records are persisted above and remain
                # available to the audit/UI timeline.
                result_payload["authoritative_mutation"] = mutation_evidence
                if run_state is not None and run_state[2]:
                    result_payload["provider_finalization_failure"] = (
                        sanitize_durable_error_text(run_state[2])
                    )
            failure_message = (
                run_state[2]
                if run_state is not None and run_state[2]
                else _agent_run_completion_failure_message(
                    user_input=self._user_input,
                    reply=reply,
                    search_tool_result_count=len(self._search_tool_results),
                    successful_mutation_tool_count=successful_mutation_tool_count,
                    completion_confirmed=completion_confirmed,
                    tool_rounds_exhausted=_client_tool_rounds_exhausted(client),
                    tool_loop_failed=_client_tool_loop_failed(client),
                )
            )
            if mutation_evidence is not None:
                failure_message = None
            if failure_message:
                failure_kwargs = {"result": result_payload}
                if completion_metadata:
                    failure_kwargs["metadata"] = completion_metadata
                terminal_result = await self._agent_run_service.fail_run(
                    self._agent_run_id,
                    failure_message,
                    **failure_kwargs,
                )
            else:
                completion_kwargs = {
                    "result": result_payload,
                    "message": (
                        "Assistant generation completed from authoritative mutation evidence"
                        if mutation_evidence is not None
                        else "Assistant generation completed"
                    ),
                }
                if completion_metadata:
                    completion_kwargs["metadata"] = completion_metadata
                terminal_result = await self._agent_run_service.complete_run(
                    self._agent_run_id,
                    **completion_kwargs,
                )
            if terminal_result is None:
                raise RuntimeError("AgentRun terminal state was not persisted")
            if run_state is not None:
                await _ack_client_agent_run_state(client, self._agent_run_id)
            self.finished = True
        except Exception as exc:
            print(f"[{self._log_prefix}] AgentRun completion update failed: {exc}")

    async def fail(
        self,
        error_text: str,
        reply: Optional[str] = None,
        client=None,
        *,
        status: str = "failed",
    ) -> None:
        if self.finished:
            return
        run_state = await _peek_client_agent_run_state(client, self._agent_run_id)
        if not self._agent_run_service:
            await _discard_client_generation_run(client, self._agent_run_id)
            self.finished = True
            return
        try:
            tool_calls = (
                run_state[0]
                if run_state is not None
                else _client_tool_calls(client)
            )
            await self._persist_tool_calls(tool_calls)

            result = _agent_run_completion_result(
                reply=reply,
                search_tool_results=[],
                tool_calls=tool_calls,
            )
            context_snapshot = _client_context_snapshot(client)
            if context_snapshot:
                result["context_snapshot"] = context_snapshot
            context_manifest_ref = _client_context_manifest_ref(client)
            if context_manifest_ref:
                result["context_manifest"] = context_manifest_ref
            usage = (
                run_state[1]
                if run_state is not None
                else _client_agent_run_usage(client)
            )
            failure_metadata = {"usage": usage} if usage else None
            if usage:
                result["usage"] = usage
            failure_kwargs = {"result": result}
            if failure_metadata:
                failure_kwargs["metadata"] = failure_metadata
            safe_status = status if status in {"failed", "cancelled"} else "failed"
            terminal_result = await self._agent_run_service.fail_run(
                self._agent_run_id,
                (run_state[2] if run_state is not None and run_state[2] else error_text),
                status=safe_status,
                **failure_kwargs,
            )
            if terminal_result is None:
                raise RuntimeError("AgentRun failure state was not persisted")
            if run_state is not None:
                await _ack_client_agent_run_state(client, self._agent_run_id)
            else:
                await _discard_client_generation_run(client, self._agent_run_id)
            self.finished = True
        except Exception as exc:
            print(f"[{self._log_prefix}] AgentRun failure update failed: {exc}")
