"""Encrypted append-only personal reminder selection/draft events (offline rollout)."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20261022_0003"
down_revision = "20261022_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "personal_reminder_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column("candidate_entry_id", UUID(as_uuid=True), nullable=False),
        sa.Column("selection_ref", UUID(as_uuid=True)),
        sa.Column("operation_key", sa.String(160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("owner_user_id", "id", name="uq_personal_reminder_owner_id"),
        sa.ForeignKeyConstraint(["owner_user_id", "candidate_entry_id"], ["personal_ledger_entries.owner_user_id", "personal_ledger_entries.id"], name="fk_personal_reminder_owned_candidate"),
        sa.ForeignKeyConstraint(["owner_user_id", "selection_ref"], ["personal_reminder_events.owner_user_id", "personal_reminder_events.id"], name="fk_personal_reminder_owned_selection"),
        sa.UniqueConstraint("owner_user_id", "operation_key", name="uq_personal_reminder_operation"),
        sa.CheckConstraint("event_type IN ('selection', 'proposed')", name="ck_personal_reminder_type"),
        sa.CheckConstraint("(event_type = 'proposed') = (selection_ref IS NOT NULL)", name="ck_personal_reminder_selection"),
    )
    op.create_index("ix_personal_reminder_owner", "personal_reminder_events", ["owner_user_id", "recorded_at", "id"])
    op.create_index("uq_personal_reminder_selection", "personal_reminder_events", ["selection_ref"], unique=True,
                    postgresql_where=sa.text("event_type = 'proposed'"))
    op.execute("""
        CREATE FUNCTION personal_reminder_events_append_only() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' OR (TG_OP = 'DELETE' AND pg_trigger_depth() <= 1) THEN
                RAISE EXCEPTION 'personal_reminder_events is append-only' USING ERRCODE = '23000';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER personal_reminder_events_append_only BEFORE UPDATE OR DELETE
        ON personal_reminder_events FOR EACH ROW EXECUTE FUNCTION personal_reminder_events_append_only()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS personal_reminder_events_append_only ON personal_reminder_events")
    op.execute("DROP FUNCTION IF EXISTS personal_reminder_events_append_only()")
    op.drop_table("personal_reminder_events")
