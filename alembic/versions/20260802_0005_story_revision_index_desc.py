"""story_episode_revisions の履歴索引を降順で作り直す。

20260802_0004 を適用済みの環境では、同名の索引が昇順のまま残っている。
索引の張り替えだけを行い、テーブルやデータには触れない。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260802_0005"
down_revision = "20260802_0004"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_story_episode_revisions_episode_rev_desc"


def upgrade() -> None:
    op.execute(sa.text(f"DROP INDEX IF EXISTS {_INDEX_NAME}"))
    op.create_index(
        _INDEX_NAME,
        "story_episode_revisions",
        ["episode_id", sa.text("rev_no DESC")],
    )


def downgrade() -> None:
    op.execute(sa.text(f"DROP INDEX IF EXISTS {_INDEX_NAME}"))
    op.create_index(_INDEX_NAME, "story_episode_revisions", ["episode_id", "rev_no"])
