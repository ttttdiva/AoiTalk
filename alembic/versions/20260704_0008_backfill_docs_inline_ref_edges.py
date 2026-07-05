"""Backfill Docs inline reference edges from explicit node tokens.

Revision ID: 20260704_0008
Revises: 20260704_0007
Create Date: 2026-07-04
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260704_0008"
down_revision: Union[str, None] = "20260704_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not (_table_exists("knowledge_nodes") and _table_exists("knowledge_edges")):
        return

    op.execute(
        """
        INSERT INTO knowledge_edges (
            source_node_id,
            target_node_id,
            relation_type,
            confidence,
            created_by,
            created_at
        )
        SELECT
            source_node.id,
            target_node.id,
            'inline_ref',
            1,
            source_node.updated_by,
            NOW()
        FROM knowledge_nodes AS source_node
        CROSS JOIN LATERAL regexp_matches(
            concat_ws(E'\n', source_node.title, COALESCE(source_node.body_text, '')),
            '(?:@docs:|aoitalk://docs/|\\[\\[node:)([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})',
            'gi'
        ) AS ref(target_id)
        JOIN knowledge_nodes AS target_node
          ON target_node.id = ref.target_id[1]::uuid
         AND target_node.workspace_id = source_node.workspace_id
         AND target_node.archived_at IS NULL
        WHERE source_node.archived_at IS NULL
          AND source_node.id <> target_node.id
          AND NOT EXISTS (
            SELECT 1
            FROM knowledge_edges AS existing
            WHERE existing.source_node_id = source_node.id
              AND existing.target_node_id = target_node.id
              AND existing.relation_type = 'inline_ref'
          )
        """
    )


def downgrade() -> None:
    # Backfilled inline_ref edges are indistinguishable from legitimate saved edges.
    pass
