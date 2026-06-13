"""Core task, calendar, timer, report, and notification services."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional
from uuid import UUID, uuid4

import httpx
from dateutil.rrule import rrulestr
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..memory.models import (
    NotificationDelivery,
    Project,
    ProjectNotificationSetting,
    ProjectMember,
    Space,
    Tag,
    Task,
    TaskActivity,
    TaskAssignee,
    TaskComment,
    TaskOccurrence,
    TaskRecurrenceRule,
    TaskTag,
    TimeEntry,
    User,
)
from ..memory.project_repository import ProjectRepository
from ..task_time import DEFAULT_TASK_TIMEZONE, normalize_task_timezone
from .project_color_service import extract_project_color

VALID_TASK_STATUSES = {
    "todo",
    "open",
    "in_progress",
    "blocked",
    "on_hold",
    "review",
    "closed",
    "cancelled",
}
VALID_PRIORITIES = {"low", "medium", "high", "urgent"}
DEFAULT_USER_NOTIFICATION_MINUTES = 5
DISALLOWED_PLACEHOLDER_TITLES = {"無題のタスク", "Untitled task"}
DEFAULT_MEMBER_PERMISSIONS = {
    "owner": {
        "read": True,
        "write": True,
        "delete": True,
        "manage_members": True,
        "manage_settings": True,
    },
    "admin": {
        "read": True,
        "write": True,
        "delete": True,
        "manage_members": True,
        "manage_settings": False,
    },
    "member": {
        "read": True,
        "write": True,
        "delete": False,
        "manage_members": False,
        "manage_settings": False,
    },
    "viewer": {
        "read": True,
        "write": False,
        "delete": False,
        "manage_members": False,
        "manage_settings": False,
    },
}
LEGACY_STATUS_MAP = {
    "todo": "todo",
    "open": "open",
    "in_progress": "in_progress",
    "paused": "on_hold",
    "blocked": "blocked",
    "on_hold": "on_hold",
    "review": "review",
    "done": "closed",
    "closed": "closed",
}

logger = logging.getLogger(__name__)


class TaskManagementError(Exception):
    """Task management domain error with HTTP-like status information."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class ScheduledOccurrence:
    """Pure representation of an occurrence window."""

    start_at: datetime
    end_at: datetime
    is_generated: bool
    source_kind: str


def normalize_task_status(status: str) -> str:
    """Normalize legacy and public task statuses into the current enum."""
    normalized = (status or "").strip().lower()
    mapped = LEGACY_STATUS_MAP.get(normalized, normalized)
    if mapped not in VALID_TASK_STATUSES:
        raise TaskManagementError(f"Invalid status: {status}", status_code=400)
    return mapped


def normalize_priority(priority: Optional[str]) -> str:
    """Normalize priority values."""
    normalized = (priority or "medium").strip().lower()
    if normalized == "normal":
        normalized = "medium"
    if normalized not in VALID_PRIORITIES:
        raise TaskManagementError(f"Invalid priority: {priority}", status_code=400)
    return normalized


def _ensure_reminder_offsets(
    reminder_offsets: Optional[Iterable[Any]],
    *,
    default: Optional[Iterable[int]] = None,
) -> list[int]:
    if reminder_offsets is None:
        return list(default or [])

    normalized: list[int] = []
    for offset in reminder_offsets:
        try:
            value = int(offset)
        except (TypeError, ValueError) as exc:
            raise TaskManagementError(f"Invalid reminder offset: {offset}") from exc
        if value < 0:
            raise TaskManagementError("Reminder offsets must be >= 0")
        normalized.append(value)

    unique_sorted = sorted(set(normalized))
    return unique_sorted or list(default or [])


def _normalize_task_title(title: Optional[str]) -> str:
    normalized = (title or "").strip()
    if not normalized:
        raise TaskManagementError("title is required", status_code=400)
    if normalized in DISALLOWED_PLACEHOLDER_TITLES:
        raise TaskManagementError(
            "placeholder task titles are not allowed", status_code=400
        )
    return normalized


def _strip_google_calendar_metadata(
    metadata: Optional[dict[str, Any]],
) -> dict[str, Any]:
    cleaned = dict(metadata or {})
    cleaned.pop("google_calendar", None)
    return cleaned


def _get_user_notification_minutes(user: Optional[User]) -> int:
    raw = None
    if user is not None:
        raw = (user.user_settings or {}).get("task_notification_minutes_before")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_USER_NOTIFICATION_MINUTES
    return value if value >= 0 else DEFAULT_USER_NOTIFICATION_MINUTES


def _get_user_task_notifications_default_enabled(user: Optional[User]) -> bool:
    if user is None:
        return True
    raw = (user.user_settings or {}).get("task_notifications_default_enabled")
    return raw if isinstance(raw, bool) else True


def _is_midnight(value: Optional[datetime]) -> bool:
    return (
        value is not None
        and value.hour == 0
        and value.minute == 0
        and value.second == 0
        and value.microsecond == 0
    )


def _is_date_only_occurrence(occurrence: TaskOccurrence, task: Task) -> bool:
    if occurrence.all_day or task.all_day:
        return True
    if occurrence.start_at and occurrence.end_at:
        return _is_midnight(occurrence.start_at) and _is_midnight(occurrence.end_at)
    return _is_midnight(occurrence.start_at or occurrence.end_at)


def _normalize_member_permissions(role: str, permissions: Any) -> dict[str, bool]:
    if isinstance(permissions, dict):
        return permissions
    if isinstance(permissions, str):
        try:
            parsed = json.loads(permissions)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return DEFAULT_MEMBER_PERMISSIONS.get(
        role, DEFAULT_MEMBER_PERMISSIONS["member"]
    ).copy()


