"""Add Docs supertag inheritance.

Revision ID: 20260702_0002
Revises: 20260702_0001
Create Date: 2026-07-02
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260702_0002"
down_revision: Union[str, None] = "20260702_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _fk_exists(table_name: str, fk_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(fk["name"] == fk_name for fk in inspector.get_foreign_keys(table_name))


def upgrade() -> None:
    if not _column_exists("knowledge_supertags", "parent_supertag_id"):
        op.add_column(
            "knowledge_supertags",
            sa.Column("parent_supertag_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
    if not _index_exists("knowledge_supertags", "ix_knowledge_supertags_parent"):
        op.create_index(
            "ix_knowledge_supertags_parent",
            "knowledge_supertags",
            ["parent_supertag_id"],
        )
    if not _fk_exists("knowledge_supertags", "fk_knowledge_supertags_parent"):
        op.create_foreign_key(
            "fk_knowledge_supertags_parent",
            "knowledge_supertags",
            "knowledge_supertags",
            ["parent_supertag_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    if _fk_exists("knowledge_supertags", "fk_knowledge_supertags_parent"):
        op.drop_constraint(
            "fk_knowledge_supertags_parent",
            "knowledge_supertags",
            type_="foreignkey",
        )
    if _index_exists("knowledge_supertags", "ix_knowledge_supertags_parent"):
        op.drop_index("ix_knowledge_supertags_parent", table_name="knowledge_supertags")
    if _column_exists("knowledge_supertags", "parent_supertag_id"):
        op.drop_column("knowledge_supertags", "parent_supertag_id")
