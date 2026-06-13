"""Add long-lived API tokens for server-to-server access.

Revision ID: 20260612_0001
Revises: 20260610_0001
Create Date: 2026-06-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260612_0001"
down_revision = "20260610_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "long_lived_api_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=24), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column(
            "revoked", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_long_lived_api_tokens_token_hash"),
    )
    op.create_index(
        "ix_long_lived_api_tokens_user_id",
        "long_lived_api_tokens",
        ["user_id"],
    )
    op.create_index(
        "ix_long_lived_api_tokens_revoked",
        "long_lived_api_tokens",
        ["revoked"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_long_lived_api_tokens_revoked",
        table_name="long_lived_api_tokens",
    )
    op.drop_index(
        "ix_long_lived_api_tokens_user_id",
        table_name="long_lived_api_tokens",
    )
    op.drop_table("long_lived_api_tokens")
