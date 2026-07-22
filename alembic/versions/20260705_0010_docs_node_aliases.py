"""Add aliases to Docs knowledge nodes.

Revision ID: 20260705_0010
Revises: 20260705_0009
Create Date: 2026-07-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260705_0010"
down_revision = "20260705_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("knowledge_nodes")}
    if "aliases" not in columns:
        op.add_column(
            "knowledge_nodes",
            sa.Column("aliases", sa.JSON(), nullable=True, server_default=sa.text("'[]'::json")),
        )
    op.execute("update knowledge_nodes set aliases = '[]'::json where aliases is null")


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("knowledge_nodes")}
    if "aliases" in columns:
        op.drop_column("knowledge_nodes", "aliases")
