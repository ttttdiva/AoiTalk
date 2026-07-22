"""Add tasks.estimated_hours column (fill missing manual DDL).

pre-Alembic baseline(20260318_0000) の tasks には estimated_hours があったが、
20260319_0001 の local_task_rebuild が tasks を作り直した際にこの列が引き継がれず、
以降のチェーンにも add_column が無い。実DB には手動 ALTER で estimated_hours が
存在し、frontend/src/db/schema.ts(estimatedHours: doublePrecision) も定義している。
この欠落を埋め、fresh 構築と drift 検査を一致させる。

実DB の実態に合わせる:
    - estimated_hours DOUBLE PRECISION NULL（default 無し・FK 無し・index 無し）

既存(本番)DB は手動で同名列を既に持ち、かつこのリビジョンより先の版位置にいるため
本来実行されない。保険として列が既に存在する場合はスキップするガードを付ける。

Revision ID: 20260516_0002
Revises: 20260516_0001
Create Date: 2026-05-16 00:00:01
"""

from __future__ import annotations

from alembic import op

revision = "20260516_0002"
down_revision = "20260516_0001"
branch_labels = None
depends_on = None


def _column_exists(bind, table: str, column: str) -> bool:
    return (
        bind.exec_driver_sql(
            "SELECT 1 FROM information_schema.columns "
            f"WHERE table_name = '{table}' AND column_name = '{column}'"
        ).first()
        is not None
    )


def upgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind, "tasks", "estimated_hours"):
        bind.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN estimated_hours DOUBLE PRECISION"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _column_exists(bind, "tasks", "estimated_hours"):
        bind.exec_driver_sql("ALTER TABLE tasks DROP COLUMN estimated_hours")
