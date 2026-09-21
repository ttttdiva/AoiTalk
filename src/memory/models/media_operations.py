"""Typed Media Operations persistence for Persona Core and nine-slot intake."""

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


class MediaPlatform(str, Enum):
    X = "x"
    PIXIV = "pixiv"
    DLSITE = "dlsite"
    PATREON = "patreon"
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"


MEDIA_PLATFORM_VALUES = tuple(platform.value for platform in MediaPlatform)

_PLATFORM_COLUMNS: tuple[tuple[MediaPlatform, str], ...] = (
    (MediaPlatform.X, "platform_x"),
    (MediaPlatform.PIXIV, "platform_pixiv"),
    (MediaPlatform.DLSITE, "platform_dlsite"),
    (MediaPlatform.PATREON, "platform_patreon"),
    (MediaPlatform.YOUTUBE, "platform_youtube"),
    (MediaPlatform.INSTAGRAM, "platform_instagram"),
)


def _uuid(value: Any) -> str | None:
    return str(value) if value is not None else None


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class Persona(Base):
    """Stable Persona identity.

    Mutable Persona content never lives on this row.  Every content change is a
    new PersonaRevision.
    """

    __tablename__ = "media_personas"

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
    # Lifecycle and parent-brand references are kept on the stable identity;
    # editable policy lives in append-only PersonaRevision rows below.
    state = Column(String(16), nullable=False, default="draft", server_default="draft", index=True)
    parent_brand_ref = Column(String(164), nullable=True)
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
            "length(create_hash) = 64",
            name="ck_media_personas_create_hash_length",
        ),
        CheckConstraint(
            "state IN ('draft', 'active', 'paused', 'archived')",
            name="ck_media_personas_state",
        ),
        Index(
            "ix_media_personas_owner_project",
            "owner_user_id",
            "project_id",
        ),
        Index(
            "uq_media_personas_personal_idempotency",
            "owner_user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_media_personas_project_idempotency",
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
            "state": self.state,
            "parent_brand_ref": self.parent_brand_ref,
            "create_hash": self.create_hash,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class PersonaRevision(Base):
    """Immutable version of all editable Persona definition fields."""

    __tablename__ = "media_persona_revisions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    persona_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_personas.id", ondelete="CASCADE"),
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
    display_name = Column(String(120), nullable=False)
    summary = Column(Text, nullable=True)
    voice = Column(Text, nullable=True)
    audience = Column(Text, nullable=True)
    niche = Column(Text, nullable=True)
    positioning = Column(Text, nullable=True)
    visual_identity_json = Column("visual_identity", JSON, nullable=True)
    creative_direction = Column(Text, nullable=True)
    allowed_subjects_json = Column("allowed_subjects", JSON, nullable=True)
    prohibited_subjects_json = Column("prohibited_subjects", JSON, nullable=True)
    adult_policy = Column(String(32), nullable=True)
    sensitive_policy = Column(String(32), nullable=True)
    ip_policy = Column(String(32), nullable=True)
    disclosure_policy = Column(String(32), nullable=True)
    monetization_policy_json = Column("monetization_policy", JSON, nullable=True)
    kpi_objectives_json = Column("kpi_objectives", JSON, nullable=True)
    default_language = Column(String(16), nullable=True)
    locale = Column(String(64), nullable=True)
    timezone = Column(String(64), nullable=True)
    research_policy_json = Column("research_policy", JSON, nullable=True)
    image_production_policy_json = Column("image_production_policy", JSON, nullable=True)
    video_production_policy_json = Column("video_production_policy", JSON, nullable=True)
    public_aliases_json = Column("public_aliases", JSON, nullable=True)

    # Closed platform set.  Booleans avoid storing arbitrary enum strings in a
    # JSON array and make the persisted schema itself closed.
    platform_x = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    platform_pixiv = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    platform_dlsite = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    platform_patreon = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    platform_youtube = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    platform_instagram = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    content_pillars_json = Column(
        "content_pillars",
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    content_hash = Column(String(64), nullable=False, index=True)
    # Initial revision is covered by Persona's create idempotency key, so this
    # field is null only for version 1 created with the Persona.
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
            name="ck_media_persona_revisions_version_positive",
        ),
        CheckConstraint(
            "length(content_hash) = 64",
            name="ck_media_persona_revisions_content_hash_length",
        ),
        UniqueConstraint(
            "persona_id",
            "version",
            name="uq_media_persona_revisions_version",
        ),
        UniqueConstraint(
            "persona_id",
            "idempotency_key",
            name="uq_media_persona_revisions_idempotency",
        ),
        Index(
            "ix_media_persona_revisions_owner_project",
            "owner_user_id",
            "project_id",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        platforms = [
            platform.value
            for platform, attribute in _PLATFORM_COLUMNS
            if bool(getattr(self, attribute, False))
        ]
        raw_pillars = (
            self.content_pillars_json
            if isinstance(self.content_pillars_json, list)
            else []
        )
        return {
            "id": _uuid(self.id),
            "persona_id": _uuid(self.persona_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "version": int(self.version),
            "display_name": self.display_name,
            "summary": self.summary,
            "voice": self.voice,
            "audience": self.audience,
            "niche": self.niche,
            "positioning": self.positioning,
            "visual_identity": self.visual_identity_json or {},
            "creative_direction": self.creative_direction,
            "allowed_subjects": list(self.allowed_subjects_json or []),
            "prohibited_subjects": list(self.prohibited_subjects_json or []),
            "adult_policy": self.adult_policy,
            "sensitive_policy": self.sensitive_policy,
            "ip_policy": self.ip_policy,
            "disclosure_policy": self.disclosure_policy,
            "monetization_policy": self.monetization_policy_json or {},
            "kpi_objectives": list(self.kpi_objectives_json or []),
            "default_language": self.default_language,
            "locale": self.locale,
            "timezone": self.timezone,
            "research_policy": self.research_policy_json or {},
            "image_production_policy": self.image_production_policy_json or {},
            "video_production_policy": self.video_production_policy_json or {},
            "public_aliases": list(self.public_aliases_json or []),
            "platforms": platforms,
            "content_pillars": [
                str(item) for item in raw_pillars if isinstance(item, str)
            ],
            "content_hash": self.content_hash,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class PersonaResource(Base):
    """Typed, immutable provenance reference attached to a Persona."""

    __tablename__ = "media_persona_resources"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    persona_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_personas.id", ondelete="CASCADE"),
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
    resource_kind = Column(String(32), nullable=False)
    platform = Column(String(16), nullable=True)
    label = Column(String(255), nullable=True)

    provenance_type = Column(String(16), nullable=False)
    source_url = Column(Text, nullable=True)
    artifact_sha256 = Column(String(64), nullable=True)
    artifact_mime_type = Column(String(255), nullable=True)

    resource_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=False)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "resource_kind IN ("
            "'profile', 'reference', 'asset', 'persona_bible', "
            "'character_bible', 'world_bible', 'visual_style_reference', "
            "'reference_image', 'posting_rule', 'platform_rule', 'sensitive_rule', "
            "'forbidden_content_rule', 'ip_rights_rule', 'monetization_rule', "
            "'kpi_definition', 'experiment_policy', 'topic_source', 'idea_bank', "
            "'high_performing_content', 'supporting_document')",
            name="ck_media_persona_resources_kind",
        ),
        CheckConstraint(
            "platform IS NULL OR platform IN "
            "('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')",
            name="ck_media_persona_resources_platform",
        ),
        CheckConstraint(
            "provenance_type IN ('url', 'artifact')",
            name="ck_media_persona_resources_provenance_type",
        ),
        CheckConstraint(
            "("
            "provenance_type = 'url' "
            "AND source_url IS NOT NULL "
            "AND artifact_sha256 IS NULL "
            "AND artifact_mime_type IS NULL"
            ") OR ("
            "provenance_type = 'artifact' "
            "AND source_url IS NULL "
            "AND artifact_sha256 IS NOT NULL "
            "AND artifact_mime_type IS NOT NULL"
            ")",
            name="ck_media_persona_resources_provenance_shape",
        ),
        CheckConstraint(
            "artifact_sha256 IS NULL OR length(artifact_sha256) = 64",
            name="ck_media_persona_resources_artifact_hash_length",
        ),
        CheckConstraint(
            "length(resource_hash) = 64",
            name="ck_media_persona_resources_resource_hash_length",
        ),
        UniqueConstraint(
            "persona_id",
            "idempotency_key",
            name="uq_media_persona_resources_idempotency",
        ),
        Index(
            "ix_media_persona_resources_owner_project",
            "owner_user_id",
            "project_id",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> Dict[str, Any]:
        if self.provenance_type == "url":
            provenance: dict[str, Any] = {
                "type": "url",
                "url": self.source_url,
            }
        else:
            provenance = {
                "type": "artifact",
                "sha256": self.artifact_sha256,
                "mime_type": self.artifact_mime_type,
            }
        return {
            "id": _uuid(self.id),
            "persona_id": _uuid(self.persona_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "resource_kind": self.resource_kind,
            "platform": self.platform,
            "label": self.label,
            "provenance": provenance,
            "resource_hash": self.resource_hash,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class PersonaIntakeSlot(Base):
    """One occupied slot in the fixed 1..9 Persona intake."""

    __tablename__ = "media_persona_intake_slots"

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
    slot = Column(Integer, nullable=False)
    persona_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_personas.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "slot >= 1 AND slot <= 9",
            name="ck_media_persona_intake_slots_range",
        ),
        UniqueConstraint(
            "persona_id",
            name="uq_media_persona_intake_slots_persona",
        ),
        Index(
            "ix_media_persona_intake_slots_owner_project",
            "owner_user_id",
            "project_id",
        ),
        Index(
            "uq_media_persona_intake_slots_personal_slot",
            "owner_user_id",
            "slot",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_media_persona_intake_slots_project_slot",
            "project_id",
            "slot",
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
            "slot": int(self.slot),
            "persona_id": _uuid(self.persona_id),
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict
