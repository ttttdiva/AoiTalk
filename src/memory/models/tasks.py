"""タスク・スケジュール・タイマー・タグ系モデル。"""

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import (
    Column,
    String,
    Text,
    Integer,
    DateTime,
    Float,
    JSON,
    ForeignKey,
    Boolean,
    UniqueConstraint,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from ...task_time import DEFAULT_TASK_TIMEZONE
from ...services.project_color_service import extract_project_color
from .base import Base, _encrypted_text_property


class LocalTask(Base):
    """Task item stored in the built-in task workspace."""

    __tablename__ = "local_tasks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True, index=True
    )
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    status = Column(
        String, default="todo", nullable=False, index=True
    )  # todo/in_progress/paused/blocked/closed
    source = Column(String, default="manual", nullable=False)
    due_at = Column(DateTime, nullable=True)
    priority = Column(String, nullable=True)  # urgent/high/normal/low
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    task_metadata = Column(JSON, default=dict)

    # Relationships
    project = relationship("Project", back_populates="local_tasks")
    events = relationship(
        "TaskEvent", back_populates="task", cascade="all, delete-orphan"
    )
    execution_sessions = relationship(
        "TaskExecutionSession", back_populates="task", cascade="all, delete-orphan"
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id) if self.project_id else None,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "source": self.source,
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "priority": self.priority,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "metadata": self.task_metadata,
        }


class TaskEvent(Base):
    """タスクイベント履歴"""

    __tablename__ = "task_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(
        UUID(as_uuid=True), ForeignKey("local_tasks.id"), nullable=False, index=True
    )
    event_type = Column(
        String, nullable=False
    )  # started/paused/resumed/completed/blocked
    timestamp = Column(DateTime, default=datetime.utcnow)
    trigger_source = Column(String, default="manual")  # ahk/agent/manual
    payload = Column(JSON, default=dict)

    # Relationships
    task = relationship("LocalTask", back_populates="events")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "task_id": str(self.task_id),
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "trigger_source": self.trigger_source,
            "payload": self.payload,
        }


class TaskExecutionSession(Base):
    """タスク作業セッション（開始〜終了の区間記録）"""

    __tablename__ = "task_execution_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(
        UUID(as_uuid=True), ForeignKey("local_tasks.id"), nullable=False, index=True
    )
    started_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime, nullable=True)
    trigger_source = Column(String, default="manual")  # ahk/agent/manual
    toggl_entry_id = Column(String, nullable=True)

    # Relationships
    task = relationship("LocalTask", back_populates="execution_sessions")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "task_id": str(self.task_id),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "trigger_source": self.trigger_source,
            "toggl_entry_id": self.toggl_entry_id,
        }


