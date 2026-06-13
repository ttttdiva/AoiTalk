"""TRPGマルチプレイヤー: 参加者テーブル・ログテーブル・play_sessions拡張

Revision ID: 20260415_0001
Revises: 20260414_0010
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "20260415_0001"
down_revision = "20260414_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── A) scenario_play_sessions 拡張 ──
    op.add_column(
        "scenario_play_sessions",
        sa.Column("room_code", sa.String(12), nullable=True),
    )
    op.add_column(
        "scenario_play_sessions",
        sa.Column("room_title", sa.String(200), server_default=""),
    )
    op.add_column(
        "scenario_play_sessions",
        sa.Column("host_user_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "scenario_play_sessions",
        sa.Column("max_players", sa.Integer(), server_default="4"),
    )
    op.add_column(
        "scenario_play_sessions",
        sa.Column("gm_mode", sa.String(20), server_default="ai"),
    )
    op.add_column(
        "scenario_play_sessions",
        sa.Column("gm_user_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "scenario_play_sessions",
        sa.Column(
            "is_multiplayer",
            sa.Boolean(),
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "scenario_play_sessions",
        sa.Column(
            "is_public",
            sa.Boolean(),
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "scenario_play_sessions",
        sa.Column("turn_order", sa.JSON(), server_default="[]"),
    )
    op.add_column(
        "scenario_play_sessions",
        sa.Column(
            "current_turn_participant_id",
            UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "scenario_play_sessions",
        sa.Column("shared_state", sa.JSON(), server_default="{}"),
    )
    op.add_column(
        "scenario_play_sessions",
        sa.Column("last_gm_activity_at", sa.DateTime(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_scenario_play_sessions_room_code",
        "scenario_play_sessions",
        ["room_code"],
    )
    op.create_index(
        "ix_scenario_play_sessions_room_code",
        "scenario_play_sessions",
        ["room_code"],
    )
    op.create_index(
        "ix_scenario_play_sessions_host_user_id",
        "scenario_play_sessions",
        ["host_user_id"],
    )

    # ── B) scenario_participants ──
    op.create_table(
        "scenario_participants",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "play_session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("scenario_play_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "character_id",
            UUID(as_uuid=True),
            sa.ForeignKey("characters.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("role", sa.String(20), server_default="player"),
        sa.Column("participant_kind", sa.String(20), server_default="human"),
        sa.Column("avatar_url", sa.String(500), server_default=""),
        sa.Column("color", sa.String(20), server_default="#60a5fa"),
        sa.Column("seat_index", sa.Integer(), server_default="0"),
        sa.Column("pc_state", sa.JSON(), server_default="{}"),
        sa.Column(
            "is_active_participant",
            sa.Boolean(),
            server_default=sa.text("true"),
        ),
        sa.Column(
            "is_connected",
            sa.Boolean(),
            server_default=sa.text("false"),
        ),
        sa.Column(
            "joined_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_scenario_participants_play_session_id",
        "scenario_participants",
        ["play_session_id"],
    )
    op.create_index(
        "ix_scenario_participants_user_id",
        "scenario_participants",
        ["user_id"],
    )

    # current_turn_participant_id の FK は scenario_participants 作成後に貼る
    op.create_foreign_key(
        "fk_scenario_play_sessions_current_turn_participant_id",
        "scenario_play_sessions",
        "scenario_participants",
        ["current_turn_participant_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ── C) scenario_play_logs ──
    op.create_table(
        "scenario_play_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "play_session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("scenario_play_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "participant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("scenario_participants.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("log_type", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), server_default=""),
        sa.Column("log_metadata", sa.JSON(), server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_scenario_play_logs_play_session_id",
        "scenario_play_logs",
        ["play_session_id"],
    )
    op.create_index(
        "ix_scenario_play_logs_created_at",
        "scenario_play_logs",
        ["created_at"],
    )


def downgrade() -> None:
    # play_logs
    op.drop_index("ix_scenario_play_logs_created_at", table_name="scenario_play_logs")
    op.drop_index(
        "ix_scenario_play_logs_play_session_id", table_name="scenario_play_logs"
    )
    op.drop_table("scenario_play_logs")

    # play_sessions から参加者への FK を先に外す
    op.drop_constraint(
        "fk_scenario_play_sessions_current_turn_participant_id",
        "scenario_play_sessions",
        type_="foreignkey",
    )

    # participants
    op.drop_index(
        "ix_scenario_participants_user_id", table_name="scenario_participants"
    )
    op.drop_index(
        "ix_scenario_participants_play_session_id",
        table_name="scenario_participants",
    )
    op.drop_table("scenario_participants")

    # play_sessions の追加カラム
    op.drop_index(
        "ix_scenario_play_sessions_host_user_id",
        table_name="scenario_play_sessions",
    )
    op.drop_index(
        "ix_scenario_play_sessions_room_code",
        table_name="scenario_play_sessions",
    )
    op.drop_constraint(
        "uq_scenario_play_sessions_room_code",
        "scenario_play_sessions",
        type_="unique",
    )
    op.drop_column("scenario_play_sessions", "last_gm_activity_at")
    op.drop_column("scenario_play_sessions", "shared_state")
    op.drop_column("scenario_play_sessions", "current_turn_participant_id")
    op.drop_column("scenario_play_sessions", "turn_order")
    op.drop_column("scenario_play_sessions", "is_public")
    op.drop_column("scenario_play_sessions", "is_multiplayer")
    op.drop_column("scenario_play_sessions", "gm_user_id")
    op.drop_column("scenario_play_sessions", "gm_mode")
    op.drop_column("scenario_play_sessions", "max_players")
    op.drop_column("scenario_play_sessions", "host_user_id")
    op.drop_column("scenario_play_sessions", "room_title")
    op.drop_column("scenario_play_sessions", "room_code")
