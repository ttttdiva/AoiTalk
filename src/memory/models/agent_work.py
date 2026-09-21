"""Durable, common autonomous work models.

The work tables are deliberately domain neutral.  An ``AgentWorkItem`` is a
claimable attempt envelope materialized from an existing domain source (for
example a Task or a MediaOps schedule); it is not a replacement for that
source.  ``AgentWorkEvent`` is a bounded, append-only lifecycle ledger.

No provider response, model transcript, credential, environment, or
filesystem path belongs in either model.  ``to_safe_dict`` performs a second
projection/redaction pass so an accidentally over-broad JSON value cannot be
returned as an operations payload.
"""

from __future__ import annotations

import re
import uuid
import math
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, synonym, validates

from .base import Base


AGENT_WORK_STATES = frozenset(
    {
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
    }
)
AGENT_WORK_ITEM_STATES = AGENT_WORK_STATES
AGENT_WORK_OUTCOME_CLASSES = frozenset(
    {"succeeded", "transient", "permanent", "blocked", "awaiting_approval", "uncertain", "cancelled"}
)
AGENT_WORK_ACTOR_KINDS = frozenset({"human", "agent", "service"})

_SAFE_KEY_MARKERS = (
    "secret",
    "token",
    "password",
    "credential",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "private_key",
    "access_key",
    "refresh_key",
    "client_secret",
    "transcript",
    "prompt",
    "environment",
    "provider_response",
    "raw_response",
)
_SENSITIVE_URL_RE = re.compile(
    r"(?:https?|ftp)://[^\s]+(?:[?&](?:token|secret|password|key|sig|signature|credential)="
    r"[^\s&]+|@[^\s/]+)",
    re.IGNORECASE,
)
_PATH_RE = re.compile(r"(?:^|[\\/])(?:users?|home|tmp|var|etc|appdata)(?:[\\/]|$)", re.I)


_SAFE_INTEGRATION_REASON_CODES = frozenset({
    "integration_credential_unverified",
    "integration_credential_changed",
})


def _safe_text(value: Any, *, limit: int) -> str | None:
    """Clip text and refuse obvious secret-bearing URL/path values."""

    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    # Exact server-owned reason codes are metadata, not credential material.
    # Prefixes/suffixes and arbitrary credential-bearing text still fail below.
    if value in _SAFE_INTEGRATION_REASON_CODES:
        return value[:limit]
    lowered = value.casefold()
    if any(marker in lowered for marker in _SAFE_KEY_MARKERS):
        return None
    if _SENSITIVE_URL_RE.search(value) or _PATH_RE.search(value):
        return None
    return value[:limit]


def _safe_json(value: Any, *, depth: int = 0) -> Any:
    """Return a tiny JSON projection suitable for operator-facing DTOs."""

    if depth > 3:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _safe_text(value, limit=512)
    if isinstance(value, (list, tuple, set, frozenset)):
        result: list[Any] = []
        for item in list(value)[:64]:
            projected = _safe_json(item, depth=depth + 1)
            if projected is not None:
                result.append(projected)
        return result
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:64]:
            key = str(raw_key)
            lowered = key.casefold()
            if any(marker in lowered for marker in _SAFE_KEY_MARKERS):
                continue
            projected = _safe_json(item, depth=depth + 1)
            if projected is not None:
                result[key[:96]] = projected
        return result
    return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _id(value: Any) -> str | None:
    return str(value) if value is not None else None


