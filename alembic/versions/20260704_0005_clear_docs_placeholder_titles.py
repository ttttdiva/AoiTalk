"""Clear saved Docs placeholder node titles.

Revision ID: 20260704_0005
Revises: 20260704_0004
Create Date: 2026-07-04
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260704_0005"
down_revision: Union[str, None] = "20260704_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _table_exists("knowledge_nodes"):
        return
    op.execute(
        """
        UPDATE knowledge_nodes
        SET title = '',
            body_text = ''
        WHERE title = 'New node'
          AND COALESCE(body_text, '') IN ('', 'New node')
        """
    )


def downgrade() -> None:
    pass
