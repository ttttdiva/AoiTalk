"""Durable Heartbeat operational history.

The scheduler's :class:`~src.memory.models.automation.HeartbeatRunState` is a
mutable state machine.  This module owns the separate append-only projection
shown in Settings.  It intentionally stores only bounded counters, a boolean
continuation marker, sanitized questions, and a short result summary; the
encrypted scheduler cursor, evidence rows, prompts, tool payloads, and raw
exceptions never enter the history table.
"""

from __future__ import annotations

import base64
import binascii
import copy
import inspect
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import delete, or_, select

from ..memory.models import HeartbeatRunHistory, Project


logger = logging.getLogger(__name__)


# The values are deliberately lower than the database check's defensive
# ceiling.  A malformed provider response can therefore never make one row
# consume an unbounded amount of memory or JSON storage.
MAX_HISTORY_ROWS = 200
DEFAULT_RETENTION_DAYS = 30
MAX_PAGE_SIZE = 100
MAX_QUESTIONS = 16
MAX_QUESTION_TITLE_CHARS = 200
MAX_QUESTION_MESSAGE_CHARS = 500
MAX_RESULT_SUMMARY_CHARS = 512
MAX_COUNT = 10_000
MAX_CURSOR_BYTES = 512

_STATUS_ALIASES = {
    "ok": "succeeded",
    "success": "succeeded",
    "succeeded": "succeeded",
    "completed": "succeeded",
    "running": "running",
    "failed": "failed",
    "error": "failed",
    "timeout": "failed",
    "executor_unavailable": "failed",
    "cancelled": "failed",
    "partial_failure": "failed",
    "stale": "stale",
    "stale_running": "stale",
}
_SAFE_ERROR_CODES = frozenset(
    {
        "executor_unavailable",
        "execution_failed",
        "execution_timeout",
        "stale_running_recovered",
        "heartbeat_claim_lost",
        "history_persistence_failed",
        "invalid_result",
        "cancelled",
        "unknown_error",
    }
)
_SAFE_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SECRET_KEY_RE = re.compile(
    r"(?:^|[_\-.])(?:api[_\-.]?key|access[_\-.]?token|refresh[_\-.]?token|"
    r"auth(?:orization)?|bearer|password|passwd|passphrase|secret|credential|"
    r"private[_\-.]?key)(?:$|[_\-.])",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"(?:api[_\-.]?key|access[_\-.]?token|refresh[_\-.]?token|password|"
    r"secret)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)
_UNSAFE_SUMMARY_RE = re.compile(
    r"(?:raw|full|source)?\s*(?:evidence|chat|docs?|tool|prompt|transcript|payload)"
    r"(?:\s*(?:body|text|output|arguments?|data))?",
    re.IGNORECASE,
)


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def _safe_text(value: Any, *, limit: int) -> str:
    """Clip text and redact obvious credentials before persistence."""

    text = _clip(value, limit)
    if not text:
        return ""
    if "\x00" in text or any(
        ord(char) < 32 and char not in {"\n", "\t"} for char in text
    ):
        return ""
    if _SECRET_VALUE_RE.search(text):
        return "[REDACTED]"
    return text


def _safe_summary(value: Any) -> str:
    """Keep a stored closed summary bounded when reading legacy rows.

    Summaries are optional convenience metadata.  Require a scalar string and
    reject common evidence/prompt/body-shaped labels so a provider cannot
    accidentally turn this column into a transcript sink.
    """

    if not isinstance(value, str):
        return ""
    text = _safe_text(value, limit=MAX_RESULT_SUMMARY_CHARS)
    if not text or _UNSAFE_SUMMARY_RE.search(text):
        return ""
    return text


def _closed_result_summary(
    *,
    source: Mapping[str, Any],
    status_value: Any,
    normalized_status: str,
    memory_count: int,
    forgotten_count: int,
    question_count: int,
    continuation_pending: bool,
) -> str:
    """Return a fixed, non-provider-controlled operational summary.

    A model response is untrusted source material even when it does not look
    like a secret or an evidence body.  History therefore never accepts
    ``response``, ``summary``, or ``result_summary`` from a provider.  Project
    Steward gets the useful bounded counters; generic Heartbeats get a fixed
    outcome label.  Both shapes are closed over values derived from validated
    booleans/counts rather than arbitrary prose.
    """

    project_steward = (
        str(source.get("mode") or "").strip().casefold() == "project_steward"
        or any(
            key in source
            for key in (
                "memory_upsert_count",
                "memory_upserts",
                "forgotten_count",
                "forgotten",
                "question_count",
                "questions_count",
                "questions",
                "continuation_pending",
            )
        )
    )
    if project_steward:
        return (
            f"memory_upserts={memory_count}; forgotten={forgotten_count}; "
            f"questions={question_count}; "
            "continuation_pending="
            f"{'true' if continuation_pending else 'false'}"
        )

    raw_status = str(status_value or "").strip().casefold()
    if normalized_status == "succeeded":
        return "alert" if bool(source.get("is_alert")) or raw_status == "alert" else "ok"
    return normalized_status


def _bounded_count(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(MAX_COUNT, number))


def _safe_json_value(value: Any, *, depth: int = 0) -> Any:
    """Keep continuation/result metadata JSON-safe and tightly bounded.

    This helper is intentionally conservative.  It is used only for cursor
    values accepted as a convenience by callers; evidence/body/tool-shaped
    keys are omitted rather than copied into the operational log.
    """

    if depth > 3:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if value == value else None  # drop NaN
    if isinstance(value, str):
        return _safe_text(value, limit=200)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:32]:
            key = _clip(raw_key, 64)
            if not key or _SECRET_KEY_RE.search(key):
                continue
            lowered = key.casefold()
            if any(token in lowered for token in ("evidence", "body", "payload", "prompt", "transcript")):
                continue
            projected = _safe_json_value(raw_value, depth=depth + 1)
            if projected is not None:
                result[key] = projected
        return result
    if isinstance(value, (list, tuple, set)):
        return [
            projected
            for item in list(value)[:32]
            if (projected := _safe_json_value(item, depth=depth + 1)) is not None
        ]
    return _safe_text(value, limit=200)


