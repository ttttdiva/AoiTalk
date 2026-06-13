"""Add context builder memory tables.

Revision ID: 20260508_0029
Revises: 20260507_0028
Create Date: 2026-05-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260508_0029"
down_revision = "20260507_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_context_packs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("summary_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("goals", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column(
            "constraints",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column(
            "current_status",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "active_task_snapshot",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column(
            "decisions",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column(
            "open_questions",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column("manual_notes", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "generated_from",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", name="uq_project_context_packs_project_id"),
    )
    op.create_index(
        "ix_project_context_packs_project_id",
        "project_context_packs",
        ["project_id"],
        unique=True,
    )

    op.create_table(
        "context_memories",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.String(length=100), nullable=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=120), nullable=True),
        sa.Column("memory_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "structured_data",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("source_type", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("importance", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["conversation_sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_context_memories_user_id", "context_memories", ["user_id"])
    op.create_index("ix_context_memories_project_id", "context_memories", ["project_id"])
    op.create_index("ix_context_memories_task_id", "context_memories", ["task_id"])
    op.create_index("ix_context_memories_session_id", "context_memories", ["session_id"])
    op.create_index("ix_context_memories_scope_type", "context_memories", ["scope_type"])
    op.create_index("ix_context_memories_scope_id", "context_memories", ["scope_id"])
    op.create_index("ix_context_memories_memory_type", "context_memories", ["memory_type"])
    op.create_index("ix_context_memories_status", "context_memories", ["status"])
    op.create_index(
        "ix_context_memories_user_status",
        "context_memories",
        ["user_id", "status"],
    )
    op.create_index(
        "ix_context_memories_project_status",
        "context_memories",
        ["project_id", "status"],
    )
    op.create_index(
        "ix_context_memories_task_status",
        "context_memories",
        ["task_id", "status"],
    )
    op.create_index(
        "ix_context_memories_session_status",
        "context_memories",
        ["session_id", "status"],
    )
    op.create_index(
        "ix_context_memories_scope",
        "context_memories",
        ["scope_type", "scope_id"],
    )
    op.create_index(
        "ix_context_memories_pinned_importance",
        "context_memories",
        ["is_pinned", "importance"],
    )


def downgrade() -> None:
    op.drop_index("ix_context_memories_pinned_importance", table_name="context_memories")
    op.drop_index("ix_context_memories_scope", table_name="context_memories")
    op.drop_index("ix_context_memories_session_status", table_name="context_memories")
    op.drop_index("ix_context_memories_task_status", table_name="context_memories")
    op.drop_index("ix_context_memories_project_status", table_name="context_memories")
    op.drop_index("ix_context_memories_user_status", table_name="context_memories")
    op.drop_index("ix_context_memories_status", table_name="context_memories")
    op.drop_index("ix_context_memories_memory_type", table_name="context_memories")
    op.drop_index("ix_context_memories_scope_id", table_name="context_memories")
    op.drop_index("ix_context_memories_scope_type", table_name="context_memories")
    op.drop_index("ix_context_memories_session_id", table_name="context_memories")
    op.drop_index("ix_context_memories_task_id", table_name="context_memories")
    op.drop_index("ix_context_memories_project_id", table_name="context_memories")
    op.drop_index("ix_context_memories_user_id", table_name="context_memories")
    op.drop_table("context_memories")

    op.drop_index("ix_project_context_packs_project_id", table_name="project_context_packs")
    op.drop_table("project_context_packs")
