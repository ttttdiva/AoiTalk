"""custom_agentsテーブルを削除（キャラクターに統合済み）

Revision ID: 20260413_0001
Revises: 20260412_0002
Create Date: 2026-04-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260413_0001"
down_revision = "20260412_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_custom_agents_name", table_name="custom_agents", if_exists=True)
    op.drop_table("custom_agents", if_exists=True)


def downgrade() -> None:
    op.create_table(
        "custom_agents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("system_prompt", sa.Text, nullable=False),
        sa.Column("model", sa.String(100), server_default=""),
        sa.Column("allowed_tools", sa.JSON, server_default="[]"),
        sa.Column("is_enabled", sa.Boolean, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime),
        sa.Column("updated_at", sa.DateTime),
    )
    op.create_index("ix_custom_agents_name", "custom_agents", ["name"], unique=True)
