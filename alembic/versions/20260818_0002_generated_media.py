"""Add generated_media table for durable image delivery."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260818_0002"
down_revision = "20260817_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generated_media",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("context_type", sa.String(length=32), nullable=False),
        sa.Column("context_id", sa.String(), nullable=False),
        sa.Column("bind_type", sa.String(length=32), nullable=True),
        sa.Column("bind_id", sa.String(), nullable=True),
        sa.Column("storage_key", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("prompt_meta", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index(
        "ix_generated_media_owner_user_id",
        "generated_media",
        ["owner_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_generated_media_context_id",
        "generated_media",
        ["context_id"],
        unique=False,
    )
    op.create_index(
        "ix_generated_media_bind_id",
        "generated_media",
        ["bind_id"],
        unique=False,
    )
    op.create_index(
        "ix_generated_media_context",
        "generated_media",
        ["context_type", "context_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_generated_media_context", table_name="generated_media")
    op.drop_index("ix_generated_media_bind_id", table_name="generated_media")
    op.drop_index("ix_generated_media_context_id", table_name="generated_media")
    op.drop_index("ix_generated_media_owner_user_id", table_name="generated_media")
    op.drop_table("generated_media")
