"""Make Docs the canonical project knowledge store.

Revision ID: 20260630_0002
Revises: 20260630_0001
Create Date: 2026-06-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260630_0002"
down_revision: Union[str, None] = "20260630_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("knowledge_node_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_projects_knowledge_node_id",
        "projects",
        ["knowledge_node_id"],
    )
    op.create_foreign_key(
        "fk_projects_knowledge_node_id",
        "projects",
        "knowledge_nodes",
        ["knowledge_node_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.drop_table("project_info_sync_states")
    op.drop_table("project_facts")
    op.drop_table("project_documents")
    op.drop_table("project_info_categories")

    op.drop_table("record_events")
    op.drop_table("record_attachments")
    op.drop_table("record_views")
    op.drop_table("record_rows")
    op.drop_table("record_fields")
    op.drop_table("record_tables")


def downgrade() -> None:
    raise RuntimeError(
        "Downgrade is intentionally unsupported after the canonical Docs migration."
    )
