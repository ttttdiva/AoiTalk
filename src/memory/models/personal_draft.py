"""Owner-scoped encrypted evidence and immutable draft lifecycle events."""

import uuid
from datetime import datetime

from sqlalchemy import Column, String, JSON, DateTime, ForeignKey, ForeignKeyConstraint, Index, CheckConstraint, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID

from .base import Base, _encrypted_json_property


class PersonalDraftEvent(Base):
    __tablename__ = "personal_draft_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    event_type = Column(String(16), nullable=False)
    draft_ref = Column(UUID(as_uuid=True), nullable=True)
    evidence_ref = Column(UUID(as_uuid=True), nullable=True)
    target_entry_id = Column(UUID(as_uuid=True), nullable=True)
    # Only opaque UUID operation keys / server-derived keys; never source values.
    operation_key = Column(String(160), nullable=False)
    _payload = Column("payload", JSON, nullable=False)
    payload = _encrypted_json_property("_payload", "personal_draft_events.payload")
    recorded_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("owner_user_id", "id", name="uq_personal_draft_owner_id"),
        ForeignKeyConstraint(["owner_user_id", "draft_ref"], ["personal_draft_events.owner_user_id", "personal_draft_events.id"], name="fk_personal_draft_owned_draft"),
        ForeignKeyConstraint(["owner_user_id", "evidence_ref"], ["personal_draft_events.owner_user_id", "personal_draft_events.id"], name="fk_personal_draft_owned_evidence"),
        ForeignKeyConstraint(["owner_user_id", "target_entry_id"], ["personal_ledger_entries.owner_user_id", "personal_ledger_entries.id"], name="fk_personal_draft_owned_target"),
        UniqueConstraint("owner_user_id", "operation_key", name="uq_personal_draft_operation"),
        Index("ix_personal_draft_owner", "owner_user_id", "recorded_at", "id"),
        Index("uq_personal_draft_terminal", "draft_ref", unique=True,
              postgresql_where=text("event_type IN ('accepted', 'discarded')")),
        Index("uq_personal_draft_evidence", "evidence_ref", unique=True,
              postgresql_where=text("event_type = 'proposed'")),
        UniqueConstraint("target_entry_id", name="uq_personal_draft_target"),
        CheckConstraint("event_type IN ('evidence', 'proposed', 'accepted', 'discarded', 'receipt')", name="ck_personal_draft_type"),
        CheckConstraint("(event_type IN ('accepted', 'discarded')) = (draft_ref IS NOT NULL)", name="ck_personal_draft_ref"),
        CheckConstraint("(event_type = 'proposed') = (evidence_ref IS NOT NULL)", name="ck_personal_draft_evidence"),
        CheckConstraint("(event_type = 'accepted') = (target_entry_id IS NOT NULL)", name="ck_personal_draft_target"),
    )
