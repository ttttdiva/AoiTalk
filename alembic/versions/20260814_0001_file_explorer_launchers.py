"""Add durable per-user Files launchers."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260814_0001"
down_revision = "20260813_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "file_explorer_launchers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("icon", sa.String(length=64), nullable=True),
        sa.Column("sort_order", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "user_id", "path", name="unique_file_explorer_launcher_path"
        ),
    )
    op.create_index(
        "ix_file_explorer_launchers_user_id",
        "file_explorer_launchers",
        ["user_id"],
    )
    op.create_index(
        "ix_file_explorer_launchers_user_sort",
        "file_explorer_launchers",
        ["user_id", "sort_order"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_file_explorer_launchers_user_sort",
        table_name="file_explorer_launchers",
    )
    op.drop_index(
        "ix_file_explorer_launchers_user_id",
        table_name="file_explorer_launchers",
    )
    op.drop_table("file_explorer_launchers")

