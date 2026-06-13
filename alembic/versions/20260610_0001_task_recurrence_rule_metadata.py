"""Add task recurrence rule metadata columns.

Revision ID: 20260610_0001
Revises: 20260606_0001
Create Date: 2026-06-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260610_0001"
down_revision = "20260606_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            ALTER TABLE task_recurrence_rules
            ADD COLUMN IF NOT EXISTS trigger_status VARCHAR(32) DEFAULT 'closed',
            ADD COLUMN IF NOT EXISTS create_new BOOLEAN DEFAULT false,
            ADD COLUMN IF NOT EXISTS recur_forever BOOLEAN DEFAULT true,
            ADD COLUMN IF NOT EXISTS reset_status_to VARCHAR(32) DEFAULT 'open',
            ADD COLUMN IF NOT EXISTS end_count INTEGER,
            ADD COLUMN IF NOT EXISTS end_date TIMESTAMP NULL,
            ADD COLUMN IF NOT EXISTS skip_weekend BOOLEAN NOT NULL DEFAULT false,
            ADD COLUMN IF NOT EXISTS skip_holiday BOOLEAN NOT NULL DEFAULT false
            """
        )
    )


def downgrade() -> None:
    for column in (
        "end_date",
        "end_count",
        "reset_status_to",
        "recur_forever",
        "create_new",
        "trigger_status",
    ):
        op.execute(
            sa.text(
                f"ALTER TABLE task_recurrence_rules DROP COLUMN IF EXISTS {column}"
            )
        )
