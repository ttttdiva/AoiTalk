"""Owner-scoped, encrypted receipt evidence + unconfirmed finance candidate.

Append-only and deliberately unlinked from ``personal_finance_entries``: a
candidate never becomes a ledger row without a separate owner confirmation.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, JSON, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from .base import Base, _encrypted_json_property


class PersonalFinanceReceiptCandidate(Base):
    __tablename__ = "personal_finance_receipt_candidates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_ref = Column(UUID(as_uuid=True), nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    # Opaque UUID-derived key; never source values.
    operation_key = Column(String(160), nullable=False)
    status = Column(String(16), nullable=False, default="needs_review")
    # evidence (extracted text, spans, versions) and proposal fields, encrypted together.
    _payload = Column("payload", JSON, nullable=False)
    payload = _encrypted_json_property("_payload", "personal_finance_receipt_candidates.payload")
    recorded_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("owner_user_id", "operation_key", name="uq_personal_receipt_operation"),
        CheckConstraint("status = 'needs_review'", name="ck_personal_receipt_status"),
        Index("ix_personal_receipt_owner", "owner_user_id", "recorded_at", "id"),
    )
