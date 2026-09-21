"""Durable idempotency ledger for mediated Project storage publication."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


class ProjectStorageOperation(Base):
    __tablename__ = "project_storage_operations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    principal_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    operation_id = Column(String(256), nullable=False)
    diff_sha256 = Column(String(64), nullable=False)
    state = Column(String(16), nullable=False, default="prepared", index=True)
    result_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "principal_id",
            "operation_id",
            name="uq_project_storage_operations_identity",
        ),
        CheckConstraint(
            "state IN ('prepared','committed','failed')",
            name="ck_project_storage_operations_state",
        ),
        Index(
            "ix_project_storage_operations_project_state",
            "project_id",
            "state",
        ),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "principal_id": str(self.principal_id),
            "operation_id": self.operation_id,
            "diff_sha256": self.diff_sha256,
            "state": self.state,
            "result": self.result_json or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = ["ProjectStorageOperation"]
