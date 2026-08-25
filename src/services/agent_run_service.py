"""Service layer for durable agent run tracking."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from contextvars import ContextVar, Token
from datetime import datetime, timedelta
from typing import Any, Dict

from sqlalchemy import and_, case, delete, desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..memory.database import get_database_manager
from ..memory.models import (
    AgentRun,
    AgentRunEdge,
    AgentRunEvent,
    AgentRunToolCall,
    ConversationDispatchOutbox,
    ConversationMessage,
    ConversationSession,
)
from .agent_team_v3 import AGENT_TEAM_SUBAGENT_CATALOG
from .agent_resource_mutations import (
    DOCS_MUTATION_OPERATIONS,
    TASK_MUTATION_OPERATIONS,
    build_agent_resource_mutations,
)
from ..utils.uuid_utils import parse_uuid

logger = logging.getLogger(__name__)

RUN_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
_current_agent_run_id: ContextVar[str | None] = ContextVar(
    "aoitalk_current_agent_run_id",
    default=None,
)
_DISPATCH_PROCESS_ID = str(uuid.uuid4())
MAX_CLIENT_MESSAGE_ID_LENGTH = 512
DISPATCH_OUTBOX_RETENTION_SECONDS = 7 * 24 * 60 * 60
_AGENT_RUN_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "total_tokens",
)
_RESOURCE_MUTATION_TOOL_NAMES = frozenset(
    (*TASK_MUTATION_OPERATIONS, *DOCS_MUTATION_OPERATIONS)
)


def _monotonic_activity(value):
    """Advance a session activity marker without regressing concurrent work."""
    return case(
        (ConversationSession.last_activity.is_(None), value),
        (ConversationSession.last_activity < value, value),
        else_=ConversationSession.last_activity,
    )


class DispatchConflictError(ValueError):
    """An idempotency key was reused by another principal or request."""


def _mutation_confirmation_for_tool(
    tool_name: str,
    requested: bool,
    success: bool,
) -> bool:
    """Classify durable mutation tools even when stream results omit the flag.

    Some live tool streams do not include ``mutation_confirmed`` in their
    result payload. The tool name is the authoritative audit classification;
    success is persisted separately and still gates card extraction.
    """

    normalized_name = str(tool_name or "").rsplit(".", 1)[-1].strip().lower()
    return bool(requested) or (
        bool(success) and normalized_name in _RESOURCE_MUTATION_TOOL_NAMES
    )


def _normalized_agent_run_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    usage: dict[str, int] = {}
    for key in _AGENT_RUN_USAGE_FIELDS:
        raw = value.get(key)
        if raw is None:
            continue
        try:
            usage[key] = max(0, int(raw))
        except (TypeError, ValueError):
            continue
    if not usage:
        return None
    usage.setdefault(
        "total_tokens",
        usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
    )
    return usage


def _merge_agent_run_usage(
    current: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> dict[str, int]:
    left = _normalized_agent_run_usage(current) or {}
    right = _normalized_agent_run_usage(incoming) or {}
    return {
        key: int(left.get(key, 0)) + int(right.get(key, 0))
        for key in _AGENT_RUN_USAGE_FIELDS
    }


def dispatch_client_message_key(client_message_id: str) -> str:
    """Normalize a bounded client id into a fixed-width DB key."""
    normalized = str(client_message_id or "").strip()
    if not normalized:
        raise ValueError("client_message_id is required")
    if len(normalized) > MAX_CLIENT_MESSAGE_ID_LENGTH:
        raise ValueError("client_message_id is too long")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

AGENT_TEAM_TOOL_SUBAGENTS = {
    "agent_team_delegate": "agent_team",
}

# Read-only history compatibility.  These names are never registered by the
# runtime; they only let old AgentRun rows retain a useful actor label.
_LEGACY_HISTORY_TOOL_ACTORS = {
    "writing_assistant": ("agent_team", "story_writer", "執筆"),
    "import_assistant": ("agent_team", "story_import", "Story取り込み"),
    "utility_assistant": ("integration", "utility", "補助機能"),
    "spotify_assistant": ("integration", "spotify", "Spotify連携"),
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

DIRECT_TOOL_LABELS = {
    "web_search": "Web検索",
    "search_web": "Web検索",
    "shell_command": "シェルコマンド",
    "deep_research": "Deep Research",
    "get_weather": "天気",
    "get_weather_info": "天気",
    "get_current_time": "現在時刻",
    "calculate": "計算",
    "create_task": "タスク作成",
    "update_task": "タスク更新",
    "list_tasks": "タスク取得",
    "list_project_information": "案件情報参照",
    "get_project_context": "案件コンテキスト参照",
    "list_record_tables": "台帳参照",
    "read_file": "ファイル読み取り",
    "write_file": "ファイル書き込み",
    "execute_code": "コード実行",
    "generate_image": "画像生成",
    "media_assistant": "メディア連携",
}

SENSITIVE_TOOL_RESULT_NAMES = {
    "webex_get_thread",
    "webex_search_messages",
}
SENSITIVE_TOOL_RESULT_MARKER = (
    "[Webexメッセージ本文は一時利用のため実行履歴へ保存しません]"
)


def set_current_agent_run_id(run_id: str | None) -> Token:
    return _current_agent_run_id.set(str(run_id) if run_id else None)


def reset_current_agent_run_id(token: Token) -> None:
    _current_agent_run_id.reset(token)


def get_current_agent_run_id() -> str | None:
    return _current_agent_run_id.get()


def _jsonable(value: Any) -> Any:
    if value is None:
        return {}
    try:
        return json.loads(json.dumps(value, default=str, ensure_ascii=False))
    except Exception:
        return {"value": str(value)}


def conversation_dispatch_fingerprint(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(
        _jsonable(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _redact_sensitive_tool_data(value: Any) -> Any:
    """Remove transient private tool output before durable AgentRun storage."""

    if isinstance(value, list):
        return [_redact_sensitive_tool_data(item) for item in value]
    if not isinstance(value, dict):
        return value

    redacted = {
        str(key): _redact_sensitive_tool_data(item)
        for key, item in value.items()
    }
    tool_result = redacted.get("tool_result")
    tool_name = _clean_tool_name(
        redacted.get("tool")
        or redacted.get("tool_name")
        or redacted.get("name")
    )
    if not tool_name and isinstance(tool_result, dict):
        tool_name = _clean_tool_name(
            tool_result.get("tool")
            or tool_result.get("tool_name")
            or tool_result.get("name")
        )
    if tool_name not in SENSITIVE_TOOL_RESULT_NAMES:
        return redacted

    for key in ("output", "result", "stderr"):
        if key in redacted:
            redacted[key] = SENSITIVE_TOOL_RESULT_MARKER
    if isinstance(tool_result, dict):
        for key in ("output", "result", "stderr"):
            if key in tool_result:
                tool_result[key] = SENSITIVE_TOOL_RESULT_MARKER
    return redacted


def _durable_tool_result(tool_name: str, result: Any) -> Any:
    if _clean_tool_name(tool_name) in SENSITIVE_TOOL_RESULT_NAMES:
        return SENSITIVE_TOOL_RESULT_MARKER
    return result


def redact_sensitive_model_transcript(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Redact transient tool bodies from provider transcripts before persistence."""

    tool_names_by_call_id: dict[str, str] = {}
    redacted_messages: list[dict[str, Any]] = []
    for message in messages:
        next_message = dict(message)
        tool_calls = next_message.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                function_name = (
                    function.get("name")
                    if isinstance(function, dict)
                    else None
                )
                call_id = str(call.get("id") or call.get("tool_call_id") or "")
                tool_name = _clean_tool_name(
                    function_name or call.get("name") or call.get("tool")
                )
                if call_id and tool_name:
                    tool_names_by_call_id[call_id] = tool_name

        if next_message.get("role") == "tool":
            call_id = str(next_message.get("tool_call_id") or "")
            tool_name = _clean_tool_name(
                next_message.get("name")
                or next_message.get("tool")
                or tool_names_by_call_id.get(call_id)
            )
            if tool_name in SENSITIVE_TOOL_RESULT_NAMES:
                next_message["content"] = SENSITIVE_TOOL_RESULT_MARKER
                for key in ("output", "result"):
                    if key in next_message:
                        next_message[key] = SENSITIVE_TOOL_RESULT_MARKER
        redacted_messages.append(next_message)
    return redacted_messages


