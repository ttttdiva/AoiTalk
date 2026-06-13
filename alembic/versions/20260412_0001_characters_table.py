"""統合キャラクターテーブル作成（キャラクターYAML + カスタムエージェント統合）

Revision ID: 20260412_0001
Revises: 20260320_0004
Create Date: 2026-04-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260412_0001"
down_revision = "20260320_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "characters",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("character_type", sa.String(20), nullable=False, server_default="assistant"),
        sa.Column("system_prompt", sa.Text, nullable=False, server_default=""),
        sa.Column("model", sa.String(100), nullable=False, server_default=""),
        sa.Column("allowed_tools", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        # 音声設定
        sa.Column("voice_engine", sa.String(50), nullable=False, server_default=""),
        sa.Column("voice_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("voice_id", sa.String(50), nullable=False, server_default=""),
        sa.Column("speaker_id", sa.Integer, nullable=True),
        sa.Column("voice_parameters", sa.JSON, nullable=False, server_default="{}"),
        # 性格設定
        sa.Column("greeting", sa.Text, nullable=False, server_default=""),
        sa.Column("invalid_content_reply", sa.Text, nullable=False, server_default=""),
        sa.Column("fallback_reply", sa.Text, nullable=False, server_default=""),
        sa.Column("goodbye_reply", sa.Text, nullable=False, server_default=""),
        sa.Column("recognition_aliases", sa.JSON, nullable=False, server_default="[]"),
        # 外見・画像生成
        sa.Column("appearance_tags", sa.Text, nullable=False, server_default=""),
        sa.Column("negative_tags", sa.Text, nullable=False, server_default=""),
        sa.Column("image_gen_engine", sa.String(20), nullable=False, server_default=""),
        sa.Column("comfyui_config", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("avatar_image_path", sa.String(500), nullable=False, server_default=""),
        # タイムスタンプ
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_characters_slug", "characters", ["slug"], unique=True)
    op.create_index("ix_characters_type", "characters", ["character_type"])


def downgrade() -> None:
    op.drop_index("ix_characters_type", table_name="characters")
    op.drop_index("ix_characters_slug", table_name="characters")
    op.drop_table("characters")
