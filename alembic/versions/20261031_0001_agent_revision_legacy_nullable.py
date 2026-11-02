"""Keep retired AgentRevision Team fields readable but nullable."""

from alembic import op
from sqlalchemy import column, table, update
import sqlalchemy as sa


revision = "20261031_0001"
down_revision = "20261101_0001"
branch_labels = None
depends_on = None


_TABLE = "agent_revisions"
_LEGACY_COLUMNS = (
    "agent_team_id",
    "execution_profile_id",
    "allowed_subagent_ids",
)


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE) as batch_op:
            for name in _LEGACY_COLUMNS:
                batch_op.alter_column(
                    name,
                    existing_type=(
                        sa.JSON()
                        if name == "allowed_subagent_ids"
                        else sa.String(length=100)
                    ),
                    nullable=True,
                )
        return

    for name in _LEGACY_COLUMNS:
        op.alter_column(
            _TABLE,
            name,
            existing_type=(
                sa.JSON()
                if name == "allowed_subagent_ids"
                else sa.String(length=100)
            ),
            nullable=True,
        )


def downgrade() -> None:
    revisions = table(
        _TABLE,
        column("agent_team_id", sa.String(length=100)),
        column("execution_profile_id", sa.String(length=100)),
        column("allowed_subagent_ids", sa.JSON()),
    )
    op.execute(
        update(revisions)
        .where(revisions.c.agent_team_id.is_(None))
        .values(agent_team_id="")
    )
    op.execute(
        update(revisions)
        .where(revisions.c.execution_profile_id.is_(None))
        .values(execution_profile_id="")
    )
    op.execute(
        update(revisions)
        .where(revisions.c.allowed_subagent_ids.is_(None))
        .values(allowed_subagent_ids=[])
    )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE) as batch_op:
            for name in reversed(_LEGACY_COLUMNS):
                batch_op.alter_column(
                    name,
                    existing_type=(
                        sa.JSON()
                        if name == "allowed_subagent_ids"
                        else sa.String(length=100)
                    ),
                    nullable=False,
                )
        return

    for name in reversed(_LEGACY_COLUMNS):
        op.alter_column(
            _TABLE,
            name,
            existing_type=(
                sa.JSON()
                if name == "allowed_subagent_ids"
                else sa.String(length=100)
            ),
            nullable=False,
        )
