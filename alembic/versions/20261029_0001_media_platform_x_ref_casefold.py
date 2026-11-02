"""Enforce case-insensitive X account-ref uniqueness."""

from alembic import op
import sqlalchemy as sa


revision = "20261029_0001"
down_revision = "20261027_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_media_platform_accounts_personal_x_identity_casefold",
        "media_platform_accounts",
        ["owner_user_id", sa.text("lower(account_ref)")],
        unique=True,
        postgresql_where=sa.text("project_id IS NULL AND platform = 'x'"),
        sqlite_where=sa.text("project_id IS NULL AND platform = 'x'"),
    )
    op.create_index(
        "uq_media_platform_accounts_project_x_identity_casefold",
        "media_platform_accounts",
        ["project_id", sa.text("lower(account_ref)")],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL AND platform = 'x'"),
        sqlite_where=sa.text("project_id IS NOT NULL AND platform = 'x'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_media_platform_accounts_project_x_identity_casefold",
        table_name="media_platform_accounts",
    )
    op.drop_index(
        "uq_media_platform_accounts_personal_x_identity_casefold",
        table_name="media_platform_accounts",
    )
