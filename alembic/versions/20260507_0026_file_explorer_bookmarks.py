"""Add per-user file explorer bookmarks.

Revision ID: 20260507_0026
Revises: 20260506_0025
Create Date: 2026-05-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260507_0026"
down_revision = "20260506_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "file_explorer_bookmarks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("icon", sa.String(length=64), nullable=True),
        sa.Column("sort_order", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "path", name="unique_file_explorer_bookmark_path"),
    )
    op.create_index(
        "ix_file_explorer_bookmarks_user_id",
        "file_explorer_bookmarks",
        ["user_id"],
    )
    op.create_index(
        "ix_file_explorer_bookmarks_user_sort",
        "file_explorer_bookmarks",
        ["user_id", "sort_order"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_file_explorer_bookmarks_user_sort",
        table_name="file_explorer_bookmarks",
    )
    op.drop_index(
        "ix_file_explorer_bookmarks_user_id",
        table_name="file_explorer_bookmarks",
    )
    op.drop_table("file_explorer_bookmarks")
