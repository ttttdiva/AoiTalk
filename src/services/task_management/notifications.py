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
from sqlalchemy import and_, delete, func, or_, select, update
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
from ..outbound_privacy_service import (
    ExternalProviderBlocked,
    OutboundPrivacyGateway,
    PrivacyError,
    get_privacy_policy_context,
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

# Project Steward rows are legacy durable inbox records.  New Steward runs no
# longer create notifications, but rows written by an older worker (or by a
# mixed-version deployment during rollout) can still be present.  Keep this
# classifier intentionally narrow: only the two explicit notification types
# and the one legacy payload kind belong to Steward.  Do not infer Steward
# ownership from titles, dedupe keys, projects, or arbitrary payload fields so
# ordinary task/system notifications retain their existing semantics.
_PROJECT_STEWARD_NOTIFICATION_TYPES = frozenset(
    {"project_steward", "project_steward_alert"}
)

# Knowledge Capture deliberately has its own notification namespace.  Keep
# this classifier independent from the Project Steward classifier below: a
# mixed-version deployment must never make a new capture row look like a
# Steward alert, and Steward's legacy filtering must remain unchanged.
KNOWLEDGE_CAPTURE_NOTIFICATION_TYPES = frozenset(
    {"knowledge_capture_question", "knowledge_capture_draft"}
)
_KNOWLEDGE_CAPTURE_OPEN_MODE = "knowledge_capture_review"
_KNOWLEDGE_CAPTURE_MAX_TITLE = 160
_KNOWLEDGE_CAPTURE_MAX_MESSAGE = 1000
_KNOWLEDGE_CAPTURE_PAYLOAD_KEYS = frozenset(
    {"kind", "candidate_id", "question_id", "candidate_version"}
)


def _notification_field(notification: Any, name: str) -> Any:
    """Read a notification field from either an ORM row or a mapping."""

    if isinstance(notification, Mapping):
        return notification.get(name)
    return getattr(notification, name, None)


def is_project_steward_notification(notification: Any) -> bool:
    """Return whether *notification* is a Project Steward inbox row.

    The explicit type values are normalized for compatibility with rows from
    mixed-version workers.  Payload ``kind`` intentionally accepts only the
    exact legacy value; no title/message or fuzzy matching is performed.
    """

    notification_type = _notification_field(notification, "notification_type")
    if (
        isinstance(notification_type, str)
        and notification_type.strip().casefold()
        in _PROJECT_STEWARD_NOTIFICATION_TYPES
    ):
        return True

    payload = _notification_field(notification, "payload")
    if isinstance(payload, Mapping):
        return payload.get("kind") == "project_steward_alert"
    return False


# Keep a private spelling available for callers/tests that follow the existing
# module-level helper naming convention while exposing the descriptive public
# helper for new code.
_is_project_steward_notification = is_project_steward_notification


def _coerce_uuid(value: Any, field_name: str) -> UUID:
    """Coerce a typed knowledge-capture identifier without accepting junk."""

    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise TaskManagementError(
            f"Invalid knowledge capture {field_name}", status_code=400
        ) from exc


def _knowledge_capture_payload(
    *,
    candidate_id: UUID,
    question_id: UUID | None,
    candidate_version: int,
) -> dict[str, Any]:
    """Build the closed notification payload contract.

    The payload contains identifiers and a version only.  In particular, it
    has no model-controlled URL/action field; clients derive navigation from
    the known internal route and re-check Project ACL at the destination.
    """

    try:
        version = int(candidate_version)
    except (TypeError, ValueError) as exc:
        raise TaskManagementError(
            "Invalid knowledge capture candidate version", status_code=400
        ) from exc
    if version <= 0:
        raise TaskManagementError(
            "Invalid knowledge capture candidate version", status_code=400
        )
    return {
        "kind": "knowledge_capture",
        "candidate_id": str(candidate_id),
        "question_id": str(question_id) if question_id is not None else None,
        "candidate_version": version,
    }


def is_knowledge_capture_notification(notification: Any) -> bool:
    """Return whether a row is a valid, typed Knowledge Capture notification."""

    notification_type = _notification_field(notification, "notification_type")
    if notification_type not in KNOWLEDGE_CAPTURE_NOTIFICATION_TYPES:
        return False
    payload = _notification_field(notification, "payload")
    if not isinstance(payload, Mapping):
        return False
    if set(payload) != set(_KNOWLEDGE_CAPTURE_PAYLOAD_KEYS):
        return False
    if payload.get("kind") != "knowledge_capture":
        return False
    try:
        _coerce_uuid(payload.get("candidate_id"), "candidate_id")
        question_id = payload.get("question_id")
        if question_id is not None:
            _coerce_uuid(question_id, "question_id")
        version = int(payload.get("candidate_version"))
    except (TaskManagementError, TypeError, ValueError):
        return False
    return version > 0


def knowledge_capture_notification_dedupe_key(
    *,
    candidate_id: UUID,
    notification_type: str,
    question_id: UUID | None = None,
    candidate_version: int | None = None,
) -> str:
    """Return the stable dedupe key for one capture state notification."""

    candidate_uuid = _coerce_uuid(candidate_id, "candidate_id")
    if notification_type == "knowledge_capture_question":
        if question_id is None:
            raise TaskManagementError(
                "Knowledge Capture question notifications require question_id",
                status_code=400,
            )
        question_uuid = _coerce_uuid(question_id, "question_id")
        return f"knowledge_capture:{candidate_uuid}:question:{question_uuid}"
    if notification_type == "knowledge_capture_draft":
        if question_id is not None:
            raise TaskManagementError(
                "Knowledge Capture draft notifications cannot have question_id",
                status_code=400,
            )
        try:
            version = int(candidate_version)
        except (TypeError, ValueError) as exc:
            raise TaskManagementError(
                "Knowledge Capture draft notifications require candidate_version",
                status_code=400,
            ) from exc
        if version <= 0:
            raise TaskManagementError(
                "Knowledge Capture draft notifications require candidate_version",
                status_code=400,
            )
        return f"knowledge_capture:{candidate_uuid}:draft:{version}"
    raise TaskManagementError(
        "Unsupported Knowledge Capture notification type", status_code=400
    )


def _bounded_capture_text(value: Any, *, field_name: str, limit: int) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if not text or len(text) > limit:
        raise TaskManagementError(
            f"Invalid Knowledge Capture notification {field_name}", status_code=400
        )
    return text


def _knowledge_capture_notification_route(notification: Any) -> str | None:
    """Build the neutral notification-id route for a capture push item."""

    if not is_knowledge_capture_notification(notification):
        return None
    project_id = _notification_field(notification, "project_id")
    payload = _notification_field(notification, "payload") or {}
    try:
        # Keep the model/id validation boundary, but do not expose domain
        # identifiers or UI state in a push URL.  The notification endpoint
        # resolves the typed row after the client opens this neutral route.
        _coerce_uuid(project_id, "project_id")
        _coerce_uuid(payload.get("candidate_id"), "candidate_id")
        notification_uuid = _coerce_uuid(
            _notification_field(notification, "id"), "notification_id"
        )
    except TaskManagementError:
        return None
    return f"/chat?open_notification={notification_uuid}"


async def persist_knowledge_capture_notification(
    session: AsyncSession,
    *,
    project_id: UUID,
    recipient_user_id: UUID,
    candidate_id: UUID,
    notification_type: str,
    candidate_version: int,
    question_id: UUID | None = None,
    title: str,
    message: str,
    scheduled_for: Optional[datetime] = None,
    commit: bool = False,
) -> Optional[NotificationDelivery]:
    """Persist one Project-authorized Knowledge Capture inbox row.

    This helper is intentionally transaction-friendly.  Worker callers can
    keep the candidate transition and notification insert in one transaction
    by leaving ``commit`` false; standalone callers may request a short commit.
    """

    project_uuid = _coerce_uuid(project_id, "project_id")
    recipient_uuid = _coerce_uuid(recipient_user_id, "recipient_user_id")
    candidate_uuid = _coerce_uuid(candidate_id, "candidate_id")
    question_uuid = (
        _coerce_uuid(question_id, "question_id") if question_id is not None else None
    )
    if notification_type not in KNOWLEDGE_CAPTURE_NOTIFICATION_TYPES:
        raise TaskManagementError(
            "Unsupported Knowledge Capture notification type", status_code=400
        )
    if notification_type == "knowledge_capture_question" and question_uuid is None:
        raise TaskManagementError(
            "Knowledge Capture question notifications require question_id",
            status_code=400,
        )
    if notification_type == "knowledge_capture_draft" and question_uuid is not None:
        raise TaskManagementError(
            "Knowledge Capture draft notifications cannot have question_id",
            status_code=400,
        )

    # Revalidate the recipient against the live Project ACL immediately before
    # writing.  A stale candidate/worker must not notify a removed member.
    if not await ProjectRepository.has_permission(
        session,
        project_id=project_uuid,
        user_id=recipient_uuid,
        permission="read",
    ):
        raise TaskManagementError(
            "Knowledge Capture notification recipient is not authorized",
            status_code=403,
        )

    payload = _knowledge_capture_payload(
        candidate_id=candidate_uuid,
        question_id=question_uuid,
        candidate_version=candidate_version,
    )
    dedupe_key = knowledge_capture_notification_dedupe_key(
        candidate_id=candidate_uuid,
        notification_type=notification_type,
        question_id=question_uuid,
        candidate_version=int(candidate_version),
    )
    delivery = await _insert_notification_if_missing(
        session,
        dedupe_key=dedupe_key,
        project_id=project_uuid,
        task_id=None,
        occurrence_id=None,
        user_id=recipient_uuid,
        channel="in_app",
        notification_type=notification_type,
        title=_bounded_capture_text(
            title, field_name="title", limit=_KNOWLEDGE_CAPTURE_MAX_TITLE
        ),
        message=_bounded_capture_text(
            message, field_name="message", limit=_KNOWLEDGE_CAPTURE_MAX_MESSAGE
        ),
        scheduled_for=scheduled_for or datetime.utcnow(),
        payload=payload,
    )
    if delivery is None:
        # A duplicate retry is still successful from the workflow's point of
        # view.  Return the durable winner when it is visible to this session.
        result = await session.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.dedupe_key == dedupe_key
            )
        )
        delivery = result.scalar_one_or_none()
    if commit:
        await session.commit()
        if delivery is not None and callable(getattr(session, "refresh", None)):
            await session.refresh(delivery)
    return delivery


