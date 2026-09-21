"""Transaction-friendly Resolution Knowledge Capture candidate primitives.

This module owns only the durable queue/domain boundary.  It deliberately
does not call an LLM, perform research, publish Docs, create notifications, or
commit the caller's transaction.  A worker or future route can claim a row,
perform its bounded read-only work outside the request, and then use the
version/lease-fenced helpers below to settle it.

Task status vocabulary remains owned by ``task_management._shared``.  The
capture boundary treats ``closed`` and ``cancelled`` as terminal after calling
the existing normalizer, but only successful ``closed`` transitions enqueue a
candidate.  ``cancelled`` therefore cannot accidentally become reusable
knowledge.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..memory.models import (
    KnowledgeCaptureCandidate,
    KnowledgeCaptureQuestion,
    Project,
    ProjectKnowledgeCaptureSetting,
    Task,
    TaskActivity,
    DEFAULT_KNOWLEDGE_CAPTURE_MODE,
    KNOWLEDGE_CAPTURE_QUEUED_STATE,
    normalize_candidate_state,
    normalize_knowledge_capture_mode,
    validate_candidate_transition,
    validate_question_transition,
)
from .task_management._shared import normalize_task_status


TASK_TERMINAL_STATUSES = frozenset({"closed", "cancelled"})
SUCCESSFUL_CAPTURE_TERMINAL_STATUSES = frozenset({"closed"})
CAPTURE_CLAIMABLE_STATES = frozenset({KNOWLEDGE_CAPTURE_QUEUED_STATE, "pending", "retry_wait"})
CAPTURE_ACTIVE_STATES = frozenset({"researching"})
CAPTURE_MAX_RECOVERY_SCAN = 500
CAPTURE_MAX_CLAIM_BATCH = 50
CAPTURE_DEFAULT_LEASE_SECONDS = 60.0
CAPTURE_MAX_LEASE_SECONDS = 3600.0
CAPTURE_MAX_ATTEMPTS = 5
CAPTURE_RETRY_BASE_SECONDS = 30.0
CAPTURE_MAX_RETRY_SECONDS = 6 * 60 * 60.0


class KnowledgeCaptureError(RuntimeError):
    """Base error for durable Knowledge Capture mutations."""


class KnowledgeCaptureNotFound(KnowledgeCaptureError):
    """The requested candidate, question, or project does not exist."""


class KnowledgeCaptureConflict(KnowledgeCaptureError):
    """A state, version, or lease fence no longer matches."""


class KnowledgeCaptureValidation(ValueError, KnowledgeCaptureError):
    """Caller data is outside the bounded durable contract."""


def _uuid(value: Any, *, field: str, required: bool = True) -> UUID | None:
    if value in (None, ""):
        if required:
            raise KnowledgeCaptureValidation(f"{field} is required")
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise KnowledgeCaptureValidation(f"{field} must be a valid UUID") from exc


def _bounded_text(value: Any, *, field: str, limit: int, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise KnowledgeCaptureValidation(f"{field} is required")
        return None
    text_value = str(value).replace("\x00", "").strip()
    if not text_value:
        if required:
            raise KnowledgeCaptureValidation(f"{field} is required")
        return None
    return text_value[:limit]


def _safe_refs(value: Any, *, limit: int = 32) -> list[dict[str, Any]]:
    """Keep only small typed references; never persist raw model prose here."""

    if value is None:
        return []
    if isinstance(value, Mapping):
        values: Iterable[Any] = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values = value
    else:
        raise KnowledgeCaptureValidation("evidence references must be a list of objects")

    result: list[dict[str, Any]] = []
    for item in list(values)[:limit]:
        if not isinstance(item, Mapping):
            continue
        kind = _bounded_text(item.get("type"), field="reference.type", limit=48)
        ref_id = _bounded_text(item.get("id"), field="reference.id", limit=160)
        if not kind or not ref_id:
            continue
        projected: dict[str, Any] = {"type": kind, "id": ref_id}
        digest = _bounded_text(
            item.get("evidence_sha256"), field="reference.evidence_sha256", limit=64
        )
        if digest:
            projected["evidence_sha256"] = digest
        result.append(projected)
    return result


def _safe_question_options(value: Any) -> list[dict[str, str]]:
    """Persist the closed ``id`` + ``label`` question-option contract."""

    if value is None:
        return []
    if isinstance(value, Mapping):
        values: Iterable[Any] = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values = value
    else:
        raise KnowledgeCaptureValidation("question options must be a list of objects")

    options: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(list(values)[:8], start=1):
        if isinstance(item, str):
            label = _bounded_text(item, field="option", limit=500)
            if not label:
                continue
            option_id = f"option_{index}"
        elif isinstance(item, Mapping):
            option_id = _bounded_text(
                item.get("id") or item.get("option_id") or item.get("value"),
                field="option.id",
                limit=200,
            )
            label = _bounded_text(
                item.get("label") or item.get("text") or item.get("value"),
                field="option.label",
                limit=500,
            )
            if not label and option_id:
                label = option_id
            if not label:
                continue
            if not option_id:
                option_id = f"option_{index}"
        else:
            continue

        normalized_id = option_id.casefold()
        if normalized_id in seen_ids:
            raise KnowledgeCaptureValidation("question option IDs must be unique")
        seen_ids.add(normalized_id)
        options.append({"id": option_id, "label": label})
    return options


def _resolve_question_option(options_json: Any, option_id: Any) -> dict[str, str]:
    selected_id = _bounded_text(option_id, field="option_id", limit=200, required=True)
    assert selected_id is not None
    for option in _safe_question_options(options_json):
        if option["id"].casefold() == selected_id.casefold():
            return option
    raise KnowledgeCaptureValidation("selected question option is unavailable")


def normalize_knowledge_semantic_key(value: Any) -> str | None:
    """Normalize a stable worker-provided semantic key without inventing one."""

    if value in (None, ""):
        return None
    normalized = " ".join(str(value).replace("\x00", "").casefold().split())
    return normalized[:255] or None


def _now(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.utcnow()
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _utc_naive(value: datetime) -> datetime:
    """Normalize an optional caller cutoff to the audit clock.

    TaskActivity.created_at and the worker cutoff are UTC-naive values.  Keep
    this conversion local to recovery so Task.completed_at remains a display
    and compatibility field with its existing wall-clock contract.
    """

    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _expected_version(expected: int | None, current: Any) -> int:
    current_value = int(current or 1)
    if expected is None:
        return current_value
    try:
        normalized = int(expected)
    except (TypeError, ValueError) as exc:
        raise KnowledgeCaptureValidation("expected_version must be an integer") from exc
    if normalized < 1:
        raise KnowledgeCaptureValidation("expected_version must be positive")
    return normalized


def _scalar_one_or_none(result: Any) -> Any:
    """Read SQLAlchemy and lightweight test-double result shapes alike."""

    scalar_one_or_none = getattr(result, "scalar_one_or_none", None)
    if callable(scalar_one_or_none):
        return scalar_one_or_none()
    scalars = result.scalars()
    first = getattr(scalars, "first", None)
    if callable(first):
        return first()
    rows = getattr(scalars, "all", lambda: list(scalars))()
    return rows[0] if rows else None


def _canonical_task_status(value: Any) -> str:
    """Use the task service's canonical vocabulary, preserving no aliases."""

    try:
        return normalize_task_status(str(value or ""))
    except Exception:
        return str(value or "").strip().lower()


