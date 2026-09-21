"""Versioned inbound routing and safe, durable call metadata (no raw audio)."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import synonym

from .base import Base
from .agent_automation import _dt, _safe_json, _safe_text, _uuid


TELEPHONY_ROUTE_STATES = ("draft", "active", "paused", "retired")
TELEPHONY_CALL_STATES = ("incoming", "accepting", "active", "transferred", "completed", "rejected", "failed", "uncertain")
TELEPHONY_FALLBACK_MODES = ("reject", "voicemail", "transfer")


class TelephonyRoute(Base):
    __tablename__ = "telephony_routes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    display_name = Column(String(160), nullable=False)
    state = Column(String(16), nullable=False, default="draft", server_default="draft", index=True)
    provider = Column(String(32), nullable=False, default="openai_realtime_sip", server_default="openai_realtime_sip")
    connection_id = Column(UUID(as_uuid=True), ForeignKey("external_connections.id", ondelete="RESTRICT"), nullable=True)
    called_number_hash = Column(String(64), nullable=True, index=True)
    called_number_masked = Column(String(32), nullable=True)
    provider_route_ref = Column(String(255), nullable=True)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False, index=True)
    agent_revision_id = Column(UUID(as_uuid=True), ForeignKey("agent_revisions.id", ondelete="RESTRICT"), nullable=False)
    timezone = Column(String(64), nullable=False, default="UTC", server_default="UTC")
    greeting_override = Column(String(1200), nullable=True)
    business_hours_json = Column("business_hours", JSON, nullable=False, default=dict, server_default=text("'{}'"))
    transfer_policy_json = Column("transfer_policy", JSON, nullable=False, default=dict, server_default=text("'{}'"))
    fallback_mode = Column(String(16), nullable=False, default="reject", server_default="reject")
    fallback_destination_key = Column(String(120), nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    idempotency_key = Column(String(255), nullable=True)
    route_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))

    business_hours = synonym("business_hours_json")
    transfer_policy = synonym("transfer_policy_json")
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        CheckConstraint("state IN ('draft','active','paused','retired')", name="ck_telephony_routes_state"),
        CheckConstraint("fallback_mode IN ('reject','voicemail','transfer')", name="ck_telephony_routes_fallback"),
        CheckConstraint("fallback_mode <> 'transfer' OR (fallback_destination_key IS NOT NULL AND length(trim(fallback_destination_key)) > 0)", name="ck_telephony_routes_fallback_destination"),
        CheckConstraint("version > 0", name="ck_telephony_routes_version"),
        CheckConstraint("length(route_hash) = 64", name="ck_telephony_routes_hash"),
        CheckConstraint("called_number_hash IS NULL OR length(called_number_hash) = 64", name="ck_telephony_routes_number_hash"),
        CheckConstraint("length(trim(display_name)) BETWEEN 1 AND 160", name="ck_telephony_routes_name"),
        UniqueConstraint("agent_id", "idempotency_key", name="uq_telephony_routes_idempotency"),
        Index("uq_telephony_routes_active_number", "provider", "called_number_hash", unique=True, sqlite_where=text("state = 'active' AND called_number_hash IS NOT NULL"), postgresql_where=text("state = 'active' AND called_number_hash IS NOT NULL")),
        Index("uq_telephony_routes_active_ref", "provider", "provider_route_ref", unique=True, sqlite_where=text("state = 'active' AND provider_route_ref IS NOT NULL"), postgresql_where=text("state = 'active' AND provider_route_ref IS NOT NULL")),
    )

    def to_safe_dict(self):
        return {
            "id": _uuid(self.id), "display_name": _safe_text(self.display_name),
            "state": self.state, "provider": self.provider,
            "connection_id": _uuid(self.connection_id),
            "called_number_masked": _masked(self.called_number_masked),
            "agent_id": _uuid(self.agent_id), "agent_revision_id": _uuid(self.agent_revision_id),
            "timezone": self.timezone, "business_hours": _safe_json(self.business_hours_json),
            "greeting_override": _safe_text(self.greeting_override),
            "transfer_policy": _safe_json(self.transfer_policy_json),
            "fallback_mode": self.fallback_mode, "fallback_destination_key": self.fallback_destination_key,
            "created_by": _uuid(self.created_by), "version": self.version or 1,
            "idempotency_key": self.idempotency_key, "route_hash": self.route_hash,
            "created_at": _dt(self.created_at), "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict


def _masked(value):
    """Never trust a mistakenly unmasked stored number in a safe projection."""
    if not value:
        return None
    digits = "".join(char for char in str(value) if char.isdigit())
    return "***" + digits[-4:] if digits else "***"


class TelephonyCall(Base):
    __tablename__ = "telephony_calls"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider = Column(String(32), nullable=False)
    provider_call_id = Column(String(255), nullable=False, unique=True)
    provider_event_id = Column(String(255), nullable=True, unique=True)
    # Historical route/agent pins survive parent lifecycle changes.
    route_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    route_version = Column(Integer, nullable=True)
    route_hash = Column(String(64), nullable=True)
    agent_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    agent_revision_id = Column(UUID(as_uuid=True), nullable=True)
    conversation_session_id = Column(UUID(as_uuid=True), ForeignKey("conversation_sessions.id", ondelete="SET NULL"), nullable=True)
    agent_run_id = Column(UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    state = Column(String(16), nullable=False, default="incoming", server_default="incoming", index=True)
    caller_identity_hash = Column(String(64), nullable=True)
    caller_masked = Column(String(32), nullable=True)
    called_identity_hash = Column(String(64), nullable=True)
    called_masked = Column(String(32), nullable=True)
    transfer_destination_key = Column(String(120), nullable=True)
    safe_error_code = Column(String(128), nullable=True)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    received_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"), index=True)
    accepted_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))

    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        CheckConstraint("state IN ('incoming','accepting','active','transferred','completed','rejected','failed','uncertain')", name="ck_telephony_calls_state"),
        CheckConstraint("version > 0 AND (route_version IS NULL OR route_version > 0)", name="ck_telephony_calls_versions"),
        CheckConstraint("route_hash IS NULL OR length(route_hash) = 64", name="ck_telephony_calls_route_hash"),
        CheckConstraint("caller_identity_hash IS NULL OR length(caller_identity_hash) = 64", name="ck_telephony_calls_caller_hash"),
        CheckConstraint("called_identity_hash IS NULL OR length(called_identity_hash) = 64", name="ck_telephony_calls_called_hash"),
        CheckConstraint("state NOT IN ('accepting','active','transferred','completed') OR (route_id IS NOT NULL AND route_version IS NOT NULL AND route_hash IS NOT NULL AND agent_id IS NOT NULL AND agent_revision_id IS NOT NULL)", name="ck_telephony_calls_route_evidence"),
    )

    def to_safe_dict(self):
        return {
            "id": _uuid(self.id), "provider": self.provider,
            "provider_call_id": self.provider_call_id, "provider_event_id": self.provider_event_id,
            "route_id": _uuid(self.route_id), "route_version": self.route_version, "route_hash": self.route_hash,
            "agent_id": _uuid(self.agent_id), "agent_revision_id": _uuid(self.agent_revision_id),
            "conversation_session_id": _uuid(self.conversation_session_id), "agent_run_id": _uuid(self.agent_run_id),
            "state": self.state, "version": self.version or 1,
            "caller_masked": _masked(self.caller_masked), "called_masked": _masked(self.called_masked),
            "transfer_destination_key": self.transfer_destination_key, "safe_error_code": _safe_text(self.safe_error_code),
            "received_at": _dt(self.received_at), "accepted_at": _dt(self.accepted_at), "ended_at": _dt(self.ended_at),
            "created_at": _dt(self.created_at), "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict
