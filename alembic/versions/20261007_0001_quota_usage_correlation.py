"""既存の中央usage ownerを維持したquota台帳相関。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261007_0001"
down_revision = "20261006_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("token_usage", sa.Column("quota_reservation_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_token_usage_quota_reservation_id", "token_usage", ["quota_reservation_id"])


def downgrade():
    op.drop_index("ix_token_usage_quota_reservation_id", table_name="token_usage")
    op.drop_column("token_usage", "quota_reservation_id")
