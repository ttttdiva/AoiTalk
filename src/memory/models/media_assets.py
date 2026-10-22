"""Immutable byte bindings for existing MediaOps output identities."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


class MediaAssetRevision(Base):
    __tablename__ = "media_asset_revisions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    generation_output_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_generation_outputs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # Explicit product export binding. Never infer an artifact from a hash/path.
    artifact_version_id = Column(
        UUID(as_uuid=True),
        ForeignKey("artifact_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    artifact_binding_hash = Column(String(64), nullable=True)
    owner_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id = Column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    persona_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_personas.id", ondelete="CASCADE"),
        nullable=False,
    )
    content_item_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_content_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_kind = Column(String(32), nullable=False)
    source_id = Column(UUID(as_uuid=True), nullable=True)
    # Internal logical path only. Never returned by a public DTO or receipt.
    relative_path = Column(Text, nullable=False)
    sha256 = Column(String(64), nullable=False)
    mime_type = Column(String(120), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    revision_hash = Column(String(64), nullable=False)
    command_hash = Column(String(64), nullable=False)
    idempotency_key = Column(String(255), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "(artifact_version_id IS NULL AND artifact_binding_hash IS NULL) OR "
            "(artifact_version_id IS NOT NULL AND artifact_binding_hash IS NOT NULL AND length(artifact_binding_hash) = 64)",
            name="ck_media_asset_product_binding",
        ),
        CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 536870912", name="ck_media_asset_size"
        ),
        CheckConstraint(
            "source_kind IN ('upload','generated_media','library_file','generation_output')",
            name="ck_media_asset_source",
        ),
        CheckConstraint(
            "length(sha256) = 64 AND length(revision_hash) = 64 AND length(command_hash) = 64",
            name="ck_media_asset_hash",
        ),
        Index("ix_media_asset_scope", "owner_user_id", "project_id", "persona_id"),
        # Persona has a fixed owner/project scope, including personal NULL scope.
        Index("uq_media_asset_command", "persona_id", "idempotency_key", unique=True),
    )
