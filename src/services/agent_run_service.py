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
    "deep_research": "Deep Research",
    "get_weather": "天気",
    "get_current_time": "現在時刻",
    "calculate": "計算",
    "create_task": "タスク作成",
    "update_task": "タスク更新",
    "list_tasks": "タスク取得",
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


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


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
    clean_name = _clean_tool_name(tool_name)
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


def _timeline_event_item(run: AgentRun, event: AgentRunEvent) -> dict[str, Any]:
    payload = event.payload if isinstance(event.payload, dict) else {}
    tool_name = _event_tool_name(event.event_type, payload)
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
        "mode": _payload_text(payload, "mode", "model_mode", "reasoning_effort") or None,
        "action": _event_action(event.event_type, tool_label),
        "message": event.message,
        "tool_name": tool_name or None,
        "payload": _jsonable(payload),
        "created_at": _dt(event.created_at),
    }
    return item


def _timeline_tool_call_item(
    tool_call: AgentRunToolCall,
) -> dict[str, Any]:
    actor = _actor_for_tool(tool_call.tool_name)
    metadata = tool_call.result_metadata if isinstance(tool_call.result_metadata, dict) else {}
    return {
        "id": f"tool:{tool_call.id}",
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
        or None,
        "model": _model_text(metadata, "model", "agent_model", "model_name") or None,
        "mode": _payload_text(metadata, "mode", "model_mode", "reasoning_effort") or None,
        "action": "実行結果を記録",
        "message": tool_call.tool_name,
        "tool_name": tool_call.tool_name,
        "tool_call_id": tool_call.tool_call_id,
        "arguments": tool_call.arguments or {},
        "result_preview": _clip(tool_call.result, max_chars=1200),
        "success": bool(tool_call.success),
        "mutation_confirmed": bool(tool_call.mutation_confirmed),
        "duration_ms": tool_call.duration_ms,
        "payload": metadata,
        "created_at": _dt(tool_call.created_at),
        "started_at": _dt(tool_call.started_at),
        "ended_at": _dt(tool_call.ended_at),
    }


def build_agent_run_timeline(run: AgentRun) -> list[dict[str, Any]]:
    """Build a chronological UI timeline from run events and tool evidence."""

    items: list[tuple[datetime, int, dict[str, Any]]] = []
    tool_calls = list(getattr(run, "tool_calls", []) or [])
    recorded_tool_names = {
        _clean_tool_name(tool_call.tool_name) for tool_call in tool_calls
    }
    for event in getattr(run, "events", []) or []:
        payload = event.payload if isinstance(event.payload, dict) else {}
        tool_name = _event_tool_name(event.event_type, payload)
        if (
            event.event_type == "stream.tool_end"
            and payload.get("tool_result_already_recorded")
            and tool_name
            and tool_name in recorded_tool_names
        ):
            continue
        created_at = event.created_at or datetime.min
        items.append(
            (
                created_at,
                int(event.sequence or 0),
                _timeline_event_item(run, event),
            )
        )

    for index, tool_call in enumerate(tool_calls):
        created_at = tool_call.created_at or tool_call.started_at or datetime.min
        items.append(
            (
                created_at,
                100000 + index,
                _timeline_tool_call_item(tool_call),
            )
        )

    return [
        item
        for _created_at, _order, item in sorted(
            items,
            key=lambda row: (row[0], row[1]),
        )
    ]


def build_agent_run_timeline_columns(
    timeline: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group timeline rows by displayed actor for the chat UI."""

    columns: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    for item in timeline:
        actor_key = str(item.get("actor_key") or item.get("actor_label") or "main")
        column = by_key.get(actor_key)
        if column is None:
            column = {
                "key": actor_key,
                "label": item.get("actor_label") or actor_key,
                "actor_type": item.get("actor_type"),
                "provider": item.get("provider"),
                "model": item.get("model"),
                "items": [],
            }
            by_key[actor_key] = column
            columns.append(column)
        if not column.get("provider") and item.get("provider"):
            column["provider"] = item.get("provider")
        if not column.get("model") and item.get("model"):
            column["model"] = item.get("model")
        column["items"].append(item)
    return columns


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
            parent_uuid = _parse_uuid(parent_run_id)
            root_uuid = None
            if parent_uuid:
                parent = await session.get(AgentRun, parent_uuid)
                if parent:
                    root_uuid = parent.root_run_id or parent.id

            run = AgentRun(
                parent_run_id=parent_uuid,
                root_run_id=root_uuid,
                session_id=_parse_uuid(session_id),
                trigger_message_id=_parse_uuid(trigger_message_id),
                project_id=_parse_uuid(project_id),
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
        run_uuid = _parse_uuid(run_id)
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
                timeline = build_agent_run_timeline(run)
                payload["timeline"] = timeline
                payload["timeline_columns"] = build_agent_run_timeline_columns(
                    timeline
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
        session_uuid = _parse_uuid(session_id)
        project_uuid = _parse_uuid(project_id)
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
        run_uuid = _parse_uuid(run_id)
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
        run_uuid = _parse_uuid(run_id)
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
                event_id=_parse_uuid(event_id),
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
        parent_uuid = _parse_uuid(parent_run_id)
        child_uuid = _parse_uuid(child_run_id)
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
        run_uuid = _parse_uuid(run_id)
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
