"""プロジェクト通知設定・通知配信系モデル。"""

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import (
    Column,
    String,
    Text,
    DateTime,
    JSON,
    ForeignKey,
    Boolean,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .base import Base


class ProjectNotificationSetting(Base):
    """Project-level notification settings for task reminders."""

    __tablename__ = "project_notification_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    discord_webhook_url = Column(Text, nullable=True)
    default_reminder_offsets = Column(JSON, default=lambda: [15])
    notify_overdue = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    project = relationship("Project", back_populates="notification_settings")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "discord_webhook_url": self.discord_webhook_url,
            "default_reminder_offsets": list(self.default_reminder_offsets or []),
            "notify_overdue": self.notify_overdue,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class NotificationDelivery(Base):
    """Persisted notification/inbox item and external delivery record."""

    __tablename__ = "notification_deliveries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    task_id = Column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    occurrence_id = Column(
        UUID(as_uuid=True), ForeignKey("task_occurrences.id"), nullable=True, index=True
    )
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    channel = Column(String(32), nullable=False, index=True)  # in_app / discord_webhook
    notification_type = Column(
        String(32), nullable=False, index=True
    )  # reminder / overdue / timer
    dedupe_key = Column(String(255), nullable=False, unique=True, index=True)
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    scheduled_for = Column(DateTime, nullable=False, index=True)
    delivered_at = Column(DateTime, nullable=True, index=True)
    read_at = Column(DateTime, nullable=True, index=True)
    status = Column(String(32), default="pending", nullable=False, index=True)
    payload = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    project = relationship("Project", back_populates="notification_deliveries")
    task = relationship("Task", back_populates="notification_deliveries")
    occurrence = relationship("TaskOccurrence")
    user = relationship("User")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "task_id": str(self.task_id) if self.task_id else None,
            "occurrence_id": str(self.occurrence_id) if self.occurrence_id else None,
            "user_id": str(self.user_id) if self.user_id else None,
            "channel": self.channel,
            "notification_type": self.notification_type,
            "dedupe_key": self.dedupe_key,
            "title": self.title,
            "message": self.message,
            "scheduled_for": (
                self.scheduled_for.isoformat() if self.scheduled_for else None
            ),
            "delivered_at": (
                self.delivered_at.isoformat() if self.delivered_at else None
            ),
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "status": self.status,
            "payload": self.payload or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
