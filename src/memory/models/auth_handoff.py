"""Persistent, single-use browser authentication handoff transactions."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


class AuthHandoffTransaction(Base):
    """Short-lived state shared by the canonical and DDNS browser origins.

    Secret inputs are stored as SHA-256 digests.  ``pkce_verifier`` is an
    intentionally server-held value and is bounded by ``expires_at``; it is
    never serialized into a redirect.
    """

    __tablename__ = "auth_handoff_transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    state_hash = Column(String(64), nullable=False)
    binding_hash = Column(String(64), nullable=False)
    code_hash = Column(String(64), nullable=True)
    pkce_challenge = Column(String(64), nullable=False)
    pkce_verifier = Column(String(128), nullable=False)
    source_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    source_session_version = Column(Integer, nullable=True)
    source_expires_at = Column(DateTime(timezone=True), nullable=True)
    source_origin = Column(String(255), nullable=False)
    destination_origin = Column(String(255), nullable=False)
    callback_path = Column(String(512), nullable=False)
    return_path = Column(String(2048), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)
    authorized_at = Column(DateTime(timezone=True), nullable=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "state_hash", name="uq_auth_handoff_transactions_state_hash"
        ),
        UniqueConstraint(
            "code_hash", name="uq_auth_handoff_transactions_code_hash"
        ),
        CheckConstraint(
            "length(state_hash) = 64",
            name="ck_auth_handoff_transactions_state_hash",
        ),
        CheckConstraint(
            "length(binding_hash) = 64",
            name="ck_auth_handoff_transactions_binding_hash",
        ),
        CheckConstraint(
            "code_hash IS NULL OR length(code_hash) = 64",
            name="ck_auth_handoff_transactions_code_hash",
        ),
        CheckConstraint(
            "length(pkce_challenge) = 43",
            name="ck_auth_handoff_transactions_pkce_challenge",
        ),
        CheckConstraint(
            "source_session_version IS NULL OR source_session_version > 0",
            name="ck_auth_handoff_transactions_source_session_version",
        ),
        CheckConstraint(
            "return_path LIKE '/%' AND return_path NOT LIKE '//%'",
            name="ck_auth_handoff_transactions_return_path",
        ),
        Index("ix_auth_handoff_transactions_expires_at", "expires_at"),
        Index("ix_auth_handoff_transactions_source_user_id", "source_user_id"),
        Index("ix_auth_handoff_transactions_binding_hash", "binding_hash"),
    )


class AuthHandoffClientTransaction(Base):
    """Short-lived browser handoff state for a registered external client.

    Opaque request handles, recipient CSRF state, PKCE codes, and the client
    registration binding are persisted only as SHA-256 digests.  The source
    user reference is nullable until the canonical browser authorizes the
    request, and is cascaded when that user is removed.
    """

    __tablename__ = "auth_handoff_client_transactions"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    request_hash = Column(String(64), nullable=False)
    recipient_state_hash = Column(String(64), nullable=False)
    client_id = Column(String(64), nullable=False)
    registration_hash = Column(String(64), nullable=False)
    source_origin = Column(String(255), nullable=False)
    destination_origin = Column(String(255), nullable=False)
    callback_path = Column(String(512), nullable=False)
    audience = Column(String(255), nullable=False)
    pkce_challenge = Column(String(43), nullable=False)
    code_hash = Column(String(64), nullable=True)
    source_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    source_session_version = Column(Integer, nullable=True)
    source_expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("CURRENT_TIMESTAMP"),
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)
    authorized_at = Column(DateTime(timezone=True), nullable=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "request_hash",
            name="uq_auth_handoff_client_transactions_request_hash",
        ),
        UniqueConstraint(
            "code_hash",
            name="uq_auth_handoff_client_transactions_code_hash",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_auth_handoff_client_transactions_request_hash",
        ),
        CheckConstraint(
            "length(recipient_state_hash) = 64",
            name="ck_auth_handoff_client_transactions_recipient_state_hash",
        ),
        CheckConstraint(
            "length(registration_hash) = 64",
            name="ck_auth_handoff_client_transactions_registration_hash",
        ),
        CheckConstraint(
            "code_hash IS NULL OR length(code_hash) = 64",
            name="ck_auth_handoff_client_transactions_code_hash",
        ),
        CheckConstraint(
            "length(pkce_challenge) = 43",
            name="ck_auth_handoff_client_transactions_pkce_challenge",
        ),
        CheckConstraint(
            "source_session_version IS NULL OR source_session_version > 0",
            name="ck_auth_handoff_client_transactions_source_session_version",
        ),
        Index(
            "ix_auth_handoff_client_transactions_expires_at",
            "expires_at",
        ),
        Index(
            "ix_auth_handoff_client_transactions_source_user_id",
            "source_user_id",
        ),
        Index(
            "ix_auth_handoff_client_transactions_client_id_created_at",
            "client_id",
            "created_at",
        ),
    )


class AuthHandoffRateLimit(Base):
    """Persistent fixed-window counters for browser handoff endpoints."""

    __tablename__ = "auth_handoff_rate_limits"

    bucket = Column(String(128), primary_key=True)
    window_started_at = Column(DateTime(timezone=True), nullable=False)
    attempts = Column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "attempts > 0",
            name="ck_auth_handoff_rate_limits_attempts",
        ),
    )

