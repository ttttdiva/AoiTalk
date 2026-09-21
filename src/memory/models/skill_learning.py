"""Durable Skill learning control-plane models.

These tables do not create a second Memory authority. They persist proposal,
usage-evidence, and append-only proposal-history records for the existing Skill
system. Live Skill mutation remains file-backed and explicit-apply only.
"""

from __future__ import annotations

import json
import uuid
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
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .base import Base, _encrypted_json_property, _encrypted_text_property
from ...security.skill_content_privacy import redact_secret_like_skill_content


def _public_provenance(value: Any) -> Any:
    """Return provenance without leaking legacy/plaintext secret material."""
    candidate = value
    if isinstance(candidate, str):
        stripped = candidate.strip()
        if not stripped:
            return {}
        try:
            candidate = json.loads(stripped)
        except (TypeError, ValueError, json.JSONDecodeError):
            candidate = stripped
    if candidate in (None, ""):
        return {}
    return redact_secret_like_skill_content(candidate)


class SkillUsageReceipt(Base):
    """Evidence that one concrete Skill was actually invoked."""

    __tablename__ = "skill_usage_receipts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(200), nullable=False, index=True)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversation_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    message_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversation_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    agent_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tool_call_id = Column(String(160), nullable=True, index=True)
    invocation_path = Column(String(32), nullable=False, index=True)
    outcome = Column(String(16), nullable=False, index=True)
    skill_name = Column(String(160), nullable=False, index=True)
    skill_scope = Column(String(16), nullable=False, index=True)
    skill_path = Column(String(512), nullable=False)
    skill_hash = Column(String(64), nullable=False, index=True)
    skill_version = Column(String(80), nullable=False)
    idempotency_key = Column(String(128), nullable=False, unique=True, index=True)
    _provenance = Column("provenance", Text, nullable=False, default="")
    provenance = _encrypted_json_property(
        "_provenance",
        "skill_usage_receipts.provenance",
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "skill_scope IN ('global', 'project')",
            name="ck_skill_usage_receipts_scope",
        ),
        CheckConstraint(
            "outcome IN ('success', 'error')",
            name="ck_skill_usage_receipts_outcome",
        ),
        Index(
            "ix_skill_usage_receipts_skill_created",
            "skill_scope",
            "skill_name",
            "created_at",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "user_id": self.user_id,
            "project_id": str(self.project_id) if self.project_id else None,
            "session_id": str(self.session_id) if self.session_id else None,
            "message_id": str(self.message_id) if self.message_id else None,
            "agent_run_id": str(self.agent_run_id) if self.agent_run_id else None,
            "tool_call_id": self.tool_call_id,
            "invocation_path": self.invocation_path,
            "outcome": self.outcome,
            "skill_name": self.skill_name,
            "skill_scope": self.skill_scope,
            "skill_path": self.skill_path,
            "skill_hash": self.skill_hash,
            "skill_version": self.skill_version,
            "provenance": _public_provenance(self.provenance),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class SkillProposal(Base):
    """Proposal-first mutation record for one canonical Skill target."""

    __tablename__ = "skill_proposals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(200), nullable=False, index=True)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    receipt_id = Column(
        UUID(as_uuid=True),
        ForeignKey("skill_usage_receipts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    operation = Column(String(16), nullable=False, index=True)
    reason_type = Column(String(24), nullable=False, default="manual", index=True)
    target_scope = Column(String(16), nullable=False, index=True)
    target_name = Column(String(160), nullable=False, index=True)
    target_path = Column(String(512), nullable=False)
    status = Column(String(16), nullable=False, default="pending", index=True)
    proposed_content = Column(JSON, nullable=False, default=dict)
    base_snapshot = Column(JSON, nullable=True)
    _base_text = Column("base_text", Text, nullable=True)
    base_text = _encrypted_text_property(
        "_base_text",
        "skill_proposals.base_text",
    )
    base_hash = Column(String(64), nullable=True, index=True)
    base_version = Column(String(80), nullable=True)
    applied_hash = Column(String(64), nullable=True, index=True)
    applied_version = Column(String(80), nullable=True)
    idempotency_key = Column(String(128), nullable=False, unique=True, index=True)
    _provenance = Column("provenance", Text, nullable=False, default="")
    provenance = _encrypted_json_property(
        "_provenance",
        "skill_proposals.provenance",
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
    applied_at = Column(DateTime, nullable=True)
    rejected_at = Column(DateTime, nullable=True)
    stale_at = Column(DateTime, nullable=True)
    rolled_back_at = Column(DateTime, nullable=True)

    receipt = relationship("SkillUsageReceipt")
    history = relationship(
        "SkillProposalHistory",
        back_populates="proposal",
        cascade="all, delete-orphan",
        order_by="SkillProposalHistory.sequence",
        lazy="selectin",
    )

    __table_args__ = (
        CheckConstraint(
            "operation IN ('create', 'update')",
            name="ck_skill_proposals_operation",
        ),
        CheckConstraint(
            "reason_type IN ('manual', 'correction', 'procedure')",
            name="ck_skill_proposals_reason_type",
        ),
        CheckConstraint(
            "target_scope IN ('global', 'project')",
            name="ck_skill_proposals_scope",
        ),
        CheckConstraint(
            "status IN ('pending', 'applied', 'rejected', 'stale')",
            name="ck_skill_proposals_status",
        ),
        Index(
            "ix_skill_proposals_target_status",
            "target_scope",
            "project_id",
            "target_name",
            "status",
        ),
    )

    def to_dict(self, *, include_history: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": str(self.id),
            "user_id": self.user_id,
            "project_id": str(self.project_id) if self.project_id else None,
            "receipt_id": str(self.receipt_id) if self.receipt_id else None,
            "operation": self.operation,
            "reason_type": self.reason_type,
            "target_scope": self.target_scope,
            "target_name": self.target_name,
            "target_path": self.target_path,
            "status": self.status,
            "proposed_content": redact_secret_like_skill_content(
                self.proposed_content or {}
            ),
            "base_snapshot": redact_secret_like_skill_content(
                self.base_snapshot
            ),
            "base_hash": self.base_hash,
            "base_version": self.base_version,
            "applied_hash": self.applied_hash,
            "applied_version": self.applied_version,
            "provenance": _public_provenance(self.provenance),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "applied_at": self.applied_at.isoformat() if self.applied_at else None,
            "rejected_at": self.rejected_at.isoformat() if self.rejected_at else None,
            "stale_at": self.stale_at.isoformat() if self.stale_at else None,
            "rolled_back_at": (
                self.rolled_back_at.isoformat() if self.rolled_back_at else None
            ),
        }
        if include_history:
            payload["history"] = [item.to_dict() for item in self.history or []]
        return payload


class SkillProposalHistory(Base):
    """Append-only audit history for a Skill proposal."""

    __tablename__ = "skill_proposal_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    proposal_id = Column(
        UUID(as_uuid=True),
        ForeignKey("skill_proposals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence = Column(Integer, nullable=False)
    event = Column(String(24), nullable=False, index=True)
    actor_user_id = Column(String(200), nullable=False)
    from_status = Column(String(16), nullable=True)
    to_status = Column(String(16), nullable=True)
    observed_hash = Column(String(64), nullable=True)
    result_hash = Column(String(64), nullable=True)
    _details = Column("details", Text, nullable=False, default="")
    details = _encrypted_json_property(
        "_details",
        "skill_proposal_history.details",
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    proposal = relationship("SkillProposal", back_populates="history")

    __table_args__ = (
        UniqueConstraint(
            "proposal_id",
            "sequence",
            name="uq_skill_proposal_history_sequence",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "proposal_id": str(self.proposal_id),
            "sequence": self.sequence,
            "event": self.event,
            "actor_user_id": self.actor_user_id,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "observed_hash": self.observed_hash,
            "result_hash": self.result_hash,
            "details": redact_secret_like_skill_content(
                self.details or {}
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }