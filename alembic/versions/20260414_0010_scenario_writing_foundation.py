"""シナリオ執筆基盤: エピソード・Canon・執筆セッション・既存テーブル拡張

Revision ID: 20260414_0010
Revises: 20260413_0009
Create Date: 2026-04-14
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "20260414_0010"
down_revision = "20260413_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── A) scenario_episodes ──
    op.create_table(
        "scenario_episodes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scenario_id",
            UUID(as_uuid=True),
            sa.ForeignKey("scenarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("synopsis_sentence", sa.Text(), server_default=""),
        sa.Column("synopsis_paragraph", sa.Text(), server_default=""),
        sa.Column("synopsis_full", sa.Text(), server_default=""),
        sa.Column("beat_sheet", sa.JSON(), server_default="[]"),
        sa.Column("status", sa.String(20), server_default="draft"),
        sa.Column("sort_order", sa.Integer(), server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_scenario_episodes_scenario_id",
        "scenario_episodes",
        ["scenario_id"],
    )

    # ── B) scenario_canon_entries ──
    op.create_table(
        "scenario_canon_entries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scenario_id",
            UUID(as_uuid=True),
            sa.ForeignKey("scenarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("fact", sa.Text(), nullable=False),
        sa.Column(
            "source_scene_id",
            UUID(as_uuid=True),
            sa.ForeignKey("scenario_scenes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_scenario_canon_entries_scenario_id",
        "scenario_canon_entries",
        ["scenario_id"],
    )

    # ── C) scenario_writing_sessions ──
    op.create_table(
        "scenario_writing_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scenario_id",
            UUID(as_uuid=True),
            sa.ForeignKey("scenarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversation_sessions.id"),
            nullable=True,
        ),
        sa.Column(
            "target_episode_id",
            UUID(as_uuid=True),
            sa.ForeignKey("scenario_episodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "target_scene_id",
            UUID(as_uuid=True),
            sa.ForeignKey("scenario_scenes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("writing_prompt", sa.Text(), server_default=""),
        sa.Column("status", sa.String(20), server_default="in_progress"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_scenario_writing_sessions_scenario_id",
        "scenario_writing_sessions",
        ["scenario_id"],
    )

    # ── D) scenarios テーブル拡張: voice_* カラム ──
    op.add_column("scenarios", sa.Column("voice_tone", sa.Text(), server_default=""))
    op.add_column(
        "scenarios", sa.Column("voice_tense_rules", sa.Text(), server_default="")
    )
    op.add_column(
        "scenarios",
        sa.Column("voice_vocabulary_register", sa.Text(), server_default=""),
    )
    op.add_column(
        "scenarios",
        sa.Column("voice_banned_expressions", sa.JSON(), server_default="[]"),
    )
    op.add_column(
        "scenarios", sa.Column("voice_example_passages", sa.Text(), server_default="")
    )

    # ── E) scenario_scenes テーブル拡張 ──
    op.add_column(
        "scenario_scenes",
        sa.Column(
            "episode_id",
            UUID(as_uuid=True),
            sa.ForeignKey("scenario_episodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("scenario_scenes", sa.Column("content", sa.Text(), server_default=""))
    op.add_column(
        "scenario_scenes", sa.Column("content_versions", sa.JSON(), server_default="[]")
    )
    op.add_column(
        "scenario_scenes", sa.Column("word_count", sa.Integer(), server_default="0")
    )
    op.add_column(
        "scenario_scenes", sa.Column("status", sa.String(20), server_default="draft")
    )
    op.add_column(
        "scenario_scenes", sa.Column("state_snapshot", sa.JSON(), server_default="{}")
    )

    # ── F) scenario_characters テーブル拡張 ──
    op.add_column(
        "scenario_characters", sa.Column("backstory", sa.Text(), server_default="")
    )
    op.add_column(
        "scenario_characters", sa.Column("psychology", sa.Text(), server_default="")
    )
    op.add_column(
        "scenario_characters",
        sa.Column("speech_patterns", sa.Text(), server_default=""),
    )
    op.add_column(
        "scenario_characters",
        sa.Column("relationships", sa.JSON(), server_default="[]"),
    )
    op.add_column(
        "scenario_characters", sa.Column("character_arc", sa.Text(), server_default="")
    )
    op.add_column(
        "scenario_characters", sa.Column("importance", sa.Integer(), server_default="0")
    )
    op.add_column(
        "scenario_characters",
        sa.Column("example_dialogues", sa.Text(), server_default=""),
    )


def downgrade() -> None:
    # scenario_characters 拡張カラム削除
    op.drop_column("scenario_characters", "example_dialogues")
    op.drop_column("scenario_characters", "importance")
    op.drop_column("scenario_characters", "character_arc")
    op.drop_column("scenario_characters", "relationships")
    op.drop_column("scenario_characters", "speech_patterns")
    op.drop_column("scenario_characters", "psychology")
    op.drop_column("scenario_characters", "backstory")

    # scenario_scenes 拡張カラム削除
    op.drop_column("scenario_scenes", "state_snapshot")
    op.drop_column("scenario_scenes", "status")
    op.drop_column("scenario_scenes", "word_count")
    op.drop_column("scenario_scenes", "content_versions")
    op.drop_column("scenario_scenes", "content")
    op.drop_column("scenario_scenes", "episode_id")

    # scenarios 拡張カラム削除
    op.drop_column("scenarios", "voice_example_passages")
    op.drop_column("scenarios", "voice_banned_expressions")
    op.drop_column("scenarios", "voice_vocabulary_register")
    op.drop_column("scenarios", "voice_tense_rules")
    op.drop_column("scenarios", "voice_tone")

    # 新テーブル削除
    op.drop_index(
        "ix_scenario_writing_sessions_scenario_id",
        table_name="scenario_writing_sessions",
    )
    op.drop_table("scenario_writing_sessions")
    op.drop_index(
        "ix_scenario_canon_entries_scenario_id", table_name="scenario_canon_entries"
    )
    op.drop_table("scenario_canon_entries")
    op.drop_index("ix_scenario_episodes_scenario_id", table_name="scenario_episodes")
    op.drop_table("scenario_episodes")
