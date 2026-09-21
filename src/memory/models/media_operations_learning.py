"""MediaOps WS8 proposal-first learning model.

Learning proposals are human-review artifacts.  They contain bounded prose,
hash-only evidence references and confidence metadata; they never contain raw
metric payloads and never mutate Skills or managed Memory.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


class LearningProposalStatus(str, Enum):
    """String constants kept dependency-free for route/service use."""

    PENDING_REVIEW = "pending_review"
    REJECTED = "rejected"
    ACCEPTED = "accepted"
    STALE = "stale"


LEARNING_PROPOSAL_STATUS_VALUES = tuple(
    item.value for item in LearningProposalStatus
)


def _uuid(value: Any) -> str | None:
    return str(value) if value is not None else None


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class LearningProposal(Base):
    """An evidence-backed, review-gated learning recommendation."""

    __tablename__ = "media_learning_proposals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    subject_type = Column(String(32), nullable=False)
    subject_ref = Column(String(164), nullable=False)
    proposal_type = Column(String(32), nullable=False, default="learning")
    title = Column(String(255), nullable=False)
    summary = Column(Text, nullable=False)
    recommendation = Column(Text, nullable=False)
    evidence_refs = Column(JSON, nullable=False, default=list)
    # A proposal is a durable, reviewable diff rather than free-form model
    # output.  Keep the target and the before/after values in bounded JSON so
    # an approval can revalidate the exact Persona revision without consulting
    # an external model or managed-memory store.
    human_decision_refs_json = Column(
        "human_decision_refs",
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    target_fields_json = Column(
        "target_fields",
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    proposed_before_json = Column(
        "proposed_before",
        JSON,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    proposed_after_json = Column(
        "proposed_after",
        JSON,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    expected_persona_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_persona_revisions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    expected_persona_revision_version = Column(Integer, nullable=True)
    expected_persona_revision_hash = Column(String(64), nullable=True, index=True)
    # Review actions are append-only entries in this JSON ledger.  The
    # service never exposes an in-place history edit/delete command and uses
    # action idempotency keys to make retries deterministic.
    review_history_json = Column(
        "review_history",
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    applied_persona_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_persona_revisions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    applied_persona_revision_version = Column(Integer, nullable=True)
    applied_persona_revision_hash = Column(String(64), nullable=True, index=True)
    window_start = Column(DateTime, nullable=False)
    window_end = Column(DateTime, nullable=False)
    confidence = Column(Float, nullable=False)
    uncertainty = Column(Float, nullable=False)
    human_review_required = Column(Boolean, nullable=False, default=True, server_default="1")
    review_policy = Column(String(64), nullable=False, default="human_review_before_apply")
    status = Column(String(16), nullable=False, default=LearningProposalStatus.PENDING_REVIEW, index=True)
    proposal_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=False)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_review', 'rejected', 'accepted', 'stale')",
            name="ck_media_learning_proposals_status",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_media_learning_proposals_confidence",
        ),
        CheckConstraint(
            "uncertainty >= 0 AND uncertainty <= 1",
            name="ck_media_learning_proposals_uncertainty",
        ),
        CheckConstraint(
            "human_review_required = TRUE",
            name="ck_media_learning_proposals_human_review",
        ),
        CheckConstraint(
            "length(proposal_hash) = 64",
            name="ck_media_learning_proposals_hash",
        ),
        CheckConstraint(
            "expected_persona_revision_hash IS NULL OR length(expected_persona_revision_hash) = 64",
            name="ck_media_learning_proposals_expected_revision_hash",
        ),
        CheckConstraint(
            "applied_persona_revision_hash IS NULL OR length(applied_persona_revision_hash) = 64",
            name="ck_media_learning_proposals_applied_revision_hash",
        ),
        Index("ix_media_learning_proposals_owner_project", "owner_user_id", "project_id"),
        Index(
            "uq_media_learning_proposals_personal_idempotency",
            "owner_user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_media_learning_proposals_project_idempotency",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
            sqlite_where=text("project_id IS NOT NULL"),
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "subject_type": self.subject_type,
            "subject_ref": self.subject_ref,
            "proposal_type": self.proposal_type,
            "title": self.title,
            "summary": self.summary,
            "recommendation": self.recommendation,
            "evidence_refs": self.evidence_refs or [],
            "human_decision_refs": list(self.human_decision_refs_json or []),
            "target_fields": list(self.target_fields_json or []),
            "proposed_before": dict(self.proposed_before_json or {}),
            "proposed_after": dict(self.proposed_after_json or {}),
            "expected_persona_revision_id": _uuid(self.expected_persona_revision_id),
            "expected_persona_revision_version": self.expected_persona_revision_version,
            "expected_persona_revision_hash": self.expected_persona_revision_hash,
            "review_history": list(self.review_history_json or []),
            "applied_persona_revision_id": _uuid(self.applied_persona_revision_id),
            "applied_persona_revision_version": self.applied_persona_revision_version,
            "applied_persona_revision_hash": self.applied_persona_revision_hash,
            "window_start": _dt(self.window_start),
            "window_end": _dt(self.window_end),
            "confidence": self.confidence,
            "uncertainty": self.uncertainty,
            "human_review_required": True,
            "review_policy": self.review_policy,
            "status": self.status,
            "proposal_hash": self.proposal_hash,
            "idempotency_key": self.idempotency_key,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


__all__ = [
    "LearningProposalStatus",
    "LEARNING_PROPOSAL_STATUS_VALUES",
    "LearningProposal",
]
