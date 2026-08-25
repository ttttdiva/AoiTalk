"""Appのlifecycle_statusを廃止し、archived_atを唯一の状態にする。

Appのlifecycle_statusは実行・権限・保存版の挙動を制御しておらず、
archived_atと二重管理になっていた。通常のAppはarchived_at IS NULLで表す。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260802_0007"
down_revision = "20260802_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("apps")}
    # 旧値だけを救済してから、適用済みの過去Migrationは書き換えずに列を落とす。
    # 部分適用済みDBでも再実行できるよう、列が存在する場合だけ旧値を参照する。
    if "lifecycle_status" in columns:
        op.execute(
            sa.text(
                """
                UPDATE apps
                SET archived_at = COALESCE(archived_at, updated_at, CURRENT_TIMESTAMP)
                WHERE lifecycle_status = 'archived'
                  AND archived_at IS NULL
                """
            )
        )
    op.execute(sa.text('DROP INDEX IF EXISTS "ix_apps_owner_lifecycle"'))
    op.execute(sa.text('DROP INDEX IF EXISTS "ix_apps_lifecycle_status"'))
    op.execute(sa.text('ALTER TABLE apps DROP CONSTRAINT IF EXISTS "ck_apps_lifecycle_status"'))
    if "lifecycle_status" in columns:
        op.execute(sa.text('ALTER TABLE apps DROP COLUMN lifecycle_status'))


def downgrade() -> None:
    # 新設計では archived_at が唯一の状態であり、旧 lifecycle_status の
    # developing/active/maintenance の区別は保存されない。旧スキーマへ戻す場合は
    # 未アーカイブのAppを developing として復元する。
    op.add_column(
        "apps",
        sa.Column("lifecycle_status", sa.String(length=32), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE apps
            SET lifecycle_status = CASE
              WHEN archived_at IS NOT NULL THEN 'archived'
              ELSE 'developing'
            END
            """
        )
    )
    op.alter_column(
        "apps",
        "lifecycle_status",
        existing_type=sa.String(length=32),
        nullable=False,
        server_default="developing",
    )
    op.create_check_constraint(
        "ck_apps_lifecycle_status",
        "apps",
        "lifecycle_status IN ('developing','active','maintenance','archived')",
    )
    op.create_index("ix_apps_lifecycle_status", "apps", ["lifecycle_status"])
    op.create_index("ix_apps_owner_lifecycle", "apps", ["owner_user_id", "lifecycle_status"])
