"""Task management: 通知設定と配信。"""

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


class NotificationMixin:
    """通知設定と配信。"""

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
