"""conversation_sessionsにRPステアリングスライダー設定カラム追加

Revision ID: 20260413_0009
Revises: 20260413_0008
Create Date: 2026-04-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260413_0009"
down_revision = "20260413_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_sessions",
        sa.Column("rp_settings", sa.JSON(), server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("conversation_sessions", "rp_settings")
