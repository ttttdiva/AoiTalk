"""charactersテーブルにロールプレイ用フィールドを追加

Revision ID: 20260413_0003
Revises: 20260413_0002
Create Date: 2026-04-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260413_0003"
down_revision = "20260413_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("characters", sa.Column("description", sa.Text(), nullable=True, server_default=""))
    op.add_column("characters", sa.Column("personality_summary", sa.Text(), nullable=True, server_default=""))
    op.add_column("characters", sa.Column("first_message", sa.Text(), nullable=True, server_default=""))
    op.add_column("characters", sa.Column("alternate_greetings", sa.JSON(), nullable=True, server_default="[]"))
    op.add_column("characters", sa.Column("example_messages", sa.Text(), nullable=True, server_default=""))
    op.add_column("characters", sa.Column("scenario", sa.Text(), nullable=True, server_default=""))


def downgrade() -> None:
    op.drop_column("characters", "scenario")
    op.drop_column("characters", "example_messages")
    op.drop_column("characters", "alternate_greetings")
    op.drop_column("characters", "first_message")
    op.drop_column("characters", "personality_summary")
    op.drop_column("characters", "description")