def _safe_question(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    title = _safe_text(value.get("title"), limit=MAX_QUESTION_TITLE_CHARS)
    message = _safe_text(value.get("message"), limit=MAX_QUESTION_MESSAGE_CHARS)
    if not title and not message:
        return None
    urgency = str(value.get("urgency") or "normal").strip().casefold()
    if urgency not in {"low", "normal", "high"}:
        urgency = "normal"
    return {"title": title, "message": message, "urgency": urgency}


def _safe_questions(value: Any) -> list[dict[str, str]]:
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    questions: list[dict[str, str]] = []
    for item in list(value)[:MAX_QUESTIONS]:
        question = _safe_question(item)
        if question is not None:
            questions.append(question)
    return questions


def _action_counts(value: Any) -> tuple[int, int]:
    """Return aggregate action and action-failure counts only.

    Heartbeat action output/configuration can contain arbitrary external data;
    retaining the two counts gives operators useful signal without copying
    that payload into the durable log.
    """

    if not isinstance(value, (list, tuple)):
        return 0, 0
    total = min(MAX_COUNT, len(value))
    failures = 0
    for item in list(value)[:MAX_COUNT]:
        if isinstance(item, Mapping):
            status = str(item.get("status") or "").strip().casefold()
            if status in {"error", "failed", "failure"}:
                failures += 1
    return total, min(total, failures)


def normalize_status(value: Any, *, default: str = "succeeded") -> str:
    """Normalize provider/runner labels to the four history statuses."""

    status = str(value or "").strip().casefold()
    return _STATUS_ALIASES.get(status, default)


def normalize_safe_error_code(value: Any, *, default: str = "execution_failed") -> str:
    """Return an allow-listed error code, never a raw exception message."""

    code = str(value or "").strip().casefold().replace(" ", "_")
    if code in _SAFE_ERROR_CODES:
        return code
    # Accept future machine codes only when they are syntactically safe.  A
    # provider exception itself (spaces, stack traces, URLs) therefore falls
    # back to the generic code.
    if _SAFE_ERROR_CODE_RE.fullmatch(code) and len(code) <= 64:
        return code
    return default


def project_result(result: Mapping[str, Any] | None = None, *, status: Any = None, error_code: Any = None) -> dict[str, Any]:
    """Build the only result shape accepted by the durable history store.

    The function is public so Runner/Project Steward integration can project a
    provider result before deciding whether a history write failed.  No raw
    mapping is retained or returned.
    """

    source: Mapping[str, Any] = result if isinstance(result, Mapping) else {}
    status_value = status if status is not None else source.get("status")
    if status_value in (None, "") and error_code not in (None, ""):
        status_value = "failed"
    normalized_status = normalize_status(status_value)
    raw_questions = source.get("questions")
    questions = _safe_questions(raw_questions)

    # Results from Project Steward use the names below.  Older callers may
    # use *_count aliases, so accept both while retaining one canonical DTO.
    memory_count = _bounded_count(
        source.get("memory_upsert_count", source.get("memory_upserts", 0))
    )
    forgotten_count = _bounded_count(
        source.get("forgotten_count", source.get("forgotten", 0))
    )
    explicit_question_count = _bounded_count(
        source.get("question_count", source.get("questions_count", len(questions)))
    )
    question_count = len(questions) if isinstance(raw_questions, (list, tuple, Mapping)) else explicit_question_count
    action_count, action_failure_count = _action_counts(source.get("action_results"))
    if "generic_action_count" in source:
        action_count = _bounded_count(source.get("generic_action_count"))
    if "generic_action_failure_count" in source:
        action_failure_count = min(
            action_count,
            _bounded_count(source.get("generic_action_failure_count")),
        )

    continuation_pending = bool(source.get("continuation_pending", False))
    summary = _closed_result_summary(
        source=source,
        status_value=status_value,
        normalized_status=normalized_status,
        memory_count=memory_count,
        forgotten_count=forgotten_count,
        question_count=max(question_count, len(questions)),
        continuation_pending=continuation_pending,
    )

    safe_error = None
    if normalized_status in {"failed", "stale"}:
        safe_error = normalize_safe_error_code(
            error_code if error_code is not None else source.get("safe_error_code", source.get("error_code")),
            default="stale_running_recovered" if normalized_status == "stale" else "execution_failed",
        )

    return {
        "status": normalized_status,
        "success": normalized_status == "succeeded",
        "memory_upsert_count": memory_count,
        "forgotten_count": forgotten_count,
        "question_count": max(question_count, len(questions)),
        "continuation_pending": continuation_pending,
        "forced": bool(source.get("forced", False)),
        "generic_action_count": action_count,
        "generic_action_failure_count": action_failure_count,
        "questions": questions,
        "result_summary": summary or None,
        "safe_error_code": safe_error,
    }


# Descriptive aliases used by integrations/tests.
project_heartbeat_result = project_result
safe_result_projection = project_result
project_result_projection = project_result


def _as_datetime(value: Any, *, default: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            parsed = default or datetime.utcnow()
    else:
        parsed = default or datetime.utcnow()
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def encode_history_cursor(started_at: datetime, row_id: uuid.UUID | str) -> str:
    """Encode the stable ``(started_at DESC, id DESC)`` keyset marker."""

    try:
        normalized_id = str(uuid.UUID(str(row_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid history cursor id") from exc
    payload = {
        "v": 1,
        "started_at": _as_datetime(started_at).isoformat(),
        "id": normalized_id,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if len(token) > MAX_CURSOR_BYTES:
        raise ValueError("history cursor too large")
    return token


def decode_history_cursor(value: str | None) -> tuple[datetime, uuid.UUID] | None:
    """Decode/validate a cursor; invalid/tampered values fail closed."""

    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > MAX_CURSOR_BYTES:
        raise ValueError("invalid history cursor")
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError("invalid history cursor") from exc
    if not isinstance(payload, Mapping) or payload.get("v") != 1:
        raise ValueError("invalid history cursor")
    try:
        raw_started_at = payload["started_at"]
        if not isinstance(raw_started_at, str):
            raise ValueError("invalid history cursor timestamp")
        started_at = datetime.fromisoformat(raw_started_at.replace("Z", "+00:00"))
        if started_at.tzinfo is not None:
            started_at = started_at.astimezone().replace(tzinfo=None)
        row_id = uuid.UUID(str(payload["id"]))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid history cursor") from exc
    return started_at, row_id


# Short aliases are useful for route code and preserve discoverability.
encode_cursor = encode_history_cursor
decode_cursor = decode_history_cursor


def _normalize_scope(
    scope_type: Any,
    scope_id: Any,
    project_id: Any,
) -> tuple[str, str, uuid.UUID | None]:
    normalized_type = str(scope_type or "global").strip().casefold()
    if normalized_type == "global":
        return "global", "global", None
    if normalized_type != "project":
        raise ValueError("unsupported Heartbeat scope_type")
    try:
        project_uuid = project_id if isinstance(project_id, uuid.UUID) else uuid.UUID(str(project_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("project Heartbeat scope requires project_id") from exc
    normalized_id = str(project_uuid)
    supplied_id = str(scope_id or "").strip()
    if supplied_id and supplied_id != normalized_id:
        raise ValueError("Heartbeat project scope_id/project_id mismatch")
    return "project", normalized_id, project_uuid


@dataclass(frozen=True)
class HeartbeatRunHistoryDTO:
    """Safe wire projection of :class:`HeartbeatRunHistory`."""

    id: str
    heartbeat_name: str
    mode: str
    scope_type: str
    scope_id: str
    project_id: str | None
    started_at: str | None
    completed_at: str | None
    status: str
    success: bool | None
    memory_upsert_count: int
    forgotten_count: int
    question_count: int
    continuation_pending: bool
    forced: bool = False
    generic_action_count: int = 0
    generic_action_failure_count: int = 0
    questions: list[dict[str, str]] = field(default_factory=list)
    result_summary: str | None = None
    safe_error_code: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_model(cls, row: HeartbeatRunHistory) -> "HeartbeatRunHistoryDTO":
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        questions = _safe_questions(row.questions_json)
        return cls(
            id=str(row.id),
            heartbeat_name=str(row.heartbeat_name),
            mode=str(row.mode),
            scope_type=str(row.scope_type),
            scope_id=str(row.scope_id),
            project_id=str(row.project_id) if row.project_id else None,
            started_at=iso(row.started_at),
            completed_at=iso(row.completed_at),
            status=normalize_status(row.status, default="failed"),
            success=row.success,
            memory_upsert_count=_bounded_count(row.memory_upsert_count),
            forgotten_count=_bounded_count(row.forgotten_count),
            question_count=max(_bounded_count(row.question_count), len(questions)),
            continuation_pending=bool(row.continuation_pending),
            forced=bool(getattr(row, "forced", False)),
            generic_action_count=_bounded_count(
                getattr(row, "generic_action_count", 0)
            ),
            generic_action_failure_count=_bounded_count(
                getattr(row, "generic_action_failure_count", 0)
            ),
            questions=questions,
            result_summary=_safe_summary(row.result_summary) or None,
            safe_error_code=(
                normalize_safe_error_code(row.safe_error_code)
                if row.safe_error_code
                else None
            ),
            created_at=iso(row.created_at),
            updated_at=iso(row.updated_at),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "heartbeat_name": self.heartbeat_name,
            "mode": self.mode,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "project_id": self.project_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "success": self.success,
            "memory_upsert_count": self.memory_upsert_count,
            "memory_upserts": self.memory_upsert_count,
            "forgotten_count": self.forgotten_count,
            "forgotten": self.forgotten_count,
            "question_count": self.question_count,
            "questions_count": self.question_count,
            "continuation_pending": self.continuation_pending,
            "forced": self.forced,
            "generic_action_count": self.generic_action_count,
            "generic_action_failure_count": self.generic_action_failure_count,
            "questions": copy.deepcopy(self.questions),
            "result_summary": self.result_summary,
            "safe_error_code": self.safe_error_code,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        return payload

    # FastAPI/Pydantic callers often use ``model_dump``/``dict`` names.  A
    # tiny explicit alias avoids coupling the storage layer to Pydantic.
    model_dump = to_dict
    dict = to_dict


@dataclass(frozen=True)
class HeartbeatRunHistoryPage:
    items: list[HeartbeatRunHistoryDTO]
    next_cursor: str | None = None
    has_more: bool = False

    @property
    def runs(self) -> list[HeartbeatRunHistoryDTO]:
        return self.items

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "runs": [item.to_dict() for item in self.items],
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
        }


HistoryPage = HeartbeatRunHistoryPage
HeartbeatRunDTO = HeartbeatRunHistoryDTO


class HeartbeatRunHistoryRepository:
    """Async repository for the bounded Heartbeat operational log."""

    def __init__(
        self,
        session_factory: Any | None = None,
        *,
        clock: Any | None = None,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_rows: int = MAX_HISTORY_ROWS,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or datetime.utcnow
        self.retention_days = max(1, int(retention_days))
        self.max_rows = max(1, min(MAX_HISTORY_ROWS, int(max_rows)))

    def _now(self) -> datetime:
        return _as_datetime(self._clock())

    async def _get_session(self) -> Any:
        if self._session_factory is not None:
            session = self._session_factory()
            if inspect.isawaitable(session):
                session = await session
            return session
        from ..memory.database import get_database_manager

        return await get_database_manager().get_session()

    @staticmethod
    async def _close_session(session: Any) -> None:
        close = getattr(session, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    @staticmethod
    async def _rollback(session: Any) -> None:
        rollback = getattr(session, "rollback", None)
        if callable(rollback):
            try:
                result = rollback()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug("Heartbeat history rollback failed", exc_info=True)

    async def start_run(
        self,
        *,
        heartbeat_name: str,
        mode: str,
        scope_type: str = "global",
        scope_id: str | None = None,
        project_id: Any | None = None,
        owner_user_id: Any | None = None,  # accepted for caller compatibility; not stored
        started_at: datetime | str | None = None,
        run_id: uuid.UUID | str | None = None,
        forced: bool = False,
    ) -> HeartbeatRunHistory:
        del owner_user_id
        name = _clip(heartbeat_name, 120)
        if not name:
            raise ValueError("heartbeat_name is required")
        normalized_mode = str(mode or "").strip().casefold()
        if normalized_mode not in {"agent_check", "project_steward"}:
            raise ValueError("unsupported Heartbeat mode")
        normalized_scope_type, normalized_scope_id, normalized_project_id = _normalize_scope(
            scope_type,
            scope_id,
            project_id,
        )
        now = _as_datetime(started_at, default=self._now())
        if run_id is None:
            row_uuid = uuid.uuid4()
        else:
            try:
                row_uuid = run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValueError("invalid Heartbeat history run_id") from exc

        row = HeartbeatRunHistory(
            id=row_uuid,
            heartbeat_name=name,
            mode=normalized_mode,
            scope_type=normalized_scope_type,
            scope_id=normalized_scope_id,
            project_id=normalized_project_id,
            started_at=now,
            completed_at=None,
            status="running",
            success=None,
            memory_upsert_count=0,
            forgotten_count=0,
            question_count=0,
            continuation_pending=False,
            forced=bool(forced),
            generic_action_count=0,
            generic_action_failure_count=0,
            questions_json=[],
            result_summary=None,
            safe_error_code=None,
            created_at=now,
            updated_at=now,
        )
        session = await self._get_session()
        try:
            session.add(row)
            commit = getattr(session, "commit", None)
            if not callable(commit):
                raise RuntimeError("Heartbeat history session is not writable")
            result = commit()
            if inspect.isawaitable(result):
                await result
        except Exception:
            await self._rollback(session)
            raise
        finally:
            await self._close_session(session)
        await self._best_effort_retention()
        return row

    # Names used by older runner adapters.
    record_start = start_run

    async def complete_run(
        self,
        run_id: uuid.UUID | str,
        result: Mapping[str, Any] | None = None,
        *,
        status: Any = None,
        completed_at: datetime | str | None = None,
        error_code: Any = None,
        safe_error_code: Any = None,
        memory_upserts: Any = None,
        memory_upsert_count: Any = None,
        forgotten: Any = None,
        forgotten_count: Any = None,
        questions: Any = None,
        questions_json: Any = None,
        question_count: Any = None,
        continuation_pending: Any = None,
        forced: Any = None,
        generic_action_count: Any = None,
        generic_action_failure_count: Any = None,
        result_summary: Any = None,
        cursor_json: Any = None,
        **_ignored: Any,
    ) -> HeartbeatRunHistory:
        try:
            normalized_id = run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("invalid Heartbeat history run_id") from exc

        source: dict[str, Any] = dict(result) if isinstance(result, Mapping) else {}
        forced_supplied = forced is not None or "forced" in source
        del cursor_json  # scheduler cursor intentionally never enters history
        if memory_upserts is None and memory_upsert_count is not None:
            memory_upserts = memory_upsert_count
        if forgotten is None and forgotten_count is not None:
            forgotten = forgotten_count
        if questions is None and questions_json is not None:
            questions = questions_json
        if memory_upserts is not None:
            source["memory_upsert_count"] = memory_upserts
        if forgotten is not None:
            source["forgotten_count"] = forgotten
        if questions is not None:
            source["questions"] = questions
        if question_count is not None and questions is None:
            source["question_count"] = question_count
        if continuation_pending is not None:
            source["continuation_pending"] = continuation_pending
        if forced is not None:
            source["forced"] = forced
        if generic_action_count is not None:
            source["generic_action_count"] = generic_action_count
        if generic_action_failure_count is not None:
            source["generic_action_failure_count"] = generic_action_failure_count
        # ``result_summary`` is accepted for compatibility with transitional
        # callers, but provider-controlled prose is never an input to the
        # closed summary projector.
        del result_summary
        if error_code is None:
            error_code = safe_error_code
        projected = project_result(source, status=status, error_code=error_code)
        finished_at = _as_datetime(completed_at, default=self._now())

        session = await self._get_session()
        try:
            row = await session.scalar(
                select(HeartbeatRunHistory)
                .where(HeartbeatRunHistory.id == normalized_id)
                .with_for_update()
            )
            if row is None:
                await self._rollback(session)
                raise LookupError("Heartbeat history run not found")
            # Completion is idempotent: a retry after a successful commit must
            # not rewrite a terminal row or create a second operational entry.
            if row.status != "running":
                await self._rollback(session)
                return row

            row.status = projected["status"]
            row.success = projected["success"]
            row.completed_at = finished_at
            row.updated_at = finished_at
            row.memory_upsert_count = projected["memory_upsert_count"]
            row.forgotten_count = projected["forgotten_count"]
            row.question_count = projected["question_count"]
            row.continuation_pending = projected["continuation_pending"]
            if forced_supplied:
                row.forced = bool(projected.get("forced", row.forced))
            row.generic_action_count = projected.get("generic_action_count", 0)
            row.generic_action_failure_count = projected.get(
                "generic_action_failure_count", 0
            )
            row.questions_json = projected["questions"]
            row.result_summary = projected["result_summary"]
            row.safe_error_code = projected["safe_error_code"]
            await session.commit()
        except LookupError:
            raise
        except Exception:
            await self._rollback(session)
            raise
        finally:
            await self._close_session(session)
        await self._best_effort_retention()
        return row

    record_complete = complete_run

    async def record_run(
        self,
        *,
        heartbeat_name: str,
        mode: str,
        scope_type: str = "global",
        scope_id: str | None = None,
        project_id: Any | None = None,
        owner_user_id: Any | None = None,
        started_at: datetime | str | None = None,
        completed_at: datetime | str | None = None,
        result: Mapping[str, Any] | None = None,
        status: Any = None,
        error_code: Any = None,
        **result_fields: Any,
    ) -> HeartbeatRunHistory:
        # Transitional runner adapters may include these identifiers while
        # they are deciding whether a start row already exists.  ``record_run``
        # creates its own row, so never forward them as result fields.
        result_fields.pop("history_id", None)
        result_fields.pop("run_id", None)
        forced = bool(result_fields.get("forced", False))
        if error_code is None:
            error_code = result_fields.pop("safe_error_code", None)
        else:
            result_fields.pop("safe_error_code", None)
        row = await self.start_run(
            heartbeat_name=heartbeat_name,
            mode=mode,
            scope_type=scope_type,
            scope_id=scope_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            started_at=started_at,
            forced=forced,
        )
        return await self.complete_run(
            row.id,
            result,
            status=status,
            completed_at=completed_at,
            error_code=error_code,
            **result_fields,
        )

    create = record_run

    async def get_run(
        self,
        run_id: uuid.UUID | str,
        *,
        project_id: Any | None = None,
        owner_user_id: Any | None = None,
    ) -> HeartbeatRunHistoryDTO | None:
        try:
            normalized_id = run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))
        except (TypeError, ValueError, AttributeError):
            return None
        session = await self._get_session()
        try:
            query = select(HeartbeatRunHistory).where(HeartbeatRunHistory.id == normalized_id)
            if project_id is not None:
                try:
                    query = query.where(
                        HeartbeatRunHistory.project_id
                        == (project_id if isinstance(project_id, uuid.UUID) else uuid.UUID(str(project_id)))
                    )
                except (TypeError, ValueError, AttributeError):
                    return None
            # ``owner_user_id`` is intentionally resolved through the Project
            # ACL rather than a duplicated owner column in the history table.
            if owner_user_id is not None:
                try:
                    owner_uuid = owner_user_id if isinstance(owner_user_id, uuid.UUID) else uuid.UUID(str(owner_user_id))
                except (TypeError, ValueError, AttributeError):
                    return None
                query = query.outerjoin(Project, HeartbeatRunHistory.project_id == Project.id).where(
                    or_(
                        HeartbeatRunHistory.scope_type == "global",
                        Project.owner_id == owner_uuid,
                    )
                )
            row = await session.scalar(query)
            return HeartbeatRunHistoryDTO.from_model(row) if row is not None else None
        finally:
            await self._close_session(session)

    async def list_runs(
        self,
        *,
        heartbeat_name: str | None = None,
        mode: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        project_id: Any | None = None,
        project_ids: Iterable[Any] | None = None,
        owner_user_id: Any | None = None,
        statuses: Sequence[str] | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> HeartbeatRunHistoryPage:
        page_size = max(1, min(MAX_PAGE_SIZE, int(limit or 50)))
        session = await self._get_session()
        try:
            query = select(HeartbeatRunHistory)
            if heartbeat_name:
                query = query.where(HeartbeatRunHistory.heartbeat_name == _clip(heartbeat_name, 120))
            if mode:
                query = query.where(HeartbeatRunHistory.mode == str(mode).strip().casefold())
            if scope_type:
                query = query.where(HeartbeatRunHistory.scope_type == str(scope_type).strip().casefold())
            if scope_id:
                query = query.where(HeartbeatRunHistory.scope_id == str(scope_id).strip())
            if project_id is not None:
                try:
                    normalized_project_id = project_id if isinstance(project_id, uuid.UUID) else uuid.UUID(str(project_id))
                except (TypeError, ValueError, AttributeError):
                    return HeartbeatRunHistoryPage([])
                query = query.where(HeartbeatRunHistory.project_id == normalized_project_id)
            if project_ids is not None:
                normalized_ids = []
                for value in project_ids:
                    try:
                        normalized_ids.append(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))
                    except (TypeError, ValueError, AttributeError):
                        continue
                if not normalized_ids:
                    return HeartbeatRunHistoryPage([])
                query = query.where(HeartbeatRunHistory.project_id.in_(normalized_ids))
            if owner_user_id is not None:
                try:
                    owner_uuid = owner_user_id if isinstance(owner_user_id, uuid.UUID) else uuid.UUID(str(owner_user_id))
                except (TypeError, ValueError, AttributeError):
                    return HeartbeatRunHistoryPage([])
                query = query.outerjoin(Project, HeartbeatRunHistory.project_id == Project.id).where(
                    or_(
                        HeartbeatRunHistory.scope_type == "global",
                        Project.owner_id == owner_uuid,
                    )
                )
            if statuses:
                normalized_statuses = [normalize_status(status, default="failed") for status in statuses]
                query = query.where(HeartbeatRunHistory.status.in_(normalized_statuses))

            marker = decode_history_cursor(cursor)
            if marker is not None:
                marker_started, marker_id = marker
                query = query.where(
                    or_(
                        HeartbeatRunHistory.started_at < marker_started,
                        (
                            HeartbeatRunHistory.started_at == marker_started
                        )
                        & (HeartbeatRunHistory.id < marker_id),
                    )
                )

            query = query.order_by(
                HeartbeatRunHistory.started_at.desc(),
                HeartbeatRunHistory.id.desc(),
            ).limit(page_size + 1)
            rows = list((await session.execute(query)).scalars().all())
        finally:
            await self._close_session(session)

        has_more = len(rows) > page_size
        rows = rows[:page_size]
        items = [HeartbeatRunHistoryDTO.from_model(row) for row in rows]
        next_cursor = (
            encode_history_cursor(rows[-1].started_at, rows[-1].id)
            if has_more and rows
            else None
        )
        return HeartbeatRunHistoryPage(items, next_cursor, has_more)

    list = list_runs

    async def reconcile_stale(
        self,
        *,
        cutoff: datetime | str | None = None,
        stale_after: timedelta = timedelta(minutes=15),
    ) -> int:
        now = self._now()
        effective_cutoff = _as_datetime(cutoff, default=now - stale_after)
        session = await self._get_session()
        changed = 0
        try:
            rows = list(
                (
                    await session.execute(
                        select(HeartbeatRunHistory)
                        .where(
                            HeartbeatRunHistory.status == "running",
                            HeartbeatRunHistory.started_at < effective_cutoff,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.status = "stale"
                row.success = False
                row.completed_at = now
                row.updated_at = now
                row.safe_error_code = "stale_running_recovered"
                changed += 1
            if changed:
                await session.commit()
            else:
                await self._rollback(session)
        except Exception:
            await self._rollback(session)
            raise
        finally:
            await self._close_session(session)
        return changed

    mark_stale = reconcile_stale
    reconcile_stale_runs = reconcile_stale

    async def purge(
        self,
        *,
        now: datetime | str | None = None,
        retention_days: int | None = None,
        max_rows: int | None = None,
        heartbeat_name: str | None = None,
    ) -> int:
        """Delete aged rows and enforce a bounded cap for each scope."""

        effective_now = _as_datetime(now, default=self._now())
        days = max(1, int(retention_days if retention_days is not None else self.retention_days))
        cap = max(
            1,
            min(
                MAX_HISTORY_ROWS,
                int(max_rows if max_rows is not None else self.max_rows),
            ),
        )
        deleted_count = 0
        session = await self._get_session()
        try:
            age_predicates = [
                HeartbeatRunHistory.created_at
                < effective_now - timedelta(days=days)
            ]
            if heartbeat_name:
                age_predicates.append(
                    HeartbeatRunHistory.heartbeat_name == _clip(heartbeat_name, 120)
                )
            result = await session.execute(
                delete(HeartbeatRunHistory).where(*age_predicates)
            )
            deleted_count += max(0, int(getattr(result, "rowcount", 0) or 0))
            # Select rows once and group by the complete scope identity.  The
            # cap is intentionally per heartbeat/mode/project stream so a
            # busy global heartbeat cannot evict every Project Steward row.
            rows_query = select(HeartbeatRunHistory).order_by(
                HeartbeatRunHistory.heartbeat_name.asc(),
                HeartbeatRunHistory.mode.asc(),
                HeartbeatRunHistory.scope_type.asc(),
                HeartbeatRunHistory.scope_id.asc(),
                HeartbeatRunHistory.started_at.desc(),
                HeartbeatRunHistory.id.desc(),
            )
            if heartbeat_name:
                rows_query = rows_query.where(
                    HeartbeatRunHistory.heartbeat_name == _clip(heartbeat_name, 120)
                )
            rows = list((await session.execute(rows_query)).scalars().all())
            seen_by_scope: dict[tuple[Any, ...], int] = {}
            old_ids: list[uuid.UUID] = []
            for row in rows:
                key = (
                    row.heartbeat_name,
                    row.mode,
                    row.scope_type,
                    row.scope_id,
                    str(row.project_id) if row.project_id is not None else None,
                )
                seen_by_scope[key] = seen_by_scope.get(key, 0) + 1
                if seen_by_scope[key] > cap:
                    old_ids.append(row.id)
            if old_ids:
                result = await session.execute(
                    delete(HeartbeatRunHistory).where(HeartbeatRunHistory.id.in_(old_ids))
                )
                deleted_count += max(0, int(getattr(result, "rowcount", 0) or 0))
            await session.commit()
        except Exception:
            await self._rollback(session)
            raise
        finally:
            await self._close_session(session)
        return deleted_count

    enforce_retention = purge

    async def _best_effort_retention(self) -> None:
        try:
            await self.purge()
        except Exception:
            # A retention failure must not turn an otherwise committed
            # Heartbeat result into a retry/duplicate mutation.
            logger.warning("Heartbeat history retention cleanup failed", exc_info=True)


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "HeartbeatRunDTO",
    "HeartbeatRunHistoryDTO",
    "HeartbeatRunHistoryPage",
    "HeartbeatRunHistoryRepository",
    "HistoryPage",
    "MAX_HISTORY_ROWS",
    "MAX_PAGE_SIZE",
    "decode_cursor",
    "decode_history_cursor",
    "encode_cursor",
    "encode_history_cursor",
    "normalize_safe_error_code",
    "normalize_status",
    "project_heartbeat_result",
    "project_result",
    "project_result_projection",
    "safe_result_projection",
]
