"""charactersテーブルにRP画像自動生成設定カラム追加

Revision ID: 20260413_0008
Revises: 20260413_0007
Create Date: 2026-04-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260413_0008"
down_revision = "20260413_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column("auto_image_gen", sa.Boolean(), server_default=sa.text("false")),
    )
    op.add_column(
        "characters",
        sa.Column("image_gen_trigger", sa.String(20), server_default="scene_change"),
    )
    op.add_column(
        "characters",
        sa.Column("image_gen_interval", sa.Integer(), server_default="5"),
    )


def downgrade() -> None:
    op.drop_column("characters", "image_gen_interval")
    op.drop_column("characters", "image_gen_trigger")
    op.drop_column("characters", "auto_image_gen")
