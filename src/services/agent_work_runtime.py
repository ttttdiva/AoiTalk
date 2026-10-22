"""Durable common runtime for autonomous Agent work.

This module is intentionally domain-neutral.  A :class:`WorkSource` inspects
an existing domain table and returns bounded candidates; an
:class:`ExecutionAdapter` invokes the domain service/provider boundary; and
the :class:`AgentWorkCoordinator` owns the durable claim, lease, retry and
settlement lifecycle.  No provider payload, prompt, credential or model text
is persisted by this layer.

The implementation uses a short database transaction for every state change.
PostgreSQL workers use ``FOR UPDATE SKIP LOCKED`` while SQLite fixtures use a
compare-and-set update after the row is selected.  The latter is deliberate:
SQLite has no row-level lock, so sharing PostgreSQL SQL literally would make
the test/runtime path unsafe.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import os
import re
import socket
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from sqlalchemy import and_, desc, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError

from ..features import Features
from ..memory.models import (
    Agent,
    AgentDialogue,
    AgentDialogueDelivery,
    AgentDialogueReservation,
    AgentDialogueUsage,
    AgentProjectGrant,
    AgentRevision,
    AgentRun,
    AgentRunEvent,
    ConversationParticipant,
    ConversationDispatchOutbox,
    AgentWorkEvent,
    AgentWorkItem,
    ConversationSession,
    ExternalAction,
    User,
)
from ..memory.project_repository import ProjectRepository

from .provider_failure_metadata import USAGE_COUNTER_KEYS, safe_usage_snapshot, terminal_failure_event_fields

logger = logging.getLogger(__name__)


WORK_STATES = (
    "pending",
    "claimed",
    "running",
    "awaiting_approval",
    "blocked",
    "retry_wait",
    "uncertain",
    "succeeded",
    "failed",
    "cancelled",
    "dead_letter",
)
WORK_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "dead_letter"})
WORK_ACTIVE_STATES = frozenset({"claimed", "running"})
OUTCOME_CLASSIFICATIONS = frozenset(
    {"succeeded", "transient", "permanent", "blocked", "awaiting_approval", "uncertain", "cancelled"}
)
MAX_CAUSAL_DEPTH = 16
MAX_ATTEMPTS = 100
MAX_PRIORITY = 10_000
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0
MAX_SHUTDOWN_TIMEOUT_SECONDS = 30.0
_CANONICAL_RESOURCE_KEY = re.compile(
    r"^(?:project:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}|workspace:[0-9a-f]{64})$",
    re.IGNORECASE,
)

_SECRET_KEY_MARKERS = frozenset(
    {
        "secret",
        "token",
        "password",
        "credential",
        "authorization",
        "cookie",
        "api_key",
        "apikey",
        "provider_response",
        "raw_response",
        "transcript",
        "prompt",
        "environment",
        "env",
        "path",
    }
)
_INTERACTIVE_ADMISSION_LOCKS: dict[str, asyncio.Lock] = {}
_INTERACTIVE_ADMISSION_LOCKS_GUARD = asyncio.Lock()


async def _interactive_admission_lock(session_id: uuid.UUID) -> asyncio.Lock:
    key = str(session_id)
    # The DB row/advisory lock remains authoritative across processes.  This
    # short local mutex closes the SQLite/no-row-lock gap for concurrent live
    # requests handled by one process.
    async with _INTERACTIVE_ADMISSION_LOCKS_GUARD:
        lock = _INTERACTIVE_ADMISSION_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _INTERACTIVE_ADMISSION_LOCKS[key] = lock
        return lock


def _now_utc() -> datetime:
    return datetime.utcnow()


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _string(value: Any, *, limit: int = 255) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _canonical_resource_key(value: Any) -> str | None:
    key = str(value or "").strip().lower()
    return key if key and _CANONICAL_RESOURCE_KEY.fullmatch(key) else None


def _datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
    except (TypeError, ValueError):
        return None


def _safe_json(value: Any, *, depth: int = 0) -> Any:
    """Return bounded metadata-only JSON for work rows/events."""

    if depth > 5:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str):
            lowered = value.casefold()
            if any(marker in lowered for marker in ("bearer ", "api_key", "apikey", "password=", "token=", "secret=", "credential=", "cookie=")):
                return "[REDACTED]"
            return value[:2048]
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in list(value.items())[:128]:
            name = _string(key, limit=80)
            normalized = name.casefold().replace("-", "_")
            numeric_counter = (
                normalized in USAGE_COUNTER_KEYS
                and isinstance(item, (int, float))
                and not isinstance(item, bool)
            )
            if not numeric_counter and any(marker in normalized for marker in _SECRET_KEY_MARKERS):
                continue
            output[name] = _safe_json(item, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_json(item, depth=depth + 1) for item in list(value)[:128]]
    # Datetimes and UUIDs occur frequently in adapters; serialize only these
    # known scalar types rather than calling arbitrary ``str`` on objects.
    if isinstance(value, (datetime, uuid.UUID)):
        return str(value)
    return None


def canonical_work_json(value: Any) -> str:
    return json.dumps(_safe_json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def work_mutation_fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_work_json(value).encode("utf-8")).hexdigest()


def _safe_error(value: Any, *, limit: int = 500) -> str | None:
    text = _string(value, limit=limit)
    if not text:
        return None
    # Exception strings can contain URLs or provider response fragments.  Do
    # not persist obvious secret-bearing tokens even when an adapter forgot to
    # classify the error.
    lowered = text.casefold()
    if any(
        marker in lowered
        for marker in (
            "authorization:",
            "bearer ",
            "api_key",
            "apikey",
            "password=",
            "token=",
            "secret=",
            "credential=",
            "cookie=",
            "signature=",
        )
    ):
        return "execution failed (sensitive details redacted)"
    return text


def _consume_async_task(task: asyncio.Task[Any]) -> None:
    """Consume a detached task result so late failures stay observed."""

    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        return


def _number(value: Any) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else 0.0
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class WorkCandidate:
    """Bounded candidate returned by a domain WorkSource."""

    source_type: str
    source_id: str
    source_revision: str
    intent_key: str
    domain: str
    space_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    persona_id: str | None = None
    app_id: str | None = None
    # Chat work is linked to the durable conversation session.  Keep this
    # optional so non-chat sources and rolling deployments without the new FK
    # remain compatible.
    conversation_session_id: str | None = None
    assigned_agent_id: str | None = None
    agent_revision_id: str | None = None
    required_capabilities: tuple[str, ...] = ()
    execution_adapter: str = "default"
    priority: int = 0
    not_before: datetime | None = None
    deadline: datetime | None = None
    max_attempts: int = 3
    concurrency_key: str | None = None
    budget_reservation: Mapping[str, Any] | None = None
    root_work_item_id: str | None = None
    parent_work_item_id: str | None = None
    causation_id: str | None = None
    causal_depth: int = 0
    mutation_fingerprint: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def normalized(self) -> "WorkCandidate":
        source_type = _string(self.source_type, limit=80)
        source_id = _string(self.source_id, limit=255)
        source_revision = _string(self.source_revision, limit=160) or "0"
        intent_key = _string(self.intent_key, limit=255)
        domain = _string(self.domain, limit=80) or "internal"
        if not source_type or not source_id or not intent_key:
            raise ValueError("source_type, source_id and intent_key are required")
        raw_depth = int(self.causal_depth or 0)
        if raw_depth < 0 or raw_depth > MAX_CAUSAL_DEPTH:
            raise ValueError("causal_depth exceeds the bounded causal graph")
        depth = raw_depth
        attempts = max(1, min(int(self.max_attempts or 3), MAX_ATTEMPTS))
        caps = tuple(dict.fromkeys(_string(item, limit=80) for item in self.required_capabilities if _string(item, limit=80)))[:64]
        fingerprint = _string(self.mutation_fingerprint, limit=64) if self.mutation_fingerprint else None
        if fingerprint and len(fingerprint) != 64:
            raise ValueError("mutation_fingerprint must be a SHA-256 digest")
        normalized_not_before = _datetime(self.not_before)
        normalized_deadline = _datetime(self.deadline)
        if self.not_before not in (None, "") and normalized_not_before is None:
            raise ValueError("not_before must be a datetime")
        if self.deadline not in (None, "") and normalized_deadline is None:
            raise ValueError("deadline must be a datetime")
        if normalized_not_before is not None and normalized_deadline is not None and normalized_deadline < normalized_not_before:
            raise ValueError("deadline must not precede not_before")
        return WorkCandidate(
            source_type=source_type,
            source_id=source_id,
            source_revision=source_revision,
            intent_key=intent_key,
            domain=domain,
            space_id=_string(self.space_id, limit=64) or None,
            project_id=_string(self.project_id, limit=64) or None,
            task_id=_string(self.task_id, limit=64) or None,
            persona_id=_string(self.persona_id, limit=64) or None,
            app_id=_string(self.app_id, limit=64) or None,
            conversation_session_id=_string(self.conversation_session_id, limit=64) or None,
            assigned_agent_id=_string(self.assigned_agent_id, limit=64) or None,
            agent_revision_id=_string(self.agent_revision_id, limit=64) or None,
            required_capabilities=caps,
            execution_adapter=_string(self.execution_adapter, limit=120) or "default",
            priority=max(-MAX_PRIORITY, min(int(self.priority or 0), MAX_PRIORITY)),
            not_before=normalized_not_before,
            deadline=normalized_deadline,
            max_attempts=attempts,
            concurrency_key=_string(self.concurrency_key, limit=255) or None,
            budget_reservation=_safe_json(self.budget_reservation or {}),
            root_work_item_id=_string(self.root_work_item_id, limit=64) or None,
            parent_work_item_id=_string(self.parent_work_item_id, limit=64) or None,
            causation_id=_string(self.causation_id, limit=255) or None,
            causal_depth=depth,
            mutation_fingerprint=fingerprint,
            metadata=_safe_json(self.metadata or {}),
        )


@dataclass(frozen=True)
class WorkClaim:
    work_item_id: str
    lease_token: str
    lease_owner: str
    attempt: int
    source_type: str
    source_id: str
    source_revision: str
    intent_key: str
    domain: str
    assigned_agent_id: str | None
    agent_revision_id: str | None
    execution_adapter: str
    required_capabilities: tuple[str, ...]
    project_id: str | None
    task_id: str | None
    persona_id: str | None
    concurrency_key: str | None
    budget_reservation: Mapping[str, Any] = field(default_factory=dict)
    space_id: str | None = None
    app_id: str | None = None
    causal_depth: int = 0
    mutation_fingerprint: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    conversation_session_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_item_id": self.work_item_id,
            # Lease tokens are fencing credentials; never expose them through
            # snapshots, API payloads, or adapter evidence.  The internal
            # WorkClaim object retains the token for guarded transitions.
            "lease_owner": self.lease_owner,
            "attempt": self.attempt,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "intent_key": self.intent_key,
            "domain": self.domain,
            "assigned_agent_id": self.assigned_agent_id,
            "agent_revision_id": self.agent_revision_id,
            "execution_adapter": self.execution_adapter,
            "required_capabilities": list(self.required_capabilities),
            "project_id": self.project_id,
            "task_id": self.task_id,
            "persona_id": self.persona_id,
            "conversation_session_id": self.conversation_session_id,
            "concurrency_key": self.concurrency_key,
            "budget_reservation": _safe_json(self.budget_reservation),
            "space_id": self.space_id,
            "app_id": self.app_id,
            "causal_depth": self.causal_depth,
            "mutation_fingerprint": self.mutation_fingerprint,
            "metadata": _safe_json(self.metadata),
        }


@dataclass
class ExecutionOutcome:
    """Normalized adapter result.

    ``success`` is accepted as a compatibility convenience.  New adapters
    should use ``classification`` so transient, blocked, approval and
    uncertain outcomes cannot collapse into a boolean failure.
    """

    classification: str = "succeeded"
    success: bool | None = None
    result_summary: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    domain_ref: str | None = None
    domain_status: str | None = None
    result: Mapping[str, Any] | None = None
    evidence: Sequence[Mapping[str, Any]] = ()
    usage: Mapping[str, Any] | None = None
    retry_after_seconds: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def normalized_classification(self) -> str:
        value = _string(self.classification, limit=32).casefold().replace("-", "_")
        if self.success is True:
            value = "succeeded"
        elif self.success is False and value in {"", "succeeded", "success", "ok"}:
            value = "transient"
        aliases = {"success": "succeeded", "ok": "succeeded", "awaitingapproval": "awaiting_approval", "deadletter": "permanent"}
        value = aliases.get(value, value)
        return value if value in OUTCOME_CLASSIFICATIONS else "permanent"

    @classmethod
    def from_value(cls, value: Any) -> "ExecutionOutcome":
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls(
                classification=value.get("classification", value.get("status", "succeeded")),
                success=value.get("success"),
                result_summary=value.get("result_summary") or value.get("summary"),
                error_code=value.get("error_code") or value.get("safe_error_code"),
                error_message=value.get("error_message") or value.get("error"),
                domain_ref=value.get("domain_ref") or value.get("ref"),
                domain_status=value.get("domain_status") or value.get("domain_state"),
                result=value.get("result") if isinstance(value.get("result"), Mapping) else None,
                evidence=value.get("evidence") if isinstance(value.get("evidence"), Sequence) and not isinstance(value.get("evidence"), (str, bytes)) else (),
                usage=value.get("usage") if isinstance(value.get("usage"), Mapping) else None,
                retry_after_seconds=value.get("retry_after_seconds"),
                metadata=value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {},
            )
        if value is True:
            return cls(classification="succeeded", success=True)
        if value is False or value is None:
            return cls(classification="transient", success=False)
        return cls(classification="permanent", error_message=str(value))


class InteractiveAdmissionError(RuntimeError):
    """A trusted live-chat work item could not be admitted."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = _string(code, limit=96) or "interactive_admission_denied"
        super().__init__(message or self.code)


_AUTHORITY_ADMISSION_REASONS = frozenset(
    {
        "agent_authority_denied",
        "agent_binding_changed",
        "agent_not_active",
        "agent_revision_mismatch",
        "conversation_read_denied",
        "human_actor_unavailable",
        "human_project_write_denied",
        "interactive_session_unavailable",
        "space_assignment_missing",
    }
)


def _interactive_admission_safe_code(value: Any) -> str:
    """Map a typed admission reason to the durable safe-code namespace."""

    raw = _safe_error(value, limit=96) or "interactive_admission_denied"
    if raw.startswith(("interactive_admission_", "agent_route_", "agent_dialogue_budget_")):
        return raw
    if raw in _AUTHORITY_ADMISSION_REASONS:
        return f"interactive_admission_authority_{raw}"
    return f"interactive_admission_{raw}"


def _outcome_model_route_audit(outcome: ExecutionOutcome) -> tuple[dict[str, Any], dict[str, str]]:
    """Safe model provenance an adapter reported for its AgentRun.

    Adapters put ``{"model_routes": {phase: route}}`` (and optionally
    ``primary_model_route``) in ``ExecutionOutcome.metadata``.  Only
    whitelisted identifier fields survive; nothing else from adapter metadata
    reaches the durable run row.
    """

    metadata = outcome.metadata if isinstance(outcome.metadata, Mapping) else {}
    if "model_routes" not in metadata:
        return {}, {}
    from .agent_model_routing import safe_route_projections

    routes = safe_route_projections(metadata.get("model_routes"))
    if not routes:
        return {}, {}
    primary_name = _string(metadata.get("primary_model_route"), limit=64)
    primary = routes.get(primary_name) or next(iter(routes.values()))
    return (
        {"model_routes": routes},
        {"provider": primary["provider"], "model": primary["model"]},
    )


@runtime_checkable
class WorkSource(Protocol):
    """Protocol for domain discovery adapters."""

    source_type: str

    async def discover(self, session: Any, *, now: datetime | None = None) -> Sequence[WorkCandidate]:
        ...

    async def refresh(self, session: Any, claim: WorkClaim) -> bool:
        """Return whether source state still permits execution/settlement."""


@runtime_checkable
class ExecutionAdapter(Protocol):
    """Protocol for provider/domain execution adapters."""

    adapter_key: str

    def required_capabilities(self, claim: WorkClaim) -> Sequence[str]:
        ...

    async def execute(self, claim: WorkClaim, *, coordinator: "AgentWorkCoordinator") -> ExecutionOutcome | Mapping[str, Any]:
        ...


