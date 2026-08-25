"""Persist user profile avatar file references.

Revision ID: 20260808_0012
Revises: 20260808_0011
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260808_0012"
down_revision = "20260808_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("avatar_path", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "avatar_path")
