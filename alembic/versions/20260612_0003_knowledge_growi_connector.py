"""Add GROWI connector token column to knowledge_sources.

Revision ID: 20260612_0003
Revises: 20260612_0002
Create Date: 2026-06-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260612_0003"
down_revision = "20260612_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_sources",
        sa.Column("growi_api_token", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("knowledge_sources", "growi_api_token")