def canonical_task_status(value: Any) -> str:
    """Public status projection used by recovery/tests."""

    return _canonical_task_status(value)


def is_terminal_task_status(value: Any) -> bool:
    return _canonical_task_status(value) in TASK_TERMINAL_STATUSES


def is_successful_capture_status(value: Any) -> bool:
    return _canonical_task_status(value) in SUCCESSFUL_CAPTURE_TERMINAL_STATUSES


def completion_fingerprint(
    task: Any,
    *,
    task_activity_id: UUID | str | None = None,
) -> str:
    """Build a stable identity for one successful completion occurrence.

    ``completed_at`` is preferred so an ordinary post-completion edit does not
    create a new candidate.  Canonical close/reopen/close writes assign a new
    completion timestamp; callers with an activity identity can supply it for
    legacy rows that do not have one.
    """

    task_id = _uuid(getattr(task, "id", None), field="task.id")
    project_id = _uuid(getattr(task, "project_id", None), field="task.project_id")
    status = _canonical_task_status(getattr(task, "status", None))
    if status not in SUCCESSFUL_CAPTURE_TERMINAL_STATUSES:
        raise KnowledgeCaptureValidation("task is not successfully completed")
    activity_id = _uuid(task_activity_id, field="task_activity_id", required=False)
    completed_at = getattr(task, "completed_at", None)
    updated_at = getattr(task, "updated_at", None)
    if activity_id is not None:
        completion_marker = str(activity_id)
    elif completed_at is not None:
        completion_marker = _now(completed_at).isoformat()
    elif updated_at is not None:
        completion_marker = _now(updated_at).isoformat()
    else:
        # Legacy closed rows have no generation marker.  They are recovered
        # once by task identity; canonical lifecycle hooks always provide a
        # completed_at value for later reopen/new-completion distinction.
        completion_marker = "legacy-completion"
    payload = json.dumps(
        {
            "project_id": str(project_id),
            "task_id": str(task_id),
            "status": status,
            "completion_marker": completion_marker,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


async def get_project_knowledge_capture_mode(
    session: AsyncSession,
    project_id: UUID | str,
) -> str:
    """Resolve a Project mode; no row intentionally means ``suggest``."""

    project_uuid = _uuid(project_id, field="project_id")
    result = await session.execute(
        select(ProjectKnowledgeCaptureSetting).where(
            ProjectKnowledgeCaptureSetting.project_id == project_uuid
        )
    )
    scalar_one_or_none = getattr(result, "scalar_one_or_none", None)
    if callable(scalar_one_or_none):
        setting = scalar_one_or_none()
    else:
        scalars = result.scalars()
        first = getattr(scalars, "first", None)
        if callable(first):
            setting = first()
        else:
            rows = getattr(scalars, "all", lambda: list(scalars))()
            setting = rows[0] if rows else None
    if setting is None:
        return "suggest"
    try:
        return normalize_knowledge_capture_mode(
            getattr(setting, "mode", DEFAULT_KNOWLEDGE_CAPTURE_MODE)
        )
    except ValueError as exc:
        raise KnowledgeCaptureValidation(str(exc)) from exc


resolve_project_knowledge_capture_mode = get_project_knowledge_capture_mode


async def upsert_project_knowledge_capture_setting(
    session: AsyncSession,
    project_id: UUID | str,
    mode: str,
    *,
    expected_version: int | None = None,
    updated_by_user_id: UUID | str | None = None,
) -> ProjectKnowledgeCaptureSetting:
    """Create/update a setting without committing the caller's transaction."""

    project_uuid = _uuid(project_id, field="project_id")
    updated_by_uuid = _uuid(
        updated_by_user_id,
        field="updated_by_user_id",
        required=False,
    )
    try:
        normalized_mode = normalize_knowledge_capture_mode(mode)
    except ValueError as exc:
        raise KnowledgeCaptureValidation(str(exc)) from exc
    result = await session.execute(
        select(ProjectKnowledgeCaptureSetting)
        .where(ProjectKnowledgeCaptureSetting.project_id == project_uuid)
        .with_for_update()
    )
    setting = _scalar_one_or_none(result)
    if setting is None:
        if expected_version is not None:
            raise KnowledgeCaptureConflict("setting version conflict")
        setting = ProjectKnowledgeCaptureSetting(
            project_id=project_uuid,
            mode=normalized_mode,
            version=1,
            updated_by=updated_by_uuid,
        )
        session.add(setting)
        return setting
    expected = _expected_version(expected_version, setting.version)
    if int(setting.version or 1) != expected:
        raise KnowledgeCaptureConflict("setting version conflict")
    setting.mode = normalized_mode
    setting.updated_by = updated_by_uuid
    setting.version = expected + 1
    setting.updated_at = datetime.utcnow()
    return setting


async def enqueue_for_completed_task(
    session: AsyncSession,
    task: Any,
    trigger_user_id: UUID | str | None,
    task_activity_id: UUID | str | None = None,
) -> KnowledgeCaptureCandidate | None:
    """Idempotently enqueue one successfully completed Task.

    The helper only writes a durable candidate row and never commits.  It is
    safe to call from the same transaction as the Task status/activity write.
    """

    project_uuid = _uuid(
        getattr(task, "project_id", None), field="task.project_id", required=False
    )
    task_uuid = _uuid(getattr(task, "id", None), field="task.id", required=False)
    # Lightweight task doubles used by legacy service tests are not durable
    # rows.  The canonical ORM Task always has both IDs; skip only these
    # non-persistent compatibility objects.
    if project_uuid is None or task_uuid is None:
        return None
    if not is_successful_capture_status(getattr(task, "status", None)):
        return None
    mode = await get_project_knowledge_capture_mode(session, project_uuid)
    if mode == "off":
        return None
    activity_uuid = _uuid(task_activity_id, field="task_activity_id", required=False)
    trigger_uuid = _uuid(trigger_user_id, field="trigger_user_id", required=False)
    fingerprint = completion_fingerprint(task, task_activity_id=activity_uuid)
    result = await session.execute(
        select(KnowledgeCaptureCandidate).where(
            KnowledgeCaptureCandidate.completion_fingerprint == fingerprint
        )
    )
    existing = _scalar_one_or_none(result)
    if existing is not None:
        return existing

    completed_at = getattr(task, "completed_at", None)
    evidence_refs: list[dict[str, Any]] = [{"type": "task", "id": str(task_uuid)}]
    if activity_uuid is not None:
        evidence_refs.append({"type": "task_activity", "id": str(activity_uuid)})
    candidate = KnowledgeCaptureCandidate(
        project_id=project_uuid,
        seed_task_id=task_uuid,
        task_activity_id=activity_uuid,
        trigger_user_id=trigger_uuid,
        completion_fingerprint=fingerprint,
        terminal_status="closed",
        status=KNOWLEDGE_CAPTURE_QUEUED_STATE,
        mode_snapshot=mode,
        version=1,
        evidence_refs=evidence_refs,
        attempt_count=0,
        max_attempts=CAPTURE_MAX_ATTEMPTS,
        completed_at=completed_at,
    )
    # The unique fingerprint is the final race boundary.  A nested savepoint
    # lets a concurrent duplicate lose without poisoning the caller's larger
    # Task transaction; no commit is performed here.
    try:
        async with session.begin_nested():
            session.add(candidate)
            await session.flush()
    except IntegrityError:
        result = await session.execute(
            select(KnowledgeCaptureCandidate).where(
                KnowledgeCaptureCandidate.completion_fingerprint == fingerprint
            )
        )
        existing = _scalar_one_or_none(result)
        if existing is None:
            raise
        return existing
    return candidate


async def recover_missing_candidates(
    session: AsyncSession,
    *,
    limit: int = 100,
    completed_after: datetime | None = None,
) -> dict[str, int]:
    """Recover missing completion candidates within an optional rollout window.

    ``completed_after`` is the worker safety boundary.  When it is supplied,
    legacy rows without ``completed_at`` and completions before that boundary
    are intentionally excluded so periodic recovery cannot become an
    accidental historical backfill.

    ``None`` keeps the explicit maintenance behavior for callers that
    intentionally request broad recovery.
    """

    bounded_limit = max(1, min(int(limit or 100), CAPTURE_MAX_RECOVERY_SCAN))
    recovery_cutoff = (
        _utc_naive(completed_after) if completed_after is not None else None
    )
    current_episode_captured = exists(
        select(KnowledgeCaptureCandidate.id).where(
            KnowledgeCaptureCandidate.seed_task_id == Task.id,
            KnowledgeCaptureCandidate.terminal_status == "closed",
            or_(
                KnowledgeCaptureCandidate.completed_at == Task.completed_at,
                and_(
                    KnowledgeCaptureCandidate.completed_at.is_(None),
                    Task.completed_at.is_(None),
                ),
            ),
        )
    )
    capture_disabled = exists(
        select(ProjectKnowledgeCaptureSetting.project_id).where(
            ProjectKnowledgeCaptureSetting.project_id == Task.project_id,
            ProjectKnowledgeCaptureSetting.mode == "off",
        )
    )
    filters = [
        Project.deleted_at.is_(None),
        or_(Project.is_completed.is_(False), Project.is_completed.is_(None)),
        Task.deleted_at.is_(None),
        Task.archived_at.is_(None),
        Task.status.in_(('closed', 'done')),
        ~current_episode_captured,
        ~capture_disabled,
    ]
    completion_activities: list[TaskActivity | None]
    if recovery_cutoff is None:
        # Explicit maintenance callers retain the historical behavior.  In
        # particular, legacy closed Tasks without an audit row remain eligible.
        result = await session.execute(
            select(Task)
            .join(Project, Project.id == Task.project_id)
            .where(*filters)
            .order_by(
                Task.completed_at.asc().nullsfirst(),
                Task.updated_at.asc(),
                Task.id.asc(),
            )
            .limit(bounded_limit)
        )
        tasks = list(result.scalars().all())
        completion_activities = [None] * len(tasks)
    else:
        # Periodic recovery is intentionally activity-driven.  Pick the
        # latest lifecycle marker for each Task, not merely any historical
        # close marker, so close -> reopen cannot resurrect the old episode.
        completion_activity = aliased(TaskActivity)
        later_activity = aliased(TaskActivity)
        completion_status = func.lower(
            func.coalesce(
                completion_activity.payload["status"].as_string(),
                "",
            )
        )
        completion_previous_status = func.lower(
            func.coalesce(
                completion_activity.payload["previous_status"].as_string(),
                "",
            )
        )
        later_status = func.lower(
            func.coalesce(
                later_activity.payload["status"].as_string(),
                "",
            )
        )
        lifecycle_types = (
            "task_created",
            "task_updated",
            "occurrence_updated",
        )
        completion_event = or_(
            and_(
                completion_activity.activity_type == "closed_by_parent",
                ~completion_previous_status.in_(("closed", "cancelled")),
            ),
            completion_activity.activity_type == "task_auto_closed_on_due",
            and_(
                completion_activity.activity_type.in_(lifecycle_types),
                completion_status.in_(("closed", "done")),
                ~completion_previous_status.in_(("closed", "cancelled")),
            ),
        )
        lifecycle_event = or_(
            later_activity.activity_type.in_(
                ("closed_by_parent", "task_auto_closed_on_due")
            ),
            and_(
                later_activity.activity_type.in_(lifecycle_types),
                later_status != "",
            ),
        )
        later_lifecycle = exists(
            select(later_activity.id).where(
                later_activity.task_id == Task.id,
                lifecycle_event,
                or_(
                    later_activity.created_at > completion_activity.created_at,
                    and_(
                        later_activity.created_at == completion_activity.created_at,
                        later_activity.id > completion_activity.id,
                    ),
                ),
            )
        )
        setting_changed_after_completion = exists(
            select(ProjectKnowledgeCaptureSetting.id).where(
                ProjectKnowledgeCaptureSetting.project_id == Task.project_id,
                ProjectKnowledgeCaptureSetting.updated_at
                > completion_activity.created_at,
            )
        )
        current_episode_captured_for_activity = exists(
            select(KnowledgeCaptureCandidate.id).where(
                KnowledgeCaptureCandidate.seed_task_id == Task.id,
                KnowledgeCaptureCandidate.terminal_status == "closed",
                or_(
                    KnowledgeCaptureCandidate.task_activity_id
                    == completion_activity.id,
                    and_(
                        KnowledgeCaptureCandidate.task_activity_id.is_(None),
                        KnowledgeCaptureCandidate.completed_at
                        == Task.completed_at,
                    ),
                ),
            )
        )
        filters.extend(
            [
                Task.completed_at.is_not(None),
                Task.status.in_(("closed", "done")),
                completion_activity.created_at >= recovery_cutoff,
                completion_event,
                ~later_lifecycle,
                ~setting_changed_after_completion,
                ~current_episode_captured_for_activity,
            ]
        )
        result = await session.execute(
            select(Task, completion_activity)
            .join(
                completion_activity,
                completion_activity.task_id == Task.id,
            )
            .join(Project, Project.id == Task.project_id)
            .where(*filters)
            .order_by(
                completion_activity.created_at.asc(),
                completion_activity.id.asc(),
                Task.id.asc(),
            )
            .limit(bounded_limit)
        )
        rows = list(result.all())
        tasks = [row[0] for row in rows]
        completion_activities = [row[1] for row in rows]
    stats = {"scanned": 0, "enqueued": 0, "skipped": 0}
    for task, completion_activity in zip(tasks, completion_activities):
        stats["scanned"] += 1
        if not is_successful_capture_status(getattr(task, "status", None)):
            stats["skipped"] += 1
            continue
        activity_id = (
            getattr(completion_activity, "id", None)
            if completion_activity is not None
            else None
        )
        fingerprint = completion_fingerprint(
            task,
            task_activity_id=activity_id,
        )
        existing_result = await session.execute(
            select(KnowledgeCaptureCandidate.id).where(
                or_(
                    KnowledgeCaptureCandidate.completion_fingerprint == fingerprint,
                    and_(
                        KnowledgeCaptureCandidate.seed_task_id == task.id,
                        or_(
                            KnowledgeCaptureCandidate.completed_at
                            == getattr(task, "completed_at", None),
                            and_(
                                activity_id is not None,
                                KnowledgeCaptureCandidate.task_activity_id
                                == activity_id,
                            ),
                        ),
                    ),
                )
            )
        )
        if _scalar_one_or_none(existing_result) is not None:
            stats["skipped"] += 1
            continue
        candidate = await enqueue_for_completed_task(
            session,
            task,
            getattr(completion_activity, "user_id", None)
            if completion_activity is not None
            else getattr(task, "created_by", None),
            task_activity_id=activity_id,
        )
        if candidate is None:
            stats["skipped"] += 1
        else:
            stats["enqueued"] += 1
    return stats


recovery_scan = recover_missing_candidates
enqueue_recovery_scan = recover_missing_candidates


async def _load_candidate(
    session: AsyncSession,
    candidate_id: UUID | str,
    *,
    lock: bool = False,
) -> KnowledgeCaptureCandidate:
    candidate_uuid = _uuid(candidate_id, field="candidate_id")
    statement = select(KnowledgeCaptureCandidate).where(
        KnowledgeCaptureCandidate.id == candidate_uuid
    )
    if lock:
        statement = statement.with_for_update()
    result = await session.execute(statement)
    candidate = _scalar_one_or_none(result)
    if candidate is None:
        raise KnowledgeCaptureNotFound("knowledge capture candidate not found")
    return candidate


def _lease_seconds(value: float) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise KnowledgeCaptureValidation("lease_seconds must be numeric") from exc
    return max(1.0, min(seconds, CAPTURE_MAX_LEASE_SECONDS))


def retry_delay_seconds(attempt_count: int) -> float:
    """Return a bounded deterministic exponential retry delay."""

    try:
        attempt = max(1, int(attempt_count))
    except (TypeError, ValueError):
        attempt = 1
    return min(CAPTURE_MAX_RETRY_SECONDS, CAPTURE_RETRY_BASE_SECONDS * (2 ** min(attempt - 1, 12)))


async def claim_candidate(
    session: AsyncSession,
    candidate_id: UUID | str | None = None,
    *,
    worker_id: str,
    lease_seconds: float = CAPTURE_DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> KnowledgeCaptureCandidate | None:
    """Claim one pending/retry candidate with a fenced lease."""

    owner = _bounded_text(worker_id, field="worker_id", limit=128, required=True)
    current_time = _now(now)
    lease_duration = _lease_seconds(lease_seconds)
    if candidate_id is None:
        statement = (
            select(KnowledgeCaptureCandidate)
            .where(
                KnowledgeCaptureCandidate.status.in_(tuple(CAPTURE_CLAIMABLE_STATES)),
                or_(
                    KnowledgeCaptureCandidate.next_retry_at.is_(None),
                    KnowledgeCaptureCandidate.next_retry_at <= current_time,
                ),
                or_(
                    KnowledgeCaptureCandidate.lease_expires_at.is_(None),
                    KnowledgeCaptureCandidate.lease_expires_at <= current_time,
                ),
            )
            .order_by(KnowledgeCaptureCandidate.created_at.asc(), KnowledgeCaptureCandidate.id.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    else:
        candidate_uuid = _uuid(candidate_id, field="candidate_id")
        statement = (
            select(KnowledgeCaptureCandidate)
            .where(KnowledgeCaptureCandidate.id == candidate_uuid)
            .with_for_update()
        )
    result = await session.execute(statement)
    candidate = _scalar_one_or_none(result)
    if candidate is None:
        return None
    if candidate_id is not None and candidate.status not in CAPTURE_CLAIMABLE_STATES:
        raise KnowledgeCaptureConflict("candidate is not claimable")
    if candidate.next_retry_at is not None and candidate.next_retry_at > current_time:
        return None
    if candidate.lease_expires_at is not None and candidate.lease_expires_at > current_time:
        return None
    if int(candidate.attempt_count or 0) >= int(candidate.max_attempts or CAPTURE_MAX_ATTEMPTS):
        validate_candidate_transition(candidate.status, "failed")
        candidate.status = "failed"
        candidate.version = int(candidate.version or 1) + 1
        candidate.last_error_code = "max_attempts_exhausted"
        candidate.updated_at = current_time
        return None
    token = uuid4().hex
    expected = int(candidate.version or 1)
    candidate.status = "researching"
    candidate.attempt_count = int(candidate.attempt_count or 0) + 1
    candidate.lease_owner = owner
    candidate.lease_token = token
    candidate.lease_expires_at = current_time + timedelta(seconds=lease_duration)
    candidate.heartbeat_at = current_time
    candidate.next_retry_at = None
    candidate.last_error_code = None
    candidate.last_error_message = None
    candidate.version = expected + 1
    candidate.updated_at = current_time
    await session.flush()
    return candidate


async def claim_next_candidate(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: float = CAPTURE_DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> KnowledgeCaptureCandidate | None:
    return await claim_candidate(
        session,
        None,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        now=now,
    )


async def claim_candidates(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int = 1,
    lease_seconds: float = CAPTURE_DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> list[KnowledgeCaptureCandidate]:
    bounded_limit = max(1, min(int(limit or 1), CAPTURE_MAX_CLAIM_BATCH))
    rows: list[KnowledgeCaptureCandidate] = []
    for _ in range(bounded_limit):
        row = await claim_next_candidate(
            session,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            now=now,
        )
        if row is None:
            break
        rows.append(row)
    return rows


async def renew_candidate_lease(
    session: AsyncSession,
    candidate_id: UUID | str,
    *,
    worker_id: str,
    lease_token: str,
    expected_version: int | None = None,
    lease_seconds: float = CAPTURE_DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> KnowledgeCaptureCandidate:
    candidate = await _load_candidate(session, candidate_id, lock=True)
    expected = _expected_version(expected_version, candidate.version)
    if int(candidate.version or 1) != expected:
        raise KnowledgeCaptureConflict("candidate version conflict")
    if (
        candidate.status not in CAPTURE_ACTIVE_STATES
        or candidate.lease_owner != worker_id
        or candidate.lease_token != lease_token
        or candidate.lease_expires_at is None
        or candidate.lease_expires_at <= _now(now)
    ):
        raise KnowledgeCaptureConflict("candidate lease is not active")
    current_time = _now(now)
    candidate.heartbeat_at = current_time
    candidate.lease_expires_at = current_time + timedelta(seconds=_lease_seconds(lease_seconds))
    candidate.version = expected + 1
    candidate.updated_at = current_time
    await session.flush()
    return candidate


async def transition_candidate(
    session: AsyncSession,
    candidate_id: UUID | str,
    target_status: str,
    *,
    expected_version: int | None = None,
    lease_owner: str | None = None,
    lease_token: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    published_node_id: UUID | str | None = None,
    target_node_id: UUID | str | None = None,
    target_revision_id: UUID | str | None = None,
    published_revision_id: UUID | str | None = None,
    knowledge_semantic_key: str | None = None,
    dismissed_by_user_id: UUID | str | None = None,
    now: datetime | None = None,
) -> KnowledgeCaptureCandidate:
    """Apply a legal version/lease-fenced candidate state transition."""

    candidate = await _load_candidate(session, candidate_id, lock=True)
    expected = _expected_version(expected_version, candidate.version)
    if int(candidate.version or 1) != expected:
        raise KnowledgeCaptureConflict("candidate version conflict")
    target = str(target_status or "").strip().lower()
    try:
        validate_candidate_transition(candidate.status, target)
    except ValueError as exc:
        raise KnowledgeCaptureConflict(str(exc)) from exc
    if lease_owner is not None or lease_token is not None:
        if candidate.lease_owner != lease_owner or candidate.lease_token != lease_token:
            raise KnowledgeCaptureConflict("candidate lease fence mismatch")
        if candidate.lease_expires_at is None or candidate.lease_expires_at <= _now(now):
            raise KnowledgeCaptureConflict("candidate lease is expired")
    current_time = _now(now)
    candidate.status = target
    candidate.version = expected + 1
    candidate.updated_at = current_time
    if error_code is not None:
        candidate.last_error_code = _bounded_text(error_code, field="error_code", limit=96)
    if error_message is not None:
        candidate.last_error_message = _bounded_text(
            error_message,
            field="error_message",
            limit=512,
        )
    if published_node_id is not None:
        candidate.published_node_id = _uuid(
            published_node_id,
            field="published_node_id",
            required=False,
        )
    if target_node_id is not None:
        candidate.target_node_id = _uuid(
            target_node_id,
            field="target_node_id",
            required=False,
        )
    if target_revision_id is not None:
        candidate.target_revision_id = _uuid(
            target_revision_id,
            field="target_revision_id",
            required=False,
        )
    if published_revision_id is not None:
        candidate.published_revision_id = _uuid(
            published_revision_id,
            field="published_revision_id",
            required=False,
        )
    if knowledge_semantic_key is not None:
        candidate.knowledge_semantic_key = _bounded_text(
            knowledge_semantic_key,
            field="knowledge_semantic_key",
            limit=255,
        )
    if target == "dismissed":
        candidate.dismissed_at = current_time
        candidate.dismissed_by = _uuid(
            dismissed_by_user_id,
            field="dismissed_by_user_id",
            required=False,
        )
    if target != "researching":
        candidate.lease_owner = None
        candidate.lease_token = None
        candidate.lease_expires_at = None
        candidate.heartbeat_at = None
    if target not in {"retry_wait"}:
        candidate.next_retry_at = None
    await session.flush()
    return candidate


async def retry_candidate(
    session: AsyncSession,
    candidate_id: UUID | str,
    *,
    worker_id: str,
    lease_token: str,
    expected_version: int | None = None,
    error_code: str = "capture_retryable_error",
    error_message: str | None = None,
    now: datetime | None = None,
) -> KnowledgeCaptureCandidate:
    candidate = await _load_candidate(session, candidate_id, lock=True)
    expected = _expected_version(expected_version, candidate.version)
    if int(candidate.version or 1) != expected:
        raise KnowledgeCaptureConflict("candidate version conflict")
    current_time = _now(now)
    if (
        candidate.status != "researching"
        or candidate.lease_owner != worker_id
        or candidate.lease_token != lease_token
        or candidate.lease_expires_at is None
        or candidate.lease_expires_at <= current_time
    ):
        raise KnowledgeCaptureConflict("candidate lease is not active")
    attempts = int(candidate.attempt_count or 0)
    target = "retry_wait" if attempts < int(candidate.max_attempts or CAPTURE_MAX_ATTEMPTS) else "failed"
    delay = retry_delay_seconds(attempts)
    candidate.status = target
    candidate.version = expected + 1
    candidate.lease_owner = None
    candidate.lease_token = None
    candidate.lease_expires_at = None
    candidate.heartbeat_at = None
    candidate.next_retry_at = (
        current_time + timedelta(seconds=delay) if target == "retry_wait" else None
    )
    candidate.last_error_code = _bounded_text(error_code, field="error_code", limit=96)
    candidate.last_error_message = _bounded_text(
        error_message,
        field="error_message",
        limit=512,
    )
    candidate.updated_at = current_time
    await session.flush()
    return candidate


async def recover_expired_candidate_leases(
    session: AsyncSession,
    *,
    limit: int = 100,
    now: datetime | None = None,
) -> dict[str, int]:
    """Return expired researching rows to retry or fail, bounded by ``limit``."""

    current_time = _now(now)
    bounded_limit = max(1, min(int(limit or 100), CAPTURE_MAX_RECOVERY_SCAN))
    result = await session.execute(
        select(KnowledgeCaptureCandidate)
        .where(
            KnowledgeCaptureCandidate.status == "researching",
            KnowledgeCaptureCandidate.lease_expires_at.is_not(None),
            KnowledgeCaptureCandidate.lease_expires_at <= current_time,
        )
        .order_by(KnowledgeCaptureCandidate.lease_expires_at.asc(), KnowledgeCaptureCandidate.id.asc())
        .limit(bounded_limit)
        .with_for_update(skip_locked=True)
    )
    rows = list(result.scalars().all())
    recovered = 0
    failed = 0
    for candidate in rows:
        attempts = int(candidate.attempt_count or 0)
        target = "retry_wait" if attempts < int(candidate.max_attempts or CAPTURE_MAX_ATTEMPTS) else "failed"
        candidate.status = target
        candidate.version = int(candidate.version or 1) + 1
        candidate.lease_owner = None
        candidate.lease_token = None
        candidate.lease_expires_at = None
        candidate.heartbeat_at = current_time
        candidate.next_retry_at = current_time if target == "retry_wait" else None
        candidate.last_error_code = (
            "stale_lease_recovered" if target == "retry_wait" else "max_attempts_exhausted"
        )
        candidate.last_error_message = None
        candidate.updated_at = current_time
        if target == "retry_wait":
            recovered += 1
        else:
            failed += 1
    await session.flush()
    return {"recovered": recovered, "failed": failed}


recover_stale_leases = recover_expired_candidate_leases
recover_leases = recover_expired_candidate_leases


async def create_question(
    session: AsyncSession,
    candidate_id: UUID | str,
    question: str | None = None,
    *,
    round_number: int | None = None,
    asked_by_user_id: UUID | str | None = None,
    expected_candidate_version: int | None = None,
    title: str | None = None,
    message: str | None = None,
    options_json: Any = None,
    evidence_digest: str | None = None,
) -> KnowledgeCaptureQuestion:
    """Create one question, enforcing two rounds and one pending row."""

    candidate = await _load_candidate(session, candidate_id, lock=True)
    expected = _expected_version(expected_candidate_version, candidate.version)
    if int(candidate.version or 1) != expected:
        raise KnowledgeCaptureConflict("candidate version conflict")
    if candidate.status in {"published", "dismissed", "discarded", "superseded", "failed"}:
        raise KnowledgeCaptureConflict("candidate cannot receive a question")
    text_value = _bounded_text(
        message if message not in (None, "") else question,
        field="question",
        limit=2000,
        required=True,
    )
    title_value = _bounded_text(title, field="title", limit=240)
    message_value = _bounded_text(message, field="message", limit=2000)
    options = _safe_question_options(options_json)
    evidence_value = _bounded_text(
        evidence_digest,
        field="evidence_digest",
        limit=64,
    )
    pending_result = await session.execute(
        select(KnowledgeCaptureQuestion).where(
            KnowledgeCaptureQuestion.candidate_id == candidate.id,
            KnowledgeCaptureQuestion.status == "pending",
        )
    )
    if _scalar_one_or_none(pending_result) is not None:
        raise KnowledgeCaptureConflict("candidate already has a pending question")
    max_result = await session.execute(
        select(func.max(KnowledgeCaptureQuestion.round_number)).where(
            KnowledgeCaptureQuestion.candidate_id == candidate.id
        )
    )
    highest = int(_scalar_one_or_none(max_result) or 0)
    requested_round = highest + 1 if round_number is None else int(round_number)
    if requested_round < 1 or requested_round > 2:
        raise KnowledgeCaptureValidation("knowledge capture question rounds are limited to two")
    if requested_round <= highest:
        raise KnowledgeCaptureConflict("knowledge capture question round already exists")
    asked_uuid = _uuid(asked_by_user_id, field="asked_by_user_id", required=False)
    row = KnowledgeCaptureQuestion(
        candidate_id=candidate.id,
        project_id=candidate.project_id,
        round_number=requested_round,
        status="pending",
        question=text_value,
        title=title_value,
        message=message_value,
        options_json=options,
        evidence_digest=evidence_value,
        candidate_version=expected + 1,
        asked_by_user_id=asked_uuid,
        version=1,
    )
    validate_candidate_transition(candidate.status, "needs_user")
    candidate.status = "needs_user"
    candidate.question_rounds = max(int(candidate.question_rounds or 0), requested_round)
    candidate.version = expected + 1
    candidate.updated_at = datetime.utcnow()
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise KnowledgeCaptureConflict("candidate question uniqueness conflict") from exc
    return row


ask_question = create_question
create_pending_question = create_question


async def answer_question(
    session: AsyncSession,
    question_id: UUID | str,
    answer: str | None,
    *,
    option_id: str | None = None,
    expected_candidate_id: UUID | str | None = None,
    expected_version: int | None = None,
    answered_by_user_id: UUID | str | None = None,
    answer_source_refs: Any = None,
    expected_candidate_version: int | None = None,
) -> KnowledgeCaptureQuestion:
    """Resolve one user answer against the durable question contract.

    Option answers become their semantic label before they can become
    authoritative evidence. ``dont_save`` is workflow intent and dismisses the
    candidate instead of requeueing Curator research.
    """

    question_uuid = _uuid(question_id, field="question_id")
    result = await session.execute(
        select(KnowledgeCaptureQuestion)
        .where(KnowledgeCaptureQuestion.id == question_uuid)
        .with_for_update()
    )
    row = _scalar_one_or_none(result)
    if row is None:
        raise KnowledgeCaptureNotFound("knowledge capture question not found")
    # The question UUID is not sufficient authorization for this route: the
    # URL also carries the candidate UUID.  Check the locked question binding
    # before resolving options or loading the candidate so a question from a
    # different candidate/project can never mutate its owner through a guessed
    # URL candidate id.
    if expected_candidate_id is not None:
        expected_candidate_uuid = _uuid(
            expected_candidate_id,
            field="expected_candidate_id",
        )
        question_candidate_uuid = _uuid(
            row.candidate_id,
            field="question.candidate_id",
        )
        if question_candidate_uuid != expected_candidate_uuid:
            raise KnowledgeCaptureConflict("question candidate binding mismatch")
    expected = _expected_version(expected_version, row.version)
    if int(row.version or 1) != expected:
        raise KnowledgeCaptureConflict("question version conflict")
    if row.status != "pending":
        raise KnowledgeCaptureConflict("question is not pending")

    selected_option = (
        _resolve_question_option(row.options_json, option_id)
        if option_id not in (None, "")
        else None
    )
    candidate = await _load_candidate(session, row.candidate_id, lock=True)
    candidate_expected = _expected_version(expected_candidate_version, candidate.version)
    if int(candidate.version or 1) != candidate_expected:
        raise KnowledgeCaptureConflict("candidate version conflict")
    if (
        row.candidate_version is not None
        and int(candidate.version or 1) != int(row.candidate_version)
    ):
        raise KnowledgeCaptureConflict("question candidate binding is stale")
    if candidate.status in {"published", "dismissed", "discarded", "superseded", "failed"}:
        raise KnowledgeCaptureConflict("candidate cannot receive an answer")

    answered_uuid = _uuid(answered_by_user_id, field="answered_by_user_id", required=False)
    now = datetime.utcnow()
    source_refs = _safe_refs(answer_source_refs)
    if selected_option is not None:
        source_refs = [*source_refs, {"type": "question_option", "id": selected_option["id"]}]

    if selected_option is not None and selected_option["id"].casefold() == "dont_save":
        answer_value = selected_option["label"]
        validate_question_transition(row.status, "dismissed")
        validate_candidate_transition(candidate.status, "dismissed")
        row.answer = answer_value
        row.answer_source_refs = source_refs
        row.status = "dismissed"
        row.answered_by_user_id = answered_uuid
        row.answered_at = now
        row.dismissed_at = now
        row.version = expected + 1
        row.updated_at = now
        answers = list(candidate.answers_json or [])
        answers.append(
            {
                "question_id": str(row.id),
                "round_number": int(row.round_number),
                "answer_sha256": hashlib.sha256(answer_value.encode("utf-8")).hexdigest(),
                "option_id": selected_option["id"],
            }
        )
        candidate.answers_json = answers[-8:]
        candidate.status = "dismissed"
        candidate.dismissed_at = now
        candidate.dismissed_by = answered_uuid
        candidate.next_retry_at = None
        candidate.lease_owner = None
        candidate.lease_token = None
        candidate.lease_expires_at = None
        candidate.heartbeat_at = None
        candidate.version = candidate_expected + 1
        candidate.updated_at = now
        await session.flush()
        return row

    if selected_option is not None:
        answer_prefix = f"選択: {selected_option['label']}\n補足: "
        supplement = _bounded_text(
            answer,
            field="answer_supplement",
            limit=4000 - len(answer_prefix),
            required=False,
        )
        if supplement:
            answer_input = answer_prefix + supplement
        else:
            answer_input = selected_option["label"]
    else:
        answer_input = answer
    answer_value = _bounded_text(
        answer_input,
        field="answer",
        limit=4000,
        required=True,
    )
    assert answer_value is not None
    validate_question_transition(row.status, "answered")
    row.answer = answer_value
    row.answer_source_refs = source_refs
    row.status = "answered"
    row.answered_by_user_id = answered_uuid
    row.answered_at = now
    row.version = expected + 1
    row.updated_at = now
    answers = list(candidate.answers_json or [])
    answer_record = {
        "question_id": str(row.id),
        "round_number": int(row.round_number),
        "answer_sha256": hashlib.sha256(answer_value.encode("utf-8")).hexdigest(),
    }
    if selected_option is not None:
        answer_record["option_id"] = selected_option["id"]
    answers.append(answer_record)
    candidate.answers_json = answers[-8:]
    validate_candidate_transition(candidate.status, KNOWLEDGE_CAPTURE_QUEUED_STATE)
    candidate.status = KNOWLEDGE_CAPTURE_QUEUED_STATE
    candidate.next_retry_at = None
    candidate.version = candidate_expected + 1
    candidate.updated_at = now
    await session.flush()
    return row


answer_candidate_question = answer_question


async def dismiss_question(
    session: AsyncSession,
    question_id: UUID | str,
    *,
    expected_version: int | None = None,
    dismiss_candidate: bool = True,
    dismissed_by_user_id: UUID | str | None = None,
) -> KnowledgeCaptureQuestion:
    """Dismiss a pending question; by default close its unresolved candidate."""

    question_uuid = _uuid(question_id, field="question_id")
    result = await session.execute(
        select(KnowledgeCaptureQuestion)
        .where(KnowledgeCaptureQuestion.id == question_uuid)
        .with_for_update()
    )
    row = _scalar_one_or_none(result)
    if row is None:
        raise KnowledgeCaptureNotFound("knowledge capture question not found")
    expected = _expected_version(expected_version, row.version)
    if int(row.version or 1) != expected:
        raise KnowledgeCaptureConflict("question version conflict")
    if row.status != "pending":
        raise KnowledgeCaptureConflict("question is not pending")
    now = datetime.utcnow()
    row.status = "dismissed"
    row.dismissed_at = now
    row.version = expected + 1
    row.updated_at = now
    if dismiss_candidate:
        candidate = await _load_candidate(session, row.candidate_id, lock=True)
        if candidate.status not in {"published", "superseded", "dismissed", "discarded", "failed"}:
            validate_candidate_transition(candidate.status, "dismissed")
            candidate.status = "dismissed"
            candidate.dismissed_at = now
            candidate.dismissed_by = _uuid(
                dismissed_by_user_id,
                field="dismissed_by_user_id",
                required=False,
            )
            candidate.version = int(candidate.version or 1) + 1
            candidate.updated_at = now
    await session.flush()
    return row


dismiss_candidate_question = dismiss_question


async def edit_question(
    session: AsyncSession,
    question_id: UUID | str,
    question: str,
    *,
    expected_version: int | None = None,
) -> KnowledgeCaptureQuestion:
    """Edit only a pending question with an optimistic version check."""

    question_uuid = _uuid(question_id, field="question_id")
    result = await session.execute(
        select(KnowledgeCaptureQuestion)
        .where(KnowledgeCaptureQuestion.id == question_uuid)
        .with_for_update()
    )
    row = _scalar_one_or_none(result)
    if row is None:
        raise KnowledgeCaptureNotFound("knowledge capture question not found")
    expected = _expected_version(expected_version, row.version)
    if int(row.version or 1) != expected:
        raise KnowledgeCaptureConflict("question version conflict")
    if row.status != "pending":
        raise KnowledgeCaptureConflict("only pending questions can be edited")
    row.question = _bounded_text(question, field="question", limit=2000, required=True)
    row.version = expected + 1
    row.updated_at = datetime.utcnow()
    await session.flush()
    return row


async def edit_candidate(
    session: AsyncSession,
    candidate_id: UUID | str,
    *,
    draft_json: Mapping[str, Any],
    expected_version: int | None = None,
    knowledge_semantic_key: str | None = None,
    mark_user_edited: bool = True,
) -> KnowledgeCaptureCandidate:
    """Edit a reviewable draft without allowing terminal-row resurrection."""

    candidate = await _load_candidate(session, candidate_id, lock=True)
    expected = _expected_version(expected_version, candidate.version)
    if int(candidate.version or 1) != expected:
        raise KnowledgeCaptureConflict("candidate version conflict")
    if candidate.status not in {"needs_user", "draft_ready", "approved"}:
        raise KnowledgeCaptureConflict("candidate is not editable")
    if not isinstance(draft_json, Mapping):
        raise KnowledgeCaptureValidation("draft_json must be an object")
    # Browser edits are partial patches.  Preserve the server-validated
    # schema/evidence bindings instead of allowing a title/section edit to
    # erase citations and turn a publishable draft into an ungrounded one.
    existing = dict(candidate.draft_json or {})
    bounded = dict(existing)
    bounded.update(dict(list(draft_json.items())[:64]))
    for field in ("procedure", "verification", "pitfalls"):
        incoming = bounded.get(field)
        prior = existing.get(field)
        if not isinstance(incoming, list) or not isinstance(prior, list):
            continue
        merged_items = []
        for index, item in enumerate(incoming[:32]):
            if not isinstance(item, Mapping):
                continue
            projected = dict(item)
            if not projected.get("evidence_ids") and index < len(prior):
                prior_item = prior[index]
                if isinstance(prior_item, Mapping) and prior_item.get("evidence_ids"):
                    projected["evidence_ids"] = list(prior_item["evidence_ids"])
            merged_items.append(projected)
        bounded[field] = merged_items
    candidate.draft_json = bounded
    if knowledge_semantic_key is not None:
        candidate.knowledge_semantic_key = normalize_knowledge_semantic_key(
            knowledge_semantic_key
        )
    candidate.status = "draft_ready"
    if mark_user_edited:
        candidate.user_edited = True
    candidate.version = expected + 1
    candidate.updated_at = datetime.utcnow()
    await session.flush()
    return candidate


class KnowledgeCaptureCandidateService:
    """Class facade for workers/routes while keeping helpers transaction-local."""

    enqueue_for_completed_task = staticmethod(enqueue_for_completed_task)
    recover_missing_candidates = staticmethod(recover_missing_candidates)
    recovery_scan = staticmethod(recover_missing_candidates)
    get_project_knowledge_capture_mode = staticmethod(get_project_knowledge_capture_mode)
    upsert_project_knowledge_capture_setting = staticmethod(
        upsert_project_knowledge_capture_setting
    )
    claim_candidate = staticmethod(claim_candidate)
    claim_next_candidate = staticmethod(claim_next_candidate)
    claim_candidates = staticmethod(claim_candidates)
    renew_candidate_lease = staticmethod(renew_candidate_lease)
    transition_candidate = staticmethod(transition_candidate)
    retry_candidate = staticmethod(retry_candidate)
    recover_expired_candidate_leases = staticmethod(recover_expired_candidate_leases)
    create_question = staticmethod(create_question)
    ask_question = staticmethod(create_question)
    answer_question = staticmethod(answer_question)
    dismiss_question = staticmethod(dismiss_question)
    edit_question = staticmethod(edit_question)
    edit_candidate = staticmethod(edit_candidate)


__all__ = [
    "CAPTURE_ACTIVE_STATES",
    "CAPTURE_CLAIMABLE_STATES",
    "CAPTURE_DEFAULT_LEASE_SECONDS",
    "CAPTURE_MAX_ATTEMPTS",
    "CAPTURE_MAX_RECOVERY_SCAN",
    "CAPTURE_MAX_RETRY_SECONDS",
    "KnowledgeCaptureCandidateService",
    "KnowledgeCaptureConflict",
    "KnowledgeCaptureError",
    "KnowledgeCaptureNotFound",
    "KnowledgeCaptureValidation",
    "TASK_TERMINAL_STATUSES",
    "SUCCESSFUL_CAPTURE_TERMINAL_STATUSES",
    "answer_candidate_question",
    "answer_question",
    "ask_question",
    "canonical_task_status",
    "claim_candidate",
    "claim_candidates",
    "claim_next_candidate",
    "completion_fingerprint",
    "create_pending_question",
    "create_question",
    "dismiss_candidate_question",
    "dismiss_question",
    "edit_candidate",
    "edit_question",
    "enqueue_for_completed_task",
    "enqueue_recovery_scan",
    "get_project_knowledge_capture_mode",
    "is_successful_capture_status",
    "is_terminal_task_status",
    "normalize_knowledge_semantic_key",
    "recover_expired_candidate_leases",
    "recover_leases",
    "recover_missing_candidates",
    "recover_stale_leases",
    "recovery_scan",
    "renew_candidate_lease",
    "retry_candidate",
    "retry_delay_seconds",
    "transition_candidate",
    "upsert_project_knowledge_capture_setting",
]
