"""Durable agent run tracking models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .base import Base


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class AgentRun(Base):
    """One durable execution unit for an assistant turn or child agent."""

    __tablename__ = "agent_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    root_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id"),
        nullable=True,
        index=True,
    )
    parent_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id"),
        nullable=True,
        index=True,
    )
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversation_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    trigger_message_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversation_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    app_id = Column(
        UUID(as_uuid=True),
        ForeignKey("apps.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Target は (app_id, app_target_id) の複合 FK で App に閉じ込める。
    app_target_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    base_revision = Column(String(80), nullable=True, index=True)
    result_revision = Column(String(80), nullable=True, index=True)
    user_id = Column(String(200), nullable=True, index=True)
    client_message_id = Column(String(512), nullable=True)
    client_message_key = Column(String(64), nullable=True)
    request_fingerprint = Column(String(64), nullable=True)
    run_type = Column(String(64), nullable=False, default="chat_turn", index=True)
    status = Column(String(32), nullable=False, default="queued", index=True)
    title = Column(String(255), nullable=False, default="")
    objective = Column(Text, nullable=False, default="")
    generation_profile = Column(String(64), nullable=True)
    provider = Column(String(80), nullable=True)
    model = Column(String(160), nullable=True)
    error = Column(Text, nullable=True)
    result = Column(JSON, default=dict)
    validation = Column(JSON, default=dict)
    run_metadata = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    last_event_at = Column(DateTime, nullable=True)

    events = relationship(
        "AgentRunEvent",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="AgentRunEvent.sequence",
    )
    tool_calls = relationship(
        "AgentRunToolCall",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="AgentRunToolCall.created_at",
    )
    parent = relationship(
        "AgentRun",
        remote_side=[id],
        foreign_keys=[parent_run_id],
        backref="children",
    )

    __table_args__ = (
        Index(
            "ix_agent_runs_session_status_created",
            "session_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_agent_runs_project_status_created",
            "project_id",
            "status",
            "created_at",
        ),
        UniqueConstraint(
            "session_id",
            "user_id",
            "client_message_key",
            name="uq_agent_runs_session_user_client_message_key",
        ),
        # 実 DB は ON DELETE SET NULL (app_target_id)。app_id は巻き込まない。
        ForeignKeyConstraint(
            ["app_id", "app_target_id"],
            ["app_targets.app_id", "app_targets.id"],
            name="fk_agent_runs_app_target_app",
            ondelete="SET NULL",
        ),
        # 複合 FK は MATCH SIMPLE のため app_id が NULL だと検査されない。
        CheckConstraint(
            "app_target_id IS NULL OR app_id IS NOT NULL",
            name="ck_agent_runs_app_target_requires_app",
        ),
    )

    def to_dict(
        self,
        *,
        include_events: bool = False,
        include_tool_calls: bool = False,
        include_edges: bool = False,
    ) -> Dict[str, Any]:
        metadata = self.run_metadata if isinstance(self.run_metadata, dict) else {}
        result = self.result if isinstance(self.result, dict) else {}
        usage = metadata.get("usage") or result.get("usage")
        payload: Dict[str, Any] = {
            "id": str(self.id),
            "root_run_id": str(self.root_run_id) if self.root_run_id else None,
            "parent_run_id": str(self.parent_run_id) if self.parent_run_id else None,
            "session_id": str(self.session_id) if self.session_id else None,
            "trigger_message_id": (
                str(self.trigger_message_id) if self.trigger_message_id else None
            ),
            "project_id": str(self.project_id) if self.project_id else None,
            "app_id": str(self.app_id) if self.app_id else None,
            "app_target_id": str(self.app_target_id) if self.app_target_id else None,
            "base_revision": self.base_revision,
            "result_revision": self.result_revision,
            "user_id": self.user_id,
            "client_message_id": self.client_message_id,
            "client_message_key": self.client_message_key,
            "request_fingerprint": self.request_fingerprint,
            "run_type": self.run_type,
            "status": self.status,
            "title": self.title,
            "objective": self.objective,
            "generation_profile": self.generation_profile,
            "provider": self.provider,
            "model": self.model,
            "error": self.error,
            "result": self.result or {},
            "validation": self.validation or {},
            "metadata": self.run_metadata or {},
            "usage": usage if isinstance(usage, dict) else None,
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
            "started_at": _dt(self.started_at),
            "ended_at": _dt(self.ended_at),
            "last_event_at": _dt(self.last_event_at),
        }
        if include_events:
            payload["events"] = [event.to_dict() for event in self.events or []]
        if include_tool_calls:
            payload["tool_calls"] = [
                tool_call.to_dict() for tool_call in self.tool_calls or []
            ]
        if include_edges:
            payload["child_edges"] = [
                edge.to_dict() for edge in getattr(self, "child_edges", []) or []
            ]
            payload["parent_edges"] = [
                edge.to_dict() for edge in getattr(self, "parent_edges", []) or []
            ]
        return payload


class ConversationDispatchOutbox(Base):
    """Durable hand-off from an idempotent REST dispatch to the in-process worker."""

    __tablename__ = "conversation_dispatch_outbox"

    run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversation_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(String(200), nullable=False)
    client_message_id = Column(String(512), nullable=False)
    client_message_key = Column(String(64), nullable=False)
    request_fingerprint = Column(String(64), nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    status = Column(String(32), nullable=False, default="pending", index=True)
    lease_owner = Column(String(64), nullable=True)
    lease_token = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    attempts = Column(Integer, nullable=False, default=0)
    delivered_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "user_id",
            "client_message_key",
            name="uq_conversation_dispatch_outbox_session_user_client_key",
        ),
    )


class AgentRunEvent(Base):
    """Append-only event emitted by an agent run."""

    __tablename__ = "agent_run_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence = Column(Integer, nullable=False)
    event_type = Column(String(80), nullable=False, index=True)
    status = Column(String(32), nullable=True)
    message = Column(Text, nullable=True)
    payload = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    run = relationship("AgentRun", back_populates="events")

    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_agent_run_events_sequence"),
        Index("ix_agent_run_events_run_created", "run_id", "created_at"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "run_id": str(self.run_id),
            "sequence": self.sequence,
            "event_type": self.event_type,
            "status": self.status,
            "message": self.message,
            "payload": self.payload or {},
            "created_at": _dt(self.created_at),
        }


class AgentRunToolCall(Base):
    """Tool-call evidence associated with an agent run."""

    __tablename__ = "agent_run_tool_calls"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_run_events.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tool_name = Column(String(160), nullable=False, index=True)
    tool_call_id = Column(String(160), nullable=True)
    arguments = Column(JSON, default=dict)
    result = Column(Text, nullable=True)
    success = Column(Boolean, default=False, nullable=False, index=True)
    mutation_confirmed = Column(Boolean, default=False, nullable=False, index=True)
    result_metadata = Column(JSON, default=dict)
    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    run = relationship("AgentRun", back_populates="tool_calls")
    event = relationship("AgentRunEvent")

    __table_args__ = (
        Index("ix_agent_run_tool_calls_run_tool", "run_id", "tool_name"),
        UniqueConstraint(
            "run_id",
            "tool_call_id",
            name="uq_agent_run_tool_calls_run_tool_call_id",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "run_id": str(self.run_id),
            "event_id": str(self.event_id) if self.event_id else None,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "arguments": self.arguments or {},
            "result": self.result,
            "success": bool(self.success),
            "mutation_confirmed": bool(self.mutation_confirmed),
            "metadata": self.result_metadata or {},
            "started_at": _dt(self.started_at),
            "ended_at": _dt(self.ended_at),
            "duration_ms": self.duration_ms,
            "created_at": _dt(self.created_at),
        }


class AgentRunEdge(Base):
    """Parent-child relationship between durable agent runs."""

    __tablename__ = "agent_run_edges"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    parent_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    child_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    purpose = Column(String(160), nullable=True)
    status = Column(String(32), nullable=False, default="open", index=True)
    edge_metadata = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    closed_at = Column(DateTime, nullable=True)

    parent_run = relationship(
        "AgentRun",
        foreign_keys=[parent_run_id],
        backref="child_edges",
    )
    child_run = relationship(
        "AgentRun",
        foreign_keys=[child_run_id],
        backref="parent_edges",
    )

    __table_args__ = (
        UniqueConstraint(
            "parent_run_id",
            "child_run_id",
            name="uq_agent_run_edges_parent_child",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "parent_run_id": str(self.parent_run_id),
            "child_run_id": str(self.child_run_id),
            "purpose": self.purpose,
            "status": self.status,
            "metadata": self.edge_metadata or {},
            "created_at": _dt(self.created_at),
            "closed_at": _dt(self.closed_at),
        }