class AgentWorkCoordinator:
    """Common durable claim/lease/retry/recovery coordinator."""

    def __init__(
        self,
        db_manager: Any | None = None,
        *,
        worker_id: str | None = None,
        lease_seconds: float = 60.0,
        max_concurrency: int | None = 2,
        max_attempts: int = 3,
        clock: Callable[[], datetime] | None = None,
        enabled: bool | None = None,
        config: Any | None = None,
        execution_actor: Any | None = None,
        task_executor: Callable[..., Any] | None = None,
        shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        self._db_manager = db_manager
        self.worker_id = _string(worker_id, limit=120) or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        self.lease_seconds = max(1.0, float(lease_seconds))
        # None means no global active-work ceiling. Claim batches remain
        # bounded separately so discovery and cancellation stay responsive.
        self.max_concurrency = (
            None if max_concurrency is None else max(1, int(max_concurrency))
        )
        self.max_attempts = max(1, min(int(max_attempts), MAX_ATTEMPTS))
        self._clock = clock or _now_utc
        self.config = config
        self.execution_actor = execution_actor
        # Optional deployment-provided callback for canonical Task mutations.
        # The coordinator never invents a User identity; without this trusted
        # service adapter TaskExecutionAdapter fails closed.
        self.task_executor = task_executor
        try:
            shutdown_timeout = float(shutdown_timeout_seconds)
        except (TypeError, ValueError):
            shutdown_timeout = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
        if not math.isfinite(shutdown_timeout) or shutdown_timeout <= 0:
            shutdown_timeout = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
        self.shutdown_timeout_seconds = max(
            0.05,
            min(shutdown_timeout, MAX_SHUTDOWN_TIMEOUT_SECONDS),
        )
        # An explicit constructor override may disable the runtime for tests
        # or maintenance, but can never enable it against the global feature
        # profile (especially Enterprise fail-closed profiles).
        self.interactive_enabled = enabled is not False
        self.enabled = Features.autonomous_agent_runtime() and enabled is not False
        self.sources: dict[str, WorkSource] = {}
        self.adapters: dict[str, ExecutionAdapter] = {}
        self._poll_task: asyncio.Task[Any] | None = None
        self._dispatch_lock = asyncio.Lock()
        self._execution_tasks: dict[asyncio.Task[Any], WorkClaim] = {}
        self._stop_event = asyncio.Event()
        self._running_tasks: set[asyncio.Task[Any]] = set()
        self._protected_tasks: set[asyncio.Task[Any]] = set()
        self._owned_task_callbacks: set[asyncio.Task[Any]] = set()
        self._running_task_owners: dict[
            asyncio.Task[Any], tuple[ExecutionAdapter, WorkClaim]
        ] = {}
        self._adapter_close_tasks: dict[asyncio.Task[Any], asyncio.Task[Any]] = {}

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is not None:
            value = value.astimezone().replace(tzinfo=None)
        return value

    def _runtime_enabled(self) -> bool:
        """Re-read the global runtime gate for long-lived coordinators."""

        return bool(self.enabled and Features.autonomous_agent_runtime())

    async def _session(self) -> Any:
        manager = self._db_manager
        if manager is None:
            from ..memory.database import get_database_manager

            manager = get_database_manager()
        if manager is None or not callable(getattr(manager, "get_session", None)):
            raise RuntimeError("database manager is unavailable")
        return await _maybe_await(manager.get_session())

    @staticmethod
    async def _close(session: Any) -> None:
        close = getattr(session, "close", None)
        if callable(close):
            await _maybe_await(close())

    @staticmethod
    async def _rollback(session: Any) -> None:
        rollback = getattr(session, "rollback", None)
        if callable(rollback):
            try:
                await _maybe_await(rollback())
            except Exception:
                pass

    def register_source(self, source: WorkSource, *, source_type: str | None = None) -> WorkSource:
        key = _string(source_type or getattr(source, "source_type", ""), limit=80)
        if not key:
            raise ValueError("WorkSource must declare source_type")
        if self._runtime_enabled():
            self.sources[key] = source
        return source

    def register_adapter(self, adapter: ExecutionAdapter, *, adapter_key: str | None = None) -> ExecutionAdapter:
        key = _string(adapter_key or getattr(adapter, "adapter_key", ""), limit=120)
        if not key:
            raise ValueError("ExecutionAdapter must declare adapter_key")
        if self._runtime_enabled():
            self.adapters[key] = adapter
        return adapter

    async def discover_and_materialize(
        self,
        *,
        source_type: str | None = None,
        project_id: Any | None = None,
        space_id: Any | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Discover candidates and durably dedupe them by source identity."""

        if not self._runtime_enabled():
            return []
        selected = [self.sources[source_type]] if source_type and source_type in self.sources else list(self.sources.values())
        session = await self._session()
        materialized: list[dict[str, Any]] = []
        try:
            for source in selected:
                discover_kwargs: dict[str, Any] = {"now": now or self._now()}
                if project_id is not None:
                    discover_kwargs["project_id"] = project_id
                if space_id is not None:
                    discover_kwargs["space_id"] = space_id
                try:
                    signature = inspect.signature(source.discover)
                    if not any(item.kind is inspect.Parameter.VAR_KEYWORD for item in signature.parameters.values()):
                        discover_kwargs = {key: value for key, value in discover_kwargs.items() if key in signature.parameters}
                except (TypeError, ValueError):
                    pass
                candidates = await _maybe_await(source.discover(session, **discover_kwargs))
                for candidate in candidates or ():
                    if project_id is not None and str(getattr(candidate, "project_id", None) or (candidate.get("project_id") if isinstance(candidate, Mapping) else "")) != str(project_id):
                        continue
                    if space_id is not None and str(getattr(candidate, "space_id", None) or (candidate.get("space_id") if isinstance(candidate, Mapping) else "")) != str(space_id):
                        continue
                    row = await self._materialize_one(session, candidate)
                    if row is not None:
                        materialized.append(row.to_safe_dict() if hasattr(row, "to_safe_dict") else row)
            await session.commit()
            return materialized
        except Exception:
            await self._rollback(session)
            raise
        finally:
            await self._close(session)

    async def materialize(self, candidate: WorkCandidate, *, session: Any | None = None) -> dict[str, Any] | None:
        if not self._runtime_enabled():
            return None
        owns = session is None
        db_session = session or await self._session()
        try:
            row = await self._materialize_one(db_session, candidate)
            if owns:
                await db_session.commit()
            return row.to_safe_dict() if row is not None else None
        except Exception:
            if owns:
                await self._rollback(db_session)
            raise
        finally:
            if owns:
                await self._close(db_session)

    async def _materialize_one(self, session: Any, candidate: WorkCandidate | Mapping[str, Any]) -> AgentWorkItem | None:
        if isinstance(candidate, Mapping):
            raw = dict(candidate)
            # WorkSource adapters may expose domain metadata alongside the
            # generic envelope.  Never pass unknown/provider-shaped keys into
            # the dataclass; retain a bounded copy under metadata instead.
            allowed = {
                "source_type", "source_id", "source_revision", "intent_key", "domain",
                "space_id", "project_id", "task_id", "persona_id", "app_id",
                "conversation_session_id",
                "assigned_agent_id", "agent_revision_id", "required_capabilities",
                "execution_adapter", "priority", "not_before", "deadline", "max_attempts",
                "concurrency_key", "budget_reservation", "root_work_item_id",
                "parent_work_item_id", "causation_id", "causal_depth", "mutation_fingerprint",
                "metadata",
            }
            extra = {key: value for key, value in raw.items() if key not in allowed}
            metadata = dict(raw.get("metadata") or {}) if isinstance(raw.get("metadata"), Mapping) else {}
            if extra:
                metadata.update(_safe_json(extra) or {})
            raw["metadata"] = metadata
            raw.pop("required_capabilities_json", None)
            raw.pop("budget_reservation_json", None)
            candidate = WorkCandidate(**{key: value for key, value in raw.items() if key in allowed})
        normalized = candidate.normalized()
        for field_name in (
            "space_id", "project_id", "task_id", "persona_id", "app_id",
            "conversation_session_id",
            "assigned_agent_id", "agent_revision_id", "root_work_item_id",
            "parent_work_item_id",
        ):
            raw_value = getattr(normalized, field_name)
            if raw_value not in (None, "") and _as_uuid(raw_value) is None:
                # Keep source-only unit tests able to expose opaque IDs, but
                # reject them at the persistence boundary where these columns
                # are native UUID FKs.
                raise ValueError(f"{field_name} must be a UUID")
        if normalized.causal_depth > MAX_CAUSAL_DEPTH:
            return None
        metadata = normalized.metadata if isinstance(normalized.metadata, Mapping) else {}
        # A canonical Project/workspace resource is a shared write fence in
        # addition to the per-session lane.  Workspace adapters place this
        # key in the bounded Work metadata; applying it here keeps ordinary
        # Chat admission and late-start checks on the same generic Work
        # resource lock without turning an arbitrary path into authority.
        dialogue_metadata = metadata.get("agent_dialogue")
        nested_resource_key = (
            dialogue_metadata.get("resource_key") or dialogue_metadata.get("workspace_resource_key")
            if isinstance(dialogue_metadata, Mapping)
            else None
        )
        resource_key = _canonical_resource_key(
            metadata.get("resource_key")
            or metadata.get("workspace_resource_key")
            or nested_resource_key
        )
        if resource_key and (
            normalized.concurrency_key in (None, "")
            or normalized.concurrency_key == f"conversation:{normalized.conversation_session_id}"
        ):
            normalized = WorkCandidate(
                **{**normalized.__dict__, "concurrency_key": resource_key}
            )
        # A work item cannot be materialized in response to its own emitted
        # event/fingerprint, and a bounded causal graph prevents trigger loops.
        if str(metadata.get("origin_work_item_id") or "") == normalized.source_id:
            return None
        if normalized.mutation_fingerprint:
            fingerprint_result = await session.execute(
                select(AgentWorkItem).where(
                    AgentWorkItem.source_type == normalized.source_type,
                    AgentWorkItem.source_id == normalized.source_id,
                    AgentWorkItem.mutation_fingerprint == normalized.mutation_fingerprint,
                )
            )
            fingerprint_row = fingerprint_result.scalars().first()
            if fingerprint_row is not None:
                return fingerprint_row
        values = {
            "source_type": normalized.source_type,
            "source_id": normalized.source_id,
            "source_revision": normalized.source_revision,
            "intent_key": normalized.intent_key,
        }
        # A conversation is a single interactive lane.  The explicit session
        # field is persisted when the rolling schema provides it; the
        # concurrency key remains the compatibility fence for old rows.
        derived_concurrency_key = normalized.concurrency_key
        if normalized.conversation_session_id and not derived_concurrency_key:
            derived_concurrency_key = f"conversation:{normalized.conversation_session_id}"
        elif normalized.task_id and not derived_concurrency_key:
            # Shared Task work uses one canonical resource key across all
            # source adapters; project/workspace callers may still provide a
            # narrower conflict key from their real source identity.
            derived_concurrency_key = f"task:{normalized.task_id}"
        if derived_concurrency_key != normalized.concurrency_key:
            normalized = WorkCandidate(
                **{
                    **normalized.__dict__,
                    "concurrency_key": derived_concurrency_key,
                }
            )
        active_revision = await session.execute(
            select(AgentWorkItem)
            .where(
                AgentWorkItem.source_type == normalized.source_type,
                AgentWorkItem.source_id == normalized.source_id,
                AgentWorkItem.intent_key == normalized.intent_key,
                AgentWorkItem.state.in_(tuple(WORK_ACTIVE_STATES)),
            )
            .order_by(desc(AgentWorkItem.updated_at))
            .limit(1)
        )
        active_row = active_revision.scalars().first()
        if active_row is not None and str(active_row.source_revision) != normalized.source_revision:
            # Keep one active execution per canonical source/intent while a
            # newer source revision is discovered; the next discovery after
            # settlement will materialize the new revision safely.
            return active_row
        existing_result = await session.execute(select(AgentWorkItem).where(*[getattr(AgentWorkItem, key) == value for key, value in values.items()]))
        row = existing_result.scalars().first()
        if row is not None:
            existing_metadata = (
                row.metadata_json if isinstance(row.metadata_json, Mapping) else {}
            )
            if (
                row.source_type == "api_agent_task"
                and row.execution_adapter == "api_agent_task"
                and row.state == "pending"
                and existing_metadata.get("admission_state") == "admitting"
            ):
                # API-Agent admission is an idempotency transaction whose
                # first durable fingerprint/nonce owns a non-executable Work
                # row. A concurrent materialize for the same stable source
                # identity must observe that winner without refreshing its
                # metadata, otherwise a later payload could steal the nonce
                # before the private session is linked.
                return row
            if row.state not in WORK_TERMINAL_STATES | WORK_ACTIVE_STATES | {"blocked"}:
                # Serialize source metadata refresh with domain reservations.
                await session.execute(update(AgentWorkItem).where(AgentWorkItem.id==row.id).values(updated_at=AgentWorkItem.updated_at))
                await session.refresh(row)
            # Refresh mutable scheduling metadata, but never resurrect terminal
            # work or overwrite an active lease from a discovery replay.
            if row.state not in WORK_TERMINAL_STATES | WORK_ACTIVE_STATES | {"blocked"}:
                row.source_revision = normalized.source_revision
                row.not_before = normalized.not_before
                row.deadline = normalized.deadline
                row.priority = normalized.priority
                row.required_capabilities_json = list(normalized.required_capabilities)
                metadata = dict(_safe_json(normalized.metadata) or {})
                # Domain-owned money holds survive discovery/retry replay.
                # WorkSource metadata cannot mint or replace these holds.
                metadata.pop("media_generation_costs", None)
                if "media_generation_costs" in (row.metadata_json or {}):
                    metadata = {"media_generation_costs":row.metadata_json["media_generation_costs"], **metadata}
                row.metadata_json = metadata
                row.domain = normalized.domain
                row.space_id = _as_uuid(normalized.space_id)
                row.project_id = _as_uuid(normalized.project_id)
                row.task_id = _as_uuid(normalized.task_id)
                row.persona_id = _as_uuid(normalized.persona_id)
                row.app_id = _as_uuid(normalized.app_id)
                if hasattr(row, "conversation_session_id"):
                    row.conversation_session_id = _as_uuid(normalized.conversation_session_id)
                if "media_generation_costs" not in metadata:
                    row.assigned_agent_id = _as_uuid(normalized.assigned_agent_id)
                    row.agent_revision_id = _as_uuid(normalized.agent_revision_id)
                row.execution_adapter = normalized.execution_adapter
                row.max_attempts = normalized.max_attempts
                row.concurrency_key = normalized.concurrency_key
                row.mutation_fingerprint = normalized.mutation_fingerprint
                row.updated_at = self._now()
            return row
        authority_blocker = await self._recurring_authority_blocker(session, normalized)
        if authority_blocker is not None:
            return authority_blocker
        row = AgentWorkItem(
            id=uuid.uuid4(),
            **values,
            domain=normalized.domain,
            space_id=_as_uuid(normalized.space_id),
            project_id=_as_uuid(normalized.project_id),
            task_id=_as_uuid(normalized.task_id),
            persona_id=_as_uuid(normalized.persona_id),
            app_id=_as_uuid(normalized.app_id),
            **(
                {"conversation_session_id": _as_uuid(normalized.conversation_session_id)}
                if hasattr(AgentWorkItem, "conversation_session_id")
                else {}
            ),
            assigned_agent_id=_as_uuid(normalized.assigned_agent_id),
            agent_revision_id=_as_uuid(normalized.agent_revision_id),
            required_capabilities_json=list(normalized.required_capabilities),
            execution_adapter=normalized.execution_adapter,
            priority=normalized.priority,
            not_before=normalized.not_before,
            deadline=normalized.deadline,
            state="pending",
            attempt_count=0,
            max_attempts=normalized.max_attempts,
            concurrency_key=normalized.concurrency_key,
            budget_reservation_json=_safe_json(normalized.budget_reservation or {}),
            root_work_item_id=_as_uuid(normalized.root_work_item_id),
            parent_work_item_id=_as_uuid(normalized.parent_work_item_id),
            causation_id=normalized.causation_id,
            causal_depth=normalized.causal_depth,
            mutation_fingerprint=normalized.mutation_fingerprint,
            metadata_json={key:value for key,value in (_safe_json(normalized.metadata or {}) or {}).items() if key!="media_generation_costs"},
            created_at=self._now(),
            updated_at=self._now(),
        )
        session.add(row)
        nested = None
        try:
            begin_nested = getattr(session, "begin_nested", None)
            if callable(begin_nested):
                nested = await _maybe_await(begin_nested())
            await session.flush()
        except IntegrityError:
            # Another discoverer won the source-key race.  The caller keeps
            # the transaction alive and reads that winner.
            if nested is not None:
                await _maybe_await(nested.rollback())
            else:
                await self._rollback(session)
            winner = (await session.execute(select(AgentWorkItem).where(*[getattr(AgentWorkItem, key) == value for key, value in values.items()]))).scalars().first()
            return winner
        else:
            if nested is not None:
                await _maybe_await(nested.commit())
        return row

    async def _recurring_authority_blocker(
        self, session: Any, candidate: WorkCandidate
    ) -> AgentWorkItem | None:
        """Reuse denial evidence only while the same pinned authority still denies.

        Recurring sources may advance their source revision every tick. A
        durable authority denial must not create an unlimited ledger of the
        same blocked attempt, but is never an authorization cache: resolve
        current grants and policy again before suppressing each recurrence.
        """

        if not candidate.assigned_agent_id or not candidate.agent_revision_id:
            return None
        conditions = [
            AgentWorkItem.source_type == candidate.source_type,
            AgentWorkItem.source_id == candidate.source_id,
            AgentWorkItem.intent_key == candidate.intent_key,
            AgentWorkItem.state == "blocked",
            AgentWorkItem.domain == candidate.domain,
            AgentWorkItem.execution_adapter == candidate.execution_adapter,
        ]
        for field_name in (
            "assigned_agent_id", "agent_revision_id", "space_id", "project_id",
            "task_id", "persona_id", "app_id",
        ):
            conditions.append(
                getattr(AgentWorkItem, field_name) == _as_uuid(getattr(candidate, field_name))
            )
        result = await session.execute(
            select(AgentWorkItem)
            .where(*conditions)
            .order_by(desc(AgentWorkItem.updated_at), desc(AgentWorkItem.id))
            .limit(1)
        )
        blocked = result.scalars().first()
        if blocked is None or set(blocked.required_capabilities_json or ()) != set(
            candidate.required_capabilities
        ):
            return None
        reason = str(blocked.blocker_code or blocked.safe_error_code or "")
        if not reason or reason.endswith("_unavailable"):
            # Dependency outages are not durable authorization decisions.
            return None
        allowed, current_reason = await self._authority_allows(
            self._claim_from_row(blocked, "")
        )
        if allowed or current_reason != reason:
            return None
        # Return the original evidence unchanged. In particular, do not
        # repin an old attempt, revive its run, or append an event per tick.
        return blocked

    # Repository-natural aliases used by WorkSource adapters during the
    # rolling WS02 migration.  All point to the same durable idempotency path.
    materialize_work_item = materialize
    ensure_work_item = materialize
    upsert_work_item = materialize

    async def claim_one(self, *, source_type: str | None = None) -> WorkClaim | None:
        claims = await self.claim_many(1, source_type=source_type)
        return claims[0] if claims else None

    async def claim_many(self, limit: int, *, source_type: str | None = None) -> list[WorkClaim]:
        if not self._runtime_enabled():
            return []
        count = max(1, min(int(limit or 1), 32 if self.max_concurrency is None else self.max_concurrency))
        source_key = _string(source_type, limit=80) if source_type else None
        session = await self._session()
        try:
            now = self._now()
            # Serialize the global capacity decision across PostgreSQL
            # workers. A read-then-update race could otherwise let two
            # workers exceed max_concurrency. SQLite relies on its writer
            # lock and the CAS predicates below.
            await self._lock_concurrency_key(
                session,
                "__aoitalk_agent_work_capacity__",
            )
            if self.max_concurrency is not None and await self._active_count(session) >= self.max_concurrency:
                await session.commit()
                return []
            # Do not leave due work silently stranded once its hard deadline
            # has elapsed.  Project an explicit blocker/dead-letter event so
            # operators can see why it was not claimed and budget reservations
            # are released through the same transition helper.
            expired_conditions = [
                AgentWorkItem.state.in_(("pending", "retry_wait")),
                AgentWorkItem.deadline.is_not(None),
                AgentWorkItem.deadline <= now,
            ]
            if source_key:
                expired_conditions.append(AgentWorkItem.source_type == source_key)
            expired_result = await session.execute(
                select(AgentWorkItem)
                .where(*expired_conditions)
                .order_by(AgentWorkItem.deadline, AgentWorkItem.id)
                .limit(count),
            )
            for expired in (await _maybe_await(expired_result.scalars().all())):
                await self._transition_locked(
                    session,
                    expired,
                    "blocked",
                    token=None,
                    error_code="deadline_expired",
                    error_message="work item deadline has expired",
                )
            eligible = or_(
                and_(AgentWorkItem.state == "pending", or_(AgentWorkItem.not_before.is_(None), AgentWorkItem.not_before <= now)),
                and_(AgentWorkItem.state == "retry_wait", or_(AgentWorkItem.not_before.is_(None), AgentWorkItem.not_before <= now)),
            )
            claim_conditions = [eligible, or_(AgentWorkItem.deadline.is_(None), AgentWorkItem.deadline > now)]
            # Interactive chat is admitted and driven by the live transport.
            # The background coordinator is delivery/recovery infrastructure
            # for autonomous work and must never replay a chat turn after a
            # restart or lease expiry.
            claim_conditions.extend(
                [
                    AgentWorkItem.source_type != "chat_turn",
                    AgentWorkItem.execution_adapter != "interactive_chat",
                ]
            )
            if source_key:
                claim_conditions.append(AgentWorkItem.source_type == source_key)
            local_claims = self._local_active_claims()
            if local_claims:
                # Recovery may enqueue another attempt after a lost lease.
                # Never overlap attempts of the same item while its previous
                # adapter/session/cleanup is still owned by this coordinator.
                claim_conditions.append(AgentWorkItem.id.not_in(
                    [_as_uuid(work_id) for work_id in local_claims]
                ))
            active_key_result = await session.execute(
                select(AgentWorkItem.concurrency_key).where(
                    AgentWorkItem.state.in_(tuple(WORK_ACTIVE_STATES)),
                    AgentWorkItem.concurrency_key.is_not(None),
                )
            )
            active_keys = {str(value) for value in active_key_result.scalars().all() if value}
            # Cancelled/lost-lease adapters may still be draining native work
            # or closing their session. Keep their local resource exclusion
            # until the owned cleanup completes, even after DB settlement.
            active_keys.update(
                claim.concurrency_key
                for claim in local_claims.values()
                if claim.concurrency_key
            )
            if active_keys:
                # Exclude busy resources before LIMIT so a queued sibling on
                # one resource does not strand independent work in a free slot.
                claim_conditions.append(or_(
                    AgentWorkItem.concurrency_key.is_(None),
                    AgentWorkItem.concurrency_key.not_in(active_keys),
                ))
            stmt = (
                select(AgentWorkItem)
                .where(*claim_conditions)
                .order_by(desc(AgentWorkItem.priority), AgentWorkItem.created_at, AgentWorkItem.id)
                .limit(count)
            )
            try:
                stmt = stmt.with_for_update(skip_locked=True)
            except TypeError:  # tiny query doubles
                stmt = stmt.with_for_update()
            rows = list((await session.execute(stmt)).scalars().all())
            # Keep work pending until its domain adapter is registered. This
            # prevents startup/lazy-harness races from consuming attempts and
            # dead-lettering otherwise valid code/media work.
            rows = [
                row
                for row in rows
                if str(row.execution_adapter or "default") in self.adapters
                or "default" in self.adapters
                or (not self.adapters and not self.sources)
            ]
            claims: list[WorkClaim] = []
            for row in rows:
                if self.max_concurrency is not None and await self._active_count(session) >= self.max_concurrency:
                    break
                attempts = int(row.attempt_count or 0)
                max_attempts = max(1, min(int(row.max_attempts or self.max_attempts), MAX_ATTEMPTS))
                if attempts >= max_attempts:
                    await self._transition_locked(session, row, "dead_letter", token=None, error_code="max_attempts_exhausted", error_message="maximum attempts exhausted")
                    continue
                concurrency_key = str(row.concurrency_key or "").strip()
                await self._lock_concurrency_key(session, concurrency_key)
                if concurrency_key:
                    current_key = await session.execute(
                        select(func.count(AgentWorkItem.id)).where(
                            AgentWorkItem.state.in_(tuple(WORK_ACTIVE_STATES)),
                            AgentWorkItem.concurrency_key == concurrency_key,
                        )
                    )
                    if int(current_key.scalar_one() if hasattr(current_key, "scalar_one") else current_key.scalar() or 0) > 0:
                        continue
                if concurrency_key and concurrency_key in active_keys:
                    continue
                if not await self._agent_capacity_available(session, row):
                    continue
                if not await self._interactive_session_available(session, row):
                    continue
                budget = row.budget_reservation_json if isinstance(row.budget_reservation_json, Mapping) else {}
                if budget and (
                    (budget.get("exhausted") is True)
                    or (budget.get("remaining") is not None and _number(budget.get("remaining")) <= 0)
                    or (budget.get("remaining_units") is not None and _number(budget.get("remaining_units")) <= 0)
                ):
                    await self._transition_locked(session, row, "blocked", token=None, error_code="budget_exhausted", error_message="budget reservation exhausted")
                    continue
                budget = dict(budget)
                if budget and not (budget.get("awaiting_approval") is True and budget.get("settled_state") == "awaiting_approval"):
                    reservation = self._reserve_budget(budget)
                    if reservation is None:
                        await self._transition_locked(session, row, "blocked", token=None, error_code="budget_exhausted", error_message="budget reservation exhausted")
                        continue
                token = uuid.uuid4().hex
                result = await session.execute(
                    update(AgentWorkItem)
                    .where(
                        AgentWorkItem.id == row.id,
                        AgentWorkItem.state.in_(("pending", "retry_wait")),
                        or_(AgentWorkItem.not_before.is_(None), AgentWorkItem.not_before <= now),
                    )
                    .values(
                        state="claimed",
                        attempt_count=attempts + 1,
                        lease_owner=self.worker_id,
                        lease_token=token,
                        lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                        heartbeat_at=now,
                        claimed_at=now,
                        updated_at=now,
                        safe_error_code=None,
                        budget_reservation_json=budget,
                    )
                )
                if int(getattr(result, "rowcount", 0) or 0) != 1:
                    continue
                row.state = "claimed"
                row.attempt_count = attempts + 1
                row.lease_owner = self.worker_id
                row.lease_token = token
                row.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
                row.heartbeat_at = now
                row.updated_at = now
                row.claimed_at = now
                row.budget_reservation_json = budget
                if budget.get("awaiting_approval") is True:
                    budget.pop("awaiting_approval", None)
                    budget.pop("settled_state", None)
                if concurrency_key:
                    active_keys.add(concurrency_key)
                claims.append(self._claim_from_row(row, token))
                await self._append_event_in_session(
                    session,
                    row,
                    event_type="work.claimed",
                    actor_kind="service",
                    actor_id=self.worker_id,
                    payload={"attempt": row.attempt_count},
                )
            await session.commit()
            return claims
        except OperationalError:
            await self._rollback(session)
            # SQLite's whole-database writer lock is a supported test/runtime
            # condition.  A caller may retry the tick; do not turn it into a
            # fabricated claim.
            return []
        except Exception:
            await self._rollback(session)
            raise
        finally:
            await self._close(session)

    async def admit_interactive(
        self,
        *,
        session_id: str,
        run_id: str,
        agent_id: str,
        agent_revision_id: str,
        user_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        client_message_id: str | None = None,
        source_revision: str | None = None,
        concurrency_key: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        session: Any | None = None,
    ) -> dict[str, Any]:
        """Admit one trusted chat turn into the common Work ledger.

        This is the live lane's explicit admission path.  It intentionally
        does not expose a lease and it never makes the row eligible for the
        background poller; the transport must call :meth:`start_interactive`.
        ``session`` is accepted so REST dispatch can enlist the row in its
        existing message/run/outbox transaction.
        """

        session_uuid = _as_uuid(session_id)
        run_uuid = _as_uuid(run_id)
        agent_uuid = _as_uuid(agent_id)
        revision_uuid = _as_uuid(agent_revision_id)
        if not session_uuid or not run_uuid or not agent_uuid or not revision_uuid:
            raise InteractiveAdmissionError("invalid_interactive_identity")
        if not self.interactive_enabled:
            raise InteractiveAdmissionError("interactive_lane_disabled")
        admission_lock = await _interactive_admission_lock(session_uuid)
        await admission_lock.acquire()
        owns = session is None
        db_session = session
        try:
            if db_session is None:
                db_session = await self._session()
            try:
                conversation_result = await db_session.execute(
                    select(ConversationSession)
                    .where(ConversationSession.id == session_uuid)
                    .with_for_update()
                )
                conversation = conversation_result.scalars().first()
            except Exception:
                conversation = await db_session.get(ConversationSession, session_uuid)
            if conversation is None or getattr(conversation, "deleted_at", None) is not None:
                raise InteractiveAdmissionError("session_unavailable")
            agent = await db_session.get(Agent, agent_uuid)
            revision = await db_session.get(AgentRevision, revision_uuid)
            if agent is None or str(getattr(agent, "state", "")) != "active":
                raise InteractiveAdmissionError("agent_not_active")
            if revision is None or _as_uuid(getattr(revision, "agent_id", None)) != agent_uuid:
                raise InteractiveAdmissionError("agent_revision_mismatch")
            expected_user_id = str(
                user_id or getattr(conversation, "user_id", None) or ""
            )
            # A dispatch deadletter can terminalize its AgentRun/outbox while
            # leaving a never-started interactive Work row pending. Reconcile
            # only that exact, fenced row in this admission transaction before
            # applying the per-session busy check. The helper is typed and
            # same-session; it never replays a provider or touches a live or
            # uncertain attempt.
            from .agent_run_service import AgentRunService

            await AgentRunService(
                self._db_manager,
                config=self.config,
            ).reconcile_deadletter_interactive_work_in_session(
                db_session,
                session_id=session_uuid,
                agent_id=agent_uuid,
                agent_revision_id=revision_uuid,
                user_id=user_id or getattr(conversation, "user_id", None),
            )
            # The live admission session uses autoflush=False in production
            # paths as well as in the ORM regression fixture.  Make the
            # reconciliation visible to the following busy-fence SELECT in
            # this same transaction; otherwise PostgreSQL/SQLite can still
            # observe the stale pending row.
            await db_session.flush()
            existing_result = await db_session.execute(
                select(AgentWorkItem).where(
                    AgentWorkItem.source_type == "chat_turn",
                    AgentWorkItem.source_id == str(run_uuid),
                    AgentWorkItem.source_revision == (str(source_revision or revision_uuid)),
                    AgentWorkItem.intent_key == "interactive_chat",
                )
            )
            existing = existing_result.scalars().first()
            if existing is not None:
                existing_state = str(getattr(existing, "state", "") or "").casefold()
                if existing_state == "uncertain":
                    proven = await self._restart_uncertain_work_is_releasable(
                        db_session,
                        existing,
                        session_id=session_uuid,
                        user_id=expected_user_id,
                        agent_id=agent_uuid,
                        agent_revision_id=revision_uuid,
                    )
                    if proven is None:
                        raise InteractiveAdmissionError("interactive_session_busy")
                    # The exact retry is idempotent, but the uncertain Work
                    # remains historical and is never reactivated.
                    return proven.to_safe_dict()
                if (
                    _as_uuid(getattr(existing, "assigned_agent_id", None)) != agent_uuid
                    or _as_uuid(getattr(existing, "agent_revision_id", None)) != revision_uuid
                ):
                    raise InteractiveAdmissionError("interactive_identity_conflict")
                if getattr(existing, "active_agent_run_id", None) not in (None, run_uuid):
                    raise InteractiveAdmissionError("interactive_run_conflict")
                if existing_state in WORK_TERMINAL_STATES:
                    # Idempotent retries may converge on the original Run,
                    # but a terminal Work is never reactivated or rebound to
                    # a provider attempt.
                    return existing.to_safe_dict()
                if getattr(existing, "active_agent_run_id", None) is None:
                    existing.active_agent_run_id = run_uuid
                if hasattr(existing, "conversation_session_id"):
                    existing.conversation_session_id = session_uuid
                if owns:
                    await db_session.commit()
                return existing.to_safe_dict()
            # One active interactive turn per session, regardless of project
            # membership.  The live transport may admit another turn only
            # after the previous work has settled. Fetch every uncertain row
            # and prove each one independently; an unknown row must not be
            # hidden by a query predicate or a one-row LIMIT.
            session_conditions = [
                AgentWorkItem.state.in_(
                    ("pending", "claimed", "running", "awaiting_approval", "uncertain")
                ),
                AgentWorkItem.execution_adapter == "interactive_chat",
            ]
            if hasattr(AgentWorkItem, "conversation_session_id"):
                session_conditions.append(AgentWorkItem.conversation_session_id == session_uuid)
            else:
                session_conditions.extend(
                    [
                        AgentWorkItem.source_type == "chat_turn",
                        AgentWorkItem.concurrency_key == f"conversation:{session_uuid}",
                    ]
                )
            active_session = await db_session.execute(
                select(AgentWorkItem).where(*session_conditions)
            )
            for active_row in active_session.scalars().all():
                if str(getattr(active_row, "state", "") or "").casefold() != "uncertain":
                    raise InteractiveAdmissionError("interactive_session_busy")
                proven = await self._restart_uncertain_work_is_releasable(
                    db_session,
                    active_row,
                    session_id=session_uuid,
                    user_id=expected_user_id,
                    agent_id=agent_uuid,
                    agent_revision_id=revision_uuid,
                )
                if proven is not None:
                    continue
                raise InteractiveAdmissionError("interactive_session_busy")
            shared_key = _string(concurrency_key, limit=255) or None
            task_uuid = _as_uuid(task_id)
            if shared_key is None and task_uuid is not None:
                # Task identity is a real shared resource across sessions;
                # the conversation key above remains the per-session fence.
                shared_key = f"task:{task_uuid}"
            if shared_key:
                await self._lock_concurrency_key(db_session, shared_key)
                shared_result = await db_session.execute(
                    select(AgentWorkItem.id).where(
                        AgentWorkItem.state.in_(
                            ("pending", "claimed", "running", "awaiting_approval", "uncertain")
                        ),
                        AgentWorkItem.concurrency_key == shared_key,
                    ).limit(1)
                )
                if shared_result.scalar() is not None:
                    raise InteractiveAdmissionError("interactive_shared_resource_busy")
            candidate = WorkCandidate(
                source_type="chat_turn",
                source_id=str(run_uuid),
                source_revision=str(source_revision or revision_uuid),
                intent_key="interactive_chat",
                domain="conversation",
                project_id=str(project_id) if project_id else None,
                task_id=str(task_id) if task_id else None,
                conversation_session_id=str(session_uuid),
                assigned_agent_id=str(agent_uuid),
                agent_revision_id=str(revision_uuid),
                execution_adapter="interactive_chat",
                max_attempts=1,
                concurrency_key=shared_key or f"conversation:{session_uuid}",
                metadata={
                    **dict(metadata or {}),
                    "interactive": True,
                    "bookkeeping_only": True,
                    "side_effects_approved": False,
                    "conversation_session_id": str(session_uuid),
                    "run_id": str(run_uuid),
                    **({"client_message_id": str(client_message_id)} if client_message_id else {}),
                },
            )
            if not await self._agent_capacity_available(
                db_session,
                AgentWorkItem(
                    assigned_agent_id=agent_uuid,
                    agent_revision_id=revision_uuid,
                    task_id=_as_uuid(task_id),
                    state="pending",
                ),
            ):
                raise InteractiveAdmissionError("agent_concurrency_limit")
            row = await self._materialize_one(db_session, candidate)
            if row is None:
                raise InteractiveAdmissionError("interactive_admission_denied")
            row.active_agent_run_id = run_uuid
            if hasattr(row, "conversation_session_id"):
                row.conversation_session_id = session_uuid
            else:
                metadata_row = dict(getattr(row, "metadata_json", None) or {})
                metadata_row["conversation_session_id"] = str(session_uuid)
                row.metadata_json = metadata_row
            run = await db_session.get(AgentRun, run_uuid)
            if run is not None:
                if run.work_item_id not in (None, row.id):
                    raise InteractiveAdmissionError("run_work_binding_conflict")
                run.work_item_id = row.id
            if owns:
                await db_session.commit()
            return row.to_safe_dict()
        except InteractiveAdmissionError:
            if owns:
                await self._rollback(db_session)
            raise
        except IntegrityError as exc:
            if owns:
                await self._rollback(db_session)
            raise InteractiveAdmissionError("interactive_duplicate") from exc
        except Exception:
            if owns:
                await self._rollback(db_session)
            raise
        finally:
            if owns and db_session is not None:
                await self._close(db_session)
            admission_lock.release()

    async def admit_transport(
        self,
        candidate: WorkCandidate | Mapping[str, Any],
        *,
        session: Any | None = None,
    ) -> dict[str, Any] | None:
        """Materialize one provider-free AgentDialogue transport Work item.

        Dialogue handoff is interactive infrastructure, so it remains usable
        while the autonomous runtime gate is off.  This narrow exception does
        not alter :meth:`_runtime_enabled`, the autonomous poller, source
        discovery, or ordinary Agent capacity accounting.
        """

        if not self.interactive_enabled:
            raise InteractiveAdmissionError("interactive_lane_disabled")
        if isinstance(candidate, Mapping):
            candidate = WorkCandidate(**{
                key: value
                for key, value in dict(candidate).items()
                if key in WorkCandidate.__dataclass_fields__
            })
        normalized = candidate.normalized()
        if (
            normalized.source_type != "agent_dialogue"
            or normalized.execution_adapter != "dialogue_transport"
            or normalized.assigned_agent_id is not None
            or normalized.agent_revision_id is not None
        ):
            raise InteractiveAdmissionError("transport_binding_invalid")
        owns = session is None
        db_session = session or await self._session()
        try:
            row = await self._materialize_one(db_session, normalized)
            if owns:
                await db_session.commit()
            return row.to_safe_dict() if row is not None else None
        except Exception:
            if owns:
                await self._rollback(db_session)
            raise
        finally:
            if owns:
                await self._close(db_session)

    async def claim_transport(
        self,
        limit: int = 1,
        *,
        work_item_id: str | None = None,
        source_type: str | None = None,
        execution_adapter: str | None = None,
    ) -> list[WorkClaim]:
        """Claim only AgentDialogue transport rows with the common Work lease.

        The query intentionally bypasses the autonomous runtime gate and
        Agent capacity limit, while retaining the DB resource fence, deadline,
        CAS lease and per-session concurrency key.  It never invokes an
        adapter/provider; the transport service settles the claim after the
        ordinary Chat handoff commits.
        """

        if not self.interactive_enabled:
            return []
        if source_type not in (None, "agent_dialogue") or execution_adapter not in (None, "dialogue_transport"):
            return []
        count = max(1, min(int(limit or 1), 32 if self.max_concurrency is None else self.max_concurrency))
        requested_id = _as_uuid(work_item_id) if work_item_id else None
        session = await self._session()
        try:
            now = self._now()
            conditions = [
                AgentWorkItem.source_type == "agent_dialogue",
                AgentWorkItem.execution_adapter == "dialogue_transport",
                AgentWorkItem.assigned_agent_id.is_(None),
                AgentWorkItem.state.in_(("pending", "retry_wait")),
                or_(AgentWorkItem.not_before.is_(None), AgentWorkItem.not_before <= now),
                or_(AgentWorkItem.deadline.is_(None), AgentWorkItem.deadline > now),
            ]
            if requested_id is not None:
                conditions.append(AgentWorkItem.id == requested_id)
            # Recover only an unhanded transport after restart.  Once the
            # bridge has recorded a receiver Run, the handoff boundary is
            # ambiguous and must remain ``uncertain``; it is never replayed
            # blindly.  This is the narrow transport equivalent of the
            # existing common Work stale-lease recovery and does not create a
            # second scheduler.
            from ..memory.models import AgentDialogueDelivery

            stale_rows = list(
                (
                    await session.execute(
                        select(AgentWorkItem)
                        .where(
                            AgentWorkItem.source_type == "agent_dialogue",
                            AgentWorkItem.execution_adapter == "dialogue_transport",
                            AgentWorkItem.assigned_agent_id.is_(None),
                            AgentWorkItem.state.in_(tuple(WORK_ACTIVE_STATES)),
                            AgentWorkItem.lease_expires_at.is_not(None),
                            AgentWorkItem.lease_expires_at <= now,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for stale in stale_rows:
                delivery = await session.get(AgentDialogueDelivery, _as_uuid(stale.source_id))
                previous = str(stale.state or "")
                observed_owner = stale.lease_owner
                observed_token = stale.lease_token
                observed_expiry = stale.lease_expires_at
                observed_updated_at = stale.updated_at
                observed_active_run = stale.active_agent_run_id
                handed_off = delivery is not None and getattr(delivery, "receiver_run_id", None) is not None
                if handed_off:
                    recovered_state = "uncertain"
                    recovered_classification = "uncertain"
                    recovered_error_code = "transport_handoff_uncertain"
                    recovered_completed_at = now
                    recovered_not_before = None
                    recovered_next_attempt_at = None
                else:
                    recovered_state = "pending"
                    recovered_classification = None
                    recovered_error_code = "stale_transport_handoff_recovered"
                    recovered_completed_at = None
                    recovered_not_before = now
                    recovered_next_attempt_at = now
                observed_attempts = int(stale.attempt_count or 0)
                recovered_attempts = (
                    observed_attempts
                    if handed_off
                    else max(0, observed_attempts - 1)
                )
                # A stale snapshot is only eligible for recovery while the
                # exact lease observed above is still current.  Another
                # process may have reclaimed and claimed the same row after
                # the SELECT; rowcount=0 then leaves that fresh lease intact.
                recovered = await session.execute(
                    update(AgentWorkItem)
                    .where(
                        AgentWorkItem.id == stale.id,
                        AgentWorkItem.source_type == "agent_dialogue",
                        AgentWorkItem.execution_adapter == "dialogue_transport",
                        AgentWorkItem.assigned_agent_id.is_(None),
                        AgentWorkItem.state == previous,
                        AgentWorkItem.lease_owner == observed_owner,
                        AgentWorkItem.lease_token == observed_token,
                        AgentWorkItem.lease_expires_at == observed_expiry,
                        AgentWorkItem.active_agent_run_id == observed_active_run,
                        AgentWorkItem.updated_at == observed_updated_at,
                        AgentWorkItem.lease_expires_at.is_not(None),
                        AgentWorkItem.lease_expires_at <= now,
                    )
                    .values(
                        state=recovered_state,
                        outcome_classification=recovered_classification,
                        safe_error_code=recovered_error_code,
                        completed_at=recovered_completed_at,
                        not_before=recovered_not_before,
                        next_attempt_at=recovered_next_attempt_at,
                        attempt_count=recovered_attempts,
                        lease_owner=None,
                        lease_token=None,
                        lease_expires_at=None,
                        heartbeat_at=now,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if int(getattr(recovered, "rowcount", 0) or 0) != 1:
                    continue
                stale.state = recovered_state
                stale.outcome_classification = recovered_classification
                stale.safe_error_code = recovered_error_code
                stale.completed_at = recovered_completed_at
                stale.not_before = recovered_not_before
                stale.next_attempt_at = recovered_next_attempt_at
                stale.attempt_count = recovered_attempts
                stale.lease_owner = None
                stale.lease_token = None
                stale.lease_expires_at = None
                stale.heartbeat_at = now
                stale.updated_at = now
                await self._append_event_in_session(
                    session,
                    stale,
                    event_type=f"work.{stale.state}",
                    actor_kind="service",
                    actor_id=self.worker_id,
                    from_state=previous,
                    to_state=str(stale.state),
                    safe_error_code=str(stale.safe_error_code),
                    payload={"recovered_after_restart": True, "transport": True},
                )
            # A transport handoff is not autonomous Agent execution.  Keep
            # resource exclusion, but do not call _active_count or
            # _agent_capacity_available (assigned_agent_id is NULL by
            # contract and must not consume an Agent slot).
            active_result = await session.execute(
                select(AgentWorkItem.concurrency_key).where(
                    AgentWorkItem.state.in_(tuple(WORK_ACTIVE_STATES)),
                    AgentWorkItem.concurrency_key.is_not(None),
                )
            )
            active_keys = {str(value) for value in active_result.scalars().all() if value}
            if active_keys:
                conditions.append(
                    or_(
                        AgentWorkItem.concurrency_key.is_(None),
                        AgentWorkItem.concurrency_key.not_in(active_keys),
                    )
                )
            statement = (
                select(AgentWorkItem)
                .where(*conditions)
                .order_by(desc(AgentWorkItem.priority), AgentWorkItem.created_at, AgentWorkItem.id)
                .limit(count)
            )
            try:
                statement = statement.with_for_update(skip_locked=True)
            except TypeError:
                statement = statement.with_for_update()
            rows = list((await session.execute(statement)).scalars().all())
            claims: list[WorkClaim] = []
            for row in rows:
                key = str(row.concurrency_key or "")
                if key:
                    await self._lock_concurrency_key(session, key)
                    current = await session.execute(
                        select(func.count(AgentWorkItem.id)).where(
                            AgentWorkItem.state.in_(tuple(WORK_ACTIVE_STATES)),
                            AgentWorkItem.concurrency_key == key,
                        )
                    )
                    if int(current.scalar_one() if hasattr(current, "scalar_one") else current.scalar() or 0) > 0:
                        continue
                token = uuid.uuid4().hex
                attempts = int(row.attempt_count or 0)
                max_attempts = max(1, min(int(row.max_attempts or self.max_attempts), MAX_ATTEMPTS))
                if attempts >= max_attempts:
                    await self._transition_locked(
                        session,
                        row,
                        "dead_letter",
                        token=None,
                        error_code="max_attempts_exhausted",
                        error_message="transport handoff attempts exhausted",
                    )
                    continue
                expiry = now + timedelta(seconds=self.lease_seconds)
                fenced = await session.execute(
                    update(AgentWorkItem)
                    .where(
                        AgentWorkItem.id == row.id,
                        AgentWorkItem.source_type == "agent_dialogue",
                        AgentWorkItem.execution_adapter == "dialogue_transport",
                        AgentWorkItem.assigned_agent_id.is_(None),
                        AgentWorkItem.state.in_(("pending", "retry_wait")),
                        or_(AgentWorkItem.not_before.is_(None), AgentWorkItem.not_before <= now),
                        or_(AgentWorkItem.deadline.is_(None), AgentWorkItem.deadline > now),
                    )
                    .values(
                        state="claimed",
                        attempt_count=attempts + 1,
                        lease_owner=self.worker_id,
                        lease_token=token,
                        lease_expires_at=expiry,
                        heartbeat_at=now,
                        claimed_at=now,
                        updated_at=now,
                        safe_error_code=None,
                    )
                )
                if int(getattr(fenced, "rowcount", 0) or 0) != 1:
                    continue
                row.state = "claimed"
                row.attempt_count = attempts + 1
                row.lease_owner = self.worker_id
                row.lease_token = token
                row.lease_expires_at = expiry
                row.heartbeat_at = now
                row.claimed_at = now
                row.updated_at = now
                if key:
                    active_keys.add(key)
                claims.append(self._claim_from_row(row, token))
                await self._append_event_in_session(
                    session,
                    row,
                    event_type="work.transport_claimed",
                    actor_kind="service",
                    actor_id=self.worker_id,
                    payload={"transport": True, "attempt": attempts + 1},
                )
            await session.commit()
            return claims
        except OperationalError:
            await self._rollback(session)
            return []
        except Exception:
            await self._rollback(session)
            raise
        finally:
            await self._close(session)

    async def renew_transport(self, claim: WorkClaim | Mapping[str, Any]) -> bool:
        normalized = self._coerce_claim(claim)
        if (
            normalized.source_type != "agent_dialogue"
            or normalized.execution_adapter != "dialogue_transport"
            or not self.interactive_enabled
        ):
            return False
        session = await self._session()
        try:
            now = self._now()
            result = await session.execute(
                update(AgentWorkItem)
                .where(
                    AgentWorkItem.id == _as_uuid(normalized.work_item_id),
                    AgentWorkItem.state == "claimed",
                    AgentWorkItem.lease_owner == normalized.lease_owner,
                    AgentWorkItem.lease_token == normalized.lease_token,
                    AgentWorkItem.lease_expires_at > now,
                )
                .values(
                    heartbeat_at=now,
                    lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                    updated_at=now,
                )
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0) == 1
        except Exception:
            await self._rollback(session)
            return False
        finally:
            await self._close(session)

    async def release_transport(
        self,
        claim: WorkClaim | Mapping[str, Any],
        *,
        retry_at: datetime | None = None,
    ) -> bool:
        """Return a transport claim to pending without consuming a retry."""

        normalized = self._coerce_claim(claim)
        if normalized.source_type != "agent_dialogue" or normalized.execution_adapter != "dialogue_transport":
            return False
        session = await self._session()
        try:
            now = self._now()
            when = retry_at or now
            result = await session.execute(
                update(AgentWorkItem)
                .where(
                    AgentWorkItem.id == _as_uuid(normalized.work_item_id),
                    AgentWorkItem.state == "claimed",
                    AgentWorkItem.lease_owner == normalized.lease_owner,
                    AgentWorkItem.lease_token == normalized.lease_token,
                    AgentWorkItem.lease_expires_at > now,
                    AgentWorkItem.attempt_count > 0,
                )
                .values(
                    state="pending",
                    # A claim released before handoff (for example because
                    # availability was deferred) did not attempt a provider
                    # or consume a retry.  Return its attempt budget so a
                    # busy root/child can wait without cost growth.
                    attempt_count=AgentWorkItem.attempt_count - 1,
                    not_before=when,
                    next_attempt_at=when,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    heartbeat_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0) == 1
        except Exception:
            await self._rollback(session)
            return False
        finally:
            await self._close(session)

    async def settle_transport(
        self,
        claim: WorkClaim | Mapping[str, Any],
        outcome: ExecutionOutcome | Mapping[str, Any] | bool,
        *,
        source_ok: bool | None = True,
    ) -> dict[str, Any] | None:
        normalized = self._coerce_claim(claim)
        if normalized.source_type != "agent_dialogue" or normalized.execution_adapter != "dialogue_transport":
            return {"work_item_id": normalized.work_item_id, "settled": False, "reason": "not_transport"}
        # The existing settle path already owns the token/CAS transition and
        # event/budget bookkeeping.  _interactive is the narrow gate escape;
        # no provider or Agent capacity path is changed.
        return await self.settle(normalized, outcome, source_ok=source_ok, _interactive=True)

    admit_dialogue_transport = admit_transport
    claim_dialogue_transport = claim_transport
    renew_dialogue_transport = renew_transport
    release_dialogue_transport = release_transport
    settle_dialogue_transport = settle_transport

    async def start_interactive(
        self,
        work_item_id: str,
        *,
        run_id: str | None = None,
        lease_seconds: float | None = None,
    ) -> WorkClaim | None:
        """Start an exact admitted chat item with a private fenced lease."""

        if not self.interactive_enabled:
            return None
        item_uuid = _as_uuid(work_item_id)
        if item_uuid is None:
            return None
        db_session = await self._session()
        try:
            now = self._now()
            result = await db_session.execute(
                select(AgentWorkItem).where(AgentWorkItem.id == item_uuid).with_for_update()
            )
            row = result.scalars().first()
            if row is None or (
                str(row.source_type or "") != "chat_turn"
                or str(row.execution_adapter or "") != "interactive_chat"
            ):
                await self._rollback(db_session)
                return None
            if run_id and getattr(row, "active_agent_run_id", None) not in (None, _as_uuid(run_id)):
                await self._rollback(db_session)
                return None
            if row.state == "running" and row.lease_owner == self.worker_id:
                return self._claim_from_row(row, str(row.lease_token or ""))
            if row.state != "pending":
                await self._rollback(db_session)
                return None
            await self._validate_interactive_start_authority(
                db_session,
                row,
                requested_run_id=run_id,
            )
            if not await self._interactive_session_available(db_session, row):
                raise InteractiveAdmissionError("interactive_session_busy")
            if not await self._interactive_shared_resource_available(db_session, row):
                raise InteractiveAdmissionError("interactive_shared_resource_busy")
            if not await self._agent_capacity_available(db_session, row):
                await self._rollback(db_session)
                return None
            token = uuid.uuid4().hex
            expiry = now + timedelta(seconds=max(1.0, float(lease_seconds or self.lease_seconds)))
            fence = await db_session.execute(
                update(AgentWorkItem).where(
                    AgentWorkItem.id == item_uuid,
                    AgentWorkItem.state == "pending",
                    AgentWorkItem.lease_token.is_(None),
                ).values(
                    state="running",
                    attempt_count=1,
                    lease_owner=self.worker_id,
                    lease_token=token,
                    lease_expires_at=expiry,
                    heartbeat_at=now,
                    claimed_at=now,
                    started_at=now,
                    updated_at=now,
                )
            )
            if int(getattr(fence, "rowcount", 0) or 0) != 1:
                await self._rollback(db_session)
                return None
            row.state = "running"
            row.attempt_count = 1
            row.lease_owner = self.worker_id
            row.lease_token = token
            row.lease_expires_at = expiry
            row.heartbeat_at = now
            row.claimed_at = now
            row.started_at = now
            row.updated_at = now
            await self._append_event_in_session(
                db_session,
                row,
                event_type="work.running",
                actor_kind="service",
                actor_id=self.worker_id,
                agent_run_id=getattr(row, "active_agent_run_id", None),
                from_state="pending",
                to_state="running",
                payload={"interactive": True, "run_id": str(getattr(row, "active_agent_run_id", "") or "")},
            )
            await db_session.commit()
            return self._claim_from_row(row, token)
        except InteractiveAdmissionError as exc:
            # Admission is a durable decision.  A current ACL/grant denial
            # must not leave a pending row for a later blind provider retry.
            try:
                if "row" in locals() and row is not None:
                    await self._block_interactive_start(
                        db_session,
                        row,
                        code=exc.code,
                        message=str(exc),
                    )
                else:
                    await self._rollback(db_session)
            except Exception:
                await self._rollback(db_session)
            raise
        except Exception:
            await self._rollback(db_session)
            return None
        finally:
            await self._close(db_session)

    async def _validate_interactive_start_authority(
        self,
        session: Any,
        row: AgentWorkItem,
        *,
        requested_run_id: str | None,
    ) -> None:
        """Revalidate the human/session and Agent scopes before provider start.

        Admission and provider execution are separate transactions.  This
        second check intentionally reconstructs the current authority from
        the durable session/run rows and the exact immutable binding; a grant
        or conversation ACL revoked between those phases therefore fences the
        provider before it receives a request.
        """

        active_run_id = _as_uuid(getattr(row, "active_agent_run_id", None))
        requested_uuid = _as_uuid(requested_run_id)
        if active_run_id is None:
            raise InteractiveAdmissionError(
                "interactive_run_missing",
                "interactive Chat Agent run is missing",
            )
        if requested_uuid is not None and requested_uuid != active_run_id:
            raise InteractiveAdmissionError(
                "interactive_run_conflict",
                "interactive Chat Agent run does not match the Work item",
            )

        locked_run = (
            await session.execute(
                select(AgentRun)
                .where(AgentRun.id == active_run_id)
                .with_for_update()
            )
        ).scalars().first()
        if locked_run is None or str(locked_run.run_type or "") != "chat_turn":
            raise InteractiveAdmissionError(
                "interactive_run_missing",
                "interactive Chat Agent run is unavailable",
            )

        session_id = _as_uuid(getattr(row, "conversation_session_id", None))
        if session_id is None:
            metadata = getattr(row, "metadata_json", None)
            if isinstance(metadata, Mapping):
                session_id = _as_uuid(metadata.get("conversation_session_id"))
        if session_id is None:
            raise InteractiveAdmissionError(
                "interactive_session_missing",
                "interactive Chat session identity is missing",
            )
        conversation = (
            await session.execute(
                select(ConversationSession)
                .where(
                    ConversationSession.id == session_id,
                    ConversationSession.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalars().first()
        if conversation is None:
            raise InteractiveAdmissionError(
                "interactive_session_unavailable",
                "interactive Chat session is unavailable",
            )

        actor_id = _as_uuid(getattr(locked_run, "user_id", None))
        actor = await session.get(User, actor_id) if actor_id is not None else None
        if actor is None or not bool(getattr(actor, "is_active", True)):
            raise InteractiveAdmissionError(
                "human_actor_unavailable",
                "interactive Chat human actor is unavailable",
            )
        owner_id = str(getattr(conversation, "user_id", "") or "")
        actor_is_admin = str(getattr(actor, "role", "") or "").strip().lower() == "admin"
        if owner_id != str(actor_id) and not actor_is_admin:
            participant = (
                await session.execute(
                    select(ConversationParticipant).where(
                        ConversationParticipant.session_id == session_id,
                        ConversationParticipant.participant_type == "user",
                        ConversationParticipant.participant_id == str(actor_id),
                        ConversationParticipant.status == "joined",
                        ConversationParticipant.role.in_(
                            ("owner", "admin", "member")
                        ),
                    )
                )
            ).scalars().first()
            if participant is None:
                raise InteractiveAdmissionError(
                    "conversation_read_denied",
                    "conversation read permission was revoked",
                )

        agent_id = _as_uuid(getattr(row, "assigned_agent_id", None))
        revision_id = _as_uuid(getattr(row, "agent_revision_id", None))
        canonical_agent_id = _as_uuid(getattr(conversation, "agent_id", None))
        if agent_id is None or revision_id is None or canonical_agent_id != agent_id:
            raise InteractiveAdmissionError(
                "agent_binding_changed",
                "conversation Agent binding changed",
            )
        run_agent_id = _as_uuid(getattr(locked_run, "agent_id", None))
        run_revision_id = _as_uuid(getattr(locked_run, "agent_revision_id", None))
        if run_agent_id != agent_id or run_revision_id != revision_id:
            raise InteractiveAdmissionError(
                "agent_binding_changed",
                "interactive run identity no longer matches the Work item",
            )
        agent = await session.get(Agent, agent_id)
        revision = await session.get(AgentRevision, revision_id)
        if (
            agent is None
            or str(getattr(agent, "state", "")) != "active"
            or revision is None
            or _as_uuid(getattr(revision, "agent_id", None)) != agent_id
        ):
            raise InteractiveAdmissionError(
                "agent_authority_denied",
                "conversation Agent authority is no longer active",
            )

        conversation_project_id = _as_uuid(getattr(conversation, "project_id", None))
        work_project_id = _as_uuid(getattr(row, "project_id", None))
        if work_project_id is not None and conversation_project_id != work_project_id:
            raise InteractiveAdmissionError(
                "agent_binding_changed",
                "interactive Work project scope changed",
            )
        project_id = conversation_project_id or work_project_id
        if project_id is not None:
            if not await ProjectRepository.has_permission(
                session,
                project_id=project_id,
                user_id=actor_id,
                permission="write",
            ):
                raise InteractiveAdmissionError(
                    "human_project_write_denied",
                    "human Project write permission was revoked",
                )
            grant = await session.scalar(
                select(AgentProjectGrant).where(
                    AgentProjectGrant.agent_id == agent_id,
                    AgentProjectGrant.project_id == project_id,
                    AgentProjectGrant.state == "active",
                )
            )
            now = self._now()
            permissions = getattr(grant, "permissions", None)
            if (
                grant is None
                or (getattr(grant, "active_from", None) is not None and grant.active_from > now)
                or (getattr(grant, "active_until", None) is not None and grant.active_until < now)
                or not isinstance(permissions, Mapping)
                or permissions.get("read") is not True
            ):
                raise InteractiveAdmissionError(
                    "agent_permission_denied",
                    "current Agent Project grant was revoked",
                )

        run_metadata = getattr(locked_run, "run_metadata", None)
        binding_metadata = (
            run_metadata.get("agent_binding")
            if isinstance(run_metadata, Mapping)
            else None
        )
        binding_id = (
            binding_metadata.get("binding_id")
            if isinstance(binding_metadata, Mapping)
            else None
        )
        expected_hash = (
            binding_metadata.get("snapshot_hash")
            if isinstance(binding_metadata, Mapping)
            else None
        )
        if not binding_id or not expected_hash:
            raise InteractiveAdmissionError(
                "agent_binding_changed",
                "interactive Agent binding snapshot is missing",
            )
        from .conversation_agent_service import ConversationAgentService

        try:
            binding = await ConversationAgentService(
                db_manager=self._db_manager,
                config=self.config,
            ).resolve_execution_binding(
                session_id=session_id,
                agent_id=agent_id,
                agent_revision_id=revision_id,
                binding_id=binding_id,
                db_session=session,
            )
        except Exception as exc:
            raise InteractiveAdmissionError(
                "agent_binding_changed",
                "interactive Agent binding snapshot is unavailable",
            ) from exc
        if str(getattr(binding, "snapshot_hash", "")) != str(expected_hash):
            raise InteractiveAdmissionError(
                "agent_binding_changed",
                "interactive Agent binding snapshot changed",
            )

        # This is a fresh authority read.  Interactive bookkeeping remains
        # usable while the autonomous feature gate is off because no tool
        # capability is requested here; current Agent/Revision/project grant
        # state is still required before a provider receives the turn.
        from .agent_authority import AgentAuthorityResolver

        authority = await AgentAuthorityResolver(
            self._db_manager,
            config=self.config,
        ).resolve(
            agent_id=agent_id,
            revision_id=revision_id,
            project_id=project_id,
        )
        if not authority.allowed:
            reason = authority.reason or "agent_authority_denied"
            raise InteractiveAdmissionError(
                _safe_error(reason, limit=96) or "agent_authority_denied",
                "current Agent authority denied interactive Chat start",
            )

    async def _block_interactive_start(
        self,
        session: Any,
        row: AgentWorkItem,
        *,
        code: str,
        message: str,
    ) -> None:
        """Fence a denied pending item before the transport starts a provider."""

        safe_code = _interactive_admission_safe_code(code)
        safe_notice = f"Interactive Agent admission blocked ({safe_code})"
        active_run_id = _as_uuid(getattr(row, "active_agent_run_id", None))
        if str(getattr(row, "state", "")) == "pending":
            await self._transition_locked(
                session,
                row,
                "blocked",
                token=None,
                error_code=safe_code,
                error_message=message,
            )
        # Keep a queued/running AgentRun terminal with its Work denial in this
        # same transaction.  The normal transport emitter may later repeat
        # ``fail_run``; its terminal CAS preserves this first outcome.
        if active_run_id is not None:
            run_result = await session.execute(
                select(AgentRun)
                .where(AgentRun.id == active_run_id)
                .with_for_update()
            )
            run = run_result.scalars().first()
            if run is not None and str(getattr(run, "status", "")) not in {
                "succeeded",
                "failed",
                "cancelled",
            }:
                now = self._now()
                run.status = "failed"
                # Keep private provider/ACL prose out of the durable status
                # identity.  The typed safe code is the only failure fact
                # consumed by transport/materialization.
                run.error = safe_code
                run.ended_at = run.ended_at or now
                run.updated_at = now
                run.last_event_at = now
                result = dict(run.result or {}) if isinstance(run.result, Mapping) else {}
                result.update(
                    {
                        "source": "interactive_admission",
                        "safe_error_code": safe_code,
                        "provider_started": False,
                        "authority_outcome": "blocked",
                    }
                )
                run.result = result
                metadata = dict(run.run_metadata or {}) if isinstance(run.run_metadata, Mapping) else {}
                metadata["safe_error_code"] = safe_code
                metadata["provider_started"] = False
                marker = metadata.get("agent_dialogue")
                if isinstance(marker, Mapping):
                    marker_copy = dict(marker)
                    marker_copy["safe_error_code"] = safe_code
                    marker_copy["provider_started"] = False
                    metadata["agent_dialogue"] = marker_copy
                run.run_metadata = metadata
                sequence = await session.scalar(
                    select(func.max(AgentRunEvent.sequence)).where(
                        AgentRunEvent.run_id == run.id
                    )
                )
                session.add(
                    AgentRunEvent(
                        id=uuid.uuid4(),
                        run_id=run.id,
                        sequence=int(sequence or 0) + 1,
                        event_type="run.failed",
                        status="failed",
                        message=safe_notice,
                        payload={
                            "source": "interactive_admission",
                            "error_code": safe_code,
                            "safe_error_code": safe_code,
                            "provider_started": False,
                            "authority_outcome": "blocked",
                        },
                        created_at=now,
                    )
                )
        await session.commit()

    async def renew_interactive(self, claim: WorkClaim | Mapping[str, Any]) -> bool:
        normalized = self._coerce_claim(claim)
        if normalized.source_type != "chat_turn" or normalized.execution_adapter != "interactive_chat":
            return False
        if not self.interactive_enabled:
            return False
        session = await self._session()
        try:
            now = self._now()
            result = await session.execute(
                update(AgentWorkItem)
                .where(
                    AgentWorkItem.id == _as_uuid(normalized.work_item_id),
                    AgentWorkItem.state == "running",
                    AgentWorkItem.lease_owner == normalized.lease_owner,
                    AgentWorkItem.lease_token == normalized.lease_token,
                    AgentWorkItem.lease_expires_at > now,
                )
                .values(
                    heartbeat_at=now,
                    lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                    updated_at=now,
                )
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0) == 1
        except Exception:
            await self._rollback(session)
            return False
        finally:
            await self._close(session)

    async def settle_interactive(
        self,
        claim: WorkClaim | Mapping[str, Any],
        outcome: ExecutionOutcome | Mapping[str, Any] | bool,
        *,
        source_ok: bool | None = True,
    ) -> dict[str, Any] | None:
        normalized = self._coerce_claim(claim)
        if normalized.source_type != "chat_turn" or normalized.execution_adapter != "interactive_chat":
            return {"work_item_id": normalized.work_item_id, "settled": False, "reason": "not_interactive"}
        # The final settlement races with the Human-answer/recovery admission
        # that locks Run -> Outbox -> Work.  PostgreSQL resolves that lock
        # inversion by aborting one transaction; the aborted settle rolls back
        # completely and the lease fence keeps a retry idempotent.  Give up
        # after a few attempts and never retry any other error.
        attempts = 4
        for attempt in range(1, attempts + 1):
            try:
                return await self.settle(
                    normalized,
                    outcome,
                    source_ok=source_ok,
                    _interactive=True,
                )
            except Exception as exc:
                if attempt >= attempts or not _is_transaction_deadlock(exc):
                    raise
                logger.warning(
                    "Retrying interactive Work settlement after a database deadlock: work=%s attempt=%s",
                    normalized.work_item_id,
                    attempt,
                )
                await asyncio.sleep(0.1 * attempt)

    async def cancel_interactive(
        self,
        work_item_id: str,
        *,
        claim: WorkClaim | Mapping[str, Any] | None = None,
        reason: str = "cancelled",
        actor: Any = None,
    ) -> bool:
        """Cancel one exact Chat work item through the existing fence."""

        item_uuid = _as_uuid(work_item_id)
        if item_uuid is None or not self.interactive_enabled:
            return False
        lease_token = lease_owner = None
        if claim is not None:
            normalized = self._coerce_claim(claim)
            if (
                normalized.work_item_id != str(item_uuid)
                or normalized.source_type != "chat_turn"
                or normalized.execution_adapter != "interactive_chat"
            ):
                return False
            lease_token = normalized.lease_token
            lease_owner = normalized.lease_owner
        session = await self._session()
        try:
            row = await session.get(AgentWorkItem, item_uuid)
            if row is None or (
                str(row.source_type or "") != "chat_turn"
                or str(row.execution_adapter or "") != "interactive_chat"
            ):
                return False
        finally:
            await self._close(session)
        return await self.cancel(
            str(item_uuid),
            reason=reason,
            actor=actor,
            lease_token=lease_token,
            lease_owner=lease_owner,
        )

    # Explicit aliases make the trusted lane easy to discover for transports
    # while preserving one implementation and one fence.
    admit_interactive_work = admit_interactive
    start_interactive_work = start_interactive
    renew_interactive_work = renew_interactive
    settle_interactive_work = settle_interactive
    cancel_interactive_work = cancel_interactive

    async def _active_count(self, session: Any) -> int:
        result = await session.execute(select(func.count(AgentWorkItem.id)).where(AgentWorkItem.state.in_(tuple(WORK_ACTIVE_STATES))))
        value = result.scalar_one() if hasattr(result, "scalar_one") else result.scalar()
        return int(value or 0)

    @staticmethod
    def _policy_limit(policy: Any, key: str) -> int | None:
        if not isinstance(policy, Mapping):
            return None
        raw = policy.get(key)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    async def _agent_capacity_available(
        self,
        session: Any,
        row: AgentWorkItem,
    ) -> bool:
        """Check the pinned revision's concurrency policy under an Agent fence.

        The global coordinator slot is deliberately separate from this gate:
        two sessions of one Agent may run concurrently up to the pinned limit,
        while unrelated Agents continue to use their own capacity.
        """

        agent_id = getattr(row, "assigned_agent_id", None)
        if agent_id is None:
            return True
        await self._lock_concurrency_key(session, f"agent:{agent_id}:capacity")
        try:
            await session.execute(
                select(Agent.id).where(Agent.id == agent_id).with_for_update()
            )
        except Exception:
            # SQLite/fake sessions do not provide row locks; the advisory/CAS
            # path below remains the serialization boundary there.
            pass
        revision_id = getattr(row, "agent_revision_id", None)
        revision = await session.get(AgentRevision, revision_id) if revision_id else None
        policy = getattr(revision, "concurrency_policy_json", None) if revision is not None else None
        max_runs = self._policy_limit(policy, "max_parallel_runs")
        max_tasks = self._policy_limit(policy, "max_parallel_tasks")
        if max_runs is None and max_tasks is None:
            return True
        total_result = await session.execute(
            select(func.count(AgentWorkItem.id)).where(
                AgentWorkItem.assigned_agent_id == agent_id,
                AgentWorkItem.state.in_(tuple(WORK_ACTIVE_STATES)),
            )
        )
        total = total_result.scalar_one() if hasattr(total_result, "scalar_one") else total_result.scalar()
        if max_runs is not None and int(total or 0) >= max_runs:
            return False
        task_id = getattr(row, "task_id", None)
        if max_tasks is not None and task_id is not None:
            task_result = await session.execute(
                select(func.count(AgentWorkItem.id)).where(
                    AgentWorkItem.assigned_agent_id == agent_id,
                    AgentWorkItem.task_id == task_id,
                    AgentWorkItem.state.in_(tuple(WORK_ACTIVE_STATES)),
                )
            )
            tasks = task_result.scalar_one() if hasattr(task_result, "scalar_one") else task_result.scalar()
            if int(tasks or 0) >= max_tasks:
                return False
        return True

    async def _interactive_shared_resource_available(
        self,
        session: Any,
        row: AgentWorkItem,
    ) -> bool:
        """Fence a real Task/workspace source key across Chat sessions."""

        key = _string(getattr(row, "concurrency_key", None), limit=255)
        if not key or key.startswith("conversation:"):
            return True
        await self._lock_concurrency_key(session, key)
        result = await session.execute(
            select(func.count(AgentWorkItem.id)).where(
                AgentWorkItem.id != row.id,
                AgentWorkItem.state.in_(
                    ("pending", "claimed", "running", "awaiting_approval", "uncertain")
                ),
                AgentWorkItem.concurrency_key == key,
            )
        )
        value = result.scalar_one() if hasattr(result, "scalar_one") else result.scalar()
        return int(value or 0) == 0

    async def _interactive_session_available(
        self,
        session: Any,
        row: AgentWorkItem,
    ) -> bool:
        session_id = getattr(row, "conversation_session_id", None)
        if session_id is None:
            metadata = getattr(row, "metadata_json", None)
            session_id = metadata.get("conversation_session_id") if isinstance(metadata, Mapping) else None
        if session_id is None:
            return True
        # The session key is normally sufficient.  Keep an explicit predicate
        # for rows materialized during rolling deployment before the FK/index
        # was present.
        # CLI turns used workspace:<hash> keys before moving to
        # conversation:<session_id>; the FK predicate keeps one active turn per
        # session while old and new keys coexist.
        if (
            getattr(row, "source_type", None) not in {"chat_turn", "cli_session_turn"}
            and getattr(row, "execution_adapter", None) not in {"interactive_chat", "cli_session_turn"}
        ):
            return True
        # DB settlement (cancel/lost lease) can precede adapter cleanup; the
        # coordinator still owns that session until the local claim drains.
        wanted = str(_as_uuid(session_id))
        for claim in self._local_active_claims().values():
            if claim.work_item_id != str(row.id) and claim.conversation_session_id and str(_as_uuid(claim.conversation_session_id)) == wanted:
                return False
        conditions = [
            AgentWorkItem.state.in_(tuple(WORK_ACTIVE_STATES)),
            AgentWorkItem.id != row.id,
        ]
        if hasattr(AgentWorkItem, "conversation_session_id"):
            conditions.append(AgentWorkItem.conversation_session_id == _as_uuid(session_id))
        else:
            conditions.append(AgentWorkItem.concurrency_key == f"conversation:{session_id}")
        result = await session.execute(select(func.count(AgentWorkItem.id)).where(*conditions))
        value = result.scalar_one() if hasattr(result, "scalar_one") else result.scalar()
        return int(value or 0) == 0

    async def _restart_uncertain_work_is_releasable(
        self,
        session: Any,
        work: AgentWorkItem,
        *,
        session_id: uuid.UUID,
        user_id: str,
        agent_id: uuid.UUID,
        agent_revision_id: uuid.UUID,
    ) -> AgentWorkItem | None:
        """Prove one restart-fenced Work row is historical before admission.

        The restart reconciler locks Run -> Outbox -> Work. Admission follows
        that same order for uncertain candidates; the initial Work query is a
        read-only candidate scan so it cannot form a lock inversion.
        """

        run_id = _as_uuid(getattr(work, "source_id", None))
        if run_id is None:
            return None
        run_result = await session.execute(
            select(AgentRun)
            .where(AgentRun.id == run_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        run = run_result.scalars().first()
        if run is None:
            return None
        outbox_result = await session.execute(
            select(ConversationDispatchOutbox)
            .where(ConversationDispatchOutbox.run_id == run.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        outbox = outbox_result.scalars().first()
        work_result = await session.execute(
            select(AgentWorkItem)
            .where(AgentWorkItem.id == work.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        locked_work = work_result.scalars().first()
        if locked_work is None:
            return None
        work = locked_work
        if (
            str(getattr(work, "state", "") or "").casefold() != "uncertain"
            or str(getattr(work, "outcome_classification", "") or "").casefold()
            != "uncertain"
            or str(getattr(work, "source_type", "") or "").casefold() != "chat_turn"
            or str(getattr(work, "intent_key", "") or "").casefold()
            != "interactive_chat"
            or str(getattr(work, "execution_adapter", "") or "").casefold()
            != "interactive_chat"
            or getattr(work, "completed_at", None) is None
            or str(getattr(work, "safe_error_code", "") or "").casefold()
            != "agent_run_restart_uncertain"
            or getattr(work, "active_agent_run_id", None) is not None
            or getattr(work, "lease_owner", None) is not None
            or getattr(work, "lease_token", None) is not None
            or getattr(work, "lease_expires_at", None) is not None
        ):
            return None
        if (
            str(getattr(run, "status", "") or "").casefold() != "failed"
            or getattr(run, "ended_at", None) is None
            or str(getattr(run, "run_type", "") or "").casefold() != "chat_turn"
            or _as_uuid(getattr(run, "session_id", None)) != session_id
            or _as_uuid(getattr(work, "conversation_session_id", None))
            != session_id
            or str(getattr(run, "work_item_id", "") or "") != str(work.id)
            or str(getattr(work, "source_id", "") or "") != str(run.id)
            or str(getattr(work, "source_revision", "") or "")
            != str(getattr(run, "agent_revision_id", "") or "")
            or _as_uuid(getattr(work, "assigned_agent_id", None))
            != _as_uuid(getattr(run, "agent_id", None))
            or _as_uuid(getattr(work, "agent_revision_id", None))
            != _as_uuid(getattr(run, "agent_revision_id", None))
            or _as_uuid(getattr(run, "agent_id", None)) != agent_id
            or _as_uuid(getattr(run, "agent_revision_id", None))
            != agent_revision_id
        ):
            return None
        if user_id and str(getattr(run, "user_id", "") or "") != user_id:
            return None
        result = run.result if isinstance(run.result, Mapping) else {}
        metadata = run.run_metadata if isinstance(run.run_metadata, Mapping) else {}
        if (
            str(result.get("safe_error_code") or "").casefold()
            != "agent_run_restart_uncertain"
            or result.get("reconciled_after_restart") is not True
            or metadata.get("reconciled_after_restart") is not True
            or str(metadata.get("restart_recovery_state") or "").casefold()
            != "uncertain"
        ):
            return None
        if outbox is None:
            return work
        if (
            _as_uuid(getattr(outbox, "session_id", None)) != session_id
            or str(getattr(outbox, "user_id", "") or "") != user_id
        ):
            return None
        payload = outbox.payload if isinstance(outbox.payload, Mapping) else {}
        payload_run_id = payload.get("agent_run_id")
        if payload_run_id not in (None, "") and _as_uuid(payload_run_id) != run.id:
            return None
        if str(getattr(outbox, "status", "") or "").casefold() not in {
            "delivered",
            "cancelled",
            "deadletter",
        }:
            return None
        return work

    def _reserve_budget(self, budget: dict[str, Any]) -> dict[str, Any] | None:
        """Atomically advance a bounded per-item reservation snapshot."""

        amount = _number(
            budget.get("reservation_units", budget.get("units", budget.get("amount", 1)))
        )
        if amount <= 0:
            amount = 1.0
        remaining_key = "remaining" if "remaining" in budget else "remaining_units" if "remaining_units" in budget else None
        if remaining_key is not None:
            remaining = _number(budget.get(remaining_key))
            if remaining < amount:
                return None
            budget[remaining_key] = remaining - amount
        if "limit" in budget:
            limit = _number(budget.get("limit"))
            reserved = _number(budget.get("reserved", 0))
            consumed = _number(budget.get("consumed", budget.get("used", 0)))
            if limit > 0 and reserved + consumed + amount > limit:
                return None
        budget["reserved"] = _number(budget.get("reserved", 0)) + amount
        budget["reservation_count"] = int(_number(budget.get("reservation_count", 0))) + 1
        budget["reserved_by"] = self.worker_id
        budget["reservation_amount"] = amount
        return budget

    def _settle_budget(
        self,
        budget: dict[str, Any],
        outcome: ExecutionOutcome,
        *,
        state: str,
    ) -> dict[str, Any]:
        """Settle/release the claim reservation and retain safe usage."""

        amount = _number(budget.get("reservation_amount", 0))
        if state == "awaiting_approval":
            budget["awaiting_approval"] = True
            budget["settled_state"] = state
            return budget
        if state in {"retry_wait", "failed", "blocked", "cancelled", "dead_letter"} and amount > 0:
            if "remaining" in budget:
                budget["remaining"] = _number(budget.get("remaining")) + amount
            elif "remaining_units" in budget:
                budget["remaining_units"] = _number(budget.get("remaining_units")) + amount
            budget["reserved"] = max(0.0, _number(budget.get("reserved", 0)) - amount)
        elif state == "succeeded" and amount > 0:
            # A successful claim consumes its reservation exactly once. Keep
            # cumulative consumption for total-budget ceilings while clearing
            # the active reservation shown to operators.
            budget["reserved"] = max(0.0, _number(budget.get("reserved", 0)) - amount)
            budget["consumed"] = _number(
                budget.get("consumed", budget.get("used", 0))
            ) + amount
        usage = _safe_json(outcome.usage or {})
        if usage:
            budget["last_usage"] = usage
            if isinstance(usage, Mapping):
                budget["used"] = _number(budget.get("used", 0)) + _number(
                    usage.get("units", usage.get("total_tokens", 0))
                )
        budget["settled"] = True
        budget["settled_state"] = state
        budget.pop("reservation_amount", None)
        budget.pop("reserved_by", None)
        return budget

    async def _lock_concurrency_key(self, session: Any, key: str) -> None:
        """Serialize same-key claims on PostgreSQL; SQLite uses its writer lock."""

        if not key:
            return
        bind = getattr(session, "bind", None)
        if bind is None and callable(getattr(session, "get_bind", None)):
            try:
                bind = session.get_bind()
            except Exception:
                bind = None
        dialect = str(getattr(getattr(bind, "dialect", None), "name", "")).lower()
        if dialect != "postgresql":
            return
        try:
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
        except Exception:
            # If advisory locks are unavailable, fail closed for this key
            # rather than pretending a concurrency ceiling was enforced.
            raise RuntimeError("concurrency reservation is unavailable")

    @staticmethod
    def _claim_from_row(row: AgentWorkItem, token: str) -> WorkClaim:
        return WorkClaim(
            work_item_id=str(row.id),
            lease_token=token,
            lease_owner=str(row.lease_owner or ""),
            attempt=int(row.attempt_count or 0),
            source_type=str(row.source_type),
            source_id=str(row.source_id),
            source_revision=str(row.source_revision),
            intent_key=str(row.intent_key),
            domain=str(row.domain),
            assigned_agent_id=str(row.assigned_agent_id) if row.assigned_agent_id else None,
            agent_revision_id=str(getattr(row, "agent_revision_id", None)) if getattr(row, "agent_revision_id", None) else None,
            execution_adapter=str(row.execution_adapter or "default"),
            required_capabilities=tuple(str(item) for item in (getattr(row, "required_capabilities_json", None) or []) if str(item).strip()),
            project_id=str(row.project_id) if row.project_id else None,
            task_id=str(row.task_id) if row.task_id else None,
            persona_id=str(row.persona_id) if row.persona_id else None,
            conversation_session_id=(
                str(getattr(row, "conversation_session_id"))
                if getattr(row, "conversation_session_id", None)
                else None
            ),
            concurrency_key=str(row.concurrency_key) if row.concurrency_key else None,
            budget_reservation=getattr(row, "budget_reservation_json", None) or {},
            space_id=str(row.space_id) if row.space_id else None,
            app_id=str(row.app_id) if row.app_id else None,
            causal_depth=int(getattr(row, "causal_depth", 0) or 0),
            mutation_fingerprint=getattr(row, "mutation_fingerprint", None),
            metadata=getattr(row, "metadata_json", None) or {},
        )

    async def start_claim(self, claim: WorkClaim) -> bool:
        """Move a claimed item to running, fenced by its lease token."""

        if not self._runtime_enabled():
            return False
        session = await self._session()
        try:
            now = self._now()
            row = await session.get(AgentWorkItem, _as_uuid(claim.work_item_id))
            previous_state = str(getattr(row, "state", "claimed") or "claimed") if row is not None else "claimed"
            result = await session.execute(
                update(AgentWorkItem)
                .where(
                    AgentWorkItem.id == _as_uuid(claim.work_item_id),
                    AgentWorkItem.state == "claimed",
                    AgentWorkItem.lease_owner == claim.lease_owner,
                    AgentWorkItem.lease_token == claim.lease_token,
                    AgentWorkItem.lease_expires_at > now,
                )
                .values(state="running", started_at=now, heartbeat_at=now, updated_at=now)
            )
            ok = int(getattr(result, "rowcount", 0) or 0) == 1
            if ok:
                if row is not None:
                    await self._append_event_in_session(session, row, event_type="work.running", actor_kind="service", actor_id=self.worker_id, from_state=previous_state, to_state="running", payload={"attempt": claim.attempt})
            await session.commit()
            return ok
        except Exception:
            await self._rollback(session)
            return False
        finally:
            await self._close(session)

    async def renew_lease(self, claim: WorkClaim | Mapping[str, Any]) -> bool:
        if not self._runtime_enabled():
            return False
        normalized = self._coerce_claim(claim)
        session = await self._session()
        try:
            now = self._now()
            result = await session.execute(
                update(AgentWorkItem)
                .where(
                    AgentWorkItem.id == _as_uuid(normalized.work_item_id),
                    AgentWorkItem.state.in_(tuple(WORK_ACTIVE_STATES)),
                    AgentWorkItem.lease_owner == normalized.lease_owner,
                    AgentWorkItem.lease_token == normalized.lease_token,
                    AgentWorkItem.lease_expires_at > now,
                )
                .values(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=self.lease_seconds), updated_at=now)
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0) == 1
        except Exception:
            await self._rollback(session)
            return False
        finally:
            await self._close(session)

    async def settle(
        self,
        claim: WorkClaim | Mapping[str, Any],
        outcome: ExecutionOutcome | Mapping[str, Any] | bool,
        *,
        source_ok: bool | None = None,
        _interactive: bool = False,
    ) -> dict[str, Any] | None:
        """Settle a claim with a token-fenced transition and append evidence."""

        normalized = self._coerce_claim(claim)
        result = ExecutionOutcome.from_value(outcome)
        classification = result.normalized_classification()
        if not self._runtime_enabled() and not (_interactive and self.interactive_enabled):
            return {
                "work_item_id": normalized.work_item_id,
                "settled": False,
                "reason": "runtime_disabled",
            }
        session = await self._session()
        try:
            if source_ok is None:
                source_ok = await self._refresh_source(session, normalized)
            if source_ok is False and classification != "uncertain":
                classification = "blocked"
                result.error_code = result.error_code or "source_changed"
                result.error_message = result.error_message or "source state no longer permits settlement"
            if classification in {"succeeded", "awaiting_approval"} and normalized.assigned_agent_id:
                authority_ok, authority_reason = await self._authority_allows(normalized)
                if not authority_ok:
                    classification = "blocked"
                    result.error_code = authority_reason or "authority_revoked"
                    result.error_message = "Agent authority was revoked before settlement"
            try:
                row_result = await session.execute(
                    select(AgentWorkItem)
                    .where(AgentWorkItem.id == _as_uuid(normalized.work_item_id))
                    .with_for_update()
                )
                row = row_result.scalars().first()
            except Exception:
                row = await session.get(AgentWorkItem, _as_uuid(normalized.work_item_id))
            if row is None:
                await session.rollback()
                return None
            now = self._now()
            # Token/owner/state fencing is checked before every important
            # mutation, including after the adapter's long-running work.
            previous_state = str(row.state or "")
            active_run_id = getattr(row, "active_agent_run_id", None)
            if (
                str(row.lease_token or "") != normalized.lease_token
                or str(row.lease_owner or "") != normalized.lease_owner
                or row.state not in WORK_ACTIVE_STATES
                or row.lease_expires_at is None
                or row.lease_expires_at <= now
            ):
                await session.rollback()
                return {"work_item_id": normalized.work_item_id, "settled": False, "reason": "lease_lost"}
            if classification == "succeeded":
                state = "succeeded"
            elif classification == "transient":
                state = "retry_wait" if int(row.attempt_count or 0) < int(row.max_attempts or self.max_attempts) else "dead_letter"
            elif classification == "permanent":
                state = "failed"
            elif classification == "blocked":
                state = "blocked"
            elif classification == "awaiting_approval":
                state = "awaiting_approval"
            elif classification == "uncertain":
                state = "uncertain"
            else:
                state = "cancelled"
            if state == "retry_wait":
                delay = self._retry_delay(normalized, result)
                row.not_before = now + timedelta(seconds=delay)
                row.next_attempt_at = row.not_before
            else:
                row.not_before = None
                row.next_attempt_at = None
            row.state = state
            row.outcome_classification = classification
            row.lease_owner = None
            row.lease_token = None
            row.lease_expires_at = None
            if state != "running":
                row.active_agent_run_id = None
            row.heartbeat_at = now
            row.updated_at = now
            row.safe_error_code = _safe_error(result.error_code)
            row.blocker_code = _safe_error(result.error_code) if state == "blocked" else None
            row.escalation_reason = _safe_error(result.error_message) if state in {"blocked", "dead_letter", "uncertain"} else None
            row.result_summary = _safe_error(result.result_summary, limit=2000)
            row.result_summary_json = _safe_json(result.result or {})
            budget = row.budget_reservation_json if isinstance(row.budget_reservation_json, Mapping) else {}
            if budget:
                row.budget_reservation_json = self._settle_budget(
                    dict(budget), result, state=state
                )
            # Preserve only bounded domain references needed to resume an
            # approval/reconciliation flow.  In particular, a publication
            # adapter's ExternalAction id must survive the awaiting-approval
            # transition so a later human approval can execute that exact
            # action rather than proposing a duplicate.
            if result.domain_ref or result.domain_status:
                metadata = (
                    dict(row.metadata_json)
                    if isinstance(row.metadata_json, Mapping)
                    else {}
                )
                if result.domain_ref:
                    safe_ref = _safe_error(result.domain_ref, limit=160)
                    if safe_ref:
                        metadata["domain_ref"] = safe_ref
                        if state == "awaiting_approval":
                            metadata["external_action_id"] = safe_ref
                            metadata["action_id"] = safe_ref
                if result.domain_status:
                    safe_status = _safe_error(result.domain_status, limit=64)
                    if safe_status:
                        metadata["domain_status"] = safe_status
                row.metadata_json = _safe_json(metadata or {})
            if state in WORK_TERMINAL_STATES or state in {"blocked", "uncertain", "awaiting_approval"}:
                row.completed_at = now
            if state == "cancelled":
                row.cancelled_at = now
            # Re-check the token/owner/state/expiry in the database at the
            # mutation boundary.  A SELECT ... FOR UPDATE check alone can
            # become stale if this transaction waited for a lock or the
            # adapter's lease expired just before flush.
            fence_values = {
                "state": row.state,
                "outcome_classification": row.outcome_classification,
                "lease_owner": row.lease_owner,
                "lease_token": row.lease_token,
                "lease_expires_at": row.lease_expires_at,
                "heartbeat_at": row.heartbeat_at,
                "updated_at": row.updated_at,
                "not_before": row.not_before,
                "next_attempt_at": row.next_attempt_at,
                "safe_error_code": row.safe_error_code,
                "blocker_code": row.blocker_code,
                "escalation_reason": row.escalation_reason,
                "result_summary": row.result_summary,
                "result_summary_json": row.result_summary_json,
                "budget_reservation_json": row.budget_reservation_json,
                "metadata_json": row.metadata_json,
                "completed_at": row.completed_at,
                "cancelled_at": row.cancelled_at,
                "active_agent_run_id": row.active_agent_run_id,
            }
            no_autoflush = getattr(session, "no_autoflush", None)
            if no_autoflush is None:
                fence_result = await session.execute(
                    update(AgentWorkItem)
                    .where(
                        AgentWorkItem.id == _as_uuid(normalized.work_item_id),
                        AgentWorkItem.state.in_(tuple(WORK_ACTIVE_STATES)),
                        AgentWorkItem.lease_owner == normalized.lease_owner,
                        AgentWorkItem.lease_token == normalized.lease_token,
                        AgentWorkItem.lease_expires_at > now,
                    )
                    .values(**fence_values)
                )
            else:
                with no_autoflush:
                    fence_result = await session.execute(
                        update(AgentWorkItem)
                        .where(
                            AgentWorkItem.id == _as_uuid(normalized.work_item_id),
                            AgentWorkItem.state.in_(tuple(WORK_ACTIVE_STATES)),
                            AgentWorkItem.lease_owner == normalized.lease_owner,
                            AgentWorkItem.lease_token == normalized.lease_token,
                            AgentWorkItem.lease_expires_at > now,
                        )
                        .values(**fence_values)
                    )
            if int(getattr(fence_result, "rowcount", 0) or 0) != 1:
                await session.rollback()
                return {
                    "work_item_id": normalized.work_item_id,
                    "settled": False,
                    "reason": "lease_lost",
                }
            await self._append_event_in_session(
                session,
                row,
                event_type=f"work.{state}",
                actor_kind="service",
                actor_id=self.worker_id,
                agent_run_id=active_run_id,
                from_state=previous_state,
                to_state=state,
                causation_id=row.causation_id,
                causal_depth=int(row.causal_depth or 0),
                mutation_fingerprint=None,
                result_summary=result.result_summary,
                safe_error_code=result.error_code,
                payload={
                    "classification": classification,
                    "error_code": _safe_error(result.error_code, limit=96),
                    "result_summary": _safe_error(result.result_summary, limit=500),
                    "domain_ref": _safe_error(result.domain_ref, limit=160),
                    "domain_status": _safe_error(result.domain_status, limit=64),
                    "evidence_count": min(len(result.evidence or ()), 32),
                    "evidence": _safe_json(list(result.evidence or ())[:16]),
                    "usage": safe_usage_snapshot(result.usage),
                    **_safe_json(terminal_failure_event_fields(result.metadata)),
                    "attempt": int(row.attempt_count or 0),
                },
            )
            await session.commit()
            return row.to_safe_dict()
        except Exception:
            await self._rollback(session)
            raise
        finally:
            await self._close(session)

    async def cancel(
        self,
        work_item_id: str,
        *,
        reason: str = "cancelled",
        actor: Any = None,
        lease_token: str | None = None,
        lease_owner: str | None = None,
    ) -> bool:
        session = await self._session()
        try:
            locked = await session.execute(
                select(AgentWorkItem)
                .where(AgentWorkItem.id == _as_uuid(work_item_id))
                .with_for_update(nowait=True)
            )
            row = locked.scalars().first()
            if row is None:
                await session.rollback()
                return False
            if row.state == "cancelled":
                resignal_run_id = None
                if (
                    str(row.source_type or "") == "chat_turn"
                    and str(row.execution_adapter or "") == "interactive_chat"
                ):
                    if not await self._interactive_cancel_graph_settled_in_session(session, row):
                        await session.rollback()
                        return False
                    run_id = _as_uuid(row.source_id)
                    if run_id is not None:
                        settled_run = (
                            await session.execute(
                                select(AgentRun)
                                .where(AgentRun.id == run_id)
                                .with_for_update(nowait=True)
                            )
                        ).scalars().first()
                        if settled_run is not None and str(settled_run.status or "").casefold() == "cancelled":
                            resignal_run_id = str(settled_run.id)
                await session.commit()
                if resignal_run_id is not None:
                    try:
                        from ..llm.generation_cancellation import request_generation_cancellation

                        request_generation_cancellation(resignal_run_id)
                    except Exception:
                        logger.warning("Provider stop resignal failed after durable cancellation", exc_info=True)
                return True
            if row.state in WORK_TERMINAL_STATES:
                await session.rollback()
                return False
            if lease_token is not None and (
                str(row.lease_token or "") != str(lease_token)
                or (lease_owner is not None and str(row.lease_owner or "") != str(lease_owner))
            ):
                await session.rollback()
                return False
            actor_kind_marker = (
                actor.get("actor_type") or actor.get("kind") or actor.get("principal_kind")
                if isinstance(actor, Mapping)
                else getattr(actor, "actor_type", None)
                or getattr(actor, "kind", None)
                or getattr(actor, "principal_kind", None)
            )
            actor_raw_id = (
                actor.get("id") or actor.get("user_id")
                if isinstance(actor, Mapping)
                else getattr(actor, "id", None)
                or getattr(actor, "user_id", None)
            )
            actor_uuid = _as_uuid(actor_raw_id)
            if actor_uuid is not None and await session.get(Agent, actor_uuid) is not None:
                await session.rollback()
                return False
            if bool(
                actor.get("is_agent")
                if isinstance(actor, Mapping)
                else getattr(actor, "is_agent", False)
            ) or str(actor_kind_marker or "").strip().casefold() == "agent":
                await session.rollback()
                return False
            actor_kind = "human" if actor_uuid is not None else "service"
            actor_id = str(actor_uuid) if actor_uuid is not None else self.worker_id
            # API Agent task cancellation is cooperative: the API provider and
            # any active tool call must unwind before Work/Run can be marked
            # stopped. Persist the request, signal the exact run after commit,
            # and let the adapter's normal token-fenced settlement win.
            if (
                str(row.execution_adapter or "") == "api_agent_task"
                and str(row.state or "") in {"claimed", "running"}
            ):
                task_metadata = (
                    dict(row.metadata_json)
                    if isinstance(row.metadata_json, Mapping)
                    else {}
                )
                event_written = task_metadata.get("cancellation_event_written") is True
                task_metadata["cancellation_requested"] = True
                row.metadata_json = _safe_json(task_metadata)
                row.updated_at = self._now()
                active_run_id = str(row.active_agent_run_id or "").strip()
                if not event_written:
                    task_metadata["cancellation_event_written"] = True
                    row.metadata_json = _safe_json(task_metadata)
                    await self._append_event_in_session(
                        session,
                        row,
                        event_type="work.cancel_requested",
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        from_state=str(row.state or "running"),
                        to_state=str(row.state or "running"),
                        payload={"cancellation_requested": True},
                    )
                await session.commit()
                if active_run_id:
                    try:
                        from ..llm.generation_cancellation import request_generation_cancellation

                        request_generation_cancellation(active_run_id)
                    except Exception:
                        logger.warning(
                            "API Agent task provider stop signal failed",
                            exc_info=True,
                        )
                return True
            interactive_snapshot = None
            interactive_hook_run_id = None
            if (
                str(row.source_type or "") == "chat_turn"
                and str(row.execution_adapter or "") == "interactive_chat"
            ):
                interactive_snapshot = {
                    "state": row.state,
                    "updated_at": row.updated_at,
                    "active_agent_run_id": row.active_agent_run_id,
                    "lease_owner": row.lease_owner,
                    "lease_token": row.lease_token,
                    "lease_expires_at": row.lease_expires_at,
                }
                interactive_result = await self._cancel_interactive_work_in_session(
                    session,
                    row,
                    reason=reason,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                )
                if not interactive_result:
                    await self._rollback(session)
                    return False
                if isinstance(interactive_result, str):
                    interactive_hook_run_id = interactive_result
            previous = str(row.state or "")
            row.state = "cancelled"
            row.outcome_classification = "cancelled"
            row.cancelled_at = self._now()
            row.lease_owner = None
            row.lease_token = None
            row.lease_expires_at = None
            row.updated_at = self._now()
            row.safe_error_code = "cancelled"
            if (
                str(row.source_type or "") == "chat_turn"
                and str(row.execution_adapter or "") == "interactive_chat"
            ):
                row.active_agent_run_id = None
            if isinstance(row.budget_reservation_json, Mapping) and row.budget_reservation_json:
                row.budget_reservation_json = self._settle_budget(
                    dict(row.budget_reservation_json),
                    ExecutionOutcome(classification="cancelled"),
                    state="cancelled",
                )
            if interactive_snapshot is not None:
                conditions = [
                    AgentWorkItem.id == row.id,
                    AgentWorkItem.state == interactive_snapshot["state"],
                    AgentWorkItem.updated_at == interactive_snapshot["updated_at"],
                ]
                for field in ("active_agent_run_id", "lease_owner", "lease_token", "lease_expires_at"):
                    value = interactive_snapshot[field]
                    column = getattr(AgentWorkItem, field)
                    conditions.append(column.is_(None) if value is None else column == value)
                with session.no_autoflush:
                    cas = await session.execute(
                        update(AgentWorkItem)
                        .where(*conditions)
                        .values(
                            state=row.state,
                            outcome_classification=row.outcome_classification,
                            cancelled_at=row.cancelled_at,
                            lease_owner=row.lease_owner,
                            lease_token=row.lease_token,
                            lease_expires_at=row.lease_expires_at,
                            updated_at=row.updated_at,
                            safe_error_code=row.safe_error_code,
                            active_agent_run_id=row.active_agent_run_id,
                            budget_reservation_json=row.budget_reservation_json,
                        )
                        .execution_options(synchronize_session=False)
                    )
                if int(getattr(cas, "rowcount", 0) or 0) != 1:
                    await self._rollback(session)
                    return False
            await self._append_event_in_session(session, row, event_type="work.cancelled", actor_kind=actor_kind, actor_id=actor_id, from_state=previous, to_state="cancelled", payload={"reason": _safe_error(reason, limit=200)})
            await session.commit()
            if interactive_hook_run_id is not None:
                try:
                    from ..llm.generation_cancellation import request_generation_cancellation

                    request_generation_cancellation(interactive_hook_run_id)
                except Exception:
                    logger.warning("Provider stop failed after durable cancellation", exc_info=True)
            return True
        except Exception:
            await self._rollback(session)
            return False
        finally:
            await self._close(session)

    @staticmethod
    def _interactive_dialogue_marker(run: AgentRun) -> Mapping[str, Any] | None:
        metadata = run.run_metadata if isinstance(run.run_metadata, Mapping) else {}
        marker = metadata.get("agent_dialogue")
        return marker if isinstance(marker, Mapping) else None

    @staticmethod
    async def _lock_source_dialogue_deliveries_in_session(
        session: Any,
        work: AgentWorkItem,
    ) -> list[Any]:
        result = await session.execute(
            select(AgentDialogueDelivery)
            .where(AgentDialogueDelivery.source_work_item_id == work.id)
            .order_by(AgentDialogueDelivery.sequence, AgentDialogueDelivery.id)
            .with_for_update(nowait=True)
        )
        return list(result.scalars().all())

    async def _release_reserved_dialogue_if_never_started(
        self,
        session: Any,
        *,
        run: AgentRun,
        work: AgentWorkItem,
        outbox: Any | None,
        reservation: Any,
        allow_terminal: bool = False,
        release: bool = True,
    ) -> bool:
        """Use the canonical budget release only for a proven zero-start Run."""

        marker = self._interactive_dialogue_marker(run)
        dialogue_id = _as_uuid(marker.get("dialogue_id")) if marker else None
        role = str(marker.get("role") or "").strip().casefold() if marker else ""
        if dialogue_id is None or role not in {"root", "worker"}:
            raise RuntimeError("interactive_cancel_reservation_binding_invalid")
        if (
            reservation.dialogue_id != dialogue_id
            or reservation.run_id != run.id
            or str(reservation.reservation_key or "") != f"run:{run.id}"
            or int(reservation.charged_tokens or 0) != 0
            or int(reservation.charged_cost_micros or 0) != 0
        ):
            raise RuntimeError("interactive_cancel_reservation_binding_invalid")
        dialogue = (
            await session.execute(
                select(AgentDialogue)
                .where(AgentDialogue.id == dialogue_id)
                .with_for_update(nowait=True)
            )
        ).scalars().first()
        if dialogue is None:
            raise RuntimeError("interactive_cancel_dialogue_missing")
        expected_session = dialogue.root_session_id if role == "root" else dialogue.worker_session_id
        expected_agent = dialogue.root_agent_id if role == "root" else dialogue.worker_agent_id
        expected_revision = (
            dialogue.root_agent_revision_id
            if role == "root"
            else dialogue.worker_agent_revision_id
        )
        if (
            run.session_id != expected_session
            or run.agent_id != expected_agent
            or run.agent_revision_id != expected_revision
            or str(run.user_id or "") != str(dialogue.owner_user_id or "")
            or work.conversation_session_id != expected_session
            or work.assigned_agent_id != expected_agent
            or work.agent_revision_id != expected_revision
            or str(work.source_type or "") != "chat_turn"
            or str(work.execution_adapter or "") != "interactive_chat"
            or str(work.intent_key or "") != "interactive_chat"
            or str(work.source_id or "") != str(run.id)
            or str(work.source_revision or "") != str(run.agent_revision_id or "")
            or int(work.attempt_count or 0) != 0
            or str(work.state or "").casefold()
            not in ({"pending", "blocked", "failed", "cancelled"} if allow_terminal else {"pending"})
            or work.started_at is not None
            or work.active_agent_run_id not in (None, run.id)
            or work.lease_owner is not None
            or work.lease_token is not None
            or work.lease_expires_at is not None
            or str(run.status or "").casefold()
            not in ({"queued", "failed"} if allow_terminal else {"queued"})
            or run.started_at is not None
        ):
            raise RuntimeError("interactive_cancel_never_started_proof_invalid")
        if outbox is not None:
            outbox_status = str(outbox.status or "").casefold()
            if outbox_status not in {"pending", "cancelled", "deadletter"}:
                raise RuntimeError("interactive_cancel_never_started_outbox_invalid")
            if outbox_status == "pending" and int(outbox.attempts or 0) != 0:
                raise RuntimeError("interactive_cancel_never_started_outbox_attempted")
        evidence_values: list[Any] = []
        for value in (
            run.result,
            run.run_metadata,
            marker,
            outbox.payload if outbox is not None else None,
        ):
            if not isinstance(value, Mapping):
                continue
            if isinstance(value.get("usage"), Mapping):
                raise RuntimeError("interactive_cancel_provider_usage_present")
            if "provider_started" in value:
                evidence_values.append(value.get("provider_started"))
            nested = value.get("agent_dialogue")
            if isinstance(nested, Mapping):
                if isinstance(nested.get("usage"), Mapping):
                    raise RuntimeError("interactive_cancel_provider_usage_present")
                if "provider_started" in nested:
                    evidence_values.append(nested.get("provider_started"))
        if marker.get("provider_started") is not False or any(value is True for value in evidence_values) or any(value not in (False,) for value in evidence_values):
            raise RuntimeError("interactive_cancel_provider_start_unknown")
        usage = await session.scalar(
            select(AgentDialogueUsage)
            .where(AgentDialogueUsage.run_id == run.id)
            .with_for_update(nowait=True)
        )
        if usage is not None:
            raise RuntimeError("interactive_cancel_usage_receipt_present")
        if release:
            from .agent_dialogue_budget import release_dialogue_budget

            await release_dialogue_budget(
                session,
                dialogue.id,
                f"run:{run.id}",
                expected_dialogue_id=dialogue.id,
                expected_run_id=run.id,
            )
        return True

    async def _interactive_cancel_graph_settled_in_session(
        self,
        session: Any,
        work: AgentWorkItem,
    ) -> bool:
        """Return true only when a prior interactive cancel closed its graph."""

        run_id = _as_uuid(work.source_id)
        if run_id is None or work.active_agent_run_id is not None or any(
            getattr(work, field, None) is not None
            for field in ("lease_owner", "lease_token", "lease_expires_at")
        ):
            return False
        run = (
            await session.execute(
                select(AgentRun).where(AgentRun.id == run_id).with_for_update(nowait=True)
            )
        ).scalars().first()
        if (
            run is None
            or run.work_item_id != work.id
            or str(run.run_type or "").casefold() != "chat_turn"
            or str(run.status or "").casefold() not in {"failed", "cancelled"}
        ):
            return False
        deliveries = await self._lock_source_dialogue_deliveries_in_session(session, work)
        for delivery in deliveries:
            state = str(delivery.state or "").casefold()
            if state in {"pending", "available", "leased", "delivered"}:
                return False
            if state not in {"cancelled", "failed", "expired", "acked"}:
                return False
            transport_id = _as_uuid(delivery.transport_work_item_id)
            if transport_id is None:
                continue
            transport = (
                await session.execute(
                    select(AgentWorkItem)
                    .where(AgentWorkItem.id == transport_id)
                    .with_for_update(nowait=True)
                )
            ).scalars().first()
            transport_state = str(transport.state or "").casefold() if transport is not None else ""
            if transport is None or transport_state in {
                "pending",
                "claimed",
                "running",
                "awaiting_approval",
                "uncertain",
            }:
                return False
            if transport_state not in {"succeeded", "failed", "cancelled", "dead_letter"}:
                return False
        outbox, outbox_cancel = await self._lock_dispatch_outbox_for_interactive_cancel(session, run)
        if outbox is not None and outbox_cancel:
            return False
        marker = self._interactive_dialogue_marker(run)
        if marker is not None:
            reservation = await self._lock_reserved_dialogue_reservation_in_session(session, run)
            if reservation is not None:
                return False
        return True

    async def _cancel_interactive_work_in_session(
        self,
        session: Any,
        work: AgentWorkItem,
        *,
        reason: str,
        actor_kind: str,
        actor_id: str,
    ) -> bool | str:
        """Cancel one exact interactive Chat Run/Work pair.

        The generic Work cancellation path intentionally remains domain
        neutral.  Interactive Chat has a typed Run/Dialogue transport graph,
        however, so a direct Work cancellation must first prove that the
        canonical Run and any unconsumed Delivery/transport Work are the same
        pair.  Any uncertain, live or foreign binding fails closed before the
        caller mutates the aggregate Work row.
        """

        run_id = _as_uuid(getattr(work, "source_id", None))
        if run_id is None:
            return False
        result = await session.execute(
            select(AgentRun)
            .where(AgentRun.id == run_id)
            .with_for_update(nowait=True)
        )
        run = result.scalars().first()
        if run is None:
            return False
        if (
            str(getattr(run, "run_type", "") or "").casefold() != "chat_turn"
            or getattr(run, "work_item_id", None) != work.id
            or str(getattr(work, "source_id", "") or "") != str(run.id)
            or str(getattr(work, "source_revision", "") or "")
            != str(getattr(run, "agent_revision_id", "") or "")
            or getattr(run, "session_id", None) != getattr(work, "conversation_session_id", None)
            or getattr(run, "agent_id", None) != getattr(work, "assigned_agent_id", None)
            or getattr(run, "agent_revision_id", None) != getattr(work, "agent_revision_id", None)
            or str(getattr(work, "intent_key", "") or "") != "interactive_chat"
            or getattr(work, "active_agent_run_id", None) not in (None, run.id)
        ):
            return False

        status = str(getattr(run, "status", "") or "").casefold()
        run_snapshot = {
            "status": run.status,
            "updated_at": run.updated_at,
            "error": run.error,
            "ended_at": run.ended_at,
        }
        active_states = {"queued", "running", "awaiting_approval", "uncertain"}
        dialogue_marker = (
            run.run_metadata.get("agent_dialogue")
            if isinstance(run.run_metadata, Mapping)
            else None
        )
        if not isinstance(dialogue_marker, Mapping) or dialogue_marker.get("dialogue_id") in (None, ""):
            # A normal Chat Run has no typed Dialogue edge.  Prove that
            # absence before allowing cancellation; a hidden/foreign source
            # Delivery must fail closed instead of being skipped.
            if await self._lock_source_dialogue_deliveries_in_session(session, work):
                raise RuntimeError("interactive_cancel_dialogue_marker_missing")
            dialogue_marker = None
        dialogue_transport = None
        if isinstance(dialogue_marker, Mapping) and dialogue_marker.get("dialogue_id") not in (None, ""):
            from .agent_dialogue_transport import AgentDialogueTransport

            dialogue_transport = AgentDialogueTransport(
                self._db_manager,
                coordinator=self,
                config=self.config,
            )
        if status in active_states:
            work_state = str(getattr(work, "state", "") or "").casefold()
            if work_state not in {"pending", "claimed", "running", "awaiting_approval", "uncertain"}:
                return False
            if (
                status != "queued"
                and getattr(work, "active_agent_run_id", None) != run.id
            ):
                return False
            outbox, outbox_cancel = await self._lock_dispatch_outbox_for_interactive_cancel(
                session,
                run,
            )
            reservation = await self._lock_reserved_dialogue_reservation_in_session(session, run)
            if reservation is not None and status != "queued":
                raise RuntimeError("interactive_cancel_reserved_provider_run")
            if dialogue_transport is not None:
                await dialogue_transport.cancel_interactive_source_in_session(
                    session,
                    run=run,
                    work=work,
                    reason=reason,
                    validate_only=True,
                )
            if reservation is not None:
                await self._release_reserved_dialogue_if_never_started(
                    session,
                    run=run,
                    work=work,
                    outbox=outbox,
                    reservation=reservation,
                    release=True,
                )
            now = self._now()
            safe_reason = _safe_error(reason, limit=500) or "cancelled"
            if dialogue_transport is not None:
                await dialogue_transport.cancel_interactive_source_in_session(
                    session,
                    run=run,
                    work=work,
                    reason=reason,
                )
            from .agent_run_service import AgentRunService
            run_cas_conditions = [
                AgentRun.id == run.id,
                AgentRun.status == run_snapshot["status"],
                AgentRun.updated_at == run_snapshot["updated_at"],
            ]
            for field in ("error", "ended_at"):
                value = run_snapshot[field]
                column = getattr(AgentRun, field)
                run_cas_conditions.append(column.is_(None) if value is None else column == value)
            with session.no_autoflush:
                run_cas = await session.execute(
                    update(AgentRun)
                    .where(*run_cas_conditions)
                    .values(
                        status="cancelled",
                        error=safe_reason,
                        ended_at=now,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
            if int(getattr(run_cas, "rowcount", 0) or 0) != 1:
                raise RuntimeError("interactive_cancel_run_cas_lost")
            run.status = "cancelled"
            run.error = safe_reason
            run.ended_at = now
            run.updated_at = now
            await AgentRunService(self._db_manager, config=self.config)._append_event(
                session,
                run,
                "run.cancelled",
                status="cancelled",
                message=safe_reason,
                payload={
                    "source": "agent_work_cancel",
                    "actor_kind": actor_kind,
                    "actor_id": actor_id,
                    "run_id": str(run.id),
                },
            )
            sibling = await session.scalar(
                select(AgentRun.id)
                .where(
                    AgentRun.session_id == run.session_id,
                    AgentRun.status.in_(tuple(active_states)),
                    AgentRun.id != run.id,
                )
                .limit(1)
            )
            if sibling is None and run.session_id is not None:
                active_sibling_exists = select(AgentRun.id).where(
                    AgentRun.session_id == run.session_id,
                    AgentRun.status.in_(tuple(active_states)),
                    AgentRun.id != run.id,
                ).exists()
                await session.execute(
                    update(ConversationSession)
                    .where(
                        ConversationSession.id == run.session_id,
                        ConversationSession.development_status == "working",
                        ~active_sibling_exists,
                    )
                    .values(development_status="waiting_for_user")
                )
            if outbox is not None and outbox_cancel:
                await self._cancel_dispatch_outbox_cas(session, outbox)
            return str(run.id)

        if status not in {"failed", "cancelled"} or str(getattr(work, "state", "") or "") != "blocked":
            return False
        if any(
            getattr(work, field, None) is not None
            for field in ("lease_owner", "lease_token", "lease_expires_at")
        ):
            return False

        # A terminal Run is history.  Never rewrite its error/result/message;
        # only clear a stale aggregate pointer after proving the exact link.
        outbox, outbox_cancel = await self._lock_dispatch_outbox_for_interactive_cancel(
            session,
            run,
        )
        reservation = await self._lock_reserved_dialogue_reservation_in_session(session, run)
        if dialogue_transport is not None:
            await dialogue_transport.cancel_interactive_source_in_session(
                session,
                run=run,
                work=work,
                reason=reason,
                validate_only=True,
            )
        if reservation is not None:
            await self._release_reserved_dialogue_if_never_started(
                session,
                run=run,
                work=work,
                outbox=outbox,
                reservation=reservation,
                allow_terminal=True,
            )
        if getattr(work, "active_agent_run_id", None) == run.id:
            work.active_agent_run_id = None

        # The transport helper validates and cancels only the Delivery rows
        # sourced by this exact Run/Work pair.  It leaves acked requests,
        # sibling/root rows and unrelated conversations untouched.
        if dialogue_transport is not None:
            await dialogue_transport.cancel_interactive_source_in_session(
                session,
                run=run,
                work=work,
                reason=reason,
            )
        if outbox is not None and outbox_cancel:
            await self._cancel_dispatch_outbox_cas(session, outbox)
        return True

    async def _lock_dispatch_outbox_for_interactive_cancel(
        self,
        session: Any,
        run: AgentRun,
    ) -> tuple[Any | None, bool]:
        """Lock and prove one Run-owned dispatch outbox cancellation."""

        outbox = (
            await session.execute(
                select(ConversationDispatchOutbox)
                .where(ConversationDispatchOutbox.run_id == run.id)
                .with_for_update(nowait=True)
            )
        ).scalars().first()
        if outbox is None:
            return None, False
        payload = outbox.payload if isinstance(outbox.payload, Mapping) else {}
        if (
            getattr(outbox, "session_id", None) != getattr(run, "session_id", None)
            or str(getattr(outbox, "user_id", "") or "")
            != str(getattr(run, "user_id", "") or "")
            or (
                payload.get("agent_run_id") not in (None, "")
                and _as_uuid(payload.get("agent_run_id")) != run.id
            )
        ):
            raise RuntimeError("interactive_cancel_outbox_binding_invalid")
        status = str(getattr(outbox, "status", "") or "").casefold()
        if status == "pending":
            if any(
                getattr(outbox, field, None) not in (None, "")
                for field in ("lease_owner", "lease_token", "lease_expires_at")
            ):
                raise RuntimeError("interactive_cancel_outbox_lease_invalid")
            return outbox, True
        if status == "claimed":
            expires = getattr(outbox, "lease_expires_at", None)
            if (
                not getattr(outbox, "lease_owner", None)
                or not getattr(outbox, "lease_token", None)
                or expires is None
                or expires > self._now()
            ):
                raise RuntimeError("interactive_cancel_outbox_live")
            return outbox, True
        if status in {"delivered", "cancelled", "deadletter"}:
            return outbox, False
        raise RuntimeError("interactive_cancel_outbox_state_invalid")

    @staticmethod
    async def _cancel_dispatch_outbox_cas(session: Any, outbox: Any) -> None:
        conditions = [
            ConversationDispatchOutbox.run_id == outbox.run_id,
            ConversationDispatchOutbox.status == outbox.status,
            ConversationDispatchOutbox.updated_at == outbox.updated_at,
        ]
        for field in ("lease_owner", "lease_token", "lease_expires_at"):
            value = getattr(outbox, field, None)
            column = getattr(ConversationDispatchOutbox, field)
            conditions.append(column.is_(None) if value is None else column == value)
        with session.no_autoflush:
            result = await session.execute(
                update(ConversationDispatchOutbox)
                .where(*conditions)
                .values(
                    status="cancelled",
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=datetime.utcnow(),
                )
                .execution_options(synchronize_session=False)
            )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            raise RuntimeError("interactive_cancel_outbox_cas_lost")
        outbox.status = "cancelled"
        outbox.lease_owner = None
        outbox.lease_token = None
        outbox.lease_expires_at = None
        outbox.updated_at = datetime.utcnow()

    @staticmethod
    async def _lock_reserved_dialogue_reservation_in_session(
        session: Any,
        run: AgentRun,
    ) -> Any | None:
        """Lock a reserved Dialogue budget only for typed Dialogue Runs."""

        metadata = run.run_metadata if isinstance(run.run_metadata, Mapping) else {}
        marker = metadata.get("agent_dialogue")
        if not isinstance(marker, Mapping) or marker.get("dialogue_id") in (None, ""):
            return None
        return await session.scalar(
            select(AgentDialogueReservation)
            .where(
                AgentDialogueReservation.run_id == run.id,
                AgentDialogueReservation.state == "reserved",
            )
            .with_for_update(nowait=True)
        )

    async def resume_after_approval(
        self,
        work_item_id: str,
        *,
        action_id: str | None = None,
    ) -> bool:
        """Return an awaiting-approval item to pending after human approval.

        The method never approves an action and never executes a provider. It
        merely projects the existing ExternalAction approval ledger back into
        the common queue so the configured trusted adapter can continue.
        """

        session = await self._session()
        try:
            try:
                locked = await session.execute(
                    select(AgentWorkItem)
                    .where(AgentWorkItem.id == _as_uuid(work_item_id))
                    .with_for_update()
                )
                row = locked.scalars().first()
            except Exception:
                row = await session.get(AgentWorkItem, _as_uuid(work_item_id))
            if row is None or row.state != "awaiting_approval":
                await session.rollback()
                return False
            stmt = select(ExternalAction).where(
                ExternalAction.origin_work_item_id == row.id,
                ExternalAction.status == "approved",
            )
            if action_id:
                stmt = stmt.where(ExternalAction.id == _as_uuid(action_id))
            try:
                stmt = stmt.with_for_update()
            except Exception:
                pass
            action = (
                await session.execute(
                    stmt.order_by(desc(ExternalAction.updated_at)).limit(1)
                )
            ).scalars().first()
            if action is None:
                await session.rollback()
                return False
            previous = str(row.state or "")
            row.state = "pending"
            row.outcome_classification = None
            row.not_before = self._now()
            row.next_attempt_at = row.not_before
            row.completed_at = None
            row.safe_error_code = None
            row.escalation_reason = None
            row.updated_at = self._now()
            await self._append_event_in_session(session, row, event_type="work.pending", actor_kind="service", actor_id=self.worker_id, from_state=previous, to_state="pending", payload={"approval_resumed": True, "action_id": str(action.id)})
            await session.commit()
            return True
        except Exception:
            await self._rollback(session)
            return False
        finally:
            await self._close(session)

    async def recover_stale(self, *, limit: int = 500) -> dict[str, Any]:
        """Recover expired claimed/running work after process/application restart."""

        if not self._runtime_enabled():
            return {"recovered": 0, "dead_lettered": 0}
        session = await self._session()
        recovered = 0
        dead = 0
        stale_run_ids: list[str] = []
        recovery_hook_failed = False
        try:
            now = self._now()
            recovery_conditions = []
            for key, adapter in self.adapters.items():
                if callable(getattr(adapter, "recover_stale", None)):
                    condition = getattr(adapter, "recovery_condition", None)
                    try:
                        recovery_conditions.append(and_(AgentWorkItem.execution_adapter == key,
                            AgentWorkItem.state.in_(("blocked", "dead_letter", "retry_wait")),
                            condition() if callable(condition) else True))
                    except Exception:
                        recovery_hook_failed = True
                        raise
            stmt = (
                select(AgentWorkItem)
                .where(or_(
                    and_(AgentWorkItem.state.in_(tuple(WORK_ACTIVE_STATES)), AgentWorkItem.lease_expires_at <= now),
                    *recovery_conditions,
                ))
                .order_by(AgentWorkItem.updated_at)
                .limit(max(1, min(int(limit), 5000)))
            )
            try:
                stmt = stmt.with_for_update(skip_locked=True)
            except TypeError:
                stmt = stmt.with_for_update()
            for row in (await session.execute(stmt)).scalars().all():
                stale_run_id = getattr(row, "active_agent_run_id", None)
                previous_state = str(row.state or "")
                # SQLite ignores FOR UPDATE. Recheck the observed lease before
                # any recovery hook/domain writes, and retain a write lock
                # until this transaction commits. A committed cancel or renew
                # must win over this stale ORM snapshot. The no-op preserves
                # updated_at and the existing adapter recovery contract.
                fence = await session.execute(update(AgentWorkItem).where(
                    AgentWorkItem.id == row.id,
                    AgentWorkItem.state == row.state,
                    AgentWorkItem.lease_owner == row.lease_owner,
                    AgentWorkItem.lease_token == row.lease_token,
                    AgentWorkItem.lease_expires_at == row.lease_expires_at,
                    AgentWorkItem.active_agent_run_id == stale_run_id,
                    AgentWorkItem.updated_at == row.updated_at,
                ).values(updated_at=AgentWorkItem.updated_at)
                    .execution_options(synchronize_session=False))
                if int(fence.rowcount or 0) != 1:
                    continue
                hook = getattr(self.adapters.get(row.execution_adapter), "recover_stale", None)
                if callable(hook):
                    try:
                        handled = await _maybe_await(hook(session, row, coordinator=self, now=now))
                    except Exception:
                        recovery_hook_failed = True
                        raise
                    if handled:
                        recovered += 1
                        # A hook that kept the same Run linked continues or
                        # parks it; only a released Run is failed as stale.
                        if stale_run_id and getattr(row, "active_agent_run_id", None) != stale_run_id:
                            stale_run_ids.append(str(stale_run_id))
                        continue
                if previous_state not in WORK_ACTIVE_STATES:
                    continue
                # Interactive chat has an in-process owner and no safe way to
                # reconstruct provider state after a restart.  Mark it
                # uncertain so the live client can surface the interruption;
                # never hand it to the autonomous poller for a blind retry.
                interactive_source = (
                    str(row.source_type or "") == "chat_turn"
                    or str(row.execution_adapter or "") == "interactive_chat"
                )
                if interactive_source:
                    state = "uncertain"
                    row.not_before = None
                    row.next_attempt_at = None
                elif int(row.attempt_count or 0) >= int(row.max_attempts or self.max_attempts):
                    state = "dead_letter"
                    dead += 1
                else:
                    state = "retry_wait"
                    row.not_before = now
                    row.next_attempt_at = now
                    recovered += 1
                if state != "retry_wait":
                    row.not_before = None
                    row.next_attempt_at = None
                row.state = state
                row.lease_owner = None
                row.lease_token = None
                row.lease_expires_at = None
                row.active_agent_run_id = None
                if isinstance(row.budget_reservation_json, Mapping) and row.budget_reservation_json:
                    row.budget_reservation_json = self._settle_budget(
                        dict(row.budget_reservation_json),
                        ExecutionOutcome(classification="transient"),
                        state=state,
                    )
                row.heartbeat_at = now
                row.updated_at = now
                row.safe_error_code = "stale_lease_recovered" if state == "retry_wait" else "max_attempts_exhausted"
                if stale_run_id:
                    stale_run_ids.append(str(stale_run_id))
                await self._append_event_in_session(session, row, event_type=f"work.{state}", actor_kind="service", actor_id=self.worker_id, agent_run_id=stale_run_id, from_state=previous_state, to_state=state, safe_error_code=row.safe_error_code, payload={"recovered_after_restart": True, "error_code": row.safe_error_code})
            await session.commit()
            if stale_run_ids:
                try:
                    from .agent_run_service import AgentRunService
                    for run_id in stale_run_ids:
                        await AgentRunService(
                            self._db_manager,
                            config=self.config,
                        ).fail_run(
                            run_id,
                            "AgentWork lease expired during process recovery",
                        )
                except Exception:
                    logger.debug("stale AgentRun reconciliation failed", exc_info=True)
            return {"recovered": recovered, "dead_lettered": dead}
        except Exception:
            await self._rollback(session)
            if recovery_hook_failed:
                raise RuntimeError("adapter_recovery_failed") from None
            return {"recovered": 0, "dead_lettered": 0}
        finally:
            await self._close(session)

    async def reconcile_run_settlements(self, *, limit: int = 100) -> int:
        """Repair AgentRuns left in-flight after a committed WorkItem outcome."""

        if not self._runtime_enabled():
            return 0
        session = await self._session()
        pairs: list[tuple[AgentRun, AgentWorkItem]] = []
        try:
            try:
                result = await session.execute(
                    select(AgentRun, AgentWorkItem)
                    .join(AgentWorkItem, AgentWorkItem.id == AgentRun.work_item_id)
                    .where(
                        AgentRun.status.in_(("queued", "running")),
                        AgentWorkItem.state.in_(
                            tuple(
                                WORK_TERMINAL_STATES
                                | {"blocked", "uncertain", "awaiting_approval"}
                            )
                        ),
                    )
                    .order_by(AgentRun.updated_at)
                    .limit(max(1, min(int(limit), 500)))
                )
                pairs = list(result.all())
            except Exception:
                await self._rollback(session)
                return 0
        finally:
            await self._close(session)
        repaired = 0
        try:
            from .agent_run_service import AgentRunService

            service = AgentRunService(self._db_manager, config=self.config)
            for run, item in pairs:
                state = str(item.state or "")
                try:
                    if state == "succeeded":
                        await service.complete_run(
                            str(run.id),
                            result=item.result_summary_json
                            if isinstance(item.result_summary_json, dict)
                            else {},
                            message=item.result_summary,
                            metadata={
                                "work_item_id": str(item.id),
                                "reconciled_from_work_item": True,
                            },
                        )
                    else:
                        await service.fail_run(
                            str(run.id),
                            item.safe_error_code or "work_item_terminal",
                            status=(
                                "awaiting_approval"
                                if state == "awaiting_approval"
                                else "failed"
                            ),
                            metadata={
                                "work_item_id": str(item.id),
                                "classification": item.outcome_classification
                                or state,
                                "reconciled_from_work_item": True,
                            },
                        )
                    repaired += 1
                except Exception:
                    logger.warning(
                        "AgentRun settlement reconciliation failed for %s",
                        run.id,
                        exc_info=True,
                    )
        except Exception:
            logger.warning("AgentRun settlement reconciliation unavailable", exc_info=True)
        return repaired

    async def create_agent_run(self, claim: WorkClaim) -> dict[str, Any] | None:
        """Create exactly one pinned AgentRun for this attempt."""

        if not claim.work_item_id:
            return None
        session = await self._session()
        try:
            row = await session.get(AgentWorkItem, _as_uuid(claim.work_item_id))
            if row is None:
                await session.rollback()
                return None
            # Reuse an existing run for an idempotent execute_once replay.
            existing_id = getattr(row, "active_agent_run_id", None)
            if existing_id and str(row.state or "") in {"claimed", "running"}:
                existing = await session.get(AgentRun, existing_id)
                if existing is not None and str(existing.status or "") not in {"succeeded", "failed", "cancelled"}:
                    await session.commit()
                    return existing.to_dict()
            agent_run_service = None
            try:
                from .agent_run_service import AgentRunService

                agent_run_service = AgentRunService(
                    self._db_manager,
                    config=self.config,
                )
            except Exception:
                agent_run_service = None
            if agent_run_service is None:
                await session.rollback()
                return None
            # AgentRunService owns validation/redaction and its own short
            # transaction.  We only pass typed fields from the durable claim.
            previous_attempt_id = None
            route_snapshot: dict[str, Any] | None = None
            if row.agent_revision_id is not None and row.assigned_agent_id is not None:
                from .agent_model_routing import AgentRoutingPins, build_agent_routing_snapshot

                revision = await session.get(AgentRevision, row.agent_revision_id)
                if revision is None or revision.agent_id != row.assigned_agent_id:
                    await session.rollback()
                    raise ValueError("agent revision is unavailable for routing snapshot")
                pins = AgentRoutingPins.from_revision(revision)
                route_snapshot = build_agent_routing_snapshot(self.config, pins).get("roles")
            previous_result = await session.execute(
                select(AgentRun)
                .where(AgentRun.work_item_id == _as_uuid(claim.work_item_id))
                .order_by(desc(AgentRun.created_at))
                .limit(1)
            )
            previous_row = previous_result.scalars().first()
            if previous_row is not None:
                previous_attempt_id = str(previous_row.id)
            previous_manifest = (
                dict(previous_row.resolved_execution_manifest)
                if previous_row is not None and isinstance(previous_row.resolved_execution_manifest, dict)
                else None
            )
            previous_manifest_hash = (
                str(previous_row.execution_manifest_hash)
                if previous_row is not None and previous_row.execution_manifest_hash
                else None
            )
            if previous_row is not None and (
                not isinstance(previous_manifest, Mapping)
                or not isinstance(previous_manifest.get("route_snapshot"), Mapping)
            ):
                # A v1 AgentRun never captured the provider/model route.  Do
                # not recreate a route from today's App Config on retry: the
                # old attempt may already have run under a different model.
                # The caller turns this into a terminal blocked work item so a
                # scheduler cannot retry it forever; an operator must create
                # a fresh, explicitly reviewed work item.
                from .agent_model_routing import AgentModelRouteError

                await session.rollback()
                raise AgentModelRouteError("legacy_route_snapshot_required")
            await session.commit()
        finally:
            await self._close(session)
        execution_manifest = previous_manifest or {
            "schema_version": 2,
            "agent_id": claim.assigned_agent_id,
            "agent_revision_id": claim.agent_revision_id,
            "capabilities": list(claim.required_capabilities),
            "project_id": claim.project_id,
            "task_id": claim.task_id,
            "work_item_id": claim.work_item_id,
            "source": claim.source_type,
            "team_id": pins.agent_team_id if route_snapshot is not None else None,
            "execution_profile_id": pins.execution_profile_id if route_snapshot is not None else None,
            "route_snapshot": route_snapshot,
        }
        kwargs = {
            "agent_id": claim.assigned_agent_id,
            "agent_revision_id": claim.agent_revision_id,
            "project_id": claim.project_id,
            "task_id": claim.task_id,
            "work_item_id": claim.work_item_id,
            "work_item_attempt": claim.attempt,
            "previous_attempt_run_id": previous_attempt_id,
            "run_type": "autonomous_work",
            "title": f"{claim.source_type}:{claim.source_id}",
            "objective": "",
            "metadata": {"work_item_id": claim.work_item_id, "attempt": claim.attempt, "domain": claim.domain},
            "execution_manifest": execution_manifest,
            "execution_manifest_hash": previous_manifest_hash,
        }
        # The typed AgentRunService signature is part of the common ledger
        # contract.  An internal TypeError must block this attempt; retrying
        # with fields stripped could orphan the WorkItem/run fence.
        return await agent_run_service.create_run(**kwargs)

    def _register_owned_task(
        self,
        task: asyncio.Task[Any],
        *,
        adapter: ExecutionAdapter | None = None,
        claim: WorkClaim | None = None,
        protected: bool = False,
    ) -> None:
        """Register a task until its own ``finally`` releases ownership."""

        self._running_tasks.add(task)
        if task not in self._owned_task_callbacks:
            self._owned_task_callbacks.add(task)
            task.add_done_callback(self._owned_task_done)
        if protected:
            self._protected_tasks.add(task)
        if adapter is not None and claim is not None:
            self._running_task_owners[task] = (adapter, claim)

    def _release_owned_task(self, task: asyncio.Task[Any] | None) -> None:
        if task is None:
            return
        self._running_task_owners.pop(task, None)
        self._protected_tasks.discard(task)
        self._running_tasks.discard(task)
        if task.done():
            self._owned_task_callbacks.discard(task)

    def _owned_task_done(self, task: asyncio.Task[Any]) -> None:
        """Release every owned task through one completion callback."""

        self._owned_task_callbacks.discard(task)
        self._release_owned_task(task)
        _consume_async_task(task)

    async def _cancel_owned_task(
        self,
        task: asyncio.Task[Any],
        *,
        timeout_seconds: float | None = None,
    ) -> bool:
        """Cancel one owned task without waiting past the shutdown budget."""

        if not task.done():
            task.cancel()
        if task.done():
            _consume_async_task(task)
            return True
        timeout = self.shutdown_timeout_seconds if timeout_seconds is None else max(0.0, float(timeout_seconds))
        try:
            # Shield the child so a timeout/cancellation of this coordinator
            # does not interrupt the adapter's own native cleanup path.
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            task.add_done_callback(_consume_async_task)
            return False
        except asyncio.CancelledError:
            # A cancelled child reports CancelledError through the shield too;
            # only propagate cancellation when the coordinator itself was
            # cancelled while the child is still pending.
            if task.done():
                _consume_async_task(task)
                return True
            task.add_done_callback(_consume_async_task)
            raise
        except BaseException:
            _consume_async_task(task)
            return True
        _consume_async_task(task)
        return True

    async def _run_adapter_cleanup(
        self,
        adapter: ExecutionAdapter,
        claim: WorkClaim,
    ) -> None:
        """Run adapter cleanup with a finite, cancellation-safe drain."""

        cleanup = getattr(adapter, "cleanup", None)
        if not callable(cleanup):
            return
        from .agent_run_service import (
            reset_current_agent_run_id,
            set_current_agent_run_id,
        )

        cleanup_context_token = set_current_agent_run_id(None)
        try:
            # Bind before invoking cleanup as well as before scheduling its
            # awaitable.  A synchronous adapter may return an already-created
            # Task, whose ContextVar context is fixed at creation time.
            value = cleanup(claim)
            if not inspect.isawaitable(value):
                return
            cleanup_task = asyncio.ensure_future(value)
        finally:
            reset_current_agent_run_id(cleanup_context_token)
        self._register_owned_task(cleanup_task, adapter=adapter, claim=claim, protected=True)
        try:
            # ``shield`` lets an application stop return on its own deadline
            # while native/provider cleanup continues and is still observed.
            await asyncio.wait_for(
                asyncio.shield(cleanup_task),
                timeout=self.shutdown_timeout_seconds,
            )
        except asyncio.TimeoutError:
            cleanup_task.add_done_callback(_consume_async_task)
            logger.warning(
                "AgentWork adapter cleanup exceeded shutdown budget for %s",
                claim.work_item_id,
            )
        except asyncio.CancelledError:
            if cleanup_task.done():
                _consume_async_task(cleanup_task)
            else:
                cleanup_task.add_done_callback(_consume_async_task)
            raise
        except BaseException:
            _consume_async_task(cleanup_task)
            logger.warning(
                "AgentWork adapter cleanup failed for %s",
                claim.work_item_id,
                exc_info=True,
            )
        finally:
            if cleanup_task.done():
                self._release_owned_task(cleanup_task)

    async def _run_owned_adapter(
        self,
        adapter: ExecutionAdapter,
        claim: WorkClaim,
        *,
        session: Any,
        run_id: str | None,
        run: Mapping[str, Any] | None,
    ) -> Any:
        """Invoke an adapter while retaining session/task ownership."""

        current = asyncio.current_task()
        from .agent_run_service import (
            reset_current_agent_run_id,
            set_current_agent_run_id,
        )

        # Bind every adapter attempt, including an explicit ``None``.  This
        # prevents a parent task's Run from leaking into provider usage and
        # keeps success/failure/cancellation task-local.
        run_context_token = set_current_agent_run_id(run_id)
        try:
            return await self._invoke_adapter(
                adapter,
                claim,
                session=session,
                run_id=run_id,
                run=run,
            )
        finally:
            # Reset before creating the session-close task so it cannot copy
            # the adapter's Run context.  The same applies to any deferred
            # cleanup task created by the coordinator after this returns.
            reset_current_agent_run_id(run_context_token)
            # The adapter task owns its DB session.  This is essential when a
            # native worker outlives the coordinator's bounded drain window.
            close_context_token = set_current_agent_run_id(None)
            try:
                close_task = asyncio.ensure_future(self._close(session))
            finally:
                reset_current_agent_run_id(close_context_token)
            self._register_owned_task(close_task, adapter=adapter, claim=claim, protected=True)
            if current is not None:
                self._adapter_close_tasks[current] = close_task

                def _close_done(finished: asyncio.Task[Any]) -> None:
                    if self._adapter_close_tasks.get(current) is finished:
                        self._adapter_close_tasks.pop(current, None)

                close_task.add_done_callback(_close_done)
            try:
                await asyncio.wait_for(
                    asyncio.shield(close_task),
                    timeout=self.shutdown_timeout_seconds,
                )
            except asyncio.TimeoutError:
                close_task.add_done_callback(_consume_async_task)
                logger.warning(
                    "AgentWork adapter session close exceeded shutdown budget for %s",
                    claim.work_item_id,
                )
            except asyncio.CancelledError:
                close_task.add_done_callback(_consume_async_task)
            except BaseException:
                _consume_async_task(close_task)
                logger.warning(
                    "AgentWork adapter session close failed for %s",
                    claim.work_item_id,
                    exc_info=True,
                )
            finally:
                if close_task.done():
                    self._release_owned_task(close_task)
                self._release_owned_task(current)

    def _defer_adapter_cleanup(
        self,
        adapter_task: asyncio.Task[Any],
        adapter: ExecutionAdapter,
        claim: WorkClaim,
    ) -> None:
        """Run adapter cleanup only after adapter and session close finish."""

        async def wait_then_cleanup() -> None:
            current = asyncio.current_task()
            try:
                while not adapter_task.done():
                    try:
                        await asyncio.shield(adapter_task)
                    except asyncio.CancelledError:
                        # This task is protected from coordinator stop, but
                        # retain the loop if an embedding cancels it directly.
                        continue
                    except BaseException:
                        break
                _consume_async_task(adapter_task)
                while True:
                    close_task = self._adapter_close_tasks.get(adapter_task)
                    if close_task is None or close_task.done():
                        break
                    try:
                        await asyncio.shield(close_task)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        break
                cleanup_task = asyncio.create_task(
                    self._run_adapter_cleanup(adapter, claim),
                    name=f"agent-work-cleanup:{claim.work_item_id}",
                )
                while not cleanup_task.done():
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        break
                _consume_async_task(cleanup_task)
            finally:
                self._release_owned_task(current)

        from .agent_run_service import (
            reset_current_agent_run_id,
            set_current_agent_run_id,
        )

        cleanup_context_token = set_current_agent_run_id(None)
        try:
            task = asyncio.create_task(
                wait_then_cleanup(),
                name=f"agent-work-deferred-cleanup:{claim.work_item_id}",
            )
        finally:
            reset_current_agent_run_id(cleanup_context_token)
        self._register_owned_task(task, adapter=adapter, claim=claim, protected=True)

    def _local_active_claims(self) -> dict[str, WorkClaim]:
        """Count each claim once, including cancellation/cleanup still in flight."""

        claims = {
            claim.work_item_id: claim
            for task, claim in self._execution_tasks.items()
            if not task.done()
        }
        claims.update({
            claim.work_item_id: claim
            for task, (_adapter, claim) in self._running_task_owners.items()
            if not task.done()
        })
        return claims

    def _execution_task_done(self, task: asyncio.Task[Any]) -> None:
        claim = self._execution_tasks.pop(task, None)
        error = None if task.cancelled() else task.exception()
        if error is not None:
            logger.warning(
                "AgentWork execution monitor failed for %s; lease recovery remains authoritative",
                claim.work_item_id if claim else "unknown",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _dispatch_once(
        self, *, limit: int, source_type: str | None = None
    ) -> list[asyncio.Task[list[dict[str, Any]]]]:
        """Discover/recover and dispatch only vacant slots; do not await adapters."""

        async with self._dispatch_lock:
            if not self._runtime_enabled():
                return []
            count = max(1, min(int(limit or 1), 32 if self.max_concurrency is None else self.max_concurrency))
            await self.reconcile_run_settlements(limit=count)
            if self.sources:
                try:
                    await self.discover_and_materialize(source_type=source_type)
                except Exception:
                    logger.warning("AgentWork discovery/materialization failed", exc_info=True)
            await self.recover_stale(limit=count)
            available = count if self.max_concurrency is None else self.max_concurrency - len(self._local_active_claims())
            if available <= 0:
                return []
            claims = await self.claim_many(min(count, available), source_type=source_type)
            tasks = []
            for claim in claims:
                task = asyncio.create_task(
                    self._execute_claim(claim),
                    name=f"agent-work-execution:{claim.work_item_id}",
                )
                self._execution_tasks[task] = claim
                self._register_owned_task(task)
                task.add_done_callback(self._execution_task_done)
                tasks.append(task)
            return tasks

    async def execute_once(self, *, limit: int = 1, source_type: str | None = None) -> list[dict[str, Any]]:
        """Dispatch at most *limit* independent claims and await this batch's results."""

        tasks = await self._dispatch_once(limit=limit, source_type=source_type)
        if not tasks:
            return []
        batches = await asyncio.gather(*tasks)
        return [result for batch in batches for result in batch]

    async def _execute_claim(self, claim: WorkClaim) -> list[dict[str, Any]]:
        """Own one attempt's session, heartbeat, fencing and settlement independently."""

        results: list[dict[str, Any]] = []
        adapter = self.adapters.get(claim.execution_adapter) or self.adapters.get("default")
        if adapter is None:
            await self.settle(claim, ExecutionOutcome(classification="permanent", error_code="execution_adapter_unavailable", error_message="execution adapter unavailable"))
            return results
        if not claim.assigned_agent_id and claim.execution_adapter not in {"system", "service"}:
            outcome = ExecutionOutcome(classification="blocked", error_code="agent_identity_missing", error_message="autonomous work requires an assigned Agent")
            settled = await self.settle(claim, outcome, source_ok=True)
            results.append({"claim": claim.to_dict(), "run": None, "outcome": outcome.normalized_classification(), "settlement": settled})
            return results
        if claim.assigned_agent_id and not claim.agent_revision_id:
            await self._settle_claimed_blocked(claim, "agent_revision_missing")
            return results
        try:
            from .agent_team_v3 import AGENT_TEAM_CAPABILITY_CATALOG
            unknown_caps = [item for item in claim.required_capabilities if item not in AGENT_TEAM_CAPABILITY_CATALOG]
        except Exception:
            unknown_caps = list(claim.required_capabilities)
        if unknown_caps:
            outcome = ExecutionOutcome(classification="blocked", error_code="unknown_capability", error_message="work item declares an unknown capability")
            settled = await self.settle(claim, outcome, source_ok=True)
            results.append({"claim": claim.to_dict(), "run": None, "outcome": outcome.normalized_classification(), "settlement": settled})
            return results
        if not await self.start_claim(claim):
            return results
        source_session = await self._session()
        try:
            source_allowed = await self._refresh_source(source_session, claim)
        finally:
            await self._close(source_session)
        if source_allowed is False:
            outcome = ExecutionOutcome(classification="blocked", error_code="source_not_executable", error_message="source state changed before execution")
            settled = await self.settle(claim, outcome, source_ok=False)
            results.append({"claim": claim.to_dict(), "run": None, "outcome": outcome.normalized_classification(), "settlement": settled})
            return results
        authority_ok, authority_reason = await self._authority_allows(claim)
        if not authority_ok:
            outcome = ExecutionOutcome(classification="blocked", error_code=authority_reason or "authority_denied", error_message="current Agent authority does not permit this work")
            settled = await self.settle(claim, outcome, source_ok=True)
            results.append({"claim": claim.to_dict(), "run": None, "outcome": outcome.normalized_classification(), "settlement": settled})
            return results
        try:
            run_payload = await self.create_agent_run(claim)
        except Exception as exc:
            if getattr(exc, "code", None) == "legacy_route_snapshot_required":
                await self._settle_claimed_blocked(
                    claim, "legacy_route_snapshot_required"
                )
                results.append(
                    {
                        "claim": claim.to_dict(),
                        "run": None,
                        "outcome": "blocked",
                        "settlement": {
                            "work_item_id": claim.work_item_id,
                            "settled": True,
                            "state": "blocked",
                            "reason": "legacy_route_snapshot_required",
                        },
                    }
                )
                return results
            logger.warning("AgentRun creation failed for %s", claim.work_item_id, exc_info=True)
            outcome = ExecutionOutcome(classification="transient", error_code="agent_run_create_failed", error_message=_safe_error(exc))
            settled = await self.settle(claim, outcome)
            results.append({"claim": claim.to_dict(), "run": None, "outcome": outcome.normalized_classification(), "settlement": settled})
            return results
        if claim.assigned_agent_id and not run_payload:
            outcome = ExecutionOutcome(classification="blocked", error_code="agent_run_unavailable", error_message="durable AgentRun could not be created")
            settled = await self.settle(claim, outcome, source_ok=True)
            results.append({"claim": claim.to_dict(), "run": None, "outcome": outcome.normalized_classification(), "settlement": settled})
            return results
        if run_payload and run_payload.get("id"):
            if not await self._link_run(claim, str(run_payload["id"])):
                # The lease may have expired or been reclaimed between
                # AgentRun creation and pointer linkage. Never invoke a
                # domain/provider adapter without a durable fenced link.
                try:
                    from .agent_run_service import AgentRunService

                    await AgentRunService(
                        self._db_manager,
                        config=self.config,
                    ).fail_run(
                        str(run_payload["id"]),
                        "AgentWork lease was lost before execution started",
                        metadata={
                            "work_item_id": claim.work_item_id,
                            "classification": "uncertain",
                            "lease_lost_before_execution": True,
                        },
                    )
                except Exception:
                    logger.debug(
                        "Unable to reconcile orphaned AgentRun after lease loss",
                        exc_info=True,
                    )
                results.append(
                    {
                        "claim": claim.to_dict(),
                        "run": run_payload,
                        "outcome": "uncertain",
                        "settlement": {
                            "work_item_id": claim.work_item_id,
                            "settled": False,
                            "reason": "lease_lost_before_execution",
                        },
                    }
                )
                return results
            try:
                from .agent_run_service import AgentRunService

                await AgentRunService(
                    self._db_manager,
                    config=self.config,
                ).mark_running(str(run_payload["id"]))
            except Exception:
                logger.debug("AgentRun running event could not be recorded", exc_info=True)
        adapter_task: asyncio.Task[Any] | None = None
        try:
            required = tuple(_string(item, limit=80) for item in adapter.required_capabilities(claim))
            if required and any(item not in set(claim.required_capabilities) for item in required):
                outcome = ExecutionOutcome(classification="blocked", error_code="capability_not_declared", error_message="required capability is not declared by the work item")
            else:
                lease_stop = asyncio.Event()
                lease_failed = asyncio.Event()

                async def renew_loop() -> None:
                    interval = max(0.25, self.lease_seconds / 3.0)
                    while not lease_stop.is_set():
                        try:
                            await asyncio.wait_for(lease_stop.wait(), timeout=interval)
                        except asyncio.TimeoutError:
                            pass
                        if lease_stop.is_set():
                            return
                        if not await self.renew_lease(claim):
                            lease_failed.set()
                            abort = getattr(adapter, "abort", None) or getattr(
                                adapter, "cancel", None
                            )
                            if callable(abort):
                                try:
                                    value = abort(claim)
                                    if inspect.isawaitable(value):
                                        await value
                                except Exception:
                                    logger.debug(
                                        "AgentWork adapter abort after lease loss failed",
                                        exc_info=True,
                                    )
                            return

                renewal = asyncio.create_task(renew_loop(), name=f"agent-work-renew:{claim.work_item_id}")
                try:
                    adapter_session = await self._session()
                    adapter_task = asyncio.create_task(
                        self._run_owned_adapter(
                            adapter,
                            claim,
                            session=adapter_session,
                            run_id=(run_payload or {}).get("id")
                            if isinstance(run_payload, Mapping)
                            else None,
                            run=run_payload,
                        ),
                        name=f"agent-work-adapter:{claim.work_item_id}",
                    )
                    self._register_owned_task(
                        adapter_task,
                        adapter=adapter,
                        claim=claim,
                    )
                    while True:
                        done, _ = await asyncio.wait(
                            {adapter_task},
                            timeout=0.25,
                        )
                        if done:
                            outcome = ExecutionOutcome.from_value(
                                adapter_task.result()
                            )
                            break
                        if lease_failed.is_set():
                            # A lost/disabled lease must stop further
                            # domain work where cancellation is
                            # cooperative. Any already-issued external
                            # side effect is settled as uncertain, never
                            # retried blindly.
                            await self._cancel_owned_task(adapter_task)
                            outcome = ExecutionOutcome(
                                classification="uncertain",
                                error_code="lease_lost",
                                error_message="work lease was lost during execution",
                            )
                            break
                finally:
                    lease_stop.set()
                    renewal.cancel()
                    try:
                        await renewal
                    except asyncio.CancelledError:
                        pass
                if lease_failed.is_set() and outcome.normalized_classification() == "succeeded":
                    # A lost lease invalidates the result; never let a
                    # stale worker publish success after another worker
                    # has reclaimed the item.
                    outcome = ExecutionOutcome(classification="uncertain", error_code="lease_lost", error_message="work lease was lost during execution")
        except asyncio.CancelledError:
            if adapter_task is not None and not adapter_task.done():
                await self._cancel_owned_task(adapter_task)
            outcome = ExecutionOutcome(
                classification="uncertain" if run_payload else "transient",
                error_code="execution_cancelled",
                error_message=(
                    "execution outcome is unknown after cancellation"
                    if run_payload
                    else "execution cancelled before adapter invocation"
                ),
            )
        except Exception as exc:
            logger.warning("AgentWork adapter failed for %s", claim.work_item_id, exc_info=True)
            outcome = ExecutionOutcome(classification="transient", error_code="adapter_error", error_message=_safe_error(exc))
        settled = await self.settle(claim, outcome)
        # A failed token-fenced settlement means this worker no longer
        # owns the work item (another worker may have reclaimed it, or a
        # database failure prevented the transition).  Do not
        # terminalize the attempt from a stale worker; leave the run for
        # recovery/reconciliation instead of publishing a false success
        # or failure after lease loss.
        settlement_owned = isinstance(settled, Mapping) and settled.get("settled", True) is not False
        if settlement_owned and run_payload and run_payload.get("id"):
            try:
                from .agent_run_service import AgentRunService
                route_metadata, route_kwargs = _outcome_model_route_audit(outcome)
                if outcome.normalized_classification() == "succeeded":
                    await AgentRunService(
                        self._db_manager,
                        config=self.config,
                    ).complete_run(str(run_payload["id"]), result=dict(outcome.result or {}), message=outcome.result_summary, metadata={"work_item_id": claim.work_item_id, "classification": outcome.normalized_classification(), "domain_ref": outcome.domain_ref, "domain_status": outcome.domain_status, "usage": dict(outcome.usage or {}), **route_metadata}, **route_kwargs)
                elif outcome.normalized_classification() in {"permanent", "blocked", "cancelled", "transient", "awaiting_approval", "uncertain"}:
                    classification = outcome.normalized_classification()
                    await AgentRunService(
                        self._db_manager,
                        config=self.config,
                    ).fail_run(
                        str(run_payload["id"]),
                        outcome.error_message or outcome.error_code or "work failed",
                        # Keep the adapter's terminal payload (e.g. the
                        # Agent's last answer) on the failed Run.
                        result=dict(outcome.result) if outcome.result else None,
                        status=(
                            "cancelled"
                            if classification == "cancelled"
                            else "awaiting_approval"
                            if classification == "awaiting_approval"
                            else "failed"
                        ),
                        metadata={
                            "work_item_id": claim.work_item_id,
                            "classification": classification,
                            "domain_ref": outcome.domain_ref,
                            "domain_status": outcome.domain_status,
                            # Known prior usage is already persisted on the Run;
                            # re-sending it would merge it twice.
                            **(
                                {"usage": dict(outcome.metadata["unpersisted_usage"])}
                                if isinstance(outcome.metadata.get("unpersisted_usage"), Mapping)
                                else {} if outcome.metadata.get("usage_state") == "known"
                                else {"usage": dict(outcome.usage or {})}
                            ),
                            **terminal_failure_event_fields(outcome.metadata),
                            **route_metadata,
                        },
                        **route_kwargs,
                    )
            except Exception:
                logger.debug("AgentRun settlement event could not be recorded", exc_info=True)
        if settlement_owned and outcome.normalized_classification() in {
            "succeeded", "permanent", "blocked", "cancelled", "uncertain"
        }:
            try:
                adapter_close_task = (
                    self._adapter_close_tasks.get(adapter_task)
                    if adapter_task is not None
                    else None
                )
                if adapter_task is not None and (
                    not adapter_task.done()
                    or (
                        adapter_close_task is not None
                        and not adapter_close_task.done()
                    )
                ):
                    self._defer_adapter_cleanup(adapter_task, adapter, claim)
                else:
                    await self._run_adapter_cleanup(adapter, claim)
            except Exception:
                logger.warning("AgentWork adapter cleanup failed for %s", claim.work_item_id, exc_info=True)
        results.append({"claim": claim.to_dict(), "run": run_payload, "outcome": outcome.normalized_classification(), "settlement": settled})
        return results

    async def _link_run(self, claim: WorkClaim, run_id: str) -> bool:
        session = await self._session()
        try:
            now = self._now()
            result = await session.execute(
                update(AgentWorkItem)
                .where(
                    AgentWorkItem.id == _as_uuid(claim.work_item_id),
                    AgentWorkItem.state.in_(tuple(WORK_ACTIVE_STATES)),
                    AgentWorkItem.lease_owner == claim.lease_owner,
                    AgentWorkItem.lease_token == claim.lease_token,
                    AgentWorkItem.lease_expires_at > now,
                )
                .values(
                    active_agent_run_id=_as_uuid(run_id),
                    updated_at=now,
                )
            )
            if int(getattr(result, "rowcount", 0) or 0) != 1:
                await session.rollback()
                return False
            await session.commit()
            return True
        except Exception:
            await self._rollback(session)
            return False
        finally:
            await self._close(session)

    async def _settle_claimed_blocked(self, claim: WorkClaim, code: str) -> None:
        """Record a blocked claim that never entered execution."""

        session = await self._session()
        try:
            row = await session.get(AgentWorkItem, _as_uuid(claim.work_item_id))
            if row is None or row.lease_token != claim.lease_token:
                await session.rollback()
                return
            previous = str(row.state or "claimed")
            row.state = "blocked"
            row.outcome_classification = "blocked"
            row.safe_error_code = code
            row.blocker_code = code
            row.lease_owner = row.lease_token = row.lease_expires_at = None
            if isinstance(row.budget_reservation_json, Mapping) and row.budget_reservation_json:
                row.budget_reservation_json = self._settle_budget(
                    dict(row.budget_reservation_json),
                    ExecutionOutcome(classification="blocked"),
                    state="blocked",
                )
            row.updated_at = self._now()
            await self._append_event_in_session(session, row, event_type="work.blocked", actor_kind="service", actor_id=self.worker_id, from_state=previous, to_state="blocked", safe_error_code=code, payload={"error_code": code})
            await session.commit()
        except Exception:
            await self._rollback(session)
        finally:
            await self._close(session)

    def _poll_task_done(self, task: asyncio.Task[Any]) -> None:
        if self._poll_task is task:
            self._poll_task = None

    def _has_pending_owned_tasks(self) -> bool:
        return any(
            not task.done()
            for task in self._running_tasks | self._protected_tasks
        )

    async def start(self, *, poll_interval_seconds: float = 5.0) -> None:
        if not self._runtime_enabled():
            return
        previous = self._poll_task
        if previous is not None:
            if not previous.done():
                return
            self._release_owned_task(previous)
            if self._poll_task is previous:
                self._poll_task = None
        if self._has_pending_owned_tasks():
            return
        self._stop_event.clear()
        task = asyncio.create_task(
            self._poll_loop(max(0.05, float(poll_interval_seconds))),
            name="agent-work-coordinator",
        )
        self._poll_task = task
        self._register_owned_task(task)
        task.add_done_callback(self._poll_task_done)

    async def stop(self, *, timeout_seconds: float | None = None) -> None:
        """Stop polling and drain owned adapter tasks within a finite budget."""

        self._stop_event.set()
        poll_task = self._poll_task
        if poll_task is not None and poll_task not in self._running_tasks:
            self._register_owned_task(poll_task)
        if timeout_seconds is None:
            timeout = self.shutdown_timeout_seconds
        else:
            try:
                timeout = float(timeout_seconds)
            except (TypeError, ValueError):
                timeout = self.shutdown_timeout_seconds
            if not math.isfinite(timeout):
                timeout = self.shutdown_timeout_seconds
            timeout = max(0.0, min(timeout, MAX_SHUTDOWN_TIMEOUT_SECONDS))
        deadline = asyncio.get_running_loop().time() + timeout

        running = [task for task in self._running_tasks if not task.done()]
        owned = set(running)
        if poll_task is not None and not poll_task.done():
            owned.add(poll_task)
        # Request cancellation for every child before awaiting any one of
        # them.  This propagates to native worker cancel_event handlers and
        # prevents a slow first cleanup from delaying the shutdown signal for
        # another adapter.
        for task in owned:
            if task not in self._protected_tasks:
                task.cancel()

        pending: set[asyncio.Task[Any]] = set()
        if owned:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            _done, pending = await asyncio.wait(owned, timeout=remaining)
            for task in _done:
                _consume_async_task(task)
                self._release_owned_task(task)
            for task in pending:
                task.add_done_callback(_consume_async_task)

        # Cancellation may make the poll task create one final shielded
        # cleanup/session-close child.  Give those children the same remaining
        # bounded drain window, without ever extending the caller's deadline.
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        if remaining:
            late = {
                task for task in self._running_tasks if not task.done()
            }
            if late:
                _done, late_pending = await asyncio.wait(late, timeout=remaining)
                for task in _done:
                    _consume_async_task(task)
                    self._release_owned_task(task)
                for task in late_pending:
                    task.add_done_callback(_consume_async_task)

        # Cooperative adapters release themselves from their own finally
        # blocks.  Keep pending tasks in the ledger after a bounded timeout so
        # a later stop can observe them and late exceptions remain consumed.
        for task in list(self._running_tasks):
            if task.done():
                self._release_owned_task(task)
                if self._poll_task is task:
                    self._poll_task = None

    async def _poll_loop(self, interval: float) -> None:
        first_poll = True
        while not self._stop_event.is_set():
            if not self._runtime_enabled():
                return
            try:
                await self._dispatch_once(limit=32 if self.max_concurrency is None else self.max_concurrency)
                if first_poll:
                    logger.info(
                        "共通AgentWork coordinatorの初回ポーリングが完了しました: worker_id=%s",
                        self.worker_id,
                    )
                    first_poll = False
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("AgentWork coordinator tick failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def snapshot(self, *, limit: int = 100) -> dict[str, Any]:
        session = await self._session()
        try:
            stmt = select(AgentWorkItem).order_by(desc(AgentWorkItem.updated_at)).limit(max(1, min(int(limit), 500)))
            rows = (await session.execute(stmt)).scalars().all()
            counts: dict[str, int] = {}
            for row in rows:
                counts[str(row.state)] = counts.get(str(row.state), 0) + 1
            return {"enabled": self._runtime_enabled(), "worker_id": self.worker_id, "counts": counts, "work_items": [row.to_safe_dict() for row in rows]}
        finally:
            await self._close(session)

    async def record_event(
        self,
        event: Mapping[str, Any] | None = None,
        *,
        payload: Mapping[str, Any] | None = None,
        claim: WorkClaim | Mapping[str, Any] | None = None,
        event_type: str | None = None,
    ) -> dict[str, Any] | None:
        """Append bounded runner evidence to the canonical WorkEvent ledger."""

        raw = dict(event or payload or {})
        normalized_claim = self._coerce_claim(claim) if claim is not None else None
        work_item_id = (
            normalized_claim.work_item_id
            if normalized_claim is not None
            else raw.get("work_item_id") or raw.get("id")
        )
        if not work_item_id:
            return None
        session = await self._session()
        try:
            row = await session.get(AgentWorkItem, _as_uuid(work_item_id))
            if row is None:
                await session.rollback()
                return None
            item = await self._append_event_in_session(
                session,
                row,
                event_type=event_type or str(raw.get("event") or raw.get("event_type") or "runner.event"),
                actor_kind="service",
                actor_id=self.worker_id,
                agent_run_id=getattr(row, "active_agent_run_id", None),
                causation_id=row.causation_id,
                causal_depth=int(row.causal_depth or 0),
                payload=raw,
            )
            await session.commit()
            return item.to_safe_dict()
        except Exception:
            await self._rollback(session)
            return None
        finally:
            await self._close(session)

    record_work_event = record_event
    append_event = record_event

    async def _refresh_source(self, session: Any, claim: WorkClaim) -> bool | None:
        source = self.sources.get(claim.source_type)
        if source is None or not callable(getattr(source, "refresh", None)):
            # A missing source adapter means current source state cannot be
            # revalidated; never settle an old snapshot as success.
            return False
        try:
            value = await _maybe_await(source.refresh(session, claim))
            if isinstance(value, Mapping):
                return bool(value.get("allowed", value.get("refreshed", value.get("ok", False))))
            return bool(value)
        except Exception:
            logger.warning("WorkSource refresh failed for %s", claim.work_item_id, exc_info=True)
            return False

    async def _authority_allows(self, claim: WorkClaim) -> tuple[bool, str | None]:
        """Resolve every required capability against fresh durable grants."""

        if not claim.assigned_agent_id:
            return True, None
        try:
            from .agent_authority import AgentAuthorityResolver

            resolver = AgentAuthorityResolver(self._db_manager, config=self.config)
            requested = list(claim.required_capabilities) or [None]
            for capability in requested:
                decision = await resolver.resolve(
                    agent_id=claim.assigned_agent_id,
                    revision_id=claim.agent_revision_id,
                    project_id=claim.project_id,
                    space_id=claim.space_id,
                    persona_id=claim.persona_id,
                    required_capability=capability,
                    tool_capabilities=claim.required_capabilities or None,
                    harness_capabilities=claim.required_capabilities or None,
                )
                if not decision.allowed:
                    return False, decision.reason or "authority_denied"
            return True, None
        except Exception:
            logger.warning("Agent authority resolution failed for %s", claim.work_item_id, exc_info=True)
            return False, "authority_unavailable"

    async def _invoke_adapter(
        self,
        adapter: ExecutionAdapter,
        claim: WorkClaim,
        *,
        session: Any,
        run_id: str | None = None,
        run: Mapping[str, Any] | None = None,
    ) -> Any:
        method = getattr(adapter, "execute", None)
        if not callable(method):
            raise RuntimeError("execution adapter has no execute method")
        kwargs: dict[str, Any] = {
            "coordinator": self,
            "session": session,
            "actor": self.execution_actor,
            "attempt": claim.attempt,
            "run_id": run_id,
            "run": run,
        }
        # Only this server-owned development adapter receives a capability
        # issuer. Other marketplace adapters keep their existing contract.
        is_crowdworks_development = False
        if getattr(adapter, "adapter_key", None) in {
            "crowdworks_development_web_development", "crowdworks_development_app_development",
            "crowdworks_fulfillment_code",
        }:
            from .marketplace.client_development import CrowdWorksDevelopmentAdapter

            is_crowdworks_development = isinstance(adapter, CrowdWorksDevelopmentAdapter)
        if is_crowdworks_development:
            if claim.execution_adapter != adapter.adapter_key:
                raise RuntimeError("CrowdWorks adapter does not match the claimed execution adapter")
            scope_run = {
                "id": run.get("id"),
                "execution_manifest_hash": run.get("execution_manifest_hash"),
            } if isinstance(run, Mapping) else None

            async def harness_scope_factory(*, workspace: Any, resource_limits: Any, mode: str = "workspace_write") -> Any:
                return await self._issue_crowdworks_claim_scope(
                    claim, run_id=run_id, run=scope_run, workspace=workspace,
                    resource_limits=resource_limits, mode=mode,
                )

            kwargs["harness_scope_factory"] = harness_scope_factory
        try:
            signature = inspect.signature(method)
            params = signature.parameters
            if not any(item.kind is inspect.Parameter.VAR_KEYWORD for item in params.values()):
                kwargs = {key: value for key, value in kwargs.items() if key in params}
        except (TypeError, ValueError):
            pass
        return await _maybe_await(method(claim, **kwargs))

    async def _issue_crowdworks_claim_scope(
        self, claim: WorkClaim, *, run_id: str | None,
        run: Mapping[str, Any] | None, workspace: Any,
        resource_limits: Any, mode: str,
    ) -> Any:
        """Issue a scope only for a live, pinned, freshly authorized CW claim."""
        from pathlib import Path

        from .agent_authority import AgentAuthorityResolver
        from ..memory.models import AgentProjectGrant
        from ..memory.models.operations import EngagementOpportunity
        from .project_permissions import normalize_project_member_permissions
        from .harness_execution_scope_service import (
            HarnessExecutionScopeServiceError,
            _config_get,
            _path_has_any_link_or_reparse,
            build_agent_work_harness_scope,
        )
        from .marketplace.client_development import SOURCE_DIRNAME, client_workspace_root, development_source_revision
        from .marketplace.crowdworks_specialists import DEVELOPMENT_CAPABILITIES, DEVELOPMENT_WORKER_KINDS, normalize_worker_kind

        def deny(reason: str) -> None:
            raise HarnessExecutionScopeServiceError(f"CrowdWorks claim execution denied: {reason}")

        canonical_run_id = _as_uuid(run_id)
        if (
            not claim.assigned_agent_id or not claim.agent_revision_id
            or not claim.project_id or canonical_run_id is None
            or not isinstance(run, Mapping) or _as_uuid(run.get("id")) != canonical_run_id
            or claim.source_type != "marketplace_crowdworks_fulfillment" or claim.domain != "marketplace"
            or not set(DEVELOPMENT_CAPABILITIES).issubset(claim.required_capabilities)
        ):
            deny("claim_identity_missing")
        # The prepared workspace is only a proposed path. Reconstruct its
        # exact deterministic server namespace, ignoring model/metadata roots.
        source_uuid = _as_uuid(claim.source_id)
        if source_uuid is None:
            deny("invalid_source")
        configured_root = os.environ.get("AOITALK_CLIENT_WORKSPACES_DIR") or _config_get(
            self.config, "client_development.workspace_root"
        )
        lexical_root = Path(str(configured_root)).expanduser().absolute() if configured_root else (
            Path(os.environ.get("AOITALK_WORKSPACES_DIR", "./workspaces")).expanduser().absolute()
            / "_client_engagements"
        )
        if _path_has_any_link_or_reparse(lexical_root):
            deny("workspace_root_link")
        root = client_workspace_root(self.config)
        expected_root = root / f"opportunity_{source_uuid}"
        expected_source = expected_root / SOURCE_DIRNAME
        try:
            proposed_root = Path(workspace.root).absolute()
            proposed_source = Path(workspace.source).absolute()
            if (
                _path_has_any_link_or_reparse(proposed_root)
                or _path_has_any_link_or_reparse(proposed_source)
                or _path_has_any_link_or_reparse(expected_source)
                or proposed_root.resolve() != expected_root.resolve()
                or proposed_source.resolve() != expected_source.resolve()
            ):
                deny("workspace_identity_mismatch")
        except HarnessExecutionScopeServiceError:
            raise
        except (AttributeError, TypeError, ValueError, OSError) as exc:
            raise HarnessExecutionScopeServiceError("CrowdWorks workspace is unavailable") from exc

        resolver = AgentAuthorityResolver(self._db_manager, config=self.config)
        identity = None
        for capability in claim.required_capabilities:
            decision = await resolver.resolve(
                agent_id=claim.assigned_agent_id, revision_id=claim.agent_revision_id,
                project_id=claim.project_id, space_id=claim.space_id,
                persona_id=claim.persona_id, required_capability=capability,
                tool_capabilities=claim.required_capabilities,
                harness_capabilities=claim.required_capabilities,
            )
            if not decision.allowed:
                deny(decision.reason or "authority_denied")
            current_identity = (
                decision.agent_id, decision.agent_revision_id, decision.organization_id,
                decision.project_id, decision.space_id, decision.persona_id,
            )
            if (
                decision.agent_id != claim.assigned_agent_id
                or decision.agent_revision_id != claim.agent_revision_id
                or decision.project_id != claim.project_id or not decision.organization_id
                or (claim.space_id and decision.space_id != claim.space_id)
                or decision.persona_id != claim.persona_id
                or (identity is not None and identity != current_identity)
            ):
                deny("authority_identity_mismatch")
            identity = current_identity

        # Read through a new short session after authorization so an adapter's
        # cached ORM objects cannot conceal a changed lease/run/assignment.
        fresh_session = await self._session()
        try:
            item = await fresh_session.get(AgentWorkItem, _as_uuid(claim.work_item_id))
            agent_run = await fresh_session.get(AgentRun, canonical_run_id)
            now = _now_utc()
            if (
                item is None or agent_run is None or item.state != "running"
                or item.lease_token != claim.lease_token or item.lease_owner != claim.lease_owner
                or not item.lease_expires_at or item.lease_expires_at <= now
                or item.attempt_count != claim.attempt
                or item.active_agent_run_id != canonical_run_id
                or agent_run.status != "running" or agent_run.ended_at is not None
                or agent_run.work_item_id != item.id or agent_run.work_item_attempt != claim.attempt
            ):
                deny("lease_or_run_lost")
            for name in ("assigned_agent_id", "agent_revision_id", "project_id", "space_id", "persona_id", "app_id", "task_id"):
                if getattr(item, name) != _as_uuid(getattr(claim, name)):
                    deny("durable_claim_identity_mismatch")
            for name, expected in (
                ("agent_id", claim.assigned_agent_id), ("agent_revision_id", claim.agent_revision_id),
                ("project_id", claim.project_id), ("task_id", claim.task_id), ("app_id", claim.app_id),
            ):
                if getattr(agent_run, name) != _as_uuid(expected):
                    deny("durable_run_identity_mismatch")
            if (
                item.source_type != claim.source_type or item.source_id != claim.source_id
                or item.source_revision != claim.source_revision or item.intent_key != claim.intent_key
                or item.execution_adapter != claim.execution_adapter
                or set(item.required_capabilities_json or ()) != set(claim.required_capabilities)
            ):
                deny("durable_claim_scope_mismatch")
            manifest = agent_run.resolved_execution_manifest
            if not isinstance(manifest, dict) or not isinstance(manifest.get("route_snapshot"), dict):
                deny("execution_manifest_missing")
            for name, expected in (
                ("agent_id", claim.assigned_agent_id), ("agent_revision_id", claim.agent_revision_id),
                ("project_id", claim.project_id), ("work_item_id", claim.work_item_id),
            ):
                if manifest.get(name) != expected:
                    deny("execution_manifest_identity_mismatch")
            if set(manifest.get("capabilities") or ()) != set(claim.required_capabilities):
                deny("execution_manifest_capabilities_mismatch")
            manifest_hash = hashlib.sha256(
                json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            if (
                manifest_hash != agent_run.execution_manifest_hash
                or agent_run.execution_manifest_hash != run.get("execution_manifest_hash")
            ):
                deny("execution_manifest_hash_mismatch")
            opportunity = await fresh_session.get(EngagementOpportunity, source_uuid)
            if (
                opportunity is None or opportunity.project_id != _as_uuid(claim.project_id)
                or opportunity.status != "contract_accepted"
            ):
                deny("opportunity_identity_mismatch")
            metadata = opportunity.metadata_json
            if not isinstance(metadata, dict):
                deny("opportunity_identity_mismatch")
            kind = normalize_worker_kind(metadata.get("worker_kind"))
            expected_adapters = {f"crowdworks_development_{kind}"}
            if kind == "app_development":
                expected_adapters.add("crowdworks_fulfillment_code")
            if (
                kind not in DEVELOPMENT_WORKER_KINDS
                or claim.execution_adapter not in expected_adapters
                or development_source_revision(opportunity, kind, agent_revision_id=claim.agent_revision_id)
                != claim.source_revision
            ):
                deny("opportunity_source_revision_changed")
            grant = await fresh_session.scalar(select(AgentProjectGrant).where(
                AgentProjectGrant.agent_id == _as_uuid(claim.assigned_agent_id),
                AgentProjectGrant.project_id == _as_uuid(claim.project_id),
                AgentProjectGrant.state == "active",
            ))
            if (
                grant is None or (grant.active_from and grant.active_from > now)
                or (grant.active_until and grant.active_until < now)
            ):
                deny("project_grant_missing")
            permissions = normalize_project_member_permissions(grant.permissions)
            if permissions.get("read") is not True or permissions.get("write") is not True:
                deny("project_permission_denied")
            delete_workspace = permissions.get("delete") is True
        finally:
            await self._close(fresh_session)

        return build_agent_work_harness_scope(
            workspace_path=expected_source, harness_workspace_root=root,
            resource_limits=resource_limits, agent_id=identity[0], organization_id=identity[2],
            project_id=identity[3], app_id=claim.app_id, run_id=str(canonical_run_id),
            audit_id=f"agent-work:{claim.work_item_id}:{claim.attempt}:{mode}", mode=mode,
            delete_workspace=delete_workspace,
        )

    @staticmethod
    def _coerce_claim(value: WorkClaim | Mapping[str, Any]) -> WorkClaim:
        if isinstance(value, WorkClaim):
            return value
        raw = dict(value)
        return WorkClaim(
            work_item_id=str(raw.get("work_item_id") or raw.get("id")),
            lease_token=str(raw.get("lease_token") or raw.get("token") or ""),
            lease_owner=str(raw.get("lease_owner") or raw.get("owner") or ""),
            attempt=int(raw.get("attempt") or 0),
            source_type=str(raw.get("source_type") or ""),
            source_id=str(raw.get("source_id") or ""),
            source_revision=str(raw.get("source_revision") or "0"),
            intent_key=str(raw.get("intent_key") or ""),
            domain=str(raw.get("domain") or "internal"),
            assigned_agent_id=raw.get("assigned_agent_id"),
            agent_revision_id=raw.get("agent_revision_id"),
            execution_adapter=str(raw.get("execution_adapter") or "default"),
            required_capabilities=tuple(raw.get("required_capabilities") or ()),
            project_id=raw.get("project_id"),
            task_id=raw.get("task_id"),
            persona_id=raw.get("persona_id"),
            conversation_session_id=raw.get("conversation_session_id"),
            concurrency_key=raw.get("concurrency_key"),
            budget_reservation=raw.get("budget_reservation") if isinstance(raw.get("budget_reservation"), Mapping) else {},
            space_id=raw.get("space_id"),
            app_id=raw.get("app_id"),
            causal_depth=int(raw.get("causal_depth") or 0),
            mutation_fingerprint=raw.get("mutation_fingerprint"),
            metadata=raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {},
        )

    def _retry_delay(self, claim: WorkClaim, outcome: ExecutionOutcome) -> float:
        if outcome.retry_after_seconds is not None:
            try:
                return max(1.0, min(float(outcome.retry_after_seconds), 86_400.0))
            except (TypeError, ValueError):
                pass
        # Stable jitter per source/attempt avoids a thundering herd while
        # remaining deterministic across process restarts.
        digest = hashlib.sha256(f"{claim.work_item_id}:{claim.attempt}".encode()).digest()
        jitter = int.from_bytes(digest[:2], "big") / 65535.0
        base = min(86_400.0, 2.0 ** max(0, claim.attempt - 1))
        return max(1.0, base + jitter)

    async def _transition_locked(self, session: Any, row: AgentWorkItem, state: str, *, token: str | None, error_code: str, error_message: str) -> None:
        previous_state = str(row.state or "")
        active_run_id = getattr(row, "active_agent_run_id", None)
        transition_time = self._now()
        row.state = state
        row.lease_owner = None
        row.lease_token = None
        row.lease_expires_at = None
        if state != "running":
            row.active_agent_run_id = None
        row.updated_at = transition_time
        row.safe_error_code = error_code
        row.blocker_code = error_code if state == "blocked" else None
        row.outcome_classification = "permanent" if state == "dead_letter" else state
        row.escalation_reason = _safe_error(error_message)
        if state in WORK_TERMINAL_STATES or state in {
            "blocked",
            "uncertain",
            "awaiting_approval",
        }:
            row.completed_at = transition_time
        if state == "cancelled":
            row.cancelled_at = transition_time
        if state in {"blocked", "failed", "cancelled", "dead_letter"} and isinstance(
            row.budget_reservation_json, Mapping
        ) and row.budget_reservation_json:
            row.budget_reservation_json = self._settle_budget(
                dict(row.budget_reservation_json),
                ExecutionOutcome(classification="blocked" if state == "blocked" else "permanent"),
                state=state,
            )
        await self._append_event_in_session(
            session,
            row,
            event_type=f"work.{state}",
            actor_kind="service",
            actor_id=self.worker_id,
            agent_run_id=active_run_id,
            from_state=previous_state,
            to_state=state,
            causation_id=row.causation_id,
            causal_depth=int(row.causal_depth or 0),
            mutation_fingerprint=None,
            safe_error_code=error_code,
            payload={"error_code": error_code},
        )

    async def _append_event_in_session(
        self,
        session: Any,
        row: AgentWorkItem,
        *,
        event_type: str,
        actor_kind: str,
        actor_id: str | None,
        agent_run_id: Any | None = None,
        from_state: str | None = None,
        to_state: str | None = None,
        causation_id: str | None = None,
        causal_depth: int = 0,
        mutation_fingerprint: str | None = None,
        safe_error_code: str | None = None,
        result_summary: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> AgentWorkEvent:
        # Lock the parent before allocating the next sequence so concurrent
        # workers cannot append duplicate sequence numbers on PostgreSQL.
        try:
            await session.execute(
                select(AgentWorkItem.id).where(AgentWorkItem.id == row.id).with_for_update()
            )
        except Exception:
            # Tiny fake sessions and SQLite do not expose row locks; the
            # unique sequence constraint/CAS still protects the write path.
            pass
        result = await session.execute(select(func.max(AgentWorkEvent.sequence)).where(AgentWorkEvent.work_item_id == row.id))
        max_sequence = result.scalar_one() if hasattr(result, "scalar_one") else result.scalar()
        event = AgentWorkEvent(
            id=uuid.uuid4(),
            work_item_id=row.id,
            sequence=int(max_sequence or 0) + 1,
            event_type=_string(event_type, limit=80),
            from_state=_string(from_state, limit=32) or None,
            to_state=_string(to_state, limit=32) or None,
            actor_kind=_string(actor_kind, limit=16) or "service",
            actor_id=_string(actor_id, limit=160) or None,
            actor_user_id=_as_uuid(actor_id) if actor_kind == "human" else None,
            actor_agent_id=_as_uuid(actor_id) if actor_kind == "agent" else None,
            actor_service_key=(
                _string(actor_id, limit=128)
                if actor_kind == "service" and str(actor_id or "") in {
                    "aoitalk.system",
                    "aoitalk.agent-harness",
                    "aoitalk.migrations",
                    "aoitalk.media-adapter",
                }
                else ("aoitalk.system" if actor_kind == "service" else None)
            ),
            agent_run_id=_as_uuid(agent_run_id),
            causation_id=_string(causation_id, limit=255) or None,
            causal_depth=max(0, min(int(causal_depth or 0), MAX_CAUSAL_DEPTH)),
            mutation_fingerprint=_string(mutation_fingerprint, limit=64) or None,
            safe_error_code=_safe_error(safe_error_code, limit=96),
            result_summary=_safe_error(result_summary, limit=500),
            payload_json=_safe_json(payload or {}),
            message=_safe_error((payload or {}).get("message")) if isinstance(payload, Mapping) else None,
            created_at=self._now(),
        )
        session.add(event)
        return event


def _is_transaction_deadlock(exc: BaseException) -> bool:
    """Return whether an exception chain is a PostgreSQL deadlock/serialization abort."""

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for candidate in (current, getattr(current, "orig", None)):
            if candidate is None:
                continue
            state = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
            if str(state or "") in {"40P01", "40001"}:
                return True
            if type(candidate).__name__ in {"DeadlockDetectedError", "SerializationError"}:
                return True
        current = current.__cause__ or current.__context__
    return False


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


# Compatibility aliases used by early WS02 callers.
AgentWorkRuntime = AgentWorkCoordinator
WorkCoordinator = AgentWorkCoordinator

__all__ = [
    "WORK_STATES",
    "WORK_TERMINAL_STATES",
    "OUTCOME_CLASSIFICATIONS",
    "WorkCandidate",
    "WorkClaim",
    "ExecutionOutcome",
    "InteractiveAdmissionError",
    "WorkSource",
    "ExecutionAdapter",
    "AgentWorkCoordinator",
    "AgentWorkRuntime",
    "WorkCoordinator",
    "canonical_work_json",
    "work_mutation_fingerprint",
]