class Task(Base):
    """First-class local task entity used by the rebuilt task system."""

    __tablename__ = "tasks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    legacy_local_task_id = Column(
        UUID(as_uuid=True), ForeignKey("local_tasks.id"), nullable=True, unique=True
    )
    title = Column(String(255), nullable=False)
    description = Column(Text)
    status = Column(String(32), nullable=False, default="todo", index=True)
    priority = Column(String(16), default="normal", index=True)
    start_at = Column(DateTime, nullable=True, index=True)
    end_at = Column(DateTime, nullable=True, index=True)
    all_day = Column(Boolean, default=False, nullable=False)
    reminder_offsets = Column(JSON, default=list)
    notifications_enabled = Column(Boolean, default=True, nullable=False)
    source = Column(String(32), default="local", nullable=False)
    created_by = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    completed_at = Column(DateTime, nullable=True)
    archived_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    deleted_at = Column(DateTime, nullable=True, index=True)  # tombstone for sync
    task_metadata = Column(JSON, default=dict)
    estimated_hours = Column(Float, nullable=True)
    sort_order = Column(Float, default=0, nullable=False)
    parent_task_id = Column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=True, index=True
    )

    project = relationship("Project", back_populates="tasks")
    creator = relationship("User", foreign_keys=[created_by])
    legacy_local_task = relationship("LocalTask")
    assignees = relationship(
        "TaskAssignee", back_populates="task", cascade="all, delete-orphan"
    )
    comments = relationship(
        "TaskComment", back_populates="task", cascade="all, delete-orphan"
    )
    attachments = relationship(
        "TaskAttachment", back_populates="task", cascade="all, delete-orphan"
    )
    activities = relationship(
        "TaskActivity", back_populates="task", cascade="all, delete-orphan"
    )
    dependencies = relationship(
        "TaskDependency",
        foreign_keys="TaskDependency.task_id",
        back_populates="task",
        cascade="all, delete-orphan",
    )
    recurrence_rule = relationship(
        "TaskRecurrenceRule",
        back_populates="task",
        cascade="all, delete-orphan",
        uselist=False,
    )
    occurrences = relationship(
        "TaskOccurrence", back_populates="task", cascade="all, delete-orphan"
    )
    time_entries = relationship(
        "TimeEntry", back_populates="task", cascade="all, delete-orphan"
    )
    task_tags = relationship(
        "TaskTag", back_populates="task", cascade="all, delete-orphan"
    )
    notification_deliveries = relationship(
        "NotificationDelivery", back_populates="task", passive_deletes=True
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "legacy_local_task_id": (
                str(self.legacy_local_task_id) if self.legacy_local_task_id else None
            ),
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "end_at": self.end_at.isoformat() if self.end_at else None,
            "all_day": self.all_day,
            "reminder_offsets": list(self.reminder_offsets or []),
            "notifications_enabled": self.notifications_enabled,
            "source": self.source,
            "created_by": str(self.created_by) if self.created_by else None,
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
            "metadata": self.task_metadata or {},
            "estimated_hours": self.estimated_hours,
            "sort_order": self.sort_order,
            "parent_task_id": str(self.parent_task_id) if self.parent_task_id else None,
            "project_color": (
                extract_project_color(self.project.project_metadata)
                if self.project is not None
                else None
            ),
            "project_name": self.project.name if self.project is not None else None,
            "assignees": [assignee.to_dict() for assignee in self.assignees],
            "recurrence_rule": (
                self.recurrence_rule.to_dict() if self.recurrence_rule else None
            ),
            "tags": [
                {**tt.tag.to_dict(), "project_id": str(self.project_id)}
                for tt in self.task_tags
                if tt.tag is not None
            ],
        }


class TaskAssignee(Base):
    """Users assigned to a task."""

    __tablename__ = "task_assignees"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False, index=True
    )
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    is_primary = Column(Boolean, default=False, nullable=False)
    assigned_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    assigned_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    task = relationship("Task", back_populates="assignees")
    user = relationship("User", foreign_keys=[user_id])
    assigner = relationship("User", foreign_keys=[assigned_by])

    __table_args__ = (
        UniqueConstraint("task_id", "user_id", name="unique_task_assignee"),
    )

    def to_dict(self) -> Dict[str, Any]:
        user_display = None
        username = None
        if self.user is not None:
            user_display = self.user.display_name
            username = self.user.username
        return {
            "id": str(self.id),
            "task_id": str(self.task_id),
            "user_id": str(self.user_id),
            "is_primary": self.is_primary,
            "assigned_at": self.assigned_at.isoformat() if self.assigned_at else None,
            "assigned_by": str(self.assigned_by) if self.assigned_by else None,
            "display_name": user_display,
            "username": username,
        }


class TaskComment(Base):
    """Discussion/comments for a task."""

    __tablename__ = "task_comments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False, index=True
    )
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    _content = Column("content", Text, nullable=False)
    content = _encrypted_text_property("_content", "task_comments.content")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    task = relationship("Task", back_populates="comments")
    user = relationship("User")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "task_id": str(self.task_id),
            "user_id": str(self.user_id),
            "content": self.content,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "username": self.user.username if self.user else None,
            "display_name": self.user.display_name if self.user else None,
        }


