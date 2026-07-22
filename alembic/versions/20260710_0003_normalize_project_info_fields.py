"""案件情報スーパータグのfield定義を正規化

Revision ID: 20260710_0003
Revises: 20260710_0002
Create Date: 2026-07-10
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "20260710_0003"
down_revision: Union[str, None] = "20260710_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE knowledge_fields AS field
        SET field_type = 'reference',
            updated_at = NOW()
        FROM knowledge_supertags AS supertag
        WHERE supertag.id = field.supertag_id
          AND supertag.system_key = 'project_info'
          AND field.name = 'Project'
        """
    )
    op.execute(
        """
        UPDATE knowledge_fields AS field
        SET field_type = 'options',
            options_json = '{"values":["canonical","child","archive"]}'::json,
            default_value_json = '"canonical"'::json,
            updated_at = NOW()
        FROM knowledge_supertags AS supertag
        WHERE supertag.id = field.supertag_id
          AND supertag.system_key = 'project_info'
          AND field.name = 'Page Role'
        """
    )


def downgrade() -> None:
    # 旧選択肢へ戻すと canonical 値を表示できなくなるため、定義は維持する。
    pass
