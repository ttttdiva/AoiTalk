"""MediaOps WS5 content variants, typed platform payloads and QA/rights ledger.

The model layer deliberately keeps platform data in a bounded semantic JSON
shape.  Validation is performed by ``MediaOperationsContentService`` before a
row is inserted; provider payloads, credentials and remote asset identifiers
never belong in these tables.
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
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


class ContentVariantPlatform(str, Enum):
    X = "x"
    PIXIV = "pixiv"
    DLSITE = "dlsite"
    PATREON = "patreon"
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"


CONTENT_VARIANT_PLATFORM_VALUES = tuple(
    value.value for value in ContentVariantPlatform
)


class QAAssessmentResult(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    REVIEW_REQUIRED = "review_required"


QA_ASSESSMENT_RESULT_VALUES = tuple(value.value for value in QAAssessmentResult)


class RightsAssessmentResult(str, Enum):
    CLEARED = "cleared"
    BLOCKED = "blocked"
    REVIEW_REQUIRED = "review_required"


RIGHTS_ASSESSMENT_RESULT_VALUES = tuple(
    value.value for value in RightsAssessmentResult
)


def _uuid(value: Any) -> str | None:
    return str(value) if value is not None else None


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class ContentVariant(Base):
    """Stable ACL-scoped identity for one ContentItem/platform pair.

    Editable content lives exclusively in ``ContentVariantRevision`` rows.  A
    stable variant therefore cannot be overwritten and remains safe to refer to
    from later publication/metrics slices.
    """

    __tablename__ = "media_content_variants"

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
    content_item_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_content_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    platform = Column(String(16), nullable=False, index=True)
    create_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=False)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "platform IN ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')",
            name="ck_media_content_variants_platform",
        ),
        CheckConstraint(
            "length(create_hash) = 64",
            name="ck_media_content_variants_create_hash",
        ),
        Index(
            "ix_media_content_variants_owner_project",
            "owner_user_id",
            "project_id",
        ),
        Index(
            "uq_media_content_variants_personal_identity",
            "owner_user_id",
            "content_item_id",
            "platform",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_media_content_variants_project_identity",
            "project_id",
            "content_item_id",
            "platform",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
            sqlite_where=text("project_id IS NOT NULL"),
        ),
        Index(
            "uq_media_content_variants_personal_idempotency",
            "owner_user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_media_content_variants_project_idempotency",
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
            "content_item_id": _uuid(self.content_item_id),
            "platform": self.platform,
            "create_hash": self.create_hash,
            "idempotency_key": self.idempotency_key,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class ContentVariantRevision(Base):
    """Immutable typed payload revision pinned to upstream hashes."""

    __tablename__ = "media_content_variant_revisions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content_variant_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_content_variants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
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
    version = Column(Integer, nullable=False)
    content_item_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_content_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content_item_hash = Column(String(64), nullable=False, index=True)
    persona_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_persona_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    persona_revision_hash = Column(String(64), nullable=False, index=True)
    platform_account_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_platform_accounts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    platform_account_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_platform_account_revisions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    platform_account_revision_hash = Column(String(64), nullable=True, index=True)
    platform = Column(String(16), nullable=False, index=True)
    # The service stores the normalized, closed semantic payload here.  It is
    # not a provider response/blob and never contains credentials.
    payload_json = Column("payload", JSON, nullable=False)
    # Ordered references contain only local GenerationOutput UUIDs and hashes.
    generation_output_refs_json = Column(
        "generation_output_refs",
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    source_evidence_json = Column(
        "source_evidence",
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    content_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=True)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "version > 0",
            name="ck_media_content_variant_revisions_version",
        ),
        CheckConstraint(
            "platform IN ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')",
            name="ck_media_content_variant_revisions_platform",
        ),
        CheckConstraint(
            "length(content_item_hash) = 64",
            name="ck_media_content_variant_revisions_content_item_hash",
        ),
        CheckConstraint(
            "length(persona_revision_hash) = 64",
            name="ck_media_content_variant_revisions_persona_hash",
        ),
        CheckConstraint(
            "platform_account_revision_hash IS NULL OR length(platform_account_revision_hash) = 64",
            name="ck_media_content_variant_revisions_account_hash",
        ),
        CheckConstraint(
            "length(content_hash) = 64",
            name="ck_media_content_variant_revisions_hash",
        ),
        UniqueConstraint(
            "content_variant_id",
            "version",
            name="uq_media_content_variant_revisions_version",
        ),
        UniqueConstraint(
            "content_variant_id",
            "idempotency_key",
            name="uq_media_content_variant_revisions_idempotency",
        ),
        Index(
            "ix_media_content_variant_revisions_owner_project",
            "owner_user_id",
            "project_id",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "content_variant_id": _uuid(self.content_variant_id),
            "variant_id": _uuid(self.content_variant_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "version": int(self.version),
            "content_item_id": _uuid(self.content_item_id),
            "content_item_hash": self.content_item_hash,
            "persona_revision_id": _uuid(self.persona_revision_id),
            "persona_revision_hash": self.persona_revision_hash,
            "platform_account_id": _uuid(self.platform_account_id),
            "platform_account_revision_id": _uuid(self.platform_account_revision_id),
            "platform_account_revision_hash": self.platform_account_revision_hash,
            "platform": self.platform,
            "payload": self.payload_json or {},
            "generation_output_refs": list(self.generation_output_refs_json or []),
            "source_evidence": list(self.source_evidence_json or []),
            "content_hash": self.content_hash,
            "idempotency_key": self.idempotency_key,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class QAAssessment(Base):
    """Append-only QA assessment pinned to one immutable variant revision."""

    __tablename__ = "media_content_variant_qa_assessments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content_variant_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_content_variants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content_variant_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_content_variant_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
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
    revision_hash = Column(String(64), nullable=False, index=True)
    policy_revision_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    policy_revision_hash = Column(String(64), nullable=False, index=True)
    result = Column(String(24), nullable=False, index=True)
    checks_json = Column(
        "checks",
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    findings_json = Column(
        "findings",
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    evidence_json = Column(
        "evidence",
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    assessment_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=True, index=True)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "result IN ('passed', 'failed', 'review_required')",
            name="ck_media_content_variant_qa_result",
        ),
        CheckConstraint(
            "length(revision_hash) = 64",
            name="ck_media_content_variant_qa_revision_hash",
        ),
        CheckConstraint(
            "length(policy_revision_hash) = 64",
            name="ck_media_content_variant_qa_policy_hash",
        ),
        CheckConstraint(
            "length(assessment_hash) = 64",
            name="ck_media_content_variant_qa_hash",
        ),
        UniqueConstraint(
            "content_variant_revision_id",
            "assessment_hash",
            name="uq_media_content_variant_qa_assessment_hash",
        ),
        UniqueConstraint(
            "content_variant_revision_id",
            "idempotency_key",
            name="uq_media_content_variant_qa_idempotency",
        ),
        Index(
            "ix_media_content_variant_qa_owner_project",
            "owner_user_id",
            "project_id",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "content_variant_id": _uuid(self.content_variant_id),
            "content_variant_revision_id": _uuid(self.content_variant_revision_id),
            "variant_revision_id": _uuid(self.content_variant_revision_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "revision_hash": self.revision_hash,
            "content_hash": self.revision_hash,
            "policy_revision_id": _uuid(self.policy_revision_id),
            "policy_revision_hash": self.policy_revision_hash,
            "result": self.result,
            "checks": list(self.checks_json or []),
            "findings": list(self.findings_json or []),
            "evidence": list(self.evidence_json or []),
            "assessment_hash": self.assessment_hash,
            "idempotency_key": self.idempotency_key,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class RightsAssessment(Base):
    """Append-only rights assessment pinned to one immutable variant revision."""

    __tablename__ = "media_content_variant_rights_assessments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content_variant_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_content_variants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content_variant_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_content_variant_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
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
    revision_hash = Column(String(64), nullable=False, index=True)
    policy_revision_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    policy_revision_hash = Column(String(64), nullable=False, index=True)
    result = Column(String(24), nullable=False, index=True)
    checks_json = Column(
        "checks",
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    findings_json = Column(
        "findings",
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    evidence_json = Column(
        "evidence",
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    assessment_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=True, index=True)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "result IN ('cleared', 'blocked', 'review_required')",
            name="ck_media_content_variant_rights_result",
        ),
        CheckConstraint(
            "length(revision_hash) = 64",
            name="ck_media_content_variant_rights_revision_hash",
        ),
        CheckConstraint(
            "length(policy_revision_hash) = 64",
            name="ck_media_content_variant_rights_policy_hash",
        ),
        CheckConstraint(
            "length(assessment_hash) = 64",
            name="ck_media_content_variant_rights_hash",
        ),
        UniqueConstraint(
            "content_variant_revision_id",
            "assessment_hash",
            name="uq_media_content_variant_rights_assessment_hash",
        ),
        UniqueConstraint(
            "content_variant_revision_id",
            "idempotency_key",
            name="uq_media_content_variant_rights_idempotency",
        ),
        Index(
            "ix_media_content_variant_rights_owner_project",
            "owner_user_id",
            "project_id",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "content_variant_id": _uuid(self.content_variant_id),
            "content_variant_revision_id": _uuid(self.content_variant_revision_id),
            "variant_revision_id": _uuid(self.content_variant_revision_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "revision_hash": self.revision_hash,
            "content_hash": self.revision_hash,
            "policy_revision_id": _uuid(self.policy_revision_id),
            "policy_revision_hash": self.policy_revision_hash,
            "result": self.result,
            "checks": list(self.checks_json or []),
            "findings": list(self.findings_json or []),
            "evidence": list(self.evidence_json or []),
            "assessment_hash": self.assessment_hash,
            "idempotency_key": self.idempotency_key,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


__all__ = [
    "CONTENT_VARIANT_PLATFORM_VALUES",
    "ContentVariantPlatform",
    "QA_ASSESSMENT_RESULT_VALUES",
    "QAAssessmentResult",
    "RIGHTS_ASSESSMENT_RESULT_VALUES",
    "RightsAssessmentResult",
    "ContentVariant",
    "ContentVariantRevision",
    "QAAssessment",
    "RightsAssessment",
]