class AgentWorkItem(Base):
    """One durable, claimable unit of autonomous work.

    The source identity tuple (``source_type``, ``source_id``,
    ``source_revision``, ``intent_key``) is unique.  WorkSource adapters can
    therefore safely materialize candidates repeatedly without creating
    duplicate logical work.  Retries remain separate AgentRun attempts linked
    by ``work_item_id``; this row records the aggregate lifecycle and fenced
    lease only.
    """

    __tablename__ = "agent_work_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    source_type = Column(String(64), nullable=False, index=True)
    source_id = Column(String(255), nullable=False, index=True)
    source_revision = Column(String(128), nullable=False, index=True)
    intent_key = Column(String(255), nullable=False)
    domain = Column(String(64), nullable=False, index=True)

    space_id = Column(
        UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="SET NULL"), nullable=True, index=True
    )
    project_id = Column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task_id = Column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    persona_id = Column(
        UUID(as_uuid=True), ForeignKey("media_personas.id", ondelete="SET NULL"), nullable=True, index=True
    )
    app_id = Column(
        UUID(as_uuid=True), ForeignKey("apps.id", ondelete="SET NULL"), nullable=True, index=True
    )

    assigned_agent_id = Column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_revision_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_revisions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    required_capabilities_json = Column(
        "required_capabilities", JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    execution_adapter = Column(
        String(128), nullable=False, default="default", server_default=text("'default'"), index=True
    )
    priority = Column(Integer, nullable=False, default=0, server_default=text("'0'"), index=True)
    not_before = Column(DateTime, nullable=True, index=True)
    deadline = Column(DateTime, nullable=True, index=True)

    state = Column(
        String(32), nullable=False, default="pending", server_default=text("'pending'"), index=True
    )
    attempt_count = Column(Integer, nullable=False, default=0, server_default=text("'0'"), index=True)
    max_attempts = Column(Integer, nullable=False, default=3, server_default=text("'3'"))
    lease_owner = Column(String(128), nullable=True, index=True)
    lease_token = Column(String(128), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    heartbeat_at = Column(DateTime, nullable=True, index=True)
    claimed_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    next_attempt_at = Column(DateTime, nullable=True, index=True)

    concurrency_key = Column(String(255), nullable=True, index=True)
    budget_reservation_json = Column(
        "budget_reservation", JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    root_work_item_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_work_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    parent_work_item_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_work_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    causation_id = Column(String(255), nullable=True, index=True)
    causal_depth = Column(Integer, nullable=False, default=0, server_default=text("'0'"))
    mutation_fingerprint = Column(String(64), nullable=True, index=True)
    metadata_json = Column("metadata", JSON, nullable=False, default=dict, server_default=text("'{}'"))

    outcome_classification = Column(String(32), nullable=True, index=True)
    blocker_code = Column(String(128), nullable=True)
    escalation_reason = Column(String(512), nullable=True)
    safe_error_code = Column(String(96), nullable=True)
    result_summary = Column(String(2048), nullable=True)
    result_summary_json = Column(JSON, nullable=True)

    completed_at = Column(DateTime, nullable=True, index=True)
    cancelled_at = Column(DateTime, nullable=True)
    active_agent_run_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"), index=True
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    events = relationship(
        "AgentWorkEvent", back_populates="work_item", cascade="all, delete-orphan", order_by="AgentWorkEvent.sequence"
    )
    runs = relationship(
        "AgentRun",
        foreign_keys="AgentRun.work_item_id",
        viewonly=True,
    )
    parent = relationship(
        "AgentWorkItem",
        remote_side=[id],
        foreign_keys=[parent_work_item_id],
        backref="children",
    )
    root = relationship(
        "AgentWorkItem",
        remote_side=[id],
        foreign_keys=[root_work_item_id],
        backref="root_descendants",
    )

    # Compatibility aliases used by coordinator and earlier queue adapters.
    required_capabilities = synonym("required_capabilities_json")
    budget_reservation = synonym("budget_reservation_json")
    budget_reservation_id = synonym("budget_reservation_json")
    budget_reference = synonym("budget_reservation_json")
    error_code = synonym("safe_error_code")
    error_message = synonym("escalation_reason")
    next_retry_at = synonym("next_attempt_at")
    attempts = synonym("attempt_count")
    metadata_projection = synonym("metadata_json")
    agent_run_id = synonym("active_agent_run_id")

    __table_args__ = (
        UniqueConstraint(
            "source_type", "source_id", "source_revision", "intent_key", name="uq_agent_work_items_source_identity"
        ),
        Index(
            "ix_agent_work_items_claimable", "state", "not_before", "priority", "created_at"
        ),
        Index(
            "ix_agent_work_items_lease_recovery", "state", "lease_expires_at", "heartbeat_at"
        ),
        Index(
            "ix_agent_work_items_context_state", "domain", "project_id", "task_id", "state"
        ),
        Index(
            "uq_agent_work_items_source_mutation_fingerprint",
            "source_type",
            "source_id",
            "mutation_fingerprint",
            unique=True,
            postgresql_where=text("mutation_fingerprint IS NOT NULL"),
            sqlite_where=text("mutation_fingerprint IS NOT NULL"),
        ),
        CheckConstraint(
            "length(source_type) BETWEEN 1 AND 64", name="ck_agent_work_items_source_type"
        ),
        CheckConstraint(
            "length(source_id) BETWEEN 1 AND 255", name="ck_agent_work_items_source_id"
        ),
        CheckConstraint(
            "length(source_revision) BETWEEN 1 AND 128", name="ck_agent_work_items_source_revision"
        ),
        CheckConstraint(
            "length(intent_key) BETWEEN 1 AND 255", name="ck_agent_work_items_intent_key"
        ),
        CheckConstraint("length(domain) BETWEEN 1 AND 64", name="ck_agent_work_items_domain"),
        CheckConstraint(
            "state IN ('pending','claimed','running','awaiting_approval','blocked','retry_wait','uncertain','succeeded','failed','cancelled','dead_letter')",
            name="ck_agent_work_items_state",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= 1000000", name="ck_agent_work_items_attempt_count"
        ),
        CheckConstraint(
            "max_attempts > 0 AND max_attempts <= 1000000", name="ck_agent_work_items_max_attempts"
        ),
        CheckConstraint(
            "causal_depth >= 0 AND causal_depth <= 64", name="ck_agent_work_items_causal_depth"
        ),
        CheckConstraint(
            "mutation_fingerprint IS NULL OR length(mutation_fingerprint) = 64",
            name="ck_agent_work_items_mutation_fingerprint",
        ),
        CheckConstraint(
            "outcome_classification IS NULL OR outcome_classification IN ('succeeded','transient','permanent','blocked','awaiting_approval','uncertain','cancelled')",
            name="ck_agent_work_items_outcome_classification",
        ),
        CheckConstraint(
            "length(execution_adapter) BETWEEN 1 AND 128", name="ck_agent_work_items_execution_adapter"
        ),
        CheckConstraint(
            "priority BETWEEN -1000000 AND 1000000", name="ck_agent_work_items_priority"
        ),
        CheckConstraint(
            "deadline IS NULL OR not_before IS NULL OR deadline >= not_before",
            name="ck_agent_work_items_schedule_window",
        ),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL) OR (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_agent_work_items_lease_triplet",
        ),
        CheckConstraint(
            "length(result_summary) <= 2048", name="ck_agent_work_items_result_summary"
        ),
    )

    def to_safe_dict(self, *, include_events: bool = False) -> Dict[str, Any]:
        """Return a bounded, secret-free operator projection.

        Lease tokens are fencing credentials and are intentionally never
        returned.  Callers can use ``lease_active`` and expiry for diagnostics.
        """

        required = self.required_capabilities_json
        if isinstance(required, (list, tuple)):
            capabilities = [
                projected
                for item in list(required)[:64]
                if (projected := _safe_text(item, limit=96)) is not None
            ]
        else:
            capabilities = []
        now = datetime.utcnow()
        lease_active = bool(
            self.lease_owner and self.lease_expires_at is not None and self.lease_expires_at > now
        )
        payload: Dict[str, Any] = {
            "id": _id(self.id),
            "source_type": _safe_text(self.source_type, limit=64),
            "source_id": _safe_text(self.source_id, limit=255),
            "source_revision": _safe_text(self.source_revision, limit=128),
            "intent_key": _safe_text(self.intent_key, limit=255),
            "domain": _safe_text(self.domain, limit=64),
            "space_id": _id(self.space_id),
            "project_id": _id(self.project_id),
            "task_id": _id(self.task_id),
            "persona_id": _id(self.persona_id),
            "app_id": _id(self.app_id),
            "assigned_agent_id": _id(self.assigned_agent_id),
            "agent_revision_id": _id(self.agent_revision_id),
            "required_capabilities": capabilities,
            "required_capabilities_json": capabilities,
            "execution_adapter": _safe_text(self.execution_adapter or "default", limit=128),
            "priority": int(self.priority or 0),
            "not_before": _iso(self.not_before),
            "deadline": _iso(self.deadline),
            "state": self.state or "pending",
            "attempt_count": int(self.attempt_count or 0),
            "attempts": int(self.attempt_count or 0),
            "max_attempts": int(self.max_attempts or 3),
            "lease_owner": _safe_text(self.lease_owner, limit=128),
            "lease_active": lease_active,
            "lease_expires_at": _iso(self.lease_expires_at),
            "heartbeat_at": _iso(self.heartbeat_at),
            "claimed_at": _iso(self.claimed_at),
            "started_at": _iso(self.started_at),
            "next_attempt_at": _iso(self.next_attempt_at),
            "next_retry_at": _iso(self.next_attempt_at),
            "concurrency_key": _safe_text(self.concurrency_key, limit=255),
            "budget_reservation": _safe_json(self.budget_reservation_json) or {},
            "budget_reservation_id": _safe_text(self.budget_reservation_ref, limit=255)
            if hasattr(self, "budget_reservation_ref")
            else None,
            "root_work_item_id": _id(self.root_work_item_id),
            "parent_work_item_id": _id(self.parent_work_item_id),
            "causation_id": _safe_text(self.causation_id, limit=255),
            "causal_depth": int(self.causal_depth or 0),
            "mutation_fingerprint": self.mutation_fingerprint,
            "metadata": _safe_json(self.metadata_json) or {},
            "outcome_classification": self.outcome_classification,
            "blocker_code": _safe_text(self.blocker_code, limit=128),
            "escalation_reason": _safe_text(self.escalation_reason, limit=512),
            "error_code": _safe_text(self.safe_error_code, limit=96),
            "safe_error_code": _safe_text(self.safe_error_code, limit=96),
            "result_summary": _safe_text(self.result_summary, limit=2048),
            "result_summary_json": _safe_json(self.result_summary_json),
            "completed_at": _iso(self.completed_at),
            "cancelled_at": _iso(self.cancelled_at),
            "active_agent_run_id": _id(self.active_agent_run_id),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if include_events:
            payload["events"] = [
                event.to_safe_dict() for event in (self.events or []) if event is not None
            ]
        return payload

    def to_dict(self, *, include_events: bool = False) -> Dict[str, Any]:
        """Compatibility alias for repository models that expose ``to_dict``."""

        return self.to_safe_dict(include_events=include_events)

    @validates("required_capabilities_json")
    def _validate_required_capabilities(self, _key: str, value: Any) -> list[str]:
        if not isinstance(value, (list, tuple, set, frozenset)):
            return []
        result: list[str] = []
        for item in list(value)[:64]:
            projected = _safe_text(item, limit=96)
            if projected is not None:
                result.append(projected)
        return list(dict.fromkeys(result))

    @validates("budget_reservation_json", "metadata_json", "result_summary_json")
    def _validate_json_projection(self, _key: str, value: Any) -> Any:
        projected = _safe_json(value)
        return projected if projected is not None else ({} if _key != "result_summary_json" else None)

    @validates("result_summary", "escalation_reason", "safe_error_code", "blocker_code")
    def _validate_bounded_text(self, key: str, value: Any) -> str | None:
        limits = {"result_summary": 2048, "escalation_reason": 512, "safe_error_code": 96, "blocker_code": 128}
        return _safe_text(value, limit=limits[key])

    @property
    def budget_reservation_ref(self) -> str | None:
        """Opaque reservation identifier when a JSON reservation stores one."""

        value = self.budget_reservation_json
        if isinstance(value, dict):
            for key in ("id", "reservation_id", "reference", "ref"):
                ref = _safe_text(value.get(key), limit=255)
                if ref:
                    return ref
        return _safe_text(value, limit=255) if isinstance(value, str) else None

    @budget_reservation_ref.setter
    def budget_reservation_ref(self, value: Any) -> None:
        if value is None:
            self.budget_reservation_json = {}
        elif isinstance(value, dict):
            self.budget_reservation_json = _safe_json(value) or {}
        else:
            ref = _safe_text(value, limit=255)
            self.budget_reservation_json = {"reference": ref} if ref else {}


class AgentWorkEvent(Base):
    """Append-only, bounded lifecycle evidence for an ``AgentWorkItem``."""

    __tablename__ = "agent_work_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    work_item_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_work_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence = Column(Integer, nullable=False)
    event_type = Column(String(80), nullable=False, index=True)
    from_state = Column(String(32), nullable=True)
    to_state = Column(String(32), nullable=True)
    status = Column(String(32), nullable=True, index=True)
    actor_kind = Column(
        String(16), nullable=False, default="service", server_default=text("'service'")
    )
    actor_id = Column(String(255), nullable=True)
    actor_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_agent_id = Column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_service_key = Column(String(128), nullable=True)
    agent_run_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    causation_id = Column(String(255), nullable=True, index=True)
    causal_depth = Column(Integer, nullable=False, default=0, server_default=text("'0'"))
    mutation_fingerprint = Column(String(64), nullable=True, index=True)
    safe_error_code = Column(String(96), nullable=True)
    message = Column(String(512), nullable=True)
    result_summary = Column(String(2048), nullable=True)
    payload_json = Column("payload", JSON, nullable=False, default=dict, server_default=text("'{}'"))
    created_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"), index=True
    )

    work_item = relationship("AgentWorkItem", back_populates="events")
    agent_run = relationship("AgentRun", foreign_keys=[agent_run_id], viewonly=True)

    state = synonym("status")
    error_code = synonym("safe_error_code")
    error_message = synonym("message")
    occurred_at = synonym("created_at")
    payload = synonym("payload_json")

    __table_args__ = (
        UniqueConstraint("work_item_id", "sequence", name="uq_agent_work_events_sequence"),
        Index("ix_agent_work_events_work_created", "work_item_id", "created_at"),
        Index(
            "uq_agent_work_events_mutation_fingerprint",
            "work_item_id",
            "mutation_fingerprint",
            unique=True,
            postgresql_where=text("mutation_fingerprint IS NOT NULL"),
            sqlite_where=text("mutation_fingerprint IS NOT NULL"),
        ),
        CheckConstraint(
            "sequence >= 1 AND sequence <= 1000000", name="ck_agent_work_events_sequence"
        ),
        CheckConstraint(
            "length(event_type) BETWEEN 1 AND 80", name="ck_agent_work_events_event_type"
        ),
        CheckConstraint(
            "actor_kind IN ('human','agent','service')", name="ck_agent_work_events_actor_kind"
        ),
        CheckConstraint(
            "from_state IS NULL OR from_state IN ('pending','claimed','running','awaiting_approval','blocked','retry_wait','uncertain','succeeded','failed','cancelled','dead_letter')",
            name="ck_agent_work_events_from_state",
        ),
        CheckConstraint(
            "to_state IS NULL OR to_state IN ('pending','claimed','running','awaiting_approval','blocked','retry_wait','uncertain','succeeded','failed','cancelled','dead_letter')",
            name="ck_agent_work_events_to_state",
        ),
        CheckConstraint(
            "status IS NULL OR status IN ('pending','claimed','running','awaiting_approval','blocked','retry_wait','uncertain','succeeded','failed','cancelled','dead_letter','transient','permanent')",
            name="ck_agent_work_events_status",
        ),
        CheckConstraint(
            "NOT (actor_user_id IS NOT NULL AND actor_agent_id IS NOT NULL)",
            name="ck_agent_work_events_actor_user_agent_xor",
        ),
        CheckConstraint(
            "(actor_kind = 'human' AND actor_user_id IS NOT NULL AND actor_agent_id IS NULL AND actor_service_key IS NULL) OR (actor_kind = 'agent' AND actor_agent_id IS NOT NULL AND actor_user_id IS NULL AND actor_service_key IS NULL) OR (actor_kind = 'service' AND actor_service_key IS NOT NULL AND actor_user_id IS NULL AND actor_agent_id IS NULL)",
            name="ck_agent_work_events_actor_kind_fields",
        ),
        CheckConstraint(
            "actor_service_key IS NULL OR actor_service_key IN ('aoitalk.system','aoitalk.agent-harness','aoitalk.migrations','aoitalk.media-adapter')",
            name="ck_agent_work_events_actor_service_key",
        ),
        CheckConstraint(
            "causal_depth >= 0 AND causal_depth <= 64", name="ck_agent_work_events_causal_depth"
        ),
        CheckConstraint(
            "mutation_fingerprint IS NULL OR length(mutation_fingerprint) = 64",
            name="ck_agent_work_events_mutation_fingerprint",
        ),
        CheckConstraint("length(message) <= 512", name="ck_agent_work_events_message"),
        CheckConstraint(
            "length(result_summary) <= 2048", name="ck_agent_work_events_result_summary"
        ),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        """Return bounded event evidence without lease/credential material."""

        projected_payload = _safe_json(self.payload_json)
        return {
            "id": _id(self.id),
            "work_item_id": _id(self.work_item_id),
            "sequence": int(self.sequence or 0),
            "event_type": _safe_text(self.event_type, limit=80),
            "from_state": self.from_state,
            "to_state": self.to_state,
            "status": self.status,
            "state": self.status or self.to_state,
            "actor_kind": self.actor_kind,
            "actor_id": _safe_text(self.actor_id, limit=255),
            "actor_user_id": _id(self.actor_user_id),
            "actor_agent_id": _id(self.actor_agent_id),
            "actor_service_key": _safe_text(self.actor_service_key, limit=128),
            "agent_run_id": _id(self.agent_run_id),
            "causation_id": _safe_text(self.causation_id, limit=255),
            "causal_depth": int(self.causal_depth or 0),
            "mutation_fingerprint": self.mutation_fingerprint,
            "safe_error_code": _safe_text(self.safe_error_code, limit=96),
            "error_code": _safe_text(self.safe_error_code, limit=96),
            "message": _safe_text(self.message, limit=512),
            "error_message": _safe_text(self.message, limit=512),
            "result_summary": _safe_text(self.result_summary, limit=2048),
            "payload": projected_payload if projected_payload is not None else {},
            "payload_json": projected_payload if projected_payload is not None else {},
            "created_at": _iso(self.created_at),
            "occurred_at": _iso(self.created_at),
        }

    @validates("payload_json")
    def _validate_payload(self, _key: str, value: Any) -> dict[str, Any]:
        projected = _safe_json(value)
        return projected if isinstance(projected, dict) else {}

    @validates("message", "result_summary", "safe_error_code")
    def _validate_event_text(self, key: str, value: Any) -> str | None:
        limits = {"message": 512, "result_summary": 2048, "safe_error_code": 96}
        return _safe_text(value, limit=limits[key])

    def to_dict(self) -> Dict[str, Any]:
        return self.to_safe_dict()


__all__ = [
    "AGENT_WORK_ACTOR_KINDS",
    "AGENT_WORK_ITEM_STATES",
    "AGENT_WORK_OUTCOME_CLASSES",
    "AGENT_WORK_STATES",
    "AgentWorkEvent",
    "AgentWorkItem",
]
