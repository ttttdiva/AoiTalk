"""Personal finance ENTRY model (F1 minimal slice).

Own table only: ``personal_finance_entries``.  The private row/source content
lives in the encrypted ``payload`` column; scope, idempotency, digest, and actor
kind stay in clear columns so uniqueness is enforced by the database.

The accepted-dataset snapshot (``PersonalFinanceDataset`` + members) freezes a set of
owner-confirmed entries for the F2 summary.  All three tables are append only
(DB trigger in migration ``20261022_0201``).  Receipt candidates are never linked here.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from .base import Base, _encrypted_json_property


class PersonalFinanceEntry(Base):
    """One owner-confirmed finance entry (append only)."""

    __tablename__ = "personal_finance_entries"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_personal_finance_entries_owner_idempotency_key",
        ),
        UniqueConstraint(
            "owner_user_id",
            "row_ref",
            name="uq_personal_finance_entries_owner_row_ref",
        ),
        UniqueConstraint(
            "owner_user_id",
            "id",
            name="uq_personal_finance_entries_owner_id",
        ),
        CheckConstraint(
            "confirmed_by_kind = 'user_action'",
            name="ck_personal_finance_entries_owner_confirmed",
        ),
    )

    id = Column(
        PGUUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        default=uuid4,
    )
    owner_user_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    row_ref = Column(PGUUID(as_uuid=True), nullable=False)
    idempotency_key = Column(String(128), nullable=False)
    content_digest = Column(String(64), nullable=False)
    confirmed_by_kind = Column(String(16), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    _payload = Column("payload", JSON, nullable=False)
    payload = _encrypted_json_property(
        "_payload",
        "personal_finance_entries.payload",
    )


class PersonalFinanceDataset(Base):
    """Immutable accepted dataset header (cleartext refs, counts and digest only)."""

    __tablename__ = "personal_finance_datasets"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "id", name="uq_personal_finance_datasets_owner_id"),
        UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_personal_finance_datasets_owner_idempotency_key",
        ),
        CheckConstraint(
            "member_count BETWEEN 1 AND 1000",
            name="ck_personal_finance_datasets_member_count",
        ),
    )

    id = Column(PGUUID(as_uuid=True), primary_key=True, nullable=False, default=uuid4)
    owner_user_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    idempotency_key = Column(String(128), nullable=False)
    member_count = Column(Integer, nullable=False)
    complete = Column(Boolean, nullable=False)
    members_digest = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PersonalFinanceDatasetMember(Base):
    """One frozen entry reference of an accepted dataset."""

    __tablename__ = "personal_finance_dataset_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_user_id", "dataset_id"],
            ["personal_finance_datasets.owner_user_id", "personal_finance_datasets.id"],
            name="fk_personal_finance_members_owned_dataset",
        ),
        ForeignKeyConstraint(
            ["owner_user_id", "entry_id"],
            ["personal_finance_entries.owner_user_id", "personal_finance_entries.id"],
            name="fk_personal_finance_members_owned_entry",
        ),
    )

    dataset_id = Column(PGUUID(as_uuid=True), primary_key=True, nullable=False)
    row_ref = Column(PGUUID(as_uuid=True), primary_key=True, nullable=False)
    owner_user_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    entry_id = Column(PGUUID(as_uuid=True), nullable=False)
    content_digest = Column(String(64), nullable=False)
