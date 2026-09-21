"""Durable automation and scheduler state models."""

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID

from .base import Base, _encrypted_json_property


class HeartbeatRunState(Base):
    """Durable scheduler state for one Heartbeat and execution scope."""

    __tablename__ = "heartbeat_run_states"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    heartbeat_name = Column(
        String(120),
        nullable=False,
    )
    scope_type = Column(
        String(16),
        nullable=False,
    )
    scope_id = Column(
        String(120),
        nullable=False,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
    )
    last_started_at = Column(
        DateTime,
        nullable=True,
    )
    last_completed_at = Column(
        DateTime,
        nullable=True,
    )
    last_success_at = Column(
        DateTime,
        nullable=True,
    )
    next_due_at = Column(
        DateTime,
        nullable=True,
    )
    status = Column(
        String(16),
        default="idle",
        server_default="idle",
        nullable=False,
    )
    error_message = Column(
        Text,
        nullable=True,
    )
    _cursor_json = Column(
        "cursor_json",
        JSON,
        nullable=True,
    )
    cursor_json = _encrypted_json_property(
        "_cursor_json",
        "heartbeat_run_states.cursor_json",
    )
    created_at = Column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "heartbeat_name",
            "scope_type",
            "scope_id",
            name="uq_heartbeat_run_states_heartbeat_scope",
        ),
        CheckConstraint(
            "heartbeat_name <> ''",
            name="ck_heartbeat_run_states_heartbeat_name",
        ),
        CheckConstraint(
            "scope_type IN ('project', 'global')",
            name="ck_heartbeat_run_states_scope_type",
        ),
        CheckConstraint(
            "("
            "scope_type = 'project' AND project_id IS NOT NULL AND scope_id <> ''"
            ") OR ("
            "scope_type = 'global' AND project_id IS NULL AND scope_id = 'global'"
            ")",
            name="ck_heartbeat_run_states_scope_identity",
        ),
        CheckConstraint(
            "status IN ('idle', 'running', 'failed')",
            name="ck_heartbeat_run_states_status",
        ),
        Index(
            "ix_heartbeat_run_states_project_id",
            "project_id",
        ),
        Index(
            "ix_heartbeat_run_states_next_due_at",
            "next_due_at",
        ),
        Index(
            "ix_heartbeat_run_states_status_next_due_at",
            "status",
            "next_due_at",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "heartbeat_name": self.heartbeat_name,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "project_id": str(self.project_id) if self.project_id else None,
            "last_started_at": (
                self.last_started_at.isoformat()
                if self.last_started_at
                else None
            ),
            "last_completed_at": (
                self.last_completed_at.isoformat()
                if self.last_completed_at
                else None
            ),
            "last_success_at": (
                self.last_success_at.isoformat()
                if self.last_success_at
                else None
            ),
            "next_due_at": (
                self.next_due_at.isoformat()
                if self.next_due_at
                else None
            ),
            "status": self.status,
            "error_message": self.error_message,
            "cursor_json": self.cursor_json,
            "created_at": (
                self.created_at.isoformat()
                if self.created_at
                else None
            ),
            "updated_at": (
                self.updated_at.isoformat()
                if self.updated_at
                else None
            ),
        }
