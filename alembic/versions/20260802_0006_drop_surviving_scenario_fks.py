"""存続テーブルから scenarios への外部キーを外す。

シナリオスタジオ移行で ORM 側の ForeignKey は既に外れているが、DB 側に残ると
旧 scenarios テーブルを落とす際のブロッカーになる。制約だけを削除し、
scenario_id 列とデータはそのまま残す。

旧 scenario_* / trpg_scenario_documents 側の外部キーは、テーブルごと削除する
別手順で消えるためここでは触らない。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260802_0006"
down_revision = "20260802_0005"
branch_labels = None
depends_on = None

_CONSTRAINTS = (
    ("trpg_player_character_sheets", "trpg_player_character_sheets_scenario_id_fkey"),
    ("world_books", "fk_world_books_scenario_id"),
)


def upgrade() -> None:
    for table, constraint in _CONSTRAINTS:
        op.execute(sa.text(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{constraint}"'))


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("scenarios"):
        return
    for table, constraint in _CONSTRAINTS:
        op.create_foreign_key(constraint, table, "scenarios", ["scenario_id"], ["id"], ondelete="CASCADE")
