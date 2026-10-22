"""Daily原文を変更しない非同期整理の受付・履歴。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261002_0002"
down_revision = "20261002_0001"
branch_labels = None
depends_on = None


def upgrade():
    uid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "docs_daily_captures",
        sa.Column("id", uid, primary_key=True),
        sa.Column("actor_user_id", uid, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("docs_library_id", uid, sa.ForeignKey("docs_libraries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_id", uid, sa.ForeignKey("knowledge_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day_node_id", uid, sa.ForeignKey("knowledge_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("idempotency_key", sa.String(36), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("source_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("lease_expires_at", sa.DateTime()),
        sa.Column("history_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("actor_user_id", "idempotency_key", name="uq_daily_capture_actor_key"),
        sa.CheckConstraint("status IN ('queued','running','succeeded','failed','undone')", name="ck_daily_capture_status"),
    )
    op.create_index("ix_daily_capture_claim", "docs_daily_captures", ["status", "lease_expires_at", "created_at"])
    op.create_index("ix_daily_capture_actor_day", "docs_daily_captures", ["actor_user_id", "docs_library_id", "day"])


def downgrade():
    op.drop_table("docs_daily_captures")
