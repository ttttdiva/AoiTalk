"""Durable idempotency receipts for authenticated Task API mutations."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, JSON, String
from sqlalchemy.dialects.postgresql import UUID

from .base import Base, _encrypted_json_property


class TaskApiOperationReceipt(Base):
    __tablename__ = "task_api_operation_receipts"

    # Retain the actor UUID as durable receipt identity even if an account is
    # later removed; unlike the result body, this is not a user data cascade.
    actor_id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    operation_id = Column(UUID(as_uuid=True), primary_key=True)
    operation = Column("op", String(16), nullable=False)
    task_id = Column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    request_hash = Column(String(64), nullable=False)
    _result_json = Column("result_json", JSON, nullable=False, default=dict)
    result_json = _encrypted_json_property(
        "_result_json", "task_api_operation_receipts.result_json"
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
