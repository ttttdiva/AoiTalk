"""Durable Daily capture organization ledger; canonical content remains in Docs."""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Column, Date, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from .base import Base, _encrypted_json_property


class DocsDailyCapture(Base):
    __tablename__ = "docs_daily_captures"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    docs_library_id = Column(UUID(as_uuid=True), ForeignKey("docs_libraries.id", ondelete="CASCADE"), nullable=False)
    node_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_nodes.id", ondelete="CASCADE"), nullable=False)
    day_node_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_nodes.id", ondelete="CASCADE"), nullable=False)
    day = Column(Date, nullable=False)
    idempotency_key = Column(String(36), nullable=False)
    request_hash = Column(String(64), nullable=False)
    source_version = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default="queued")
    attempt_count = Column(Integer, nullable=False, default=0)
    lease_token = Column(String(36), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    _history = Column("history_json", JSON, nullable=False, default=dict)
    history = _encrypted_json_property("_history", "docs_daily_captures.history_json")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("actor_user_id", "idempotency_key", name="uq_daily_capture_actor_key"),
        CheckConstraint("status IN ('queued','running','succeeded','failed','undone')", name="ck_daily_capture_status"),
        Index("ix_daily_capture_claim", "status", "lease_expires_at", "created_at"),
        Index("ix_daily_capture_actor_day", "actor_user_id", "docs_library_id", "day"),
    )
