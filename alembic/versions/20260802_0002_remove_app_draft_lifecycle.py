"""App と保存版の作成途中 draft を廃止し、既存値を利用可能な状態へ移行する。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260802_0002"
down_revision = "20260802_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE apps SET lifecycle_status = 'developing' "
            "WHERE lifecycle_status = 'draft'"
        )
    )
    op.drop_constraint("ck_apps_lifecycle_status", "apps", type_="check")
    op.create_check_constraint(
        "ck_apps_lifecycle_status",
        "apps",
        "lifecycle_status IN ('developing','active','maintenance','archived')",
    )
    op.alter_column(
        "apps",
        "lifecycle_status",
        server_default=sa.text("'developing'"),
    )
    op.execute(
        sa.text(
            "UPDATE app_releases SET status = 'published' "
            "WHERE status = 'draft'"
        )
    )
    op.drop_constraint("ck_app_releases_status", "app_releases", type_="check")
    op.create_check_constraint(
        "ck_app_releases_status",
        "app_releases",
        "status IN ('published','deprecated')",
    )
    op.alter_column(
        "app_releases",
        "status",
        server_default=sa.text("'published'"),
    )


def downgrade() -> None:
    op.drop_constraint("ck_apps_lifecycle_status", "apps", type_="check")
    op.create_check_constraint(
        "ck_apps_lifecycle_status",
        "apps",
        "lifecycle_status IN ('draft','developing','active','maintenance','archived')",
    )
    op.alter_column(
        "apps",
        "lifecycle_status",
        server_default=sa.text("'draft'"),
    )
    op.drop_constraint("ck_app_releases_status", "app_releases", type_="check")
    op.create_check_constraint(
        "ck_app_releases_status",
        "app_releases",
        "status IN ('draft','published','deprecated')",
    )
    op.alter_column(
        "app_releases",
        "status",
        server_default=sa.text("'draft'"),
    )
