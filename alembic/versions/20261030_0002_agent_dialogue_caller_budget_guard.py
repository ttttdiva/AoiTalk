"""Let the AgentDialogue guard accept caller_run_budget settlement.

``delegate`` keeps the caller Run's token/cost reservation of a Dialogue in
``authorization_json["caller_run_budget"]`` and settles it (state, usage and the
rewritten ``authorization_hash``) when the Dialogue ends.  The identity guard
rejected every change of ``authorization_json``/``authorization_hash`` with
``agent_dialogue_identity_immutable``, so the settlement - which runs inside
``GET /api/agent-dialogues/{id}`` - failed with HTTP 500 and the reservation was
never released.

The guard now allows exactly that: ``authorization_json`` may change only in its
``caller_run_budget`` key, and ``authorization_hash`` may change only together
with such a change.  Every other identity column stays immutable.
"""

from alembic import op
import sqlalchemy as sa


revision = "20261030_0002"
down_revision = "20261030_0001"
branch_labels = None
depends_on = None


_IDENTITY_PG = """
               NEW.id IS DISTINCT FROM OLD.id OR NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
               OR NEW.root_session_id IS DISTINCT FROM OLD.root_session_id OR NEW.root_agent_id IS DISTINCT FROM OLD.root_agent_id
               OR NEW.root_agent_revision_id IS DISTINCT FROM OLD.root_agent_revision_id OR NEW.worker_session_id IS DISTINCT FROM OLD.worker_session_id
               OR NEW.worker_agent_id IS DISTINCT FROM OLD.worker_agent_id OR NEW.worker_agent_revision_id IS DISTINCT FROM OLD.worker_agent_revision_id
               OR NEW.project_id IS DISTINCT FROM OLD.project_id OR NEW.task_id IS DISTINCT FROM OLD.task_id
               OR NEW.human_message_id IS DISTINCT FROM OLD.human_message_id OR NEW.human_run_id IS DISTINCT FROM OLD.human_run_id
               OR NEW.human_work_item_id IS DISTINCT FROM OLD.human_work_item_id
               OR NEW.max_turns IS DISTINCT FROM OLD.max_turns
               OR NEW.max_depth IS DISTINCT FROM OLD.max_depth OR NEW.created_at IS DISTINCT FROM OLD.created_at
"""

PG_STRICT_FUNCTION = f"""
        CREATE OR REPLACE FUNCTION agent_dialogue_immutable_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF {_IDENTITY_PG}
               OR to_jsonb(NEW.authorization_json) IS DISTINCT FROM to_jsonb(OLD.authorization_json)
               OR NEW.authorization_hash IS DISTINCT FROM OLD.authorization_hash THEN
                RAISE EXCEPTION 'agent_dialogue_identity_immutable';
            END IF;
            RETURN NEW;
        END; $$;
"""

PG_BUDGET_FUNCTION = f"""
        CREATE OR REPLACE FUNCTION agent_dialogue_immutable_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF {_IDENTITY_PG}
               OR (to_jsonb(NEW.authorization_json) - 'caller_run_budget')
                  IS DISTINCT FROM (to_jsonb(OLD.authorization_json) - 'caller_run_budget')
               OR (NEW.authorization_hash IS DISTINCT FROM OLD.authorization_hash
                   AND to_jsonb(NEW.authorization_json) IS NOT DISTINCT FROM to_jsonb(OLD.authorization_json)) THEN
                RAISE EXCEPTION 'agent_dialogue_identity_immutable';
            END IF;
            RETURN NEW;
        END; $$;
"""

_IDENTITY_SQLITE = """
                    NEW.id IS NOT OLD.id OR NEW.owner_user_id IS NOT OLD.owner_user_id OR
                    NEW.root_session_id IS NOT OLD.root_session_id OR NEW.root_agent_id IS NOT OLD.root_agent_id OR
                    NEW.root_agent_revision_id IS NOT OLD.root_agent_revision_id OR NEW.worker_session_id IS NOT OLD.worker_session_id OR
                    NEW.worker_agent_id IS NOT OLD.worker_agent_id OR NEW.worker_agent_revision_id IS NOT OLD.worker_agent_revision_id OR
                    NEW.project_id IS NOT OLD.project_id OR NEW.task_id IS NOT OLD.task_id OR
                    NEW.human_message_id IS NOT OLD.human_message_id OR NEW.human_run_id IS NOT OLD.human_run_id OR
                    NEW.human_work_item_id IS NOT OLD.human_work_item_id OR
                    NEW.max_turns IS NOT OLD.max_turns OR
                    NEW.max_depth IS NOT OLD.max_depth OR NEW.created_at IS NOT OLD.created_at
"""

SQLITE_DROP = "DROP TRIGGER IF EXISTS agent_dialogues_immutable_identity_update"

SQLITE_STRICT_TRIGGER = f"""
            CREATE TRIGGER agent_dialogues_immutable_identity_update
            BEFORE UPDATE ON agent_dialogues
            BEGIN
                SELECT CASE WHEN {_IDENTITY_SQLITE}
                    OR NEW.authorization_json IS NOT OLD.authorization_json
                    OR NEW.authorization_hash IS NOT OLD.authorization_hash
                THEN RAISE(ABORT, 'agent_dialogue_identity_immutable') END;
            END
"""

SQLITE_BUDGET_TRIGGER = f"""
            CREATE TRIGGER agent_dialogues_immutable_identity_update
            BEFORE UPDATE ON agent_dialogues
            BEGIN
                SELECT CASE WHEN {_IDENTITY_SQLITE}
                    OR json_remove(NEW.authorization_json, '$.caller_run_budget')
                       IS NOT json_remove(OLD.authorization_json, '$.caller_run_budget')
                    OR (NEW.authorization_hash IS NOT OLD.authorization_hash
                        AND NEW.authorization_json IS OLD.authorization_json)
                THEN RAISE(ABORT, 'agent_dialogue_identity_immutable') END;
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
        op.execute(sa.text(SQLITE_BUDGET_TRIGGER))
        return
    op.execute(sa.text(PG_BUDGET_FUNCTION))


def downgrade() -> None:
    if not _has_table():
        return
    if _is_sqlite():
        op.execute(sa.text(SQLITE_DROP))
        op.execute(sa.text(SQLITE_STRICT_TRIGGER))
        return
    op.execute(sa.text(PG_STRICT_FUNCTION))
