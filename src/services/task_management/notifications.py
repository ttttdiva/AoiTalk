"""Task management: 通知設定と配信。"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...memory.models import (
    NotificationDelivery,
    WebPushSubscription,
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
from ..web_push import (
    WebPushEndpointError,
    get_web_push_public_key,
    is_expired_subscription_error,
    validate_web_push_endpoint,
    send_web_push,
)
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

_CLOSED_NOTIFICATION_STATUSES = {"closed", "done", "cancelled", "canceled"}
_DISCORD_WEBHOOK_HOSTS = {
    "discord.com",
    "canary.discord.com",
    "ptb.discord.com",
    # Discord used these domains for copied webhook URLs before discord.com.
    "discordapp.com",
    "canary.discordapp.com",
    "ptb.discordapp.com",
}
_DISCORD_WEBHOOK_PATH_RE = re.compile(
    r"^/api(?:/v(?:6|7|8|9|10))?/webhooks/[0-9]{17,20}/[A-Za-z0-9._-]{20,200}$"
)
_WEB_PUSH_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,512}$")
_WEB_PUSH_CONTENT_ENCODINGS = {"aes128gcm", "aesgcm"}
_DEFAULT_TASK_NOTIFICATION_LOOKAHEAD_MINUTES = 7 * 24 * 60


def _web_push_late_ttl() -> timedelta:
    try:
        seconds = int(os.getenv("AOITALK_WEB_PUSH_LATE_TTL_SECONDS", "900"))
    except (TypeError, ValueError):
        seconds = 900
    return timedelta(seconds=max(60, seconds))


_PUSH_LATE_TTL = _web_push_late_ttl()


def _task_notification_lookahead_minutes() -> int:
    try:
        minutes = int(
            os.getenv(
                "AOITALK_TASK_NOTIFICATION_LOOKAHEAD_MINUTES",
                str(_DEFAULT_TASK_NOTIFICATION_LOOKAHEAD_MINUTES),
            )
        )
    except (TypeError, ValueError):
        minutes = _DEFAULT_TASK_NOTIFICATION_LOOKAHEAD_MINUTES
    return max(15, minutes)


def _normalize_discord_webhook_url(value: str | None) -> str | None:
    """Validate and normalize a Discord-owned HTTPS webhook endpoint.

    The webhook token is a secret embedded in the URL path.  Exact host and
    path validation prevents user input or a legacy poisoned DB row from
    turning the notification worker into an SSRF client.
    """
    text = str(value or "").strip()
    if not text:
        return None

    def invalid() -> TaskManagementError:
        return TaskManagementError("Invalid Discord webhook URL", status_code=400)

    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise invalid()
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise invalid() from exc

    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or host not in _DISCORD_WEBHOOK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or not _DISCORD_WEBHOOK_PATH_RE.fullmatch(parsed.path)
    ):
        raise invalid()
    return f"https://{host}{parsed.path}"


def _normalize_web_push_endpoint(value: Any) -> str:
    try:
        from ..web_push import normalize_web_push_endpoint

        return normalize_web_push_endpoint(value)
    except WebPushEndpointError as exc:
        raise TaskManagementError("Invalid Web Push endpoint", status_code=400) from exc


def _normalize_web_push_key(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not _WEB_PUSH_KEY_RE.fullmatch(text):
        raise TaskManagementError(
            f"Invalid Web Push {field_name}", status_code=400
        )
    return text


def _normalize_web_push_expiration(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TaskManagementError(
            "Invalid Web Push expiration_time", status_code=400
        ) from exc
    if not (number > 0):
        return None
    # PushSubscription.expirationTime is a Unix timestamp in milliseconds.
    if number > 10_000_000_000:
        number /= 1000
    try:
        return datetime.utcfromtimestamp(number)
    except (OverflowError, OSError, ValueError) as exc:
        raise TaskManagementError(
            "Invalid Web Push expiration_time", status_code=400
        ) from exc


def _task_notification_now(now: Optional[datetime] = None) -> datetime:
    """Return the wall-clock time used by timestamp-without-time-zone task rows."""
    timezone = ZoneInfo(DEFAULT_TASK_TIMEZONE)
    if now is None:
        return datetime.now(timezone).replace(tzinfo=None)
    if now.tzinfo is not None:
        return now.astimezone(timezone).replace(tzinfo=None)
    return now


def _conflict_safe_insert(session: AsyncSession, model: type[Any]):
    """Build the native upsert statement for PostgreSQL and SQLite test DBs."""
    try:
        dialect_name = session.get_bind().dialect.name
    except (AttributeError, TypeError):
        dialect_name = "postgresql"
    insert_factory = sqlite_insert if dialect_name == "sqlite" else postgresql_insert
    return insert_factory(model)


def _should_skip_task_notification(task: Task, occurrence: TaskOccurrence) -> bool:
    task_status = str(task.status or "").lower()
    occurrence_status = str(occurrence.status or "").lower()
    return bool(
        task.archived_at
        or task.deleted_at
        or occurrence.deleted_at
        or occurrence.source_kind == "recurrence_skip"
        or task_status in _CLOSED_NOTIFICATION_STATUSES
        or occurrence_status in _CLOSED_NOTIFICATION_STATUSES
        or _is_date_only_occurrence(occurrence, task)
    )


def _should_skip_task_only_notification(task: Task) -> bool:
    """オカレンスを持たないタスク（= 非繰り返しタスク）の抑止判定。

    _should_skip_task_notification と同じ条件を tasks 行だけで評価する。
    Task も TaskOccurrence と同じ all_day / start_at / end_at を持つため、
    日付のみ（時刻なし）の判定は occurrence 版をそのまま流用できる。
    """
    task_status = str(task.status or "").lower()
    return bool(
        task.archived_at
        or task.deleted_at
        or task_status in _CLOSED_NOTIFICATION_STATUSES
        or _is_date_only_occurrence(task, task)
    )


def _normalize_reminder_offsets(values: Any) -> list[int]:
    """Return unique non-negative integer offsets without raising on legacy JSON."""

    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        values = [values]
    try:
        iterator = iter(values)
    except TypeError:
        iterator = iter([values])
    normalized: list[int] = []
    for raw in iterator:
        try:
            offset = int(raw)
        except (TypeError, ValueError):
            continue
        if offset < 0 or offset in normalized:
            continue
        normalized.append(offset)
    return normalized


def _notification_recipients(
    task: Any,
) -> tuple[list[UUID], dict[UUID, Any]]:
    """Return creator + assignees as a stable, de-duplicated recipient union."""

    recipients: list[UUID] = []
    users_by_id: dict[UUID, Any] = {}

    def add(user_id: Any, user: Any = None) -> None:
        if user_id is None and user is not None:
            user_id = getattr(user, "id", None)
        if user_id is None or user_id in recipients:
            if user_id is not None and user is not None:
                users_by_id[user_id] = user
            return
        recipients.append(user_id)
        if user is not None:
            users_by_id[user_id] = user

    creator = getattr(task, "creator", None)
    add(getattr(task, "created_by", None), creator)
    for assignee in getattr(task, "assignees", None) or []:
        user = getattr(assignee, "user", None)
        add(getattr(assignee, "user_id", None), user)
    return recipients, users_by_id


def _user_task_notification_offset(user: Any) -> int | None:
    """Read a recipient's setting without substituting the global default."""

    raw = (getattr(user, "user_settings", None) or {}).get(
        "task_notification_minutes_before"
    ) if user is not None else None
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _recipient_reminder_offsets(
    *,
    recipients: Iterable[UUID],
    users_by_id: Mapping[UUID, Any],
    explicit_offsets: Any,
) -> tuple[dict[UUID, list[int]], dict[UUID, str]]:
    """Resolve in-app reminder offsets independently for each recipient.

    Explicit task/occurrence offsets retain their existing precedence.  When
    they are absent, each recipient's user setting is authoritative; missing
    or invalid settings retain the legacy five-minute in-app contract.  The
    project setting is intentionally not used here: it controls Discord and
    task/occurrence fallback behavior, not an individual's inbox schedule.
    """

    task_offsets = _normalize_reminder_offsets(explicit_offsets)
    offsets_by_recipient: dict[UUID, list[int]] = {}
    source_by_recipient: dict[UUID, str] = {}
    for recipient_id in recipients:
        if task_offsets:
            offsets_by_recipient[recipient_id] = list(task_offsets)
            source_by_recipient[recipient_id] = "task"
            continue
        user_offset = _user_task_notification_offset(users_by_id.get(recipient_id))
        if user_offset is not None:
            offsets_by_recipient[recipient_id] = [user_offset]
            source_by_recipient[recipient_id] = "user"
        else:
            offsets_by_recipient[recipient_id] = [DEFAULT_USER_NOTIFICATION_MINUTES]
            source_by_recipient[recipient_id] = "user_default"
    return offsets_by_recipient, source_by_recipient


