"""Generic integration credential persistence.

This module owns only the database shape.  Encryption/decryption and provider
verification remain service responsibilities.  The public projection is
deliberately write-only with respect to credential material.
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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import synonym

from .base import Base


INTEGRATION_CREDENTIAL_STATUSES = (
    "verification_pending",
    "verified",
    "invalid",
    "unsupported",
    "disabled",
    "key_unavailable",
)
INTEGRATION_CREDENTIAL_AUDIT_EVENTS = ("add", "rotate", "verify", "disable", "rekey")


def _uuid(value: Any) -> str | None:
    return str(value) if value is not None else None


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


_SECRET_MARKERS = (
    "secret",
    "token",
    "password",
    "credential",
    "api_key",
    "apikey",
    "cookie",
    "authorization",
    "bearer",
    "payload",
    "private_key",
)


def _safe_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return None
    if isinstance(value, dict):
        if len(value) > 64:
            return {}
        return {
            str(key): _safe_json(item, depth=depth + 1)
            for key, item in value.items()
            if not any(marker in str(key).casefold() for marker in _SECRET_MARKERS)
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_json(item, depth=depth + 1) for item in list(value)[:128]]
    if isinstance(value, str):
        lowered = value.casefold()
        if any(marker in lowered for marker in ("bearer ", "token=", "secret=", "password=")):
            return "[REDACTED]"
        return value[:4096]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


class IntegrationCredential(Base):
    """One encrypted, non-Media credential bound to an ExternalConnection."""

    __tablename__ = "integration_credentials"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id = Column(
        UUID(as_uuid=True),
        ForeignKey("external_connections.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    owner_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id = Column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    credential_kind = Column(String(64), nullable=False)
    # This is the only secret-bearing column.  The crypto worker owns its
    # format; the persistence layer stores ciphertext as opaque text.
    encrypted_payload = Column(Text, nullable=False)
    payload_digest = Column(String(64), nullable=False)
    encryption_key_id = Column(String(128), nullable=False)
    revision = Column(Integer, nullable=False, default=1, server_default="1")
    status = Column(
        String(32),
        nullable=False,
        default="verification_pending",
        server_default="verification_pending",
        index=True,
    )
    state_hash = Column(String(64), nullable=False)
    verification_code = Column(String(128), nullable=True)
    capabilities_json = Column(
        "capabilities", JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    verified_at = Column(DateTime, nullable=True)
    created_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"), index=True
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        index=True,
    )

    capabilities = synonym("capabilities_json")

    __table_args__ = (
        CheckConstraint("length(trim(credential_kind)) BETWEEN 1 AND 64", name="ck_integration_credentials_kind"),
        CheckConstraint("revision > 0", name="ck_integration_credentials_revision"),
        CheckConstraint(
            "status IN ('verification_pending','verified','invalid','unsupported','disabled','key_unavailable')",
            name="ck_integration_credentials_status",
        ),
        CheckConstraint("length(trim(encrypted_payload)) > 0", name="ck_integration_credentials_payload"),
        CheckConstraint("length(payload_digest) = 64", name="ck_integration_credentials_payload_digest"),
        CheckConstraint("length(state_hash) = 64", name="ck_integration_credentials_state_hash"),
        Index("ix_integration_credentials_owner_project", "owner_user_id", "project_id"),
        Index("ix_integration_credentials_status_updated", "status", "updated_at"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        """Return readiness metadata without ciphertext, hashes, or key IDs."""

        return {
            "id": _uuid(self.id),
            "connection_id": _uuid(self.connection_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "credential_kind": self.credential_kind,
            "revision": int(self.revision or 1),
            "status": self.status,
            "state_hash": self.state_hash,
            "capabilities": _safe_json(self.capabilities_json),
            "verified_at": _dt(self.verified_at),
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict


class IntegrationCredentialAuditEvent(Base):
    """Append-only safe audit event for a generic IntegrationCredential."""

    __tablename__ = "integration_credential_audit_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # No FK is intentional: deleting a credential must not erase its audit
    # history, matching the existing Media credential audit pattern.
    credential_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    connection_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    revision = Column(Integer, nullable=False)
    event_type = Column(String(32), nullable=False, index=True)
    actor_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    actor_type = Column(String(16), nullable=False, default="human", server_default="human")
    service_actor_key = Column(String(120), nullable=True)
    state_hash = Column(String(64), nullable=False)
    provider_code = Column(String(128), nullable=True)
    created_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"), index=True
    )

    actor_kind = synonym("actor_type")
    safe_provider_code = synonym("provider_code")

    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_integration_credential_audit_revision"),
        CheckConstraint(
            "length(event_type) BETWEEN 1 AND 32",
            name="ck_integration_credential_audit_event_type",
        ),
        CheckConstraint(
            "actor_type IN ('human','service','system')",
            name="ck_integration_credential_audit_actor_type",
        ),
        CheckConstraint("length(state_hash) = 64", name="ck_integration_credential_audit_state_hash"),
        CheckConstraint(
            "NOT (actor_id IS NOT NULL AND service_actor_key IS NOT NULL)",
            name="ck_integration_credential_audit_actor_fields_xor",
        ),
        Index("ix_integration_credential_audit_credential_created", "credential_id", "created_at"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "credential_id": _uuid(self.credential_id),
            "connection_id": _uuid(self.connection_id),
            "revision": int(self.revision or 1),
            "event_type": self.event_type,
            "actor_id": _uuid(self.actor_id),
            "actor_type": self.actor_type,
            "service_actor_key": self.service_actor_key,
            "state_hash": self.state_hash,
            "provider_code": self.provider_code,
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


__all__ = [
    "INTEGRATION_CREDENTIAL_STATUSES",
    "INTEGRATION_CREDENTIAL_AUDIT_EVENTS",
    "IntegrationCredential",
    "IntegrationCredentialAuditEvent",
]