async def close_knowledge_capture_notifications(
    session: AsyncSession,
    *,
    candidate_id: UUID,
    question_id: UUID | None = None,
    project_id: UUID | None = None,
    commit: bool = False,
) -> int:
    """Cancel open capture rows for an answer, dismiss, or publication.

    ``question_id`` narrows closure to one question.  Omitting it closes all
    capture rows for the candidate, which is used by dismiss/publish.
    """

    candidate_uuid = _coerce_uuid(candidate_id, "candidate_id")
    question_uuid = (
        _coerce_uuid(question_id, "question_id") if question_id is not None else None
    )
    conditions = [
        NotificationDelivery.channel == "in_app",
        NotificationDelivery.notification_type.in_(
            tuple(KNOWLEDGE_CAPTURE_NOTIFICATION_TYPES)
        ),
        NotificationDelivery.status != "cancelled",
    ]
    if project_id is not None:
        conditions.append(
            NotificationDelivery.project_id == _coerce_uuid(project_id, "project_id")
        )
    result = await session.execute(select(NotificationDelivery).where(and_(*conditions)))
    now = datetime.utcnow()
    closed = 0
    for notification in result.scalars().all():
        payload = notification.payload if isinstance(notification.payload, Mapping) else {}
        if payload.get("kind") != "knowledge_capture":
            continue
        if str(payload.get("candidate_id")) != str(candidate_uuid):
            continue
        if question_uuid is not None and str(payload.get("question_id")) != str(question_uuid):
            continue
        notification.status = "cancelled"
        notification.delivered_at = now
        notification.updated_at = now
        closed += 1
    if commit:
        await session.commit()
    return closed


