"""Bind App Action execution to the owning App user namespace."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261018_0002"
down_revision = "20261018_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 20261018_0001 may already be present on development databases.  Add the
    # owner binding additively and backfill existing runs from their immutable
    # authenticated starter before making the column required.
    op.add_column(
        "app_action_runs",
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE app_action_runs "
            "SET owner_user_id = started_by "
            "WHERE owner_user_id IS NULL"
        )
    )
    op.alter_column("app_action_runs", "owner_user_id", nullable=False)
    op.create_foreign_key(
        "fk_app_action_runs_owner_user",
        "app_action_runs",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_app_action_runs_owner_user_id",
        "app_action_runs",
        ["owner_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_app_action_runs_owner_user_id", table_name="app_action_runs")
    op.drop_constraint("fk_app_action_runs_owner_user", "app_action_runs", type_="foreignkey")
    op.drop_column("app_action_runs", "owner_user_id")
