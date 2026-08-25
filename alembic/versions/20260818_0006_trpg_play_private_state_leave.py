"""TRPG Play private state と participant leave 用カラム。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260818_0006"
down_revision = "20260818_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trpg_play_participants",
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_trpg_play_participants_session_active",
        "trpg_play_participants",
        ["session_id"],
        unique=False,
        postgresql_where=sa.text("left_at IS NULL"),
    )

    op.create_table(
        "trpg_play_private_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("participant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["trpg_play_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["trpg_play_participants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "participant_id",
            name="uq_trpg_play_private_states_session_participant",
        ),
    )
    op.create_index(
        "ix_trpg_play_private_states_session_id",
        "trpg_play_private_states",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        "ix_trpg_play_private_states_participant_id",
        "trpg_play_private_states",
        ["participant_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_trpg_play_private_states_participant_id", table_name="trpg_play_private_states")
    op.drop_index("ix_trpg_play_private_states_session_id", table_name="trpg_play_private_states")
    op.drop_table("trpg_play_private_states")
    op.drop_index("ix_trpg_play_participants_session_active", table_name="trpg_play_participants")
    op.drop_column("trpg_play_participants", "left_at")