def build_occurrence_schedule(
    *,
    start_at: Optional[datetime],
    end_at: Optional[datetime],
    recurrence_rrule: Optional[str],
    horizon_days: int = 90,
    base_now: Optional[datetime] = None,
) -> list[ScheduledOccurrence]:
    """Pure helper that expands a task schedule into materialized occurrences."""
    if not start_at or not end_at:
        return []
    if end_at <= start_at:
        raise TaskManagementError("end_at must be after start_at", status_code=400)

    if not recurrence_rrule:
        return [
            ScheduledOccurrence(
                start_at=start_at,
                end_at=end_at,
                is_generated=False,
                source_kind="task_schedule",
            )
        ]

    duration = end_at - start_at
    now = base_now or datetime.utcnow()
    window_start = min(start_at, now - duration)
    window_end = now + timedelta(days=horizon_days)

    try:
        rule = rrulestr(recurrence_rrule, dtstart=start_at)
    except Exception as exc:
        raise TaskManagementError(f"Invalid recurrence rule: {exc}") from exc

    starts = list(rule.between(window_start, window_end, inc=True))
    if start_at not in starts and start_at <= window_end:
        starts.insert(0, start_at)

    unique_starts = sorted(set(starts))
    return [
        ScheduledOccurrence(
            start_at=occurrence_start,
            end_at=occurrence_start + duration,
            is_generated=True,
            source_kind="recurrence",
        )
        for occurrence_start in unique_starts
    ]


_TIMER_UTC_SKEW_MIN = timedelta(hours=8)
_TIMER_UTC_SKEW_MAX = timedelta(hours=10)


def correct_likely_timer_started_at(
    started_at: Optional[datetime],
    created_at: Optional[datetime],
    source: Optional[str],
) -> Optional[datetime]:
    """timer 起動時に UTC が混入した started_at を created_at で補正する。

    Web BFF（frontend/src/lib/server/db-time.ts の correctLikelyTimerStartedAt）と
    同じ発見的補正。DB はローカル壁時計時刻で保存する規約のため、
    started_at と created_at の差が 8〜10 時間ある timer エントリは
    UTC で書かれた可能性が高く、created_at を開始時刻として扱う。
    """
    if (
        source == "timer"
        and started_at is not None
        and created_at is not None
        and _TIMER_UTC_SKEW_MIN <= (created_at - started_at) <= _TIMER_UTC_SKEW_MAX
    ):
        return created_at
    return started_at