class TaskAttachment(Base):
    """Files attached to a task and stored under the project workspace."""

    __tablename__ = "task_attachments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id = Column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_path = Column(Text, nullable=False)
    display_name = Column(String(255), nullable=False)
    mime_type = Column(String(255), nullable=True)
    size_bytes = Column(Integer, default=0, nullable=False)
    kind = Column(String(32), default="file", nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    attachment_metadata = Column("metadata", JSON, default=dict)

    task = relationship("Task", back_populates="attachments")
    project = relationship("Project")
    creator = relationship("User", foreign_keys=[created_by])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "task_id": str(self.task_id),
            "project_id": str(self.project_id),
            "file_path": self.file_path,
            "display_name": self.display_name,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "kind": self.kind,
            "created_by": str(self.created_by) if self.created_by else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "metadata": self.attachment_metadata or {},
        }


class TaskActivity(Base):
    """Task activity log used for audit and live updates."""

    __tablename__ = "task_activities"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False, index=True
    )
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    activity_type = Column(String(64), nullable=False, index=True)
    payload = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    task = relationship("Task", back_populates="activities")
    user = relationship("User")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "task_id": str(self.task_id),
            "user_id": str(self.user_id) if self.user_id else None,
            "activity_type": self.activity_type,
            "payload": self.payload or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "username": self.user.username if self.user else None,
            "display_name": self.user.display_name if self.user else None,
        }


