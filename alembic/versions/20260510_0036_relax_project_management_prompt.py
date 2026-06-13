"""案件管理アシスタントの過剰な案件確認を抑制

Revision ID: 20260510_0036
Revises: 20260509_0035
Create Date: 2026-05-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260510_0036"
down_revision = "20260509_0035"
branch_labels = None
depends_on = None


PROJECT_MANAGEMENT_PROMPT = """
通常チャットに答えつつ、ユーザーが案件・タスク・WBS・進捗・予定・台帳などの管理作業を求めた時だけ、AoiTalk の Project を案件の基準IDとして扱って支援する。
日本語で簡潔、実務的、結論先出しで応答する。
""".strip()


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE characters
            SET system_prompt = :prompt
            WHERE slug = 'project_management_assistant'
            """
        ),
        {"prompt": PROJECT_MANAGEMENT_PROMPT},
    )


def downgrade() -> None:
    # 既存ユーザー編集済みのプロンプトを壊さないため、データ変更は戻さない。
    pass
