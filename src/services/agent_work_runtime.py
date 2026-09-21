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
import socket
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from sqlalchemy import and_, desc, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError

from ..features import Features
from ..memory.models import Agent, AgentRun, AgentWorkEvent, AgentWorkItem, ExternalAction

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
            if any(marker in normalized for marker in _SECRET_KEY_MARKERS):
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
        max_concurrency: int = 2,
        max_attempts: int = 3,
        clock: Callable[[], datetime] | None = None,
        enabled: bool | None = None,
        config: Any | None = None,
        execution_actor: Any | None = None,
        task_executor: Callable[..., Any] | None = None,
    ) -> None:
        self._db_manager = db_manager
        self.worker_id = _string(worker_id, limit=120) or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.max_concurrency = max(1, min(int(max_concurrency), 128))
        self.max_attempts = max(1, min(int(max_attempts), MAX_ATTEMPTS))
        self._clock = clock or _now_utc
        self.config = config
        self.execution_actor = execution_actor
        # Optional deployment-provided callback for canonical Task mutations.
        # The coordinator never invents a User identity; without this trusted
        # service adapter TaskExecutionAdapter fails closed.
        self.task_executor = task_executor
        # An explicit constructor override may disable the runtime for tests
        # or maintenance, but can never enable it against the global feature
        # profile (especially Enterprise fail-closed profiles).
        self.enabled = Features.autonomous_agent_runtime() and enabled is not False
        self.sources: dict[str, WorkSource] = {}
        self.adapters: dict[str, ExecutionAdapter] = {}
        self._poll_task: asyncio.Task[Any] | None = None
        self._stop_event = asyncio.Event()
        self._running_tasks: set[asyncio.Task[Any]] = set()

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
            # Refresh mutable scheduling metadata, but never resurrect terminal
            # work or overwrite an active lease from a discovery replay.
            if row.state not in WORK_TERMINAL_STATES and row.state not in WORK_ACTIVE_STATES:
                row.source_revision = normalized.source_revision
                row.not_before = normalized.not_before
                row.deadline = normalized.deadline
                row.priority = normalized.priority
                row.required_capabilities_json = list(normalized.required_capabilities)
                row.metadata_json = _safe_json(normalized.metadata)
                row.domain = normalized.domain
                row.space_id = _as_uuid(normalized.space_id)
                row.project_id = _as_uuid(normalized.project_id)
                row.task_id = _as_uuid(normalized.task_id)
                row.persona_id = _as_uuid(normalized.persona_id)
                row.app_id = _as_uuid(normalized.app_id)
                row.assigned_agent_id = _as_uuid(normalized.assigned_agent_id)
                row.agent_revision_id = _as_uuid(normalized.agent_revision_id)
                row.execution_adapter = normalized.execution_adapter
                row.max_attempts = normalized.max_attempts
                row.concurrency_key = normalized.concurrency_key
                row.mutation_fingerprint = normalized.mutation_fingerprint
                row.updated_at = self._now()
            return row
        row = AgentWorkItem(
            id=uuid.uuid4(),
            **values,
            domain=normalized.domain,
            space_id=_as_uuid(normalized.space_id),
            project_id=_as_uuid(normalized.project_id),
            task_id=_as_uuid(normalized.task_id),
            persona_id=_as_uuid(normalized.persona_id),
            app_id=_as_uuid(normalized.app_id),
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
            metadata_json=_safe_json(normalized.metadata or {}),
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

    # Repository-natural aliases used by WorkSource adapters during the
    # rolling WS02 migration.  All point to the same durable idempotency path.
    materialize_work_item = materialize
    ensure_work_item = materialize
    upsert_work_item = materialize

    async def claim_one(self) -> WorkClaim | None:
        claims = await self.claim_many(1)
        return claims[0] if claims else None

    async def claim_many(self, limit: int) -> list[WorkClaim]:
        if not self._runtime_enabled():
            return []
        count = max(1, min(int(limit or 1), self.max_concurrency))
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
            if await self._active_count(session) >= self.max_concurrency:
                await session.commit()
                return []
            # Do not leave due work silently stranded once its hard deadline
            # has elapsed.  Project an explicit blocker/dead-letter event so
            # operators can see why it was not claimed and budget reservations
            # are released through the same transition helper.
            expired_result = await session.execute(
                select(AgentWorkItem)
                .where(
                    AgentWorkItem.state.in_(("pending", "retry_wait")),
                    AgentWorkItem.deadline.is_not(None),
                    AgentWorkItem.deadline <= now,
                )
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
            stmt = (
                select(AgentWorkItem)
                .where(eligible, or_(AgentWorkItem.deadline.is_(None), AgentWorkItem.deadline > now))
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
            active_key_result = await session.execute(
                select(AgentWorkItem.concurrency_key).where(
                    AgentWorkItem.state.in_(tuple(WORK_ACTIVE_STATES)),
                    AgentWorkItem.concurrency_key.is_not(None),
                )
            )
            active_keys = {str(value) for value in active_key_result.scalars().all() if value}
            claims: list[WorkClaim] = []
            for row in rows:
                if await self._active_count(session) >= self.max_concurrency:
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

    async def _active_count(self, session: Any) -> int:
        result = await session.execute(select(func.count(AgentWorkItem.id)).where(AgentWorkItem.state.in_(tuple(WORK_ACTIVE_STATES))))
        value = result.scalar_one() if hasattr(result, "scalar_one") else result.scalar()
        return int(value or 0)

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
    ) -> dict[str, Any] | None:
        """Settle a claim with a token-fenced transition and append evidence."""

        normalized = self._coerce_claim(claim)
        result = ExecutionOutcome.from_value(outcome)
        classification = result.normalized_classification()
        if not self._runtime_enabled():
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
                    "usage": _safe_json(result.usage or {}),
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
            try:
                locked = await session.execute(
                    select(AgentWorkItem)
                    .where(AgentWorkItem.id == _as_uuid(work_item_id))
                    .with_for_update()
                )
                row = locked.scalars().first()
            except Exception:
                row = await session.get(AgentWorkItem, _as_uuid(work_item_id))
            if row is None:
                await session.rollback()
                return False
            if row.state == "cancelled":
                await session.commit()
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
            previous = str(row.state or "")
            row.state = "cancelled"
            row.outcome_classification = "cancelled"
            row.cancelled_at = self._now()
            row.lease_owner = None
            row.lease_token = None
            row.lease_expires_at = None
            row.updated_at = self._now()
            row.safe_error_code = "cancelled"
            if isinstance(row.budget_reservation_json, Mapping) and row.budget_reservation_json:
                row.budget_reservation_json = self._settle_budget(
                    dict(row.budget_reservation_json),
                    ExecutionOutcome(classification="cancelled"),
                    state="cancelled",
                )
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
            await self._append_event_in_session(session, row, event_type="work.cancelled", actor_kind=actor_kind, actor_id=actor_id, from_state=previous, to_state="cancelled", payload={"reason": _safe_error(reason, limit=200)})
            await session.commit()
            return True
        except Exception:
            await self._rollback(session)
            return False
        finally:
            await self._close(session)

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
                hook = getattr(self.adapters.get(row.execution_adapter), "recover_stale", None)
                if callable(hook):
                    try:
                        handled = await _maybe_await(hook(session, row, coordinator=self, now=now))
                    except Exception:
                        recovery_hook_failed = True
                        raise
                    if handled:
                        recovered += 1
                        if stale_run_id:
                            stale_run_ids.append(str(stale_run_id))
                        continue
                if previous_state not in WORK_ACTIVE_STATES:
                    continue
                if int(row.attempt_count or 0) >= int(row.max_attempts or self.max_attempts):
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
            previous_result = await session.execute(
                select(AgentRun)
                .where(AgentRun.work_item_id == _as_uuid(claim.work_item_id))
                .order_by(desc(AgentRun.created_at))
                .limit(1)
            )
            previous_row = previous_result.scalars().first()
            if previous_row is not None:
                previous_attempt_id = str(previous_row.id)
            await session.commit()
        finally:
            await self._close(session)
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
            "execution_manifest": {
                "schema_version": 1,
                "agent_id": claim.assigned_agent_id,
                "agent_revision_id": claim.agent_revision_id,
                "capabilities": list(claim.required_capabilities),
                "project_id": claim.project_id,
                "task_id": claim.task_id,
                "work_item_id": claim.work_item_id,
                "source": claim.source_type,
            },
        }
        try:
            return await agent_run_service.create_run(**kwargs)
        except TypeError as exc:
            # Only tolerate a genuinely old AgentRunService signature during
            # rolling deployment; do not retry arbitrary internal TypeErrors
            # and accidentally create duplicate attempts.
            if "work_item_id" not in str(exc):
                raise
            kwargs.pop("work_item_id", None)
            kwargs.pop("work_item_attempt", None)
            kwargs.pop("previous_attempt_run_id", None)
            return await agent_run_service.create_run(**kwargs)

    async def execute_once(self, *, limit: int = 1) -> list[dict[str, Any]]:
        """Claim and execute at most *limit* work items."""

        if not self._runtime_enabled():
            return []
        await self.reconcile_run_settlements(limit=limit)
        if self.sources:
            try:
                await self.discover_and_materialize()
            except Exception:
                logger.warning("AgentWork discovery/materialization failed", exc_info=True)
        await self.recover_stale(limit=limit)
        claims = await self.claim_many(limit)
        results: list[dict[str, Any]] = []
        for claim in claims:
            adapter = self.adapters.get(claim.execution_adapter) or self.adapters.get("default")
            if adapter is None:
                await self.settle(claim, ExecutionOutcome(classification="permanent", error_code="execution_adapter_unavailable", error_message="execution adapter unavailable"))
                continue
            if not claim.assigned_agent_id and claim.execution_adapter not in {"system", "service"}:
                outcome = ExecutionOutcome(classification="blocked", error_code="agent_identity_missing", error_message="autonomous work requires an assigned Agent")
                settled = await self.settle(claim, outcome, source_ok=True)
                results.append({"claim": claim.to_dict(), "run": None, "outcome": outcome.normalized_classification(), "settlement": settled})
                continue
            if claim.assigned_agent_id and not claim.agent_revision_id:
                await self._settle_claimed_blocked(claim, "agent_revision_missing")
                continue
            try:
                from .agent_team_v3 import AGENT_TEAM_CAPABILITY_CATALOG
                unknown_caps = [item for item in claim.required_capabilities if item not in AGENT_TEAM_CAPABILITY_CATALOG]
            except Exception:
                unknown_caps = list(claim.required_capabilities)
            if unknown_caps:
                outcome = ExecutionOutcome(classification="blocked", error_code="unknown_capability", error_message="work item declares an unknown capability")
                settled = await self.settle(claim, outcome, source_ok=True)
                results.append({"claim": claim.to_dict(), "run": None, "outcome": outcome.normalized_classification(), "settlement": settled})
                continue
            if not await self.start_claim(claim):
                continue
            source_session = await self._session()
            try:
                source_allowed = await self._refresh_source(source_session, claim)
            finally:
                await self._close(source_session)
            if source_allowed is False:
                outcome = ExecutionOutcome(classification="blocked", error_code="source_not_executable", error_message="source state changed before execution")
                settled = await self.settle(claim, outcome, source_ok=False)
                results.append({"claim": claim.to_dict(), "run": None, "outcome": outcome.normalized_classification(), "settlement": settled})
                continue
            authority_ok, authority_reason = await self._authority_allows(claim)
            if not authority_ok:
                outcome = ExecutionOutcome(classification="blocked", error_code=authority_reason or "authority_denied", error_message="current Agent authority does not permit this work")
                settled = await self.settle(claim, outcome, source_ok=True)
                results.append({"claim": claim.to_dict(), "run": None, "outcome": outcome.normalized_classification(), "settlement": settled})
                continue
            try:
                run_payload = await self.create_agent_run(claim)
            except Exception as exc:
                logger.warning("AgentRun creation failed for %s", claim.work_item_id, exc_info=True)
                outcome = ExecutionOutcome(classification="transient", error_code="agent_run_create_failed", error_message=_safe_error(exc))
                settled = await self.settle(claim, outcome)
                results.append({"claim": claim.to_dict(), "run": None, "outcome": outcome.normalized_classification(), "settlement": settled})
                continue
            if claim.assigned_agent_id and not run_payload:
                outcome = ExecutionOutcome(classification="blocked", error_code="agent_run_unavailable", error_message="durable AgentRun could not be created")
                settled = await self.settle(claim, outcome, source_ok=True)
                results.append({"claim": claim.to_dict(), "run": None, "outcome": outcome.normalized_classification(), "settlement": settled})
                continue
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
                    continue
                try:
                    from .agent_run_service import AgentRunService

                    await AgentRunService(
                        self._db_manager,
                        config=self.config,
                    ).mark_running(str(run_payload["id"]))
                except Exception:
                    logger.debug("AgentRun running event could not be recorded", exc_info=True)
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
                        try:
                            adapter_task = asyncio.create_task(
                                self._invoke_adapter(
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
                                    # cooperative. Any already-issued
                                    # external side effect is settled as
                                    # uncertain, never retried blindly.
                                    adapter_task.cancel()
                                    try:
                                        await adapter_task
                                    except asyncio.CancelledError:
                                        pass
                                    outcome = ExecutionOutcome(
                                        classification="uncertain",
                                        error_code="lease_lost",
                                        error_message="work lease was lost during execution",
                                    )
                                    break
                        finally:
                            await self._close(adapter_session)
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
                    if outcome.normalized_classification() == "succeeded":
                        await AgentRunService(
                            self._db_manager,
                            config=self.config,
                        ).complete_run(str(run_payload["id"]), result=dict(outcome.result or {}), message=outcome.result_summary, metadata={"work_item_id": claim.work_item_id, "classification": outcome.normalized_classification(), "domain_ref": outcome.domain_ref, "domain_status": outcome.domain_status, "usage": dict(outcome.usage or {})})
                    elif outcome.normalized_classification() in {"permanent", "blocked", "cancelled", "transient", "awaiting_approval", "uncertain"}:
                        classification = outcome.normalized_classification()
                        await AgentRunService(
                            self._db_manager,
                            config=self.config,
                        ).fail_run(
                            str(run_payload["id"]),
                            outcome.error_message or outcome.error_code or "work failed",
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
                                "usage": dict(outcome.usage or {}),
                            },
                        )
                except Exception:
                    logger.debug("AgentRun settlement event could not be recorded", exc_info=True)
            cleanup = getattr(adapter, "cleanup", None)
            if callable(cleanup) and settlement_owned and outcome.normalized_classification() in {
                "succeeded", "permanent", "blocked", "cancelled", "uncertain"
            }:
                try:
                    value = cleanup(claim)
                    if inspect.isawaitable(value):
                        await value
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

    async def start(self, *, poll_interval_seconds: float = 5.0) -> None:
        if not self._runtime_enabled() or self._poll_task is not None and not self._poll_task.done():
            return
        self._stop_event.clear()
        self._poll_task = asyncio.create_task(self._poll_loop(max(0.05, float(poll_interval_seconds))), name="agent-work-coordinator")

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._poll_task
        self._poll_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        running = list(self._running_tasks)
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        self._running_tasks.clear()

    async def _poll_loop(self, interval: float) -> None:
        while not self._stop_event.is_set():
            if not self._runtime_enabled():
                return
            try:
                await self.execute_once(limit=self.max_concurrency)
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
        try:
            signature = inspect.signature(method)
            params = signature.parameters
            if not any(item.kind is inspect.Parameter.VAR_KEYWORD for item in params.values()):
                kwargs = {key: value for key, value in kwargs.items() if key in params}
        except (TypeError, ValueError):
            pass
        return await _maybe_await(method(claim, **kwargs))

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
    "WorkSource",
    "ExecutionAdapter",
    "AgentWorkCoordinator",
    "AgentWorkRuntime",
    "WorkCoordinator",
    "canonical_work_json",
    "work_mutation_fingerprint",
]