class TaskDependency(Base):
    """Task-level dependency relation."""

    __tablename__ = "task_dependencies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False, index=True
    )
    depends_on_task_id = Column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False, index=True
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    task = relationship("Task", foreign_keys=[task_id], back_populates="dependencies")
    depends_on_task = relationship("Task", foreign_keys=[depends_on_task_id])

    __table_args__ = (
        UniqueConstraint(
            "task_id", "depends_on_task_id", name="unique_task_dependency"
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "task_id": str(self.task_id),
            "depends_on_task_id": str(self.depends_on_task_id),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class TaskRecurrenceRule(Base):
    """Recurrence rule stored as RRULE text."""

    __tablename__ = "task_recurrence_rules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    rrule = Column(Text, nullable=False)
    timezone = Column(String(64), default=DEFAULT_TASK_TIMEZONE, nullable=False)
    horizon_days = Column(Integer, default=90, nullable=False)
    trigger_status = Column(String(32), default="closed")
    create_new = Column(Boolean, default=False)
    recur_forever = Column(Boolean, default=True)
    reset_status_to = Column(String(32), default="open")
    end_count = Column(Integer, nullable=True)
    end_date = Column(DateTime, nullable=True)
    skip_weekend = Column(Boolean, default=False, nullable=False)
    skip_holiday = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    task = relationship("Task", back_populates="recurrence_rule")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "task_id": str(self.task_id),
            "rrule": self.rrule,
            "timezone": self.timezone,
            "horizon_days": self.horizon_days,
            "trigger_status": self.trigger_status or "closed",
            "create_new": bool(self.create_new),
            "recur_forever": (
                True if self.recur_forever is None else bool(self.recur_forever)
            ),
            "reset_status_to": self.reset_status_to or "open",
            "end_count": self.end_count,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "skip_weekend": bool(self.skip_weekend),
            "skip_holiday": bool(self.skip_holiday),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TaskOccurrence(Base):
    """Materialized occurrence for scheduled or recurring tasks."""

    __tablename__ = "task_occurrences"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False, index=True
    )
    start_at = Column(DateTime, nullable=False, index=True)
    end_at = Column(DateTime, nullable=False, index=True)
    status = Column(String(32), nullable=False, default="todo", index=True)
    all_day = Column(Boolean, default=False, nullable=False)
    reminder_offsets = Column(JSON, default=list)
    source_kind = Column(String(32), default="task_schedule", nullable=False)
    is_generated = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    deleted_at = Column(DateTime, nullable=True, index=True)  # tombstone for sync

    task = relationship("Task", back_populates="occurrences")
    time_entries = relationship("TimeEntry", back_populates="occurrence")

    __table_args__ = (
        UniqueConstraint("task_id", "start_at", name="unique_task_occurrence_start"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "task_id": str(self.task_id),
            "project_id": (
                str(self.task.project_id)
                if self.task and self.task.project_id
                else None
            ),
            "project_color": (
                extract_project_color(self.task.project.project_metadata)
                if self.task and self.task.project is not None
                else None
            ),
            "project_name": (
                self.task.project.name
                if self.task and self.task.project is not None
                else None
            ),
            "title": self.task.title if self.task else None,
            "status": self.status,
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "end_at": self.end_at.isoformat() if self.end_at else None,
            "all_day": self.all_day,
            "reminder_offsets": list(self.reminder_offsets or []),
            "tags": [
                {**tt.tag.to_dict(), "project_id": str(self.task.project_id)}
                for tt in (self.task.task_tags if self.task else [])
                if tt.tag is not None
            ],
            "source_kind": self.source_kind,
            "is_generated": self.is_generated,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TimeEntry(Base):
    """Tracked work against a task."""

    __tablename__ = "time_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False, index=True
    )
    occurrence_id = Column(
        UUID(as_uuid=True), ForeignKey("task_occurrences.id"), nullable=True, index=True
    )
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    started_at = Column(DateTime, nullable=False, index=True)
    ended_at = Column(DateTime, nullable=True, index=True)
    source = Column(String(32), default="manual", nullable=False)
    note = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    deleted_at = Column(DateTime, nullable=True, index=True)  # tombstone for sync
    entry_metadata = Column(JSON, default=dict)

    task = relationship("Task", back_populates="time_entries")
    occurrence = relationship("TaskOccurrence", back_populates="time_entries")
    user = relationship("User")

    __table_args__ = (
        Index(
            "ix_time_entries_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        duration_seconds = None
        if self.started_at and self.ended_at:
            duration_seconds = int((self.ended_at - self.started_at).total_seconds())
        return {
            "id": str(self.id),
            "task_id": str(self.task_id),
            "occurrence_id": str(self.occurrence_id) if self.occurrence_id else None,
            "user_id": str(self.user_id),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_seconds": duration_seconds,
            "source": self.source,
            "note": self.note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
            "metadata": self.entry_metadata or {},
            "username": self.user.username if self.user else None,
            "display_name": self.user.display_name if self.user else None,
            "task_title": self.task.title if self.task else None,
            "project_id": (
                str(self.task.project_id)
                if self.task and self.task.project_id
                else None
            ),
            "project_name": (
                self.task.project.name
                if self.task and self.task.project is not None
                else None
            ),
            "occurrence_start_at": (
                self.occurrence.start_at.isoformat()
                if self.occurrence and self.occurrence.start_at
                else None
            ),
            "occurrence_end_at": (
                self.occurrence.end_at.isoformat()
                if self.occurrence and self.occurrence.end_at
                else None
            ),
        }


class Tag(Base):
    """スペーススコープのタグ（タスクに付与可能）"""

    __tablename__ = "tags"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    space_id = Column(
        UUID(as_uuid=True),
        ForeignKey("spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(100), nullable=False)
    color = Column(String(32), nullable=True)
    created_by = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("space_id", "name", name="uq_tags_space_name"),
    )

    space = relationship("Space", back_populates="tags")
    task_tags = relationship(
        "TaskTag", back_populates="tag", cascade="all, delete-orphan"
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "space_id": str(self.space_id),
            "name": self.name,
            "color": self.color,
            "created_by": str(self.created_by) if self.created_by else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class TaskTag(Base):
    """タスクとタグの中間テーブル"""

    __tablename__ = "task_tags"

    task_id = Column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id = Column(
        UUID(as_uuid=True), ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )

    task = relationship("Task", back_populates="task_tags")
    tag = relationship("Tag", back_populates="task_tags")
