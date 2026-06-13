"""Add sort_order to tasks table.

Revision ID: 20260320_0003
Revises: 20260320_0002
Create Date: 2026-03-20 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260320_0003"
down_revision = "20260320_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("sort_order", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "sort_order")
