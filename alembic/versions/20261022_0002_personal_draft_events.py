"""Encrypted append-only personal draft and evidence lifecycle (offline rollout)."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20261022_0002"
down_revision = "20261022_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_personal_ledger_owner_id", "personal_ledger_entries", ["owner_user_id", "id"])
    op.create_table(
        "personal_draft_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column("draft_ref", UUID(as_uuid=True)),
        sa.Column("evidence_ref", UUID(as_uuid=True)),
        sa.Column("target_entry_id", UUID(as_uuid=True)),
        sa.Column("operation_key", sa.String(160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("owner_user_id", "id", name="uq_personal_draft_owner_id"),
        sa.ForeignKeyConstraint(["owner_user_id", "draft_ref"], ["personal_draft_events.owner_user_id", "personal_draft_events.id"], name="fk_personal_draft_owned_draft"),
        sa.ForeignKeyConstraint(["owner_user_id", "evidence_ref"], ["personal_draft_events.owner_user_id", "personal_draft_events.id"], name="fk_personal_draft_owned_evidence"),
        sa.ForeignKeyConstraint(["owner_user_id", "target_entry_id"], ["personal_ledger_entries.owner_user_id", "personal_ledger_entries.id"], name="fk_personal_draft_owned_target"),
        sa.UniqueConstraint("owner_user_id", "operation_key", name="uq_personal_draft_operation"),
        sa.UniqueConstraint("target_entry_id", name="uq_personal_draft_target"),
        sa.CheckConstraint("event_type IN ('evidence', 'proposed', 'accepted', 'discarded', 'receipt')", name="ck_personal_draft_type"),
        sa.CheckConstraint("(event_type IN ('accepted', 'discarded')) = (draft_ref IS NOT NULL)", name="ck_personal_draft_ref"),
        sa.CheckConstraint("(event_type = 'proposed') = (evidence_ref IS NOT NULL)", name="ck_personal_draft_evidence"),
        sa.CheckConstraint("(event_type = 'accepted') = (target_entry_id IS NOT NULL)", name="ck_personal_draft_target"),
    )
    op.create_index("ix_personal_draft_owner", "personal_draft_events", ["owner_user_id", "recorded_at", "id"])
    op.create_index("uq_personal_draft_terminal", "personal_draft_events", ["draft_ref"], unique=True,
                    postgresql_where=sa.text("event_type IN ('accepted', 'discarded')"))
    op.create_index("uq_personal_draft_evidence", "personal_draft_events", ["evidence_ref"], unique=True,
                    postgresql_where=sa.text("event_type = 'proposed'"))
    op.execute("""
        CREATE FUNCTION personal_draft_events_append_only() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' OR (TG_OP = 'DELETE' AND pg_trigger_depth() <= 1) THEN
                RAISE EXCEPTION 'personal_draft_events is append-only' USING ERRCODE = '23000';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER personal_draft_events_append_only BEFORE UPDATE OR DELETE
        ON personal_draft_events FOR EACH ROW EXECUTE FUNCTION personal_draft_events_append_only()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS personal_draft_events_append_only ON personal_draft_events")
    op.execute("DROP FUNCTION IF EXISTS personal_draft_events_append_only()")
    op.drop_table("personal_draft_events")
    op.drop_constraint("uq_personal_ledger_owner_id", "personal_ledger_entries", type_="unique")
