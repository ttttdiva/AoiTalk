"""canonical案件情報ノードのPage Roleを補完

Revision ID: 20260710_0002
Revises: 20260710_0001
Create Date: 2026-07-10
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "20260710_0002"
down_revision: Union[str, None] = "20260710_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO knowledge_field_values (
            node_id,
            field_id,
            value_json,
            value_text,
            updated_at,
            updated_by
        )
        SELECT
            node.id,
            field.id,
            '"canonical"'::json,
            'canonical',
            NOW(),
            COALESCE(node.updated_by, project.owner_id)
        FROM projects AS project
        JOIN knowledge_nodes AS node
          ON node.id = project.knowledge_node_id
         AND node.archived_at IS NULL
        JOIN knowledge_node_supertags AS node_tag
          ON node_tag.node_id = node.id
        JOIN knowledge_supertags AS supertag
          ON supertag.id = node_tag.supertag_id
         AND supertag.workspace_id = node.workspace_id
         AND supertag.system_key = 'project_info'
        JOIN knowledge_fields AS field
          ON field.supertag_id = supertag.id
         AND field.name = 'Page Role'
        WHERE project.deleted_at IS NULL
        ON CONFLICT (node_id, field_id) DO NOTHING
        """
    )


def downgrade() -> None:
    # 補完後にユーザーが値を編集した可能性があるため、downgradeでは削除しない。
    pass
