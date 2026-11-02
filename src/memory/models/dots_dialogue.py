"""Durable, owner-scoped records for the optional external DOTS route.

These rows do not represent AoiDots Agents. Text, callback URLs, signing keys,
and one-time delivery credentials are encrypted at rest. Principal identity is
stored only as a stable SHA-256 fingerprint; raw OAuth tokens are never stored.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from .base import Base, _encrypted_text_property


class DotsDialogueBinding(Base):
    __tablename__ = "dots_dialogue_bindings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    principal_fingerprint = Column(String(64), nullable=False, unique=True)
    # OAuth-backed bindings remain conversation-scoped at request time.  The
    # dedicated stdio/Tunnel connection sets this once during owner
    # confirmation so a changed local config cannot silently retarget it.
    dedicated_conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversation_sessions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    intended_chat_label = Column(String(120), nullable=False, default="")
    owner_confirmed_at = Column(DateTime, nullable=False)
    generation = Column(Integer, nullable=False, default=1, server_default="1")
    next_sequence = Column(BigInteger, nullable=False, default=0, server_default="0")
    cursor_position = Column(BigInteger, nullable=False, default=0, server_default="0")
    state = Column(String(16), nullable=False, default="active", server_default="active")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow,
                        server_default=text("CURRENT_TIMESTAMP"))

    __table_args__ = (
        CheckConstraint("generation > 0", name="ck_dots_dialogue_binding_generation"),
        CheckConstraint("state IN ('active', 'awaiting_confirmation', 'revoked')", name="ck_dots_dialogue_binding_state"),
        CheckConstraint("next_sequence >= 0 AND cursor_position >= 0", name="ck_dots_dialogue_binding_cursor"),
        Index("ix_dots_dialogue_binding_owner_state", "owner_user_id", "state"),
    )


class DotsDialogueSubscription(Base):
    __tablename__ = "dots_dialogue_subscriptions"

    id = Column(String(68), primary_key=True)
    binding_id = Column(UUID(as_uuid=True), ForeignKey("dots_dialogue_bindings.id", ondelete="CASCADE"),
                        nullable=False)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    principal_fingerprint = Column(String(64), nullable=False)
    generation = Column(Integer, nullable=False)
    event_name = Column(String(100), nullable=False)
    arguments_hash = Column(String(64), nullable=False)
    callback_hash = Column(String(64), nullable=False)
    _callback_url = Column("callback_url", Text, nullable=False)
    callback_url = _encrypted_text_property("_callback_url", "dots_dialogue_subscriptions.callback_url")
    _signing_secret = Column("signing_secret", Text, nullable=False)
    signing_secret = _encrypted_text_property("_signing_secret", "dots_dialogue_subscriptions.signing_secret")
    _previous_signing_secret = Column("previous_signing_secret", Text, nullable=True)
    previous_signing_secret = _encrypted_text_property(
        "_previous_signing_secret", "dots_dialogue_subscriptions.previous_signing_secret"
    )
    secret_rotation_until = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    state = Column(String(16), nullable=False, default="active", server_default="active")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow,
                        server_default=text("CURRENT_TIMESTAMP"))

    __table_args__ = (
        UniqueConstraint("binding_id", "generation", name="uq_dots_dialogue_subscription_generation"),
        CheckConstraint("generation > 0", name="ck_dots_dialogue_subscription_generation"),
        CheckConstraint("state IN ('active', 'revoked', 'expired')", name="ck_dots_dialogue_subscription_state"),
        Index("ix_dots_dialogue_subscription_principal", "principal_fingerprint", "state", "expires_at"),
    )


class DotsDialogueRequest(Base):
    __tablename__ = "dots_dialogue_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    binding_id = Column(UUID(as_uuid=True), ForeignKey("dots_dialogue_bindings.id", ondelete="CASCADE"), nullable=False)
    subscription_id = Column(String(68), ForeignKey("dots_dialogue_subscriptions.id", ondelete="CASCADE"), nullable=False)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversation_sessions.id", ondelete="CASCADE"), nullable=False)
    generation = Column(Integer, nullable=False)
    sequence = Column(BigInteger, nullable=False)
    idempotency_hash = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)
    _message = Column("message", Text, nullable=False)
    message = _encrypted_text_property("_message", "dots_dialogue_requests.message")
    delivery_token_hash = Column(String(64), nullable=False)
    _delivery_token = Column("delivery_token", Text, nullable=False)
    delivery_token = _encrypted_text_property("_delivery_token", "dots_dialogue_requests.delivery_token")
    state = Column(String(24), nullable=False, default="pending", server_default="pending")
    expires_at = Column(DateTime, nullable=False)
    delivery_accepted_at = Column(DateTime, nullable=True)
    replied_at = Column(DateTime, nullable=True)
    _reply = Column("reply", Text, nullable=True)
    reply = _encrypted_text_property("_reply", "dots_dialogue_requests.reply")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow,
                        server_default=text("CURRENT_TIMESTAMP"))

    __table_args__ = (
        UniqueConstraint("binding_id", "generation", "idempotency_hash", name="uq_dots_dialogue_request_idempotency"),
        UniqueConstraint("binding_id", "generation", "sequence", name="uq_dots_dialogue_request_sequence"),
        CheckConstraint("generation > 0 AND sequence > 0", name="ck_dots_dialogue_request_route"),
        CheckConstraint(
            "state IN ('pending', 'delivery_accepted', 'read', 'replied', 'cancelled', 'expired', 'delivery_failed')",
            name="ck_dots_dialogue_request_state",
        ),
        Index("ix_dots_dialogue_request_owner_state", "owner_user_id", "state", "created_at"),
        Index("ix_dots_dialogue_request_expiry", "expires_at", "state"),
    )


class DotsDialogueOutbox(Base):
    __tablename__ = "dots_dialogue_outbox"

    request_id = Column(UUID(as_uuid=True), ForeignKey("dots_dialogue_requests.id", ondelete="CASCADE"), primary_key=True)
    subscription_id = Column(String(68), ForeignKey("dots_dialogue_subscriptions.id", ondelete="CASCADE"), nullable=False)
    generation = Column(Integer, nullable=False)
    sequence = Column(BigInteger, nullable=False)
    cursor_after = Column(UUID(as_uuid=True), nullable=False, default=uuid.uuid4, unique=True)
    state = Column(String(16), nullable=False, default="pending", server_default="pending")
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))
    lease_id = Column(UUID(as_uuid=True), nullable=True)
    leased_until = Column(DateTime, nullable=True)
    last_http_status = Column(Integer, nullable=True)
    last_error_code = Column(String(48), nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))

    __table_args__ = (
        UniqueConstraint("subscription_id", "generation", "sequence", name="uq_dots_dialogue_outbox_sequence"),
        CheckConstraint("generation > 0 AND sequence > 0 AND attempt_count >= 0", name="ck_dots_dialogue_outbox_attempt"),
        CheckConstraint("state IN ('pending', 'leased', 'acked', 'failed', 'cancelled')",
                        name="ck_dots_dialogue_outbox_state"),
        Index("ix_dots_dialogue_outbox_due", "subscription_id", "generation", "state", "next_attempt_at", "sequence"),
    )


class DotsDialogueOperationReceipt(Base):
    __tablename__ = "dots_dialogue_operation_receipts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    binding_id = Column(UUID(as_uuid=True), ForeignKey("dots_dialogue_bindings.id", ondelete="CASCADE"), nullable=False)
    request_id = Column(UUID(as_uuid=True), ForeignKey("dots_dialogue_requests.id", ondelete="CASCADE"), nullable=False, unique=True)
    generation = Column(Integer, nullable=False)
    operation_hash = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)
    state = Column(String(16), nullable=False, default="accepted", server_default="accepted")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))

    __table_args__ = (
        UniqueConstraint("binding_id", "generation", "operation_hash", name="uq_dots_dialogue_operation_key"),
        CheckConstraint("generation > 0", name="ck_dots_dialogue_operation_generation"),
        CheckConstraint("state = 'accepted'", name="ck_dots_dialogue_operation_state"),
        Index("ix_dots_dialogue_operation_owner", "owner_user_id", "created_at"),
    )
