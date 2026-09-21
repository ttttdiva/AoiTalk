"""Durable, bounded Heartbeat execution history.

``HeartbeatRunState`` is the scheduler's mutable source of truth.  This
module deliberately keeps the append-only execution log in a separate model
so a run can be inspected after the scheduler has moved on to its next due
time.  Values written by :mod:`src.heartbeat.history` are already projected
to a small, non-sensitive shape; this model therefore does not carry model
transcripts, evidence bodies, tool payloads, or credentials.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    JSON,
    String,
)
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


# The operational log deliberately uses a small, presentation-independent
# vocabulary.  Runner/provider-specific labels (``ok``, ``timeout``,
# ``executor_unavailable``) are normalized by the repository before a row is
# written.  ``stale`` marks a run recovered after an interrupted process.
HEARTBEAT_HISTORY_STATUSES = frozenset(
    {"running", "succeeded", "failed", "stale"}
)


class HeartbeatRunHistory(Base):
    """One durable Heartbeat execution attempt.

    The row is created at claim/start time and completed in a later
    transaction.  ``success`` is nullable while a row is ``running`` and is
    explicit for terminal states, which makes operational queries independent
    of presentation-specific status labels such as ``ok`` or ``error``.
    """

    __tablename__ = "heartbeat_run_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    heartbeat_name = Column(String(120), nullable=False)
    mode = Column(String(32), nullable=False)
    scope_type = Column(String(16), nullable=False)
    scope_id = Column(String(120), nullable=False)
    project_id = Column(
        UUID(as_uuid=True),
        nullable=True,
    )
    started_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    status = Column(
        String(32),
        nullable=False,
        default="running",
        server_default="running",
        index=True,
    )
    success = Column(Boolean, nullable=True)

    memory_upsert_count = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    forgotten_count = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    question_count = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    continuation_pending = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    # Continuation is intentionally represented as a boolean only.  The
    # scheduler's encrypted cursor remains in ``HeartbeatRunState`` and is not
    # duplicated in an operator-facing history row.
    questions_json = Column(JSON, nullable=True)
    # A history row is an operator-facing projection, not a transcript.  Keep
    # the database column bounded as a second defence in addition to the
    # repository's clipping/redaction.
    result_summary = Column(String(512), nullable=True)
    safe_error_code = Column(String(64), nullable=True)

    # ``forced`` distinguishes a manual Settings trigger from the normal
    # scheduler path without retaining request/user payloads.
    forced = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    # Action outcomes are useful operational metadata.  Only aggregate counts
    # are retained; action config/output/error payloads never enter history.
    generic_action_count = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    generic_action_failure_count = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        CheckConstraint(
            "heartbeat_name <> ''",
            name="ck_heartbeat_run_history_heartbeat_name",
        ),
        CheckConstraint(
            "mode IN ('agent_check', 'project_steward')",
            name="ck_heartbeat_run_history_mode",
        ),
        CheckConstraint(
            "scope_type IN ('project', 'global')",
            name="ck_heartbeat_run_history_scope_type",
        ),
        CheckConstraint(
            "("
            "scope_type = 'project' AND project_id IS NOT NULL AND scope_id <> ''"
            ") OR ("
            "scope_type = 'global' AND project_id IS NULL AND scope_id = 'global'"
            ")",
            name="ck_heartbeat_run_history_scope_identity",
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'stale')",
            name="ck_heartbeat_run_history_status",
        ),
        CheckConstraint(
            "memory_upsert_count >= 0 AND memory_upsert_count <= 100000",
            name="ck_heartbeat_run_history_memory_count",
        ),
        CheckConstraint(
            "forgotten_count >= 0 AND forgotten_count <= 100000",
            name="ck_heartbeat_run_history_forgotten_count",
        ),
        CheckConstraint(
            "question_count >= 0 AND question_count <= 100000",
            name="ck_heartbeat_run_history_question_count",
        ),
        CheckConstraint(
            "generic_action_count >= 0 AND generic_action_count <= 100000",
            name="ck_heartbeat_run_history_action_count",
        ),
        CheckConstraint(
            "generic_action_failure_count >= 0 AND generic_action_failure_count <= generic_action_count",
            name="ck_heartbeat_run_history_action_failure_count",
        ),
        Index(
            "ix_heartbeat_run_history_heartbeat_started",
            "heartbeat_name",
            "started_at",
            "id",
        ),
        Index(
            "ix_heartbeat_run_history_scope_started",
            "scope_type",
            "scope_id",
            "started_at",
            "id",
        ),
        Index(
            "ix_heartbeat_run_history_project_started",
            "project_id",
            "started_at",
            "id",
        ),
    )

    # These aliases keep the DTO/result vocabulary used by Project Steward
    # callers discoverable without duplicating database columns.
    @property
    def memory_upserts(self) -> int:
        return int(self.memory_upsert_count or 0)

    @memory_upserts.setter
    def memory_upserts(self, value: Any) -> None:
        self.memory_upsert_count = int(value or 0)

    @property
    def forgotten(self) -> int:
        return int(self.forgotten_count or 0)

    @forgotten.setter
    def forgotten(self, value: Any) -> None:
        self.forgotten_count = int(value or 0)

    @property
    def questions(self) -> list[dict[str, Any]]:
        value = self.questions_json
        return list(value) if isinstance(value, list) else []

    @questions.setter
    def questions(self, value: Any) -> None:
        self.questions_json = value if isinstance(value, list) else []

    def to_dict(self) -> Dict[str, Any]:
        """Return a compact operational snapshot.

        Repository-created rows have already passed the stronger projection
        in ``src.heartbeat.history``.  ``to_dict`` still avoids exposing any
        SQLAlchemy internals and gives callers stable count aliases.
        """

        def _iso(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        questions = self.questions_json if isinstance(self.questions_json, list) else []
        return {
            "id": str(self.id),
            "heartbeat_name": self.heartbeat_name,
            "mode": self.mode,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "project_id": str(self.project_id) if self.project_id else None,
            "started_at": _iso(self.started_at),
            "completed_at": _iso(self.completed_at),
            "status": self.status,
            "success": self.success,
            "memory_upsert_count": int(self.memory_upsert_count or 0),
            "memory_upserts": int(self.memory_upsert_count or 0),
            "forgotten_count": int(self.forgotten_count or 0),
            "forgotten": int(self.forgotten_count or 0),
            "question_count": int(self.question_count or 0),
            "questions_count": int(self.question_count or 0),
            "continuation_pending": bool(self.continuation_pending),
            "forced": bool(self.forced),
            "generic_action_count": int(self.generic_action_count or 0),
            "generic_action_failure_count": int(
                self.generic_action_failure_count or 0
            ),
            "questions": questions,
            "result_summary": self.result_summary,
            "safe_error_code": self.safe_error_code,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


# A few integrations historically used the word ``Record`` for append-only
# operational rows.  Keep aliases cheap and explicit rather than creating a
# second mapped table.
HeartbeatRunRecord = HeartbeatRunHistory
HeartbeatHistoryEntry = HeartbeatRunHistory


__all__ = [
    "HEARTBEAT_HISTORY_STATUSES",
    "HeartbeatHistoryEntry",
    "HeartbeatRunHistory",
    "HeartbeatRunRecord",
]
