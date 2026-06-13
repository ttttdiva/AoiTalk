"""Normalize task sort order.

Revision ID: 20260518_0039
Revises: 20260515_0038
Create Date: 2026-05-18 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260518_0039"
down_revision = "20260515_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            WITH ranked AS (
              SELECT
                id,
                row_number() OVER (
                  PARTITION BY project_id, parent_task_id
                  ORDER BY sort_order ASC NULLS LAST, created_at ASC, id ASC
                ) - 1 AS next_sort_order
              FROM tasks
            )
            UPDATE tasks AS t
            SET sort_order = ranked.next_sort_order::float
            FROM ranked
            WHERE t.id = ranked.id
            """
        )
    )
    op.alter_column(
        "tasks",
        "sort_order",
        existing_type=sa.Float(),
        nullable=False,
        server_default="0",
    )


def downgrade() -> None:
    op.alter_column(
        "tasks",
        "sort_order",
        existing_type=sa.Float(),
        nullable=True,
        server_default=None,
    )
