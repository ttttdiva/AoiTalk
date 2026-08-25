"""Add skip_mode to task_recurrence_rules.

土日・祝日に当たった回を「翌営業日へずらす」「前営業日へずらす」「その回は実施しない」の
いずれで扱うかを保持する。既存行は従来挙動（翌営業日へずらす）を維持する。

Revision ID: 20260803_0004
Revises: 20260803_0003
Create Date: 2026-08-03 12:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260803_0004"
down_revision = "20260803_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_recurrence_rules",
        sa.Column(
            "skip_mode",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'shift_forward'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("task_recurrence_rules", "skip_mode")
