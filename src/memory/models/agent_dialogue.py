"""Private Agent-to-Agent dialogue ledger models.

The rows in this module are identity, routing, and delivery metadata only.
Conversation text remains in the encrypted ``conversation_messages`` table;
the dialogue ledger stores references to those messages and to the ordinary
Run/Work rows that already own execution evidence.

The private AoiDots root deliberately points at an ordinary ``Agent`` and an
ordinary ``ConversationSession``.  It does not carry a revision snapshot or a
second scheduler/agent type.  Every child dialogue pins its own ordinary
worker session and exact AgentRevision.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    BigInteger,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .base import Base


AGENT_PRIVATE_ROOT_PURPOSES = frozenset({"aoidots"})
AGENT_PRIVATE_ROOT_STATES = frozenset({"active", "disabled", "deleted"})
AGENT_DIALOGUE_STATES = frozenset(
    {"queued", "running", "waiting", "succeeded", "failed", "cancelled", "blocked", "expired"}
)
AGENT_DIALOGUE_DELIVERY_KINDS = frozenset({"request", "response", "control", "event"})
AGENT_DIALOGUE_DELIVERY_STATES = frozenset(
    {"pending", "available", "leased", "delivered", "acked", "failed", "cancelled", "expired"}
)
AGENT_DIALOGUE_INPUT_KINDS = frozenset({"agent_question", "human_question"})
AGENT_DIALOGUE_INPUT_STATES = frozenset(
    {"pending", "answered", "invalidated", "blocked", "expired", "cancelled"}
)


def _id(value: Any) -> str | None:
    return str(value) if value is not None else None


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class AgentPrivateRoot(Base):
    """Owner-scoped pointer to the ordinary AoiDots Agent and root session."""

    __tablename__ = "agent_private_roots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    purpose = Column(String(64), nullable=False, default="aoidots", server_default="aoidots")
    agent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversation_sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    state = Column(String(16), nullable=False, default="active", server_default="active", index=True)
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        index=True,
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    agent = relationship("Agent", foreign_keys=[agent_id], viewonly=True)
    session = relationship("ConversationSession", foreign_keys=[session_id], viewonly=True)
    owner = relationship("User", foreign_keys=[owner_user_id], viewonly=True)
    dialogues = relationship(
        "AgentDialogue",
        primaryjoin="AgentPrivateRoot.session_id == AgentDialogue.root_session_id",
        foreign_keys="AgentDialogue.root_session_id",
        viewonly=True,
    )

    __table_args__ = (
        UniqueConstraint("owner_user_id", "purpose", name="uq_agent_private_roots_owner_purpose"),
        UniqueConstraint("agent_id", name="uq_agent_private_roots_agent"),
        UniqueConstraint("session_id", name="uq_agent_private_roots_session"),
        CheckConstraint(
            "purpose IN ('aoidots')",
            name="ck_agent_private_roots_purpose",
        ),
        CheckConstraint(
            "state IN ('active','disabled','deleted')",
            name="ck_agent_private_roots_state",
        ),
        Index("ix_agent_private_roots_owner_state", "owner_user_id", "state"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _id(self.id),
            "owner_user_id": _id(self.owner_user_id),
            "purpose": self.purpose,
            "agent_id": _id(self.agent_id),
            "session_id": _id(self.session_id),
            "state": self.state,
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict


class AgentDialogue(Base):
    """One bounded request from a root Agent to one ordinary child session."""

    __tablename__ = "agent_dialogues"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    root_session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversation_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    root_agent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    root_agent_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_revisions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    worker_session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversation_sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    worker_agent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    worker_agent_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_revisions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    budget_root_dialogue_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_dialogues.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    parent_dialogue_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_dialogues.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    causal_depth = Column(Integer, nullable=False, default=0, server_default="0")
    project_id = Column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task_id = Column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    human_message_id = Column(
        UUID(as_uuid=True), ForeignKey("conversation_messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    human_run_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    human_work_item_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_work_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    authorization_json = Column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    authorization_hash = Column(String(64), nullable=False, index=True)
    control_revision = Column(Integer, nullable=False, default=0, server_default="0")
    state = Column(String(16), nullable=False, default="queued", server_default="queued", index=True)
    max_turns = Column(Integer, nullable=False, default=8, server_default="8")
    turn_count = Column(Integer, nullable=False, default=0, server_default="0")
    max_depth = Column(Integer, nullable=False, default=4, server_default="4")
    max_attempts = Column(Integer, nullable=False, default=16, server_default="16")
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    max_tokens = Column(BigInteger, nullable=True)
    input_tokens = Column(BigInteger, nullable=False, default=0, server_default="0")
    output_tokens = Column(BigInteger, nullable=False, default=0, server_default="0")
    cached_tokens = Column(BigInteger, nullable=False, default=0, server_default="0")
    total_tokens = Column(BigInteger, nullable=False, default=0, server_default="0")
    reserved_tokens = Column(BigInteger, nullable=False, default=0, server_default="0")
    max_cost_micros = Column(BigInteger, nullable=True)
    cost_micros = Column(BigInteger, nullable=True)
    reserved_cost_micros = Column(BigInteger, nullable=False, default=0, server_default="0")
    cost_currency = Column(String(16), nullable=True)
    usage_state = Column(String(24), nullable=False, default="known", server_default="known")
    deadline = Column(DateTime, nullable=True, index=True)
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        index=True,
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    deliveries = relationship(
        "AgentDialogueDelivery",
        back_populates="dialogue",
        cascade="all, delete-orphan",
        order_by="AgentDialogueDelivery.sequence",
    )
    inputs = relationship(
        "AgentDialogueInput",
        back_populates="dialogue",
        cascade="all, delete-orphan",
        order_by="AgentDialogueInput.revision",
    )
    usage_receipts = relationship(
        "AgentDialogueUsage",
        back_populates="dialogue",
        cascade="all, delete-orphan",
        order_by="AgentDialogueUsage.created_at",
    )
    reservations = relationship(
        "AgentDialogueReservation",
        back_populates="dialogue",
        cascade="all, delete-orphan",
        order_by="AgentDialogueReservation.created_at",
    )
    parent_dialogue = relationship(
        "AgentDialogue",
        remote_side=[id],
        foreign_keys=[parent_dialogue_id],
        viewonly=True,
    )

    __table_args__ = (
        Index("ix_agent_dialogues_owner_state", "owner_user_id", "state", "created_at"),
        Index("ix_agent_dialogues_root_state", "root_session_id", "state", "created_at"),
        CheckConstraint("root_session_id <> worker_session_id", name="ck_agent_dialogues_distinct_sessions"),
        CheckConstraint("authorization_hash IS NOT NULL AND length(authorization_hash) = 64", name="ck_agent_dialogues_authorization_hash"),
        CheckConstraint("control_revision >= 0", name="ck_agent_dialogues_control_revision"),
        CheckConstraint("state IN ('queued','running','waiting','succeeded','failed','cancelled','blocked','expired')", name="ck_agent_dialogues_state"),
        CheckConstraint("max_turns BETWEEN 1 AND 100", name="ck_agent_dialogues_max_turns"),
        CheckConstraint("turn_count BETWEEN 0 AND max_turns", name="ck_agent_dialogues_turn_count"),
        CheckConstraint("max_depth BETWEEN 0 AND 32", name="ck_agent_dialogues_max_depth"),
        CheckConstraint("causal_depth BETWEEN 0 AND 32", name="ck_agent_dialogues_budget_causal_depth"),
        CheckConstraint("max_attempts BETWEEN 1 AND 100", name="ck_agent_dialogues_budget_max_attempts"),
        CheckConstraint("attempt_count BETWEEN 0 AND max_attempts", name="ck_agent_dialogues_budget_attempt_count"),
        CheckConstraint("max_tokens IS NULL OR max_tokens BETWEEN 1 AND 1000000", name="ck_agent_dialogues_budget_max_tokens"),
        CheckConstraint("input_tokens >= 0 AND output_tokens >= 0 AND cached_tokens >= 0 AND total_tokens >= 0", name="ck_agent_dialogues_budget_token_usage"),
        CheckConstraint("reserved_tokens >= 0", name="ck_agent_dialogues_budget_reserved_tokens"),
        CheckConstraint("max_cost_micros IS NULL OR max_cost_micros BETWEEN 1 AND 1000000000000", name="ck_agent_dialogues_budget_max_cost"),
        CheckConstraint("cost_micros IS NULL OR cost_micros >= 0", name="ck_agent_dialogues_budget_cost"),
        CheckConstraint("reserved_cost_micros >= 0", name="ck_agent_dialogues_budget_reserved_cost"),
        CheckConstraint("usage_state IN ('known','unknown','cost_unknown','token_unknown')", name="ck_agent_dialogues_budget_usage_state"),
        CheckConstraint("root_agent_revision_id IS NOT NULL AND root_agent_id IS NOT NULL", name="ck_agent_dialogues_root_revision_requires_agent"),
        CheckConstraint("worker_agent_revision_id IS NOT NULL AND worker_agent_id IS NOT NULL", name="ck_agent_dialogues_worker_revision_requires_agent"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _id(self.id),
            "owner_user_id": _id(self.owner_user_id),
            "root_session_id": _id(self.root_session_id),
            "root_agent_id": _id(self.root_agent_id),
            "root_agent_revision_id": _id(self.root_agent_revision_id),
            "worker_session_id": _id(self.worker_session_id),
            "worker_agent_id": _id(self.worker_agent_id),
            "worker_agent_revision_id": _id(self.worker_agent_revision_id),
            "budget_root_dialogue_id": _id(self.budget_root_dialogue_id),
            "parent_dialogue_id": _id(self.parent_dialogue_id),
            "causal_depth": int(self.causal_depth or 0),
            "project_id": _id(self.project_id),
            "task_id": _id(self.task_id),
            "human_message_id": _id(self.human_message_id),
            "human_run_id": _id(self.human_run_id),
            "human_work_item_id": _id(self.human_work_item_id),
            "authorization_hash": self.authorization_hash,
            "control_revision": int(self.control_revision or 0),
            "state": self.state,
            "max_turns": int(self.max_turns or 0),
            "turn_count": int(self.turn_count or 0),
            "max_depth": int(self.max_depth or 0),
            "max_attempts": int(self.max_attempts or 0),
            "attempt_count": int(self.attempt_count or 0),
            "max_tokens": int(self.max_tokens) if self.max_tokens is not None else None,
            "input_tokens": int(self.input_tokens or 0),
            "output_tokens": int(self.output_tokens or 0),
            "cached_tokens": int(self.cached_tokens or 0),
            "total_tokens": int(self.total_tokens or 0),
            "reserved_tokens": int(self.reserved_tokens or 0),
            "max_cost_micros": int(self.max_cost_micros) if self.max_cost_micros is not None else None,
            "cost_micros": int(self.cost_micros) if self.cost_micros is not None else None,
            "reserved_cost_micros": int(self.reserved_cost_micros or 0),
            "cost_currency": self.cost_currency,
            "usage_state": self.usage_state,
            "deadline": _dt(self.deadline),
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict


class AgentDialogueBudgetRoot(Base):
    """Owner-scoped aggregate budget shared by one Dialogue chain."""

    __tablename__ = "agent_dialogue_budget_roots"

    root_dialogue_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_dialogues.id", ondelete="CASCADE"), primary_key=True
    )
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    root_session_id = Column(UUID(as_uuid=True), ForeignKey("conversation_sessions.id", ondelete="RESTRICT"), nullable=False, index=True)
    root_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False, index=True)
    root_agent_revision_id = Column(UUID(as_uuid=True), ForeignKey("agent_revisions.id", ondelete="RESTRICT"), nullable=False, index=True)
    human_message_id = Column(UUID(as_uuid=True), ForeignKey("conversation_messages.id", ondelete="SET NULL"), nullable=True, index=True)
    human_run_id = Column(UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    human_work_item_id = Column(UUID(as_uuid=True), ForeignKey("agent_work_items.id", ondelete="SET NULL"), nullable=True, index=True)
    authorization_hash = Column(String(64), nullable=False, index=True)
    state = Column(String(16), nullable=False, default="active", server_default="active", index=True)
    max_turns = Column(Integer, nullable=False, default=8, server_default="8")
    turn_count = Column(Integer, nullable=False, default=0, server_default="0")
    max_depth = Column(Integer, nullable=False, default=4, server_default="4")
    max_attempts = Column(Integer, nullable=False, default=16, server_default="16")
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    max_tokens = Column(BigInteger, nullable=True)
    input_tokens = Column(BigInteger, nullable=False, default=0, server_default="0")
    output_tokens = Column(BigInteger, nullable=False, default=0, server_default="0")
    cached_tokens = Column(BigInteger, nullable=False, default=0, server_default="0")
    total_tokens = Column(BigInteger, nullable=False, default=0, server_default="0")
    reserved_tokens = Column(BigInteger, nullable=False, default=0, server_default="0")
    max_cost_micros = Column(BigInteger, nullable=True)
    cost_micros = Column(BigInteger, nullable=True)
    reserved_cost_micros = Column(BigInteger, nullable=False, default=0, server_default="0")
    cost_currency = Column(String(16), nullable=True)
    usage_state = Column(String(24), nullable=False, default="known", server_default="known")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"), index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))

    dialogue = relationship("AgentDialogue", foreign_keys=[root_dialogue_id], viewonly=True)

    __table_args__ = (
        CheckConstraint("length(authorization_hash) = 64", name="ck_agent_dialogue_budget_root_auth_hash"),
        CheckConstraint("state IN ('active','blocked','succeeded','failed','cancelled','expired')", name="ck_agent_dialogue_budget_root_state"),
        CheckConstraint("max_turns BETWEEN 1 AND 100", name="ck_agent_dialogue_budget_root_max_turns"),
        CheckConstraint("turn_count BETWEEN 0 AND max_turns", name="ck_agent_dialogue_budget_root_turn_count"),
        CheckConstraint("max_depth BETWEEN 0 AND 32", name="ck_agent_dialogue_budget_root_max_depth"),
        CheckConstraint("max_attempts BETWEEN 1 AND 100", name="ck_agent_dialogue_budget_root_max_attempts"),
        CheckConstraint("attempt_count BETWEEN 0 AND max_attempts", name="ck_agent_dialogue_budget_root_attempt_count"),
        CheckConstraint("max_tokens IS NULL OR max_tokens BETWEEN 1 AND 1000000", name="ck_agent_dialogue_budget_root_max_tokens"),
        CheckConstraint("input_tokens >= 0 AND output_tokens >= 0 AND cached_tokens >= 0 AND total_tokens >= 0 AND reserved_tokens >= 0", name="ck_agent_dialogue_budget_root_tokens"),
        CheckConstraint("max_cost_micros IS NULL OR max_cost_micros BETWEEN 1 AND 1000000000000", name="ck_agent_dialogue_budget_root_max_cost"),
        CheckConstraint("cost_micros IS NULL OR cost_micros >= 0", name="ck_agent_dialogue_budget_root_cost"),
        CheckConstraint("reserved_cost_micros >= 0", name="ck_agent_dialogue_budget_root_reserved_cost"),
        CheckConstraint("usage_state IN ('known','unknown','cost_unknown','token_unknown')", name="ck_agent_dialogue_budget_root_usage_state"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "root_dialogue_id": _id(self.root_dialogue_id),
            "owner_user_id": _id(self.owner_user_id),
            "root_session_id": _id(self.root_session_id),
            "root_agent_id": _id(self.root_agent_id),
            "root_agent_revision_id": _id(self.root_agent_revision_id),
            "human_message_id": _id(self.human_message_id),
            "human_run_id": _id(self.human_run_id),
            "human_work_item_id": _id(self.human_work_item_id),
            "authorization_hash": self.authorization_hash,
            "state": self.state,
            "max_turns": int(self.max_turns or 0),
            "turn_count": int(self.turn_count or 0),
            "max_depth": int(self.max_depth or 0),
            "max_attempts": int(self.max_attempts or 0),
            "attempt_count": int(self.attempt_count or 0),
            "max_tokens": int(self.max_tokens) if self.max_tokens is not None else None,
            "total_tokens": int(self.total_tokens or 0),
            "reserved_tokens": int(self.reserved_tokens or 0),
            "max_cost_micros": int(self.max_cost_micros) if self.max_cost_micros is not None else None,
            "cost_micros": int(self.cost_micros) if self.cost_micros is not None else None,
            "reserved_cost_micros": int(self.reserved_cost_micros or 0),
            "cost_currency": self.cost_currency,
            "usage_state": self.usage_state,
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict


class AgentDialogueReservation(Base):
    """A fenced in-flight token/cost reservation for one invocation."""

    __tablename__ = "agent_dialogue_reservations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dialogue_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_dialogues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    budget_root_dialogue_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_dialogue_budget_roots.root_dialogue_id", ondelete="CASCADE"), nullable=True, index=True
    )
    run_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    reservation_key = Column(String(255), nullable=False)
    state = Column(String(16), nullable=False, default="reserved", server_default="reserved", index=True)
    reserved_tokens = Column(BigInteger, nullable=False, default=0, server_default="0")
    reserved_cost_micros = Column(BigInteger, nullable=False, default=0, server_default="0")
    charged_tokens = Column(BigInteger, nullable=False, default=0, server_default="0")
    charged_cost_micros = Column(BigInteger, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"), index=True)
    released_at = Column(DateTime, nullable=True)

    dialogue = relationship("AgentDialogue", back_populates="reservations")
    run = relationship("AgentRun", foreign_keys=[run_id], viewonly=True)

    __table_args__ = (
        UniqueConstraint("dialogue_id", "reservation_key", name="uq_agent_dialogue_reservation_key"),
        CheckConstraint("state IN ('reserved','released','charged','overrun')", name="ck_agent_dialogue_reservation_state"),
        CheckConstraint("reserved_tokens >= 0 AND reserved_cost_micros >= 0", name="ck_agent_dialogue_reservation_reserved"),
        CheckConstraint("charged_tokens >= 0 AND charged_cost_micros >= 0", name="ck_agent_dialogue_reservation_charged"),
        CheckConstraint("length(reservation_key) BETWEEN 1 AND 255", name="ck_agent_dialogue_reservation_key"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _id(self.id),
            "dialogue_id": _id(self.dialogue_id),
            "budget_root_dialogue_id": _id(self.budget_root_dialogue_id),
            "run_id": _id(self.run_id),
            "reservation_key": self.reservation_key,
            "state": self.state,
            "reserved_tokens": int(self.reserved_tokens or 0),
            "reserved_cost_micros": int(self.reserved_cost_micros or 0),
            "charged_tokens": int(self.charged_tokens or 0),
            "charged_cost_micros": int(self.charged_cost_micros or 0),
            "created_at": _dt(self.created_at),
            "released_at": _dt(self.released_at),
        }

    to_dict = to_safe_dict


class AgentDialogueUsage(Base):
    """One idempotent usage receipt for one Dialogue receiver Run.

    Provider usage is deliberately kept as a small accounting record.  A
    missing or malformed provider usage is represented by ``state='unknown'``;
    it is never converted to zero and cannot be counted twice for the same
    ``(dialogue_id, run_id)`` identity.
    """

    __tablename__ = "agent_dialogue_usage"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dialogue_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_dialogues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    budget_root_dialogue_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_dialogue_budget_roots.root_dialogue_id", ondelete="CASCADE"), nullable=True, index=True
    )
    run_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    usage_key = Column(String(255), nullable=False)
    state = Column(String(24), nullable=False, default="known", server_default="known", index=True)
    input_tokens = Column(BigInteger, nullable=True)
    output_tokens = Column(BigInteger, nullable=True)
    cached_tokens = Column(BigInteger, nullable=True)
    total_tokens = Column(BigInteger, nullable=True)
    cost_micros = Column(BigInteger, nullable=True)
    cost_currency = Column(String(16), nullable=True)
    reason = Column(String(128), nullable=True)
    usage_digest = Column(String(64), nullable=False)
    metadata_json = Column("metadata", JSON, nullable=False, default=dict, server_default=text("'{}'"))
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        index=True,
    )

    dialogue = relationship("AgentDialogue", back_populates="usage_receipts")
    run = relationship("AgentRun", foreign_keys=[run_id], viewonly=True)

    __table_args__ = (
        UniqueConstraint("dialogue_id", "run_id", name="uq_agent_dialogue_usage_dialogue_run"),
        UniqueConstraint("dialogue_id", "usage_key", name="uq_agent_dialogue_usage_dialogue_key"),
        CheckConstraint("state IN ('known','unknown','cost_unknown','token_unknown')", name="ck_agent_dialogue_usage_state"),
        CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="ck_agent_dialogue_usage_input_tokens"),
        CheckConstraint("output_tokens IS NULL OR output_tokens >= 0", name="ck_agent_dialogue_usage_output_tokens"),
        CheckConstraint("cached_tokens IS NULL OR cached_tokens >= 0", name="ck_agent_dialogue_usage_cached_tokens"),
        CheckConstraint("total_tokens IS NULL OR total_tokens >= 0", name="ck_agent_dialogue_usage_total_tokens"),
        CheckConstraint("cost_micros IS NULL OR cost_micros >= 0", name="ck_agent_dialogue_usage_cost_micros"),
        CheckConstraint("length(usage_key) BETWEEN 1 AND 255", name="ck_agent_dialogue_usage_key"),
        CheckConstraint("length(usage_digest) = 64", name="ck_agent_dialogue_usage_digest"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _id(self.id),
            "dialogue_id": _id(self.dialogue_id),
            "budget_root_dialogue_id": _id(self.budget_root_dialogue_id),
            "run_id": _id(self.run_id),
            "usage_key": self.usage_key,
            "state": self.state,
            "input_tokens": int(self.input_tokens) if self.input_tokens is not None else None,
            "output_tokens": int(self.output_tokens) if self.output_tokens is not None else None,
            "cached_tokens": int(self.cached_tokens) if self.cached_tokens is not None else None,
            "total_tokens": int(self.total_tokens) if self.total_tokens is not None else None,
            "cost_micros": int(self.cost_micros) if self.cost_micros is not None else None,
            "cost_currency": self.cost_currency,
            "reason": self.reason,
            "usage_digest": self.usage_digest,
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class AgentDialogueDelivery(Base):
    """Reference-only transport ledger row for one dialogue delivery."""

    __tablename__ = "agent_dialogue_deliveries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dialogue_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_dialogues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_session_id = Column(
        UUID(as_uuid=True), ForeignKey("conversation_sessions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_agent_id = Column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    source_agent_revision_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_revisions.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    source_run_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_work_item_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_work_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_message_id = Column(
        UUID(as_uuid=True), ForeignKey("conversation_messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    receiver_session_id = Column(
        UUID(as_uuid=True), ForeignKey("conversation_sessions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    receiver_agent_id = Column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    receiver_agent_revision_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_revisions.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    receiver_run_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    receiver_work_item_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_work_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    receiver_message_id = Column(
        UUID(as_uuid=True), ForeignKey("conversation_messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    transport_work_item_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_work_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind = Column(String(32), nullable=False, default="request", server_default="request", index=True)
    state = Column(String(32), nullable=False, default="pending", server_default="pending", index=True)
    sequence = Column(Integer, nullable=False, default=0, server_default="0")
    causation_id = Column(String(255), nullable=True, index=True)
    depth = Column(Integer, nullable=False, default=0, server_default="0")
    expected_control_revision = Column(Integer, nullable=False, default=0, server_default="0")
    idempotency_key = Column(String(255), nullable=False)
    request_digest = Column(String(64), nullable=False)
    response_digest = Column(String(64), nullable=True)
    available_at = Column(DateTime, nullable=True, index=True)
    deadline = Column(DateTime, nullable=True, index=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        index=True,
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    dialogue = relationship("AgentDialogue", back_populates="deliveries")

    __table_args__ = (
        UniqueConstraint("dialogue_id", "idempotency_key", name="uq_agent_dialogue_deliveries_idempotency"),
        UniqueConstraint("dialogue_id", "sequence", name="uq_agent_dialogue_deliveries_sequence"),
        Index("ix_agent_dialogue_deliveries_receiver_state", "receiver_session_id", "state", "available_at"),
        CheckConstraint("kind IN ('request','response','control','event')", name="ck_agent_dialogue_deliveries_kind"),
        CheckConstraint("state IN ('pending','available','leased','delivered','acked','failed','cancelled','expired')", name="ck_agent_dialogue_deliveries_state"),
        CheckConstraint("sequence >= 0", name="ck_agent_dialogue_deliveries_sequence"),
        CheckConstraint("depth BETWEEN 0 AND 32", name="ck_agent_dialogue_deliveries_depth"),
        CheckConstraint("expected_control_revision >= 0", name="ck_agent_dialogue_deliveries_control_revision"),
        CheckConstraint("attempt_count BETWEEN 0 AND 100", name="ck_agent_dialogue_deliveries_attempt_count"),
        CheckConstraint("length(idempotency_key) BETWEEN 1 AND 255", name="ck_agent_dialogue_deliveries_idempotency_key"),
        CheckConstraint("length(request_digest) = 64", name="ck_agent_dialogue_deliveries_request_digest"),
        CheckConstraint("response_digest IS NULL OR length(response_digest) = 64", name="ck_agent_dialogue_deliveries_response_digest"),
        CheckConstraint("(source_agent_id IS NULL AND source_agent_revision_id IS NULL) OR (source_agent_id IS NOT NULL AND source_agent_revision_id IS NOT NULL)", name="ck_agent_dialogue_deliveries_source_agent_pair"),
        CheckConstraint("(receiver_agent_id IS NULL AND receiver_agent_revision_id IS NULL) OR (receiver_agent_id IS NOT NULL AND receiver_agent_revision_id IS NOT NULL)", name="ck_agent_dialogue_deliveries_receiver_agent_pair"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _id(self.id),
            "dialogue_id": _id(self.dialogue_id),
            "source_session_id": _id(self.source_session_id),
            "source_agent_id": _id(self.source_agent_id),
            "source_agent_revision_id": _id(self.source_agent_revision_id),
            "source_run_id": _id(self.source_run_id),
            "source_work_item_id": _id(self.source_work_item_id),
            "source_message_id": _id(self.source_message_id),
            "receiver_session_id": _id(self.receiver_session_id),
            "receiver_agent_id": _id(self.receiver_agent_id),
            "receiver_agent_revision_id": _id(self.receiver_agent_revision_id),
            "receiver_run_id": _id(self.receiver_run_id),
            "receiver_work_item_id": _id(self.receiver_work_item_id),
            "receiver_message_id": _id(self.receiver_message_id),
            "transport_work_item_id": _id(self.transport_work_item_id),
            "kind": self.kind,
            "state": self.state,
            "sequence": int(self.sequence or 0),
            "causation_id": self.causation_id,
            "depth": int(self.depth or 0),
            "expected_control_revision": int(self.expected_control_revision or 0),
            "idempotency_key": self.idempotency_key,
            "request_digest": self.request_digest,
            "response_digest": self.response_digest,
            "available_at": _dt(self.available_at),
            "deadline": _dt(self.deadline),
            "attempt_count": int(self.attempt_count or 0),
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict


class AgentDialogueInput(Base):
    """Reference-only input/answer ledger row for one dialogue."""

    __tablename__ = "agent_dialogue_inputs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dialogue_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_dialogues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delivery_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_dialogue_deliveries.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_run_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_work_item_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_work_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_message_id = Column(
        UUID(as_uuid=True), ForeignKey("conversation_messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    input_kind = Column(String(32), nullable=False, default="agent_question", server_default="agent_question", index=True)
    reference_id = Column(String(255), nullable=True, index=True)
    expected_control_revision = Column(Integer, nullable=False, default=0, server_default="0")
    state = Column(String(16), nullable=False, default="pending", server_default="pending", index=True)
    answer_message_id = Column(
        UUID(as_uuid=True), ForeignKey("conversation_messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    answer_digest = Column(String(64), nullable=True)
    revision = Column(Integer, nullable=False, default=1, server_default="1")
    deadline = Column(DateTime, nullable=True, index=True)
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        index=True,
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    dialogue = relationship("AgentDialogue", back_populates="inputs")
    delivery = relationship("AgentDialogueDelivery", foreign_keys=[delivery_id], viewonly=True)

    __table_args__ = (
        UniqueConstraint("dialogue_id", "revision", name="uq_agent_dialogue_inputs_revision"),
        UniqueConstraint("dialogue_id", "delivery_id", name="uq_agent_dialogue_inputs_delivery"),
        Index("ix_agent_dialogue_inputs_state_deadline", "state", "deadline"),
        CheckConstraint("input_kind IN ('agent_question','human_question')", name="ck_agent_dialogue_inputs_kind"),
        CheckConstraint("state IN ('pending','answered','invalidated','blocked','expired','cancelled')", name="ck_agent_dialogue_inputs_state"),
        CheckConstraint("expected_control_revision >= 0", name="ck_agent_dialogue_inputs_control_revision"),
        CheckConstraint("revision BETWEEN 1 AND 1000", name="ck_agent_dialogue_inputs_revision"),
        CheckConstraint("answer_digest IS NULL OR length(answer_digest) = 64", name="ck_agent_dialogue_inputs_answer_digest"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _id(self.id),
            "dialogue_id": _id(self.dialogue_id),
            "delivery_id": _id(self.delivery_id),
            "source_run_id": _id(self.source_run_id),
            "source_work_item_id": _id(self.source_work_item_id),
            "source_message_id": _id(self.source_message_id),
            "input_kind": self.input_kind,
            "reference_id": self.reference_id,
            "expected_control_revision": int(self.expected_control_revision or 0),
            "state": self.state,
            "answer_message_id": _id(self.answer_message_id),
            "answer_digest": self.answer_digest,
            "revision": int(self.revision or 0),
            "deadline": _dt(self.deadline),
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict


__all__ = [
    "AGENT_PRIVATE_ROOT_PURPOSES",
    "AGENT_PRIVATE_ROOT_STATES",
    "AGENT_DIALOGUE_STATES",
    "AGENT_DIALOGUE_DELIVERY_KINDS",
    "AGENT_DIALOGUE_DELIVERY_STATES",
    "AGENT_DIALOGUE_INPUT_KINDS",
    "AGENT_DIALOGUE_INPUT_STATES",
    "AgentPrivateRoot",
    "AgentDialogue",
    "AgentDialogueBudgetRoot",
    "AgentDialogueReservation",
    "AgentDialogueUsage",
    "AgentDialogueDelivery",
    "AgentDialogueInput",
]
