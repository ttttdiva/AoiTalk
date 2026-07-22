"""Service layer for durable agent run tracking."""

from __future__ import annotations

import json
import logging
import uuid
from contextvars import ContextVar, Token
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..memory.database import get_database_manager
from ..memory.models import AgentRun, AgentRunEdge, AgentRunEvent, AgentRunToolCall
from .agent_team_service import AGENT_TEAM_MEMBER_LABELS
from ..utils.uuid_utils import parse_uuid

logger = logging.getLogger(__name__)

RUN_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
_current_agent_run_id: ContextVar[str | None] = ContextVar(
    "aoitalk_current_agent_run_id",
    default=None,
)

AGENT_TEAM_TOOL_MEMBERS = {
    "agent_team_delegate": "agent_team",
    "advanced_reasoning_assistant": "advanced_reasoning",
    "utility_assistant": "utility",
    "media_assistant": "media",
    "spotify_assistant": "spotify",
    "scenario_assistant": "scenario",
    "writing_assistant": "writing",
    "import_assistant": "import",
}

DIRECT_TOOL_LABELS = {
    "web_search": "Web検索",
    "search_web": "Web検索",
    "shell_command": "シェルコマンド",
    "deep_research": "Deep Research",
    "get_weather": "天気",
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
}


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


def _clip(text: Any, max_chars: int = 20000) -> str | None:
    if text is None:
        return None
    value = str(text)
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + "\n... (truncated)"


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
    return _payload_text(
        payload,
        "operation_id",
        "tool_call_id",
        "call_id",
        "agent_instance_key",
        "actor_instance_key",
    )


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
    member_key = AGENT_TEAM_TOOL_MEMBERS.get(clean_name)
    if member_key:
        return {
            "actor_type": "agent_team",
            "actor_key": member_key,
            "actor_label": AGENT_TEAM_MEMBER_LABELS.get(member_key, member_key),
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
                or AGENT_TEAM_MEMBER_LABELS.get(actor_key, actor_key)
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


def _timeline_event_item(run: AgentRun, event: AgentRunEvent) -> dict[str, Any]:
    payload = event.payload if isinstance(event.payload, dict) else {}
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
        "group_id": _payload_text(payload, "group_id", "model_group") or None,
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
    operation_error: str | None = None,
) -> dict[str, Any]:
    raw_tool_name = _clean_tool_name(tool_call.tool_name)
    tool_name = _normalize_tool_name(raw_tool_name)
    actor = _actor_for_tool(tool_name)
    metadata = tool_call.result_metadata if isinstance(tool_call.result_metadata, dict) else {}
    arguments = tool_call.arguments or {}
    if tool_name == "shell_command" and raw_tool_name != tool_name:
        arguments = dict(arguments)
        arguments.setdefault("command", raw_tool_name)
    error = operation_error or _payload_text(metadata, "error") or None
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
        "status": "succeeded" if tool_call.success else "failed",
        "display_status": "succeeded" if tool_call.success else "failed",
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
        "group_id": _payload_text(metadata, "group_id", "model_group") or None,
        "action": _tool_operation_label(tool_name, actor.get("actor_label")),
        "message": tool_name,
        "tool_name": tool_name,
        "raw_tool_name": raw_tool_name if raw_tool_name != tool_name else None,
        "tool_call_id": tool_call.tool_call_id,
        "arguments": arguments,
        "result": _clip(tool_call.result),
        "result_preview": _clip(tool_call.result, max_chars=1200),
        "error": error,
        "success": bool(tool_call.success),
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
                if not stable_id:
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
                if operation is None and not queue and not stable_id:
                    candidates = [
                        candidate
                        for candidate in tool_operations
                        if candidate.get("end") is None
                        and candidate.get("tool_name") == tool_name
                        and candidate.get("signature") == signature
                    ]
                    if not candidates:
                        candidates = [
                            candidate
                            for candidate in tool_operations
                            if candidate.get("end") is None
                            and candidate.get("tool_name") == tool_name
                        ]
                    if candidates:
                        candidate_key = str(candidates[0].get("key") or "")
                        queue = open_tool_operations.get(candidate_key, [])
                        queue_key = candidate_key
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
        item.update(
            {
                "id": f"operation:agent:{start.id if start else end.id}",
                "event_type": "agent_operation",
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
                    operation_error=(
                        _event_tool_error(end.payload)
                        if end and isinstance(end.payload, dict)
                        else None
                    ),
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
        error = _event_tool_error(end_payload) or (
            str(run.error) if interrupted_status == "failed" and run.error else None
        )
        result = _event_tool_result(end_payload)
        tool_name = str(operation.get("tool_name") or item.get("tool_name") or "")
        item.update(
            {
                "id": f"operation:tool:{start.id if start else end.id}",
                "event_type": "tool_operation",
                "status": (
                    interrupted_status
                    if end is None and interrupted_status
                    else "running"
                    if end is None
                    else "failed"
                    if error
                    else "succeeded"
                ),
                "display_status": (
                    interrupted_status
                    if end is None and interrupted_status
                    else "started"
                    if end is None
                    else "failed"
                    if error
                    else "succeeded"
                ),
                "action": _tool_operation_label(tool_name, item.get("actor_label")),
                "message": None,
                "arguments": _event_tool_arguments(start_payload) or _event_tool_arguments(end_payload),
                "result": result,
                "result_preview": _clip(result, max_chars=1200),
                "error": error,
                "success": (
                    False
                    if end is None and interrupted_status == "failed"
                    else None
                    if end is None
                    else not bool(error)
                ),
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
        project_id: str | None = None,
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

            run = AgentRun(
                parent_run_id=parent_uuid,
                root_run_id=root_uuid,
                session_id=parse_uuid(session_id),
                trigger_message_id=parse_uuid(trigger_message_id),
                project_id=parse_uuid(project_id),
                user_id=str(user_id) if user_id else None,
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
        except Exception:
            await session.rollback()
            logger.exception("Failed to create agent run")
            raise
        finally:
            await session.close()

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
                payload["timeline"] = build_agent_run_timeline(run)
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
            event = await self._append_event(
                session,
                run,
                event_type,
                status=status,
                message=message,
                payload=payload,
            )
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
    ) -> Dict[str, Any] | None:
        return await self._set_status(
            run_id,
            "succeeded",
            "run.succeeded",
            message=message or "Agent run completed",
            result=result,
            ended=True,
        )

    async def fail_run(
        self,
        run_id: str | None,
        error: str,
        *,
        result: Dict[str, Any] | None = None,
        status: str = "failed",
    ) -> Dict[str, Any] | None:
        safe_status = status if status in {"failed", "cancelled"} else "failed"
        event_type = "run.cancelled" if safe_status == "cancelled" else "run.failed"
        return await self._set_status(
            run_id,
            safe_status,
            event_type,
            message=error,
            result=result,
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

        session = await self._session()
        try:
            run = await session.get(AgentRun, run_uuid)
            if not run:
                return None
            now = datetime.utcnow()
            tool_call = AgentRunToolCall(
                run_id=run.id,
                event_id=parse_uuid(event_id),
                tool_name=str(tool_name),
                tool_call_id=tool_call_id,
                arguments=_jsonable(arguments),
                result=_clip(result),
                success=bool(success),
                mutation_confirmed=bool(mutation_confirmed),
                result_metadata=_jsonable(metadata),
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
                    "mutation_confirmed": bool(mutation_confirmed),
                },
            )
            await session.commit()
            await session.refresh(tool_call)
            return tool_call.to_dict()
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
            if provider:
                run.provider = provider
            if model:
                run.model = model
            if error:
                run.error = _clip(error, max_chars=5000)
            if result is not None:
                run.result = _jsonable(result)
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
                    payload={"result": _jsonable(result)} if result is not None else {},
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
