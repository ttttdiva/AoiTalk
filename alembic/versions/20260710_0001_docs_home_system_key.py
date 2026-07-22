"""Docs Homeノード用のsystem_keyを追加

Revision ID: 20260710_0001
Revises: 20260707_0001
Create Date: 2026-07-10
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260710_0001"
down_revision: Union[str, None] = "20260707_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("knowledge_nodes", sa.Column("system_key", sa.Text(), nullable=True))
    op.create_unique_constraint(
        "uq_knowledge_nodes_workspace_system_key",
        "knowledge_nodes",
        ["workspace_id", "system_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_knowledge_nodes_workspace_system_key",
        "knowledge_nodes",
        type_="unique",
    )
    op.drop_column("knowledge_nodes", "system_key")
