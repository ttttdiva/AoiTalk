"""MediaOps WS2 persistence: bulk Persona drafts and Platform Accounts."""

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


class DraftFactState(str, Enum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


DRAFT_FACT_STATE_VALUES = tuple(value.value for value in DraftFactState)


class PlatformCapabilityStatus(str, Enum):
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    UNSUPPORTED = "unsupported"


PLATFORM_CAPABILITY_STATUS_VALUES = tuple(
    value.value for value in PlatformCapabilityStatus
)


class PlatformCredentialStatus(str, Enum):
    UNKNOWN = "unknown"
    NOT_CONFIGURED = "not_configured"
    CONFIGURED = "configured"
    INVALID = "invalid"


PLATFORM_CREDENTIAL_STATUS_VALUES = tuple(
    value.value for value in PlatformCredentialStatus
)


_PLATFORM_COLUMNS: tuple[tuple[str, str], ...] = (
    ("x", "platform_x"),
    ("pixiv", "platform_pixiv"),
    ("dlsite", "platform_dlsite"),
    ("patreon", "platform_patreon"),
    ("youtube", "platform_youtube"),
    ("instagram", "platform_instagram"),
)


def _uuid(value: Any) -> str | None:
    return str(value) if value is not None else None


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _fact(
    *,
    state: str,
    value: Any,
    evidence: str | None,
) -> dict[str, Any]:
    return {
        "state": state,
        "value": value,
        "evidence": evidence,
    }


class PersonaBulkDraft(Base):
    """Mutable review envelope for one fixed nine-slot Persona import."""

    __tablename__ = "media_persona_bulk_drafts"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
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
    source_hash = Column(
        String(64),
        nullable=False,
        index=True,
    )
    draft_hash = Column(
        String(64),
        nullable=False,
        index=True,
    )
    version = Column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    status = Column(
        String(16),
        nullable=False,
        default="draft",
        server_default="draft",
        index=True,
    )
    idempotency_key = Column(
        String(255),
        nullable=False,
    )

    apply_idempotency_key = Column(
        String(255),
        nullable=True,
    )
    apply_hash = Column(
        String(64),
        nullable=True,
    )
    applied_at = Column(
        DateTime,
        nullable=True,
    )

    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        index=True,
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        CheckConstraint(
            "length(source_hash) = 64",
            name="ck_media_persona_bulk_drafts_source_hash",
        ),
        CheckConstraint(
            "length(draft_hash) = 64",
            name="ck_media_persona_bulk_drafts_draft_hash",
        ),
        CheckConstraint(
            "version > 0",
            name="ck_media_persona_bulk_drafts_version_positive",
        ),
        CheckConstraint(
            "status IN ('draft', 'applied')",
            name="ck_media_persona_bulk_drafts_status",
        ),
        CheckConstraint(
            "("
            "status = 'draft' "
            "AND apply_idempotency_key IS NULL "
            "AND apply_hash IS NULL "
            "AND applied_at IS NULL"
            ") OR ("
            "status = 'applied' "
            "AND apply_idempotency_key IS NOT NULL "
            "AND apply_hash IS NOT NULL "
            "AND applied_at IS NOT NULL"
            ")",
            name="ck_media_persona_bulk_drafts_apply_shape",
        ),
        CheckConstraint(
            "apply_hash IS NULL OR length(apply_hash) = 64",
            name="ck_media_persona_bulk_drafts_apply_hash",
        ),
        Index(
            "ix_media_persona_bulk_drafts_owner_project",
            "owner_user_id",
            "project_id",
        ),
        Index(
            "uq_media_persona_bulk_drafts_personal_idempotency",
            "owner_user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_media_persona_bulk_drafts_project_idempotency",
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
            "source_hash": self.source_hash,
            "draft_hash": self.draft_hash,
            "version": int(self.version or 1),
            "status": self.status,
            "applied_at": _dt(self.applied_at),
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict


class PersonaBulkDraftSlot(Base):
    """One typed slot inside a nine-Persona review draft."""

    __tablename__ = "media_persona_bulk_draft_slots"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    draft_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "media_persona_bulk_drafts.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    slot = Column(
        Integer,
        nullable=False,
    )

    display_name_state = Column(
        String(16),
        nullable=False,
    )
    display_name_value = Column(
        String(120),
        nullable=True,
    )
    display_name_evidence = Column(
        Text,
        nullable=True,
    )

    summary_state = Column(
        String(16),
        nullable=False,
    )
    summary_value = Column(
        Text,
        nullable=True,
    )
    summary_evidence = Column(
        Text,
        nullable=True,
    )

    voice_state = Column(
        String(16),
        nullable=False,
    )
    voice_value = Column(
        Text,
        nullable=True,
    )
    voice_evidence = Column(
        Text,
        nullable=True,
    )

    audience_state = Column(
        String(16),
        nullable=False,
    )
    audience_value = Column(
        Text,
        nullable=True,
    )
    audience_evidence = Column(
        Text,
        nullable=True,
    )

    platforms_state = Column(
        String(16),
        nullable=False,
    )
    platform_x = Column(
        Boolean,
        nullable=True,
    )
    platform_pixiv = Column(
        Boolean,
        nullable=True,
    )
    platform_dlsite = Column(
        Boolean,
        nullable=True,
    )
    platform_patreon = Column(
        Boolean,
        nullable=True,
    )
    platform_youtube = Column(
        Boolean,
        nullable=True,
    )
    platform_instagram = Column(
        Boolean,
        nullable=True,
    )
    platforms_evidence = Column(
        Text,
        nullable=True,
    )

    content_pillars_state = Column(
        String(16),
        nullable=False,
    )
    content_pillars_json = Column(
        "content_pillars",
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    content_pillars_evidence = Column(
        Text,
        nullable=True,
    )

    slot_hash = Column(
        String(64),
        nullable=False,
        index=True,
    )

    __table_args__ = (
        CheckConstraint(
            "slot >= 1 AND slot <= 9",
            name="ck_media_persona_bulk_draft_slots_range",
        ),
        UniqueConstraint(
            "draft_id",
            "slot",
            name="uq_media_persona_bulk_draft_slots_slot",
        ),
        CheckConstraint(
            "length(slot_hash) = 64",
            name="ck_media_persona_bulk_draft_slots_hash",
        ),
        CheckConstraint(
            "("
            "display_name_state = 'unknown' "
            "AND display_name_value IS NULL "
            "AND display_name_evidence IS NULL"
            ") OR ("
            "display_name_state = 'explicit' "
            "AND display_name_value IS NOT NULL"
            ") OR ("
            "display_name_state = 'inferred' "
            "AND display_name_value IS NOT NULL "
            "AND display_name_evidence IS NOT NULL"
            ")",
            name="ck_media_persona_bulk_draft_display_name_fact",
        ),
        CheckConstraint(
            "("
            "summary_state = 'unknown' "
            "AND summary_value IS NULL "
            "AND summary_evidence IS NULL"
            ") OR ("
            "summary_state = 'explicit' "
            "AND summary_value IS NOT NULL"
            ") OR ("
            "summary_state = 'inferred' "
            "AND summary_value IS NOT NULL "
            "AND summary_evidence IS NOT NULL"
            ")",
            name="ck_media_persona_bulk_draft_summary_fact",
        ),
        CheckConstraint(
            "("
            "voice_state = 'unknown' "
            "AND voice_value IS NULL "
            "AND voice_evidence IS NULL"
            ") OR ("
            "voice_state = 'explicit' "
            "AND voice_value IS NOT NULL"
            ") OR ("
            "voice_state = 'inferred' "
            "AND voice_value IS NOT NULL "
            "AND voice_evidence IS NOT NULL"
            ")",
            name="ck_media_persona_bulk_draft_voice_fact",
        ),
        CheckConstraint(
            "("
            "audience_state = 'unknown' "
            "AND audience_value IS NULL "
            "AND audience_evidence IS NULL"
            ") OR ("
            "audience_state = 'explicit' "
            "AND audience_value IS NOT NULL"
            ") OR ("
            "audience_state = 'inferred' "
            "AND audience_value IS NOT NULL "
            "AND audience_evidence IS NOT NULL"
            ")",
            name="ck_media_persona_bulk_draft_audience_fact",
        ),
        CheckConstraint(
            "("
            "platforms_state = 'unknown' "
            "AND platform_x IS NULL "
            "AND platform_pixiv IS NULL "
            "AND platform_dlsite IS NULL "
            "AND platform_patreon IS NULL "
            "AND platform_youtube IS NULL "
            "AND platform_instagram IS NULL "
            "AND platforms_evidence IS NULL"
            ") OR ("
            "platforms_state = 'explicit' "
            "AND platform_x IS NOT NULL "
            "AND platform_pixiv IS NOT NULL "
            "AND platform_dlsite IS NOT NULL "
            "AND platform_patreon IS NOT NULL "
            "AND platform_youtube IS NOT NULL "
            "AND platform_instagram IS NOT NULL"
            ") OR ("
            "platforms_state = 'inferred' "
            "AND platform_x IS NOT NULL "
            "AND platform_pixiv IS NOT NULL "
            "AND platform_dlsite IS NOT NULL "
            "AND platform_patreon IS NOT NULL "
            "AND platform_youtube IS NOT NULL "
            "AND platform_instagram IS NOT NULL "
            "AND platforms_evidence IS NOT NULL"
            ")",
            name="ck_media_persona_bulk_draft_platforms_fact",
        ),
        CheckConstraint(
            "content_pillars_state IN "
            "('explicit', 'inferred', 'unknown')",
            name="ck_media_persona_bulk_draft_pillars_state",
        ),
        CheckConstraint(
            "content_pillars_state != 'inferred' "
            "OR content_pillars_evidence IS NOT NULL",
            name="ck_media_persona_bulk_draft_pillars_inferred_evidence",
        ),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        if self.platforms_state == "unknown":
            platforms: list[str] | None = None
        else:
            platforms = [
                platform
                for platform, attribute in _PLATFORM_COLUMNS
                if bool(getattr(self, attribute))
            ]

        if self.content_pillars_state == "unknown":
            content_pillars: list[str] | None = None
        else:
            raw_pillars = (
                self.content_pillars_json
                if isinstance(
                    self.content_pillars_json,
                    list,
                )
                else []
            )
            content_pillars = [
                str(item)
                for item in raw_pillars
                if isinstance(item, str)
            ]

        return {
            "slot": int(self.slot),
            "display_name": _fact(
                state=self.display_name_state,
                value=self.display_name_value,
                evidence=self.display_name_evidence,
            ),
            "summary": _fact(
                state=self.summary_state,
                value=self.summary_value,
                evidence=self.summary_evidence,
            ),
            "voice": _fact(
                state=self.voice_state,
                value=self.voice_value,
                evidence=self.voice_evidence,
            ),
            "audience": _fact(
                state=self.audience_state,
                value=self.audience_value,
                evidence=self.audience_evidence,
            ),
            "platforms": _fact(
                state=self.platforms_state,
                value=platforms,
                evidence=self.platforms_evidence,
            ),
            "content_pillars": _fact(
                state=self.content_pillars_state,
                value=content_pillars,
                evidence=self.content_pillars_evidence,
            ),
        }

    to_dict = to_safe_dict


class PlatformAccount(Base):
    """Stable external platform account identity without credentials."""

    __tablename__ = "media_platform_accounts"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
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
    persona_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_personas.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    connection_id = Column(
        UUID(as_uuid=True),
        ForeignKey("external_connections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    account_type = Column(String(32), nullable=False, default="profile", server_default="profile")
    remote_url = Column(Text, nullable=True)
    status = Column(String(16), nullable=False, default="active", server_default="active", index=True)
    platform = Column(
        String(16),
        nullable=False,
        index=True,
    )
    account_ref = Column(
        String(255),
        nullable=False,
    )
    create_hash = Column(
        String(64),
        nullable=False,
        index=True,
    )
    idempotency_key = Column(
        String(255),
        nullable=False,
    )
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        index=True,
    )

    __table_args__ = (
        CheckConstraint(
            "platform IN "
            "('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')",
            name="ck_media_platform_accounts_platform",
        ),
        CheckConstraint(
            "length(create_hash) = 64",
            name="ck_media_platform_accounts_create_hash",
        ),
        CheckConstraint(
            "status IN ('active', 'paused')",
            name="ck_media_platform_accounts_status",
        ),
        Index(
            "ix_media_platform_accounts_owner_project",
            "owner_user_id",
            "project_id",
        ),
        Index(
            "uq_media_platform_accounts_personal_identity",
            "owner_user_id",
            "platform",
            "account_ref",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_media_platform_accounts_project_identity",
            "project_id",
            "platform",
            "account_ref",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
            sqlite_where=text("project_id IS NOT NULL"),
        ),
        Index(
            "uq_media_platform_accounts_personal_idempotency",
            "owner_user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_media_platform_accounts_project_idempotency",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
            sqlite_where=text("project_id IS NOT NULL"),
        ),
        Index(
            "uq_media_platform_accounts_connection_id",
            "connection_id",
            unique=True,
            postgresql_where=text("connection_id IS NOT NULL"),
            sqlite_where=text("connection_id IS NOT NULL"),
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
            "persona_id": _uuid(self.persona_id),
            "connection_id": _uuid(self.connection_id),
            "account_type": self.account_type,
            "remote_url": self.remote_url,
            "status": self.status,
            "platform": self.platform,
            "account_ref": self.account_ref,
            "create_hash": self.create_hash,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class PlatformAccountRevision(Base):
    """Immutable capability/credential-status revision."""

    __tablename__ = "media_platform_account_revisions"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    platform_account_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "media_platform_accounts.id",
            ondelete="CASCADE",
        ),
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
    version = Column(
        Integer,
        nullable=False,
    )
    display_name = Column(
        String(255),
        nullable=False,
    )
    publish_capability = Column(
        String(16),
        nullable=False,
    )
    media_capability = Column(
        String(16),
        nullable=False,
    )
    analytics_capability = Column(
        String(16),
        nullable=False,
    )
    credential_status = Column(
        String(24),
        nullable=False,
    )
    remote_url = Column(Text, nullable=True)
    locale = Column(String(64), nullable=True)
    timezone = Column(String(64), nullable=True)
    supported_content_modes_json = Column("supported_content_modes", JSON, nullable=True)
    disclosure_defaults_json = Column("disclosure_defaults", JSON, nullable=True)
    rating_defaults_json = Column("rating_defaults", JSON, nullable=True)
    adapter_ref = Column(String(164), nullable=True)
    content_hash = Column(
        String(64),
        nullable=False,
        index=True,
    )
    idempotency_key = Column(
        String(255),
        nullable=True,
    )
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        index=True,
    )

    __table_args__ = (
        CheckConstraint(
            "version > 0",
            name="ck_media_platform_account_revisions_version",
        ),
        CheckConstraint(
            "publish_capability IN "
            "('unknown', 'available', 'unsupported')",
            name="ck_media_platform_account_revisions_publish",
        ),
        CheckConstraint(
            "media_capability IN "
            "('unknown', 'available', 'unsupported')",
            name="ck_media_platform_account_revisions_media",
        ),
        CheckConstraint(
            "analytics_capability IN "
            "('unknown', 'available', 'unsupported')",
            name="ck_media_platform_account_revisions_analytics",
        ),
        CheckConstraint(
            "credential_status IN "
            "('unknown', 'not_configured', 'configured', 'invalid')",
            name="ck_media_platform_account_revisions_credentials",
        ),
        CheckConstraint(
            "timezone IS NULL OR length(timezone) <= 64",
            name="ck_media_platform_account_revisions_timezone",
        ),
        CheckConstraint(
            "length(content_hash) = 64",
            name="ck_media_platform_account_revisions_hash",
        ),
        UniqueConstraint(
            "platform_account_id",
            "version",
            name="uq_media_platform_account_revisions_version",
        ),
        UniqueConstraint(
            "platform_account_id",
            "idempotency_key",
            name="uq_media_platform_account_revisions_idempotency",
        ),
        Index(
            "ix_media_platform_account_revisions_owner_project",
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
            "platform_account_id": _uuid(
                self.platform_account_id
            ),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "version": int(self.version),
            "display_name": self.display_name,
            "publish_capability": self.publish_capability,
            "media_capability": self.media_capability,
            "analytics_capability": self.analytics_capability,
            "credential_status": self.credential_status,
            "remote_url": self.remote_url,
            "locale": self.locale,
            "timezone": self.timezone,
            "supported_content_modes": list(self.supported_content_modes_json or []),
            "disclosure_defaults": self.disclosure_defaults_json or {},
            "rating_defaults": self.rating_defaults_json or {},
            "adapter_ref": self.adapter_ref,
            "content_hash": self.content_hash,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict
