"""Create durable agent run tables.

Revision ID: 20260619_0001
Revises: 20260612_0003
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260619_0001"
down_revision = "20260612_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("root_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("parent_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("trigger_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", sa.String(length=200), nullable=True),
        sa.Column("run_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("generation_profile", sa.String(length=64), nullable=True),
        sa.Column("provider", sa.String(length=80), nullable=True),
        sa.Column("model", sa.String(length=160), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("validation", sa.JSON(), nullable=True),
        sa.Column("run_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("last_event_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["root_run_id"], ["agent_runs.id"]),
        sa.ForeignKeyConstraint(["parent_run_id"], ["agent_runs.id"]),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["conversation_sessions.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_message_id"],
            ["conversation_messages.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_agent_runs_root_run_id", "agent_runs", ["root_run_id"])
    op.create_index("ix_agent_runs_parent_run_id", "agent_runs", ["parent_run_id"])
    op.create_index("ix_agent_runs_session_id", "agent_runs", ["session_id"])
    op.create_index(
        "ix_agent_runs_trigger_message_id",
        "agent_runs",
        ["trigger_message_id"],
    )
    op.create_index("ix_agent_runs_project_id", "agent_runs", ["project_id"])
    op.create_index("ix_agent_runs_user_id", "agent_runs", ["user_id"])
    op.create_index("ix_agent_runs_run_type", "agent_runs", ["run_type"])
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])
    op.create_index("ix_agent_runs_created_at", "agent_runs", ["created_at"])
    op.create_index(
        "ix_agent_runs_session_status_created",
        "agent_runs",
        ["session_id", "status", "created_at"],
    )
    op.create_index(
        "ix_agent_runs_project_status_created",
        "agent_runs",
        ["project_id", "status", "created_at"],
    )

    op.create_table(
        "agent_run_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_agent_run_events_sequence"),
    )
    op.create_index("ix_agent_run_events_run_id", "agent_run_events", ["run_id"])
    op.create_index(
        "ix_agent_run_events_event_type",
        "agent_run_events",
        ["event_type"],
    )
    op.create_index(
        "ix_agent_run_events_created_at",
        "agent_run_events",
        ["created_at"],
    )
    op.create_index(
        "ix_agent_run_events_run_created",
        "agent_run_events",
        ["run_id", "created_at"],
    )

    op.create_table(
        "agent_run_tool_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("tool_name", sa.String(length=160), nullable=False),
        sa.Column("tool_call_id", sa.String(length=160), nullable=True),
        sa.Column("arguments", sa.JSON(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("mutation_confirmed", sa.Boolean(), nullable=False),
        sa.Column("result_metadata", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["agent_run_events.id"],
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_agent_run_tool_calls_run_id",
        "agent_run_tool_calls",
        ["run_id"],
    )
    op.create_index(
        "ix_agent_run_tool_calls_event_id",
        "agent_run_tool_calls",
        ["event_id"],
    )
    op.create_index(
        "ix_agent_run_tool_calls_tool_name",
        "agent_run_tool_calls",
        ["tool_name"],
    )
    op.create_index(
        "ix_agent_run_tool_calls_success",
        "agent_run_tool_calls",
        ["success"],
    )
    op.create_index(
        "ix_agent_run_tool_calls_mutation_confirmed",
        "agent_run_tool_calls",
        ["mutation_confirmed"],
    )
    op.create_index(
        "ix_agent_run_tool_calls_created_at",
        "agent_run_tool_calls",
        ["created_at"],
    )
    op.create_index(
        "ix_agent_run_tool_calls_run_tool",
        "agent_run_tool_calls",
        ["run_id", "tool_name"],
    )

    op.create_table(
        "agent_run_edges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("parent_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("child_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(length=160), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("edge_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["parent_run_id"],
            ["agent_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["child_run_id"],
            ["agent_runs.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "parent_run_id",
            "child_run_id",
            name="uq_agent_run_edges_parent_child",
        ),
    )
    op.create_index(
        "ix_agent_run_edges_parent_run_id",
        "agent_run_edges",
        ["parent_run_id"],
    )
    op.create_index(
        "ix_agent_run_edges_child_run_id",
        "agent_run_edges",
        ["child_run_id"],
    )
    op.create_index("ix_agent_run_edges_status", "agent_run_edges", ["status"])
    op.create_index("ix_agent_run_edges_created_at", "agent_run_edges", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_run_edges_created_at", table_name="agent_run_edges")
    op.drop_index("ix_agent_run_edges_status", table_name="agent_run_edges")
    op.drop_index("ix_agent_run_edges_child_run_id", table_name="agent_run_edges")
    op.drop_index("ix_agent_run_edges_parent_run_id", table_name="agent_run_edges")
    op.drop_table("agent_run_edges")

    op.drop_index("ix_agent_run_tool_calls_run_tool", table_name="agent_run_tool_calls")
    op.drop_index(
        "ix_agent_run_tool_calls_created_at",
        table_name="agent_run_tool_calls",
    )
    op.drop_index(
        "ix_agent_run_tool_calls_mutation_confirmed",
        table_name="agent_run_tool_calls",
    )
    op.drop_index("ix_agent_run_tool_calls_success", table_name="agent_run_tool_calls")
    op.drop_index(
        "ix_agent_run_tool_calls_tool_name",
        table_name="agent_run_tool_calls",
    )
    op.drop_index("ix_agent_run_tool_calls_event_id", table_name="agent_run_tool_calls")
    op.drop_index("ix_agent_run_tool_calls_run_id", table_name="agent_run_tool_calls")
    op.drop_table("agent_run_tool_calls")

    op.drop_index("ix_agent_run_events_run_created", table_name="agent_run_events")
    op.drop_index("ix_agent_run_events_created_at", table_name="agent_run_events")
    op.drop_index("ix_agent_run_events_event_type", table_name="agent_run_events")
    op.drop_index("ix_agent_run_events_run_id", table_name="agent_run_events")
    op.drop_table("agent_run_events")

    op.drop_index("ix_agent_runs_project_status_created", table_name="agent_runs")
    op.drop_index("ix_agent_runs_session_status_created", table_name="agent_runs")
    op.drop_index("ix_agent_runs_created_at", table_name="agent_runs")
    op.drop_index("ix_agent_runs_status", table_name="agent_runs")
    op.drop_index("ix_agent_runs_run_type", table_name="agent_runs")
    op.drop_index("ix_agent_runs_user_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_project_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_trigger_message_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_session_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_parent_run_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_root_run_id", table_name="agent_runs")
    op.drop_table("agent_runs")
