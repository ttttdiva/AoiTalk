"""TRPG Play 実行系テーブル（sessions / participants / events / whispers）。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260818_0004"
down_revision = "20260818_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trpg_play_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("host_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("gm_mode", sa.String(length=16), nullable=False, server_default="human"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="lobby"),
        sa.Column("invite_code", sa.String(length=32), nullable=True),
        sa.Column(
            "snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["work_id"], ["story_works.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["host_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invite_code"),
    )
    op.create_index(
        "ix_trpg_play_sessions_work_id",
        "trpg_play_sessions",
        ["work_id"],
        unique=False,
    )
    op.create_index(
        "ix_trpg_play_sessions_host_user_id",
        "trpg_play_sessions",
        ["host_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_trpg_play_sessions_host_status",
        "trpg_play_sessions",
        ["host_user_id", "status"],
        unique=False,
    )

    op.create_table(
        "trpg_play_participants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="player"),
        sa.Column("story_character_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_npc", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["trpg_play_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["story_character_id"], ["story_characters.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_trpg_play_participants_session_id",
        "trpg_play_participants",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        "ix_trpg_play_participants_user_id",
        "trpg_play_participants",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "uq_trpg_play_participants_session_user",
        "trpg_play_participants",
        ["session_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )

    op.create_table(
        "trpg_play_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_participant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["trpg_play_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["actor_participant_id"], ["trpg_play_participants.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_trpg_play_events_session_id",
        "trpg_play_events",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        "ix_trpg_play_events_session_created",
        "trpg_play_events",
        ["session_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "trpg_play_whispers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sender_participant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["trpg_play_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["sender_participant_id"], ["trpg_play_participants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_trpg_play_whispers_session_id",
        "trpg_play_whispers",
        ["session_id"],
        unique=False,
    )

    op.create_table(
        "trpg_play_whisper_recipients",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("whisper_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("participant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["whisper_id"], ["trpg_play_whispers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["trpg_play_participants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "whisper_id",
            "participant_id",
            name="uq_trpg_play_whisper_recipient",
        ),
    )
    op.create_index(
        "ix_trpg_play_whisper_recipients_whisper_id",
        "trpg_play_whisper_recipients",
        ["whisper_id"],
        unique=False,
    )
    op.create_index(
        "ix_trpg_play_whisper_recipients_participant_id",
        "trpg_play_whisper_recipients",
        ["participant_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_trpg_play_whisper_recipients_participant_id", table_name="trpg_play_whisper_recipients")
    op.drop_index("ix_trpg_play_whisper_recipients_whisper_id", table_name="trpg_play_whisper_recipients")
    op.drop_table("trpg_play_whisper_recipients")
    op.drop_index("ix_trpg_play_whispers_session_id", table_name="trpg_play_whispers")
    op.drop_table("trpg_play_whispers")
    op.drop_index("ix_trpg_play_events_session_created", table_name="trpg_play_events")
    op.drop_index("ix_trpg_play_events_session_id", table_name="trpg_play_events")
    op.drop_table("trpg_play_events")
    op.drop_index("uq_trpg_play_participants_session_user", table_name="trpg_play_participants")
    op.drop_index("ix_trpg_play_participants_user_id", table_name="trpg_play_participants")
    op.drop_index("ix_trpg_play_participants_session_id", table_name="trpg_play_participants")
    op.drop_table("trpg_play_participants")
    op.drop_index("ix_trpg_play_sessions_host_status", table_name="trpg_play_sessions")
    op.drop_index("ix_trpg_play_sessions_host_user_id", table_name="trpg_play_sessions")
    op.drop_index("ix_trpg_play_sessions_work_id", table_name="trpg_play_sessions")
    op.drop_table("trpg_play_sessions")