def redact_sensitive_chat_metadata(
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Apply transcript redaction at the final conversation persistence boundary."""

    redacted = dict(metadata)
    transcript = redacted.get("model_transcript")
    if isinstance(transcript, list):
        redacted["model_transcript"] = redact_sensitive_model_transcript(
            [
                dict(message)
                for message in transcript
                if isinstance(message, dict)
            ]
        )
    return redacted


def _clip(text: Any, max_chars: int = 20000) -> str | None:
    if text is None:
        return None
    value = str(text)
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + "\n... (truncated)"


def fold_cancelled_chat_snapshot(
    events: list[AgentRunEvent],
) -> dict[str, Any]:
    """Fold the latest streamed attempt into a durable cancelled-turn snapshot."""

    content_parts: list[str] = []
    tool_results: list[dict[str, Any]] = []
    stream_start_sequence: int | None = None
    last_stream_token_sequence: int | None = None

    for event in sorted(events, key=lambda item: int(item.sequence or 0)):
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.event_type == "stream.stream_start":
            content_parts = []
            tool_results = []
            stream_start_sequence = int(event.sequence or 0)
            last_stream_token_sequence = None
            continue
        if event.event_type == "stream.stream_token":
            content = payload.get("content")
            if isinstance(content, str) and content:
                content_parts.append(content)
                last_stream_token_sequence = int(event.sequence or 0)
            continue
        if event.event_type == "stream.stream_end":
            content = payload.get("content")
            if isinstance(content, str) and content:
                content_parts = [content]
            continue
        if event.event_type != "stream.tool_end":
            continue
        tool_result = payload.get("tool_result")
        if isinstance(tool_result, dict):
            tool_results.append(
                {
                    str(key): _jsonable(value)
                    for key, value in tool_result.items()
                    if key != "tool_call_id"
                }
            )

    return {
        "content": "".join(content_parts),
        "tool_results": tool_results,
        "stream_start_sequence": stream_start_sequence,
        "last_stream_token_sequence": last_stream_token_sequence,
    }


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _clean_tool_name(value: Any) -> str:
    return str(value or "").strip()


def _looks_like_shell_command(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    lower = text.lower()
    shell_markers = (
        "powershell.exe",
        "\\pwsh.exe",
        "/pwsh",
        "cmd.exe",
        " -command ",
        " -command'",
        " -command\"",
        " /c ",
        " -c ",
    )
    return any(marker in lower for marker in shell_markers)


def _normalize_tool_name(value: Any) -> str:
    clean_name = _clean_tool_name(value)
    if _looks_like_shell_command(clean_name):
        return "shell_command"
    return clean_name


def _payload_shell_command(payload: dict[str, Any]) -> str:
    tool_args = payload.get("tool_args")
    if isinstance(tool_args, dict):
        command = tool_args.get("command")
        if isinstance(command, str) and _looks_like_shell_command(command):
            return command.strip()

    tool_result = payload.get("tool_result")
    if isinstance(tool_result, dict):
        arguments = tool_result.get("arguments")
        if isinstance(arguments, dict):
            command = arguments.get("command")
            if isinstance(command, str) and _looks_like_shell_command(command):
                return command.strip()
    return ""


def _payload_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _model_text(payload: dict[str, Any], *keys: str) -> str:
    value = _payload_text(payload, *keys)
    if value.strip().lower() == "default":
        return ""
    return value


def _humanize_key(value: str) -> str:
    return value.replace("_", " ").strip() or "ツール"


TOOL_OPERATION_LABELS = {
    "web_search": "Webを検索",
    "search_web": "Webを検索",
    "shell_command": "コマンドを実行",
    "get_weather": "天気を確認",
    "get_current_time": "現在時刻を確認",
    "calculate": "計算を実行",
    "create_task": "タスクを作成",
    "update_task": "タスクを更新",
    "list_tasks": "タスクを確認",
    "list_project_information": "案件情報を確認",
    "get_project_context": "案件コンテキストを確認",
    "list_record_tables": "台帳を確認",
    "read_file": "ファイルを読み取り",
    "write_file": "ファイルを編集",
    "execute_code": "コードを実行",
    "generate_image": "画像を生成",
}


def _tool_operation_label(tool_name: str, actor_label: str | None = None) -> str:
    return TOOL_OPERATION_LABELS.get(
        tool_name,
        f"{actor_label or _humanize_key(tool_name)}を実行",
    )


def _event_operation_key(event: AgentRunEvent) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    # Newer emitters put the correlation id on the event itself, while some
    # provider adapters only have room for it in the durable ``tool_result``
    # object.  Read both shapes (and the two historical call containers) so a
    # start/end pair does not depend on which adapter produced the event.
    sources: list[tuple[dict[str, Any], bool]] = [(payload, False)]
    for key in ("tool_result", "tool_call", "call"):
        value = payload.get(key)
        if isinstance(value, dict):
            sources.append((value, key in {"tool_call", "call"}))
    for source, include_generic_id in sources:
        keys = (
            "operation_id",
            "tool_call_id",
            "call_id",
            "agent_instance_key",
            "actor_instance_key",
        )
        if include_generic_id:
            keys = (*keys, "id")
        key = _payload_text(
            source,
            *keys,
        )
        if key:
            return key
    return ""


def _event_tool_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("tool_args", "arguments", "args"):
        value = payload.get(key)
        if isinstance(value, dict):
            return _jsonable(value)
    tool_result = payload.get("tool_result")
    if isinstance(tool_result, dict):
        for key in ("arguments", "args"):
            value = tool_result.get(key)
            if isinstance(value, dict):
                return _jsonable(value)
    return {}


def _event_tool_result(payload: dict[str, Any]) -> str | None:
    tool_result = payload.get("tool_result")
    if not isinstance(tool_result, dict):
        return None
    for key in ("output", "result"):
        value = tool_result.get(key)
        if value is not None:
            return _clip(value)
    return None


_TOOL_SUCCESS_STATUSES = frozenset(
    {"success", "succeeded", "complete", "completed", "done", "ok"}
)
_TOOL_FAILURE_STATUSES = frozenset(
    {"failed", "failure", "error", "errored", "cancelled", "canceled", "aborted"}
)


def _tool_status_value(value: Any) -> bool | None:
    """Normalize the status variants emitted by provider/tool adapters."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 0:
            return False
        if value == 1:
            return True
    normalized = str(value or "").strip().lower()
    if normalized in _TOOL_SUCCESS_STATUSES or normalized in {"true", "yes", "1"}:
        return True
    if normalized in _TOOL_FAILURE_STATUSES or normalized in {"false", "no", "0"}:
        return False
    return None


def _tool_payload_outcome(
    payload: dict[str, Any] | None,
    *,
    event_status: Any = None,
    completed: bool = False,
) -> tuple[bool | None, str | None, str | None, bool]:
    """Return ``(success, error, result, explicit)`` for one tool payload.

    ``explicit`` distinguishes a provider's status/error from the historical
    shape where a ``stream.tool_end`` merely meant "the operation ended".
    This lets a durable ToolCall remain authoritative when old end events have
    no success field, while still honoring a concrete tool error/status.
    """

    source = payload if isinstance(payload, dict) else {}
    tool_result = source.get("tool_result")
    result_payload = tool_result if isinstance(tool_result, dict) else source
    result = None
    for key in ("output", "result"):
        value = result_payload.get(key)
        if value is not None:
            result = _clip(value)
            break

    error = _event_tool_error(source)
    if error:
        return False, error, result, True

    status_values: list[Any] = [event_status, source.get("status"), source.get("state")]
    for candidate in (tool_result,):
        if isinstance(candidate, dict):
            status_values.extend([candidate.get("status"), candidate.get("state")])
    for value in status_values:
        status = _tool_status_value(value)
        if status is not None:
            return status, None, result, True

    bool_values: list[Any] = []
    for candidate in (source, tool_result):
        if not isinstance(candidate, dict):
            continue
        bool_values.extend(
            candidate.get(key)
            for key in ("success", "successful", "ok", "succeeded")
            if key in candidate
        )
    for value in bool_values:
        status = _tool_status_value(value)
        if status is not None:
            return status, None, result, True

    # A non-zero process/HTTP exit code is a tool failure even when the
    # adapter omitted the separate ``error`` field.
    for candidate in (source, tool_result):
        if not isinstance(candidate, dict):
            continue
        for key in ("exit_code", "returncode", "exit_status"):
            if key not in candidate or candidate[key] is None:
                continue
            try:
                code = int(candidate[key])
            except (TypeError, ValueError):
                continue
            return (code == 0), None, result, True

    if completed:
        # Legacy ``stream.tool_end`` records had no status but did indicate a
        # completed invocation.  Preserve that successful-history behavior.
        return True, None, result, False
    return None, None, result, False


def _event_tool_error(payload: dict[str, Any]) -> str | None:
    tool_result = payload.get("tool_result")
    if isinstance(tool_result, dict) and tool_result.get("error"):
        return _clip(tool_result["error"], max_chars=4000)
    if payload.get("error"):
        return _clip(payload["error"], max_chars=4000)
    return None


def _event_tool_name(event_type: str, payload: dict[str, Any]) -> str:
    tool_name = _clean_tool_name(
        payload.get("tool")
        or payload.get("tool_name")
        or payload.get("name")
    )
    if tool_name:
        return tool_name

    tool_result = payload.get("tool_result")
    if isinstance(tool_result, dict):
        return _clean_tool_name(
            tool_result.get("tool")
            or tool_result.get("tool_name")
            or tool_result.get("name")
        )

    if event_type.startswith("tool."):
        return _clean_tool_name(payload.get("tool_name"))
    return ""


def _actor_for_tool(tool_name: str) -> dict[str, str | None]:
    clean_name = _normalize_tool_name(tool_name)
    legacy = _LEGACY_HISTORY_TOOL_ACTORS.get(clean_name)
    if legacy:
        actor_type, actor_key, actor_label = legacy
        return {
            "actor_type": actor_type,
            "actor_key": actor_key,
            "actor_label": actor_label,
        }
    if clean_name in _SPOTIFY_TOOL_NAMES:
        return {
            "actor_type": "integration",
            "actor_key": "spotify",
            "actor_label": "Spotify連携",
        }
    subagent_id = AGENT_TEAM_TOOL_SUBAGENTS.get(clean_name)
    if subagent_id:
        return {
            "actor_type": "agent_team",
            "actor_key": subagent_id,
            "actor_label": AGENT_TEAM_SUBAGENT_CATALOG.get(subagent_id, {}).get("name", subagent_id),
        }
    if clean_name:
        return {
            "actor_type": "tool",
            "actor_key": clean_name,
            "actor_label": DIRECT_TOOL_LABELS.get(
                clean_name,
                _humanize_key(clean_name),
            ),
        }
    return {
        "actor_type": "assistant",
        "actor_key": "main",
        "actor_label": "メインエージェント",
    }


def _actor_for_event(run: AgentRun, event: AgentRunEvent) -> dict[str, str | None]:
    payload = event.payload if isinstance(event.payload, dict) else {}
    actor_key = _clean_tool_name(
        payload.get("agent_instance_key")
        or payload.get("actor_instance_key")
        or payload.get("subagent_id")
        or payload.get("agent_member_key")
        or payload.get("actor_key")
    )
    if actor_key:
        return {
            "actor_type": str(payload.get("actor_type") or "agent_team"),
            "actor_key": actor_key,
            "actor_label": str(
                payload.get("agent_label")
                or payload.get("actor_label")
                or AGENT_TEAM_SUBAGENT_CATALOG.get(actor_key, {}).get("name", actor_key)
            ),
        }

    tool_name = _event_tool_name(event.event_type, payload)
    if tool_name:
        return _actor_for_tool(tool_name)

    if event.event_type.startswith("run.queued"):
        return {
            "actor_type": "system",
            "actor_key": "system",
            "actor_label": "システム",
        }
    return {
        "actor_type": "assistant",
        "actor_key": "main",
        "actor_label": "メインエージェント",
    }


def _event_action(event_type: str, tool_label: str | None = None) -> str:
    if event_type == "run.queued":
        return "実行をキューに追加"
    if event_type == "run.started":
        return "応答生成を開始"
    if event_type == "run.succeeded":
        return "応答生成を完了"
    if event_type == "run.failed":
        return "応答生成に失敗"
    if event_type == "run.cancelled":
        return "応答生成を停止"
    if event_type.endswith(".ignored"):
        return "状態更新を無視"
    if event_type == "stream.stream_start":
        return "ストリームを開始"
    if event_type == "stream.tool_start":
        return f"{tool_label or 'ツール'}を実行開始"
    if event_type == "stream.tool_end":
        return f"{tool_label or 'ツール'}の実行完了"
    if event_type == "stream.stream_end":
        return "ストリームを完了"
    if event_type == "stream.stream_cancelled":
        return "ストリームを停止"
    if event_type == "stream.assistant_text":
        return "途中経過"
    if event_type == "stream.thinking":
        return "思考"
    if event_type == "stream.reasoning_progress":
        return "推論状況を更新"
    if event_type == "stream.status_update":
        return "進捗を更新"
    if event_type == "stream.steering_update":
        return "追加指示を受信"
    if event_type == "agent_team.instance_started":
        return f"{tool_label or 'エージェント'}を実行開始"
    if event_type == "agent_team.instance_succeeded":
        return f"{tool_label or 'エージェント'}の実行完了"
    if event_type == "agent_team.instance_failed":
        return f"{tool_label or 'エージェント'}の実行に失敗"
    if event_type == "director.round_started":
        return "Directorへ送信"
    if event_type in {"director.raw_reply", "director.reply_received"}:
        return "Directorから受信"
    if event_type == "director.final_answer":
        return "Directorが最終回答を作成"
    if event_type == "director.round_limit_reached":
        return "Director往復上限で停止"
    if event_type == "director.session_save_failed":
        return "Director会話情報の保存に失敗"
    if event_type == "director.needs_human":
        return "ChatGPT接続の確認が必要"
    if event_type == "director.busy":
        return "ChatGPT接続が使用中"
    if event_type == "director.operator_started":
        return "Operatorを実行開始"
    if event_type == "director.operator_succeeded":
        return "Operatorの実行完了"
    if event_type == "director.operator_failed":
        return "Operatorの実行に失敗"
    if event_type == "tool.end":
        return f"{tool_label or 'ツール'}の結果を記録"
    if event_type == "tool.failed":
        return f"{tool_label or 'ツール'}の失敗を記録"
    return _humanize_key(event_type)


def _timeline_event_display_status(event: AgentRunEvent) -> str | None:
    if event.event_type == "run.queued":
        return "recorded"
    if event.event_type == "run.started":
        return "started"
    if event.status in {"queued", "running", "tool"}:
        if event.event_type.endswith("_start") or event.event_type.endswith(".started"):
            return "started"
        return "recorded"
    return event.status


def _timeline_event_message(
    message: str | None,
    *,
    raw_tool_name: str,
    tool_name: str,
) -> str | None:
    if (
        message
        and tool_name == "shell_command"
        and raw_tool_name
        and raw_tool_name in message
    ):
        return message.replace(raw_tool_name, tool_name)
    return message


def _timeline_event_visibility(
    event_type: str,
    payload: dict[str, Any],
) -> str:
    """Separate concise user-facing progress from the complete audit trail."""

    if event_type == "stream.assistant_text":
        return "normal"
    if event_type == "stream.thinking":
        kind = str(payload.get("kind") or "").strip().lower()
        if (
            kind in {"summary", "reasoning_summary"}
            or payload.get("is_summary") is True
            or payload.get("reasoning_summary") is True
        ):
            return "normal"
        return "audit"

    explicit = str(
        payload.get("visibility")
        or payload.get("display_kind")
        or ""
    ).strip().lower()
    if explicit in {"normal", "audit"}:
        return explicit
    if payload.get("user_visible") is True:
        return "normal"

    # Only normalized, actionable summaries are promoted.  Provider JSONL,
    # turn/session/item lifecycle and unknown CLI statuses remain audit-only.
    if event_type in {
        "director.needs_human",
        "director.busy",
        "director.operator_started",
        "director.operator_succeeded",
        "director.operator_failed",
        "director.final_answer",
        "stream.agentic_review",
    }:
        return "normal"
    if event_type.startswith("agent_team.instance_"):
        return "normal"
    return "audit"


def _timeline_event_item(run: AgentRun, event: AgentRunEvent) -> dict[str, Any]:
    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.event_type == "stream.thinking" and (
        str(payload.get("kind") or "").strip().lower() == "reasoning_summary"
        or payload.get("is_summary") is True
        or payload.get("reasoning_summary") is True
    ):
        payload = {**payload, "kind": "summary"}
    raw_tool_name = _event_tool_name(event.event_type, payload)
    tool_name = _normalize_tool_name(raw_tool_name)
    raw_display_tool_name = (
        raw_tool_name
        if raw_tool_name and raw_tool_name != tool_name
        else _payload_shell_command(payload)
    )
    actor = _actor_for_event(run, event)
    provider = _payload_text(payload, "provider", "agent_provider", "model_provider")
    model = _model_text(payload, "model", "agent_model", "model_name")
    if actor["actor_key"] == "main":
        provider = provider or run.provider or ""
        model = model or _model_text({"model": run.model}, "model")
    tool_label = (
        str(actor["actor_label"])
        if tool_name and actor.get("actor_label")
        else None
    )
    if event.event_type.startswith("agent_team.") and actor.get("actor_label"):
        tool_label = str(actor["actor_label"])
    item: dict[str, Any] = {
        "id": f"event:{event.id}",
        "source": "event",
        "run_id": str(event.run_id),
        "sequence": event.sequence,
        "event_type": event.event_type,
        "visibility": _timeline_event_visibility(event.event_type, payload),
        "status": event.status,
        "display_status": _timeline_event_display_status(event),
        "actor_type": actor["actor_type"],
        "actor_key": actor["actor_key"],
        "actor_label": actor["actor_label"],
        "provider": provider or None,
        "model": model or None,
        "mode": _payload_text(
            payload, "mode", "model_mode", "reasoning_effort", "effort"
        ) or None,
        "team_id": _payload_text(payload, "team_id") or None,
        "subagent_id": _payload_text(payload, "subagent_id", "agent_member_key") or None,
        "llm_profile_id": _payload_text(payload, "llm_profile_id") or None,
        "routing_profile": _payload_text(
            payload, "routing_profile", "routing_profile_id"
        )
        or None,
        "pool": _payload_text(payload, "pool", "pool_id") or None,
        "credential_profile": _payload_text(
            payload, "credential_profile", "credential_profile_id"
        )
        or None,
        "candidate": _payload_text(payload, "candidate", "candidate_id") or None,
        "quota_pool_ids": list(payload.get("quota_pool_ids") or []),
        "fallback_count": int(payload.get("fallback_count") or 0),
        "action": _event_action(event.event_type, tool_label),
        "message": _timeline_event_message(
            event.message,
            raw_tool_name=raw_display_tool_name,
            tool_name=tool_name,
        ),
        "tool_name": tool_name or None,
        "raw_tool_name": (
            raw_display_tool_name if raw_display_tool_name else None
        ),
        "tool_call_id": _event_operation_key(event) or None,
        "arguments": _event_tool_arguments(payload),
        "result": _event_tool_result(payload),
        "result_preview": _clip(_event_tool_result(payload), max_chars=1200),
        "error": _event_tool_error(payload),
        "payload": _jsonable(payload),
        "created_at": _dt(event.created_at),
    }
    return item


def _timeline_tool_call_item(
    run: AgentRun,
    tool_call: AgentRunToolCall,
    *,
    operation_id: str | None = None,
    operation_started_at: datetime | None = None,
    operation_ended_at: datetime | None = None,
    operation_end_payload: dict[str, Any] | None = None,
    operation_end_status: Any = None,
) -> dict[str, Any]:
    raw_tool_name = _clean_tool_name(tool_call.tool_name)
    tool_name = _normalize_tool_name(raw_tool_name)
    actor = _actor_for_tool(tool_name)
    metadata = tool_call.result_metadata if isinstance(tool_call.result_metadata, dict) else {}
    arguments = tool_call.arguments or {}
    if tool_name == "shell_command" and raw_tool_name != tool_name:
        arguments = dict(arguments)
        arguments.setdefault("command", raw_tool_name)
    end_success, end_error, end_result, end_explicit = _tool_payload_outcome(
        operation_end_payload,
        event_status=operation_end_status,
        completed=operation_end_payload is not None,
    )
    durable_success, durable_error, durable_result, durable_explicit = _tool_payload_outcome(
        metadata,
    )
    # Concrete tool evidence wins. A bare legacy end event defers to the
    # durable ToolCall, and the parent run status is deliberately ignored.
    if end_error or (end_explicit and end_success is False):
        success = False
        error = end_error
    elif durable_error or (durable_explicit and durable_success is False):
        success = False
        error = durable_error
    elif end_explicit:
        success = bool(end_success)
        error = None
    elif durable_explicit:
        success = bool(durable_success)
        error = None
    else:
        success = bool(tool_call.success)
        error = None
    # Keep the durable audit result as the primary display value. Older
    # streams may contain a truncated/provider-specific end preview.
    result = _clip(tool_call.result) or durable_result or end_result
    started_at = tool_call.started_at or operation_started_at
    ended_at = tool_call.ended_at or operation_ended_at
    duration_ms = tool_call.duration_ms
    if duration_ms is None and started_at and ended_at and ended_at >= started_at:
        duration_ms = int((ended_at - started_at).total_seconds() * 1000)
    return {
        "id": operation_id or f"tool:{tool_call.id}",
        "source": "tool_call",
        "run_id": str(tool_call.run_id),
        "event_id": str(tool_call.event_id) if tool_call.event_id else None,
        "event_type": "tool_call",
        "visibility": "normal",
        "status": "succeeded" if success else "failed",
        "display_status": "succeeded" if success else "failed",
        "actor_type": actor["actor_type"],
        "actor_key": actor["actor_key"],
        "actor_label": actor["actor_label"],
        "provider": _payload_text(
            metadata,
            "provider",
            "agent_provider",
            "model_provider",
        )
        or run.provider
        or None,
        "model": _model_text(metadata, "model", "agent_model", "model_name")
        or _model_text({"model": run.model}, "model")
        or None,
        "mode": _payload_text(metadata, "mode", "model_mode", "reasoning_effort") or None,
        "team_id": _payload_text(metadata, "team_id") or None,
        "subagent_id": _payload_text(metadata, "subagent_id", "agent_member_key") or None,
        "llm_profile_id": _payload_text(metadata, "llm_profile_id") or None,
        "action": _tool_operation_label(tool_name, actor.get("actor_label")),
        "message": tool_name,
        "tool_name": tool_name,
        "raw_tool_name": raw_tool_name if raw_tool_name != tool_name else None,
        "tool_call_id": tool_call.tool_call_id,
        "arguments": arguments,
        "result": result,
        "result_preview": _clip(result, max_chars=1200),
        "error": error,
        "success": success,
        "mutation_confirmed": bool(tool_call.mutation_confirmed),
        "duration_ms": duration_ms,
        "payload": metadata,
        "created_at": _dt(tool_call.created_at),
        "started_at": _dt(started_at),
        "ended_at": _dt(ended_at),
    }


def build_agent_run_timeline(run: AgentRun) -> list[dict[str, Any]]:
    """Build UI work records, correlating lifecycle events into one operation."""

    items: list[tuple[datetime, int, dict[str, Any]]] = []
    events = sorted(
        list(getattr(run, "events", []) or []),
        key=lambda event: (event.created_at or datetime.min, int(event.sequence or 0)),
    )
    tool_calls = sorted(
        list(getattr(run, "tool_calls", []) or []),
        key=lambda call: call.created_at or call.started_at or datetime.min,
    )

    tool_operations: list[dict[str, Any]] = []
    open_tool_operations: dict[str, list[dict[str, Any]]] = {}
    agent_operations: list[dict[str, Any]] = []
    open_agent_operations: dict[str, list[dict[str, Any]]] = {}
    interrupted_status = run.status if run.status in {"failed", "cancelled"} else None

    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        event_type = event.event_type
        created_at = event.created_at or datetime.min

        if event_type.startswith("agent_team.instance_"):
            actor = _actor_for_event(run, event)
            stable_key = _event_operation_key(event) or str(actor.get("actor_key") or "")
            if event_type == "agent_team.instance_started":
                operation = {"start": event, "end": None, "key": stable_key}
                agent_operations.append(operation)
                open_agent_operations.setdefault(stable_key, []).append(operation)
            else:
                queue = open_agent_operations.get(stable_key, [])
                operation = queue.pop(0) if queue else {
                    "start": None,
                    "end": None,
                    "key": stable_key,
                }
                if not queue:
                    open_agent_operations.pop(stable_key, None)
                if operation not in agent_operations:
                    agent_operations.append(operation)
                operation["end"] = event
            continue

        if event_type in {"stream.tool_start", "stream.tool_end"}:
            raw_tool_name = _event_tool_name(event_type, payload)
            tool_name = _normalize_tool_name(raw_tool_name)
            stable_id = _event_operation_key(event)
            queue_key = f"id:{stable_id}" if stable_id else f"name:{tool_name}"
            if event_type == "stream.tool_start":
                signature = (
                    raw_tool_name
                    if _looks_like_shell_command(raw_tool_name)
                    else _payload_shell_command(payload)
                    or json.dumps(
                        _event_tool_arguments(payload),
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                )
                current_queue = open_tool_operations.get(queue_key, [])
                if not stable_id and current_queue:
                    previous = current_queue[-1]
                    previous_start = previous.get("start")
                    previous_at = previous_start.created_at if previous_start else None
                    is_immediate_duplicate = (
                        previous.get("signature") == signature
                        and previous_at is not None
                        and event.created_at is not None
                        and 0
                        <= (event.created_at - previous_at).total_seconds()
                        <= 0.05
                    )
                    if is_immediate_duplicate:
                        continue
                operation = {
                    "start": event,
                    "end": None,
                    "key": queue_key,
                    "tool_name": tool_name,
                    "signature": signature,
                    "used": False,
                }
                tool_operations.append(operation)
                open_tool_operations.setdefault(queue_key, []).append(operation)
            else:
                queue = open_tool_operations.get(queue_key, [])
                operation = None
                signature = (
                    raw_tool_name
                    if _looks_like_shell_command(raw_tool_name)
                    else _payload_shell_command(payload)
                    or json.dumps(
                        _event_tool_arguments(payload),
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                )
                if not stable_id:
                    matching_index = next(
                        (
                            index
                            for index, candidate in enumerate(queue)
                            if candidate.get("signature") == signature
                        ),
                        None,
                    )
                    if matching_index is not None:
                        operation = queue.pop(matching_index)
                if operation is None:
                    # Older starts did not carry an id while newer adapters
                    # may put it only on the end/result. Match those records
                    # by the legacy name/signature queue, but never attach an
                    # explicitly-correlated end to a different id.
                    candidates = [
                        candidate
                        for candidate in tool_operations
                        if candidate.get("end") is None
                        and candidate.get("tool_name") == tool_name
                        and str(candidate.get("key") or "").startswith("name:")
                        and candidate.get("signature") == signature
                    ]
                    if stable_id and len(candidates) != 1:
                        candidates = []
                    if not candidates:
                        legacy_candidates = [
                            candidate
                            for candidate in tool_operations
                            if candidate.get("end") is None
                            and candidate.get("tool_name") == tool_name
                            and str(candidate.get("key") or "").startswith("name:")
                        ]
                        # A unique legacy start can safely be paired with a
                        # newly-correlated end even when older history did
                        # not retain its arguments. With multiple candidates,
                        # require the signature to avoid cross-pairing.
                        if not stable_id or len(legacy_candidates) == 1:
                            candidates = legacy_candidates
                    if candidates:
                        candidate_key = str(candidates[0].get("key") or "")
                        queue = open_tool_operations.get(candidate_key, [])
                        queue_key = candidate_key
                        try:
                            operation = queue.pop(queue.index(candidates[0]))
                        except (ValueError, IndexError):
                            operation = candidates[0]
                if operation is None:
                    operation = queue.pop(0) if queue else {
                        "start": None,
                        "end": None,
                        "key": queue_key,
                        "tool_name": tool_name,
                        "used": False,
                    }
                if not queue:
                    open_tool_operations.pop(queue_key, None)
                if operation not in tool_operations:
                    tool_operations.append(operation)
                operation["end"] = event
            continue

        if event_type in {"tool.end", "tool.failed"}:
            # AgentRunToolCall が実内容を保持するため、記録ライフサイクルは表示しない。
            continue

        items.append((created_at, int(event.sequence or 0), _timeline_event_item(run, event)))

    for operation in agent_operations:
        start = operation.get("start")
        end = operation.get("end")
        base_event = end or start
        if base_event is None:
            continue
        item = _timeline_event_item(run, base_event)
        start_payload = start.payload if start and isinstance(start.payload, dict) else {}
        end_payload = end.payload if end and isinstance(end.payload, dict) else {}
        started_at = start.created_at if start else None
        ended_at = end.created_at if end else run.ended_at if interrupted_status else None
        duration_ms = None
        if started_at and ended_at and ended_at >= started_at:
            duration_ms = int((ended_at - started_at).total_seconds() * 1000)
        result = _payload_text(end_payload, "result", "result_preview") or None
        error = _payload_text(end_payload, "error") or (
            str(run.error) if interrupted_status == "failed" and run.error else None
        )
        task = _payload_text(start_payload, "task")
        label = str(item.get("actor_label") or "サブエージェント")
        # 子 run のタイムラインへ辿れるよう、集約 item にも子 run id を残す。
        child_run_id = (
            _payload_text(end_payload, "child_run_id")
            or _payload_text(start_payload, "child_run_id")
            or None
        )
        item.update(
            {
                "id": f"operation:agent:{start.id if start else end.id}",
                "event_type": "agent_operation",
                "visibility": "normal",
                "child_run_id": child_run_id,
                "status": (
                    interrupted_status
                    if end is None and interrupted_status
                    else "running"
                    if end is None
                    else "failed"
                    if error or str(end.status) == "failed"
                    else "succeeded"
                ),
                "display_status": (
                    interrupted_status
                    if end is None and interrupted_status
                    else "started"
                    if end is None
                    else "failed"
                    if error or str(end.status) == "failed"
                    else "succeeded"
                ),
                "action": task or label,
                "message": None,
                "result": _clip(result),
                "result_preview": _clip(result, max_chars=1200),
                "error": error,
                "success": (
                    False
                    if end is None and interrupted_status == "failed"
                    else None
                    if end is None
                    else not bool(error or str(end.status) == "failed")
                ),
                "duration_ms": duration_ms,
                "payload": {**_jsonable(start_payload), **_jsonable(end_payload)},
                "created_at": _dt(started_at or ended_at),
                "started_at": _dt(started_at),
                "ended_at": _dt(ended_at),
            }
        )
        items.append((started_at or ended_at or datetime.min, int(start.sequence if start else end.sequence or 0), item))

    for index, tool_call in enumerate(tool_calls):
        tool_name = _normalize_tool_name(_clean_tool_name(tool_call.tool_name))
        correlation_id = str(tool_call.tool_call_id or "")
        call_arguments = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}
        call_signature = (
            _clean_tool_name(tool_call.tool_name)
            if _looks_like_shell_command(_clean_tool_name(tool_call.tool_name))
            else json.dumps(call_arguments, sort_keys=True, ensure_ascii=False)
        )
        candidates = [
            operation
            for operation in tool_operations
            if not operation["used"]
            and operation["tool_name"] == tool_name
            and (
                operation["key"] == f"id:{correlation_id}"
                if correlation_id
                else operation.get("signature") == call_signature
            )
        ]
        if not candidates and not correlation_id:
            candidates = [
                operation
                for operation in tool_operations
                if not operation["used"] and operation["tool_name"] == tool_name
            ]
        if not candidates and correlation_id:
            candidates = [
                operation
                for operation in tool_operations
                if not operation["used"]
                and operation["tool_name"] == tool_name
                and operation["key"].startswith("name:")
            ]
        operation = candidates[0] if candidates else None
        if operation:
            operation["used"] = True
        start = operation.get("start") if operation else None
        end = operation.get("end") if operation else None
        operation_id = (
            f"operation:tool:{start.id if start else end.id}"
            if start or end
            else None
        )
        created_at = (
            (start.created_at if start else None)
            or tool_call.started_at
            or tool_call.created_at
            or datetime.min
        )
        items.append(
            (
                created_at,
                100000 + index,
                _timeline_tool_call_item(
                    run,
                    tool_call,
                    operation_id=operation_id,
                    operation_started_at=start.created_at if start else None,
                    operation_ended_at=end.created_at if end else None,
                    operation_end_payload=(
                        end.payload if end and isinstance(end.payload, dict) else None
                    ),
                    operation_end_status=end.status if end else None,
                ),
            )
        )

    for operation in tool_operations:
        if operation["used"]:
            continue
        start = operation.get("start")
        end = operation.get("end")
        base_event = end or start
        if base_event is None:
            continue
        item = _timeline_event_item(run, base_event)
        start_payload = start.payload if start and isinstance(start.payload, dict) else {}
        end_payload = end.payload if end and isinstance(end.payload, dict) else {}
        started_at = start.created_at if start else None
        ended_at = end.created_at if end else run.ended_at if interrupted_status else None
        duration_ms = None
        if started_at and ended_at and ended_at >= started_at:
            duration_ms = int((ended_at - started_at).total_seconds() * 1000)
        end_success, end_error, result, _end_explicit = _tool_payload_outcome(
            end_payload,
            event_status=end.status if end else None,
            completed=end is not None,
        )
        if end is None:
            status = interrupted_status or "running"
            display_status = interrupted_status or "started"
            error = (
                str(run.error)
                if interrupted_status == "failed" and run.error
                else None
            )
            success = False if interrupted_status == "failed" else None
        else:
            status = "failed" if end_success is False else "succeeded"
            display_status = status
            error = end_error
            success = False if end_success is False else True
        tool_name = str(operation.get("tool_name") or item.get("tool_name") or "")
        item.update(
            {
                "id": f"operation:tool:{start.id if start else end.id}",
                "event_type": "tool_operation",
                "visibility": "normal",
                "status": status,
                "display_status": display_status,
                "action": _tool_operation_label(tool_name, item.get("actor_label")),
                "message": None,
                "arguments": _event_tool_arguments(start_payload) or _event_tool_arguments(end_payload),
                "result": result,
                "result_preview": _clip(result, max_chars=1200),
                "error": error,
                "success": success,
                "duration_ms": duration_ms,
                "payload": {**_jsonable(start_payload), **_jsonable(end_payload)},
                "created_at": _dt(started_at or ended_at),
                "started_at": _dt(started_at),
                "ended_at": _dt(ended_at),
            }
        )
        items.append((started_at or ended_at or datetime.min, int(start.sequence if start else end.sequence or 0), item))

    return [
        item
        for _created_at, _order, item in sorted(
            items,
            key=lambda row: (row[0], row[1]),
        )
    ]


class AgentRunService:
    """Create and update durable agent execution records."""

    def __init__(self, db_manager: Any | None = None) -> None:
        self._db_manager = db_manager

    def _get_db_manager(self) -> Any:
        return self._db_manager or get_database_manager()

    async def _session(self) -> AsyncSession:
        db_manager = self._get_db_manager()
        return await db_manager.get_session()

    async def create_run(
        self,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        client_message_id: str | None = None,
        request_fingerprint: str | None = None,
        project_id: str | None = None,
        app_id: str | None = None,
        app_target_id: str | None = None,
        base_revision: str | None = None,
        result_revision: str | None = None,
        trigger_message_id: str | None = None,
        objective: str = "",
        run_type: str = "chat_turn",
        generation_profile: str | None = None,
        metadata: Dict[str, Any] | None = None,
        title: str | None = None,
        parent_run_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> Dict[str, Any]:
        session = await self._session()
        try:
            now = datetime.utcnow()
            parent_uuid = parse_uuid(parent_run_id)
            root_uuid = None
            if parent_uuid:
                parent = await session.get(AgentRun, parent_uuid)
                if parent:
                    root_uuid = parent.root_run_id or parent.id
                    # 子runの所有権は呼び出し側の値に関係なく親へ固定する。
                    # 空値や別scopeの明示指定を許すと認可境界を越えられる。
                    session_id = (
                        str(parent.session_id) if parent.session_id else None
                    )
                    user_id = str(parent.user_id) if parent.user_id else None
                    project_id = (
                        str(parent.project_id) if parent.project_id else None
                    )
                    app_id = str(parent.app_id) if parent.app_id else None
                    app_target_id = (
                        str(parent.app_target_id) if parent.app_target_id else None
                    )
                    base_revision = parent.base_revision
                    result_revision = parent.result_revision

            if app_id and not base_revision:
                try:
                    from .app_git_service import AppGitService

                    base_revision = AppGitService().status(app_id).get("revision")
                except Exception:
                    # App workspace may not have been initialized yet; the run
                    # remains durable and the missing revision is explicit.
                    base_revision = None

            run = AgentRun(
                parent_run_id=parent_uuid,
                root_run_id=root_uuid,
                session_id=parse_uuid(session_id),
                trigger_message_id=parse_uuid(trigger_message_id),
                project_id=parse_uuid(project_id),
                app_id=parse_uuid(app_id),
                app_target_id=parse_uuid(app_target_id),
                base_revision=str(base_revision).strip() if base_revision else None,
                result_revision=str(result_revision).strip() if result_revision else None,
                user_id=str(user_id) if user_id else None,
                client_message_id=(
                    str(client_message_id).strip() if client_message_id else None
                ),
                client_message_key=(
                    dispatch_client_message_key(client_message_id)
                    if client_message_id
                    else None
                ),
                request_fingerprint=request_fingerprint,
                run_type=run_type or "chat_turn",
                status="queued",
                title=(title or "")[:255],
                objective=str(objective or ""),
                generation_profile=generation_profile,
                provider=provider,
                model=model,
                result={},
                validation={},
                run_metadata=_jsonable(metadata),
                created_at=now,
                updated_at=now,
                last_event_at=now,
            )
            session.add(run)
            await session.flush()
            if run.root_run_id is None:
                run.root_run_id = run.id
            session.add(
                AgentRunEvent(
                    run_id=run.id,
                    sequence=1,
                    event_type="run.queued",
                    status="queued",
                    message="Agent run queued",
                    payload={},
                    created_at=now,
                )
            )
            await session.commit()
            await session.refresh(run)
            return run.to_dict()
        except Exception as exc:
            await session.rollback()
            if not (client_message_id and isinstance(exc, IntegrityError)):
                logger.exception("Failed to create agent run")
            raise
        finally:
            await session.close()

    async def get_dispatch_run(
        self,
        *,
        session_id: str,
        user_id: str,
        client_message_id: str,
    ) -> Dict[str, Any] | None:
        session_uuid = parse_uuid(session_id)
        normalized_client_id = str(client_message_id or "").strip()
        if session_uuid is None or not normalized_client_id:
            return None
        client_message_key = dispatch_client_message_key(normalized_client_id)
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            return None
        session = await self._session()
        try:
            result = await session.execute(
                select(AgentRun).where(
                    AgentRun.session_id == session_uuid,
                    AgentRun.user_id == normalized_user_id,
                    AgentRun.client_message_key == client_message_key,
                )
            )
            run = result.scalars().first()
            return run.to_dict() if run else None
        finally:
            await session.close()

    async def create_or_get_dispatch_run(
        self,
        *,
        session_id: str,
        user_id: str,
        client_message_id: str,
        **run_kwargs: Any,
    ) -> tuple[Dict[str, Any], bool]:
        """DB unique制約をclaimとして同一mobile dispatchを原子的に一意化する。"""
        normalized_client_id = str(client_message_id or "").strip()
        if not normalized_client_id:
            run = await self.create_run(
                session_id=session_id,
                user_id=user_id,
                **run_kwargs,
            )
            return run, True
        try:
            run = await self.create_run(
                session_id=session_id,
                user_id=user_id,
                client_message_id=normalized_client_id,
                **run_kwargs,
            )
            return run, True
        except IntegrityError:
            # PostgreSQLは競合INSERTのtransaction終了を待ってからunique violationを
            # 返すため、このSELECTはwinnerがcommitした同じrunを取得する。
            existing = await self.get_dispatch_run(
                session_id=session_id,
                user_id=user_id,
                client_message_id=normalized_client_id,
            )
            if existing is None:
                raise
            return existing, False

    async def create_or_get_dispatch_turn(
        self,
        *,
        session_id: str,
        client_message_id: str,
        content: str,
        message_metadata: Dict[str, Any],
        sender_type: str | None,
        sender_id: str | None,
        sender_display_name: str | None,
        edit_message_id: str | None,
        outbox_payload: Dict[str, Any],
        request_fingerprint: str,
        persisted_user_message_id: str | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
        app_id: str | None = None,
        app_target_id: str | None = None,
        base_revision: str | None = None,
        result_revision: str | None = None,
        objective: str = "",
        generation_profile: str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> tuple[Dict[str, Any], str, bool]:
        """Create message, run and durable outbox in one transaction.

        The session row lock serializes branch placement. The unique run/outbox
        constraints remain the cross-process idempotency authority.
        """
        session_uuid = parse_uuid(session_id)
        normalized_client_id = str(client_message_id or "").strip()
        normalized_user_id = str(user_id or "").strip()
        normalized_fingerprint = str(request_fingerprint or "").strip()
        if (
            session_uuid is None
            or not normalized_client_id
            or not normalized_user_id
            or len(normalized_fingerprint) != 64
        ):
            raise ValueError(
                "valid session_id, user_id, client_message_id and fingerprint are required"
            )
        client_message_key = dispatch_client_message_key(normalized_client_id)

        db_session = await self._session()
        try:
            conversation_result = await db_session.execute(
                select(ConversationSession)
                .where(
                    ConversationSession.id == session_uuid,
                    ConversationSession.deleted_at.is_(None),
                )
                .with_for_update()
            )
            conversation = conversation_result.scalars().first()
            if conversation is None:
                raise ValueError("Session not found or deleted")

            existing_result = await db_session.execute(
                select(AgentRun).where(
                    AgentRun.session_id == session_uuid,
                    AgentRun.client_message_key == client_message_key,
                )
            )
            matching_runs = list(existing_result.scalars().all())
            existing = next(
                (
                    run
                    for run in matching_runs
                    if str(run.user_id or "") == normalized_user_id
                ),
                matching_runs[0] if matching_runs else None,
            )
            if existing is not None:
                if str(existing.user_id or "") != normalized_user_id:
                    raise DispatchConflictError(
                        "client_message_id belongs to another principal"
                    )
                if (
                    existing.request_fingerprint
                    and existing.request_fingerprint != normalized_fingerprint
                ):
                    raise DispatchConflictError(
                        "client_message_id was reused with a different request"
                    )
                if existing.trigger_message_id is None:
                    raise RuntimeError("idempotent dispatch run is incomplete")
                await db_session.commit()
                return existing.to_dict(), str(existing.trigger_message_id), False

            message = None
            message_uuid = parse_uuid(persisted_user_message_id)
            if persisted_user_message_id:
                if message_uuid is None:
                    raise ValueError("invalid persisted user message")
                persisted_message = (
                    await db_session.execute(
                        select(ConversationMessage).where(
                            ConversationMessage.id == message_uuid,
                            ConversationMessage.session_id == session_uuid,
                            ConversationMessage.role == "user",
                        )
                    )
                ).scalars().first()
                if persisted_message is None:
                    raise ValueError("persisted user message does not match session")
            else:
                from ..memory.conversation_repository import ConversationRepository

                repository = ConversationRepository(db_session)
                await repository._ensure_linear_parent_links(db_session, session_id)
                parent_message_id = None
                branch_index = 0
                if edit_message_id:
                    edit_uuid = parse_uuid(edit_message_id)
                    original = (
                        await db_session.execute(
                            select(ConversationMessage).where(
                                ConversationMessage.id == edit_uuid,
                                ConversationMessage.session_id == session_uuid,
                                ConversationMessage.role == "user",
                            )
                        )
                    ).scalars().first()
                    if original is None:
                        raise ValueError("edit message does not match session")
                    await repository._deactivate_branch_from_message(
                        db_session,
                        str(original.id),
                    )
                    parent_message_id = original.parent_message_id
                    branch_index = await repository._count_branch_siblings(
                        db_session,
                        session_id,
                        str(parent_message_id) if parent_message_id else None,
                    )
                else:
                    parent = await repository._latest_active_message(
                        db_session,
                        session_id,
                    )
                    parent_message_id = parent.id if parent else None
                    branch_index = await repository._count_branch_siblings(
                        db_session,
                        session_id,
                        str(parent_message_id) if parent_message_id else None,
                    )

                message_uuid = uuid.uuid4()
                message = ConversationMessage(
                    id=message_uuid,
                    session_id=session_uuid,
                    role="user",
                    content=content,
                    parent_message_id=parent_message_id,
                    branch_index=branch_index,
                    is_active_branch=True,
                    message_metadata=_jsonable(message_metadata),
                    sender_type=sender_type,
                    sender_id=sender_id,
                    sender_display_name=sender_display_name,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )

            if app_id and not base_revision:
                try:
                    from .app_git_service import AppGitService

                    base_revision = AppGitService().status(app_id).get("revision")
                except Exception:
                    base_revision = None

            now = datetime.utcnow()
            run_id = uuid.uuid4()
            durable_payload = {
                **outbox_payload,
                "agent_run_id": str(run_id),
                "persisted_user_message_id": str(message_uuid),
                "skip_user_persistence": True,
            }
            run = AgentRun(
                id=run_id,
                root_run_id=run_id,
                session_id=session_uuid,
                trigger_message_id=message_uuid,
                project_id=parse_uuid(project_id),
                app_id=parse_uuid(app_id),
                app_target_id=parse_uuid(app_target_id),
                base_revision=str(base_revision).strip() if base_revision else None,
                result_revision=str(result_revision).strip() if result_revision else None,
                user_id=normalized_user_id,
                client_message_id=normalized_client_id,
                client_message_key=client_message_key,
                request_fingerprint=normalized_fingerprint,
                run_type="chat_turn",
                status="queued",
                title="",
                objective=str(objective or ""),
                generation_profile=generation_profile,
                result={},
                validation={},
                run_metadata=_jsonable(metadata),
                created_at=now,
                updated_at=now,
                last_event_at=now,
            )
            outbox = ConversationDispatchOutbox(
                run_id=run_id,
                session_id=session_uuid,
                user_id=normalized_user_id,
                client_message_id=normalized_client_id,
                client_message_key=client_message_key,
                request_fingerprint=normalized_fingerprint,
                payload=_jsonable(durable_payload),
                status="pending",
                attempts=0,
                created_at=now,
                updated_at=now,
            )
            if message is not None:
                # agent_runs.trigger_message_id は conversation_messages への
                # FK。同一 flush に混ぜると run が先に INSERT されて
                # ForeignKeyViolation になるため、user message を先に確定させる。
                db_session.add(message)
                await db_session.flush()
            db_session.add_all(
                [
                    run,
                    outbox,
                    AgentRunEvent(
                        run_id=run_id,
                        sequence=1,
                        event_type="run.queued",
                        status="queued",
                        message="Agent run queued",
                        payload={},
                        created_at=now,
                    ),
                ]
            )
            if message is not None:
                await db_session.execute(
                    update(ConversationSession)
                    .where(ConversationSession.id == session_uuid)
                    .values(
                        message_count=ConversationSession.message_count + 1,
                        last_activity=_monotonic_activity(now),
                        # App開発チャットに限らず、エージェント実行中は
                        # サイドバーのアイコンを進行中表示へ切り替える。
                        development_status="working",
                    )
                )
            await db_session.commit()
            return run.to_dict(), str(message_uuid), True
        except IntegrityError:
            await db_session.rollback()
            existing = await self.get_dispatch_run(
                session_id=session_id,
                user_id=normalized_user_id,
                client_message_id=normalized_client_id,
            )
            if existing is None or not existing.get("trigger_message_id"):
                raise
            if (
                existing.get("request_fingerprint")
                and existing["request_fingerprint"] != normalized_fingerprint
            ):
                raise DispatchConflictError(
                    "client_message_id was reused with a different request"
                )
            return existing, str(existing["trigger_message_id"]), False
        except Exception:
            await db_session.rollback()
            raise
        finally:
            await db_session.close()

    async def list_recoverable_dispatch_run_ids(
        self,
        *,
        limit: int = 50,
    ) -> list[str]:
        """List pending or expired deliveries; claim remains a separate CAS."""
        now = datetime.utcnow()
        session = await self._session()
        try:
            result = await session.execute(
                select(ConversationDispatchOutbox.run_id)
                .where(
                    or_(
                        ConversationDispatchOutbox.status == "pending",
                        and_(
                            ConversationDispatchOutbox.status == "claimed",
                            ConversationDispatchOutbox.lease_expires_at < now,
                        ),
                    )
                )
                .order_by(ConversationDispatchOutbox.created_at)
                .limit(max(1, min(int(limit), 500)))
            )
            return [str(run_id) for run_id in result.scalars().all()]
        finally:
            await session.close()

    async def purge_delivered_dispatches(
        self,
        *,
        older_than_seconds: float = DISPATCH_OUTBOX_RETENTION_SECONDS,
        limit: int = 500,
    ) -> int:
        """Bound outbox growth without removing AgentRun idempotency records."""
        cutoff = datetime.utcnow() - timedelta(
            seconds=max(0.0, older_than_seconds)
        )
        candidate_run_ids = (
            select(ConversationDispatchOutbox.run_id)
            .where(
                ConversationDispatchOutbox.status == "delivered",
                ConversationDispatchOutbox.delivered_at.is_not(None),
                ConversationDispatchOutbox.delivered_at < cutoff,
            )
            .order_by(ConversationDispatchOutbox.delivered_at)
            .limit(max(1, min(int(limit), 5_000)))
        )
        session = await self._session()
        try:
            result = await session.execute(
                delete(ConversationDispatchOutbox).where(
                    ConversationDispatchOutbox.run_id.in_(candidate_run_ids)
                )
            )
            await session.commit()
            return int(result.rowcount or 0)
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def claim_dispatch_delivery(
        self,
        *,
        run_id: str,
        lease_seconds: float = 5.0,
    ) -> Dict[str, Any] | None:
        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return None
        now = datetime.utcnow()
        lease_token = str(uuid.uuid4())
        session = await self._session()
        try:
            result = await session.execute(
                update(ConversationDispatchOutbox)
                .where(
                    ConversationDispatchOutbox.run_id == run_uuid,
                    or_(
                        ConversationDispatchOutbox.status == "pending",
                        and_(
                            ConversationDispatchOutbox.status == "claimed",
                            ConversationDispatchOutbox.lease_expires_at < now,
                        ),
                    ),
                )
                .values(
                    status="claimed",
                    lease_owner=_DISPATCH_PROCESS_ID,
                    lease_token=lease_token,
                    lease_expires_at=now
                    + timedelta(seconds=max(0.1, lease_seconds)),
                    attempts=ConversationDispatchOutbox.attempts + 1,
                    updated_at=now,
                )
                .returning(ConversationDispatchOutbox.payload)
            )
            payload = result.scalar_one_or_none()
            await session.commit()
            if payload is None:
                return None
            return {
                "lease_token": lease_token,
                "payload": dict(payload or {}),
            }
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def mark_dispatch_delivered(
        self,
        *,
        run_id: str,
        lease_token: str,
    ) -> bool:
        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return False
        now = datetime.utcnow()
        session = await self._session()
        try:
            result = await session.execute(
                update(ConversationDispatchOutbox)
                .where(
                    ConversationDispatchOutbox.run_id == run_uuid,
                    ConversationDispatchOutbox.status == "claimed",
                    ConversationDispatchOutbox.lease_token == lease_token,
                )
                .values(
                    status="delivered",
                    payload={},
                    delivered_at=now,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            await session.commit()
            return bool(result.rowcount)
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def renew_dispatch_delivery(
        self,
        *,
        run_id: str,
        lease_token: str,
        lease_seconds: float = 60.0,
    ) -> bool:
        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return False
        now = datetime.utcnow()
        session = await self._session()
        try:
            result = await session.execute(
                update(ConversationDispatchOutbox)
                .where(
                    ConversationDispatchOutbox.run_id == run_uuid,
                    ConversationDispatchOutbox.status == "claimed",
                    ConversationDispatchOutbox.lease_token == lease_token,
                )
                .values(
                    lease_expires_at=now
                    + timedelta(seconds=max(1.0, lease_seconds)),
                    updated_at=now,
                )
            )
            await session.commit()
            return bool(result.rowcount)
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def release_dispatch_delivery(
        self,
        *,
        run_id: str,
        lease_token: str,
    ) -> bool:
        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return False
        session = await self._session()
        try:
            result = await session.execute(
                update(ConversationDispatchOutbox)
                .where(
                    ConversationDispatchOutbox.run_id == run_uuid,
                    ConversationDispatchOutbox.status == "claimed",
                    ConversationDispatchOutbox.lease_token == lease_token,
                )
                .values(
                    status="pending",
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=datetime.utcnow(),
                )
            )
            await session.commit()
            return bool(result.rowcount)
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def attach_dispatch_message(
        self,
        *,
        run_id: str,
        message_id: str,
    ) -> Dict[str, Any] | None:
        run_uuid = parse_uuid(run_id)
        message_uuid = parse_uuid(message_id)
        if run_uuid is None or message_uuid is None:
            return None
        session = await self._session()
        try:
            await session.execute(
                update(AgentRun)
                .where(AgentRun.id == run_uuid)
                .values(
                    trigger_message_id=message_uuid,
                    updated_at=datetime.utcnow(),
                )
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
        return await self.get_run(str(run_uuid))

    async def update_runtime_route(
        self,
        run_id: str | None,
        *,
        provider: str | None,
        model: str | None,
        route_source: str | None = None,
        reasoning_effort: str | None = None,
    ) -> Dict[str, Any] | None:
        """Correct the route used by a live run without changing its lifecycle.

        The initial ``run.started`` event is emitted before a session-aware
        client can be materialized, so its provider/model may describe the
        process-wide client rather than the target that actually generated the
        response.  This small metadata-only update deliberately does not call
        ``_append_event``: route correction is not a second lifecycle event.
        """

        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return None

        normalized_provider = str(provider or "").strip()
        normalized_model = str(model or "").strip()
        normalized_route_source = str(route_source or "").strip()
        normalized_effort = str(reasoning_effort or "").strip()

        session = await self._session()
        try:
            run = await session.get(AgentRun, run_uuid)
            if run is None:
                return None

            if normalized_provider:
                run.provider = normalized_provider
            if normalized_model:
                run.model = normalized_model

            metadata = (
                dict(run.run_metadata)
                if isinstance(run.run_metadata, dict)
                else {}
            )
            if normalized_route_source:
                metadata["route_source"] = normalized_route_source
            if normalized_effort:
                metadata["reasoning_effort"] = normalized_effort
            run.run_metadata = _jsonable(metadata)
            run.updated_at = datetime.utcnow()

            await session.commit()
            await session.refresh(run)
            return run.to_dict()
        except Exception:
            await session.rollback()
            logger.warning(
                "Failed to update runtime route for agent run: %s",
                run_id,
                exc_info=True,
            )
            return None
        finally:
            await session.close()

    async def wait_for_dispatch_result(
        self,
        *,
        session_id: str,
        user_id: str,
        client_message_id: str,
        timeout_seconds: float = 10.0,
    ) -> Dict[str, Any] | None:
        """並行winnerがmessage IDを紐付けるまで短時間待ち、同じ結果を返す。"""
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        latest: Dict[str, Any] | None = None
        while True:
            latest = await self.get_dispatch_run(
                session_id=session_id,
                user_id=user_id,
                client_message_id=client_message_id,
            )
            if latest is None or latest.get("trigger_message_id"):
                return latest
            if time.monotonic() >= deadline:
                return latest
            await asyncio.sleep(0.02)

    async def get_run(
        self,
        run_id: str,
        *,
        include_events: bool = False,
        include_tool_calls: bool = False,
        include_edges: bool = False,
        include_timeline: bool = False,
    ) -> Dict[str, Any] | None:
        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return None

        session = await self._session()
        try:
            options = []
            if include_events or include_timeline:
                options.append(selectinload(AgentRun.events))
            if include_tool_calls or include_timeline:
                options.append(selectinload(AgentRun.tool_calls))
            if include_edges:
                options.extend(
                    [
                        selectinload(AgentRun.child_edges),
                        selectinload(AgentRun.parent_edges),
                    ]
                )
            stmt = select(AgentRun).where(AgentRun.id == run_uuid)
            if options:
                stmt = stmt.options(*options)
            result = await session.execute(stmt)
            run = result.scalars().first()
            if not run:
                return None
            payload = run.to_dict(
                include_events=include_events,
                include_tool_calls=include_tool_calls,
                include_edges=include_edges,
            )
            if include_timeline:
                payload["resource_mutations"] = build_agent_resource_mutations(
                    run.tool_calls or []
                )
                payload["timeline"] = build_agent_run_timeline(run)
            elif include_tool_calls:
                payload["resource_mutations"] = build_agent_resource_mutations(
                    run.tool_calls or []
                )
            return payload
        finally:
            await session.close()

    async def list_runs(
        self,
        *,
        session_id: str | None = None,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[Dict[str, Any]]:
        safe_limit = min(max(int(limit or 50), 1), 200)
        stmt = select(AgentRun)
        filters = []
        session_uuid = parse_uuid(session_id)
        project_uuid = parse_uuid(project_id)
        if session_id and session_uuid is None:
            return []
        if project_id and project_uuid is None:
            return []
        if session_uuid:
            filters.append(AgentRun.session_id == session_uuid)
        if project_uuid:
            filters.append(AgentRun.project_id == project_uuid)
        if status:
            filters.append(AgentRun.status == str(status))
        if filters:
            stmt = stmt.where(*filters)
        stmt = stmt.order_by(desc(AgentRun.created_at)).limit(safe_limit)

        session = await self._session()
        try:
            result = await session.execute(stmt)
            return [run.to_dict() for run in result.scalars().all()]
        finally:
            await session.close()

    async def record_event(
        self,
        run_id: str | None,
        event_type: str,
        *,
        status: str | None = None,
        message: str | None = None,
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any] | None:
        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return None

        session = await self._session()
        try:
            run = await session.get(AgentRun, run_uuid)
            if not run:
                return None
            safe_payload = _redact_sensitive_tool_data(payload or {})
            event = await self._append_event(
                session,
                run,
                event_type,
                status=status,
                message=message,
                payload=safe_payload,
            )
            usage = _normalized_agent_run_usage(safe_payload.get("usage"))
            usage_key = str(safe_payload.get("usage_key") or "").strip()
            if usage is not None and (
                not usage_key
                or usage_key
                not in set(
                    str(key)
                    for key in (run.run_metadata or {}).get(
                        "_usage_event_keys", []
                    )
                )
            ):
                metadata = dict(run.run_metadata or {})
                metadata["usage"] = _merge_agent_run_usage(
                    metadata.get("usage"),
                    usage,
                )
                if usage_key:
                    usage_keys = [
                        str(key)
                        for key in metadata.get("_usage_event_keys", [])
                        if str(key).strip()
                    ]
                    usage_keys.append(usage_key)
                    metadata["_usage_event_keys"] = usage_keys[-256:]
                run.run_metadata = _jsonable(metadata)
            await session.commit()
            return event.to_dict()
        except Exception:
            await session.rollback()
            logger.exception("Failed to record agent run event: %s", run_id)
            return None
        finally:
            await session.close()

    async def mark_running(
        self,
        run_id: str | None,
        *,
        message: str | None = None,
        metadata: Dict[str, Any] | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> Dict[str, Any] | None:
        return await self._set_status(
            run_id,
            "running",
            "run.started",
            message=message or "Agent run started",
            metadata=metadata,
            provider=provider,
            model=model,
            started=True,
        )

    async def complete_run(
        self,
        run_id: str | None,
        *,
        result: Dict[str, Any] | None = None,
        message: str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any] | None:
        return await self._set_status(
            run_id,
            "succeeded",
            "run.succeeded",
            message=message or "Agent run completed",
            result=result,
            metadata=metadata,
            ended=True,
        )

    async def fail_run(
        self,
        run_id: str | None,
        error: str,
        *,
        result: Dict[str, Any] | None = None,
        status: str = "failed",
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any] | None:
        safe_status = status if status in {"failed", "cancelled"} else "failed"
        event_type = "run.cancelled" if safe_status == "cancelled" else "run.failed"
        return await self._set_status(
            run_id,
            safe_status,
            event_type,
            message=error,
            result=result,
            metadata=metadata,
            error=error,
            ended=True,
        )

    async def cancel_run(
        self,
        run_id: str | None,
        *,
        message: str | None = None,
    ) -> Dict[str, Any] | None:
        return await self.fail_run(
            run_id,
            message or "Agent run cancelled",
            status="cancelled",
        )

    async def finalize_cancelled_chat_turn(
        self,
        run_id: str | None,
        *,
        message: str = "Conversation generation stopped by user",
    ) -> Dict[str, Any] | None:
        """Persist the partial assistant output and cancelled run exactly once."""

        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return None

        session = await self._session()
        try:
            initial_run = await session.get(AgentRun, run_uuid)
            if (
                initial_run is None
                or initial_run.session_id is None
                or initial_run.run_type != "chat_turn"
            ):
                return None
            session_uuid = initial_run.session_id

            conversation_result = await session.execute(
                select(ConversationSession)
                .where(
                    ConversationSession.id == session_uuid,
                    ConversationSession.deleted_at.is_(None),
                )
                .with_for_update()
            )
            conversation = conversation_result.scalars().first()
            if conversation is None:
                return None

            run_result = await session.execute(
                select(AgentRun)
                .where(AgentRun.id == run_uuid)
                .with_for_update()
            )
            run = run_result.scalars().first()
            if run is None:
                return None

            if run.status in {"succeeded", "failed"}:
                return None

            current_result = dict(run.result or {})
            existing_message_id = parse_uuid(
                current_result.get("assistant_message_id")
            )
            if existing_message_id is not None:
                existing_message = await session.get(
                    ConversationMessage,
                    existing_message_id,
                )
                if existing_message is not None:
                    if run.status == "cancelled":
                        existing_metadata = dict(
                            existing_message.message_metadata or {}
                        )
                        existing_metadata.update(
                            {
                                "agent_run_id": str(run.id),
                                "generation_status": "cancelled",
                                "partial": True,
                                "finish_reason": "user_stop",
                            }
                        )
                        existing_message.message_metadata = _jsonable(
                            existing_metadata
                        )
                        await session.commit()
                        await session.refresh(existing_message)
                    return existing_message.to_dict()

            existing_result = await session.execute(
                select(ConversationMessage)
                .where(
                    ConversationMessage.session_id == session_uuid,
                    ConversationMessage.role == "assistant",
                    ConversationMessage.message_metadata[
                        "agent_run_id"
                    ].as_string()
                    == str(run.id),
                )
                .order_by(
                    desc(ConversationMessage.created_at),
                    desc(ConversationMessage.id),
                )
                .limit(1)
            )
            existing_message = existing_result.scalars().first()
            if existing_message is not None:
                if run.status == "cancelled":
                    existing_metadata = dict(
                        existing_message.message_metadata or {}
                    )
                    existing_metadata.update(
                        {
                            "agent_run_id": str(run.id),
                            "generation_status": "cancelled",
                            "partial": True,
                            "finish_reason": "user_stop",
                        }
                    )
                    existing_message.message_metadata = _jsonable(
                        existing_metadata
                    )
                current_result["assistant_message_id"] = str(existing_message.id)
                run.result = _jsonable(current_result)
                await session.commit()
                await session.refresh(existing_message)
                return existing_message.to_dict()

            events_result = await session.execute(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == run.id)
                .order_by(AgentRunEvent.sequence)
            )
            snapshot = fold_cancelled_chat_snapshot(
                list(events_result.scalars().all())
            )

            from ..memory.conversation_repository import ConversationRepository

            repository = ConversationRepository(session)
            await repository._ensure_linear_parent_links(
                session,
                str(session_uuid),
            )
            parent_message_id = run.trigger_message_id
            message_is_active = True
            if parent_message_id is not None:
                trigger_message = await session.get(
                    ConversationMessage,
                    parent_message_id,
                )
                if (
                    trigger_message is None
                    or trigger_message.session_id != session_uuid
                ):
                    parent_message_id = None
                else:
                    message_is_active = bool(
                        trigger_message.is_active_branch is not False
                    )
            if parent_message_id is None:
                parent = await repository._latest_active_message(
                    session,
                    str(session_uuid),
                )
                parent_message_id = parent.id if parent else None
            branch_index = await repository._count_branch_siblings(
                session,
                str(session_uuid),
                str(parent_message_id) if parent_message_id else None,
            )

            now = datetime.utcnow()
            assistant_message_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"aoitalk:cancelled-agent-run:{run.id}",
            )
            metadata: dict[str, Any] = {
                "agent_run_id": str(run.id),
                "generation_status": "cancelled",
                "partial": True,
                "finish_reason": "user_stop",
            }
            if snapshot["tool_results"]:
                metadata["tool_results"] = snapshot["tool_results"]
            if snapshot["stream_start_sequence"] is not None:
                metadata["stream_start_sequence"] = snapshot[
                    "stream_start_sequence"
                ]
            if snapshot["last_stream_token_sequence"] is not None:
                metadata["last_stream_token_sequence"] = snapshot[
                    "last_stream_token_sequence"
                ]

            assistant_message = ConversationMessage(
                id=assistant_message_id,
                session_id=session_uuid,
                role="assistant",
                content=str(snapshot["content"] or ""),
                parent_message_id=parent_message_id,
                branch_index=branch_index,
                is_active_branch=message_is_active,
                message_metadata=_jsonable(metadata),
                sender_type="assistant",
                created_at=now,
                updated_at=now,
            )
            session.add(assistant_message)

            if run.status not in {"succeeded", "failed", "cancelled"}:
                run.status = "cancelled"
                run.error = _clip(message, max_chars=5000)
                run.ended_at = now
                await self._append_event(
                    session,
                    run,
                    "run.cancelled",
                    status="cancelled",
                    message=message,
                    payload={},
                )
            elif run.status == "cancelled" and run.ended_at is None:
                run.ended_at = now

            current_result.update(
                {
                    "assistant_message_id": str(assistant_message_id),
                    "assistant_response": str(snapshot["content"] or ""),
                    "partial": True,
                    "finish_reason": "user_stop",
                }
            )
            run.result = _jsonable(current_result)
            run.updated_at = now
            await session.execute(
                update(ConversationSession)
                .where(ConversationSession.id == session_uuid)
                .values(
                    message_count=ConversationSession.message_count + 1,
                    last_activity=_monotonic_activity(now),
                    development_status="waiting_for_user",
                )
            )
            await session.commit()
            await session.refresh(assistant_message)
            return assistant_message.to_dict()
        except IntegrityError:
            await session.rollback()
            existing_session = await self._session()
            try:
                existing_message = await existing_session.get(
                    ConversationMessage,
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"aoitalk:cancelled-agent-run:{run_uuid}",
                    ),
                )
                if existing_message is None:
                    raise RuntimeError(
                        "Cancelled chat turn persistence conflicted "
                        "without a recoverable message"
                    )
                return existing_message.to_dict()
            finally:
                await existing_session.close()
        except Exception:
            await session.rollback()
            logger.exception(
                "Failed to finalize cancelled chat turn: %s",
                run_id,
            )
            raise
        finally:
            await session.close()

    async def record_tool_call(
        self,
        run_id: str | None,
        *,
        tool_name: str,
        arguments: Dict[str, Any] | None = None,
        result: Any = None,
        success: bool = False,
        mutation_confirmed: bool = False,
        tool_call_id: str | None = None,
        event_id: str | None = None,
        metadata: Dict[str, Any] | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        duration_ms: int | None = None,
    ) -> Dict[str, Any] | None:
        run_uuid = parse_uuid(run_id)
        if run_uuid is None or not tool_name:
            return None
        normalized_tool_call_id = str(tool_call_id or "").strip() or None

        session = await self._session()
        try:
            run = await session.get(AgentRun, run_uuid)
            if not run:
                return None
            now = datetime.utcnow()
            safe_result = _durable_tool_result(tool_name, result)
            effective_mutation_confirmed = _mutation_confirmation_for_tool(
                tool_name,
                mutation_confirmed,
                success,
            )
            tool_call = AgentRunToolCall(
                run_id=run.id,
                event_id=parse_uuid(event_id),
                tool_name=str(tool_name),
                tool_call_id=normalized_tool_call_id,
                arguments=_jsonable(arguments),
                result=_clip(safe_result),
                success=bool(success),
                mutation_confirmed=effective_mutation_confirmed,
                result_metadata=_jsonable(
                    _redact_sensitive_tool_data(metadata or {})
                ),
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=duration_ms,
                created_at=now,
            )
            session.add(tool_call)
            await self._append_event(
                session,
                run,
                "tool.end" if success else "tool.failed",
                status="succeeded" if success else "failed",
                message=str(tool_name),
                payload={
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "success": bool(success),
                    "mutation_confirmed": effective_mutation_confirmed,
                },
            )
            await session.commit()
            await session.refresh(tool_call)
            return tool_call.to_dict()
        except IntegrityError:
            await session.rollback()
            if normalized_tool_call_id:
                try:
                    existing_result = await session.execute(
                        select(AgentRunToolCall).where(
                            AgentRunToolCall.run_id == run_uuid,
                            AgentRunToolCall.tool_call_id
                            == normalized_tool_call_id,
                        )
                    )
                    existing = existing_result.scalar_one_or_none()
                    if existing is not None:
                        return existing.to_dict()
                except Exception:
                    logger.exception(
                        "Failed to recover duplicate agent run tool call: %s",
                        run_id,
                    )
                    return None
            logger.exception("Failed to record agent run tool call: %s", run_id)
            return None
        except Exception:
            await session.rollback()
            logger.exception("Failed to record agent run tool call: %s", run_id)
            return None
        finally:
            await session.close()

    async def create_edge(
        self,
        *,
        parent_run_id: str,
        child_run_id: str,
        purpose: str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any] | None:
        parent_uuid = parse_uuid(parent_run_id)
        child_uuid = parse_uuid(child_run_id)
        if parent_uuid is None or child_uuid is None:
            return None

        session = await self._session()
        try:
            now = datetime.utcnow()
            edge = AgentRunEdge(
                parent_run_id=parent_uuid,
                child_run_id=child_uuid,
                purpose=purpose,
                status="open",
                edge_metadata=_jsonable(metadata),
                created_at=now,
            )
            session.add(edge)
            await session.commit()
            await session.refresh(edge)
            return edge.to_dict()
        except Exception:
            await session.rollback()
            logger.exception("Failed to create agent run edge")
            return None
        finally:
            await session.close()

    async def close_edge(
        self,
        *,
        parent_run_id: str,
        child_run_id: str,
        status: str,
    ) -> Dict[str, Any] | None:
        """Close a parent/child edge when the child run reaches a terminal state.

        Edges are created as ``open`` so the timeline can expose an in-flight
        delegation.  The run lifecycle is persisted separately; callers must
        close the relationship explicitly once the child succeeds, fails, or
        is cancelled so API traces do not leave a completed child attached to
        a permanently-open edge.
        """

        parent_uuid = parse_uuid(parent_run_id)
        child_uuid = parse_uuid(child_run_id)
        normalized_status = str(status or "").strip().lower()
        if (
            parent_uuid is None
            or child_uuid is None
            or normalized_status not in RUN_TERMINAL_STATUSES
        ):
            return None

        session = await self._session()
        try:
            result = await session.execute(
                select(AgentRunEdge).where(
                    AgentRunEdge.parent_run_id == parent_uuid,
                    AgentRunEdge.child_run_id == child_uuid,
                )
            )
            edge = result.scalar_one_or_none()
            if edge is None:
                return None
            edge.status = normalized_status
            edge.closed_at = datetime.utcnow()
            await session.commit()
            await session.refresh(edge)
            return edge.to_dict()
        except Exception:
            await session.rollback()
            logger.exception(
                "Failed to close agent run edge: %s -> %s",
                parent_run_id,
                child_run_id,
            )
            return None
        finally:
            await session.close()

    async def _set_status(
        self,
        run_id: str | None,
        status: str,
        event_type: str,
        *,
        message: str | None = None,
        metadata: Dict[str, Any] | None = None,
        provider: str | None = None,
        model: str | None = None,
        result: Dict[str, Any] | None = None,
        error: str | None = None,
        started: bool = False,
        ended: bool = False,
    ) -> Dict[str, Any] | None:
        run_uuid = parse_uuid(run_id)
        if run_uuid is None:
            return None

        session = await self._session()
        try:
            run = await session.get(AgentRun, run_uuid)
            if not run:
                return None
            already_started = bool(started and run.started_at is not None)
            if run.status in RUN_TERMINAL_STATUSES and run.status != status:
                await self._append_event(
                    session,
                    run,
                    f"{event_type}.ignored",
                    status=run.status,
                    message=message,
                    payload={
                        "attempted_status": status,
                        "current_status": run.status,
                    },
                )
                await session.commit()
                await session.refresh(run)
                return run.to_dict()
            now = datetime.utcnow()
            run.status = status
            run.updated_at = now
            if started and run.started_at is None:
                run.started_at = now
            if ended:
                run.ended_at = now
                if run.app_id:
                    try:
                        from .app_git_service import AppGitService

                        git = AppGitService()
                        status_snapshot = git.status(run.app_id)
                        if not status_snapshot.get("clean"):
                            run.result_revision = git.checkpoint(
                                run.app_id,
                                f"Agent Run {run.id} 完了時 checkpoint",
                                actor=str(run.user_id) if run.user_id else None,
                            )
                        else:
                            run.result_revision = status_snapshot.get("revision")
                    except Exception:
                        logger.warning("Failed to record App result revision for run %s", run.id)
            if provider:
                run.provider = provider
            if model:
                run.model = model
            if error:
                run.error = _clip(error, max_chars=5000)
            safe_result = (
                _redact_sensitive_tool_data(result)
                if result is not None
                else None
            )
            if safe_result is not None:
                run.result = _jsonable(safe_result)
            if metadata:
                current = dict(run.run_metadata or {})
                current.update(_jsonable(metadata))
                run.run_metadata = current
            if not already_started:
                await self._append_event(
                    session,
                    run,
                    event_type,
                    status=status,
                    message=message,
                    payload=(
                        {"result": _jsonable(safe_result)}
                        if safe_result is not None
                        else {}
                    ),
                )
            if ended and status in {"failed", "cancelled"} and run.session_id:
                # 失敗・中断時は assistant message が保存されないことがあるため、
                # ここで進行中表示を解除しないとサイドバーが回り続ける。
                await session.execute(
                    update(ConversationSession)
                    .where(
                        ConversationSession.id == run.session_id,
                        ConversationSession.development_status == "working",
                    )
                    .values(development_status="waiting_for_user")
                )
            await session.commit()
            await session.refresh(run)
            return run.to_dict()
        except Exception:
            await session.rollback()
            logger.exception("Failed to update agent run status: %s", run_id)
            return None
        finally:
            await session.close()

    async def _append_event(
        self,
        session: AsyncSession,
        run: AgentRun,
        event_type: str,
        *,
        status: str | None = None,
        message: str | None = None,
        payload: Dict[str, Any] | None = None,
    ) -> AgentRunEvent:
        result = await session.execute(
            select(func.max(AgentRunEvent.sequence)).where(
                AgentRunEvent.run_id == run.id
            )
        )
        sequence = int(result.scalar() or 0) + 1
        now = datetime.utcnow()
        event = AgentRunEvent(
            run_id=run.id,
            sequence=sequence,
            event_type=event_type,
            status=status,
            message=message,
            payload=_jsonable(payload),
            created_at=now,
        )
        session.add(event)
        run.last_event_at = now
        run.updated_at = now
        return event
