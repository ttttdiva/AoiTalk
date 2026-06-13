"""シナリオ関連テーブル作成（TRPG / インタラクティブストーリー）

Revision ID: 20260412_0002
Revises: 20260412_0001
Create Date: 2026-04-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260412_0002"
down_revision = "20260412_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── scenarios テーブル ──
    op.create_table(
        "scenarios",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("genre", sa.String(50), nullable=False, server_default=""),
        sa.Column("perspective", sa.String(20), nullable=False, server_default="first_person"),
        sa.Column("setting", sa.Text, nullable=False, server_default=""),
        sa.Column("opening_text", sa.Text, nullable=False, server_default=""),
        sa.Column("gm_instructions", sa.Text, nullable=False, server_default=""),
        sa.Column("tags", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("cover_image_path", sa.String(500), nullable=False, server_default=""),
        sa.Column("is_published", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_by", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_scenarios_genre", "scenarios", ["genre"])
    op.create_index("ix_scenarios_is_published", "scenarios", ["is_published"])

    # ── scenario_characters テーブル ──
    op.create_table(
        "scenario_characters",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scenario_id", UUID(as_uuid=True),
            sa.ForeignKey("scenarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "character_id", UUID(as_uuid=True),
            sa.ForeignKey("characters.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("role", sa.String(20), nullable=False, server_default="npc"),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("personality_override", sa.Text, nullable=False, server_default=""),
        sa.Column("appearance_tags_override", sa.Text, nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("ix_scenario_characters_scenario_id", "scenario_characters", ["scenario_id"])

    # ── scenario_scenes テーブル ──
    op.create_table(
        "scenario_scenes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scenario_id", UUID(as_uuid=True),
            sa.ForeignKey("scenarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("scene_type", sa.String(20), nullable=False, server_default="normal"),
        sa.Column("gm_instructions", sa.Text, nullable=False, server_default=""),
        sa.Column("image_prompt", sa.Text, nullable=False, server_default=""),
        sa.Column("transitions", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("ix_scenario_scenes_scenario_id", "scenario_scenes", ["scenario_id"])

    # ── scenario_play_sessions テーブル ──
    op.create_table(
        "scenario_play_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scenario_id", UUID(as_uuid=True),
            sa.ForeignKey("scenarios.id"),
            nullable=False,
        ),
        sa.Column("conversation_session_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "current_scene_id", UUID(as_uuid=True),
            sa.ForeignKey("scenario_scenes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("player_state", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("perspective", sa.String(20), nullable=False, server_default="first_person"),
        sa.Column("status", sa.String(20), nullable=False, server_default="in_progress"),
        sa.Column("started_at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_scenario_play_sessions_scenario_id", "scenario_play_sessions", ["scenario_id"])
    op.create_index("ix_scenario_play_sessions_status", "scenario_play_sessions", ["status"])


def downgrade() -> None:
    op.drop_index("ix_scenario_play_sessions_status", table_name="scenario_play_sessions")
    op.drop_index("ix_scenario_play_sessions_scenario_id", table_name="scenario_play_sessions")
    op.drop_table("scenario_play_sessions")

    op.drop_index("ix_scenario_scenes_scenario_id", table_name="scenario_scenes")
    op.drop_table("scenario_scenes")

    op.drop_index("ix_scenario_characters_scenario_id", table_name="scenario_characters")
    op.drop_table("scenario_characters")

    op.drop_index("ix_scenarios_is_published", table_name="scenarios")
    op.drop_index("ix_scenarios_genre", table_name="scenarios")
    op.drop_table("scenarios")