def build_time_report(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Pure aggregation helper used by reports and tests."""
    summary = {
        "total_seconds": 0,
        "entry_count": 0,
        "active_entries": 0,
    }
    by_project: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, Any]] = {}
    by_user: dict[str, dict[str, Any]] = {}
    by_task: dict[str, dict[str, Any]] = {}

    def _bucket(
        target: dict[str, dict[str, Any]],
        key: str,
        label: str,
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if key not in target:
            target[key] = {
                "key": key,
                "label": label,
                "seconds": 0,
                "entries": 0,
                **(extra or {}),
            }
        return target[key]

    # DB はローカル壁時計時刻で保存しているため、実行中エントリの集計も
    # ローカル現在時刻で行う（utcnow だと JST 環境で 9 時間ずれる）。
    now = datetime.now()
    for entry in entries:
        started_at = entry.get("started_at")
        ended_at = entry.get("ended_at")
        if not isinstance(started_at, datetime):
            continue

        effective_end = ended_at if isinstance(ended_at, datetime) else now
        duration_seconds = max(0, int((effective_end - started_at).total_seconds()))
        summary["total_seconds"] += duration_seconds
        summary["entry_count"] += 1
        if ended_at is None:
            summary["active_entries"] += 1

        project_key = entry.get("project_id") or "unknown"
        project_label = entry.get("project_name") or "Unknown project"
        project_bucket = _bucket(
            by_project,
            project_key,
            project_label,
            extra={"project_id": project_key, "project_name": project_label},
        )
        project_bucket["seconds"] += duration_seconds
        project_bucket["entries"] += 1

        task_key = entry.get("task_id") or "unknown"
        task_label = entry.get("task_title") or "Unknown task"
        task_bucket = _bucket(
            by_task,
            task_key,
            task_label,
            extra={
                "project_id": entry.get("project_id"),
                "project_name": entry.get("project_name"),
            },
        )
        task_bucket["seconds"] += duration_seconds
        task_bucket["entries"] += 1

        user_key = entry.get("user_id") or "unknown"
        user_label = (
            entry.get("display_name") or entry.get("username") or "Unknown user"
        )
        user_bucket = _bucket(by_user, user_key, user_label)
        user_bucket["seconds"] += duration_seconds
        user_bucket["entries"] += 1

        day_key = started_at.date().isoformat()
        day_bucket = _bucket(by_day, day_key, day_key)
        day_bucket["seconds"] += duration_seconds
        day_bucket["entries"] += 1

    return {
        "summary": summary,
        "by_project": sorted(
            by_project.values(), key=lambda item: item["seconds"], reverse=True
        ),
        "by_day": sorted(by_day.values(), key=lambda item: item["key"]),
        "by_user": sorted(
            by_user.values(), key=lambda item: item["seconds"], reverse=True
        ),
        "by_task": sorted(
            by_task.values(), key=lambda item: item["seconds"], reverse=True
        ),
    }


class TaskManagementService:
    """Stateful service for the task system."""

    def __init__(self, broadcaster=None):
        self._broadcaster = broadcaster

    async def _broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        if not self._broadcaster:
            return
        try:
            await self._broadcaster({"type": event_type, "data": data})
        except Exception:
            logger.exception("Task event broadcast failed: %s", event_type)

    async def _get_accessible_project_ids(
        self, session: AsyncSession, user_id: UUID
    ) -> list[UUID]:
        result = await session.execute(
            select(ProjectMember.project_id)
            .join(Project, Project.id == ProjectMember.project_id)
            .where(ProjectMember.user_id == user_id, Project.deleted_at.is_(None))
        )
        return list(result.scalars().all())

    async def _filter_project_ids_by_space(
        self,
        session: AsyncSession,
        *,
        project_ids: list[UUID],
        space_id: UUID,
    ) -> list[UUID]:
        if not project_ids:
            return []
        result = await session.execute(
            select(Project.id).where(
                Project.id.in_(project_ids),
                Project.space_id == space_id,
                Project.deleted_at.is_(None),
            )
        )
        return list(result.scalars().all())

    async def _ensure_inbox_membership(
        self, session: AsyncSession, user_id: UUID
    ) -> UUID:
        await ProjectRepository.ensure_user_inbox_setup(session, user_id)
        inbox_id = await ProjectRepository.get_user_inbox_project_id(session, user_id)
        if inbox_id is None:
            raise TaskManagementError(
                "Inbox project is not available for this user", status_code=503
            )
        membership = await ProjectRepository.get_member(session, inbox_id, user_id)
        if membership is None:
            await ProjectRepository.add_member(
                session,
                project_id=inbox_id,
                user_id=user_id,
                role="member",
            )
        return inbox_id

    async def _resolve_project_id(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        project_id: Optional[UUID],
        require_write: bool = True,
    ) -> UUID:
        if project_id is None:
            project_id = await self._ensure_inbox_membership(session, user_id)

        await self.require_project_permission(
            session,
            project_id=project_id,
            user_id=user_id,
            permission="write" if require_write else "read",
        )
        return project_id

    async def require_project_permission(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        user_id: UUID,
        permission: str,
    ) -> None:
        member = await ProjectRepository.get_member(session, project_id, user_id)
        project = await ProjectRepository.get_by_id(session, project_id)
        if project is None:
            raise TaskManagementError("Project not found", status_code=404)
        if member is None:
            raise TaskManagementError("Project access denied", status_code=403)

        if member.role in {"owner", "admin"}:
            return

        permissions = _normalize_member_permissions(member.role, member.permissions)
        if permission == "read" and permissions.get("read", False):
            return
        if permission == "write" and permissions.get("write", False):
            return
        if permission == "delete" and permissions.get("delete", False):
            return
        if permission == "manage_settings" and (
            permissions.get("manage_settings", False)
            or permissions.get("manage_notifications", False)
        ):
            return
        raise TaskManagementError("Project permission denied", status_code=403)

    async def _load_task(self, session: AsyncSession, task_id: UUID) -> Task:
        result = await session.execute(
            select(Task)
            .options(
                selectinload(Task.project),
                selectinload(Task.assignees).selectinload(TaskAssignee.user),
                selectinload(Task.comments).selectinload(TaskComment.user),
                selectinload(Task.activities).selectinload(TaskActivity.user),
                selectinload(Task.recurrence_rule),
                selectinload(Task.occurrences),
                selectinload(Task.time_entries).selectinload(TimeEntry.user),
                selectinload(Task.task_tags).selectinload(TaskTag.tag),
            )
            .where(Task.id == task_id, Task.deleted_at.is_(None))
        )
        task = result.scalar_one_or_none()
        if task is None:
            raise TaskManagementError("Task not found", status_code=404)
        return task

    def _build_time_entry_payload(self, entry: TimeEntry) -> dict[str, Any]:
        """TimeEntry.to_dict() に Web BFF 互換のフィールドを追加して返す。

        Web の time-entries 系レスポンス（project_color / space_id / space_name /
        original_started_at / original_ended_at とタイマー開始時刻補正）に合わせる。
        """
        data = entry.to_dict()
        task = entry.task
        project = task.project if task is not None else None
        space = project.space if project is not None else None

        if entry.ended_at is None:
            corrected = correct_likely_timer_started_at(
                entry.started_at, entry.created_at, entry.source
            )
            if corrected is not None and corrected is not entry.started_at:
                data["started_at"] = corrected.isoformat()

        data["project_color"] = (
            extract_project_color(project.project_metadata)
            if project is not None
            else None
        )
        data["space_id"] = str(space.id) if space is not None else None
        data["space_name"] = space.name if space is not None else None

        metadata = entry.entry_metadata or {}
        data["original_started_at"] = (
            metadata.get("original_started_at")
            if isinstance(metadata.get("original_started_at"), str)
            else None
        )
        data["original_ended_at"] = (
            metadata.get("original_ended_at")
            if isinstance(metadata.get("original_ended_at"), str)
            else None
        )
        return data

    async def _serialize_time_entry(
        self, session: AsyncSession, entry_id: UUID
    ) -> dict[str, Any]:
        result = await session.execute(
            select(TimeEntry)
            .options(
                selectinload(TimeEntry.task)
                .selectinload(Task.project)
                .selectinload(Project.space),
                selectinload(TimeEntry.user),
                selectinload(TimeEntry.occurrence),
            )
            .where(TimeEntry.id == entry_id)
        )
        entry = result.scalar_one()
        return self._build_time_entry_payload(entry)

    async def _replace_assignees(
        self,
        session: AsyncSession,
        *,
        task: Task,
        assignee_ids: list[UUID],
        assigned_by: UUID,
    ) -> None:
        await session.execute(
            delete(TaskAssignee).where(TaskAssignee.task_id == task.id)
        )
        unique_ids = []
        seen: set[UUID] = set()
        for assignee_id in assignee_ids:
            if assignee_id in seen:
                continue
            seen.add(assignee_id)
            unique_ids.append(assignee_id)

        if not unique_ids:
            unique_ids = [assigned_by]

        for index, assignee_id in enumerate(unique_ids):
            session.add(
                TaskAssignee(
                    task_id=task.id,
                    user_id=assignee_id,
                    is_primary=index == 0,
                    assigned_by=assigned_by,
                )
            )

    async def _replace_tags(
        self,
        session: AsyncSession,
        *,
        task: Task,
        tag_ids: list[UUID],
    ) -> None:
        await session.execute(delete(TaskTag).where(TaskTag.task_id == task.id))
        seen: set[UUID] = set()
        for tag_id in tag_ids:
            if tag_id in seen:
                continue
            seen.add(tag_id)
            session.add(TaskTag(task_id=task.id, tag_id=tag_id))

    async def _get_or_create_repeat_tag(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
    ) -> UUID:
        """スペース内の 'repeat' タグを取得し、なければ作成してIDを返す。"""
        space_id = await self._ensure_project_space_id(session, project_id=project_id)
        result = await session.execute(
            select(Tag).where(Tag.space_id == space_id, Tag.name == "repeat")
        )
        tag = result.scalar_one_or_none()
        if tag is None:
            tag = Tag(space_id=space_id, name="repeat", color="#6366f1")
            session.add(tag)
            await session.flush()
        return tag.id

    async def _get_project_space_id(
        self, session: AsyncSession, *, project_id: UUID
    ) -> Optional[UUID]:
        result = await session.execute(
            select(Project.space_id).where(Project.id == project_id)
        )
        return result.scalar_one_or_none()

    async def _ensure_project_space_id(
        self, session: AsyncSession, *, project_id: UUID
    ) -> UUID:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if project is None:
            raise TaskManagementError("Project not found", status_code=404)
        if project.space_id is not None:
            return project.space_id

        slug = f"default-{project.owner_id}"
        existing = await session.execute(
            select(Space).where(Space.owner_id == project.owner_id, Space.slug == slug)
        )
        space = existing.scalar_one_or_none()
        if space is None:
            space = Space(
                name="Default",
                slug=slug,
                owner_id=project.owner_id,
                sort_order=0,
            )
            session.add(space)
            await session.flush()
        project.space_id = space.id
        await session.flush()
        return space.id

    async def _require_space_tag_permission(
        self,
        session: AsyncSession,
        *,
        space_id: UUID,
        user_id: UUID,
        permission: str,
    ) -> None:
        result = await session.execute(
            select(Project.id)
            .join(ProjectMember, ProjectMember.project_id == Project.id)
            .where(
                Project.space_id == space_id,
                ProjectMember.user_id == user_id,
                Project.deleted_at.is_(None),
            )
        )
        for project_id in result.scalars().all():
            try:
                await self.require_project_permission(
                    session,
                    project_id=project_id,
                    user_id=user_id,
                    permission=permission,
                )
                return
            except TaskManagementError:
                continue
        raise TaskManagementError("Project permission denied", status_code=403)

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

    async def _record_activity(
        self,
        session: AsyncSession,
        *,
        task_id: UUID,
        activity_type: str,
        user_id: Optional[UUID],
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        session.add(
            TaskActivity(
                task_id=task_id,
                user_id=user_id,
                activity_type=activity_type,
                payload=payload or {},
            )
        )

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

    async def create_task(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        title: str,
        description: Optional[str] = None,
        project_id: Optional[UUID] = None,
        status: str = "todo",
        priority: Optional[str] = None,
        start_at: Optional[datetime] = None,
        end_at: Optional[datetime] = None,
        all_day: bool = False,
        reminder_offsets: Optional[Iterable[Any]] = None,
        notifications_enabled: Optional[bool] = None,
        assignee_ids: Optional[list[UUID]] = None,
        tag_ids: Optional[list[UUID]] = None,
        recurrence_rrule: Optional[str] = None,
        recurrence_timezone: str = DEFAULT_TASK_TIMEZONE,
        task_metadata: Optional[dict[str, Any]] = None,
        source: str = "local",
        legacy_local_task_id: Optional[UUID] = None,
        task_id: Optional[UUID] = None,
    ) -> dict[str, Any]:
        normalized_title = _normalize_task_title(title)
        normalized_status = normalize_task_status(status)
        normalized_priority = normalize_priority(priority)
        normalized_reminders = _ensure_reminder_offsets(reminder_offsets, default=[])
        target_project_id = await self._resolve_project_id(
            session,
            user_id=user_id,
            project_id=project_id,
            require_write=True,
        )
        if notifications_enabled is None:
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            notifications_enabled = _get_user_task_notifications_default_enabled(user)
        min_sort_result = await session.execute(
            select(func.min(Task.sort_order)).where(
                Task.project_id == target_project_id,
                Task.parent_task_id.is_(None),
                Task.deleted_at.is_(None),
            )
        )
        next_sort_order = float(min_sort_result.scalar_one_or_none() or 0) - 1

        task = Task(
            id=task_id or uuid4(),
            project_id=target_project_id,
            legacy_local_task_id=legacy_local_task_id,
            title=normalized_title,
            description=description,
            status=normalized_status,
            priority=normalized_priority,
            start_at=start_at,
            end_at=end_at,
            all_day=all_day,
            reminder_offsets=normalized_reminders,
            notifications_enabled=bool(notifications_enabled),
            source=source,
            created_by=user_id,
            completed_at=datetime.utcnow() if normalized_status == "closed" else None,
            task_metadata=_strip_google_calendar_metadata(task_metadata),
            sort_order=next_sort_order,
        )
        session.add(task)
        await session.flush()

        recurrence = await self._upsert_recurrence(
            session,
            task=task,
            recurrence_rrule=recurrence_rrule,
            timezone=recurrence_timezone,
        )
        await self._replace_assignees(
            session,
            task=task,
            assignee_ids=list(assignee_ids or []),
            assigned_by=user_id,
        )
        if tag_ids:
            await self._replace_tags(session, task=task, tag_ids=tag_ids)
        await self._sync_repeat_tag(
            session, task=task, has_recurrence=recurrence is not None
        )
        await self._materialize_occurrences(
            session,
            task,
            recurrence_rrule=recurrence.rrule if recurrence else None,
            horizon_days=recurrence.horizon_days if recurrence else 90,
        )
        await self._record_activity(
            session,
            task_id=task.id,
            activity_type="task_created",
            user_id=user_id,
            payload={"project_id": str(target_project_id)},
        )

        await session.commit()
        task = await self._load_task(session, task.id)
        await self._broadcast("task_created", task.to_dict())
        return task.to_dict()

    async def list_tasks(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        project_id: Optional[UUID] = None,
        space_id: Optional[UUID] = None,
        status: Optional[str] = None,
        assignee_id: Optional[UUID] = None,
        search: Optional[str] = None,
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
            select(Task)
            .options(
                selectinload(Task.project),
                selectinload(Task.assignees).selectinload(TaskAssignee.user),
                selectinload(Task.recurrence_rule),
                selectinload(Task.task_tags).selectinload(TaskTag.tag),
            )
            .where(
                Task.project_id.in_(accessible_project_ids),
                Task.archived_at.is_(None),
                Task.deleted_at.is_(None),
            )
            .order_by(
                Task.project_id.asc(),
                Task.parent_task_id.asc().nulls_first(),
                Task.sort_order.asc().nulls_last(),
                Task.created_at.asc(),
                Task.id.asc(),
            )
        )

        if status:
            stmt = stmt.where(Task.status == normalize_task_status(status))
        if search:
            like_term = f"%{search.strip()}%"
            stmt = stmt.where(
                or_(Task.title.ilike(like_term), Task.description.ilike(like_term))
            )
        if assignee_id:
            stmt = stmt.join(TaskAssignee).where(TaskAssignee.user_id == assignee_id)

        result = await session.execute(stmt)
        return [task.to_dict() for task in result.scalars().unique().all()]

    async def delete_task(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: UUID,
    ) -> None:
        task = await self._load_task(session, task_id)
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )
        now = datetime.utcnow()
        task.deleted_at = now
        task.updated_at = now
        await session.execute(
            update(TaskOccurrence)
            .where(
                TaskOccurrence.task_id == task.id, TaskOccurrence.deleted_at.is_(None)
            )
            .values(deleted_at=now, updated_at=now)
        )
        await session.execute(
            update(TimeEntry)
            .where(TimeEntry.task_id == task.id, TimeEntry.deleted_at.is_(None))
            .values(deleted_at=now, updated_at=now)
        )
        await session.commit()
        await self._broadcast("task_deleted", {"task_id": str(task_id)})

    async def reorder_tasks(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        project_id: UUID,
        task_ids: list[UUID],
    ) -> None:
        await self.require_project_permission(
            session, project_id=project_id, user_id=user_id, permission="write"
        )
        result = await session.execute(
            select(Task.id).where(
                Task.project_id == project_id,
                Task.parent_task_id.is_(None),
                Task.deleted_at.is_(None),
            )
        )
        top_level_ids = set(result.scalars().all())
        requested_ids = set(task_ids)
        if top_level_ids != requested_ids:
            raise TaskManagementError(
                "task_ids must include every top-level task in the project",
                status_code=409,
            )

        for index, task_id in enumerate(task_ids):
            await session.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.project_id == project_id,
                    Task.parent_task_id.is_(None),
                )
                .values(sort_order=float(index))
            )
        await session.commit()

    async def reorder_tasks_global(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_ids: list[UUID],
    ) -> None:
        """ALL 表示のトップレベルタスク並び替え（プロジェクト横断）。

        Web BFF の POST /api/tasks/reorder と同じ契約:
        - 重複は除去し、トップレベルかつ未削除のタスクのみ許可
        - 対象タスクが属する全プロジェクトに write 権限が必要
        """
        unique_ids: list[UUID] = []
        seen: set[UUID] = set()
        for task_id in task_ids:
            if task_id in seen:
                continue
            seen.add(task_id)
            unique_ids.append(task_id)
        if not unique_ids:
            return

        result = await session.execute(
            select(Task).where(
                Task.id.in_(unique_ids),
                Task.parent_task_id.is_(None),
                Task.deleted_at.is_(None),
            )
        )
        tasks_by_id = {task.id: task for task in result.scalars().all()}
        if len(tasks_by_id) != len(unique_ids):
            raise TaskManagementError(
                "task_ids contains a non top-level or missing task",
                status_code=400,
            )

        for project_id in {task.project_id for task in tasks_by_id.values()}:
            await self.require_project_permission(
                session, project_id=project_id, user_id=user_id, permission="write"
            )

        for index, task_id in enumerate(unique_ids):
            tasks_by_id[task_id].sort_order = float(index)
        await session.commit()

    async def get_task(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: UUID,
    ) -> dict[str, Any]:
        task = await self._load_task(session, task_id)
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="read"
        )

        active_entry = await self.get_active_time_entry(
            session, user_id=user_id, task_id=task.id
        )
        result = task.to_dict()
        result["comments"] = [comment.to_dict() for comment in task.comments]
        result["activities"] = [activity.to_dict() for activity in task.activities]
        result["occurrences"] = [
            occurrence.to_dict()
            for occurrence in sorted(task.occurrences, key=lambda item: item.start_at)
        ]
        result["time_entries"] = [entry.to_dict() for entry in task.time_entries]
        result["active_time_entry"] = active_entry
        return result

    async def update_task(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: UUID,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        task = await self._load_task(session, task_id)
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )

        if "project_id" in updates and updates["project_id"] is not None:
            target_project_id = await self._resolve_project_id(
                session,
                user_id=user_id,
                project_id=updates["project_id"],
                require_write=True,
            )
            task.project_id = target_project_id

        if "title" in updates and updates["title"] is not None:
            task.title = _normalize_task_title(str(updates["title"]))
        if "description" in updates:
            task.description = updates["description"]
        if "status" in updates and updates["status"] is not None:
            task.status = normalize_task_status(updates["status"])
            if task.status == "closed":
                task.completed_at = datetime.utcnow()
            else:
                task.completed_at = None
        if "priority" in updates and updates["priority"] is not None:
            task.priority = normalize_priority(updates["priority"])
        if "start_at" in updates:
            task.start_at = updates["start_at"]
        if "end_at" in updates:
            task.end_at = updates["end_at"]
        if "all_day" in updates and updates["all_day"] is not None:
            task.all_day = bool(updates["all_day"])
        if (
            "notifications_enabled" in updates
            and updates["notifications_enabled"] is not None
        ):
            task.notifications_enabled = bool(updates["notifications_enabled"])
        if "reminder_offsets" in updates and updates["reminder_offsets"] is not None:
            task.reminder_offsets = _ensure_reminder_offsets(
                updates["reminder_offsets"],
                default=[],
            )
        if "task_metadata" in updates and updates["task_metadata"] is not None:
            merged_metadata = dict(task.task_metadata or {})
            merged_metadata.update(updates["task_metadata"])
            task.task_metadata = merged_metadata
        recurrence = await self._upsert_recurrence(
            session,
            task=task,
            recurrence_rrule=updates.get(
                "recurrence_rrule",
                task.recurrence_rule.rrule if task.recurrence_rule else None,
            ),
            timezone=updates.get(
                "recurrence_timezone",
                task.recurrence_rule.timezone
                if task.recurrence_rule
                else DEFAULT_TASK_TIMEZONE,
            ),
        )

        if "assignee_ids" in updates and updates["assignee_ids"] is not None:
            await self._replace_assignees(
                session,
                task=task,
                assignee_ids=list(updates["assignee_ids"]),
                assigned_by=user_id,
            )

        if "tag_ids" in updates and updates["tag_ids"] is not None:
            await self._replace_tags(
                session, task=task, tag_ids=list(updates["tag_ids"])
            )
        await self._sync_repeat_tag(
            session, task=task, has_recurrence=recurrence is not None
        )

        await self._materialize_occurrences(
            session,
            task,
            recurrence_rrule=recurrence.rrule if recurrence else None,
            horizon_days=recurrence.horizon_days if recurrence else 90,
        )
        await self._record_activity(
            session,
            task_id=task.id,
            activity_type="task_updated",
            user_id=user_id,
            payload={
                key: str(value) for key, value in updates.items() if value is not None
            },
        )
        await session.commit()
        task = await self._load_task(session, task.id)
        await self._broadcast("task_updated", task.to_dict())
        return task.to_dict()

    async def list_tags(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        await self.require_project_permission(
            session, project_id=project_id, user_id=user_id, permission="read"
        )
        space_id = await self._get_project_space_id(session, project_id=project_id)
        if space_id is None:
            return []
        result = await session.execute(
            select(Tag).where(Tag.space_id == space_id).order_by(Tag.name)
        )
        tags = []
        for tag in result.scalars().all():
            payload = tag.to_dict()
            payload["project_id"] = str(project_id)
            tags.append(payload)
        return tags

    async def create_tag(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        name: str,
        color: Optional[str],
        user_id: UUID,
    ) -> dict[str, Any]:
        await self.require_project_permission(
            session, project_id=project_id, user_id=user_id, permission="write"
        )
        name = name.strip()
        if not name:
            raise TaskManagementError("タグ名は必須です", status_code=400)
        space_id = await self._ensure_project_space_id(session, project_id=project_id)
        existing = await session.execute(
            select(Tag).where(Tag.space_id == space_id, Tag.name == name)
        )
        if existing.scalar_one_or_none() is not None:
            raise TaskManagementError("同名のタグが既に存在します", status_code=409)
        tag = Tag(space_id=space_id, name=name, color=color, created_by=user_id)
        session.add(tag)
        await session.commit()
        await session.refresh(tag)
        payload = tag.to_dict()
        payload["project_id"] = str(project_id)
        return payload

    async def delete_tag(
        self,
        session: AsyncSession,
        *,
        tag_id: UUID,
        user_id: UUID,
    ) -> None:
        result = await session.execute(select(Tag).where(Tag.id == tag_id))
        tag = result.scalar_one_or_none()
        if tag is None:
            raise TaskManagementError("タグが見つかりません", status_code=404)
        await self._require_space_tag_permission(
            session, space_id=tag.space_id, user_id=user_id, permission="write"
        )
        await session.delete(tag)
        await session.commit()

    async def add_comment(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: UUID,
        content: str,
    ) -> dict[str, Any]:
        task = await self._load_task(session, task_id)
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )

        comment = TaskComment(task_id=task.id, user_id=user_id, content=content.strip())
        session.add(comment)
        await self._record_activity(
            session,
            task_id=task.id,
            activity_type="comment_added",
            user_id=user_id,
            payload={"content": content.strip()},
        )
        await session.commit()
        await session.refresh(comment)
        await self._broadcast("task_comment_added", comment.to_dict())
        return comment.to_dict()

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

    async def get_active_time_entry(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: Optional[UUID] = None,
    ) -> Optional[dict[str, Any]]:
        stmt = (
            select(TimeEntry)
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
            select(TimeEntry).where(
                TimeEntry.user_id == user_id,
                TimeEntry.ended_at.is_(None),
                TimeEntry.deleted_at.is_(None),
            )
        )
        active_entry = active_result.scalar_one_or_none()
        # DB のローカル壁時計時刻規約に合わせる（Web BFF の localtimestamp と同じ）。
        now = datetime.now()
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
                select(TaskOccurrence).where(TaskOccurrence.id == occurrence_id)
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

        corrected_started_at = correct_likely_timer_started_at(
            entry.started_at, entry.created_at, entry.source
        )
        if corrected_started_at is not None and corrected_started_at != entry.started_at:
            entry.started_at = corrected_started_at
        # DB のローカル壁時計時刻規約に合わせる。
        entry.ended_at = datetime.now()
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
            entry.started_at = started_at
        if ended_at is not None:
            entry.ended_at = ended_at
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
        task_id: Optional[UUID] = None,
        active_only: bool = False,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
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
                Task.project_id.in_(accessible_project_ids),
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
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> dict[str, Any]:
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
            return build_time_report([])

        stmt = (
            select(TimeEntry, Task)
            .join(Task, TimeEntry.task_id == Task.id)
            .options(
                selectinload(TimeEntry.user),
                selectinload(TimeEntry.task).selectinload(Task.project),
            )
            .where(
                Task.project_id.in_(accessible_project_ids),
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
            started_at = entry.started_at
            if entry.ended_at is None:
                started_at = correct_likely_timer_started_at(
                    entry.started_at, entry.created_at, entry.source
                )
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
                    "started_at": started_at,
                    "ended_at": entry.ended_at,
                }
            )
        return build_time_report(rows)

    async def get_or_create_notification_setting(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
    ) -> ProjectNotificationSetting:
        result = await session.execute(
            select(ProjectNotificationSetting).where(
                ProjectNotificationSetting.project_id == project_id
            )
        )
        setting = result.scalar_one_or_none()
        if setting is None:
            setting = ProjectNotificationSetting(project_id=project_id)
            session.add(setting)
            await session.commit()
            await session.refresh(setting)
        return setting

    async def update_notification_setting(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        project_id: UUID,
        discord_webhook_url: Optional[str] = None,
        default_reminder_offsets: Optional[Iterable[Any]] = None,
        notify_overdue: Optional[bool] = None,
    ) -> dict[str, Any]:
        await self.require_project_permission(
            session,
            project_id=project_id,
            user_id=user_id,
            permission="manage_settings",
        )
        setting = await self.get_or_create_notification_setting(
            session, project_id=project_id
        )

        if discord_webhook_url is not None:
            setting.discord_webhook_url = discord_webhook_url.strip() or None
        if default_reminder_offsets is not None:
            setting.default_reminder_offsets = _ensure_reminder_offsets(
                default_reminder_offsets, default=[15]
            )
        if notify_overdue is not None:
            setting.notify_overdue = bool(notify_overdue)

        await session.commit()
        await session.refresh(setting)
        return setting.to_dict()

    async def get_user_notification_preferences(
        self, session: AsyncSession, *, user_id: UUID
    ) -> dict[str, Any]:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            raise TaskManagementError("User not found", status_code=404)
        return {
            "task_notification_minutes_before": _get_user_notification_minutes(user),
            "task_notifications_default_enabled": _get_user_task_notifications_default_enabled(
                user
            ),
        }

    async def update_user_notification_preferences(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_notification_minutes_before: Optional[Any] = None,
        task_notifications_default_enabled: Optional[bool] = None,
    ) -> dict[str, Any]:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            raise TaskManagementError("User not found", status_code=404)

        next_settings = dict(user.user_settings or {})
        if task_notification_minutes_before is not None:
            try:
                minutes = int(task_notification_minutes_before)
            except (TypeError, ValueError) as exc:
                raise TaskManagementError(
                    "task_notification_minutes_before must be an integer",
                    status_code=400,
                ) from exc
            if minutes < 0:
                raise TaskManagementError(
                    "task_notification_minutes_before must be >= 0",
                    status_code=400,
                )
            next_settings["task_notification_minutes_before"] = minutes
        if task_notifications_default_enabled is not None:
            next_settings["task_notifications_default_enabled"] = bool(
                task_notifications_default_enabled
            )

        user.user_settings = next_settings
        await session.flush()
        await session.commit()
        await session.refresh(user)
        return {
            "task_notification_minutes_before": _get_user_notification_minutes(user),
            "task_notifications_default_enabled": _get_user_task_notifications_default_enabled(
                user
            ),
        }

    async def list_notifications(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        unread_only: bool = False,
    ) -> list[dict[str, Any]]:
        stmt = (
            select(NotificationDelivery)
            .where(
                NotificationDelivery.channel == "in_app",
                NotificationDelivery.user_id == user_id,
            )
            .order_by(
                NotificationDelivery.scheduled_for.desc(),
                NotificationDelivery.created_at.desc(),
            )
        )
        if unread_only:
            stmt = stmt.where(NotificationDelivery.read_at.is_(None))

        result = await session.execute(stmt)
        return [notification.to_dict() for notification in result.scalars().all()]

    async def mark_notification_read(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        notification_id: UUID,
    ) -> dict[str, Any]:
        result = await session.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.id == notification_id,
                NotificationDelivery.channel == "in_app",
                NotificationDelivery.user_id == user_id,
            )
        )
        notification = result.scalar_one_or_none()
        if notification is None:
            raise TaskManagementError("Notification not found", status_code=404)

        notification.read_at = datetime.utcnow()
        notification.status = "read"
        await session.commit()
        await session.refresh(notification)
        return notification.to_dict()

    async def mark_all_notifications_read(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
    ) -> int:
        """未読通知を一括既読化し、更新件数を返す（Web BFF の read-all と同契約）。"""
        result = await session.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.user_id == user_id,
                NotificationDelivery.read_at.is_(None),
            )
        )
        notifications = result.scalars().all()
        now = datetime.utcnow()
        for notification in notifications:
            notification.read_at = now
            notification.updated_at = now
        await session.commit()
        return len(notifications)

    async def _create_notification_if_missing(
        self,
        session: AsyncSession,
        *,
        dedupe_key: str,
        project_id: UUID,
        task_id: Optional[UUID],
        occurrence_id: Optional[UUID],
        user_id: Optional[UUID],
        channel: str,
        notification_type: str,
        title: str,
        message: str,
        scheduled_for: datetime,
        payload: Optional[dict[str, Any]] = None,
    ) -> Optional[NotificationDelivery]:
        existing = await session.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.dedupe_key == dedupe_key
            )
        )
        if existing.scalar_one_or_none() is not None:
            return None

        delivery = NotificationDelivery(
            project_id=project_id,
            task_id=task_id,
            occurrence_id=occurrence_id,
            user_id=user_id,
            channel=channel,
            notification_type=notification_type,
            dedupe_key=dedupe_key,
            title=title,
            message=message,
            scheduled_for=scheduled_for,
            payload=payload or {},
        )
        session.add(delivery)
        await session.flush()
        return delivery

    async def deliver_due_notifications(
        self, session: AsyncSession, *, now: Optional[datetime] = None
    ) -> dict[str, int]:
        current_time = now or datetime.utcnow()
        scan_from = current_time - timedelta(days=1)
        scan_to = current_time + timedelta(minutes=15)

        occurrence_result = await session.execute(
            select(TaskOccurrence)
            .options(
                selectinload(TaskOccurrence.task)
                .selectinload(Task.assignees)
                .selectinload(TaskAssignee.user),
                selectinload(TaskOccurrence.task).selectinload(Task.project),
            )
            .where(
                TaskOccurrence.end_at >= scan_from, TaskOccurrence.start_at <= scan_to
            )
        )
        occurrences = occurrence_result.scalars().all()
        stats = {"created": 0, "delivered": 0, "failed": 0}

        for occurrence in occurrences:
            if occurrence.task is None:
                continue
            task = occurrence.task
            if _is_date_only_occurrence(occurrence, task):
                continue
            setting = await self.get_or_create_notification_setting(
                session, project_id=task.project_id
            )
            recipients = [assignee.user_id for assignee in task.assignees] or (
                [task.created_by] if task.created_by else []
            )

            if task.notifications_enabled:
                explicit_offsets = list(occurrence.reminder_offsets or [])
                title = f"Upcoming: {task.title}"
                message = f"{task.title} starts at {occurrence.start_at.isoformat()}"

                for assignee in task.assignees:
                    user_offsets = explicit_offsets or [
                        _get_user_notification_minutes(assignee.user)
                    ]
                    for offset in user_offsets:
                        trigger_at = occurrence.start_at - timedelta(
                            minutes=int(offset)
                        )
                        if trigger_at > current_time:
                            continue
                        dedupe_key = f"reminder:{occurrence.id}:offset:{offset}:user:{assignee.user_id}"
                        delivery = await self._create_notification_if_missing(
                            session,
                            dedupe_key=dedupe_key,
                            project_id=task.project_id,
                            task_id=task.id,
                            occurrence_id=occurrence.id,
                            user_id=assignee.user_id,
                            channel="in_app",
                            notification_type="reminder",
                            title=title,
                            message=message,
                            scheduled_for=trigger_at,
                            payload={"offset_minutes": int(offset)},
                        )
                        if delivery:
                            stats["created"] += 1

                if task.created_by and not task.assignees:
                    owner_result = await session.execute(
                        select(User).where(User.id == task.created_by)
                    )
                    owner = owner_result.scalar_one_or_none()
                    owner_offsets = explicit_offsets or [
                        _get_user_notification_minutes(owner)
                    ]
                    for offset in owner_offsets:
                        trigger_at = occurrence.start_at - timedelta(
                            minutes=int(offset)
                        )
                        if trigger_at > current_time:
                            continue
                        dedupe_key = f"reminder:{occurrence.id}:offset:{offset}:user:{task.created_by}"
                        delivery = await self._create_notification_if_missing(
                            session,
                            dedupe_key=dedupe_key,
                            project_id=task.project_id,
                            task_id=task.id,
                            occurrence_id=occurrence.id,
                            user_id=task.created_by,
                            channel="in_app",
                            notification_type="reminder",
                            title=title,
                            message=message,
                            scheduled_for=trigger_at,
                            payload={"offset_minutes": int(offset)},
                        )
                        if delivery:
                            stats["created"] += 1

                discord_offsets = explicit_offsets or (
                    setting.default_reminder_offsets or [15]
                )
                for offset in discord_offsets:
                    trigger_at = occurrence.start_at - timedelta(minutes=int(offset))
                    if trigger_at > current_time or not setting.discord_webhook_url:
                        continue
                    dedupe_key = f"reminder:{occurrence.id}:offset:{offset}:discord"
                    delivery = await self._create_notification_if_missing(
                        session,
                        dedupe_key=dedupe_key,
                        project_id=task.project_id,
                        task_id=task.id,
                        occurrence_id=occurrence.id,
                        user_id=None,
                        channel="discord_webhook",
                        notification_type="reminder",
                        title=title,
                        message=message,
                        scheduled_for=trigger_at,
                        payload={"offset_minutes": int(offset)},
                    )
                    if delivery:
                        stats["created"] += 1

            if (
                task.notifications_enabled
                and recipients
                and setting.notify_overdue
                and occurrence.status not in {"closed", "cancelled"}
                and occurrence.end_at <= current_time
            ):
                title = f"Overdue: {task.title}"
                message = (
                    f"{task.title} should have ended at {occurrence.end_at.isoformat()}"
                )
                for recipient_id in recipients:
                    dedupe_key = f"overdue:{occurrence.id}:user:{recipient_id}"
                    delivery = await self._create_notification_if_missing(
                        session,
                        dedupe_key=dedupe_key,
                        project_id=task.project_id,
                        task_id=task.id,
                        occurrence_id=occurrence.id,
                        user_id=recipient_id,
                        channel="in_app",
                        notification_type="overdue",
                        title=title,
                        message=message,
                        scheduled_for=occurrence.end_at,
                    )
                    if delivery:
                        stats["created"] += 1
                if setting.discord_webhook_url:
                    dedupe_key = f"overdue:{occurrence.id}:discord"
                    delivery = await self._create_notification_if_missing(
                        session,
                        dedupe_key=dedupe_key,
                        project_id=task.project_id,
                        task_id=task.id,
                        occurrence_id=occurrence.id,
                        user_id=None,
                        channel="discord_webhook",
                        notification_type="overdue",
                        title=title,
                        message=message,
                        scheduled_for=occurrence.end_at,
                    )
                    if delivery:
                        stats["created"] += 1

        await session.commit()

        pending_result = await session.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.status == "pending",
                NotificationDelivery.scheduled_for <= current_time,
            )
        )
        pending_notifications = pending_result.scalars().all()

        for notification in pending_notifications:
            if notification.channel == "in_app":
                notification.delivered_at = current_time
                notification.status = "delivered"
                stats["delivered"] += 1
                await self._broadcast("notification_created", notification.to_dict())
                continue

            if notification.channel == "discord_webhook":
                setting = await self.get_or_create_notification_setting(
                    session, project_id=notification.project_id
                )
                webhook_url = setting.discord_webhook_url
                if not webhook_url:
                    notification.status = "failed"
                    stats["failed"] += 1
                    continue

                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        response = await client.post(
                            webhook_url,
                            json={
                                "content": f"**{notification.title}**\n{notification.message}"
                            },
                        )
                    response.raise_for_status()
                    notification.delivered_at = current_time
                    notification.status = "delivered"
                    stats["delivered"] += 1
                except Exception:
                    notification.status = "failed"
                    stats["failed"] += 1

        await session.commit()
        return stats
