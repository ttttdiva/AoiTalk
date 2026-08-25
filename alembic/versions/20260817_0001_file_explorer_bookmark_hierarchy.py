"""Add hierarchical file explorer bookmarks."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260817_0001"
down_revision = "20260814_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "file_explorer_bookmarks",
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "file_explorer_bookmarks",
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            server_default="bookmark",
        ),
    )
    op.create_foreign_key(
        "fk_file_explorer_bookmarks_parent_id",
        "file_explorer_bookmarks",
        "file_explorer_bookmarks",
        ["parent_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_file_explorer_bookmarks_user_parent_sort",
        "file_explorer_bookmarks",
        ["user_id", "parent_id", "sort_order"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_file_explorer_bookmarks_user_parent_sort",
        table_name="file_explorer_bookmarks",
    )
    op.drop_constraint(
        "fk_file_explorer_bookmarks_parent_id",
        "file_explorer_bookmarks",
        type_="foreignkey",
    )
    op.drop_column("file_explorer_bookmarks", "kind")
    op.drop_column("file_explorer_bookmarks", "parent_id")
