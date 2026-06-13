"""shared conversation participants

Revision ID: 20260518_0040
Revises: 20260518_0039
Create Date: 2026-05-18
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260518_0040"
down_revision = "20260518_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_participants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("participant_type", sa.String(length=32), nullable=False),
        sa.Column("participant_id", sa.String(length=200), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("auto_respond", sa.Boolean(), nullable=True),
        sa.Column("participant_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["conversation_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "participant_type",
            "participant_id",
            name="uq_conversation_participant_identity",
        ),
    )
    op.create_index(
        "ix_conversation_participants_session_id",
        "conversation_participants",
        ["session_id"],
    )
    op.create_index(
        "ix_conversation_participants_lookup",
        "conversation_participants",
        ["participant_type", "participant_id"],
    )
    op.add_column(
        "conversation_messages",
        sa.Column("sender_type", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "conversation_messages",
        sa.Column("sender_id", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "conversation_messages",
        sa.Column("sender_display_name", sa.String(length=200), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversation_messages", "sender_display_name")
    op.drop_column("conversation_messages", "sender_id")
    op.drop_column("conversation_messages", "sender_type")
    op.drop_index(
        "ix_conversation_participants_lookup",
        table_name="conversation_participants",
    )
    op.drop_index(
        "ix_conversation_participants_session_id",
        table_name="conversation_participants",
    )
    op.drop_table("conversation_participants")
