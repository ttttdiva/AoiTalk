"""会話履歴の既読時刻を保存する。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260802_0003"
down_revision = "20260802_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_sessions",
        sa.Column("last_read_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_conversation_sessions_last_read_at",
        "conversation_sessions",
        ["last_read_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_sessions_last_read_at",
        table_name="conversation_sessions",
    )
    op.drop_column("conversation_sessions", "last_read_at")
