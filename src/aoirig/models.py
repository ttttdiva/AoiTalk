"""Rig state belongs to a principal; shared Characters are never made private implicitly."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from ..memory.models import Base


def now():
    return datetime.now(timezone.utc)


class CharacterRig(Base):
    __tablename__ = "character_rigs"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    character_id = Column(UUID(as_uuid=True), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False)
    owner_id = Column(String(128), nullable=False)
    active_revision = Column(UUID(as_uuid=True), nullable=True)
    source_path = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), default=now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now, nullable=False)
    __table_args__ = (UniqueConstraint("character_id", "owner_id", name="uq_character_rigs_owner"),)


class CharacterRigBuild(Base):
    """Every build is an immutable revision candidate; active pointer is separate."""

    __tablename__ = "character_rig_builds"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rig_id = Column(UUID(as_uuid=True), ForeignKey("character_rigs.id", ondelete="CASCADE"), nullable=False)
    revision = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False, default="queued")
    format_version = Column(Integer, nullable=False, default=1)
    source_path = Column(String(500), nullable=False)
    asset_path = Column(String(500), nullable=True)
    config = Column(JSON, nullable=False, default=dict)
    report = Column(JSON, nullable=False, default=dict)
    error = Column(String(1000), nullable=True)
    created_at = Column(DateTime(timezone=True), default=now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now, nullable=False)
    __table_args__ = (UniqueConstraint("rig_id", "revision", name="uq_character_rig_revision"), Index("ix_character_rig_build_status", "status"),)


class CharacterRigPlaybackSettings(Base):
    """Mutable conversation overrides, separate from an immutable model candidate.

    Ownership is inherited through build -> rig -> (character, owner). The
    manifest digest prevents applying a saved override to different model bytes.
    """

    __tablename__ = "character_rig_playback_settings"
    build_id = Column(UUID(as_uuid=True), ForeignKey("character_rig_builds.id", ondelete="CASCADE"), primary_key=True)
    manifest_sha256 = Column(String(64), nullable=False)
    settings = Column(JSON, nullable=False, default=dict)
    revision = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now, nullable=False)
