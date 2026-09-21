"""Add durable platform-account policy and connection binding fields."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260901_0015"
down_revision = "20260901_0014"
branch_labels = None
depends_on = None


def _is_sqlite() -> bool:
    bind = op.get_bind()
    return str(getattr(getattr(bind, "dialect", None), "name", "")).lower() == "sqlite"


def _add_columns(table: str, columns: list[sa.Column]) -> None:
    if _is_sqlite():
        with op.batch_alter_table(table, recreate="always") as batch:
            for column in columns:
                batch.add_column(column)
    else:
        for column in columns:
            op.add_column(table, column)


def _drop_columns(table: str, names: list[str]) -> None:
    if _is_sqlite():
        with op.batch_alter_table(table, recreate="always") as batch:
            for name in reversed(names):
                batch.drop_column(name)
    else:
        for name in reversed(names):
            op.drop_column(table, name)


_TIMEZONE_CHECK = "timezone IS NULL OR length(timezone) <= 64"


def _add_revision_policy_checks() -> None:
    """Keep the model/Drizzle timezone bound enforced by Alembic as well."""
    if _is_sqlite():
        with op.batch_alter_table(
            "media_platform_account_revisions", recreate="always"
        ) as batch:
            batch.create_check_constraint(
                "ck_media_platform_account_revisions_timezone",
                _TIMEZONE_CHECK,
            )
    else:
        op.create_check_constraint(
            "ck_media_platform_account_revisions_timezone",
            "media_platform_account_revisions",
            _TIMEZONE_CHECK,
        )


def _drop_revision_policy_checks() -> None:
    if _is_sqlite():
        with op.batch_alter_table(
            "media_platform_account_revisions", recreate="always"
        ) as batch:
            batch.drop_constraint(
                "ck_media_platform_account_revisions_timezone", type_="check"
            )
    else:
        op.drop_constraint(
            "ck_media_platform_account_revisions_timezone",
            "media_platform_account_revisions",
            type_="check",
        )


def upgrade() -> None:
    _add_columns(
        "media_platform_accounts",
        [
            sa.Column(
                "connection_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(
                    "external_connections.id",
                    name="fk_media_platform_accounts_connection_id",
                    ondelete="SET NULL",
                ),
                nullable=True,
            ),
            sa.Column("account_type", sa.String(length=32), nullable=False, server_default="profile"),
            sa.Column("remote_url", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        ],
    )
    op.create_index("ix_media_platform_accounts_connection_id", "media_platform_accounts", ["connection_id"])
    op.create_index("ix_media_platform_accounts_status", "media_platform_accounts", ["status"])
    if _is_sqlite():
        with op.batch_alter_table("media_platform_accounts", recreate="always") as batch:
            batch.create_check_constraint(
                "ck_media_platform_accounts_status",
                "status IN ('active', 'paused')",
            )
    else:
        op.create_check_constraint(
            "ck_media_platform_accounts_status",
            "media_platform_accounts",
            "status IN ('active', 'paused')",
        )

    _add_columns(
        "media_platform_account_revisions",
        [
            sa.Column("remote_url", sa.Text(), nullable=True),
            sa.Column("locale", sa.String(length=64), nullable=True),
            sa.Column("timezone", sa.String(length=64), nullable=True),
            sa.Column("supported_content_modes", sa.JSON(), nullable=True),
            sa.Column("disclosure_defaults", sa.JSON(), nullable=True),
            sa.Column("rating_defaults", sa.JSON(), nullable=True),
            sa.Column("adapter_ref", sa.String(length=164), nullable=True),
        ],
    )
    _add_revision_policy_checks()


def downgrade() -> None:
    _drop_revision_policy_checks()
    _drop_columns(
        "media_platform_account_revisions",
        [
            "remote_url",
            "locale",
            "timezone",
            "supported_content_modes",
            "disclosure_defaults",
            "rating_defaults",
            "adapter_ref",
        ],
    )
    if _is_sqlite():
        with op.batch_alter_table("media_platform_accounts", recreate="always") as batch:
            batch.drop_constraint("ck_media_platform_accounts_status", type_="check")
    else:
        op.drop_constraint("ck_media_platform_accounts_status", "media_platform_accounts", type_="check")
    op.drop_index("ix_media_platform_accounts_status", table_name="media_platform_accounts")
    op.drop_index("ix_media_platform_accounts_connection_id", table_name="media_platform_accounts")
    _drop_columns("media_platform_accounts", ["connection_id", "account_type", "remote_url", "status"])
