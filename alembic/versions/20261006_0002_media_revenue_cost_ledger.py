"""Add immutable event-scoped cost statements without inventing legacy costs."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261006_0002"
down_revision = "20261006_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "media_cost_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True),
        sa.Column("persona_ref", sa.String(164), nullable=False),
        sa.Column("content_ref", sa.String(164), nullable=True),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("basis", sa.String(16), nullable=False),
        sa.Column("fee_amount", sa.Float(), nullable=True),
        sa.Column("production_cost_amount", sa.Float(), nullable=True),
        sa.Column("human_cost_amount", sa.Float(), nullable=True),
        sa.Column("human_minutes", sa.Float(), nullable=True),
        sa.Column("missing_reasons", sa.JSON(), nullable=False),
        sa.Column("coverage_complete", sa.Boolean(), nullable=False),
        sa.Column("coverage_note", sa.Text(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("correction_of_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("media_cost_entries.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("entry_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("entry_id", "revision", name="uq_media_cost_entry_revision"),
        sa.UniqueConstraint("correction_of_id", name="uq_media_cost_entry_successor"),
        sa.CheckConstraint("revision >= 1", name="ck_media_cost_entry_revision"),
        sa.CheckConstraint("source IN ('manual', 'imported')", name="ck_media_cost_entry_source"),
        sa.CheckConstraint("basis IN ('estimated', 'settled')", name="ck_media_cost_entry_basis"),
        sa.CheckConstraint("window_start <= window_end", name="ck_media_cost_entry_window"),
        sa.CheckConstraint("length(currency) = 3 AND length(entry_hash) = 64", name="ck_media_cost_entry_hash_currency"),
        sa.CheckConstraint("human_minutes IS NULL OR human_minutes >= 0", name="ck_media_cost_entry_minutes"),
        sa.CheckConstraint("(revision = 1 AND correction_of_id IS NULL AND entry_id = id) OR (revision > 1 AND correction_of_id IS NOT NULL)", name="ck_media_cost_entry_predecessor"),
    )
    for field in ("entry_id", "owner_user_id", "project_id", "persona_ref", "created_at"):
        op.create_index(f"ix_media_cost_entries_{field}", "media_cost_entries", [field])
    op.create_index("uq_media_cost_entry_personal_key", "media_cost_entries", ["owner_user_id", "idempotency_key"], unique=True, postgresql_where=sa.text("project_id IS NULL"), sqlite_where=sa.text("project_id IS NULL"))
    op.create_index("uq_media_cost_entry_project_key", "media_cost_entries", ["project_id", "idempotency_key"], unique=True, postgresql_where=sa.text("project_id IS NOT NULL"), sqlite_where=sa.text("project_id IS NOT NULL"))
    op.create_table(
        "media_revenue_cost_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("revenue_event_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("media_revenue_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True),
        sa.Column("revenue_event_hash", sa.String(64), nullable=False),
        sa.Column("cost_entry_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("media_cost_entries.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("basis", sa.String(16), nullable=False),
        sa.Column("attribution_method", sa.String(32), nullable=False),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("fee_amount", sa.Float(), nullable=True),
        sa.Column("production_cost_amount", sa.Float(), nullable=True),
        sa.Column("human_cost_amount", sa.Float(), nullable=True),
        sa.Column("human_minutes", sa.Float(), nullable=True),
        sa.Column("missing_reasons", sa.JSON(), nullable=False),
        sa.Column("coverage_complete", sa.Boolean(), nullable=False),
        sa.Column("coverage_note", sa.Text(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("correction_of_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("media_revenue_cost_records.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("cost_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("revenue_event_id", "revision", name="uq_media_revenue_cost_revision"),
        sa.UniqueConstraint("revenue_event_id", "idempotency_key", name="uq_media_revenue_cost_idempotency"),
        sa.UniqueConstraint("correction_of_id", name="uq_media_revenue_cost_successor"),
        sa.CheckConstraint("revision >= 1", name="ck_media_revenue_cost_revision"),
        sa.CheckConstraint("source IN ('manual', 'imported')", name="ck_media_revenue_cost_source"),
        sa.CheckConstraint("basis IN ('estimated', 'settled')", name="ck_media_revenue_cost_basis"),
        sa.CheckConstraint("attribution_method = 'direct_event'", name="ck_media_revenue_cost_attribution"),
        sa.CheckConstraint("window_start = window_end", name="ck_media_revenue_cost_window"),
        sa.CheckConstraint("length(currency) = 3", name="ck_media_revenue_cost_currency"),
        sa.CheckConstraint("length(cost_hash) = 64 AND length(revenue_event_hash) = 64", name="ck_media_revenue_cost_hash"),
        sa.CheckConstraint("human_minutes IS NULL OR human_minutes >= 0", name="ck_media_revenue_cost_minutes"),
        sa.CheckConstraint("(revision = 1 AND correction_of_id IS NULL) OR (revision > 1 AND correction_of_id IS NOT NULL)", name="ck_media_revenue_cost_predecessor"),
    )
    for field in ("revenue_event_id", "owner_user_id", "project_id", "created_at"):
        op.create_index(f"ix_media_revenue_cost_records_{field}", "media_revenue_cost_records", [field])

    op.create_table(
        "media_cost_allocations",
        sa.Column("cost_entry_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("media_cost_entries.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("cost_entry_revision_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("media_cost_entries.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("revenue_event_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("media_revenue_events.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("cost_record_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("media_revenue_cost_records.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    for table in ("media_cost_allocations", "media_revenue_cost_records", "media_cost_entries"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("Cannot downgrade: revenue cost evidence exists")
    op.drop_table("media_cost_allocations")
    op.drop_table("media_revenue_cost_records")
    op.drop_table("media_cost_entries")