# Service-style facade for candidate/research workers that do not need the
# full TaskManagementService object.  Keeping this small also gives tests a
# focused seam without changing Project Steward construction.
class KnowledgeCaptureNotificationService:
    persist = staticmethod(persist_knowledge_capture_notification)
    close = staticmethod(close_knowledge_capture_notifications)


def _notification_gateway(service: Any) -> OutboundPrivacyGateway:
    """Resolve the request/worker-scoped privacy gateway for webhooks."""

    injected = getattr(service, "privacy_gateway", None)
    if injected is None:
        injected = getattr(service, "_privacy_gateway", None)
    if injected is not None:
        return injected

    inherited = get_privacy_policy_context()
    config = getattr(service, "config", None)
    if config is None:
        config = getattr(service, "_config", None)
    return OutboundPrivacyGateway(
        config,
        user_id=str(getattr(service, "user_id", "") or ""),
        session_id=str(getattr(service, "session_id", "") or ""),
        session_context=inherited.session_context,
        project_metadata=inherited.project_metadata,
    )


def _notification_egress_descriptor(*, destination: str):
    """Build a descriptor for a Discord background webhook delivery."""

    try:
        from ..outbound_privacy_service import EgressDescriptor

        return EgressDescriptor(
            action="task_notification.webhook",
            transport="httpx",
            destination=destination,
            provider="discord",
            tool="task_notification",
            model="",
        )
    except ImportError:  # pragma: no cover - compatibility with old embeds
        from types import SimpleNamespace

        return SimpleNamespace(
            action="task_notification.webhook",
            transport="httpx",
            destination=destination,
            provider="discord",
            tool="task_notification",
            model="",
        )


