"""provider観測と既知local消費の重複を固定watermarkで相関する。"""

from alembic import op
import sqlalchemy as sa

revision = "20261007_0003"
down_revision = "20261007_0002"
branch_labels = None
depends_on = None


def upgrade():
    # Legacy observations deliberately remain unbound. Runtime counts them
    # fully until UTC reset rather than inventing a historical overlap.
    op.add_column("free_team_quota_pools", sa.Column("provider_sync_state", sa.JSON(), nullable=True))
    op.add_column("free_team_quota_pools", sa.Column("provider_sync_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("free_team_reservations", sa.Column("settlement_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("free_team_reservations", "settlement_version")
    op.drop_column("free_team_quota_pools", "provider_sync_version")
    op.drop_column("free_team_quota_pools", "provider_sync_state")