class NotificationMixin:
    """通知設定と配信。"""

    async def get_or_create_notification_setting(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        commit_if_created: bool = True,
    ) -> ProjectNotificationSetting:
        insert_result = await session.execute(
            _conflict_safe_insert(session, ProjectNotificationSetting)
            .values(project_id=project_id)
            .on_conflict_do_nothing(
                index_elements=[ProjectNotificationSetting.project_id]
            )
            .returning(ProjectNotificationSetting)
        )
        setting = insert_result.scalar_one_or_none()
        created = setting is not None
        if setting is None:
            result = await session.execute(
                select(ProjectNotificationSetting).where(
                    ProjectNotificationSetting.project_id == project_id
                )
            )
            setting = result.scalar_one()
        if created and commit_if_created:
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
        normalized_webhook_url = (
            _normalize_discord_webhook_url(discord_webhook_url)
            if discord_webhook_url is not None
            else None
        )
        setting = await self.get_or_create_notification_setting(
            session, project_id=project_id
        )

        if discord_webhook_url is not None:
            setting.discord_webhook_url = normalized_webhook_url
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

    async def get_web_push_vapid_public_key(self) -> dict[str, Any]:
        """Return the public VAPID key without exposing server secrets."""

        public_key = get_web_push_public_key()
        return {"enabled": bool(public_key), "public_key": public_key}

    async def upsert_web_push_subscription(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        endpoint: Any,
        p256dh: Any,
        auth: Any,
        expiration_time: Any = None,
        content_encoding: Any = "aes128gcm",
    ) -> dict[str, Any]:
        """Create/update one browser subscription for the authenticated user.

        Endpoint uniqueness deliberately moves an endpoint between users when
        a browser profile logs out and another account logs in. This prevents
        stale cross-account push delivery without retaining duplicate rows.
        """

        normalized_endpoint = _normalize_web_push_endpoint(endpoint)
        try:
            normalized_endpoint = await validate_web_push_endpoint(normalized_endpoint)
        except WebPushEndpointError as exc:
            raise TaskManagementError(
                "Invalid Web Push endpoint", status_code=400
            ) from exc
        normalized_p256dh = _normalize_web_push_key(p256dh, "p256dh")
        normalized_auth = _normalize_web_push_key(auth, "auth")
        encoding = str(content_encoding or "aes128gcm").strip().lower()
        if encoding not in _WEB_PUSH_CONTENT_ENCODINGS:
            raise TaskManagementError(
                "Invalid Web Push content_encoding", status_code=400
            )
        expires_at = _normalize_web_push_expiration(expiration_time)

        now = datetime.utcnow()
        # Endpoint is globally unique. Native upsert makes concurrent tabs
        # converge on one row instead of racing SELECT→INSERT and surfacing a
        # transient unique-constraint error.
        result = await session.execute(
            _conflict_safe_insert(session, WebPushSubscription)
            .values(
                id=uuid4(),
                user_id=user_id,
                endpoint=normalized_endpoint,
                p256dh=normalized_p256dh,
                auth=normalized_auth,
                expiration_time=expires_at,
                content_encoding=encoding,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[WebPushSubscription.endpoint],
                set_={
                    "user_id": user_id,
                    "p256dh": normalized_p256dh,
                    "auth": normalized_auth,
                    "expiration_time": expires_at,
                    "content_encoding": encoding,
                    "updated_at": now,
                },
            )
            .returning(WebPushSubscription)
        )
        subscription = result.scalar_one()
        await session.commit()
        await session.refresh(subscription)
        return {"success": True, "subscription": subscription.to_dict()}

    async def remove_web_push_subscription(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        endpoint: Any,
    ) -> dict[str, Any]:
        normalized_endpoint = _normalize_web_push_endpoint(endpoint)
        result = await session.execute(
            delete(WebPushSubscription).where(
                WebPushSubscription.user_id == user_id,
                WebPushSubscription.endpoint == normalized_endpoint,
            )
        )
        await session.commit()
        return {"success": True, "removed": int(result.rowcount or 0)}

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
                NotificationDelivery.status != "cancelled",
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
        if notification.status != "cancelled":
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
                NotificationDelivery.channel == "in_app",
                NotificationDelivery.read_at.is_(None),
                NotificationDelivery.status != "cancelled",
            )
        )
        notifications = result.scalars().all()
        now = datetime.utcnow()
        for notification in notifications:
            notification.read_at = now
            if notification.status != "cancelled":
                notification.status = "read"
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
        result = await session.execute(
            _conflict_safe_insert(session, NotificationDelivery)
            .values(
                id=uuid4(),
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
                status="pending",
                payload=payload or {},
            )
            .on_conflict_do_nothing(
                index_elements=[NotificationDelivery.dedupe_key]
            )
            .returning(NotificationDelivery)
        )
        return result.scalar_one_or_none()

    async def _create_in_app_reminder_deliveries(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        task_id: UUID,
        occurrence_id: UUID | None,
        recipients: list[UUID],
        title: str,
        message: str,
        anchor: datetime,
        anchor_key: str,
        recipient_offsets: Mapping[UUID, Iterable[Any]],
        recipient_offset_sources: Mapping[UUID, str],
        current_time: datetime,
    ) -> int:
        """Materialize user reminder rows before any client asks for a list.

        Dedupe keys intentionally match the former Next BFF keys so a row
        materialized by an older server is not shown twice during rollout.
        """

        created = 0
        for recipient_id in recipients:
            offsets = recipient_offsets.get(recipient_id, ())
            for raw_offset in offsets:
                try:
                    offset = int(raw_offset)
                except (TypeError, ValueError):
                    continue
                if offset < 0:
                    continue
                trigger_at = anchor - timedelta(minutes=offset)
                if trigger_at > current_time:
                    continue
                dedupe_key = (
                    f"reminder:{'occurrence:' + str(occurrence_id) if occurrence_id else 'task:' + str(task_id)}"
                    f":at:{anchor_key}:offset:{offset}:user:{recipient_id}"
                )
                delivery = await self._create_notification_if_missing(
                    session,
                    dedupe_key=dedupe_key,
                    project_id=project_id,
                    task_id=task_id,
                    occurrence_id=occurrence_id,
                    user_id=recipient_id,
                    channel="in_app",
                    notification_type="reminder",
                    title=title,
                    message=message,
                    scheduled_for=trigger_at,
                    payload={
                        "kind": "task_reminder",
                        "offset_minutes": offset,
                        "anchor_at": anchor_key,
                        "offset_source": recipient_offset_sources.get(
                            recipient_id, "project"
                        ),
                    },
                )
                if delivery:
                    created += 1
        return created

    async def _push_notification_is_current(
        self, session: AsyncSession, notification: NotificationDelivery
    ) -> bool:
        """Re-check mutable task state immediately before external push.

        A reminder row is intentionally durable for the inbox, but an edit or
        completion must not turn that old row into a late OS toast.
        """

        task = None
        if notification.task_id:
            task_result = await session.execute(
                select(Task)
                .options(selectinload(Task.recurrence_rule))
                .where(Task.id == notification.task_id)
                .with_for_update()
            )
            task = task_result.scalar_one_or_none()
            if task is None or _should_skip_task_only_notification(task):
                return False
            if task.notifications_enabled is False:
                return False
        if notification.occurrence_id:
            occurrence_result = await session.execute(
                select(TaskOccurrence)
                .where(TaskOccurrence.id == notification.occurrence_id)
                .with_for_update()
            )
            occurrence = occurrence_result.scalar_one_or_none()
            if occurrence is None or task is None:
                return False
            # ``task_schedule`` is the legacy non-recurring mirror.  Once a
            # task has no recurrence rule its canonical anchor is tasks.start_at;
            # any mirror row is stale and must never produce a toast.
            if occurrence.source_kind == "task_schedule" and hasattr(
                task, "recurrence_rule"
            ) and not task.recurrence_rule:
                return False
            if _should_skip_task_notification(task, occurrence):
                return False
            anchor = occurrence.start_at
            offsets = _normalize_reminder_offsets(occurrence.reminder_offsets)
            if not offsets:
                offsets = _normalize_reminder_offsets(task.reminder_offsets)
        else:
            if task is None:
                return False
            anchor = task.start_at
            offsets = _normalize_reminder_offsets(task.reminder_offsets)
        if notification.notification_type != "reminder":
            return True
        payload = notification.payload or {}
        expected_anchor = payload.get("anchor_at")
        if expected_anchor and anchor and str(expected_anchor) != anchor.isoformat():
            return False
        try:
            offset = int(payload.get("offset_minutes"))
        except (TypeError, ValueError):
            return False
        source = str(payload.get("offset_source") or "").strip().lower()
        if source == "task" or (not source and offsets):
            return offset in set(offsets)
        if source in {"user", "user_default"}:
            if not notification.user_id:
                return False
            user_result = await session.execute(
                select(User).where(User.id == notification.user_id)
            )
            user = user_result.scalar_one_or_none()
            user_offset = _user_task_notification_offset(user)
            if source == "user":
                return user_offset is not None and offset == user_offset
            return (
                user_offset is None
                and offset == DEFAULT_USER_NOTIFICATION_MINUTES
            )

        # ``project`` was used by an early worker build for in-app rows.  It is
        # deliberately no longer a valid source: project defaults belong to
        # Discord (and task/occurrence fallback), while an inbox recipient's
        # missing setting must remain the five-minute legacy default.  Cancel
        # any such pending row rather than resurrecting the wrong schedule.
        if source == "project":
            return False
        return False

    async def deliver_due_notifications(
        self, session: AsyncSession, *, now: Optional[datetime] = None
    ) -> dict[str, int]:
        current_time = _task_notification_now(now)
        scan_from = current_time - timedelta(days=1)
        # The worker must see reminders whose recipient offset is larger than
        # the historical 15-minute window (for example a 60-minute project
        # default or a user preference of 120 minutes).
        scan_to = current_time + timedelta(
            minutes=_task_notification_lookahead_minutes()
        )

        occurrence_result = await session.execute(
            select(TaskOccurrence)
            .join(Task, Task.id == TaskOccurrence.task_id)
            .options(
                selectinload(TaskOccurrence.task)
                .selectinload(Task.assignees)
                .selectinload(TaskAssignee.user),
                selectinload(TaskOccurrence.task).selectinload(Task.project),
                selectinload(TaskOccurrence.task).selectinload(Task.creator),
                selectinload(TaskOccurrence.task).selectinload(Task.recurrence_rule),
            )
            .where(
                TaskOccurrence.deleted_at.is_(None),
                Task.deleted_at.is_(None),
                Task.archived_at.is_(None),
                TaskOccurrence.end_at >= scan_from,
                TaskOccurrence.start_at <= scan_to,
            )
        )
        occurrences = occurrence_result.scalars().all()
        stats = {"created": 0, "delivered": 0, "failed": 0}
        # tasks 側フォールバックで二重通知しないための「タスクID:開始時刻」キー集合。
        # Web BFF（frontend/src/app/api/notifications/route.ts）と同じ突き合わせ方。
        occurrence_keys: set[tuple[UUID, Optional[datetime]]] = set()

        for occurrence in occurrences:
            if occurrence.task is None:
                continue
            task = occurrence.task
            if occurrence.source_kind == "task_schedule" and hasattr(
                task, "recurrence_rule"
            ) and not task.recurrence_rule:
                # This is the stale non-recurring mirror.  The tasks loop below
                # evaluates the canonical task anchor instead.
                continue
            occurrence_keys.add((task.id, occurrence.start_at))
            if _should_skip_task_notification(task, occurrence):
                continue
            setting = await self.get_or_create_notification_setting(
                session, project_id=task.project_id
            )
            recipients, recipient_users = _notification_recipients(task)

            if task.notifications_enabled:
                explicit_offsets = _normalize_reminder_offsets(
                    occurrence.reminder_offsets
                ) or _normalize_reminder_offsets(task.reminder_offsets)
                recipient_offsets, recipient_offset_sources = (
                    _recipient_reminder_offsets(
                        recipients=recipients,
                        users_by_id=recipient_users,
                        explicit_offsets=explicit_offsets,
                    )
                )
                title = f"Upcoming: {task.title}"
                message = f"{task.title} starts at {occurrence.start_at.isoformat()}"

                discord_offsets = explicit_offsets or (
                    _normalize_reminder_offsets(setting.default_reminder_offsets)
                    or [15]
                )
                for offset in discord_offsets:
                    trigger_at = occurrence.start_at - timedelta(minutes=offset)
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

                stats["created"] += await self._create_in_app_reminder_deliveries(
                    session,
                    project_id=task.project_id,
                    task_id=task.id,
                    occurrence_id=occurrence.id,
                    recipients=recipients,
                    title=title,
                    message=message,
                    anchor=occurrence.start_at,
                    anchor_key=occurrence.start_at.isoformat(),
                    recipient_offsets=recipient_offsets,
                    recipient_offset_sources=recipient_offset_sources,
                    current_time=current_time,
                )

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

        # --- 非繰り返しタスクのフォールバック ---
        # 繰り返しルールが無いタスクは task_occurrences にミラー行を持たない
        # （src/services/task_management/_shared.py の build_occurrence_schedule 参照）。
        # オカレンスだけを走査するとリマインダーが一切飛ばなくなるため、
        # tasks 本体からも通知候補を拾う。
        task_result = await session.execute(
            select(Task)
            .options(
                selectinload(Task.assignees).selectinload(TaskAssignee.user),
                selectinload(Task.creator),
                selectinload(Task.recurrence_rule),
            )
            .where(
                Task.deleted_at.is_(None),
                Task.archived_at.is_(None),
                Task.start_at.is_not(None),
                Task.start_at <= scan_to,
                or_(Task.end_at.is_(None), Task.end_at >= scan_from),
            )
        )
        for task in task_result.scalars().all():
            # 同じ予定のオカレンスが既に処理済みなら tasks 側では作らない。
            if (task.id, task.start_at) in occurrence_keys:
                continue
            if _should_skip_task_only_notification(task):
                continue
            if not task.notifications_enabled:
                continue

            setting = await self.get_or_create_notification_setting(
                session, project_id=task.project_id
            )
            recipients, recipient_users = _notification_recipients(task)
            # dedupe_key に開始時刻を含めることで、日付を変更した場合に
            # 新しい予定として再度リマインダーが飛ぶ（occurrence 版とも衝突しない）。
            anchor_key = task.start_at.isoformat()
            title = f"Upcoming: {task.title}"
            message = f"{task.title} starts at {anchor_key}"

            explicit_offsets = _normalize_reminder_offsets(task.reminder_offsets)
            recipient_offsets, recipient_offset_sources = _recipient_reminder_offsets(
                recipients=recipients,
                users_by_id=recipient_users,
                explicit_offsets=explicit_offsets,
            )
            discord_offsets = explicit_offsets or (
                _normalize_reminder_offsets(setting.default_reminder_offsets) or [15]
            )
            for offset in discord_offsets:
                trigger_at = task.start_at - timedelta(minutes=offset)
                if trigger_at > current_time or not setting.discord_webhook_url:
                    continue
                dedupe_key = (
                    f"reminder:task:{task.id}:at:{anchor_key}:offset:{offset}:discord"
                )
                delivery = await self._create_notification_if_missing(
                    session,
                    dedupe_key=dedupe_key,
                    project_id=task.project_id,
                    task_id=task.id,
                    occurrence_id=None,
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

            stats["created"] += await self._create_in_app_reminder_deliveries(
                session,
                project_id=task.project_id,
                task_id=task.id,
                occurrence_id=None,
                recipients=recipients,
                title=title,
                message=message,
                anchor=task.start_at,
                anchor_key=anchor_key,
                recipient_offsets=recipient_offsets,
                recipient_offset_sources=recipient_offset_sources,
                current_time=current_time,
            )

            if (
                recipients
                and setting.notify_overdue
                and task.end_at is not None
                and task.end_at <= current_time
            ):
                overdue_key = task.end_at.isoformat()
                overdue_title = f"Overdue: {task.title}"
                overdue_message = (
                    f"{task.title} should have ended at {overdue_key}"
                )
                for recipient_id in recipients:
                    dedupe_key = (
                        f"overdue:task:{task.id}:at:{overdue_key}:user:{recipient_id}"
                    )
                    delivery = await self._create_notification_if_missing(
                        session,
                        dedupe_key=dedupe_key,
                        project_id=task.project_id,
                        task_id=task.id,
                        occurrence_id=None,
                        user_id=recipient_id,
                        channel="in_app",
                        notification_type="overdue",
                        title=overdue_title,
                        message=overdue_message,
                        scheduled_for=task.end_at,
                    )
                    if delivery:
                        stats["created"] += 1
                if setting.discord_webhook_url:
                    dedupe_key = f"overdue:task:{task.id}:at:{overdue_key}:discord"
                    delivery = await self._create_notification_if_missing(
                        session,
                        dedupe_key=dedupe_key,
                        project_id=task.project_id,
                        task_id=task.id,
                        occurrence_id=None,
                        user_id=None,
                        channel="discord_webhook",
                        notification_type="overdue",
                        title=overdue_title,
                        message=overdue_message,
                        scheduled_for=task.end_at,
                    )
                    if delivery:
                        stats["created"] += 1

        await session.commit()

        pending_result = await session.execute(
            select(NotificationDelivery)
            .where(
                NotificationDelivery.status == "pending",
                NotificationDelivery.scheduled_for <= current_time,
                NotificationDelivery.read_at.is_(None),
            )
            .with_for_update(skip_locked=True)
        )
        pending_notifications = pending_result.scalars().all()

        for notification in pending_notifications:
            # A read-all request may commit between the claim SELECT and this
            # loop (especially on PostgreSQL where the worker transaction can
            # outlive the API transaction).  Treat the row as read and never
            # send an OS push for it; the SQL predicate below covers the normal
            # path and this guard closes the race.
            if getattr(notification, "read_at", None) is not None:
                notification.status = "read"
                continue
            if notification.channel == "in_app":
                reminder_is_current = True
                if notification.notification_type == "reminder":
                    reminder_is_current = await self._push_notification_is_current(
                        session, notification
                    )
                    if not reminder_is_current:
                        # Keep the durable dedupe row but suppress stale push /
                        # websocket delivery after an edit, completion, or
                        # cancellation. The list API hides cancelled rows.
                        notification.status = "cancelled"
                        notification.delivered_at = current_time
                        continue
                notification.delivered_at = current_time
                notification.status = "delivered"
                stats["delivered"] += 1
                await self._broadcast("notification_created", notification.to_dict())
                # Push is an optional side channel. It is deliberately sent
                # after the durable row is marked delivered so a provider
                # outage never removes an inbox item or affects Discord.
                if (
                    getattr(notification, "user_id", None)
                    and notification.scheduled_for >= current_time - _PUSH_LATE_TTL
                    and reminder_is_current
                ):
                    subscriptions_result = await session.execute(
                        select(WebPushSubscription).where(
                            WebPushSubscription.user_id == notification.user_id
                        )
                    )
                    push_payload = {
                        "title": notification.title,
                        "body": notification.message,
                        "notificationId": str(notification.id),
                        "taskId": str(notification.task_id)
                        if notification.task_id
                        else None,
                        "url": (
                            f"/tasks/{notification.task_id}"
                            if notification.task_id
                            else "/"
                        ),
                        "tag": f"aoitalk-{notification.id}",
                        "scheduledFor": notification.scheduled_for.isoformat(),
                    }
                    for subscription in subscriptions_result.scalars().all():
                        try:
                            endpoint = await validate_web_push_endpoint(
                                subscription.endpoint
                            )
                        except WebPushEndpointError:
                            # A stored DNS name can be rebound after the user
                            # subscribed.  Remove unsafe rows instead of
                            # allowing the worker to become an SSRF primitive.
                            await session.delete(subscription)
                            stats.setdefault("push_removed", 0)
                            stats["push_removed"] += 1
                            continue
                        result = await send_web_push(
                            {
                                "endpoint": endpoint,
                                "keys": {
                                    "p256dh": subscription.p256dh,
                                    "auth": subscription.auth,
                                },
                            },
                            push_payload,
                            content_encoding=getattr(
                                subscription, "content_encoding", "aes128gcm"
                            ),
                        )
                        if result.sent:
                            stats.setdefault("push_sent", 0)
                            stats["push_sent"] += 1
                        elif is_expired_subscription_error(result):
                            await session.delete(subscription)
                            stats.setdefault("push_removed", 0)
                            stats["push_removed"] += 1
                        elif result.reason not in {
                            "vapid_not_configured",
                            "pywebpush_unavailable",
                        }:
                            stats.setdefault("push_failed", 0)
                            stats["push_failed"] += 1
                continue

            if notification.channel == "discord_webhook":
                setting = await self.get_or_create_notification_setting(
                    session,
                    project_id=notification.project_id,
                    commit_if_created=False,
                )
                try:
                    webhook_url = _normalize_discord_webhook_url(
                        setting.discord_webhook_url
                    )
                except TaskManagementError:
                    notification.status = "failed"
                    stats["failed"] += 1
                    continue
                if not webhook_url:
                    notification.status = "failed"
                    stats["failed"] += 1
                    continue

                try:
                    async with httpx.AsyncClient(
                        timeout=10.0,
                        follow_redirects=False,
                    ) as client:
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
