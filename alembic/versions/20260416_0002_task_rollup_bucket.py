"""Add rollup_bucket column to tasks table.

Revision ID: 20260416_0002
Revises: 20260416_0001
Create Date: 2026-04-16
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260416_0002"
down_revision = "20260416_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "rollup_bucket",
            sa.String(16),
            nullable=False,
            server_default="active",
        ),
    )
    op.create_index("ix_tasks_rollup_bucket", "tasks", ["rollup_bucket"])


def downgrade() -> None:
    op.drop_index("ix_tasks_rollup_bucket", table_name="tasks")
    op.drop_column("tasks", "rollup_bucket")
