"""Local-first task, schedule, timer, and notification tables.

Revision ID: 20260319_0001
Revises:
Create Date: 2026-03-19 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260319_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("legacy_local_task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("local_tasks.id"), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="todo"),
        sa.Column("priority", sa.String(length=16), nullable=False, server_default="normal"),
        sa.Column("start_at", sa.DateTime(), nullable=True),
        sa.Column("end_at", sa.DateTime(), nullable=True),
        sa.Column("all_day", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("reminder_offsets", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="local"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("task_metadata", sa.JSON(), nullable=True),
        sa.UniqueConstraint("legacy_local_task_id", name="uq_tasks_legacy_local_task_id"),
    )
    op.create_index("ix_tasks_project_id", "tasks", ["project_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_priority", "tasks", ["priority"])
    op.create_index("ix_tasks_start_at", "tasks", ["start_at"])
    op.create_index("ix_tasks_end_at", "tasks", ["end_at"])
    op.create_index("ix_tasks_created_by", "tasks", ["created_by"])
    op.create_index("ix_tasks_archived_at", "tasks", ["archived_at"])

    op.create_table(
        "task_assignees",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("assigned_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("assigned_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.UniqueConstraint("task_id", "user_id", name="unique_task_assignee"),
    )
    op.create_index("ix_task_assignees_task_id", "task_assignees", ["task_id"])
    op.create_index("ix_task_assignees_user_id", "task_assignees", ["user_id"])

    op.create_table(
        "task_comments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_task_comments_task_id", "task_comments", ["task_id"])
    op.create_index("ix_task_comments_user_id", "task_comments", ["user_id"])

    op.create_table(
        "task_activities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("activity_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_task_activities_task_id", "task_activities", ["task_id"])
    op.create_index("ix_task_activities_user_id", "task_activities", ["user_id"])
    op.create_index("ix_task_activities_type", "task_activities", ["activity_type"])
    op.create_index("ix_task_activities_created_at", "task_activities", ["created_at"])

    op.create_table(
        "task_dependencies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("depends_on_task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("task_id", "depends_on_task_id", name="unique_task_dependency"),
    )
    op.create_index("ix_task_dependencies_task_id", "task_dependencies", ["task_id"])
    op.create_index("ix_task_dependencies_depends_on_task_id", "task_dependencies", ["depends_on_task_id"])

    op.create_table(
        "task_recurrence_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("rrule", sa.Text(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column("horizon_days", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("task_id", name="uq_task_recurrence_rules_task_id"),
    )
    op.create_index("ix_task_recurrence_rules_task_id", "task_recurrence_rules", ["task_id"])

    op.create_table(
        "task_occurrences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("start_at", sa.DateTime(), nullable=False),
        sa.Column("end_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="todo"),
        sa.Column("all_day", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("reminder_offsets", sa.JSON(), nullable=True),
        sa.Column("source_kind", sa.String(length=32), nullable=False, server_default="task_schedule"),
        sa.Column("is_generated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("task_id", "start_at", name="unique_task_occurrence_start"),
    )
    op.create_index("ix_task_occurrences_task_id", "task_occurrences", ["task_id"])
    op.create_index("ix_task_occurrences_start_at", "task_occurrences", ["start_at"])
    op.create_index("ix_task_occurrences_end_at", "task_occurrences", ["end_at"])
    op.create_index("ix_task_occurrences_status", "task_occurrences", ["status"])

    op.create_table(
        "time_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("occurrence_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("task_occurrences.id"), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("entry_metadata", sa.JSON(), nullable=True),
    )
    op.create_index("ix_time_entries_task_id", "time_entries", ["task_id"])
    op.create_index("ix_time_entries_occurrence_id", "time_entries", ["occurrence_id"])
    op.create_index("ix_time_entries_user_id", "time_entries", ["user_id"])
    op.create_index("ix_time_entries_started_at", "time_entries", ["started_at"])
    op.create_index("ix_time_entries_ended_at", "time_entries", ["ended_at"])
    op.create_index(
        "ix_time_entries_active_user",
        "time_entries",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )

    op.create_table(
        "project_notification_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("discord_webhook_url", sa.Text(), nullable=True),
        sa.Column("default_reminder_offsets", sa.JSON(), nullable=True),
        sa.Column("notify_overdue", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("project_id", name="uq_project_notification_settings_project_id"),
    )
    op.create_index("ix_project_notification_settings_project_id", "project_notification_settings", ["project_id"])

    op.create_table(
        "notification_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("occurrence_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("task_occurrences.id"), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("notification_type", sa.String(length=32), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("dedupe_key", name="uq_notification_deliveries_dedupe_key"),
    )
    op.create_index("ix_notification_deliveries_project_id", "notification_deliveries", ["project_id"])
    op.create_index("ix_notification_deliveries_task_id", "notification_deliveries", ["task_id"])
    op.create_index("ix_notification_deliveries_occurrence_id", "notification_deliveries", ["occurrence_id"])
    op.create_index("ix_notification_deliveries_user_id", "notification_deliveries", ["user_id"])
    op.create_index("ix_notification_deliveries_channel", "notification_deliveries", ["channel"])
    op.create_index("ix_notification_deliveries_notification_type", "notification_deliveries", ["notification_type"])
    op.create_index("ix_notification_deliveries_scheduled_for", "notification_deliveries", ["scheduled_for"])
    op.create_index("ix_notification_deliveries_delivered_at", "notification_deliveries", ["delivered_at"])
    op.create_index("ix_notification_deliveries_read_at", "notification_deliveries", ["read_at"])
    op.create_index("ix_notification_deliveries_status", "notification_deliveries", ["status"])


def downgrade() -> None:
    op.drop_index("ix_notification_deliveries_status", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_read_at", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_delivered_at", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_scheduled_for", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_notification_type", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_channel", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_user_id", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_occurrence_id", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_task_id", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_project_id", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")

    op.drop_index("ix_project_notification_settings_project_id", table_name="project_notification_settings")
    op.drop_table("project_notification_settings")

    op.drop_index("ix_time_entries_active_user", table_name="time_entries")
    op.drop_index("ix_time_entries_ended_at", table_name="time_entries")
    op.drop_index("ix_time_entries_started_at", table_name="time_entries")
    op.drop_index("ix_time_entries_user_id", table_name="time_entries")
    op.drop_index("ix_time_entries_occurrence_id", table_name="time_entries")
    op.drop_index("ix_time_entries_task_id", table_name="time_entries")
    op.drop_table("time_entries")

    op.drop_index("ix_task_occurrences_status", table_name="task_occurrences")
    op.drop_index("ix_task_occurrences_end_at", table_name="task_occurrences")
    op.drop_index("ix_task_occurrences_start_at", table_name="task_occurrences")
    op.drop_index("ix_task_occurrences_task_id", table_name="task_occurrences")
    op.drop_table("task_occurrences")

    op.drop_index("ix_task_recurrence_rules_task_id", table_name="task_recurrence_rules")
    op.drop_table("task_recurrence_rules")

    op.drop_index("ix_task_dependencies_depends_on_task_id", table_name="task_dependencies")
    op.drop_index("ix_task_dependencies_task_id", table_name="task_dependencies")
    op.drop_table("task_dependencies")

    op.drop_index("ix_task_activities_created_at", table_name="task_activities")
    op.drop_index("ix_task_activities_type", table_name="task_activities")
    op.drop_index("ix_task_activities_user_id", table_name="task_activities")
    op.drop_index("ix_task_activities_task_id", table_name="task_activities")
    op.drop_table("task_activities")

    op.drop_index("ix_task_comments_user_id", table_name="task_comments")
    op.drop_index("ix_task_comments_task_id", table_name="task_comments")
    op.drop_table("task_comments")

    op.drop_index("ix_task_assignees_user_id", table_name="task_assignees")
    op.drop_index("ix_task_assignees_task_id", table_name="task_assignees")
    op.drop_table("task_assignees")

    op.drop_index("ix_tasks_archived_at", table_name="tasks")
    op.drop_index("ix_tasks_created_by", table_name="tasks")
    op.drop_index("ix_tasks_end_at", table_name="tasks")
    op.drop_index("ix_tasks_start_at", table_name="tasks")
    op.drop_index("ix_tasks_priority", table_name="tasks")
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_index("ix_tasks_project_id", table_name="tasks")
    op.drop_table("tasks")
