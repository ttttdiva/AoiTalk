"""Persistence/orchestration boundary for Dreaming Memory consolidation.

The service owns source selection, durable cursor state, run bookkeeping and
retry-safe mutations.  It deliberately does not write ``ContextMemory``
directly: all memory writes are delegated to ``ScopedMemoryService`` so the
existing scope and audit checks remain authoritative.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import uuid
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from ..memory.dreaming_consolidator import (
    DreamingMemoryConsolidator,
    DreamingSourceTurn,
)

logger = logging.getLogger(__name__)

DEFAULT_DREAMING_BATCH_SIZE = 100
MAX_DREAMING_BATCH_SIZE = 1000
DEFAULT_DREAMING_IDLE_SECONDS = 5 * 60
DEFAULT_DREAMING_RETRY_BASE_SECONDS = 30.0
MAX_DREAMING_RETRY_SECONDS = 6 * 60 * 60


def _text(value: Any) -> str:
    return str(value or "").strip()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat()
    value = _text(value)
    return value or None


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _id(value: Any) -> str:
    return _text(value)


def _strongest_privacy_mode(*values: Any) -> str:
    """Return the strongest mode present in session/project metadata."""
    rank = {"direct": 0, "protected": 1, "local_only": 2}
    selected = "direct"
    for value in values:
        if isinstance(value, Mapping):
            policy = value.get("external_model_privacy")
            value = value.get("privacy_mode") or (
                policy.get("mode") if isinstance(policy, Mapping) else None
            )
        mode = _text(value).lower()
        if mode in rank and rank[mode] > rank[selected]:
            selected = mode
    return selected


def _attr(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _json_digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _callable_accepts(function: Callable[..., Any], key: str) -> bool:
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return True
    return key in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class DreamingConsolidationService:
    """Coordinate one user's backfill/incremental consolidation runs."""

    def __init__(
        self,
        *,
        consolidator: DreamingMemoryConsolidator | Any | None = None,
        memory_service: Any | None = None,
        session_factory: Callable[[], Awaitable[Any]] | None = None,
        source_turn_loader: Callable[..., Any] | None = None,
        llm_client: Any | None = None,
        llm_client_factory: Callable[..., Any] | None = None,
        now_fn: Callable[[], datetime] | None = None,
        batch_size: int = DEFAULT_DREAMING_BATCH_SIZE,
        idle_seconds: float = DEFAULT_DREAMING_IDLE_SECONDS,
        retry_base_seconds: float = DEFAULT_DREAMING_RETRY_BASE_SECONDS,
        max_retry_seconds: float = MAX_DREAMING_RETRY_SECONDS,
        privacy_transform: Callable[..., Any] | None = None,
        config: Any | None = None,
    ) -> None:
        self.consolidator = consolidator or DreamingMemoryConsolidator()
        self.memory_service = memory_service
        self._session_factory = session_factory
        self.source_turn_loader = source_turn_loader
        self.llm_client = llm_client
        self.llm_client_factory = llm_client_factory
        self.now_fn = now_fn or datetime.utcnow
        self.batch_size = max(1, min(int(batch_size), MAX_DREAMING_BATCH_SIZE))
        self.idle_seconds = max(0.0, float(idle_seconds))
        self.retry_base_seconds = max(0.0, float(retry_base_seconds))
        self.max_retry_seconds = max(self.retry_base_seconds, float(max_retry_seconds))
        # Optional deployment-specific redaction/local-only gate.  It runs
        # before an LLM call and before any cursor/state mutation.
        self.privacy_transform = privacy_transform
        self.config = config
        self._last_scan_started_at: datetime | None = None

    async def _new_session(self) -> Any:
        factory = self._session_factory
        if factory is None:
            from ..memory.database import get_db_session

            factory = get_db_session
        return await _maybe_await(factory())

    async def _close_session(self, session: Any) -> None:
        close = getattr(session, "close", None)
        if callable(close):
            with suppress(Exception):
                await _maybe_await(close())

    async def _execute(self, session: Any, statement: Any) -> Any:
        execute = getattr(session, "execute", None)
        if not callable(execute):
            return None
        return await _maybe_await(execute(statement))

    @staticmethod
    def _scalars(result: Any) -> list[Any]:
        if result is None:
            return []
        scalars = getattr(result, "scalars", None)
        if callable(scalars):
            try:
                value = scalars()
                all_method = getattr(value, "all", None)
                return list(all_method() if callable(all_method) else value)
            except Exception:
                return []
        all_method = getattr(result, "all", None)
        if callable(all_method):
            try:
                rows = list(all_method())
            except Exception:
                rows = []
        elif isinstance(result, Iterable) and not isinstance(result, (str, bytes, Mapping)):
            rows = list(result)
        else:
            rows = []
        normalized: list[Any] = []
        for row in rows:
            if isinstance(row, (tuple, list)) and len(row) == 1:
                normalized.append(row[0])
            else:
                normalized.append(row)
        return normalized

    async def _load_state(self, user_id: str, *, create: bool = False, session: Any | None = None) -> Any | None:
        owns_session = session is None
        if owns_session:
            session = await self._new_session()
        try:
            from ..memory.models import DreamingMemoryState
            from sqlalchemy import select

            result = await self._execute(
                session,
                select(DreamingMemoryState).where(DreamingMemoryState.user_id == str(user_id)),
            )
            state = None
            scalar_one_or_none = getattr(result, "scalar_one_or_none", None)
            if callable(scalar_one_or_none):
                with suppress(Exception):
                    state = scalar_one_or_none()
            if state is None:
                rows = self._scalars(result)
                state = rows[0] if rows else None
            if state is None and create:
                state = DreamingMemoryState(user_id=str(user_id))
                add = getattr(session, "add", None)
                if callable(add):
                    add(state)
                await _maybe_await(getattr(session, "flush", lambda: None)())
            return state
        finally:
            if owns_session:
                await self._close_session(session)

    async def _resolve_memory_service(self) -> Any:
        if self.memory_service is None:
            from .scoped_memory_service import ScopedMemoryService

            self.memory_service = ScopedMemoryService()
        return self.memory_service

    async def _resolve_llm_client(self, user_id: str) -> Any:
        factory = self.llm_client_factory
        if factory is None:
            return self.llm_client
        try:
            if _callable_accepts(factory, "user_id"):
                return await _maybe_await(factory(user_id=str(user_id)))
            return await _maybe_await(factory(str(user_id)))
        except TypeError:
            return await _maybe_await(factory())

    async def load_idle_users(self, *, limit: int = 20, now: datetime | None = None) -> list[str]:
        """Return users with idle active conversations and no retry backoff."""

        now = (now or self.now_fn()).replace(tzinfo=None)
        limit = max(1, min(int(limit), 1000))
        try:
            from sqlalchemy import or_, select
            from ..memory.models import ConversationSession, DreamingMemoryState

            session = await self._new_session()
            try:
                result = await self._execute(
                    session,
                    select(ConversationSession.user_id, ConversationSession.last_activity)
                    .where(
                        ConversationSession.deleted_at.is_(None),
                    )
                    .order_by(ConversationSession.last_activity.asc())
                    .limit(max(limit * 4, limit)),
                )
                rows = []
                all_method = getattr(result, "all", None)
                if callable(all_method):
                    with suppress(Exception):
                        rows = list(all_method())
                if not rows:
                    rows = self._scalars(result)
                candidates: list[tuple[str, datetime | None]] = []
                for row in rows:
                    if isinstance(row, Mapping):
                        owner = _id(row.get("user_id"))
                        activity = _datetime(row.get("last_activity"))
                    else:
                        owner = _id(row[0] if isinstance(row, (tuple, list)) else _attr(row, "user_id"))
                        activity = _datetime(
                            row[1] if isinstance(row, (tuple, list)) and len(row) > 1 else _attr(row, "last_activity")
                        )
                    if owner:
                        candidates.append((owner, activity))

                # Some lightweight fakes return no rows for a complex select;
                # fall back to the state table so tests and rolling upgrades
                # can still run housekeeping safely.
                if not candidates:
                    state_result = await self._execute(
                        session,
                        select(DreamingMemoryState)
                        .order_by(DreamingMemoryState.updated_at.asc())
                        .limit(limit * 4),
                    )
                    candidates = [
                        (_id(_attr(item, "user_id")), None)
                        for item in self._scalars(state_result)
                        if _id(_attr(item, "user_id"))
                    ]

                latest_by_owner: dict[str, datetime | None] = {}
                for owner, activity in candidates:
                    previous_activity = latest_by_owner.get(owner)
                    if previous_activity is None or (
                        activity is not None and activity > previous_activity
                    ):
                        latest_by_owner[owner] = activity
                selected: list[str] = []
                for owner, activity in sorted(
                    latest_by_owner.items(),
                    key=lambda item: item[1] or datetime.min,
                ):
                    if activity is not None and self.idle_seconds > 0:
                        if activity > now - timedelta(seconds=self.idle_seconds):
                            continue
                    state_result = await self._execute(
                        session,
                        select(DreamingMemoryState).where(DreamingMemoryState.user_id == owner),
                    )
                    state_rows = self._scalars(state_result)
                    state = state_rows[0] if state_rows else None
                    retry_at = _datetime(_attr(state, "next_retry_at"))
                    if retry_at is not None and retry_at > now:
                        continue
                    selected.append(owner)
                    if len(selected) >= limit:
                        break
                return selected
            finally:
                await self._close_session(session)
        except Exception as exc:
            logger.warning("Dreaming idle-user load failed: %s", exc)
            return []

    async def load_dreaming_source_batch(
        self,
        user_id: str,
        *,
        limit: int | None = None,
        backfill: bool | None = None,
    ) -> list[DreamingSourceTurn]:
        """Load active, non-deleted user messages with paired assistants."""

        if self.source_turn_loader is not None:
            self._last_scan_started_at = self.now_fn().replace(tzinfo=None)
            loader = self.source_turn_loader
            values = {"user_id": str(user_id), "limit": limit or self.batch_size, "backfill": backfill}
            try:
                result = loader(**values) if _callable_accepts(loader, "user_id") else loader(str(user_id))
            except TypeError:
                result = loader(str(user_id))
            result = await _maybe_await(result)
            turns: list[DreamingSourceTurn] = []
            for item in result or []:
                if isinstance(item, DreamingSourceTurn):
                    turns.append(item)
                elif isinstance(item, Mapping):
                    try:
                        turns.append(DreamingSourceTurn(**dict(item)))
                    except TypeError:
                        continue
            return turns[: max(1, min(int(limit or self.batch_size), MAX_DREAMING_BATCH_SIZE))]

        requested = max(1, min(int(limit or self.batch_size), MAX_DREAMING_BATCH_SIZE))
        session = await self._new_session()
        try:
            from sqlalchemy import or_, select
            from ..memory.models import ConversationMessage, ConversationSession, Project

            state = await self._load_state(str(user_id), create=False, session=session)
            use_backfill = bool(backfill) if backfill is not None else not bool(_attr(state, "backfill_complete", False))
            # Capture a scan boundary before selecting rows.  Writes arriving
            # during the scan are intentionally left for the next run.
            scan_start = self.now_fn().replace(tzinfo=None)
            self._last_scan_started_at = scan_start
            # First select sessions, then messages.  This avoids relying on a
            # relationship being eagerly loaded and makes filtering explicit.
            session_result = await self._execute(
                session,
                select(ConversationSession).where(
                    ConversationSession.user_id == str(user_id),
                    ConversationSession.deleted_at.is_(None),
                    # The current background consolidator writes only
                    # user-scope memories.  Project sessions require a
                    # separate live ACL/read-policy check and must not be
                    # sent to an LLM or downgraded into personal memory.
                    ConversationSession.project_id.is_(None),
                ),
            )
            sessions = self._scalars(session_result)
            project_by_id: dict[str, Any] = {}
            project_ids = [
                _attr(conversation, "project_id")
                for conversation in sessions
                if _attr(conversation, "project_id") is not None
            ]
            if project_ids:
                try:
                    project_result = await self._execute(
                        session,
                        select(Project).where(Project.id.in_(project_ids)),
                    )
                    project_by_id = {
                        _id(_attr(project, "id")): project
                        for project in self._scalars(project_result)
                    }
                except Exception:
                    # Some isolated unit fixtures do not create projects;
                    # session policy still remains authoritative there.
                    project_by_id = {}
            messages: list[tuple[Any, Any]] = []
            for conversation in sessions:
                sid = _id(_attr(conversation, "id"))
                if not sid:
                    continue
                message_result = await self._execute(
                    session,
                    select(ConversationMessage)
                    .where(
                        ConversationMessage.session_id == _attr(conversation, "id"),
                        ConversationMessage.role == "user",
                        ConversationMessage.deleted_at.is_(None),
                        or_(
                            ConversationMessage.is_active_branch.is_(True),
                            ConversationMessage.is_active_branch.is_(None),
                        ),
                    )
                    .order_by(ConversationMessage.created_at.asc(), ConversationMessage.id.asc()),
                )
                for message in self._scalars(message_result):
                    sender_id = _id(_attr(message, "sender_id"))
                    sender_type = _text(_attr(message, "sender_type")).lower()
                    if sender_id and sender_type in {"user", "human", "member"} and sender_id != str(user_id):
                        continue
                    messages.append((conversation, message))

            # Incremental cursor: equal timestamps are intentionally included;
            # the source digest/idempotency key makes this safe and prevents a
            # message inserted with the same timestamp from being lost.
            cursor = _datetime(_attr(state, "last_incremental_at"))
            if use_backfill:
                cutoff = _datetime(_attr(state, "backfill_before_at")) or self.now_fn().replace(tzinfo=None)
                messages = [
                    (conversation, message)
                    for conversation, message in messages
                    if (_datetime(_attr(message, "created_at")) is not None
                    and (_datetime(_attr(message, "created_at")) or datetime.min) <= cutoff)
                ]
                messages.sort(
                    key=lambda pair: (
                        _datetime(_attr(pair[1], "created_at")) or datetime.min,
                        _id(_attr(pair[1], "id")),
                    ),
                    reverse=True,
                )
                cursor_id = _id(_attr(state, "backfill_before_message_id"))
                if cursor_id:
                    try:
                        start = next(index for index, (_, msg) in enumerate(messages) if _id(_attr(msg, "id")) == cursor_id)
                        messages = messages[start + 1 :]
                    except StopIteration:
                        pass
            elif cursor is not None:
                messages = [
                    (conversation, message)
                    for conversation, message in messages
                    if (
                        (
                            _datetime(_attr(message, "created_at")) is not None
                            and (_datetime(_attr(message, "created_at")) or datetime.min) > cursor
                        )
                        or (
                            _datetime(_attr(message, "updated_at")) is not None
                            and (_datetime(_attr(message, "updated_at")) or datetime.min) > cursor
                        )
                    )
                    and (
                        (_datetime(_attr(message, "created_at")) is None or (_datetime(_attr(message, "created_at")) or datetime.min) <= scan_start)
                        and (_datetime(_attr(message, "updated_at")) is None or (_datetime(_attr(message, "updated_at")) or datetime.min) <= scan_start)
                    )
                ]
                messages.sort(
                    key=lambda pair: (
                        _datetime(_attr(pair[1], "updated_at")) or _datetime(_attr(pair[1], "created_at")) or datetime.min,
                        _id(_attr(pair[1], "id")),
                    )
                )
            else:
                messages = [
                    (conversation, message)
                    for conversation, message in messages
                    if (
                        _datetime(_attr(message, "created_at")) is None
                        or (_datetime(_attr(message, "created_at")) or datetime.min) <= scan_start
                    )
                    and (
                        _datetime(_attr(message, "updated_at")) is None
                        or (_datetime(_attr(message, "updated_at")) or datetime.min) <= scan_start
                    )
                ]
                messages.sort(
                    key=lambda pair: (
                        _datetime(_attr(pair[1], "updated_at")) or _datetime(_attr(pair[1], "created_at")) or datetime.min,
                        _id(_attr(pair[1], "id")),
                    )
                )

            selected = messages[:requested]
            turns: list[DreamingSourceTurn] = []
            for conversation, message in selected:
                session_context = _attr(conversation, "context")
                if not isinstance(session_context, Mapping):
                    session_context = {}
                project = project_by_id.get(_id(_attr(conversation, "project_id")))
                project_metadata = _attr(project, "project_metadata")
                if not isinstance(project_metadata, Mapping):
                    project_metadata = {}
                privacy_mode = _strongest_privacy_mode(session_context, project_metadata)
                assistant_result = await self._execute(
                    session,
                    select(ConversationMessage)
                    .where(
                        ConversationMessage.session_id == _attr(conversation, "id"),
                        ConversationMessage.parent_message_id == _attr(message, "id"),
                        ConversationMessage.role == "assistant",
                        ConversationMessage.deleted_at.is_(None),
                        or_(
                            ConversationMessage.is_active_branch.is_(True),
                            ConversationMessage.is_active_branch.is_(None),
                        ),
                    )
                    .order_by(ConversationMessage.created_at.asc(), ConversationMessage.id.asc())
                    .limit(1),
                )
                assistants = self._scalars(assistant_result)
                assistant = assistants[0] if assistants else None
                turns.append(
                    DreamingSourceTurn(
                        session_id=_id(_attr(conversation, "id")),
                        user_message_id=_id(_attr(message, "id")),
                        user_text=_text(_attr(message, "content")),
                        user_created_at=_datetime(_attr(message, "created_at")),
                        user_updated_at=_datetime(_attr(message, "updated_at")),
                        deleted_at=_datetime(_attr(message, "deleted_at")),
                        is_active_branch=_attr(message, "is_active_branch", True),
                        project_id=_id(_attr(conversation, "project_id")) or None,
                        assistant_message_id=_id(_attr(assistant, "id")) or None,
                        assistant_text=_text(_attr(assistant, "content")),
                        assistant_created_at=_datetime(_attr(assistant, "created_at")),
                        privacy_mode=privacy_mode,
                        privacy_context={
                            **dict(session_context),
                            "project_metadata": dict(project_metadata),
                        },
                    )
                )
            return [turn for turn in turns if turn.user_text]
        finally:
            await self._close_session(session)

    async def _existing_memories(self, user_id: str) -> list[dict[str, Any]]:
        service = await self._resolve_memory_service()
        method = getattr(service, "list_memories", None)
        if not callable(method):
            return []
        kwargs = {"actor_id": str(user_id), "scope_type": "user", "status": "active"}
        try:
            parameters = inspect.signature(method).parameters
            if not any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
                kwargs = {key: value for key, value in kwargs.items() if key in parameters}
        except (TypeError, ValueError):
            pass
        try:
            result = await _maybe_await(method(**kwargs))
            return [dict(item) for item in (result or []) if isinstance(item, Mapping)]
        except Exception as exc:
            logger.warning("Dreaming existing-memory load failed: %s", exc)
            return []

    @staticmethod
    def _source_digest(turns: Sequence[DreamingSourceTurn]) -> str:
        return _json_digest(
            [
                {
                    "session_id": turn.session_id,
                    "user_message_id": turn.user_message_id,
                    "created_at": _iso(turn.user_created_at),
                    "updated_at": _iso(turn.user_updated_at),
                    "deleted_at": _iso(turn.deleted_at),
                    "is_active_branch": turn.is_active_branch is not False,
                    "privacy_mode": str(turn.privacy_mode or "direct").lower(),
                    "user_text_sha256": hashlib.sha256(turn.user_text.encode("utf-8")).hexdigest(),
                }
                for turn in turns
            ]
        )

    async def _record_run(
        self,
        *,
        user_id: str,
        trigger: str,
        status: str,
        source_ids: Sequence[str],
        source_digest: str | None,
        backfill: bool,
        candidate_count: int = 0,
        mutation_count: int = 0,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        error: str | None = None,
        run_id: str | None = None,
    ) -> str | None:
        try:
            from ..memory.models import DreamingMemoryRun

            session = await self._new_session()
            try:
                run = None
                if run_id:
                    getter = getattr(session, "get", None)
                    if callable(getter):
                        with suppress(Exception):
                            run = await _maybe_await(getter(DreamingMemoryRun, uuid.UUID(str(run_id))))
                if run is None:
                    run = DreamingMemoryRun(
                        id=uuid.UUID(str(run_id)) if run_id else uuid.uuid4(),
                        user_id=str(user_id),
                        trigger=str(trigger or "idle")[:32],
                        status=str(status or "completed")[:32],
                        source_message_ids=list(source_ids),
                        source_count=len(source_ids),
                        source_digest=source_digest,
                        backfill=bool(backfill),
                        candidate_count=int(candidate_count),
                        mutation_count=int(mutation_count),
                        started_at=started_at,
                        completed_at=completed_at,
                        error=error,
                        created_at=self.now_fn(),
                        updated_at=self.now_fn(),
                    )
                    add = getattr(session, "add", None)
                    if callable(add):
                        add(run)
                else:
                    run.status = str(status or run.status)[:32]
                    run.source_message_ids = list(source_ids)
                    run.source_count = len(source_ids)
                    run.source_digest = source_digest
                    run.backfill = bool(backfill)
                    run.candidate_count = int(candidate_count)
                    run.mutation_count = int(mutation_count)
                    run.started_at = started_at or getattr(run, "started_at", None)
                    run.completed_at = completed_at
                    run.error = error
                    run.updated_at = self.now_fn()
                await _maybe_await(getattr(session, "commit", lambda: None)())
                return _id(getattr(run, "id", run_id)) or run_id
            finally:
                await self._close_session(session)
        except Exception as exc:  # pragma: no cover - ledger must not block memory
            logger.warning("Dreaming run ledger write failed: %s", exc)
            return run_id

    async def recover_stale_runs(
        self,
        *,
        user_id: str | None = None,
        stale_after_seconds: float = 10 * 60,
    ) -> int:
        """Mark process-crash ``running`` ledgers retryable on startup."""
        session = await self._new_session()
        try:
            from sqlalchemy import select
            from ..memory.models import DreamingMemoryRun

            cutoff = self.now_fn().replace(tzinfo=None) - timedelta(
                seconds=max(1.0, float(stale_after_seconds))
            )
            stmt = select(DreamingMemoryRun).where(
                DreamingMemoryRun.status == "running",
                DreamingMemoryRun.started_at < cutoff,
            )
            if user_id is not None:
                stmt = stmt.where(DreamingMemoryRun.user_id == str(user_id))
            rows = self._scalars(await self._execute(session, stmt))
            now = self.now_fn().replace(tzinfo=None)
            for row in rows:
                row.status = "failed"
                row.error = "stale_running_recovered"
                row.completed_at = now
                row.updated_at = now
            if rows:
                await _maybe_await(getattr(session, "commit", lambda: None)())
            return len(rows)
        finally:
            await self._close_session(session)

    async def _update_state(
        self,
        user_id: str,
        *,
        turns: Sequence[DreamingSourceTurn],
        source_digest: str | None,
        backfill: bool,
        complete_backfill: bool = False,
        error: str | None = None,
        failed: bool = False,
    ) -> None:
        session = await self._new_session()
        try:
            state = await self._load_state(str(user_id), create=True, session=session)
            now = self.now_fn().replace(tzinfo=None)
            if failed:
                failures = int(_attr(state, "consecutive_failures", 0) or 0) + 1
                delay = min(
                    self.max_retry_seconds,
                    self.retry_base_seconds * (2 ** max(0, failures - 1)),
                )
                state.consecutive_failures = failures
                state.last_error = _text(error)[:2000] or "dreaming_consolidation_failed"
                state.next_retry_at = now + timedelta(seconds=delay)
            else:
                state.consecutive_failures = 0
                state.last_error = None
                state.next_retry_at = None
                state.last_dreamed_at = now
                state.last_history_digest = source_digest
                if backfill:
                    if complete_backfill:
                        state.backfill_complete = True
                        state.last_full_reconcile_at = now
                    if turns:
                        oldest = min(
                            turns,
                            key=lambda turn: (
                                turn.user_created_at or turn.user_updated_at or datetime.min,
                                turn.user_message_id,
                            ),
                        )
                        # The backfill cursor is ordered by created_at/id;
                        # edits must not move it forward or backward.
                        state.backfill_before_at = oldest.user_created_at
                        with suppress(Exception):
                            state.backfill_before_message_id = uuid.UUID(str(oldest.user_message_id))
                elif turns:
                    # Capture the scan boundary, not completion time or the
                    # newest row, so messages arriving during processing are
                    # picked up by the next run.
                    state.last_incremental_at = self._last_scan_started_at or now
            state.updated_at = now
            await _maybe_await(getattr(session, "commit", lambda: None)())
        finally:
            await self._close_session(session)

    async def apply_consolidated_operations(
        self,
        user_id: str,
        operations: Sequence[Mapping[str, Any]],
        *,
        source_turns: Sequence[DreamingSourceTurn] | None = None,
        source_digest: str | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Apply validated operations through ScopedMemoryService only."""

        service = await self._resolve_memory_service()
        existing = await self._existing_memories(str(user_id))
        existing_by_key: dict[str, Mapping[str, Any]] = {}
        for memory in existing:
            key = _text(memory.get("dedupe_key"))
            if key:
                existing_by_key[key] = memory

        def op_time(item: Mapping[str, Any]) -> datetime:
            return _datetime(item.get("source_updated_at", item.get("source_at"))) or datetime.min

        ordered = sorted((item for item in operations if isinstance(item, Mapping)), key=op_time)
        changed: list[dict[str, Any]] = []
        for item in ordered:
            if _text(item.get("operation", item.get("action", "upsert"))).lower() not in {"upsert", "update", "replace"}:
                continue
            if _text(item.get("scope", item.get("scope_intent", "user"))).lower() not in {"user", "global", "user_memory"}:
                continue
            corroborated = bool(item.get("corroborated", item.get("verified", False)))
            try:
                active_threshold = (
                    corroborated
                    and float(item.get("confidence", 0.0)) >= 0.90
                    and int(item.get("importance", 0)) >= 7
                )
            except (TypeError, ValueError):
                active_threshold = False
            status_hint = _text(item.get("status")).lower()
            if status_hint == "active" and not active_threshold:
                # Never let a provider flag promote a one-session candidate.
                status_hint = "candidate"
            if status_hint not in {"active", "candidate"}:
                status_hint = "active" if active_threshold else "candidate"
            # A candidate is still persisted for review; only a verified
            # multi-session operation may be written as active.
                continue
            content = _text(item.get("content"))
            if not content:
                continue
            dedupe_key = _text(item.get("dedupe_key")) or _json_digest({"type": item.get("memory_type", "fact"), "content": content.casefold()})[:128]
            if not corroborated:
                # Keep a one-session review candidate beside (rather than
                # superseding) an already-active corroborated memory.
                dedupe_key = f"{dedupe_key}:candidate"[:128]
            previous = existing_by_key.get(dedupe_key)
            requested_memory_id = _text(item.get("memory_id"))
            if previous is None and requested_memory_id:
                previous = next(
                    (memory for memory in existing if _text(memory.get("id")) == requested_memory_id),
                    None,
                )
            if previous is not None and requested_memory_id:
                dedupe_key = _text(previous.get("dedupe_key")) or dedupe_key
            if previous is not None:
                previous_time = self._memory_source_time(previous)
                if previous_time is not None and op_time(item) < previous_time:
                    # Temporal precedence: a late retry must not replace a
                    # newer observation with stale content.
                    continue

            refs = item.get("evidence_refs")
            if not isinstance(refs, list):
                refs = []
            if not refs and source_turns:
                refs = [
                    {
                        "type": "conversation",
                        "session_id": turn.session_id,
                        "message_id": turn.user_message_id,
                        "source_at": _iso(turn.user_updated_at or turn.user_created_at),
                    }
                    for turn in source_turns
                    if turn.user_message_id in set(item.get("source_message_ids") or [])
                ]
            structured = dict(item.get("structured_data") or {}) if isinstance(item.get("structured_data"), Mapping) else {}
            source_time = _datetime(item.get("source_updated_at", item.get("source_at")))
            if source_time is None and refs:
                source_time = max(
                    (_datetime(ref.get("created_at", ref.get("source_at"))) for ref in refs if isinstance(ref, Mapping)),
                    default=None,
                )
            dreaming = dict(structured.get("dreaming") or {})
            dreaming.update(
                {
                    "mode": "history_consolidation",
                    "run_id": run_id,
                    "source_digest": source_digest,
                    "evidence_max_created_at": _iso(source_time),
                    "evidence_count": len(refs),
                }
            )
            structured.update(
                {
                    "dreaming": dreaming,
                    "source_digest": source_digest,
                    "source_message_ids": list(item.get("source_message_ids") or []),
                    "source_session_ids": list(item.get("source_session_ids") or []),
                    "source_updated_at": _iso(source_time),
                }
            )
            if source_time is not None:
                refs = [
                    {
                        **dict(ref),
                        "created_at": ref.get("created_at") or ref.get("source_at") or _iso(source_time),
                    }
                    for ref in refs
                    if isinstance(ref, Mapping)
                ]
            kwargs: dict[str, Any] = {
                "actor_id": str(user_id),
                "content": content,
                "scope_type": "user",
                "scope_id": str(user_id),
                "memory_type": _text(item.get("memory_type")) or "fact",
                "title": _text(item.get("title")) or None,
                "structured_data": structured,
                "source_type": "dreaming_auto" if corroborated else "dreaming_candidate",
                "source_ref": f"dreaming:{source_digest or _json_digest(item)}",
                "confidence": float(item.get("confidence", 0.8)),
                "importance": int(item.get("importance", 6)),
                "trust_level": "verified",
                "evidence_refs": refs,
                "evidence_span": {"text": _text(item.get("quote", item.get("evidence_span")))},
                "dedupe_key": dedupe_key,
                "status": status_hint,
                "evidence_max_created_at": source_time,
                "idempotency_key": _text(item.get("operation_id", item.get("id")))
                or f"dreaming:{str(user_id)}:{dedupe_key}:{source_digest or ''}",
            }
            method = getattr(service, "upsert_memory", None)
            replace_method = getattr(service, "replace_memory_from_dreaming", None)
            if not callable(method) and not callable(replace_method):
                continue
            try:
                target_id = (
                    _text(previous.get("id"))
                    if previous is not None and active_threshold and status_hint == "active"
                    else None
                )
                if callable(replace_method):
                    replace_kwargs = {
                        "actor_id": str(user_id),
                        "content": content,
                        "existing_memory_id": target_id,
                        "memory_type": kwargs["memory_type"],
                        "title": kwargs["title"],
                        "structured_data": structured,
                        "evidence_refs": refs,
                        "evidence_span": kwargs["evidence_span"],
                        "evidence_max_created_at": source_time,
                        "evidence_count": len(refs),
                        "confidence": kwargs["confidence"],
                        "importance": kwargs["importance"],
                        "source_type": kwargs["source_type"],
                        "source_ref": kwargs["source_ref"],
                        "run_id": run_id,
                        "status": status_hint,
                        "idempotency_key": kwargs["idempotency_key"],
                        "dedupe_key": dedupe_key,
                    }
                    try:
                        parameters = inspect.signature(replace_method).parameters
                        if not any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
                            replace_kwargs = {key: value for key, value in replace_kwargs.items() if key in parameters}
                    except (TypeError, ValueError):
                        pass
                    result = await _maybe_await(replace_method(**replace_kwargs))
                else:
                    try:
                        parameters = inspect.signature(method).parameters
                        if not any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
                            kwargs = {key: value for key, value in kwargs.items() if key in parameters}
                    except (TypeError, ValueError):
                        pass
                    result = await _maybe_await(method(**kwargs))
            except Exception:
                # A failed mutation is fatal for this batch.  The caller must
                # not advance its cursor and the worker will retry with backoff.
                raise
            payload = result.get("memory") if isinstance(result, Mapping) else result
            changed.append(dict(payload) if isinstance(payload, Mapping) else {"result": payload})
            existing_by_key[dedupe_key] = payload if isinstance(payload, Mapping) else item
        return changed

    @staticmethod
    def _memory_source_time(memory: Mapping[str, Any]) -> datetime | None:
        metadata = memory.get("structured_data")
        if isinstance(metadata, Mapping):
            value = metadata.get("source_updated_at")
            parsed = _datetime(value)
            if parsed is not None:
                return parsed
            source = metadata.get("source_metadata")
            if isinstance(source, Mapping):
                parsed = _datetime(source.get("source_updated_at", source.get("source_at")))
                if parsed is not None:
                    return parsed
        refs = memory.get("evidence_refs")
        if isinstance(refs, list):
            values = [_datetime(item.get("created_at", item.get("source_at"))) for item in refs if isinstance(item, Mapping)]
            values = [value for value in values if value is not None]
            return max(values) if values else None
        return None

    async def process_dreaming_user(
        self,
        user_id: str,
        *,
        trigger: str = "idle",
        source_turns: Sequence[DreamingSourceTurn] | None = None,
        llm_client: Any | None = None,
        backfill: bool | None = None,
        privacy_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one idempotent user consolidation and update its cursor."""

        user_id = str(user_id)
        started = self.now_fn().replace(tzinfo=None)
        turns = list(source_turns) if source_turns is not None else await self.load_dreaming_source_batch(user_id, backfill=backfill)
        use_backfill = bool(backfill) if backfill is not None else not bool(
            _attr(await self._load_state(user_id, create=False), "backfill_complete", False)
        )
        source_ids = [turn.user_message_id for turn in turns]
        digest = self._source_digest(turns) if turns else None
        if not turns:
            await self._update_state(
                user_id,
                turns=[],
                source_digest=digest,
                backfill=use_backfill,
                complete_backfill=use_backfill,
            )
            await self._record_run(
                user_id=user_id,
                trigger=trigger,
                status="skipped",
                source_ids=[],
                source_digest=None,
                backfill=use_backfill,
                started_at=started,
                completed_at=self.now_fn(),
            )
            return {"user_id": user_id, "status": "skipped", "backfill": use_backfill, "source_count": 0, "mutation_count": 0}

        previous_state = await self._load_state(user_id, create=False)
        if digest and digest == _text(_attr(previous_state, "last_history_digest")):
            return {
                "user_id": user_id,
                "status": "idempotent",
                "backfill": use_backfill,
                "source_count": len(turns),
                "source_message_ids": source_ids,
                "source_digest": digest,
                "mutation_count": 0,
            }

        run_id = await self._record_run(
            user_id=user_id,
            trigger=trigger,
            status="running",
            source_ids=source_ids,
            source_digest=digest,
            backfill=use_backfill,
            started_at=started,
        )
        try:
            existing = await self._existing_memories(user_id)
            client = llm_client if llm_client is not None else await self._resolve_llm_client(user_id)
            if client is None:
                raise RuntimeError("dreaming consolidation LLM is unavailable")
            # Partition by the effective source policy before any outbound
            # call.  A protected/local-only conversation never shares a
            # weaker direct batch; all partitions must succeed before the
            # shared user cursor is advanced below.
            partitions: dict[str, list[DreamingSourceTurn]] = {}
            for turn in turns:
                mode = str(turn.privacy_mode or "direct").strip().lower() or "direct"
                partitions.setdefault(mode, []).append(turn)
            operations: list[Mapping[str, Any]] = []
            for mode, partition_turns in sorted(partitions.items()):
                partition_existing = existing
                if self.privacy_transform is not None:
                    transform = self.privacy_transform
                    payload = {
                        "user_id": user_id,
                        "source_turns": partition_turns,
                        "existing_memories": partition_existing,
                        "privacy_context": {
                            "mode": mode,
                            **dict(privacy_context or {}),
                        },
                    }
                    try:
                        transformed = transform(payload)
                    except TypeError:
                        transformed = transform(payload, privacy_context)
                    transformed = await _maybe_await(transformed)
                    if isinstance(transformed, Mapping):
                        partition_turns = list(transformed.get("source_turns") or partition_turns)
                        partition_existing = list(
                            transformed.get("existing_memories") or partition_existing
                        )
                    elif transformed is not None:
                        partition_turns = list(transformed)
                turn_context = next(
                    (
                        dict(turn.privacy_context)
                        for turn in partition_turns
                        if isinstance(turn.privacy_context, Mapping)
                    ),
                    {},
                )
                partition_operations = await _maybe_await(
                    self.consolidator.consolidate(
                        user_id=user_id,
                        source_turns=partition_turns,
                        existing_memories=partition_existing,
                        llm_client=client,
                        privacy_context={
                            "user_id": user_id,
                            "config": self.config,
                            "mode": mode,
                            **turn_context,
                            **dict(privacy_context or {}),
                        },
                    )
                )
                if bool(getattr(self.consolidator, "last_call_failed", False)):
                    raise RuntimeError("dreaming consolidation provider failed")
                if bool(getattr(self.consolidator, "last_parse_failed", False)):
                    raise RuntimeError("dreaming consolidation output invalid")
                operations.extend(partition_operations or [])
            mutations = await self.apply_consolidated_operations(
                user_id,
                operations or [],
                source_turns=turns,
                source_digest=digest,
                run_id=run_id,
            )
            complete_backfill = use_backfill and len(turns) < self.batch_size
            await self._update_state(
                user_id,
                turns=turns,
                source_digest=digest,
                backfill=use_backfill,
                complete_backfill=complete_backfill,
            )
            await self._record_run(
                user_id=user_id,
                trigger=trigger,
                status="completed",
                source_ids=source_ids,
                source_digest=digest,
                backfill=use_backfill,
                candidate_count=len(operations or []),
                mutation_count=len(mutations),
                started_at=started,
                completed_at=self.now_fn(),
                run_id=run_id,
            )
            return {
                "user_id": user_id,
                "status": "completed",
                "backfill": use_backfill,
                "source_count": len(turns),
                "source_message_ids": source_ids,
                "source_digest": digest,
                "candidate_count": len(operations or []),
                "mutation_count": len(mutations),
                "mutations": mutations,
            }
        except Exception as exc:
            # Provider exceptions can echo request payloads.  Persist/log only
            # a stable class marker; raw history must never enter run state or
            # application logs.
            error = f"{type(exc).__name__}: dreaming_consolidation_failed"
            await self._update_state(
                user_id,
                turns=turns,
                source_digest=digest,
                backfill=use_backfill,
                error=error,
                failed=True,
            )
            await self._record_run(
                user_id=user_id,
                trigger=trigger,
                status="failed",
                source_ids=source_ids,
                source_digest=digest,
                backfill=use_backfill,
                started_at=started,
                completed_at=self.now_fn(),
                error=error,
                run_id=run_id,
            )
            logger.warning("Dreaming consolidation failed for user=%s: %s", user_id, type(exc).__name__)
            return {
                "user_id": user_id,
                "status": "failed",
                "backfill": use_backfill,
                "source_count": len(turns),
                "source_message_ids": source_ids,
                "source_digest": digest,
                "error": error,
                "mutation_count": 0,
            }

    async def get_dreaming_overview(self, user_id: str | None = None, *, limit: int = 50) -> dict[str, Any]:
        """Return compact cursor/run health data without conversation content."""

        limit = max(1, min(int(limit), 200))
        session = await self._new_session()
        try:
            from sqlalchemy import select
            from ..memory.models import DreamingMemoryRun, DreamingMemoryState

            state_stmt = select(DreamingMemoryState).order_by(DreamingMemoryState.updated_at.desc()).limit(limit)
            run_stmt = select(DreamingMemoryRun).order_by(DreamingMemoryRun.created_at.desc()).limit(limit)
            if user_id:
                state_stmt = state_stmt.where(DreamingMemoryState.user_id == str(user_id))
                run_stmt = run_stmt.where(DreamingMemoryRun.user_id == str(user_id))
            states = self._scalars(await self._execute(session, state_stmt))
            runs = self._scalars(await self._execute(session, run_stmt))
            state_payload = [item.to_dict() if callable(getattr(item, "to_dict", None)) else dict(item) for item in states]
            run_payload = [item.to_dict() if callable(getattr(item, "to_dict", None)) else dict(item) for item in runs]
            completed = sum(1 for run in run_payload if run.get("status") == "completed")
            failed = sum(1 for run in run_payload if run.get("status") == "failed")
            return {
                "user_id": str(user_id) if user_id else None,
                "states": state_payload,
                "runs": run_payload,
                "totals": {
                    "users": len(state_payload),
                    "runs": len(run_payload),
                    "completed_runs": completed,
                    "failed_runs": failed,
                },
            }
        finally:
            await self._close_session(session)


DreamingMemoryConsolidationService = DreamingConsolidationService


__all__ = [
    "DEFAULT_DREAMING_BATCH_SIZE",
    "DEFAULT_DREAMING_IDLE_SECONDS",
    "DreamingConsolidationService",
    "DreamingMemoryConsolidationService",
]
