"""Allow an agent question Input to become a human escalation."""

from alembic import op
import sqlalchemy as sa


revision = "20261030_0003"
down_revision = "20261030_0002"
branch_labels = None
depends_on = None


PG_ESCALATION_FUNCTION = """
        CREATE OR REPLACE FUNCTION agent_dialogue_input_refs_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id OR NEW.dialogue_id IS DISTINCT FROM OLD.dialogue_id OR NEW.delivery_id IS DISTINCT FROM OLD.delivery_id
               OR NEW.source_run_id IS DISTINCT FROM OLD.source_run_id OR NEW.source_work_item_id IS DISTINCT FROM OLD.source_work_item_id
               OR NEW.source_message_id IS DISTINCT FROM OLD.source_message_id
               OR (NEW.input_kind IS DISTINCT FROM OLD.input_kind
                   AND NOT (OLD.input_kind = 'agent_question' AND NEW.input_kind = 'human_question'))
               OR NEW.reference_id IS DISTINCT FROM OLD.reference_id OR NEW.expected_control_revision IS DISTINCT FROM OLD.expected_control_revision
               OR NEW.revision IS DISTINCT FROM OLD.revision OR NEW.deadline IS DISTINCT FROM OLD.deadline OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'agent_dialogue_input_refs_immutable';
            END IF;
            RETURN NEW;
        END; $$;
"""

PG_STRICT_FUNCTION = """
        CREATE OR REPLACE FUNCTION agent_dialogue_input_refs_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id OR NEW.dialogue_id IS DISTINCT FROM OLD.dialogue_id OR NEW.delivery_id IS DISTINCT FROM OLD.delivery_id
               OR NEW.source_run_id IS DISTINCT FROM OLD.source_run_id OR NEW.source_work_item_id IS DISTINCT FROM OLD.source_work_item_id
               OR NEW.source_message_id IS DISTINCT FROM OLD.source_message_id OR NEW.input_kind IS DISTINCT FROM OLD.input_kind
               OR NEW.reference_id IS DISTINCT FROM OLD.reference_id OR NEW.expected_control_revision IS DISTINCT FROM OLD.expected_control_revision
               OR NEW.revision IS DISTINCT FROM OLD.revision OR NEW.deadline IS DISTINCT FROM OLD.deadline OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'agent_dialogue_input_refs_immutable';
            END IF;
            RETURN NEW;
        END; $$;
"""

SQLITE_DROP = "DROP TRIGGER IF EXISTS agent_dialogue_inputs_immutable_refs_update"

SQLITE_ESCALATION_TRIGGER = """
            CREATE TRIGGER agent_dialogue_inputs_immutable_refs_update
            BEFORE UPDATE ON agent_dialogue_inputs
            BEGIN
                SELECT CASE WHEN
                    NEW.id IS NOT OLD.id OR NEW.dialogue_id IS NOT OLD.dialogue_id OR NEW.delivery_id IS NOT OLD.delivery_id OR
                    NEW.source_run_id IS NOT OLD.source_run_id OR NEW.source_work_item_id IS NOT OLD.source_work_item_id OR
                    NEW.source_message_id IS NOT OLD.source_message_id OR
                    (NEW.input_kind IS NOT OLD.input_kind AND NOT
                        (OLD.input_kind = 'agent_question' AND NEW.input_kind = 'human_question')) OR
                    NEW.reference_id IS NOT OLD.reference_id OR NEW.expected_control_revision IS NOT OLD.expected_control_revision OR
                    NEW.revision IS NOT OLD.revision OR NEW.deadline IS NOT OLD.deadline OR NEW.created_at IS NOT OLD.created_at
                THEN RAISE(ABORT, 'agent_dialogue_input_refs_immutable') END;
            END
"""

SQLITE_STRICT_TRIGGER = """
            CREATE TRIGGER agent_dialogue_inputs_immutable_refs_update
            BEFORE UPDATE ON agent_dialogue_inputs
            BEGIN
                SELECT CASE WHEN
                    NEW.id IS NOT OLD.id OR NEW.dialogue_id IS NOT OLD.dialogue_id OR NEW.delivery_id IS NOT OLD.delivery_id OR
                    NEW.source_run_id IS NOT OLD.source_run_id OR NEW.source_work_item_id IS NOT OLD.source_work_item_id OR
                    NEW.source_message_id IS NOT OLD.source_message_id OR NEW.input_kind IS NOT OLD.input_kind OR
                    NEW.reference_id IS NOT OLD.reference_id OR NEW.expected_control_revision IS NOT OLD.expected_control_revision OR
                    NEW.revision IS NOT OLD.revision OR NEW.deadline IS NOT OLD.deadline OR NEW.created_at IS NOT OLD.created_at
                THEN RAISE(ABORT, 'agent_dialogue_input_refs_immutable') END;
            END
"""


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _has_table() -> bool:
    return sa.inspect(op.get_bind()).has_table("agent_dialogues")


def upgrade() -> None:
    if not _has_table():
        return
    if _is_sqlite():
        op.execute(sa.text(SQLITE_DROP))
        op.execute(sa.text(SQLITE_ESCALATION_TRIGGER))
        return
    op.execute(sa.text(PG_ESCALATION_FUNCTION))


def downgrade() -> None:
    if not _has_table():
        return
    if _is_sqlite():
        op.execute(sa.text(SQLITE_DROP))
        op.execute(sa.text(SQLITE_STRICT_TRIGGER))
        return
    op.execute(sa.text(PG_STRICT_FUNCTION))
