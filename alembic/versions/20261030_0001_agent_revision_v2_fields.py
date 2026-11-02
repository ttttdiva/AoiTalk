"""Add P2 AgentRevision model, tool, and delegation fields."""

from alembic import op
import sqlalchemy as sa


revision = "20261030_0001"
down_revision = "20261029_0001"
branch_labels = None
depends_on = None


_TABLE = "agent_revisions"
_COLUMNS = (
    ("model_overrides", sa.JSON()),
    ("tool_ids", sa.JSON()),
    ("delegate_policy", sa.JSON()),
)


def upgrade() -> None:
    for name, column_type in _COLUMNS:
        op.add_column(_TABLE, sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    for name, _column_type in reversed(_COLUMNS):
        op.drop_column(_TABLE, name)
