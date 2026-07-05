"""Drop legacy project information memo tables.

Revision ID: 20260702_0001
Revises: 20260701_0004
Create Date: 2026-07-02
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260702_0001"
down_revision: Union[str, None] = "20260701_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _drop_table_if_exists(table_name: str) -> None:
    if _table_exists(table_name):
        op.drop_table(table_name)


def upgrade() -> None:
    _drop_table_if_exists("project_info_sync_states")
    _drop_table_if_exists("project_facts")
    _drop_table_if_exists("project_documents")
    _drop_table_if_exists("project_info_categories")


def downgrade() -> None:
    raise RuntimeError(
        "Downgrade is intentionally unsupported after removing the legacy "
        "project information memo schema."
    )
