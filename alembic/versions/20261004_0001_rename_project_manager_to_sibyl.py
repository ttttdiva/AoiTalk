"""案件管理キャラクターの表示名をシビュラへ変更する。

Revision ID: 20261004_0001
Revises: 20261002_0002
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20261004_0001"
down_revision = "20261002_0002"
branch_labels = None
depends_on = None


OLD_NAME = "案件管理アシスタント"
NEW_NAME = "シビュラ"
CANONICAL_SLUG = "project_manager"


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE characters
            SET
                name = :new_name,
                recognition_aliases = CASE
                    WHEN recognition_aliases IS NULL THEN
                        CAST(:new_aliases AS json)
                    WHEN NOT (
                        CAST(recognition_aliases AS jsonb)
                        @> CAST(:sibyl_alias AS jsonb)
                    ) THEN
                        CAST(
                            CAST(recognition_aliases AS jsonb)
                            || CAST(:sibyl_alias AS jsonb)
                            AS json
                        )
                    ELSE recognition_aliases
                END
            WHERE slug = :slug
            """
        ),
        {
            "new_name": NEW_NAME,
            "new_aliases": '["シビュラ", "案件管理", "案件管理アシスタント", "プロジェクト管理", "進行管理", "PM"]',
            "sibyl_alias": '["シビュラ"]',
            "slug": CANONICAL_SLUG,
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE characters
            SET name = :old_name
            WHERE slug = :slug
              AND name = :new_name
            """
        ),
        {
            "old_name": OLD_NAME,
            "new_name": NEW_NAME,
            "slug": CANONICAL_SLUG,
        },
    )
