"""Link token usage rows to their nullable AgentRun owner."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261010_0001"
down_revision = "20261009_0002"
branch_labels = None
depends_on = None


_FOREIGN_KEY = "fk_token_usage_agent_run_id"
_INDEX = "ix_token_usage_agent_run_id"


def upgrade() -> None:
    """Add an optional, deletion-safe AgentRun link to token usage rows."""

    # A batch operation keeps this additive change executable by the isolated
    # SQLite migration tests while compiling to ordinary ALTER TABLE DDL on
    # PostgreSQL.  Existing rows receive NULL and are deliberately not
    # backfilled from session/project/time fields.
    with op.batch_alter_table("token_usage") as batch_op:
        batch_op.add_column(
            sa.Column(
                "agent_run_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            )
        )
        batch_op.create_index(_INDEX, ["agent_run_id"], unique=False)
        batch_op.create_foreign_key(
            _FOREIGN_KEY,
            "agent_runs",
            ["agent_run_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Remove only the link while preserving all existing usage rows."""

    with op.batch_alter_table("token_usage") as batch_op:
        batch_op.drop_index(_INDEX)
        batch_op.drop_constraint(_FOREIGN_KEY, type_="foreignkey")
        batch_op.drop_column("agent_run_id")
