"""Owner-scoped encrypted reminder selections, drafts and in-app delivery lifecycle (append-only)."""

import uuid
from datetime import datetime

from sqlalchemy import Column, String, JSON, DateTime, ForeignKey, ForeignKeyConstraint, Index, CheckConstraint, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID

from .base import Base, _encrypted_json_property


class PersonalReminderEvent(Base):
    __tablename__ = "personal_reminder_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    event_type = Column(String(16), nullable=False)
    candidate_entry_id = Column(UUID(as_uuid=True), nullable=False)
    selection_ref = Column(UUID(as_uuid=True), nullable=True)
    # Lifecycle events (scheduled/cancelled/delivered/invalidated/acknowledged) point at the owner's draft.
    draft_ref = Column(UUID(as_uuid=True), nullable=True)
    # Opaque prefixed idempotency keys only; never document/account identifiers.
    operation_key = Column(String(160), nullable=False)
    _payload = Column("payload", JSON, nullable=False)
    payload = _encrypted_json_property("_payload", "personal_reminder_events.payload")
    recorded_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("owner_user_id", "id", name="uq_personal_reminder_owner_id"),
        ForeignKeyConstraint(["owner_user_id", "candidate_entry_id"], ["personal_ledger_entries.owner_user_id", "personal_ledger_entries.id"], name="fk_personal_reminder_owned_candidate"),
        ForeignKeyConstraint(["owner_user_id", "selection_ref"], ["personal_reminder_events.owner_user_id", "personal_reminder_events.id"], name="fk_personal_reminder_owned_selection"),
        ForeignKeyConstraint(["owner_user_id", "draft_ref"], ["personal_reminder_events.owner_user_id", "personal_reminder_events.id"], name="fk_personal_reminder_owned_draft"),
        UniqueConstraint("owner_user_id", "operation_key", name="uq_personal_reminder_operation"),
        Index("ix_personal_reminder_owner", "owner_user_id", "recorded_at", "id"),
        Index("uq_personal_reminder_selection", "selection_ref", unique=True,
              postgresql_where=text("event_type = 'proposed'")),
        Index("uq_personal_reminder_scheduled", "draft_ref", unique=True,
              postgresql_where=text("event_type = 'scheduled'")),
        Index("uq_personal_reminder_terminal", "draft_ref", unique=True,
              postgresql_where=text("event_type IN ('cancelled', 'delivered', 'invalidated')")),
        Index("uq_personal_reminder_acknowledged", "draft_ref", unique=True,
              postgresql_where=text("event_type = 'acknowledged'")),
        CheckConstraint("event_type IN ('selection', 'proposed', 'scheduled', 'cancelled', 'delivered', 'invalidated', 'acknowledged')", name="ck_personal_reminder_type"),
        CheckConstraint("(event_type IN ('scheduled', 'cancelled', 'delivered', 'invalidated', 'acknowledged')) = (draft_ref IS NOT NULL)", name="ck_personal_reminder_draft"),
        CheckConstraint("(event_type = 'proposed') = (selection_ref IS NOT NULL)", name="ck_personal_reminder_selection"),
    )
