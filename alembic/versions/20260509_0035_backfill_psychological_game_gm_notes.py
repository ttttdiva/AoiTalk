"""心理戦シナリオのGM指示を再バックフィル

Revision ID: 20260509_0035
Revises: 20260509_0034
Create Date: 2026-05-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260509_0035"
down_revision = "20260509_0034"
branch_labels = None
depends_on = None


HYOSU_CHICKEN_RACE_ID = "9ac64ff9-bf2d-43e3-9fa6-e236c7a6940c"
PIGEONHOLE_KEY_ID = "1010b861-ae8a-445f-8b92-2decaf553498"

GM_STRATEGY_NOTE = """
AI NPC作戦運用: ピリオド開始後、作戦タイム開始時、投票/選択前の相談に入った時は、必要に応じて末尾へ [NPC_STRATEGY:phase=作戦タイム,delay=30,focus=今回の争点] を付ける。NPCの秘密の本音は公開描写に書かず、30秒後のAI NPC内部思考に任せる。
""".strip()


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE scenarios
            SET gm_instructions = CASE
                WHEN COALESCE(gm_instructions, '') LIKE '%AI NPC作戦運用:%' THEN gm_instructions
                ELSE CONCAT(COALESCE(gm_instructions, ''), E'\n\n', :note)
            END
            WHERE id IN (
                CAST(:hyosu_id AS UUID),
                CAST(:pigeonhole_id AS UUID)
            )
            """
        ),
        {
            "note": GM_STRATEGY_NOTE,
            "hyosu_id": HYOSU_CHICKEN_RACE_ID,
            "pigeonhole_id": PIGEONHOLE_KEY_ID,
        },
    )


def downgrade() -> None:
    # 既存ユーザー編集済みのGM指示を壊さないため、データ変更は戻さない。
    pass
