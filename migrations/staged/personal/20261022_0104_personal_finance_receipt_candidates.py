"""Append-only encrypted receipt evidence + unconfirmed finance candidates (dormant)."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20261022_0104"
down_revision = "20261022_0201"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "personal_finance_receipt_candidates",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_ref", UUID(as_uuid=True), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("operation_key", sa.String(160), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("owner_user_id", "operation_key", name="uq_personal_receipt_operation"),
        sa.CheckConstraint("status = 'needs_review'", name="ck_personal_receipt_status"),
    )
    op.create_index("ix_personal_receipt_owner", "personal_finance_receipt_candidates", ["owner_user_id", "recorded_at", "id"])
    op.execute("""
        CREATE FUNCTION personal_finance_receipt_candidates_append_only() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' OR (TG_OP = 'DELETE' AND pg_trigger_depth() <= 1) THEN
                RAISE EXCEPTION 'personal_finance_receipt_candidates is append-only' USING ERRCODE = '23000';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER personal_finance_receipt_candidates_append_only BEFORE UPDATE OR DELETE
        ON personal_finance_receipt_candidates FOR EACH ROW
        EXECUTE FUNCTION personal_finance_receipt_candidates_append_only()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS personal_finance_receipt_candidates_append_only ON personal_finance_receipt_candidates")
    op.execute("DROP FUNCTION IF EXISTS personal_finance_receipt_candidates_append_only()")
    op.drop_index("ix_personal_receipt_owner", table_name="personal_finance_receipt_candidates")
    op.drop_table("personal_finance_receipt_candidates")
