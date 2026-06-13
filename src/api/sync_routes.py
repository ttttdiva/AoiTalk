"""Mobile sync API routes.

Current write support covers `tasks` and `time_entries`. Remaining tables are
pull-only local caches on mobile.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..memory.models import (
    ConversationMessage,
    ConversationSession,
    Project,
    ProjectMember,
    RecordField,
    RecordRow,
    RecordTable,
    Task,
    TaskAssignee,
    TaskOccurrence,
    TaskRecurrenceRule,
    TaskTag,
    TimeEntry,
)
from ..models.ecc_models import (
    Scenario,
    ScenarioCharacter,
    ScenarioEpisode,
    ScenarioScene,
)
from ..memory.project_repository import ProjectRepository
from ..services.task_management_service import TaskManagementError, TaskManagementService

logger = logging.getLogger(__name__)

SYNC_TABLES = {
    "projects",
    "tasks",
    "task_occurrences",
    "time_entries",
    "conversation_sessions",
    "conversation_messages",
    "record_tables",
    "record_fields",
    "record_rows",
    "scenarios",
    "scenario_characters",
    "scenario_scenes",
    "scenario_episodes",
}

SYNC_PULL_LIMITS = {
    "tasks": 1000,
    "task_occurrences": 1000,
    "time_entries": 1000,
    "conversation_sessions": 5000,
    "conversation_messages": 5000,
    "record_tables": 1000,
    "record_fields": 5000,
    "record_rows": 5000,
    "scenarios": 1000,
    "scenario_characters": 5000,
    "scenario_scenes": 5000,
    "scenario_episodes": 5000,
}


class SyncOperation(BaseModel):
    op_id: str
    table: str
    action: str
    entity_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    base_updated_at: Optional[str] = None


class SyncPushPayload(BaseModel):
    operations: list[SyncOperation] = Field(default_factory=list)


def _parse_uuid(value: Optional[str], field_name: str) -> Optional[UUID]:
    if value in (None, ""):
        return None
    try:
        return UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}") from exc


def _parse_datetime(value: Optional[str], field_name: str) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}") from exc


def _parse_wall_clock_datetime(
    value: Optional[str], field_name: str
) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1]
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return parsed
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}") from exc


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _ensure_not_stale(
    current_updated_at: Optional[datetime], base_updated_at: Optional[str]
) -> None:
    if current_updated_at is None or base_updated_at in (None, ""):
        return
    base_dt = _parse_datetime(base_updated_at, "base_updated_at")
    if base_dt is not None and current_updated_at > base_dt:
        raise TaskManagementError("Entity was updated on the server", status_code=409)


async def _accessible_project_ids(session: AsyncSession, user_id: UUID) -> list[UUID]:
    result = await session.execute(
        select(ProjectMember.project_id)
        .join(Project, Project.id == ProjectMember.project_id)
        .where(ProjectMember.user_id == user_id)
    )
    return list(result.scalars().all())


def _split_changes(rows: list[Any]) -> dict[str, Any]:
    changes = []
    tombstones = []
    for row in rows:
        payload = row.to_dict()
        deleted_at = getattr(row, "deleted_at", None)
        if deleted_at is None:
            changes.append(payload)
        else:
            tombstones.append({"id": str(row.id), "deleted_at": _iso(deleted_at)})
    return {"changes": changes, "tombstones": tombstones, "cursor": None}


def _conversation_session_payload(row: ConversationSession) -> dict[str, Any]:
    payload = row.to_dict()
    payload["updated_at"] = _iso(row.last_activity or row.session_start)
    payload["created_at"] = _iso(row.session_start)
    payload["session_metadata"] = row.context or {}
    return payload


def _record_table_payload(row: RecordTable) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "name": row.name,
        "description": row.description,
        "icon": row.icon,
        "sort_order": row.sort_order,
        "schema_version": row.schema_version,
        "memory_policy": row.memory_policy,
        "default_sensitivity": row.default_sensitivity,
        "metadata": row.table_metadata or {},
        "created_by": str(row.created_by) if row.created_by else None,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "deleted_at": _iso(row.deleted_at),
    }


def _record_field_payload(row: RecordField) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "table_id": str(row.table_id),
        "key": row.key,
        "label": row.label,
        "field_type": row.field_type,
        "options": row.options or {},
        "required": bool(row.required),
        "unique_value": bool(row.unique_value),
        "sort_order": row.sort_order,
        "is_title": bool(row.is_title),
        "is_due": bool(row.is_due),
        "sensitivity": row.sensitivity,
        "metadata": row.field_metadata or {},
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "deleted_at": _iso(row.deleted_at),
    }


def _record_row_payload(row: RecordRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "table_id": str(row.table_id),
        "project_id": str(row.project_id),
        "created_by": str(row.created_by) if row.created_by else None,
        "values": row.values or {},
        "title": row.title,
        "status": row.status,
        "due_at": _iso(row.due_at),
        "search_text": row.search_text,
        "sensitivity": row.sensitivity,
        "metadata": row.row_metadata or {},
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "deleted_at": _iso(row.deleted_at),
    }


def _split_record_changes(rows: list[Any], serializer) -> dict[str, Any]:
    changes = []
    tombstones = []
    for row in rows:
        if getattr(row, "deleted_at", None) is None:
            changes.append(serializer(row))
        else:
            tombstones.append({"id": str(row.id), "deleted_at": _iso(row.deleted_at)})
    return {"changes": changes, "tombstones": tombstones, "cursor": None}


def _conversation_user_ids(user_id: UUID) -> list[str]:
    """Return conversation owner ids visible to mobile sync.

    Existing conversation routes still store normal Web/mobile chat sessions
    under `default_user`. Mobile sync must include those legacy sessions until
    the conversation stack is fully user-scoped.
    """

    ids = [str(user_id)]
    if "default_user" not in ids:
        ids.append("default_user")
    return ids


async def _pull_projects(
    session: AsyncSession, project_ids: list[UUID], since: Optional[datetime]
) -> dict[str, Any]:
    if not project_ids:
        return _split_changes([])
    stmt = select(Project).where(Project.id.in_(project_ids))
    if since:
        stmt = stmt.where(
            or_(Project.updated_at > since, Project.deleted_at > since)
        )
    result = await session.execute(stmt)
    return _split_changes(list(result.scalars().all()))


async def _pull_tasks(
    session: AsyncSession, project_ids: list[UUID], since: Optional[datetime]
) -> dict[str, Any]:
    if not project_ids:
        return _split_changes([])
    stmt = (
        select(Task)
        .options(
            selectinload(Task.project),
            selectinload(Task.assignees).selectinload(TaskAssignee.user),
            selectinload(Task.recurrence_rule),
            selectinload(Task.task_tags).selectinload(TaskTag.tag),
        )
        .where(Task.project_id.in_(project_ids))
    )
    if since:
        stmt = stmt.where(or_(Task.updated_at > since, Task.deleted_at > since))
    stmt = stmt.order_by(Task.updated_at.desc()).limit(SYNC_PULL_LIMITS["tasks"])
    result = await session.execute(stmt)
    return _split_changes(list(result.scalars().unique().all()))


async def _pull_occurrences(
    session: AsyncSession, project_ids: list[UUID], since: Optional[datetime]
) -> dict[str, Any]:
    if not project_ids:
        return _split_changes([])
    stmt = (
        select(TaskOccurrence)
        .join(Task)
        .options(
            selectinload(TaskOccurrence.task).selectinload(Task.project),
            selectinload(TaskOccurrence.task)
            .selectinload(Task.task_tags)
            .selectinload(TaskTag.tag),
        )
        .where(Task.project_id.in_(project_ids))
    )
    if since:
        stmt = stmt.where(
            or_(TaskOccurrence.updated_at > since, TaskOccurrence.deleted_at > since)
        )
    stmt = stmt.order_by(TaskOccurrence.updated_at.desc()).limit(
        SYNC_PULL_LIMITS["task_occurrences"]
    )
    result = await session.execute(stmt)
    return _split_changes(list(result.scalars().unique().all()))


async def _pull_time_entries(
    session: AsyncSession, project_ids: list[UUID], since: Optional[datetime]
) -> dict[str, Any]:
    if not project_ids:
        return _split_changes([])
    stmt = (
        select(TimeEntry)
        .join(Task)
        .options(
            selectinload(TimeEntry.task).selectinload(Task.project),
            selectinload(TimeEntry.user),
            selectinload(TimeEntry.occurrence),
        )
        .where(Task.project_id.in_(project_ids))
    )
    if since:
        stmt = stmt.where(or_(TimeEntry.updated_at > since, TimeEntry.deleted_at > since))
    stmt = stmt.order_by(TimeEntry.updated_at.desc()).limit(
        SYNC_PULL_LIMITS["time_entries"]
    )
    result = await session.execute(stmt)
    return _split_changes(list(result.scalars().unique().all()))


async def _pull_conversation_sessions(
    session: AsyncSession, user_id: UUID, since: Optional[datetime]
) -> dict[str, Any]:
    visible_user_ids = _conversation_user_ids(user_id)
    stmt = select(ConversationSession).where(
        ConversationSession.user_id.in_(visible_user_ids)
    )
    if since:
        stmt = stmt.where(
            or_(
                ConversationSession.last_activity > since,
                ConversationSession.deleted_at > since,
            )
        )
    stmt = stmt.order_by(ConversationSession.last_activity.desc()).limit(
        SYNC_PULL_LIMITS["conversation_sessions"]
    )
    result = await session.execute(stmt)
    changes = []
    tombstones = []
    for row in result.scalars().all():
        if row.deleted_at is None:
            changes.append(_conversation_session_payload(row))
        else:
            tombstones.append({"id": str(row.id), "deleted_at": _iso(row.deleted_at)})

    active_ids_result = await session.execute(
        select(ConversationSession.id).where(
            ConversationSession.user_id.in_(visible_user_ids),
            ConversationSession.deleted_at.is_(None),
        )
    )
    authoritative_ids = [str(item) for item in active_ids_result.scalars().all()]
    return {
        "changes": changes,
        "tombstones": tombstones,
        "cursor": None,
        "authoritative_ids": authoritative_ids,
    }


async def _pull_conversation_messages(
    session: AsyncSession, user_id: UUID, since: Optional[datetime]
) -> dict[str, Any]:
    stmt = (
        select(ConversationMessage)
        .join(ConversationSession)
        .where(ConversationSession.user_id.in_(_conversation_user_ids(user_id)))
    )
    if since:
        stmt = stmt.where(
            or_(
                ConversationMessage.updated_at > since,
                ConversationMessage.deleted_at > since,
            )
        )
    stmt = stmt.order_by(ConversationMessage.updated_at.desc()).limit(
        SYNC_PULL_LIMITS["conversation_messages"]
    )
    result = await session.execute(stmt)
    return _split_changes(list(result.scalars().unique().all()))


async def _pull_record_tables(
    session: AsyncSession, project_ids: list[UUID], since: Optional[datetime]
) -> dict[str, Any]:
    if not project_ids:
        return _split_record_changes([], _record_table_payload)
    stmt = select(RecordTable).where(RecordTable.project_id.in_(project_ids))
    if since:
        stmt = stmt.where(
            or_(RecordTable.updated_at > since, RecordTable.deleted_at > since)
        )
    stmt = stmt.order_by(RecordTable.updated_at.desc()).limit(
        SYNC_PULL_LIMITS["record_tables"]
    )
    result = await session.execute(stmt)
    return _split_record_changes(list(result.scalars().all()), _record_table_payload)


async def _pull_record_fields(
    session: AsyncSession, project_ids: list[UUID], since: Optional[datetime]
) -> dict[str, Any]:
    if not project_ids:
        return _split_record_changes([], _record_field_payload)
    stmt = select(RecordField).join(RecordTable).where(
        RecordTable.project_id.in_(project_ids)
    )
    if since:
        stmt = stmt.where(
            or_(RecordField.updated_at > since, RecordField.deleted_at > since)
        )
    stmt = stmt.order_by(RecordField.updated_at.desc()).limit(
        SYNC_PULL_LIMITS["record_fields"]
    )
    result = await session.execute(stmt)
    return _split_record_changes(list(result.scalars().all()), _record_field_payload)


async def _pull_record_rows(
    session: AsyncSession, project_ids: list[UUID], since: Optional[datetime]
) -> dict[str, Any]:
    if not project_ids:
        return _split_record_changes([], _record_row_payload)
    stmt = select(RecordRow).where(RecordRow.project_id.in_(project_ids))
    if since:
        stmt = stmt.where(or_(RecordRow.updated_at > since, RecordRow.deleted_at > since))
    stmt = stmt.order_by(RecordRow.updated_at.desc()).limit(
        SYNC_PULL_LIMITS["record_rows"]
    )
    result = await session.execute(stmt)
    return _split_record_changes(list(result.scalars().all()), _record_row_payload)


async def _pull_scenarios(
    session: AsyncSession, since: Optional[datetime]
) -> dict[str, Any]:
    stmt = select(Scenario)
    if since:
        stmt = stmt.where(Scenario.updated_at > since)
    stmt = stmt.order_by(Scenario.updated_at.desc()).limit(SYNC_PULL_LIMITS["scenarios"])
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    active_ids_result = await session.execute(select(Scenario.id))
    return {
        "changes": [row.to_dict() for row in rows],
        "tombstones": [],
        "cursor": None,
        "authoritative_ids": [str(item) for item in active_ids_result.scalars().all()],
    }


async def _pull_scenario_characters(
    session: AsyncSession, since: Optional[datetime]
) -> dict[str, Any]:
    # ScenarioCharacter has no updated_at/deleted_at columns, so pull the
    # current authoritative set every time to catch edits and hard deletes.
    stmt = (
        select(ScenarioCharacter)
        .join(Scenario)
        .order_by(ScenarioCharacter.sort_order.asc(), ScenarioCharacter.name.asc())
        .limit(SYNC_PULL_LIMITS["scenario_characters"])
    )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    active_ids_result = await session.execute(
        select(ScenarioCharacter.id).join(Scenario)
    )
    return {
        "changes": [row.to_dict() for row in rows],
        "tombstones": [],
        "cursor": None,
        "authoritative_ids": [str(item) for item in active_ids_result.scalars().all()],
    }


async def _pull_scenario_scenes(
    session: AsyncSession, since: Optional[datetime]
) -> dict[str, Any]:
    # ScenarioScene has no updated_at/deleted_at columns, so pull the current
    # authoritative set every time to catch content edits and hard deletes.
    stmt = (
        select(ScenarioScene)
        .join(Scenario)
        .order_by(ScenarioScene.sort_order.asc(), ScenarioScene.title.asc())
        .limit(SYNC_PULL_LIMITS["scenario_scenes"])
    )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    active_ids_result = await session.execute(select(ScenarioScene.id).join(Scenario))
    return {
        "changes": [row.to_dict() for row in rows],
        "tombstones": [],
        "cursor": None,
        "authoritative_ids": [str(item) for item in active_ids_result.scalars().all()],
    }


async def _pull_scenario_episodes(
    session: AsyncSession, since: Optional[datetime]
) -> dict[str, Any]:
    stmt = select(ScenarioEpisode).join(Scenario)
    if since:
        stmt = stmt.where(ScenarioEpisode.updated_at > since)
    stmt = stmt.order_by(ScenarioEpisode.updated_at.desc()).limit(
        SYNC_PULL_LIMITS["scenario_episodes"]
    )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    active_ids_result = await session.execute(select(ScenarioEpisode.id).join(Scenario))
    return {
        "changes": [row.to_dict() for row in rows],
        "tombstones": [],
        "cursor": None,
        "authoritative_ids": [str(item) for item in active_ids_result.scalars().all()],
    }


async def _pull_table(
    table: str,
    session: AsyncSession,
    *,
    user_id: UUID,
    project_ids: list[UUID],
    since: Optional[datetime],
) -> dict[str, Any]:
    if table == "projects":
        return await _pull_projects(session, project_ids, since)
    if table == "tasks":
        return await _pull_tasks(session, project_ids, since)
    if table == "task_occurrences":
        return await _pull_occurrences(session, project_ids, since)
    if table == "time_entries":
        return await _pull_time_entries(session, project_ids, since)
    if table == "conversation_sessions":
        return await _pull_conversation_sessions(session, user_id, since)
    if table == "conversation_messages":
        return await _pull_conversation_messages(session, user_id, since)
    if table == "record_tables":
        return await _pull_record_tables(session, project_ids, since)
    if table == "record_fields":
        return await _pull_record_fields(session, project_ids, since)
    if table == "record_rows":
        return await _pull_record_rows(session, project_ids, since)
    if table == "scenarios":
        return await _pull_scenarios(session, since)
    if table == "scenario_characters":
        return await _pull_scenario_characters(session, since)
    if table == "scenario_scenes":
        return await _pull_scenario_scenes(session, since)
    if table == "scenario_episodes":
        return await _pull_scenario_episodes(session, since)
    return {"changes": [], "tombstones": [], "cursor": None}


def _task_updates_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": _parse_uuid(payload.get("project_id"), "project_id")
        if "project_id" in payload
        else None,
        "title": payload.get("title"),
        "description": payload.get("description"),
        "status": payload.get("status"),
        "priority": payload.get("priority"),
        "start_at": _parse_wall_clock_datetime(payload.get("start_at"), "start_at")
        if "start_at" in payload
        else None,
        "end_at": _parse_wall_clock_datetime(payload.get("end_at"), "end_at")
        if "end_at" in payload
        else None,
        "all_day": payload.get("all_day"),
        "reminder_offsets": payload.get("reminder_offsets"),
        "notifications_enabled": payload.get("notifications_enabled"),
        "task_metadata": payload.get("metadata") or payload.get("task_metadata"),
    }


def _time_entry_values_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": _parse_uuid(payload.get("task_id"), "task_id"),
        "occurrence_id": _parse_uuid(payload.get("occurrence_id"), "occurrence_id")
        if "occurrence_id" in payload
        else None,
        "started_at": _parse_datetime(payload.get("started_at"), "started_at")
        if "started_at" in payload
        else None,
        "ended_at": _parse_datetime(payload.get("ended_at"), "ended_at")
        if "ended_at" in payload
        else None,
        "source": str(payload.get("source") or "mobile"),
        "note": payload.get("note"),
        "entry_metadata": payload.get("metadata") or payload.get("entry_metadata") or {},
    }


def _project_values_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(payload.get("name") or "").strip(),
        "description": payload.get("description"),
        "aliases": payload.get("aliases") if isinstance(payload.get("aliases"), list) else None,
        "allow_join_requests": payload.get("allow_join_requests"),
        "storage_quota_mb": payload.get("storage_quota_mb"),
        "project_metadata": payload.get("metadata") or payload.get("project_metadata"),
    }


async def _load_current_task_payload(
    service: TaskManagementService, session: AsyncSession, task_id: UUID
) -> Optional[dict[str, Any]]:
    try:
        task = await service._load_task(session, task_id)
        return task.to_dict()
    except TaskManagementError:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task is None:
            return None
        return {
            "id": str(task.id),
            "updated_at": _iso(task.updated_at),
            "deleted_at": _iso(task.deleted_at),
        }


async def _load_current_time_entry_payload(
    service: TaskManagementService, session: AsyncSession, entry_id: UUID
) -> Optional[dict[str, Any]]:
    result = await session.execute(
        select(TimeEntry).where(TimeEntry.id == entry_id)
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        return None
    if entry.deleted_at is not None:
        return {
            "id": str(entry.id),
            "updated_at": _iso(entry.updated_at),
            "deleted_at": _iso(entry.deleted_at),
        }
    return await service._serialize_time_entry(session, entry.id)


async def _apply_task_operation(
    service: TaskManagementService,
    session: AsyncSession,
    *,
    user_id: UUID,
    operation: SyncOperation,
) -> dict[str, Any]:
    task_id = _parse_uuid(operation.entity_id, "entity_id")
    if task_id is None:
        raise TaskManagementError("entity_id is required", status_code=400)

    existing_result = await session.execute(select(Task).where(Task.id == task_id))
    existing = existing_result.scalar_one_or_none()

    if operation.action == "delete":
        if existing is not None:
            _ensure_not_stale(existing.updated_at, operation.base_updated_at)
        await service.delete_task(session, user_id=user_id, task_id=task_id)
        return {"id": str(task_id), "deleted_at": datetime.utcnow().isoformat()}

    if operation.action == "create":
        payload = operation.payload
        title = str(payload.get("title") or "").strip()
        if not title:
            raise TaskManagementError("title is required", status_code=400)
        if existing is not None and existing.deleted_at is None:
            _ensure_not_stale(existing.updated_at, operation.base_updated_at)
            return await service.update_task(
                session,
                user_id=user_id,
                task_id=task_id,
                updates=_task_updates_from_payload(payload),
            )
        created = await service.create_task(
            session,
            user_id=user_id,
            task_id=task_id,
            project_id=_parse_uuid(payload.get("project_id"), "project_id"),
            title=title,
            description=payload.get("description"),
            status=str(payload.get("status") or "open"),
            priority=payload.get("priority"),
            start_at=_parse_wall_clock_datetime(payload.get("start_at"), "start_at"),
            end_at=_parse_wall_clock_datetime(payload.get("end_at"), "end_at"),
            all_day=bool(payload.get("all_day") or False),
            reminder_offsets=payload.get("reminder_offsets"),
            task_metadata=payload.get("metadata") or payload.get("task_metadata"),
            source=str(payload.get("source") or "mobile"),
        )
        return created

    if operation.action == "update":
        current = await service._load_task(session, task_id)
        _ensure_not_stale(current.updated_at, operation.base_updated_at)
        return await service.update_task(
            session,
            user_id=user_id,
            task_id=task_id,
            updates=_task_updates_from_payload(operation.payload),
        )

    raise TaskManagementError("Unsupported task sync action", status_code=400)


async def _apply_time_entry_operation(
    service: TaskManagementService,
    session: AsyncSession,
    *,
    user_id: UUID,
    operation: SyncOperation,
) -> dict[str, Any]:
    entry_id = _parse_uuid(operation.entity_id, "entity_id")
    if entry_id is None:
        raise TaskManagementError("entity_id is required", status_code=400)

    values = _time_entry_values_from_payload(operation.payload)
    existing_result = await session.execute(select(TimeEntry).where(TimeEntry.id == entry_id))
    existing = existing_result.scalar_one_or_none()

    if operation.action == "delete":
        if existing is None:
            return {"id": str(entry_id), "deleted_at": datetime.utcnow().isoformat()}
        _ensure_not_stale(existing.updated_at, operation.base_updated_at)
        await service.delete_time_entry(session, user_id=user_id, entry_id=entry_id)
        return {"id": str(entry_id), "deleted_at": datetime.utcnow().isoformat()}

    if operation.action == "create":
        task_id = values["task_id"]
        started_at = values["started_at"]
        if task_id is None:
            raise TaskManagementError("task_id is required", status_code=400)
        if started_at is None:
            raise TaskManagementError("started_at is required", status_code=400)

        task = await service._load_task(session, task_id)
        await service.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )

        if existing is not None and existing.deleted_at is None:
            _ensure_not_stale(existing.updated_at, operation.base_updated_at)
            return await service._serialize_time_entry(session, existing.id)

        active_result = await session.execute(
            select(TimeEntry).where(
                TimeEntry.user_id == user_id,
                TimeEntry.ended_at.is_(None),
                TimeEntry.deleted_at.is_(None),
                TimeEntry.id != entry_id,
            )
        )
        for active_entry in active_result.scalars().all():
            active_entry.ended_at = started_at
            active_entry.updated_at = started_at

        entry = TimeEntry(
            id=entry_id,
            task_id=task.id,
            occurrence_id=values["occurrence_id"],
            user_id=user_id,
            started_at=started_at,
            ended_at=values["ended_at"],
            source=values["source"],
            note=values["note"],
            entry_metadata=values["entry_metadata"],
        )
        session.add(entry)
        if values["ended_at"] is None:
            task.status = "in_progress"
        await session.commit()
        return await service._serialize_time_entry(session, entry.id)

    if operation.action == "update":
        if existing is None or existing.deleted_at is not None:
            raise TaskManagementError("Time entry not found", status_code=404)
        _ensure_not_stale(existing.updated_at, operation.base_updated_at)
        return await service.update_time_entry(
            session,
            user_id=user_id,
            entry_id=entry_id,
            started_at=values["started_at"],
            ended_at=values["ended_at"] if "ended_at" in operation.payload else None,
            note=values["note"] if "note" in operation.payload else None,
        )

    raise TaskManagementError("Unsupported time entry sync action", status_code=400)


async def _apply_project_operation(
    service: TaskManagementService,
    session: AsyncSession,
    *,
    user_id: UUID,
    operation: SyncOperation,
) -> dict[str, Any]:
    project_id = _parse_uuid(operation.entity_id, "entity_id")
    if project_id is None:
        raise TaskManagementError("entity_id is required", status_code=400)

    existing = await ProjectRepository.get_by_id(session, project_id)
    values = _project_values_from_payload(operation.payload)

    if operation.action == "delete":
        if existing is None:
            return {"id": str(project_id), "deleted_at": datetime.utcnow().isoformat()}
        await service.require_project_permission(
            session, project_id=project_id, user_id=user_id, permission="delete"
        )
        _ensure_not_stale(existing.updated_at, operation.base_updated_at)
        await ProjectRepository.delete_project(session, project_id)
        return {"id": str(project_id), "deleted_at": datetime.utcnow().isoformat()}

    if operation.action == "create":
        name = values["name"]
        if not name:
            raise TaskManagementError("name is required", status_code=400)
        if existing is not None and existing.deleted_at is None:
            await service.require_project_permission(
                session, project_id=project_id, user_id=user_id, permission="write"
            )
            _ensure_not_stale(existing.updated_at, operation.base_updated_at)
            updated = await ProjectRepository.update_project(
                session,
                project_id,
                **{key: value for key, value in values.items() if value is not None},
            )
            return updated.to_dict() if updated else {"id": str(project_id)}
        created = await ProjectRepository.create_project(
            session,
            owner_id=user_id,
            project_id=project_id,
            name=name,
            description=values["description"],
            aliases=values["aliases"],
            allow_join_requests=bool(
                values["allow_join_requests"]
                if values["allow_join_requests"] is not None
                else True
            ),
            storage_quota_mb=int(values["storage_quota_mb"] or 1000),
            project_metadata=values["project_metadata"] or {},
        )
        return created.to_dict()

    if operation.action == "update":
        if existing is None or existing.deleted_at is not None:
            raise TaskManagementError("Project not found", status_code=404)
        await service.require_project_permission(
            session, project_id=project_id, user_id=user_id, permission="write"
        )
        _ensure_not_stale(existing.updated_at, operation.base_updated_at)
        update_values = {key: value for key, value in values.items() if value is not None}
        if "name" in update_values and not update_values["name"]:
            update_values.pop("name")
        updated = await ProjectRepository.update_project(session, project_id, **update_values)
        return updated.to_dict() if updated else {"id": str(project_id)}

    raise TaskManagementError("Unsupported project sync action", status_code=400)


def create_sync_router(
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/sync", tags=["sync"])
    service = TaskManagementService()

    async def _get_current_user(request: Request) -> tuple[UUID, dict[str, Any]]:
        user_info = await get_user_from_request(request)
        if not user_info or "id" not in user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return UUID(user_info["id"]), user_info

    @router.get("/pull")
    async def pull(
        request: Request,
        since: Optional[str] = None,
        tables: Optional[str] = None,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        requested = [
            table.strip()
            for table in (tables.split(",") if tables else sorted(SYNC_TABLES))
            if table.strip()
        ]
        unsupported = [table for table in requested if table not in SYNC_TABLES]
        if unsupported:
            raise HTTPException(status_code=400, detail=f"Unsupported tables: {unsupported}")

        session = await get_db_manager().get_session()
        server_time = datetime.utcnow()
        try:
            since_dt = _parse_datetime(since, "since")
            await ProjectRepository.ensure_user_inbox_setup(session, user_id)
            await session.commit()
            project_ids = await _accessible_project_ids(session, user_id)
            pulled = {}
            for table in requested:
                pulled[table] = await _pull_table(
                    table,
                    session,
                    user_id=user_id,
                    project_ids=project_ids,
                    since=since_dt,
                )
                pulled[table]["cursor"] = server_time.isoformat()
            return {
                "tables": pulled,
                "server_time": server_time.isoformat(),
                "has_more": False,
            }
        finally:
            await session.close()

    @router.post("/push")
    async def push(
        payload: SyncPushPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        results = []
        try:
            for operation in payload.operations:
                try:
                    if operation.table == "projects":
                        entity = await _apply_project_operation(
                            service, session, user_id=user_id, operation=operation
                        )
                    elif operation.table == "tasks":
                        entity = await _apply_task_operation(
                            service, session, user_id=user_id, operation=operation
                        )
                    elif operation.table == "time_entries":
                        entity = await _apply_time_entry_operation(
                            service, session, user_id=user_id, operation=operation
                        )
                    else:
                        raise TaskManagementError(
                            f"Unsupported push table: {operation.table}",
                            status_code=400,
                        )
                    results.append(
                        {
                            "op_id": operation.op_id,
                            "status": "ok",
                            "entity": entity,
                            "server_updated_at": entity.get("updated_at")
                            if isinstance(entity, dict)
                            else None,
                        }
                    )
                except TaskManagementError as exc:
                    await session.rollback()
                    status = "conflict" if exc.status_code == 409 else "error"
                    conflict_entity: Optional[dict[str, Any]] = None
                    entity_id = _parse_uuid(operation.entity_id, "entity_id")
                    if status == "conflict" and entity_id is not None:
                        if operation.table == "tasks":
                            conflict_entity = await _load_current_task_payload(
                                service, session, entity_id
                            )
                        elif operation.table == "time_entries":
                            conflict_entity = await _load_current_time_entry_payload(
                                service, session, entity_id
                            )
                    results.append(
                        {
                            "op_id": operation.op_id,
                            "status": status,
                            "reason": exc.message,
                            "entity": conflict_entity,
                            "server_updated_at": conflict_entity.get("updated_at")
                            if isinstance(conflict_entity, dict)
                            else None,
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    await session.rollback()
                    logger.exception("Sync push operation failed: %s", operation.op_id)
                    results.append(
                        {
                            "op_id": operation.op_id,
                            "status": "error",
                            "reason": str(exc),
                        }
                    )
            return {"results": results}
        finally:
            await session.close()

    return router
