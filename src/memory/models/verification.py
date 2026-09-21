"""Durable machine-readable provenance for disposable verification data.

These tables are deliberately separate from Tasks/Projects/Users.  A
verification run is an append-only identity ledger; cleanup code uses the
ledger as its allow-list and never infers disposability from human-readable
names, lifecycle state, or age.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, synonym

from .base import Base


VERIFICATION_PROVENANCE_SCHEMA_VERSION = 1
VERIFICATION_RUN_STATUSES = frozenset(
    {"running", "succeeded", "failed", "aborted", "cleaned", "cleanup_failed"}
)
VERIFICATION_ARTIFACT_CLEANUP_STATUSES = frozenset(
    {"pending", "deleted", "already_absent", "failed", "skipped"}
)
VERIFICATION_CLEANUP_STATUSES = frozenset(
    {"running", "succeeded", "failed", "partial", "already_clean"}
)
VERIFICATION_CLEANUP_ITEM_STATUSES = frozenset(
    {"pending", "deleted", "already_absent", "failed", "skipped"}
)


def _utcnow() -> datetime:
    return datetime.utcnow()


class VerificationRun(Base):
    """One server-created verification execution and its allow-list scope."""

    __tablename__ = "verification_runs"

    def __init__(self, **kwargs: Any) -> None:
        # ``metadata`` is the conventional public spelling used by service
        # adapters, but SQLAlchemy reserves that class attribute for the
        # declarative ``MetaData`` object.  Map constructor input explicitly
        # to the persisted JSON column instead of allowing the default
        # declarative constructor to shadow ``Base.metadata`` on an instance.
        if "metadata" in kwargs and "metadata_json" not in kwargs:
            kwargs["metadata_json"] = kwargs.pop("metadata")
        super().__init__(**kwargs)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Keep the public/run identity explicit instead of relying on a human title
    # or a client-provided metadata field.  It is a UUID to make accidental
    # collisions and guessing materially harder.
    run_id = Column(UUID(as_uuid=True), nullable=False, unique=True, index=True, default=uuid.uuid4)
    schema_version = Column(
        Integer,
        nullable=False,
        default=VERIFICATION_PROVENANCE_SCHEMA_VERSION,
        server_default="1",
    )
    disposable = Column(Boolean, nullable=False, default=True, server_default="true")
    source = Column(String(255), nullable=False)
    # ``harness`` is retained as a concise operator-facing label.  ``source``
    # remains the canonical attribution and is always populated by the service.
    harness = Column(String(255), nullable=True)
    actor_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status = Column(
        String(32),
        nullable=False,
        default="running",
        server_default="running",
        index=True,
    )
    metadata_json = Column("metadata", JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=_utcnow, index=True)
    started_at = Column(DateTime, nullable=False, default=_utcnow, index=True)
    completed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    actor = relationship("User", foreign_keys=[actor_user_id])
    artifacts = relationship(
        "VerificationArtifactProvenance",
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="VerificationArtifactProvenance.created_at",
    )
    cleanup_runs = relationship(
        "VerificationCleanupRun",
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="VerificationCleanupRun.created_at",
    )

    # A verification run is always an explicitly disposable scope.  Keeping
    # this invariant in the DB protects direct SQL/model writes as well as the
    # Python service validation.
    __table_args__ = (
        CheckConstraint(
            "schema_version > 0",
            name="ck_verification_runs_schema_version_positive",
        ),
        CheckConstraint(
            "disposable = true",
            name="ck_verification_runs_disposable",
        ),
        CheckConstraint(
            "length(source) > 0",
            name="ck_verification_runs_source_nonempty",
        ),
        CheckConstraint(
            "status IN ('running','succeeded','failed','aborted','cleaned','cleanup_failed')",
            name="ck_verification_runs_status",
        ),
        Index(
            "ix_verification_runs_disposable_status_created",
            "disposable",
            "status",
            "created_at",
        ),
    )

    @property
    def run_metadata(self) -> dict[str, Any]:
        return self.metadata_json or {}

    @run_metadata.setter
    def run_metadata(self, value: dict[str, Any] | None) -> None:
        self.metadata_json = value or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id) if self.id else None,
            "run_id": str(self.run_id) if self.run_id else None,
            "schema_version": self.schema_version,
            "disposable": bool(self.disposable),
            "source": self.source,
            "harness": self.harness or self.source,
            "actor_user_id": str(self.actor_user_id) if self.actor_user_id else None,
            "status": self.status,
            "metadata": self.metadata_json or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class VerificationArtifactProvenance(Base):
    """Allow-listed identity for one entity created by a verification run.

    ``entity_id`` is intentionally text: some verification artifacts are UUID
    rows while others are paths, external IDs, or composite references.  The
    run/typed identity pair is unique so retries can attach idempotently.
    """

    __tablename__ = "verification_artifact_provenance"

    def __init__(self, **kwargs: Any) -> None:
        if "metadata" in kwargs and "metadata_json" not in kwargs:
            kwargs["metadata_json"] = kwargs.pop("metadata")
        super().__init__(**kwargs)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("verification_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    schema_version = Column(
        Integer,
        nullable=False,
        default=VERIFICATION_PROVENANCE_SCHEMA_VERSION,
        server_default="1",
    )
    disposable = Column(Boolean, nullable=False, default=True, server_default="true")
    entity_type = Column(String(64), nullable=False)
    entity_id = Column(String(512), nullable=False)
    source = Column(String(255), nullable=False)
    metadata_json = Column("metadata", JSON, nullable=False, default=dict)
    cleanup_status = Column(
        String(24),
        nullable=False,
        default="pending",
        server_default="pending",
        index=True,
    )
    cleanup_error_code = Column(String(96), nullable=True)
    cleaned_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    run = relationship("VerificationRun", back_populates="artifacts")
    cleanup_items = relationship(
        "VerificationCleanupItem",
        back_populates="artifact",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "entity_type",
            "entity_id",
            name="uq_verification_artifact_provenance_identity",
        ),
        CheckConstraint(
            "schema_version > 0",
            name="ck_verification_artifact_schema_version_positive",
        ),
        CheckConstraint(
            "disposable = true",
            name="ck_verification_artifact_disposable",
        ),
        CheckConstraint(
            "length(entity_type) > 0",
            name="ck_verification_artifact_entity_type_nonempty",
        ),
        CheckConstraint(
            "length(entity_id) > 0",
            name="ck_verification_artifact_entity_id_nonempty",
        ),
        CheckConstraint(
            "length(source) > 0",
            name="ck_verification_artifact_source_nonempty",
        ),
        CheckConstraint(
            "cleanup_status IN ('pending','deleted','already_absent','failed','skipped')",
            name="ck_verification_artifact_cleanup_status",
        ),
        Index(
            "ix_verification_artifact_run_type_status",
            "run_id",
            "entity_type",
            "cleanup_status",
        ),
    )

    @property
    def artifact_metadata(self) -> dict[str, Any]:
        return self.metadata_json or {}

    @artifact_metadata.setter
    def artifact_metadata(self, value: dict[str, Any] | None) -> None:
        self.metadata_json = value or {}

    # SQLAlchemy synonym (rather than a plain property) keeps compatibility
    # with adapters that query ``artifact.status`` while the canonical column
    # remains the more descriptive ``cleanup_status``.
    status = synonym("cleanup_status")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id) if self.id else None,
            "run_id": str(self.run_id) if self.run_id else None,
            "schema_version": self.schema_version,
            "disposable": bool(self.disposable),
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "source": self.source,
            "metadata": self.metadata_json or {},
            "cleanup_status": self.cleanup_status,
            "status": self.cleanup_status,
            "cleanup_error_code": self.cleanup_error_code,
            "cleaned_at": self.cleaned_at.isoformat() if self.cleaned_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class VerificationCleanupRun(Base):
    """Durable ledger for one explicit operator cleanup attempt."""

    __tablename__ = "verification_cleanup_runs"

    def __init__(self, **kwargs: Any) -> None:
        if "metadata" in kwargs and "metadata_json" not in kwargs:
            kwargs["metadata_json"] = kwargs.pop("metadata")
        super().__init__(**kwargs)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("verification_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status = Column(
        String(24),
        nullable=False,
        default="running",
        server_default="running",
        index=True,
    )
    confirmation_sha256 = Column(String(64), nullable=True)
    summary_json = Column(JSON, nullable=False, default=dict)
    metadata_json = Column("metadata", JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=_utcnow, index=True)
    started_at = Column(DateTime, nullable=False, default=_utcnow)
    completed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    run = relationship("VerificationRun", back_populates="cleanup_runs")
    actor = relationship("User", foreign_keys=[actor_user_id])
    items = relationship(
        "VerificationCleanupItem",
        back_populates="cleanup_run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="VerificationCleanupItem.created_at",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('running','succeeded','failed','partial','already_clean')",
            name="ck_verification_cleanup_runs_status",
        ),
        CheckConstraint(
            "confirmation_sha256 IS NULL OR length(confirmation_sha256) = 64",
            name="ck_verification_cleanup_runs_confirmation_sha256",
        ),
    )

    @property
    def summary(self) -> dict[str, Any]:
        return self.summary_json or {}

    @summary.setter
    def summary(self, value: dict[str, Any] | None) -> None:
        self.summary_json = value or {}

    # ``details`` was the name used by an early cleanup adapter.  Keep it as
    # an in-memory alias while persisting the bounded summary JSON under one
    # canonical column.
    @property
    def details(self) -> dict[str, Any]:
        return self.summary_json or {}

    @details.setter
    def details(self, value: dict[str, Any] | None) -> None:
        self.summary_json = value or {}

    # Compatibility aliases used by cleanup adapters.  The persisted column
    # remains ``confirmation_sha256`` so a raw confirmation/manifest payload
    # can never be retained in the ledger.
    # SQLAlchemy synonyms keep compatibility aliases usable in both Python
    # projections and ORM filters (the persisted columns remain canonical).
    manifest_digest = synonym("confirmation_sha256")
    cleanup_id = synonym("id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id) if self.id else None,
            "run_id": str(self.run_id) if self.run_id else None,
            "actor_user_id": str(self.actor_user_id) if self.actor_user_id else None,
            "status": self.status,
            "confirmation_sha256": self.confirmation_sha256,
            "manifest_digest": self.confirmation_sha256,
            "summary": self.summary_json or {},
            "details": self.summary_json or {},
            "metadata": self.metadata_json or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class VerificationCleanupItem(Base):
    """Per-entity result for a cleanup attempt (safe counters only)."""

    __tablename__ = "verification_cleanup_items"

    def __init__(self, **kwargs: Any) -> None:
        if "result" in kwargs and "result_json" not in kwargs:
            kwargs["result_json"] = kwargs.pop("result")
        super().__init__(**kwargs)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cleanup_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("verification_cleanup_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_id = Column(
        UUID(as_uuid=True),
        ForeignKey("verification_artifact_provenance.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("verification_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    entity_type = Column(String(64), nullable=False)
    entity_id = Column(String(512), nullable=False)
    status = Column(
        String(24),
        nullable=False,
        default="pending",
        server_default="pending",
        index=True,
    )
    safe_error_code = Column(String(96), nullable=True)
    result_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=_utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    cleanup_run = relationship("VerificationCleanupRun", back_populates="items")
    artifact = relationship("VerificationArtifactProvenance", back_populates="cleanup_items")

    __table_args__ = (
        UniqueConstraint(
            "cleanup_run_id",
            "entity_type",
            "entity_id",
            name="uq_verification_cleanup_item_identity",
        ),
        CheckConstraint(
            "status IN ('pending','deleted','already_absent','failed','skipped')",
            name="ck_verification_cleanup_items_status",
        ),
        Index(
            "ix_verification_cleanup_items_run_status",
            "run_id",
            "status",
        ),
    )

    error_code = synonym("safe_error_code")
    result = synonym("result_json")
    cleanup_id = synonym("cleanup_run_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id) if self.id else None,
            "cleanup_run_id": str(self.cleanup_run_id) if self.cleanup_run_id else None,
            "artifact_id": str(self.artifact_id) if self.artifact_id else None,
            "run_id": str(self.run_id) if self.run_id else None,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "status": self.status,
            "safe_error_code": self.safe_error_code,
            "error_code": self.safe_error_code,
            "result": self.result_json or {},
            "result_json": self.result_json or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = [
    "VERIFICATION_ARTIFACT_CLEANUP_STATUSES",
    "VERIFICATION_CLEANUP_ITEM_STATUSES",
    "VERIFICATION_CLEANUP_STATUSES",
    "VERIFICATION_PROVENANCE_SCHEMA_VERSION",
    "VERIFICATION_RUN_STATUSES",
    "VerificationArtifactProvenance",
    "VerificationCleanupItem",
    "VerificationCleanupRun",
    "VerificationRun",
]
