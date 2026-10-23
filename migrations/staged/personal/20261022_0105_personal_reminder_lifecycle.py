"""Owner-confirmed private in-app reminder lifecycle.

Append-only like 20261022_0003. ``draft_ref`` points at the owner's own ``proposed`` row.
Service checks and an insert guard enforce transitions from a proposed draft. Partial unique
indexes fence duplicate schedules, terminal outcomes and acknowledgements across workers.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20261022_0105"
down_revision = "20261022_0104"
branch_labels = None
depends_on = None

_TYPES = "'selection', 'proposed', 'scheduled', 'cancelled', 'delivered', 'invalidated', 'acknowledged'"
_LIFECYCLE = "'scheduled', 'cancelled', 'delivered', 'invalidated', 'acknowledged'"


def upgrade() -> None:
    op.add_column("personal_reminder_events", sa.Column("draft_ref", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_personal_reminder_owned_draft", "personal_reminder_events", "personal_reminder_events",
        ["owner_user_id", "draft_ref"], ["owner_user_id", "id"],
    )
    op.drop_constraint("ck_personal_reminder_type", "personal_reminder_events", type_="check")
    op.create_check_constraint("ck_personal_reminder_type", "personal_reminder_events", f"event_type IN ({_TYPES})")
    op.create_check_constraint(
        "ck_personal_reminder_draft", "personal_reminder_events",
        f"(event_type IN ({_LIFECYCLE})) = (draft_ref IS NOT NULL)",
    )
    op.create_index("uq_personal_reminder_scheduled", "personal_reminder_events", ["draft_ref"], unique=True,
                    postgresql_where=sa.text("event_type = 'scheduled'"))
    op.create_index("uq_personal_reminder_terminal", "personal_reminder_events", ["draft_ref"], unique=True,
                    postgresql_where=sa.text("event_type IN ('cancelled', 'delivered', 'invalidated')"))
    op.create_index("uq_personal_reminder_acknowledged", "personal_reminder_events", ["draft_ref"], unique=True,
                    postgresql_where=sa.text("event_type = 'acknowledged'"))
    op.execute("""
        CREATE FUNCTION personal_reminder_events_lifecycle_guard() RETURNS trigger AS $$
        DECLARE draft_type text; draft_candidate uuid; has_schedule boolean; has_terminal boolean;
        BEGIN
            IF NEW.event_type NOT IN ('scheduled', 'cancelled', 'delivered', 'invalidated', 'acknowledged') THEN
                RETURN NEW;
            END IF;
            SELECT event_type, candidate_entry_id INTO draft_type, draft_candidate
              FROM personal_reminder_events
             WHERE owner_user_id = NEW.owner_user_id AND id = NEW.draft_ref
             FOR UPDATE;
            IF NOT FOUND OR draft_type <> 'proposed' OR draft_candidate <> NEW.candidate_entry_id THEN
                RAISE EXCEPTION 'reminder lifecycle must reference the owner''s proposed draft'
                    USING ERRCODE = '23514';
            END IF;
            SELECT EXISTS (SELECT 1 FROM personal_reminder_events
                WHERE owner_user_id = NEW.owner_user_id AND draft_ref = NEW.draft_ref
                  AND event_type = 'scheduled') INTO has_schedule;
            SELECT EXISTS (SELECT 1 FROM personal_reminder_events
                WHERE owner_user_id = NEW.owner_user_id AND draft_ref = NEW.draft_ref
                  AND event_type IN ('cancelled', 'delivered', 'invalidated')) INTO has_terminal;
            IF NEW.event_type = 'scheduled' AND (has_schedule OR has_terminal) THEN
                RAISE EXCEPTION 'reminder schedule transition is invalid' USING ERRCODE = '23514';
            ELSIF NEW.event_type = 'cancelled' AND has_terminal THEN
                RAISE EXCEPTION 'reminder cancellation transition is invalid' USING ERRCODE = '23514';
            ELSIF NEW.event_type = 'invalidated' AND has_terminal THEN
                RAISE EXCEPTION 'reminder invalidation transition is invalid' USING ERRCODE = '23514';
            ELSIF NEW.event_type = 'delivered' AND (NOT has_schedule OR has_terminal) THEN
                RAISE EXCEPTION 'reminder delivery transition is invalid' USING ERRCODE = '23514';
            ELSIF NEW.event_type = 'acknowledged' AND NOT EXISTS (
                SELECT 1 FROM personal_reminder_events
                 WHERE owner_user_id = NEW.owner_user_id AND draft_ref = NEW.draft_ref
                   AND event_type = 'delivered'
            ) THEN
                RAISE EXCEPTION 'reminder acknowledgement requires delivery' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER personal_reminder_events_lifecycle_guard
        BEFORE INSERT ON personal_reminder_events FOR EACH ROW
        EXECUTE FUNCTION personal_reminder_events_lifecycle_guard()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS personal_reminder_events_lifecycle_guard ON personal_reminder_events")
    op.execute("DROP FUNCTION IF EXISTS personal_reminder_events_lifecycle_guard()")
    op.drop_index("uq_personal_reminder_acknowledged", table_name="personal_reminder_events")
    op.drop_index("uq_personal_reminder_terminal", table_name="personal_reminder_events")
    op.drop_index("uq_personal_reminder_scheduled", table_name="personal_reminder_events")
    op.drop_constraint("ck_personal_reminder_draft", "personal_reminder_events", type_="check")
    op.drop_constraint("ck_personal_reminder_type", "personal_reminder_events", type_="check")
    op.create_check_constraint("ck_personal_reminder_type", "personal_reminder_events",
                               "event_type IN ('selection', 'proposed')")
    op.drop_constraint("fk_personal_reminder_owned_draft", "personal_reminder_events", type_="foreignkey")
    op.drop_column("personal_reminder_events", "draft_ref")
