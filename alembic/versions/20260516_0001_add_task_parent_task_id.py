"""Add tasks.parent_task_id column (fill missing manual DDL).

実DB と frontend/src/db/schema.ts には tasks.parent_task_id が存在するが、
マイグレーションチェーンのどこにも add_column が無く（過去に手動 ALTER された）、
20260518_0039 がこの列を参照するため fresh 構築が失敗していた。この欠落を埋める。

実DB の実態に合わせる:
    - parent_task_id UUID NULL
    - FK tasks_parent_task_id_fkey -> tasks(id) ON DELETE CASCADE
    - INDEX idx_tasks_parent_task_id (parent_task_id)

既存(本番)DB は手動で同名の列・FK・indexを既に持ち、かつこのリビジョンより
先の版位置にいるため本来実行されない。保険として列/制約/indexが既に存在する場合は
それぞれスキップするガードを付ける。

Revision ID: 20260516_0001
Revises: 20260515_0038
Create Date: 2026-05-16 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260516_0001"
down_revision = "20260515_0038"
branch_labels = None
depends_on = None


def _column_exists(bind, table: str, column: str) -> bool:
    return (
        bind.exec_driver_sql(
            "SELECT 1 FROM information_schema.columns "
            f"WHERE table_schema = current_schema() "
            f"AND table_name = '{table}' AND column_name = '{column}'"
        ).first()
        is not None
    )


def _constraint_exists(bind, table: str, constraint: str) -> bool:
    return (
        bind.exec_driver_sql(
            "SELECT 1 FROM information_schema.table_constraints "
            f"WHERE table_schema = current_schema() "
            f"AND table_name = '{table}' AND constraint_name = '{constraint}'"
        ).first()
        is not None
    )


def upgrade() -> None:
    bind = op.get_bind()

    if not _column_exists(bind, "tasks", "parent_task_id"):
        bind.exec_driver_sql("ALTER TABLE tasks ADD COLUMN parent_task_id UUID")

    if not _constraint_exists(bind, "tasks", "tasks_parent_task_id_fkey"):
        bind.exec_driver_sql(
            "ALTER TABLE tasks "
            "ADD CONSTRAINT tasks_parent_task_id_fkey "
            "FOREIGN KEY (parent_task_id) REFERENCES tasks(id) ON DELETE CASCADE"
        )

    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_tasks_parent_task_id "
        "ON tasks (parent_task_id)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_tasks_parent_task_id")
    if _constraint_exists(bind, "tasks", "tasks_parent_task_id_fkey"):
        bind.exec_driver_sql(
            "ALTER TABLE tasks DROP CONSTRAINT tasks_parent_task_id_fkey"
        )
    if _column_exists(bind, "tasks", "parent_task_id"):
        bind.exec_driver_sql("ALTER TABLE tasks DROP COLUMN parent_task_id")
