"""旧シナリオ機能とTRPGプレイ実行系のテーブルを削除する。

シナリオスタジオ（story_*）への移行・検証・Docs側の掃除が完了したため、
旧構造13テーブルを落とす。対象データは移行済みか、プレイ実行系の
テストデータのみ（docs/scenario_studio_rebuild_plan.md §11.7・§11.8）。
削除直前のフルダンプは artifacts/scenario_studio/aoitalk_before_docs_purge.dump。

trpg_rule_items 等のTRPG資産テーブルと trpg_player_character_sheets
（モデル・テストが現存）は対象外。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260803_0001"
down_revision = "20260802_0008"
branch_labels = None
depends_on = None

# 子→親の順（CASCADE を併用するため厳密な順序依存はないが、読みやすさのため）
_TABLES = (
    "scenario_play_logs",
    "scenario_participants",
    "trpg_private_messages",
    "trpg_room_disclosures",
    "scenario_play_sessions",
    "scenario_authoring_branches",
    "scenario_writing_sessions",
    "scenario_canon_entries",
    "scenario_scenes",
    "scenario_episodes",
    "scenario_characters",
    "trpg_scenario_documents",
    "scenarios",
)


def upgrade() -> None:
    for table in _TABLES:
        op.execute(sa.text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))


def downgrade() -> None:
    raise NotImplementedError(
        "旧シナリオテーブルの復元は artifacts/scenario_studio/aoitalk_before_docs_purge.dump から行う"
    )
