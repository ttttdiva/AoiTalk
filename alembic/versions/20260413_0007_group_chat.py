"""conversation_sessionsにグループチャットカラム追加

Revision ID: 20260413_0007
Revises: 20260413_0006
Create Date: 2026-04-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260413_0007"
down_revision = "20260413_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_sessions",
        sa.Column("is_group_chat", sa.Boolean(), server_default=sa.text("false")),
    )
    op.add_column(
        "conversation_sessions",
        sa.Column("group_character_names", sa.JSON(), server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("conversation_sessions", "group_character_names")
    op.drop_column("conversation_sessions", "is_group_chat")
