"""Durable audit rows for content deletion lifecycle events.

The rows in :class:`ContentDeletionEvent` intentionally keep only identity and
provenance information.  The content being deleted is never copied into this
table, so the audit trail remains useful after the subject row has gone away.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .base import Base


class ContentDeletionEvent(Base):
    """Append-only provenance for a content deletion lifecycle.

    ``entity_id`` and ``root_entity_id`` are deliberately plain text rather
    than foreign keys.  A deletion event must remain queryable after its
    subject (which may be a DB UUID, a path, or another opaque token) is
    physically removed.  The project and actor references are ordinary
    nullable ``SET NULL`` FKs because those rows may also be removed later.

    SQLAlchemy reserves the declarative attribute name ``metadata``.  The
    persisted column keeps that contract while the Python attribute is named
    ``event_metadata``.
    """

    __tablename__ = "content_deletion_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_type = Column(String(32), nullable=False)
    entity_id = Column(String(512), nullable=False)
    root_entity_id = Column(String(512), nullable=True)
    # Every lifecycle event belongs to an operation batch.  Single-entity
    # operations still receive a freshly generated UUID from the append
    # helper, so the ledger never has an orphan event without provenance.
    batch_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action = Column(String(32), nullable=False)
    display_name = Column(String(255), nullable=True)
    source = Column(String(64), nullable=True)
    event_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    event_metadata = Column("metadata", JSON, nullable=False, default=dict)

    project = relationship("Project", foreign_keys=[project_id])
    actor = relationship("User", foreign_keys=[actor_user_id])

    __table_args__ = (
        CheckConstraint(
            "action IN ('deleted', 'restored', 'purged', 'permanent_deleted')",
            name="ck_content_deletion_events_action",
        ),
        Index(
            "ix_content_deletion_events_entity",
            "entity_type",
            "entity_id",
        ),
        Index(
            "ix_content_deletion_events_root_event_at",
            "root_entity_id",
            "event_at",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Return audit metadata without exposing any content body."""

        return {
            "id": str(self.id),
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "root_entity_id": self.root_entity_id,
            "batch_id": str(self.batch_id) if self.batch_id else None,
            "project_id": str(self.project_id) if self.project_id else None,
            "actor_user_id": str(self.actor_user_id) if self.actor_user_id else None,
            "action": self.action,
            "display_name": self.display_name,
            "source": self.source,
            "event_at": self.event_at.isoformat() if self.event_at else None,
            "metadata": self.event_metadata or {},
        }


__all__ = ["ContentDeletionEvent"]
