"""Store the verified agent origin of a delegation, when present."""

from alembic import op
import sqlalchemy as sa


revision = "20261031_0002"
down_revision = "20261031_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_delegations", sa.Column("source_actor_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_delegations", "source_actor_json")
