"""Add reversible archive state to Agent Team graph headers."""

from alembic import op
import sqlalchemy as sa

revision = "20261101_0002_graph_archive"
down_revision = "20261031_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_team_graphs", sa.Column("archived_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_team_graphs", "archived_at")
