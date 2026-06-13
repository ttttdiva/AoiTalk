"""ユーザーペルソナテーブルを作成

Revision ID: 20260413_0005
Revises: 20260413_0004
Create Date: 2026-04-13
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "20260413_0005"
down_revision = "20260413_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_personas",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.String(100), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), server_default=""),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()")),
    )
    op.create_index("ix_user_personas_user_id", "user_personas", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_personas_user_id", "user_personas")
    op.drop_table("user_personas")
