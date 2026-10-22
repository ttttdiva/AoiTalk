"""Unallocated content-window cost evidence and single-event full allocations."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


class MediaCostEntry(Base):
    __tablename__ = "media_cost_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entry_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    revision = Column(Integer, nullable=False)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    persona_ref = Column(String(164), nullable=False, index=True)
    content_ref = Column(String(164), nullable=True)
    window_start = Column(DateTime, nullable=False)
    window_end = Column(DateTime, nullable=False)
    currency = Column(String(3), nullable=False)
    source = Column(String(16), nullable=False)
    basis = Column(String(16), nullable=False)
    fee_amount = Column(Float, nullable=True)
    production_cost_amount = Column(Float, nullable=True)
    human_cost_amount = Column(Float, nullable=True)
    human_minutes = Column(Float, nullable=True)
    missing_reasons = Column(JSON, nullable=False)
    coverage_complete = Column(Boolean, nullable=False)
    coverage_note = Column(Text, nullable=False)
    evidence = Column(JSON, nullable=False)
    correction_of_id = Column(UUID(as_uuid=True), ForeignKey("media_cost_entries.id", ondelete="RESTRICT"), nullable=True)
    entry_hash = Column(String(64), nullable=False)
    idempotency_key = Column(String(255), nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        UniqueConstraint("entry_id", "revision", name="uq_media_cost_entry_revision"),
        UniqueConstraint("correction_of_id", name="uq_media_cost_entry_successor"),
        CheckConstraint("revision >= 1", name="ck_media_cost_entry_revision"),
        CheckConstraint("source IN ('manual', 'imported')", name="ck_media_cost_entry_source"),
        CheckConstraint("basis IN ('estimated', 'settled')", name="ck_media_cost_entry_basis"),
        CheckConstraint("window_start <= window_end", name="ck_media_cost_entry_window"),
        CheckConstraint("length(currency) = 3 AND length(entry_hash) = 64", name="ck_media_cost_entry_hash_currency"),
        CheckConstraint("human_minutes IS NULL OR human_minutes >= 0", name="ck_media_cost_entry_minutes"),
        CheckConstraint("(revision = 1 AND correction_of_id IS NULL AND entry_id = id) OR (revision > 1 AND correction_of_id IS NOT NULL)", name="ck_media_cost_entry_predecessor"),
        Index("uq_media_cost_entry_personal_key", "owner_user_id", "idempotency_key", unique=True, postgresql_where=text("project_id IS NULL"), sqlite_where=text("project_id IS NULL")),
        Index("uq_media_cost_entry_project_key", "project_id", "idempotency_key", unique=True, postgresql_where=text("project_id IS NOT NULL"), sqlite_where=text("project_id IS NOT NULL")),
    )

    def to_safe_dict(self):
        result = {column.name: getattr(self, column.name) for column in self.__table__.columns}
        for key, value in result.items():
            if isinstance(value, uuid.UUID):
                result[key] = str(value)
            elif isinstance(value, datetime):
                result[key] = value.isoformat()
        return result


class MediaCostAllocation(Base):
    """Permanent full allocation of one immutable cost revision to one event."""

    __tablename__ = "media_cost_allocations"
    cost_entry_id = Column(UUID(as_uuid=True), ForeignKey("media_cost_entries.id", ondelete="RESTRICT"), primary_key=True)
    cost_entry_revision_id = Column(UUID(as_uuid=True), ForeignKey("media_cost_entries.id", ondelete="RESTRICT"), nullable=False, unique=True)
    revenue_event_id = Column(UUID(as_uuid=True), ForeignKey("media_revenue_events.id", ondelete="RESTRICT"), nullable=False, unique=True)
    cost_record_id = Column(UUID(as_uuid=True), ForeignKey("media_revenue_cost_records.id", ondelete="RESTRICT"), nullable=False, unique=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
