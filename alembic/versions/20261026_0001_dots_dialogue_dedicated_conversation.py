"""Bind the dedicated DOTS connection to one immutable Aoi conversation.

Revision ID: 20261026_0001
Revises: 20261024_0001
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "20261026_0001"
down_revision = "20261024_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dots_dialogue_bindings",
        sa.Column(
            "dedicated_conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversation_sessions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("dots_dialogue_bindings", "dedicated_conversation_id")
