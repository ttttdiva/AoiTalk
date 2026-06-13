"""Add remote AoiTalk server connection profiles.

Revision ID: 20260612_0002
Revises: 20260612_0001
Create Date: 2026-06-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260612_0002"
down_revision = "20260612_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "remote_server_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("auth_token", sa.Text(), nullable=True),
        sa.Column("display_color", sa.String(length=32), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("last_status", sa.String(length=32), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("last_capabilities", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "user_id", "base_url", name="uq_remote_server_profiles_user_base_url"
        ),
    )
    op.create_index(
        "ix_remote_server_profiles_user_id",
        "remote_server_profiles",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_remote_server_profiles_user_id",
        table_name="remote_server_profiles",
    )
    op.drop_table("remote_server_profiles")
