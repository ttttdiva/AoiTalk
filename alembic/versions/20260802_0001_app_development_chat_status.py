"""App開発チャットの進行状態を永続化する。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260802_0001"
down_revision = "20260801_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_sessions",
        sa.Column("development_status", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "ix_conversation_sessions_development_status",
        "conversation_sessions",
        ["development_status"],
    )
    op.create_check_constraint(
        "ck_conversation_sessions_development_status",
        "conversation_sessions",
        "development_status IS NULL OR development_status IN ('working','waiting_for_user','completed')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_conversation_sessions_development_status",
        "conversation_sessions",
        type_="check",
    )
    op.drop_index(
        "ix_conversation_sessions_development_status",
        table_name="conversation_sessions",
    )
    op.drop_column("conversation_sessions", "development_status")
