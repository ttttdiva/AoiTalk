"""Task management: タイムトラッキング（タイマー・工数・レポート）。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...memory.models import (
    NotificationDelivery,
    Project,
    ProjectNotificationSetting,
    ProjectMember,
    Space,
    Tag,
    Task,
    TaskActivity,
    TaskAssignee,
    TaskAttachment,
    TaskReference,
    TaskComment,
    TaskDependency,
    TaskOccurrence,
    TaskRecurrenceRule,
    TaskTag,
    TimeEntry,
    User,
    KnowledgeNode,
    KnowledgeNodeSupertag,
    KnowledgeSupertag,
)
from ...memory.project_repository import ProjectRepository
from ...task_time import (
    DEFAULT_TASK_TIMEZONE,
    normalize_task_timezone,
    timer_api_datetime,
    timer_api_datetime_value,
    timer_db_datetime,
    timer_duration_seconds,
    timer_now_db,
)
from ..project_color_service import extract_project_color
from ..task_reference_service import attach_agent_run_source_reference
from ._shared import (
    DEFAULT_MEMBER_PERMISSIONS,
    DEFAULT_USER_NOTIFICATION_MINUTES,
    DISALLOWED_PLACEHOLDER_TITLES,
    LEGACY_STATUS_MAP,
    VALID_PRIORITIES,
    VALID_TASK_STATUSES,
    ScheduledOccurrence,
    TaskManagementError,
    build_occurrence_schedule,
    build_time_report,
    normalize_priority,
    normalize_task_status,
    _ensure_reminder_offsets,
    _get_user_notification_minutes,
    _get_user_task_notifications_default_enabled,
    _is_date_only_occurrence,
    _is_midnight,
    _normalize_member_permissions,
    _normalize_task_title,
    _strip_google_calendar_metadata,
)

logger = logging.getLogger(__name__)


class TimeTrackingMixin:
    """タイムトラッキング（タイマー・工数・レポート）。"""

    def _build_time_entry_payload(self, entry: TimeEntry) -> dict[str, Any]:
        """TimeEntry.to_dict() に Web BFF 互換のフィールドを追加して返す。

        Web の time-entries 系レスポンス（project_color / space_id / space_name /
        original_started_at / original_ended_at and explicit timer offsets).
        """
        data = entry.to_dict()
        # TimeEntry columns are naive deployment-zone wall-clock values in
        # PostgreSQL.  Every timer-facing response uses one explicit-offset
        # serializer so list/active/start/stop/detail all agree.
        data["started_at"] = timer_api_datetime(entry.started_at)
        data["ended_at"] = timer_api_datetime(entry.ended_at)
        db_started_at = timer_db_datetime(entry.started_at)
        db_ended_at = timer_db_datetime(entry.ended_at)
        data["duration_seconds"] = (
            timer_duration_seconds(db_started_at, db_ended_at)
            if db_started_at is not None and db_ended_at is not None
            else None
        )
        task = entry.task
        project = task.project if task is not None else None
        space = project.space if project is not None else None

        data["project_color"] = (
            extract_project_color(project.project_metadata)
            if project is not None
            else None
        )
        data["space_id"] = str(space.id) if space is not None else None
        data["space_name"] = space.name if space is not None else None

        metadata = entry.entry_metadata or {}
        data["original_started_at"] = timer_api_datetime_value(
            metadata.get("original_started_at")
        )
        data["original_ended_at"] = timer_api_datetime_value(
            metadata.get("original_ended_at")
        )
        return data

    async def _serialize_time_entry(
        self, session: AsyncSession, entry_id: UUID
    ) -> dict[str, Any]:
        result = await session.execute(
            select(TimeEntry)
            .join(Task, Task.id == TimeEntry.task_id)
            .options(
                selectinload(TimeEntry.task)
                .selectinload(Task.project)
                .selectinload(Project.space),
                selectinload(TimeEntry.user),
                selectinload(TimeEntry.occurrence),
            )
            .where(
                TimeEntry.id == entry_id,
                TimeEntry.deleted_at.is_(None),
                Task.deleted_at.is_(None),
            )
        )
        entry = result.scalar_one()
        return self._build_time_entry_payload(entry)

    async def get_active_time_entry(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: Optional[UUID] = None,
    ) -> Optional[dict[str, Any]]:
        stmt = (
            select(TimeEntry)
            .join(Task, Task.id == TimeEntry.task_id)
            .options(
                selectinload(TimeEntry.task)
                .selectinload(Task.project)
                .selectinload(Project.space),
                selectinload(TimeEntry.user),
                selectinload(TimeEntry.occurrence),
            )
            .where(
                TimeEntry.user_id == user_id,
                TimeEntry.ended_at.is_(None),
                TimeEntry.deleted_at.is_(None),
                Task.deleted_at.is_(None),
            )
            .order_by(TimeEntry.started_at.desc())
        )
        if task_id is not None:
            stmt = stmt.where(TimeEntry.task_id == task_id)

        result = await session.execute(stmt)
        entry = result.scalar_one_or_none()
        return self._build_time_entry_payload(entry) if entry else None

    async def start_timer(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: UUID,
        occurrence_id: Optional[UUID] = None,
        source: str = "manual",
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        task = await self._load_task(session, task_id)
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )

        active_result = await session.execute(
            select(TimeEntry)
            .join(Task, Task.id == TimeEntry.task_id)
            .where(
                TimeEntry.user_id == user_id,
                TimeEntry.ended_at.is_(None),
                TimeEntry.deleted_at.is_(None),
                Task.deleted_at.is_(None),
            )
        )
        active_entry = active_result.scalar_one_or_none()
        # Store a naive wall-clock value in the configured deployment zone;
        # do not rely on the host operating system timezone.
        now = timer_now_db()
        if active_entry is not None:
            active_entry.ended_at = now
            await self._record_activity(
                session,
                task_id=active_entry.task_id,
                activity_type="timer_stopped_auto",
                user_id=user_id,
                payload={"time_entry_id": str(active_entry.id)},
            )

        entry = TimeEntry(
            task_id=task.id,
            occurrence_id=occurrence_id,
            user_id=user_id,
            started_at=now,
            source=source,
            note=note,
        )
        session.add(entry)
        task.status = "in_progress"
        if occurrence_id:
            occurrence_result = await session.execute(
                select(TaskOccurrence)
                .join(Task, Task.id == TaskOccurrence.task_id)
                .where(
                    TaskOccurrence.id == occurrence_id,
                    TaskOccurrence.task_id == task.id,
                    TaskOccurrence.deleted_at.is_(None),
                    Task.deleted_at.is_(None),
                )
            )
            occurrence = occurrence_result.scalar_one_or_none()
            if occurrence:
                occurrence.status = "in_progress"

        await self._record_activity(
            session,
            task_id=task.id,
            activity_type="timer_started",
            user_id=user_id,
            payload={"occurrence_id": str(occurrence_id) if occurrence_id else None},
        )
        await session.commit()
        payload = await self._serialize_time_entry(session, entry.id)
        await self._broadcast("time_entry_started", payload)
        return payload

    async def stop_timer(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        time_entry_id: Optional[UUID] = None,
    ) -> dict[str, Any]:
        stmt = select(TimeEntry).where(
            TimeEntry.user_id == user_id,
            TimeEntry.ended_at.is_(None),
            TimeEntry.deleted_at.is_(None),
        )
        if time_entry_id is not None:
            stmt = stmt.where(TimeEntry.id == time_entry_id)
        result = await session.execute(stmt.order_by(TimeEntry.started_at.desc()))
        entry = result.scalar_one_or_none()
        if entry is None:
            raise TaskManagementError("No active timer found", status_code=404)

        # Keep the original DB start untouched.  Older code rewrote starts
        # using an 8--10 hour heuristic at stop time, which corrupted valid
        # long/manual entries.  Only the stop wall-clock is generated here.
        entry.ended_at = timer_now_db()
        await self._record_activity(
            session,
            task_id=entry.task_id,
            activity_type="timer_stopped",
            user_id=user_id,
            payload={"time_entry_id": str(entry.id)},
        )
        await session.commit()
        payload = await self._serialize_time_entry(session, entry.id)
        await self._broadcast("time_entry_stopped", payload)
        return payload

    async def log_time(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: UUID,
        started_at: datetime,
        ended_at: datetime,
        occurrence_id: Optional[UUID] = None,
        source: str = "manual",
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        started_at = timer_db_datetime(started_at)
        ended_at = timer_db_datetime(ended_at)
        if ended_at <= started_at:
            raise TaskManagementError(
                "ended_at must be after started_at", status_code=400
            )

        task = await self._load_task(session, task_id)
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )

        entry = TimeEntry(
            task_id=task.id,
            occurrence_id=occurrence_id,
            user_id=user_id,
            started_at=started_at,
            ended_at=ended_at,
            source=source,
            note=note,
        )
        session.add(entry)
        await self._record_activity(
            session,
            task_id=task.id,
            activity_type="time_logged",
            user_id=user_id,
            payload={
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
            },
        )
        await session.commit()
        payload = await self._serialize_time_entry(session, entry.id)
        await self._broadcast("time_entry_logged", payload)
        return payload

    async def update_time_entry(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        entry_id: UUID,
        started_at: Optional[datetime] = None,
        ended_at: Optional[datetime] = None,
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        result = await session.execute(
            select(TimeEntry).where(
                TimeEntry.id == entry_id, TimeEntry.deleted_at.is_(None)
            )
        )
        entry = result.scalar_one_or_none()
        if entry is None:
            raise TaskManagementError("Time entry not found", status_code=404)
        task = await self._load_task(session, entry.task_id)
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )

        # Web BFF と同様に、初回編集時のみ元の時刻を metadata に保存する。
        metadata = dict(entry.entry_metadata or {})
        is_first_edit = (
            "original_started_at" not in metadata
            and "original_ended_at" not in metadata
        )
        if is_first_edit and (started_at is not None or ended_at is not None):
            if entry.started_at:
                metadata["original_started_at"] = entry.started_at.isoformat()
            if entry.ended_at:
                metadata["original_ended_at"] = entry.ended_at.isoformat()
        metadata["edited_at"] = datetime.now().isoformat()
        metadata["edited_by"] = str(user_id)
        entry.entry_metadata = metadata

        if started_at is not None:
            entry.started_at = timer_db_datetime(started_at)
        if ended_at is not None:
            entry.ended_at = timer_db_datetime(ended_at)
        if note is not None:
            entry.note = note
        if entry.ended_at and entry.started_at and entry.ended_at <= entry.started_at:
            raise TaskManagementError(
                "ended_at must be after started_at", status_code=400
            )
        await session.commit()
        return await self._serialize_time_entry(session, entry_id)

    async def delete_time_entry(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        entry_id: UUID,
    ) -> None:
        result = await session.execute(
            select(TimeEntry).where(
                TimeEntry.id == entry_id, TimeEntry.deleted_at.is_(None)
            )
        )
        entry = result.scalar_one_or_none()
        if entry is None:
            raise TaskManagementError("Time entry not found", status_code=404)
        task = await self._load_task(session, entry.task_id)
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )
        now = datetime.utcnow()
        entry.deleted_at = now
        entry.updated_at = now
        await session.commit()

    async def list_time_entries(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        project_id: Optional[UUID] = None,
        space_id: Optional[UUID] = None,
        browse_project_id: Optional[UUID | str] = None,
        browse_space_id: Optional[UUID | str] = None,
        task_id: Optional[UUID] = None,
        active_only: bool = False,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        date_from = timer_db_datetime(date_from)
        date_to = timer_db_datetime(date_to)
        participating_project_ids = await self.resolve_read_project_ids(
            session,
            user_id=user_id,
            project_id=project_id,
            space_id=space_id,
            browse_project_id=browse_project_id,
            browse_space_id=browse_space_id,
        )
        if not participating_project_ids:
            return []

        stmt = (
            select(TimeEntry)
            .join(Task)
            .options(
                selectinload(TimeEntry.task)
                .selectinload(Task.project)
                .selectinload(Project.space),
                selectinload(TimeEntry.user),
                selectinload(TimeEntry.occurrence),
            )
            .where(
                Task.project_id.in_(participating_project_ids),
                Task.deleted_at.is_(None),
                TimeEntry.deleted_at.is_(None),
            )
            .order_by(TimeEntry.started_at.desc())
        )
        if task_id is not None:
            stmt = stmt.where(TimeEntry.task_id == task_id)
        if active_only:
            stmt = stmt.where(
                TimeEntry.ended_at.is_(None), TimeEntry.user_id == user_id
            )
        if date_from:
            stmt = stmt.where(TimeEntry.started_at >= date_from)
        if date_to:
            stmt = stmt.where(TimeEntry.started_at <= date_to)

        result = await session.execute(stmt)
        return [self._build_time_entry_payload(entry) for entry in result.scalars().all()]

    async def get_time_report(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        project_id: Optional[UUID] = None,
        space_id: Optional[UUID] = None,
        browse_project_id: Optional[UUID | str] = None,
        browse_space_id: Optional[UUID | str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> dict[str, Any]:
        date_from = timer_db_datetime(date_from)
        date_to = timer_db_datetime(date_to)
        participating_project_ids = await self.resolve_read_project_ids(
            session,
            user_id=user_id,
            project_id=project_id,
            space_id=space_id,
            browse_project_id=browse_project_id,
            browse_space_id=browse_space_id,
        )
        if not participating_project_ids:
            return build_time_report([])

        stmt = (
            select(TimeEntry, Task)
            .join(Task, TimeEntry.task_id == Task.id)
            .options(
                selectinload(TimeEntry.user),
                selectinload(TimeEntry.task).selectinload(Task.project),
            )
            .where(
                Task.project_id.in_(participating_project_ids),
                Task.deleted_at.is_(None),
                TimeEntry.deleted_at.is_(None),
            )
        )
        if date_from:
            stmt = stmt.where(TimeEntry.started_at >= date_from)
        if date_to:
            stmt = stmt.where(TimeEntry.started_at <= date_to)

        result = await session.execute(stmt)
        rows = []
        for entry, task in result.fetchall():
            rows.append(
                {
                    "task_id": str(task.id),
                    "task_title": task.title,
                    "project_id": str(task.project_id),
                    "project_name": (
                        task.project.name if getattr(task, "project", None) else None
                    ),
                    "user_id": str(entry.user_id),
                    "username": entry.user.username if entry.user else None,
                    "display_name": entry.user.display_name if entry.user else None,
                    "started_at": entry.started_at,
                    "ended_at": entry.ended_at,
                }
            )
        return build_time_report(rows)
