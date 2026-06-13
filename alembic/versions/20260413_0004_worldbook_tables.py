"""ワールドブック（ロアブック）テーブルを作成

Revision ID: 20260413_0004
Revises: 20260413_0003
Create Date: 2026-04-13
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "20260413_0004"
down_revision = "20260413_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ワールドブック本体
    op.create_table(
        "world_books",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), server_default=""),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()")),
    )

    # ワールドブックエントリ
    op.create_table(
        "world_book_entries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "world_book_id",
            UUID(as_uuid=True),
            sa.ForeignKey("world_books.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), server_default=""),
        sa.Column("keywords", sa.JSON(), server_default="[]"),
        sa.Column("secondary_keywords", sa.JSON(), server_default="[]"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("priority", sa.Integer(), server_default="0"),
        sa.Column("case_sensitive", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("constant", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("insertion_position", sa.String(20), server_default="before_scenario"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()")),
    )
    op.create_index("ix_world_book_entries_world_book_id", "world_book_entries", ["world_book_id"])

    # キャラクター↔ワールドブック多対多
    op.create_table(
        "character_world_books",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "character_id",
            UUID(as_uuid=True),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "world_book_id",
            UUID(as_uuid=True),
            sa.ForeignKey("world_books.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_character_world_books",
        "character_world_books",
        ["character_id", "world_book_id"],
    )
    op.create_index("ix_character_world_books_character_id", "character_world_books", ["character_id"])


def downgrade() -> None:
    op.drop_index("ix_character_world_books_character_id", "character_world_books")
    op.drop_constraint("uq_character_world_books", "character_world_books")
    op.drop_table("character_world_books")
    op.drop_index("ix_world_book_entries_world_book_id", "world_book_entries")
    op.drop_table("world_book_entries")
    op.drop_table("world_books")