async def _insert_notification_if_missing(
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
    """Insert one durable notification, converging concurrent retries.

    ``NotificationDelivery.dedupe_key`` is the sole idempotency boundary for
    both task reminders and Project Steward alerts.  Keeping this primitive
    module-level lets the Steward persist an inbox row without constructing a
    full task-management service (and therefore without opening any task or
    Docs mutation path).
    """

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
        .on_conflict_do_nothing(index_elements=[NotificationDelivery.dedupe_key])
        .returning(NotificationDelivery)
    )
    return result.scalar_one_or_none()


async def persist_project_steward_notification(
    session: AsyncSession,
    *,
    project_id: UUID,
    owner_user_id: UUID,
    dedupe_key: str,
    title: str,
    message: str,
    payload: Optional[dict[str, Any]] = None,
    scheduled_for: Optional[datetime] = None,
) -> Optional[NotificationDelivery]:
    """Persist an owner-scoped Project Steward inbox alert.

    The project owner check is performed immediately before the insert.  A
    project can be deleted or transferred between evidence collection and
    alert persistence; in that case the operation fails closed rather than
    leaking an alert to the former owner.  The unique dedupe key makes a
    retry/redelivery exactly-once even when the first attempt already wrote
    the row but the caller lost the response.
    """

    try:
        project_uuid = (
            project_id if isinstance(project_id, UUID) else UUID(str(project_id))
        )
        owner_uuid = (
            owner_user_id
            if isinstance(owner_user_id, UUID)
            else UUID(str(owner_user_id))
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise TaskManagementError(
            "Invalid Project Steward notification scope", status_code=400
        ) from exc

    # Importing Project at module load is safe (it is already imported above),
    # and this query intentionally requires an active project owner match.
    owner_result = await session.execute(
        select(Project.owner_id).where(
            Project.id == project_uuid,
            Project.deleted_at.is_(None),
        )
    )
    current_owner = owner_result.scalar_one_or_none()
    if current_owner is None or str(current_owner) != str(owner_uuid):
        raise TaskManagementError(
            "Project Steward notification scope is no longer authorized",
            status_code=404,
        )

    # Do not let malformed/legacy callers turn this helper into an unbounded
    # text sink.  Steward model output is validated upstream, but this
    # boundary is also used by mixed-version workers during rollout.
    normalized_dedupe = str(dedupe_key or "").strip()
    normalized_title = str(title or "").strip()
    normalized_message = str(message or "").strip()
    if not normalized_dedupe or len(normalized_dedupe) > 255:
        raise TaskManagementError("Invalid notification dedupe key", status_code=400)
    if not normalized_title or len(normalized_title) > 255:
        raise TaskManagementError("Invalid notification title", status_code=400)
    if not normalized_message:
        raise TaskManagementError("Invalid notification message", status_code=400)

    delivery = await _insert_notification_if_missing(
        session,
        dedupe_key=normalized_dedupe,
        project_id=project_uuid,
        task_id=None,
        occurrence_id=None,
        user_id=owner_uuid,
        channel="in_app",
        notification_type="project_steward",
        title=normalized_title,
        message=normalized_message,
        scheduled_for=scheduled_for or datetime.utcnow(),
        payload=payload,
    )
    # The helper owns this short transaction.  Calling code treats both a new
    # row and an existing dedupe winner as success, while a commit failure
    # propagates and therefore prevents a Heartbeat cursor advance.
    await session.commit()
    return delivery


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

    async def _knowledge_capture_notification_is_current(
        self,
        session: AsyncSession,
        notification: NotificationDelivery,
    ) -> bool:
        """Revalidate the recipient's live Project read permission."""

        if not is_knowledge_capture_notification(notification):
            return True
        project_id = getattr(notification, "project_id", None)
        user_id = getattr(notification, "user_id", None)
        try:
            project_uuid = _coerce_uuid(project_id, "project_id")
            user_uuid = _coerce_uuid(user_id, "user_id")
        except TaskManagementError:
            return False
        return await ProjectRepository.has_permission(
            session,
            project_id=project_uuid,
            user_id=user_uuid,
            permission="read",
        )

    async def create_knowledge_capture_notification(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        recipient_user_id: UUID,
        candidate_id: UUID,
        notification_type: str,
        candidate_version: int,
        question_id: UUID | None = None,
        title: str,
        message: str,
        scheduled_for: Optional[datetime] = None,
        commit: bool = False,
    ) -> Optional[NotificationDelivery]:
        """Persist a typed, Project-authorized capture notification."""

        return await persist_knowledge_capture_notification(
            session,
            project_id=project_id,
            recipient_user_id=recipient_user_id,
            candidate_id=candidate_id,
            notification_type=notification_type,
            candidate_version=candidate_version,
            question_id=question_id,
            title=title,
            message=message,
            scheduled_for=scheduled_for,
            commit=commit,
        )

    async def close_knowledge_capture_notifications(
        self,
        session: AsyncSession,
        *,
        candidate_id: UUID,
        question_id: UUID | None = None,
        project_id: UUID | None = None,
        commit: bool = False,
    ) -> int:
        """Close question/draft rows after a candidate mutation."""

        return await close_knowledge_capture_notifications(
            session,
            candidate_id=candidate_id,
            question_id=question_id,
            project_id=project_id,
            commit=commit,
        )

    async def _project_steward_notification_is_current(
        self,
        session: AsyncSession,
        notification: NotificationDelivery,
    ) -> bool:
        """Revalidate owner scope before inbox/read or external delivery.

        Steward rows are intentionally owner-only.  A soft-deleted project or
        an ownership transfer must invalidate a pending row even though the
        original ``user_id`` and dedupe key remain unchanged.
        """

        if not is_project_steward_notification(notification):
            return True
        project_id = getattr(notification, "project_id", None)
        user_id = getattr(notification, "user_id", None)
        if project_id is None or user_id is None:
            return False
        result = await session.execute(
            select(Project.owner_id).where(
                Project.id == project_id,
                Project.deleted_at.is_(None),
            )
        )
        owner_id = result.scalar_one_or_none()
        return owner_id is not None and str(owner_id) == str(user_id)

    async def create_project_steward_notification(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        dedupe_key: str,
        title: str,
        message: str,
        payload: Optional[dict[str, Any]] = None,
        scheduled_for: Optional[datetime] = None,
    ) -> Optional[NotificationDelivery]:
        """Mixin facade for the owner-scoped durable Steward alert helper."""

        return await persist_project_steward_notification(
            session,
            project_id=project_id,
            owner_user_id=owner_user_id,
            dedupe_key=dedupe_key,
            title=title,
            message=message,
            payload=payload,
            scheduled_for=scheduled_for,
        )

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
            .join(Project, Project.id == NotificationDelivery.project_id)
            .where(
                NotificationDelivery.channel == "in_app",
                NotificationDelivery.user_id == user_id,
                NotificationDelivery.status != "cancelled",
                Project.deleted_at.is_(None),
            )
            .order_by(
                NotificationDelivery.scheduled_for.desc(),
                NotificationDelivery.created_at.desc(),
            )
        )
        if unread_only:
            stmt = stmt.where(NotificationDelivery.read_at.is_(None))

        result = await session.execute(stmt)
        notifications = []
        for notification in result.scalars().all():
            # Project Steward is background housekeeping, not a user-facing
            # inbox item.  Suppress every legacy Steward row regardless of
            # owner validity; the durable row remains available for bounded
            # cleanup/audit while ordinary notifications keep their existing
            # owner/project checks.
            if is_project_steward_notification(notification):
                continue
            if is_knowledge_capture_notification(notification) and not await self._knowledge_capture_notification_is_current(
                session,
                notification,
            ):
                # Membership revocation/deletion invalidates a capture inbox
                # row just like candidate authorization at the API boundary.
                continue
            if not await self._project_steward_notification_is_current(
                session,
                notification,
            ):
                # Keep the durable dedupe row for audit/retry convergence but
                # never expose stale owner-scoped alerts through the inbox.
                continue
            notifications.append(notification.to_dict())
        return notifications

    async def mark_notification_read(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        notification_id: UUID,
    ) -> dict[str, Any]:
        result = await session.execute(
            select(NotificationDelivery)
            .join(Project, Project.id == NotificationDelivery.project_id)
            .where(
                NotificationDelivery.id == notification_id,
                NotificationDelivery.channel == "in_app",
                NotificationDelivery.user_id == user_id,
                Project.deleted_at.is_(None),
            )
        )
        notification = result.scalar_one_or_none()
        # Project Steward results are never user-readable through the normal
        # notification API, including rows created by older workers.  Return
        # the same not-found contract as an unknown or unauthorized row and do
        # not mutate the durable record.
        if notification is not None and is_project_steward_notification(notification):
            raise TaskManagementError("Notification not found", status_code=404)
        if notification is not None and is_knowledge_capture_notification(
            notification
        ) and not await self._knowledge_capture_notification_is_current(
            session,
            notification,
        ):
            raise TaskManagementError("Notification not found", status_code=404)
        if notification is None or not await self._project_steward_notification_is_current(
            session,
            notification,
        ):
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
            select(NotificationDelivery)
            .join(Project, Project.id == NotificationDelivery.project_id)
            .where(
                NotificationDelivery.user_id == user_id,
                NotificationDelivery.channel == "in_app",
                NotificationDelivery.read_at.is_(None),
                NotificationDelivery.status != "cancelled",
                ~NotificationDelivery.notification_type.in_(
                    tuple(KNOWLEDGE_CAPTURE_NOTIFICATION_TYPES)
                ),
                Project.deleted_at.is_(None),
            )
        )
        notifications = [
            notification
            for notification in result.scalars().all()
            if not is_project_steward_notification(notification)
            # Keep this defensive type-only guard even though the SQL
            # predicate above is authoritative.  A mocked result or a mixed
            # transaction snapshot must never let a malformed/stale KC row
            # through based on payload validity.
            and _notification_field(notification, "notification_type")
            not in KNOWLEDGE_CAPTURE_NOTIFICATION_TYPES
        ]
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
        return await _insert_notification_if_missing(
            session,
            dedupe_key=dedupe_key,
            project_id=project_id,
            task_id=task_id,
            occurrence_id=occurrence_id,
            user_id=user_id,
            channel=channel,
            notification_type=notification_type,
            title=title,
            message=message,
            scheduled_for=scheduled_for,
            payload=payload,
        )

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
            if is_project_steward_notification(notification):
                # Project Steward is operational background work, never a
                # user-notification channel.  Cancel legacy pending rows
                # before touching websocket, web-push, or external delivery so
                # mixed-version workers cannot leak a Steward result.  Keep
                # the durable dedupe row for bounded cleanup/audit.
                notification.status = "cancelled"
                notification.delivered_at = current_time
                continue
            if is_knowledge_capture_notification(notification) and not await self._knowledge_capture_notification_is_current(
                session,
                notification,
            ):
                # Do not broadcast or push a candidate after its recipient
                # loses Project access.  Keep the durable dedupe row for
                # audit/retry convergence.
                notification.status = "cancelled"
                notification.delivered_at = current_time
                continue
            if notification.channel == "in_app":
                if not await self._project_steward_notification_is_current(
                    session,
                    notification,
                ):
                    # Owner transfer/project deletion invalidates a pending
                    # Steward row.  Preserve its unique dedupe record but do
                    # not broadcast or push stale project intelligence.
                    notification.status = "cancelled"
                    notification.delivered_at = current_time
                    continue
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
                    knowledge_route = _knowledge_capture_notification_route(
                        notification
                    )
                    knowledge_payload = (
                        notification.payload
                        if is_knowledge_capture_notification(notification)
                        and isinstance(notification.payload, Mapping)
                        else {}
                    )
                    push_payload = {
                        "title": notification.title,
                        "body": notification.message,
                        "notificationId": str(notification.id),
                        "taskId": str(notification.task_id)
                        if notification.task_id
                        else None,
                        "candidateId": knowledge_payload.get("candidate_id"),
                        "questionId": knowledge_payload.get("question_id"),
                        "projectId": str(notification.project_id)
                        if knowledge_route
                        else None,
                        # Knowledge Capture routes are derived from typed IDs;
                        # payload-provided action URLs are never forwarded.
                        "url": knowledge_route
                        or (
                            f"/tasks/{notification.task_id}"
                            if notification.task_id
                            else "/"
                        ),
                        "tag": f"aoitalk-{notification.id}",
                        "scheduledFor": notification.scheduled_for.isoformat(),
                    }
                    if knowledge_route:
                        push_payload["openMode"] = _KNOWLEDGE_CAPTURE_OPEN_MODE
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
                    gateway = _notification_gateway(self)
                    descriptor = _notification_egress_descriptor(
                        destination=webhook_url,
                    )

                    async def send(protected_payload):
                        if not isinstance(protected_payload, Mapping):
                            raise PrivacyError(
                                "privacy protection returned no protected payload"
                            )
                        async with httpx.AsyncClient(
                            timeout=10.0,
                            follow_redirects=False,
                        ) as client:
                            return await client.post(
                                webhook_url,
                                json=dict(protected_payload),
                            )

                    execute = getattr(gateway, "execute", None)
                    if not callable(execute):
                        # Background workers must never bypass the privacy
                        # transaction when running against a mixed-version
                        # gateway.  Fail closed instead of sending raw text.
                        raise PrivacyError(
                            "outbound privacy gateway does not support execution"
                        )
                    response = await execute(
                        {
                            "content": f"**{notification.title}**\n{notification.message}"
                        },
                        provider="discord",
                        descriptor=descriptor,
                        sender=send,
                        base_url=webhook_url,
                        source_kind="task_notification.webhook",
                    )
                    response.raise_for_status()
                    notification.delivered_at = current_time
                    notification.status = "delivered"
                    stats["delivered"] += 1
                except (ExternalProviderBlocked, PrivacyError):
                    notification.status = "failed"
                    stats["failed"] += 1
                except Exception:
                    notification.status = "failed"
                    stats["failed"] += 1

        await session.commit()
        return stats
