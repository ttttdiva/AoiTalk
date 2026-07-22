"""Task management: 繰り返しルールとオカレンス生成。"""

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
from ...task_time import DEFAULT_TASK_TIMEZONE, normalize_task_timezone
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
    correct_likely_timer_started_at,
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


class OccurrenceMixin:
    """繰り返しルールとオカレンス生成。"""

    async def _sync_repeat_tag(
        self,
        session: AsyncSession,
        *,
        task: Task,
        has_recurrence: bool,
    ) -> None:
        """繰り返し設定の有無に応じて 'repeat' タグを自動付与または除去する。"""
        repeat_tag_id = await self._get_or_create_repeat_tag(
            session, project_id=task.project_id
        )

        result = await session.execute(
            select(TaskTag).where(
                TaskTag.task_id == task.id, TaskTag.tag_id == repeat_tag_id
            )
        )
        existing_link = result.scalar_one_or_none()

        if has_recurrence and existing_link is None:
            session.add(TaskTag(task_id=task.id, tag_id=repeat_tag_id))
        elif not has_recurrence and existing_link is not None:
            await session.delete(existing_link)

    async def _upsert_recurrence(
        self,
        session: AsyncSession,
        *,
        task: Task,
        recurrence_rrule: Optional[str],
        timezone: str = DEFAULT_TASK_TIMEZONE,
        horizon_days: int = 90,
        trigger_status: Optional[str] = None,
        create_new: Optional[bool] = None,
        recur_forever: Optional[bool] = None,
        reset_status_to: Optional[str] = None,
        end_count: Optional[int] = None,
        end_date: Optional[datetime] = None,
        skip_weekend: Optional[bool] = None,
        skip_holiday: Optional[bool] = None,
    ) -> Optional[TaskRecurrenceRule]:
        result = await session.execute(
            select(TaskRecurrenceRule).where(TaskRecurrenceRule.task_id == task.id)
        )
        existing = result.scalar_one_or_none()
        if recurrence_rrule:
            if existing is None:
                existing = TaskRecurrenceRule(
                    task_id=task.id,
                    rrule=recurrence_rrule,
                    timezone=normalize_task_timezone(timezone),
                    horizon_days=horizon_days,
                    trigger_status=normalize_task_status(trigger_status or "closed"),
                    create_new=bool(create_new) if create_new is not None else False,
                    recur_forever=(
                        bool(recur_forever) if recur_forever is not None else True
                    ),
                    reset_status_to=normalize_task_status(reset_status_to or "open"),
                    end_count=end_count,
                    end_date=end_date,
                    skip_weekend=(
                        bool(skip_weekend) if skip_weekend is not None else False
                    ),
                    skip_holiday=(
                        bool(skip_holiday) if skip_holiday is not None else False
                    ),
                )
                session.add(existing)
            else:
                existing.rrule = recurrence_rrule
                existing.timezone = normalize_task_timezone(
                    timezone or existing.timezone
                )
                existing.horizon_days = horizon_days
                if trigger_status is not None:
                    existing.trigger_status = normalize_task_status(trigger_status)
                if create_new is not None:
                    existing.create_new = bool(create_new)
                if recur_forever is not None:
                    existing.recur_forever = bool(recur_forever)
                if reset_status_to is not None:
                    existing.reset_status_to = normalize_task_status(reset_status_to)
                if end_count is not None:
                    existing.end_count = end_count
                if end_date is not None:
                    existing.end_date = end_date
                if skip_weekend is not None:
                    existing.skip_weekend = bool(skip_weekend)
                if skip_holiday is not None:
                    existing.skip_holiday = bool(skip_holiday)
        elif existing is not None:
            await session.delete(existing)
            existing = None
        return existing

    async def _materialize_occurrences(
        self,
        session: AsyncSession,
        task: Task,
        *,
        recurrence_rrule: Optional[str] = None,
        horizon_days: int = 90,
    ) -> None:
        schedule = build_occurrence_schedule(
            start_at=task.start_at,
            end_at=task.end_at,
            recurrence_rrule=recurrence_rrule,
            horizon_days=horizon_days,
        )
        expected_starts = {occurrence.start_at: occurrence for occurrence in schedule}

        result = await session.execute(
            select(TaskOccurrence).where(TaskOccurrence.task_id == task.id)
        )
        existing_occurrences = result.scalars().all()

        for occurrence in existing_occurrences:
            planned = expected_starts.pop(occurrence.start_at, None)
            if planned is None:
                await session.delete(occurrence)
                continue
            occurrence.end_at = planned.end_at
            occurrence.source_kind = planned.source_kind
            occurrence.is_generated = planned.is_generated
            occurrence.all_day = task.all_day
            occurrence.reminder_offsets = task.reminder_offsets
            if recurrence_rrule is None:
                occurrence.status = task.status

        for planned in expected_starts.values():
            session.add(
                TaskOccurrence(
                    task_id=task.id,
                    start_at=planned.start_at,
                    end_at=planned.end_at,
                    status=task.status,
                    all_day=task.all_day,
                    reminder_offsets=task.reminder_offsets,
                    source_kind=planned.source_kind,
                    is_generated=planned.is_generated,
                )
            )

    async def list_occurrences(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        project_id: Optional[UUID] = None,
        space_id: Optional[UUID] = None,
        start_from: Optional[datetime] = None,
        end_to: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        accessible_project_ids = await self._get_accessible_project_ids(
            session, user_id
        )
        if project_id is not None:
            await self.require_project_permission(
                session, project_id=project_id, user_id=user_id, permission="read"
            )
            accessible_project_ids = [project_id]
        elif space_id is not None:
            accessible_project_ids = await self._filter_project_ids_by_space(
                session, project_ids=accessible_project_ids, space_id=space_id
            )
        if not accessible_project_ids:
            return []

        stmt = (
            select(TaskOccurrence)
            .join(Task)
            .options(
                selectinload(TaskOccurrence.task).selectinload(Task.project),
                selectinload(TaskOccurrence.task)
                .selectinload(Task.task_tags)
                .selectinload(TaskTag.tag),
            )
            .where(
                Task.project_id.in_(accessible_project_ids),
                Task.deleted_at.is_(None),
                TaskOccurrence.deleted_at.is_(None),
            )
            .order_by(TaskOccurrence.start_at.asc())
        )
        if start_from:
            stmt = stmt.where(TaskOccurrence.end_at >= start_from)
        if end_to:
            stmt = stmt.where(TaskOccurrence.start_at <= end_to)

        result = await session.execute(stmt)
        return [occurrence.to_dict() for occurrence in result.scalars().all()]

    async def update_occurrence(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        occurrence_id: UUID,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        result = await session.execute(
            select(TaskOccurrence)
            .options(
                selectinload(TaskOccurrence.task).selectinload(Task.recurrence_rule)
            )
            .where(
                TaskOccurrence.id == occurrence_id, TaskOccurrence.deleted_at.is_(None)
            )
        )
        occurrence = result.scalar_one_or_none()
        if occurrence is None or occurrence.task is None:
            raise TaskManagementError("Occurrence not found", status_code=404)

        task = occurrence.task
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )

        if "status" in updates and updates["status"] is not None:
            occurrence.status = normalize_task_status(updates["status"])
            if task.recurrence_rule is None:
                task.status = occurrence.status
                task.completed_at = (
                    datetime.utcnow() if occurrence.status == "closed" else None
                )

        shifted = False
        if "start_at" in updates and updates["start_at"] is not None:
            new_start_at = updates["start_at"]
            duration = occurrence.end_at - occurrence.start_at
            delta = new_start_at - occurrence.start_at
            occurrence.start_at = new_start_at
            occurrence.end_at = updates.get("end_at") or (new_start_at + duration)
            shifted = True
            if task.recurrence_rule is None:
                task.start_at = occurrence.start_at
                task.end_at = occurrence.end_at
            else:
                task.start_at = (task.start_at or occurrence.start_at) + delta
                task.end_at = (task.end_at or occurrence.end_at) + delta
                await self._materialize_occurrences(session, task)

        if "end_at" in updates and updates["end_at"] is not None and not shifted:
            occurrence.end_at = updates["end_at"]
            if task.recurrence_rule is None:
                task.end_at = occurrence.end_at

        if "reminder_offsets" in updates and updates["reminder_offsets"] is not None:
            occurrence.reminder_offsets = _ensure_reminder_offsets(
                updates["reminder_offsets"],
                default=[],
            )

        await self._record_activity(
            session,
            task_id=task.id,
            activity_type="occurrence_updated",
            user_id=user_id,
            payload={
                key: str(value) for key, value in updates.items() if value is not None
            },
        )
        await session.commit()
        await session.refresh(occurrence)
        payload = occurrence.to_dict()
        await self._broadcast("task_occurrence_updated", payload)
        return payload

