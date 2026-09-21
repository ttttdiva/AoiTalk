"""Credential Vault persistence for Media Operations.

The vault deliberately keeps only ciphertext in the database.  Public
serializers in this module are safe projections and never expose payload,
encryption metadata, hashes, or credential references.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

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

from .base import Base


class MediaCredentialConnectionType(str, Enum):
    COOKIE_EXPORT = "cookie_export"
    API_TOKEN = "api_token"
    OAUTH = "oauth"


class MediaCredentialStatus(str, Enum):
    VERIFICATION_PENDING = "verification_pending"
    VERIFIED = "verified"
    INVALID = "invalid"
    UNSUPPORTED = "unsupported"
    DISABLED = "disabled"
    KEY_UNAVAILABLE = "key_unavailable"


MEDIA_CREDENTIAL_CONNECTION_TYPES = (
    "cookie_export",
    "api_token",
    "oauth",
)
MEDIA_CREDENTIAL_STATUSES = (
    "verification_pending",
    "verified",
    "invalid",
    "unsupported",
    "disabled",
    "key_unavailable",
)
MEDIA_CREDENTIAL_CAPABILITY_VALUES = ("unknown", "available", "unsupported")
MEDIA_CREDENTIAL_AUDIT_EVENTS = ("add", "rotate", "verify", "disable", "rekey")


def media_credential_state_hash(
    *,
    revision: int,
    status: str,
    connection_type: str,
    payload_digest: str,
    capabilities: Mapping[str, Any] | None,
    verification_code: str | None,
    encryption_key_id: str,
) -> str:
    """Hash every server-observed field that gates credential publication."""

    canonical = json.dumps(
        {
            "revision": int(revision),
            "status": status,
            "connection_type": connection_type,
            "payload_digest": payload_digest,
            "capabilities": dict(capabilities or {}),
            "verification_code": verification_code,
            "encryption_key_id": encryption_key_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _uuid(value: Any) -> str | None:
    return str(value) if value is not None else None


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


_SECRET_KEYS = frozenset(
    {
        "payload",
        "encrypted_payload",
        "token",
        "api_token",
        "api_key",
        "api-key",
        "auth_token",
        "access_token",
        "access_key",
        "refresh_token",
        "refresh_key",
        "client_secret",
        "client-secret",
        "authorization",
        "bearer",
        "bearer_token",
        "session_token",
        "ct0",
        "cookie",
        "cookies",
        "secret",
        "password",
        "credential_ref",
        "encryption_key_id",
        "payload_digest",
    }
)
_NORMALIZED_SECRET_KEYS = frozenset(
    re.sub(r"[^a-z0-9]", "", key.casefold()) for key in _SECRET_KEYS
)
_SECRET_KEY_MARKERS = (
    "authorization",
    "token",
    "secret",
    "password",
    "cookie",
    "credential",
)
_SECRET_KEY_TOKENS = frozenset(
    {
        "authorization",
        "auth",
        "bearer",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "password",
        "passphrase",
        "passwd",
        "secret",
        "token",
    }
)
_SNAPSHOT_KEY_PART_RE = re.compile(
    r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+"
)
_SAFE_SNAPSHOT_WRAPPER_KEYS = frozenset(
    {
        # These keys are response-shape containers, not credential material.
        # Keep them in immutable audit snapshots so composite command
        # responses can be replayed byte-for-byte from their safe projection.
        "result",
        "platform_account",
        "credential",
    }
)
# Metadata fields that happen to contain the word ``credential`` but are not
# secret-bearing values.  They remain available in the replay projection.
_SAFE_SNAPSHOT_METADATA_KEYS = frozenset({"credential_status", "credential_id"})
_NORMALIZED_SAFE_SNAPSHOT_WRAPPER_KEYS = frozenset(
    re.sub(r"[^a-z0-9]", "", key.casefold())
    for key in _SAFE_SNAPSHOT_WRAPPER_KEYS
)
_NORMALIZED_SAFE_SNAPSHOT_METADATA_KEYS = frozenset(
    re.sub(r"[^a-z0-9]", "", key.casefold())
    for key in _SAFE_SNAPSHOT_METADATA_KEYS
)


def _is_secret_snapshot_key(key: Any) -> bool:
    try:
        rendered = str(key)
    except Exception:
        # A hostile mapping key must never make the serializer expose or
        # retain untrusted data.  Treat an unrenderable key as secret-shaped.
        return True
    folded = rendered.casefold()
    normalized = re.sub(r"[^a-z0-9]", "", folded)
    # Exact/normalized matches preserve the original deny-list semantics for
    # names such as ``apiKey`` and ``accessToken``.  Tokenizing on both
    # separators and camel-case boundaries then catches variants such as
    # ``credentialData``, ``passwordHash`` and ``authorizationHeader`` without
    # false positives like ``author`` or ``tokenizer``.
    if folded in _SECRET_KEYS or normalized in _NORMALIZED_SECRET_KEYS:
        return True
    prepared = re.sub(r"[^A-Za-z0-9]+", " ", rendered)
    tokens = {
        token.casefold()
        for chunk in prepared.split()
        for token in _SNAPSHOT_KEY_PART_RE.findall(chunk)
    }
    return bool(tokens & _SECRET_KEY_TOKENS)


def _safe_snapshot(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            try:
                rendered_key = str(key)
            except Exception:
                # Do not let hostile key objects abort an audit write or leak
                # their repr; omitting the field is the fail-closed choice.
                continue
            folded_key = rendered_key.casefold()
            normalized_key = re.sub(r"[^a-z0-9]", "", folded_key)
            # Preserve known safe response wrappers while redacting every
            # secret-shaped leaf.  Dropping ``credential``/``result`` here
            # used to corrupt composite idempotency snapshots on replay.
            if (
                _is_secret_snapshot_key(key)
                and folded_key not in _SAFE_SNAPSHOT_WRAPPER_KEYS
                and folded_key not in _SAFE_SNAPSHOT_METADATA_KEYS
                and normalized_key not in _NORMALIZED_SAFE_SNAPSHOT_WRAPPER_KEYS
                and normalized_key not in _NORMALIZED_SAFE_SNAPSHOT_METADATA_KEYS
            ):
                # Secret leaves are omitted from the immutable snapshot.  The
                # known safe wrappers above are retained so composite command
                # responses still replay with their original structure.
                continue
            result[rendered_key] = _safe_snapshot(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_snapshot(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class MediaPlatformCredential(Base):
    """One encrypted provider credential for a PlatformAccount."""

    __tablename__ = "media_platform_credentials"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    platform_account_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_platform_accounts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    connection_id = Column(
        UUID(as_uuid=True),
        ForeignKey("external_connections.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
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
    connection_type = Column(String(24), nullable=False)
    # This is the sole secret-bearing field.  It must always be ciphertext.
    encrypted_payload = Column(Text, nullable=False)
    encryption_key_id = Column(String(128), nullable=False)
    payload_digest = Column(String(64), nullable=False)
    revision = Column(Integer, nullable=False, default=1, server_default="1")
    state_hash = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, index=True)
    capabilities = Column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    verification_code = Column(String(64), nullable=True)
    verification_started_at = Column(DateTime, nullable=True)
    verification_completed_at = Column(DateTime, nullable=True)
    last_verified_at = Column(DateTime, nullable=True)
    disabled_at = Column(DateTime, nullable=True)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "connection_type IN ('cookie_export','api_token','oauth')",
            name="ck_media_platform_credentials_connection_type",
        ),
        CheckConstraint(
            "status IN ('verification_pending','verified','invalid','unsupported','disabled','key_unavailable')",
            name="ck_media_platform_credentials_status",
        ),
        CheckConstraint(
            "encrypted_payload LIKE 'enc:v1:aes256gcm:%'",
            name="ck_media_platform_credentials_encrypted_payload_format",
        ),
        CheckConstraint("revision > 0", name="ck_media_platform_credentials_revision"),
        CheckConstraint("length(payload_digest) = 64", name="ck_media_platform_credentials_payload_digest"),
        CheckConstraint("length(state_hash) = 64", name="ck_media_platform_credentials_state_hash"),
        Index(
            "ix_media_platform_credentials_owner_project",
            "owner_user_id",
            "project_id",
        ),
        Index(
            "ix_media_platform_credentials_status_updated",
            "status",
            "updated_at",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> dict[str, Any]:
        # Do not add credential_ref, payload_digest, encryption_key_id, or
        # ciphertext here.  Callers only receive status/capability metadata.
        caps = self.capabilities if isinstance(self.capabilities, dict) else {}
        result = {
            "id": _uuid(self.id),
            "platform_account_id": _uuid(self.platform_account_id),
            "connection_id": _uuid(self.connection_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "connection_type": self.connection_type,
            "revision": int(self.revision),
            "status": self.status,
            "verification_code": self.verification_code,
            "capabilities": {
                key: str(caps.get(key, "unknown"))
                for key in ("identity", "publish", "media", "analytics")
            },
            "verification_started_at": _dt(self.verification_started_at),
            "verification_completed_at": _dt(self.verification_completed_at),
            "last_verified_at": _dt(self.last_verified_at),
            "disabled_at": _dt(self.disabled_at),
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }
        return result

    to_dict = to_safe_dict


class MediaPlatformCredentialAuditEvent(Base):
    """Append-only safe audit event for a MediaPlatformCredential."""

    __tablename__ = "media_platform_credential_audit_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Audit history is intentionally independent of live rows so account or
    # credential deletion cannot erase the immutable record.
    credential_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    platform_account_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    owner_user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    project_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    event_type = Column(String(16), nullable=False)
    actor_id = Column(
        UUID(as_uuid=True),
        nullable=True,
    )
    actor_type = Column(String(16), nullable=False, default="human", server_default="human")
    # Snapshot is intentionally safe metadata only; no digest/key/ref/secret.
    snapshot_json = Column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    request_hash = Column(String(64), nullable=False)
    sequence = Column(Integer, nullable=False, default=1, server_default="1")
    idempotency_scope = Column(String(255), nullable=False)
    idempotency_key = Column(String(255), nullable=False)
    prev_event_hash = Column(String(64), nullable=True)
    event_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('add','rotate','verify','disable','rekey')",
            name="ck_media_platform_credential_audit_event_type",
        ),
        CheckConstraint(
            "actor_type IN ('human','admin','unknown')",
            name="ck_media_platform_credential_audit_actor_type",
        ),
        CheckConstraint("length(request_hash) = 64", name="ck_media_platform_credential_audit_request_hash"),
        CheckConstraint("length(event_hash) = 64", name="ck_media_platform_credential_audit_event_hash"),
        CheckConstraint("sequence > 0", name="ck_media_platform_credential_audit_sequence"),
        CheckConstraint(
            "prev_event_hash IS NULL OR length(prev_event_hash) = 64",
            name="ck_media_platform_credential_audit_prev_hash",
        ),
        UniqueConstraint(
            "idempotency_scope",
            "idempotency_key",
            name="uq_media_platform_credential_audit_scope_key",
        ),
        UniqueConstraint(
            "credential_id",
            "sequence",
            name="uq_media_platform_credential_audit_sequence",
        ),
        Index(
            "ix_media_platform_credential_audit_account_created",
            "platform_account_id",
            "created_at",
        ),
        Index(
            "ix_media_platform_credential_audit_owner_project_created",
            "owner_user_id",
            "project_id",
            "created_at",
        ),
    )

    @property
    def owner_id(self):
        return self.owner_user_id

    def to_safe_dict(self) -> dict[str, Any]:
        snapshot = self.snapshot_json if isinstance(self.snapshot_json, dict) else {}
        result = {
            "id": _uuid(self.id),
            "credential_id": _uuid(self.credential_id),
            "platform_account_id": _uuid(self.platform_account_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "event_type": self.event_type,
            "sequence": int(self.sequence),
            "actor_id": _uuid(self.actor_id),
            "actor_type": self.actor_type,
            "snapshot": _safe_snapshot(snapshot),
            "created_at": _dt(self.created_at),
        }
        # Additive safe convenience fields for the audit timeline.  They are
        # copied only from the already-redacted snapshot and never contain
        # provider payloads or hashes.
        if "status" in snapshot:
            result["status"] = snapshot.get("status")
        if "revision" in snapshot:
            result["revision"] = snapshot.get("revision")
        if "provider_code" in snapshot:
            result["verification_code"] = snapshot.get("provider_code")
        return result

    to_dict = to_safe_dict


__all__ = [
    "MEDIA_CREDENTIAL_AUDIT_EVENTS",
    "MEDIA_CREDENTIAL_CAPABILITY_VALUES",
    "MEDIA_CREDENTIAL_CONNECTION_TYPES",
    "MEDIA_CREDENTIAL_STATUSES",
    "MediaCredentialConnectionType",
    "MediaCredentialStatus",
    "MediaPlatformCredential",
    "MediaPlatformCredentialAuditEvent",
    "media_credential_state_hash",
]
