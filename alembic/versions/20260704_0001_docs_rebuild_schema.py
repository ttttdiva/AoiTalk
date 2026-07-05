"""Rebuild Docs schema for encrypted body and saved views.

Revision ID: 20260704_0001
Revises: 20260702_0002
Create Date: 2026-07-04
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260704_0001"
down_revision: Union[str, None] = "20260702_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BASE_TYPES = (
    "note",
    "task",
    "decision",
    "risk",
    "question",
    "meeting",
    "person",
    "vendor",
    "device",
    "spec",
    "estimate",
    "evidence",
    "email",
    "url",
    "project",
    "project_information",
)


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _constraint_exists(table_name: str, constraint_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    checks = inspector.get_check_constraints(table_name)
    uniques = inspector.get_unique_constraints(table_name)
    fks = inspector.get_foreign_keys(table_name)
    return any(
        item.get("name") == constraint_name
        for item in [*checks, *uniques, *fks]
    )


def upgrade() -> None:
    if not _column_exists("knowledge_revisions", "source_refs_json"):
        op.add_column(
            "knowledge_revisions",
            sa.Column(
                "source_refs_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'::json"),
            ),
        )

    if not _column_exists("knowledge_supertags", "pinned_field_ids"):
        op.add_column(
            "knowledge_supertags",
            sa.Column(
                "pinned_field_ids",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'::json"),
            ),
        )

    if not _constraint_exists("knowledge_supertags", "ck_knowledge_supertags_base_type"):
        allowed = ", ".join(f"'{value}'" for value in BASE_TYPES)
        op.create_check_constraint(
            "ck_knowledge_supertags_base_type",
            "knowledge_supertags",
            f"base_type IN ({allowed})",
        )

    if _table_exists("knowledge_views"):
        op.drop_index("ix_knowledge_views_workspace", table_name="knowledge_views")
        op.drop_table("knowledge_views")

    if not _table_exists("knowledge_search_index"):
        op.create_table(
            "knowledge_search_index",
            sa.Column("node_id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("title_text", sa.Text(), nullable=False, server_default=""),
            sa.Column("body_text_plain", sa.Text(), nullable=False, server_default=""),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["node_id"], ["knowledge_nodes.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["workspace_id"], ["knowledge_workspaces.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        )
    if not _index_exists("knowledge_search_index", "ix_knowledge_search_index_workspace"):
        op.create_index(
            "ix_knowledge_search_index_workspace",
            "knowledge_search_index",
            ["workspace_id"],
        )
    if not _index_exists("knowledge_search_index", "ix_knowledge_search_index_project"):
        op.create_index(
            "ix_knowledge_search_index_project",
            "knowledge_search_index",
            ["project_id"],
        )

    if not _table_exists("knowledge_saved_views"):
        op.create_table(
            "knowledge_saved_views",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("supertag_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("layout", sa.String(40), nullable=False, server_default="table"),
            sa.Column("config_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
            sa.Column("sort_order", sa.Float(), nullable=False, server_default="0"),
            sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["workspace_id"], ["knowledge_workspaces.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["supertag_id"], ["knowledge_supertags.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        )
    if not _index_exists("knowledge_saved_views", "ix_knowledge_saved_views_workspace"):
        op.create_index(
            "ix_knowledge_saved_views_workspace",
            "knowledge_saved_views",
            ["workspace_id"],
        )
    if not _index_exists("knowledge_saved_views", "ix_knowledge_saved_views_supertag"):
        op.create_index(
            "ix_knowledge_saved_views_supertag",
            "knowledge_saved_views",
            ["supertag_id"],
        )


def downgrade() -> None:
    if _column_exists("knowledge_revisions", "source_refs_json"):
        op.drop_column("knowledge_revisions", "source_refs_json")

    if _index_exists("knowledge_saved_views", "ix_knowledge_saved_views_supertag"):
        op.drop_index("ix_knowledge_saved_views_supertag", table_name="knowledge_saved_views")
    if _index_exists("knowledge_saved_views", "ix_knowledge_saved_views_workspace"):
        op.drop_index("ix_knowledge_saved_views_workspace", table_name="knowledge_saved_views")
    if _table_exists("knowledge_saved_views"):
        op.drop_table("knowledge_saved_views")

    if _index_exists("knowledge_search_index", "ix_knowledge_search_index_project"):
        op.drop_index("ix_knowledge_search_index_project", table_name="knowledge_search_index")
    if _index_exists("knowledge_search_index", "ix_knowledge_search_index_workspace"):
        op.drop_index("ix_knowledge_search_index_workspace", table_name="knowledge_search_index")
    if _table_exists("knowledge_search_index"):
        op.drop_table("knowledge_search_index")

    if not _table_exists("knowledge_views"):
        op.create_table(
            "knowledge_views",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("view_type", sa.String(40), nullable=False, server_default="table"),
            sa.Column("query_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
            sa.Column("layout_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
            sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["workspace_id"], ["knowledge_workspaces.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        )
    if not _index_exists("knowledge_views", "ix_knowledge_views_workspace"):
        op.create_index("ix_knowledge_views_workspace", "knowledge_views", ["workspace_id"])

    if _constraint_exists("knowledge_supertags", "ck_knowledge_supertags_base_type"):
        op.drop_constraint(
            "ck_knowledge_supertags_base_type",
            "knowledge_supertags",
            type_="check",
        )
    if _column_exists("knowledge_supertags", "pinned_field_ids"):
        op.drop_column("knowledge_supertags", "pinned_field_ids")
