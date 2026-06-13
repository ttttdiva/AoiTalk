"""Add skip_weekend / skip_holiday flags to task_recurrence_rules.

Revision ID: 20260420_0004
Revises: 20260420_0003
Create Date: 2026-04-20 11:45:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260420_0004"
down_revision = "20260420_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_recurrence_rules",
        sa.Column(
            "skip_weekend",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "task_recurrence_rules",
        sa.Column(
            "skip_holiday",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("task_recurrence_rules", "skip_holiday")
    op.drop_column("task_recurrence_rules", "skip_weekend")
