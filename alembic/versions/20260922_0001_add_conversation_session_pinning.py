"""Add durable conversation pin state shared by Web and native clients."""

import sqlalchemy as sa
from alembic import op

revision = "20260922_0001"
down_revision = "20260921_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "conversation_sessions",
        sa.Column(
            "is_pinned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "conversation_sessions",
        sa.Column("pinned_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "conversation_sessions",
        sa.Column("pin_updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_conversation_sessions_is_pinned",
        "conversation_sessions",
        ["is_pinned"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_sessions_pin_updated_at",
        "conversation_sessions",
        ["pin_updated_at"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_conversation_sessions_pin_updated_at",
        table_name="conversation_sessions",
    )
    op.drop_index(
        "ix_conversation_sessions_is_pinned",
        table_name="conversation_sessions",
    )
    op.drop_column("conversation_sessions", "pin_updated_at")
    op.drop_column("conversation_sessions", "pinned_at")
    op.drop_column("conversation_sessions", "is_pinned")
