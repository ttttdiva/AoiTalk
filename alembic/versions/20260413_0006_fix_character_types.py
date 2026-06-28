"""案件管理アシスタント以外のcharacter_typeをroleplayに修正

Revision ID: 20260413_0006
Revises: 20260413_0005
Create Date: 2026-04-13
"""

from __future__ import annotations

from alembic import op

revision = "20260413_0006"
down_revision = "20260413_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE characters SET character_type = 'roleplay' "
        "WHERE slug != 'project_manager' AND character_type = 'assistant'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE characters SET character_type = 'assistant' "
        "WHERE character_type = 'roleplay'"
    )
