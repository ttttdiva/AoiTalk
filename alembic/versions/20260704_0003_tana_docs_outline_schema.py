"""Add Tana-style outline Docs schema.

Revision ID: 20260704_0003
Revises: 20260704_0002
Create Date: 2026-07-04
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260704_0003"
down_revision: Union[str, None] = "20260704_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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
    uniques = inspector.get_unique_constraints(table_name)
    fks = inspector.get_foreign_keys(table_name)
    checks = inspector.get_check_constraints(table_name)
    return any(item.get("name") == constraint_name for item in [*uniques, *fks, *checks])


def upgrade() -> None:
    if not _column_exists("knowledge_nodes", "description"):
        op.add_column("knowledge_nodes", sa.Column("description", sa.Text(), nullable=False, server_default=""))
    if not _column_exists("knowledge_nodes", "display_props"):
        op.add_column(
            "knowledge_nodes",
            sa.Column("display_props", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        )
    if not _column_exists("knowledge_nodes", "query_json"):
        op.add_column("knowledge_nodes", sa.Column("query_json", sa.JSON(), nullable=True))
    if not _column_exists("knowledge_nodes", "view_json"):
        op.add_column(
            "knowledge_nodes",
            sa.Column("view_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        )
    if not _column_exists("knowledge_nodes", "day_date"):
        op.add_column("knowledge_nodes", sa.Column("day_date", sa.Date(), nullable=True))
    op.execute(
        "UPDATE knowledge_nodes SET node_type = 'node' "
        "WHERE node_type IS NULL OR node_type IN ('page', 'block', 'object')"
    )
    if not _index_exists("knowledge_nodes", "ix_knowledge_nodes_workspace_day"):
        op.create_index(
            "ix_knowledge_nodes_workspace_day",
            "knowledge_nodes",
            ["workspace_id", "day_date"],
            unique=False,
        )

    if not _column_exists("knowledge_supertags", "config_json"):
        op.add_column(
            "knowledge_supertags",
            sa.Column("config_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        )

    if not _table_exists("knowledge_supertag_fields"):
        op.create_table(
            "knowledge_supertag_fields",
            sa.Column("supertag_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("field_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("sort_order", sa.Float(), nullable=False, server_default="0"),
            sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("show_in_template", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("optional", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["supertag_id"], ["knowledge_supertags.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["field_id"], ["knowledge_fields.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("supertag_id", "field_id"),
        )
    op.execute(
        """
        INSERT INTO knowledge_supertag_fields
          (supertag_id, field_id, sort_order, required, show_in_template, optional)
        SELECT supertag_id, id, sort_order, required, true, false
        FROM knowledge_fields
        WHERE supertag_id IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )

    if not _table_exists("knowledge_node_placements"):
        op.create_table(
            "knowledge_node_placements",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("parent_node_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("sort_order", sa.Float(), nullable=False, server_default="0"),
            sa.Column("collapsed", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["node_id"], ["knowledge_nodes.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["parent_node_id"], ["knowledge_nodes.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        )
    if not _constraint_exists("knowledge_node_placements", "uq_knowledge_node_placement_parent"):
        op.create_unique_constraint(
            "uq_knowledge_node_placement_parent",
            "knowledge_node_placements",
            ["node_id", "parent_node_id"],
        )
    if not _index_exists("knowledge_node_placements", "ix_knowledge_node_placements_parent"):
        op.create_index(
            "ix_knowledge_node_placements_parent",
            "knowledge_node_placements",
            ["parent_node_id", "sort_order"],
        )


def downgrade() -> None:
    if _index_exists("knowledge_node_placements", "ix_knowledge_node_placements_parent"):
        op.drop_index("ix_knowledge_node_placements_parent", table_name="knowledge_node_placements")
    if _constraint_exists("knowledge_node_placements", "uq_knowledge_node_placement_parent"):
        op.drop_constraint(
            "uq_knowledge_node_placement_parent",
            "knowledge_node_placements",
            type_="unique",
        )
    if _table_exists("knowledge_node_placements"):
        op.drop_table("knowledge_node_placements")
    if _table_exists("knowledge_supertag_fields"):
        op.drop_table("knowledge_supertag_fields")
    if _column_exists("knowledge_supertags", "config_json"):
        op.drop_column("knowledge_supertags", "config_json")
    if _index_exists("knowledge_nodes", "ix_knowledge_nodes_workspace_day"):
        op.drop_index("ix_knowledge_nodes_workspace_day", table_name="knowledge_nodes")
    for column_name in ["day_date", "view_json", "query_json", "display_props", "description"]:
        if _column_exists("knowledge_nodes", column_name):
            op.drop_column("knowledge_nodes", column_name)
