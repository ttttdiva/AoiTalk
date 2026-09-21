"""Immutable, secret-free provider capability observations.

The code-owned policy lives in
``src.services.media_provider_capability_registry``.  This model stores only a
redacted observation bound to one PlatformAccount/credential revision.  It is
not a credential table and deliberately has no payload, token, cookie, header,
provider response or filesystem path columns.
"""

from __future__ import annotations

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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import synonym

from .base import Base


MEDIA_PROVIDER_CAPABILITY_OPERATIONS = (
    "identity",
    "oauth",
    "token",
    "cookie",
    "text",
    "image",
    "video",
    "schedule",
    "edit",
    "delete",
    "analytics",
    "revenue",
    "refresh",
    "revoke",
)
MEDIA_PROVIDER_CAPABILITY_STATUSES = (
    "automatable",
    "manual",
    "unsupported",
    "unverified",
    "unavailable",
)
MEDIA_PROVIDER_CAPABILITY_ACCOUNT_ELIGIBILITY = (
    "unknown",
    "eligible",
    "ineligible",
    "unverified",
)


def _uuid(value: Any) -> str | None:
    return str(value) if value is not None else None


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class MediaProviderCapabilitySnapshot(Base):
    """Append-only safe snapshot of one provider operation capability."""

    __tablename__ = "media_provider_capability_snapshots"

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
    platform_account_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_platform_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    account_revision_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_platform_account_revisions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    credential_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_platform_credentials.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # This is the credential row's integrity hash, not a secret payload.  It
    # is intentionally omitted from ``to_safe_dict`` and agent/API DTOs.
    credential_state_hash = Column(String(64), nullable=False)
    credential_revision = Column(Integer, nullable=False)
    account_revision = Column(Integer, nullable=False)
    credential_scope = Column(String(255), nullable=False)
    account_type = Column(String(32), nullable=False)
    provider = Column(String(16), nullable=False)
    operation = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False)
    # ``state`` is the wire terminology used by the capability contract while
    # retaining one canonical persisted status column.
    state = synonym("status")
    registry_version = Column(String(32), nullable=False)
    granted_scopes = Column(JSON, nullable=False, default=list, server_default=text("'[]'"))
    account_eligibility = Column(String(16), nullable=False, default="unknown", server_default="unknown")
    adapter_key = Column(String(128), nullable=False)
    adapter_version = Column(String(32), nullable=False)
    observed_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    snapshot_hash = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(255), nullable=False)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "provider IN ('x', 'pixiv', 'patreon', 'youtube', 'instagram', 'dlsite')",
            name="ck_media_provider_capability_snapshots_provider",
        ),
        CheckConstraint(
            "operation IN ('identity', 'oauth', 'token', 'cookie', 'text', 'image', 'video', 'schedule', 'edit', 'delete', 'analytics', 'revenue', 'refresh', 'revoke')",
            name="ck_media_provider_capability_snapshots_operation",
        ),
        CheckConstraint(
            "status IN ('automatable', 'manual', 'unsupported', 'unverified', 'unavailable')",
            name="ck_media_provider_capability_snapshots_status",
        ),
        CheckConstraint(
            "account_eligibility IN ('unknown', 'eligible', 'ineligible', 'unverified')",
            name="ck_media_provider_capability_snapshots_account_eligibility",
        ),
        CheckConstraint("credential_revision > 0", name="ck_media_provider_capability_snapshots_credential_revision"),
        CheckConstraint("account_revision > 0", name="ck_media_provider_capability_snapshots_account_revision"),
        CheckConstraint("length(credential_state_hash) = 64", name="ck_media_provider_capability_snapshots_credential_state_hash"),
        CheckConstraint("length(snapshot_hash) = 64", name="ck_media_provider_capability_snapshots_snapshot_hash"),
        CheckConstraint("length(trim(credential_scope)) > 0", name="ck_media_provider_capability_snapshots_credential_scope"),
        CheckConstraint("length(trim(adapter_key)) > 0", name="ck_media_provider_capability_snapshots_adapter_key"),
        CheckConstraint("length(trim(adapter_version)) > 0", name="ck_media_provider_capability_snapshots_adapter_version"),
        CheckConstraint("length(trim(registry_version)) > 0", name="ck_media_provider_capability_snapshots_registry_version"),
        UniqueConstraint(
            "platform_account_id",
            "operation",
            "credential_revision",
            "account_revision",
            "snapshot_hash",
            name="uq_media_provider_capability_snapshots_observation",
        ),
        Index(
            "ix_media_provider_capability_snapshots_owner_project",
            "owner_user_id",
            "project_id",
        ),
        Index(
            "ix_media_provider_cap_snap_account_op_observed",
            "platform_account_id",
            "operation",
            "observed_at",
        ),
        Index(
            "uq_media_provider_capability_snapshots_personal_idempotency",
            "owner_user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_media_provider_capability_snapshots_project_idempotency",
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
        """Return metadata safe for API, audit and agent projections.

        ``credential_state_hash`` is intentionally excluded even though it is
        only an integrity value; callers must use the server-side binding
        check rather than replaying it from an untrusted DTO.
        """

        scopes = self.granted_scopes if isinstance(self.granted_scopes, list) else []
        safe_scopes = [item for item in scopes if isinstance(item, str) and len(item) <= 256]
        return {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "platform_account_id": _uuid(self.platform_account_id),
            "account_revision_id": _uuid(self.account_revision_id),
            "credential_id": _uuid(self.credential_id),
            "credential_revision": int(self.credential_revision),
            "account_revision": int(self.account_revision),
            "credential_scope": self.credential_scope,
            "account_type": self.account_type,
            "provider": self.provider,
            "operation": self.operation,
            "status": self.status,
            "state": self.status,
            "registry_version": self.registry_version,
            "granted_scopes": safe_scopes,
            "account_eligibility": self.account_eligibility,
            "adapter_key": self.adapter_key,
            "adapter_version": self.adapter_version,
            "observed_at": _dt(self.observed_at),
            "snapshot_hash": self.snapshot_hash,
            "idempotency_key": self.idempotency_key,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


# Compatibility aliases keep the capability contract discoverable for callers
# that use ``Provider`` before ``Media`` naming.
ProviderCapabilitySnapshot = MediaProviderCapabilitySnapshot
MediaCapabilitySnapshot = MediaProviderCapabilitySnapshot


__all__ = [
    "MEDIA_PROVIDER_CAPABILITY_ACCOUNT_ELIGIBILITY",
    "MEDIA_PROVIDER_CAPABILITY_OPERATIONS",
    "MEDIA_PROVIDER_CAPABILITY_STATUSES",
    "MediaCapabilitySnapshot",
    "MediaProviderCapabilitySnapshot",
    "ProviderCapabilitySnapshot",
]
