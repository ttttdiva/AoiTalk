"""Enterprise-only durable deployment state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


class EnterpriseBootstrapState(Base):
    """Singleton recording the stable bootstrap administrator and completion."""

    __tablename__ = "enterprise_bootstrap_state"

    id = Column(Integer, primary_key=True, default=1)
    bootstrap_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_enterprise_bootstrap_singleton"),
    )
