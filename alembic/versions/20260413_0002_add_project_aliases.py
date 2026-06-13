"""projectsテーブルにaliasesカラムを追加

Revision ID: 20260413_0002
Revises: 20260413_0001
Create Date: 2026-04-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260413_0002"
down_revision = "20260413_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("aliases", sa.JSON(), nullable=True, server_default="[]"))


def downgrade() -> None:
    op.drop_column("projects", "aliases")
