"""Persist the owner-scoped AoiDots root and bounded dialogue ledgers.

The ledger rows contain only identity, routing, control, and digest metadata.
Conversation text remains in the encrypted ``conversation_messages`` table.
The migration is additive and follows the repository's PostgreSQL/SQLite
rules; the preceding workspace-file migration remains the sole parent head.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "20261012_0002"
down_revision = "20261012_0001"
branch_labels = None
depends_on = None


def _dialect_name() -> str:
    try:
        return str(op.get_bind().dialect.name).lower()
    except (AttributeError, NameError, RuntimeError):
        return ""


def _is_sqlite() -> bool:
    return _dialect_name() == "sqlite"


def _uuid_type() -> sa.types.TypeEngine:
    return sa.String(length=36) if _is_sqlite() else postgresql.UUID(as_uuid=True)


def _table_exists(name: str) -> bool:
    try:
        return bool(inspect(op.get_bind()).has_table(name))
    except Exception:
        return False


def _indexes(name: str) -> set[str]:
    try:
        return {str(item["name"]) for item in inspect(op.get_bind()).get_indexes(name)}
    except Exception:
        return set()


def _create_index_if_missing(name: str, table: str, columns: list[str], *, unique: bool = False) -> None:
    if name not in _indexes(table):
        op.create_index(name, table, columns, unique=unique)


def _create_private_root() -> None:
    table = "agent_private_roots"
    if not _table_exists(table):
        op.create_table(
            table,
            sa.Column("id", _uuid_type(), nullable=False),
            sa.Column("owner_user_id", _uuid_type(), nullable=False),
            sa.Column("purpose", sa.String(length=64), nullable=False, server_default=sa.text("'aoidots'")),
            sa.Column("agent_id", _uuid_type(), nullable=False),
            sa.Column("session_id", _uuid_type(), nullable=False),
            sa.Column("state", sa.String(length=16), nullable=False, server_default=sa.text("'active'")),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["owner_user_id"], ["users.id"], name="fk_agent_private_roots_owner_user_id", ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["agent_id"], ["agents.id"], name="fk_agent_private_roots_agent_id", ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(
                ["session_id"], ["conversation_sessions.id"], name="fk_agent_private_roots_session_id", ondelete="RESTRICT"
            ),
            sa.UniqueConstraint("owner_user_id", "purpose", name="uq_agent_private_roots_owner_purpose"),
            sa.UniqueConstraint("agent_id", name="uq_agent_private_roots_agent"),
            sa.UniqueConstraint("session_id", name="uq_agent_private_roots_session"),
            sa.CheckConstraint("purpose IN ('aoidots')", name="ck_agent_private_roots_purpose"),
            sa.CheckConstraint("state IN ('active','disabled','deleted')", name="ck_agent_private_roots_state"),
        )
    _create_index_if_missing("ix_agent_private_roots_owner_user_id", table, ["owner_user_id"])
    _create_index_if_missing("ix_agent_private_roots_agent_id", table, ["agent_id"])
    _create_index_if_missing("ix_agent_private_roots_session_id", table, ["session_id"])
    _create_index_if_missing("ix_agent_private_roots_state", table, ["state"])
    _create_index_if_missing("ix_agent_private_roots_owner_state", table, ["owner_user_id", "state"])
    _create_index_if_missing("ix_agent_private_roots_created_at", table, ["created_at"])


def _create_dialogue() -> None:
    table = "agent_dialogues"
    if not _table_exists(table):
        op.create_table(
            table,
            sa.Column("id", _uuid_type(), nullable=False),
            sa.Column("owner_user_id", _uuid_type(), nullable=False),
            sa.Column("root_session_id", _uuid_type(), nullable=False),
            sa.Column("root_agent_id", _uuid_type(), nullable=False),
            sa.Column("root_agent_revision_id", _uuid_type(), nullable=False),
            sa.Column("worker_session_id", _uuid_type(), nullable=False),
            sa.Column("worker_agent_id", _uuid_type(), nullable=False),
            sa.Column("worker_agent_revision_id", _uuid_type(), nullable=False),
            sa.Column("project_id", _uuid_type(), nullable=True),
            sa.Column("task_id", _uuid_type(), nullable=True),
            sa.Column("human_message_id", _uuid_type(), nullable=True),
            sa.Column("human_run_id", _uuid_type(), nullable=True),
            sa.Column("human_work_item_id", _uuid_type(), nullable=True),
            sa.Column("authorization_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("authorization_hash", sa.String(length=64), nullable=False),
            sa.Column("control_revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("state", sa.String(length=16), nullable=False, server_default=sa.text("'queued'")),
            sa.Column("max_turns", sa.Integer(), nullable=False, server_default=sa.text("8")),
            sa.Column("turn_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("max_depth", sa.Integer(), nullable=False, server_default=sa.text("4")),
            sa.Column("deadline", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_agent_dialogues_owner_user_id", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["root_session_id"], ["conversation_sessions.id"], name="fk_agent_dialogues_root_session_id", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["root_agent_id"], ["agents.id"], name="fk_agent_dialogues_root_agent_id", ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["root_agent_revision_id"], ["agent_revisions.id"], name="fk_agent_dialogues_root_revision_id", ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["worker_session_id"], ["conversation_sessions.id"], name="fk_agent_dialogues_worker_session_id", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["worker_agent_id"], ["agents.id"], name="fk_agent_dialogues_worker_agent_id", ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["worker_agent_revision_id"], ["agent_revisions.id"], name="fk_agent_dialogues_worker_revision_id", ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_agent_dialogues_project_id", ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name="fk_agent_dialogues_task_id", ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["human_message_id"], ["conversation_messages.id"], name="fk_agent_dialogues_human_message_id", ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["human_run_id"], ["agent_runs.id"], name="fk_agent_dialogues_human_run_id", ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["human_work_item_id"], ["agent_work_items.id"], name="fk_agent_dialogues_human_work_item_id", ondelete="SET NULL"),
            sa.UniqueConstraint("worker_session_id", name="uq_agent_dialogues_worker_session_id"),
            sa.CheckConstraint("root_session_id <> worker_session_id", name="ck_agent_dialogues_distinct_sessions"),
            sa.CheckConstraint("length(authorization_hash) = 64", name="ck_agent_dialogues_authorization_hash"),
            sa.CheckConstraint("control_revision >= 0", name="ck_agent_dialogues_control_revision"),
            sa.CheckConstraint("state IN ('queued','running','waiting','succeeded','failed','cancelled','blocked','expired')", name="ck_agent_dialogues_state"),
            sa.CheckConstraint("max_turns BETWEEN 1 AND 100", name="ck_agent_dialogues_max_turns"),
            sa.CheckConstraint("turn_count BETWEEN 0 AND max_turns", name="ck_agent_dialogues_turn_count"),
            sa.CheckConstraint("max_depth BETWEEN 0 AND 32", name="ck_agent_dialogues_max_depth"),
        )
    _create_index_if_missing("ix_agent_dialogues_owner_user_id", table, ["owner_user_id"])
    _create_index_if_missing("ix_agent_dialogues_root_session_id", table, ["root_session_id"])
    _create_index_if_missing("ix_agent_dialogues_root_agent_id", table, ["root_agent_id"])
    _create_index_if_missing("ix_agent_dialogues_root_agent_revision_id", table, ["root_agent_revision_id"])
    _create_index_if_missing("ix_agent_dialogues_worker_session_id", table, ["worker_session_id"])
    _create_index_if_missing("ix_agent_dialogues_worker_agent_id", table, ["worker_agent_id"])
    _create_index_if_missing("ix_agent_dialogues_worker_agent_revision_id", table, ["worker_agent_revision_id"])
    _create_index_if_missing("ix_agent_dialogues_project_id", table, ["project_id"])
    _create_index_if_missing("ix_agent_dialogues_task_id", table, ["task_id"])
    _create_index_if_missing("ix_agent_dialogues_human_message_id", table, ["human_message_id"])
    _create_index_if_missing("ix_agent_dialogues_human_run_id", table, ["human_run_id"])
    _create_index_if_missing("ix_agent_dialogues_human_work_item_id", table, ["human_work_item_id"])
    _create_index_if_missing("ix_agent_dialogues_authorization_hash", table, ["authorization_hash"])
    _create_index_if_missing("ix_agent_dialogues_state", table, ["state"])
    _create_index_if_missing("ix_agent_dialogues_deadline", table, ["deadline"])
    _create_index_if_missing("ix_agent_dialogues_created_at", table, ["created_at"])
    _create_index_if_missing("ix_agent_dialogues_owner_state", table, ["owner_user_id", "state", "created_at"])
    _create_index_if_missing("ix_agent_dialogues_root_state", table, ["root_session_id", "state", "created_at"])


def _create_delivery() -> None:
    table = "agent_dialogue_deliveries"
    if not _table_exists(table):
        op.create_table(
            table,
            sa.Column("id", _uuid_type(), nullable=False),
            sa.Column("dialogue_id", _uuid_type(), nullable=False),
            sa.Column("source_session_id", _uuid_type(), nullable=False),
            sa.Column("source_agent_id", _uuid_type(), nullable=True),
            sa.Column("source_agent_revision_id", _uuid_type(), nullable=True),
            sa.Column("source_run_id", _uuid_type(), nullable=True),
            sa.Column("source_work_item_id", _uuid_type(), nullable=True),
            sa.Column("source_message_id", _uuid_type(), nullable=True),
            sa.Column("receiver_session_id", _uuid_type(), nullable=False),
            sa.Column("receiver_agent_id", _uuid_type(), nullable=True),
            sa.Column("receiver_agent_revision_id", _uuid_type(), nullable=True),
            sa.Column("receiver_run_id", _uuid_type(), nullable=True),
            sa.Column("receiver_work_item_id", _uuid_type(), nullable=True),
            sa.Column("receiver_message_id", _uuid_type(), nullable=True),
            sa.Column("transport_work_item_id", _uuid_type(), nullable=True),
            sa.Column("kind", sa.String(length=32), nullable=False, server_default=sa.text("'request'")),
            sa.Column("state", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
            sa.Column("sequence", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("causation_id", sa.String(length=255), nullable=True),
            sa.Column("depth", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("expected_control_revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("idempotency_key", sa.String(length=255), nullable=False),
            sa.Column("request_digest", sa.String(length=64), nullable=False),
            sa.Column("response_digest", sa.String(length=64), nullable=True),
            sa.Column("available_at", sa.DateTime(), nullable=True),
            sa.Column("deadline", sa.DateTime(), nullable=True),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["dialogue_id"], ["agent_dialogues.id"], name="fk_agent_dialogue_deliveries_dialogue_id", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["source_session_id"], ["conversation_sessions.id"], name="fk_agent_dialogue_deliveries_source_session_id", ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["source_agent_id"], ["agents.id"], name="fk_agent_dialogue_deliveries_source_agent_id", ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["source_agent_revision_id"], ["agent_revisions.id"], name="fk_agent_dialogue_deliveries_source_revision_id", ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["source_run_id"], ["agent_runs.id"], name="fk_agent_dialogue_deliveries_source_run_id", ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["source_work_item_id"], ["agent_work_items.id"], name="fk_agent_dialogue_deliveries_source_work_item_id", ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["source_message_id"], ["conversation_messages.id"], name="fk_agent_dialogue_deliveries_source_message_id", ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["receiver_session_id"], ["conversation_sessions.id"], name="fk_agent_dialogue_deliveries_receiver_session_id", ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["receiver_agent_id"], ["agents.id"], name="fk_agent_dialogue_deliveries_receiver_agent_id", ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["receiver_agent_revision_id"], ["agent_revisions.id"], name="fk_agent_dialogue_deliveries_receiver_revision_id", ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["receiver_run_id"], ["agent_runs.id"], name="fk_agent_dialogue_deliveries_receiver_run_id", ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["receiver_work_item_id"], ["agent_work_items.id"], name="fk_agent_dialogue_deliveries_receiver_work_item_id", ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["receiver_message_id"], ["conversation_messages.id"], name="fk_agent_dialogue_deliveries_receiver_message_id", ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["transport_work_item_id"], ["agent_work_items.id"], name="fk_agent_dialogue_deliveries_transport_work_item_id", ondelete="SET NULL"),
            sa.UniqueConstraint("dialogue_id", "idempotency_key", name="uq_agent_dialogue_deliveries_idempotency"),
            sa.UniqueConstraint("dialogue_id", "sequence", name="uq_agent_dialogue_deliveries_sequence"),
            sa.CheckConstraint("kind IN ('request','response','control','event')", name="ck_agent_dialogue_deliveries_kind"),
            sa.CheckConstraint("state IN ('pending','available','leased','delivered','acked','failed','cancelled','expired')", name="ck_agent_dialogue_deliveries_state"),
            sa.CheckConstraint("sequence >= 0", name="ck_agent_dialogue_deliveries_sequence"),
            sa.CheckConstraint("depth BETWEEN 0 AND 32", name="ck_agent_dialogue_deliveries_depth"),
            sa.CheckConstraint("expected_control_revision >= 0", name="ck_agent_dialogue_deliveries_control_revision"),
            sa.CheckConstraint("attempt_count BETWEEN 0 AND 100", name="ck_agent_dialogue_deliveries_attempt_count"),
            sa.CheckConstraint("length(idempotency_key) BETWEEN 1 AND 255", name="ck_agent_dialogue_deliveries_idempotency_key"),
            sa.CheckConstraint("length(request_digest) = 64", name="ck_agent_dialogue_deliveries_request_digest"),
            sa.CheckConstraint("response_digest IS NULL OR length(response_digest) = 64", name="ck_agent_dialogue_deliveries_response_digest"),
            sa.CheckConstraint("(source_agent_id IS NULL AND source_agent_revision_id IS NULL) OR (source_agent_id IS NOT NULL AND source_agent_revision_id IS NOT NULL)", name="ck_agent_dialogue_deliveries_source_agent_pair"),
            sa.CheckConstraint("(receiver_agent_id IS NULL AND receiver_agent_revision_id IS NULL) OR (receiver_agent_id IS NOT NULL AND receiver_agent_revision_id IS NOT NULL)", name="ck_agent_dialogue_deliveries_receiver_agent_pair"),
        )
    columns = [
        "dialogue_id", "source_session_id", "source_agent_id", "source_agent_revision_id", "source_run_id",
        "source_work_item_id", "source_message_id", "receiver_session_id", "receiver_agent_id",
        "receiver_agent_revision_id", "receiver_run_id", "receiver_work_item_id", "receiver_message_id",
        "transport_work_item_id", "kind", "state", "sequence", "causation_id", "expected_control_revision",
        "idempotency_key", "request_digest", "response_digest", "available_at", "deadline", "attempt_count", "created_at",
    ]
    for column in columns:
        _create_index_if_missing(f"ix_agent_dialogue_deliveries_{column}", table, [column])
    _create_index_if_missing("ix_agent_dialogue_deliveries_receiver_state", table, ["receiver_session_id", "state", "available_at"])


def _create_input() -> None:
    table = "agent_dialogue_inputs"
    if not _table_exists(table):
        op.create_table(
            table,
            sa.Column("id", _uuid_type(), nullable=False),
            sa.Column("dialogue_id", _uuid_type(), nullable=False),
            sa.Column("delivery_id", _uuid_type(), nullable=True),
            sa.Column("source_run_id", _uuid_type(), nullable=True),
            sa.Column("source_work_item_id", _uuid_type(), nullable=True),
            sa.Column("source_message_id", _uuid_type(), nullable=True),
            sa.Column("input_kind", sa.String(length=32), nullable=False, server_default=sa.text("'agent_question'")),
            sa.Column("reference_id", sa.String(length=255), nullable=True),
            sa.Column("expected_control_revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("state", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")),
            sa.Column("answer_message_id", _uuid_type(), nullable=True),
            sa.Column("answer_digest", sa.String(length=64), nullable=True),
            sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("deadline", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["dialogue_id"], ["agent_dialogues.id"], name="fk_agent_dialogue_inputs_dialogue_id", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["delivery_id"], ["agent_dialogue_deliveries.id"], name="fk_agent_dialogue_inputs_delivery_id", ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["source_run_id"], ["agent_runs.id"], name="fk_agent_dialogue_inputs_source_run_id", ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["source_work_item_id"], ["agent_work_items.id"], name="fk_agent_dialogue_inputs_source_work_item_id", ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["source_message_id"], ["conversation_messages.id"], name="fk_agent_dialogue_inputs_source_message_id", ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["answer_message_id"], ["conversation_messages.id"], name="fk_agent_dialogue_inputs_answer_message_id", ondelete="SET NULL"),
            sa.UniqueConstraint("dialogue_id", "revision", name="uq_agent_dialogue_inputs_revision"),
            sa.UniqueConstraint("dialogue_id", "delivery_id", name="uq_agent_dialogue_inputs_delivery"),
            sa.CheckConstraint("input_kind IN ('agent_question','human_question')", name="ck_agent_dialogue_inputs_kind"),
            sa.CheckConstraint("state IN ('pending','answered','invalidated','blocked','expired','cancelled')", name="ck_agent_dialogue_inputs_state"),
            sa.CheckConstraint("expected_control_revision >= 0", name="ck_agent_dialogue_inputs_control_revision"),
            sa.CheckConstraint("revision BETWEEN 1 AND 1000", name="ck_agent_dialogue_inputs_revision"),
            sa.CheckConstraint("answer_digest IS NULL OR length(answer_digest) = 64", name="ck_agent_dialogue_inputs_answer_digest"),
        )
    columns = [
        "dialogue_id", "delivery_id", "source_run_id", "source_work_item_id", "source_message_id", "input_kind",
        "reference_id", "expected_control_revision", "state", "answer_message_id", "answer_digest", "revision", "deadline", "created_at",
    ]
    for column in columns:
        _create_index_if_missing(f"ix_agent_dialogue_inputs_{column}", table, [column])
    _create_index_if_missing("ix_agent_dialogue_inputs_state_deadline", table, ["state", "deadline"])


def _create_immutable_guards() -> None:
    if _is_sqlite():
        for trigger in (
            "agent_dialogues_immutable_identity_update",
            "agent_dialogue_deliveries_immutable_refs_update",
            "agent_dialogue_inputs_immutable_refs_update",
        ):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger}"))
        op.execute(sa.text("""
            CREATE TRIGGER agent_dialogues_immutable_identity_update
            BEFORE UPDATE ON agent_dialogues
            BEGIN
                SELECT CASE WHEN
                    NEW.id IS NOT OLD.id OR NEW.owner_user_id IS NOT OLD.owner_user_id OR
                    NEW.root_session_id IS NOT OLD.root_session_id OR NEW.root_agent_id IS NOT OLD.root_agent_id OR
                    NEW.root_agent_revision_id IS NOT OLD.root_agent_revision_id OR NEW.worker_session_id IS NOT OLD.worker_session_id OR
                    NEW.worker_agent_id IS NOT OLD.worker_agent_id OR NEW.worker_agent_revision_id IS NOT OLD.worker_agent_revision_id OR
                    NEW.project_id IS NOT OLD.project_id OR NEW.task_id IS NOT OLD.task_id OR
                    NEW.human_message_id IS NOT OLD.human_message_id OR NEW.human_run_id IS NOT OLD.human_run_id OR
                    NEW.human_work_item_id IS NOT OLD.human_work_item_id OR NEW.authorization_json IS NOT OLD.authorization_json OR
                    NEW.authorization_hash IS NOT OLD.authorization_hash OR NEW.max_turns IS NOT OLD.max_turns OR
                    NEW.max_depth IS NOT OLD.max_depth OR NEW.created_at IS NOT OLD.created_at
                THEN RAISE(ABORT, 'agent_dialogue_identity_immutable') END;
            END
        """))
        op.execute(sa.text("""
            CREATE TRIGGER agent_dialogue_deliveries_immutable_refs_update
            BEFORE UPDATE ON agent_dialogue_deliveries
            BEGIN
                SELECT CASE WHEN
                    NEW.id IS NOT OLD.id OR NEW.dialogue_id IS NOT OLD.dialogue_id OR
                    NEW.source_session_id IS NOT OLD.source_session_id OR NEW.source_agent_id IS NOT OLD.source_agent_id OR
                    NEW.source_agent_revision_id IS NOT OLD.source_agent_revision_id OR NEW.source_run_id IS NOT OLD.source_run_id OR
                    NEW.source_work_item_id IS NOT OLD.source_work_item_id OR NEW.source_message_id IS NOT OLD.source_message_id OR
                    NEW.receiver_session_id IS NOT OLD.receiver_session_id OR NEW.receiver_agent_id IS NOT OLD.receiver_agent_id OR
                    NEW.receiver_agent_revision_id IS NOT OLD.receiver_agent_revision_id OR NEW.kind IS NOT OLD.kind OR
                    NEW.sequence IS NOT OLD.sequence OR NEW.causation_id IS NOT OLD.causation_id OR NEW.depth IS NOT OLD.depth OR
                    NEW.expected_control_revision IS NOT OLD.expected_control_revision OR NEW.idempotency_key IS NOT OLD.idempotency_key OR
                    NEW.request_digest IS NOT OLD.request_digest OR NEW.created_at IS NOT OLD.created_at
                THEN RAISE(ABORT, 'agent_dialogue_delivery_refs_immutable') END;
            END
        """))
        op.execute(sa.text("""
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
        """))
        return
    op.execute(sa.text("DROP TRIGGER IF EXISTS agent_dialogues_immutable_identity_update ON agent_dialogues"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS agent_dialogue_deliveries_immutable_refs_update ON agent_dialogue_deliveries"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS agent_dialogue_inputs_immutable_refs_update ON agent_dialogue_inputs"))
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION agent_dialogue_immutable_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id OR NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
               OR NEW.root_session_id IS DISTINCT FROM OLD.root_session_id OR NEW.root_agent_id IS DISTINCT FROM OLD.root_agent_id
               OR NEW.root_agent_revision_id IS DISTINCT FROM OLD.root_agent_revision_id OR NEW.worker_session_id IS DISTINCT FROM OLD.worker_session_id
               OR NEW.worker_agent_id IS DISTINCT FROM OLD.worker_agent_id OR NEW.worker_agent_revision_id IS DISTINCT FROM OLD.worker_agent_revision_id
               OR NEW.project_id IS DISTINCT FROM OLD.project_id OR NEW.task_id IS DISTINCT FROM OLD.task_id
               OR NEW.human_message_id IS DISTINCT FROM OLD.human_message_id OR NEW.human_run_id IS DISTINCT FROM OLD.human_run_id
               OR NEW.human_work_item_id IS DISTINCT FROM OLD.human_work_item_id OR to_jsonb(NEW.authorization_json) IS DISTINCT FROM to_jsonb(OLD.authorization_json)
               OR NEW.authorization_hash IS DISTINCT FROM OLD.authorization_hash OR NEW.max_turns IS DISTINCT FROM OLD.max_turns
               OR NEW.max_depth IS DISTINCT FROM OLD.max_depth OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'agent_dialogue_identity_immutable';
            END IF;
            RETURN NEW;
        END; $$;
        CREATE TRIGGER agent_dialogues_immutable_identity_update BEFORE UPDATE ON agent_dialogues FOR EACH ROW EXECUTE FUNCTION agent_dialogue_immutable_guard();
    """))
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION agent_dialogue_delivery_refs_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id OR NEW.dialogue_id IS DISTINCT FROM OLD.dialogue_id
               OR NEW.source_session_id IS DISTINCT FROM OLD.source_session_id OR NEW.source_agent_id IS DISTINCT FROM OLD.source_agent_id
               OR NEW.source_agent_revision_id IS DISTINCT FROM OLD.source_agent_revision_id OR NEW.source_run_id IS DISTINCT FROM OLD.source_run_id
               OR NEW.source_work_item_id IS DISTINCT FROM OLD.source_work_item_id OR NEW.source_message_id IS DISTINCT FROM OLD.source_message_id
               OR NEW.receiver_session_id IS DISTINCT FROM OLD.receiver_session_id OR NEW.receiver_agent_id IS DISTINCT FROM OLD.receiver_agent_id
               OR NEW.receiver_agent_revision_id IS DISTINCT FROM OLD.receiver_agent_revision_id OR NEW.kind IS DISTINCT FROM OLD.kind
               OR NEW.sequence IS DISTINCT FROM OLD.sequence OR NEW.causation_id IS DISTINCT FROM OLD.causation_id
               OR NEW.depth IS DISTINCT FROM OLD.depth OR NEW.expected_control_revision IS DISTINCT FROM OLD.expected_control_revision
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key OR NEW.request_digest IS DISTINCT FROM OLD.request_digest
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'agent_dialogue_delivery_refs_immutable';
            END IF;
            RETURN NEW;
        END; $$;
        CREATE TRIGGER agent_dialogue_deliveries_immutable_refs_update BEFORE UPDATE ON agent_dialogue_deliveries FOR EACH ROW EXECUTE FUNCTION agent_dialogue_delivery_refs_guard();
    """))
    op.execute(sa.text("""
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
        CREATE TRIGGER agent_dialogue_inputs_immutable_refs_update BEFORE UPDATE ON agent_dialogue_inputs FOR EACH ROW EXECUTE FUNCTION agent_dialogue_input_refs_guard();
    """))


def _drop_immutable_guards() -> None:
    if _is_sqlite():
        for table, trigger in (
            ("agent_dialogues", "agent_dialogues_immutable_identity_update"),
            ("agent_dialogue_deliveries", "agent_dialogue_deliveries_immutable_refs_update"),
            ("agent_dialogue_inputs", "agent_dialogue_inputs_immutable_refs_update"),
        ):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger}"))
        return
    op.execute(sa.text("DROP TRIGGER IF EXISTS agent_dialogues_immutable_identity_update ON agent_dialogues"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS agent_dialogue_deliveries_immutable_refs_update ON agent_dialogue_deliveries"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS agent_dialogue_inputs_immutable_refs_update ON agent_dialogue_inputs"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS agent_dialogue_immutable_guard()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS agent_dialogue_delivery_refs_guard()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS agent_dialogue_input_refs_guard()"))


def upgrade() -> None:
    _create_private_root()
    _create_dialogue()
    _create_delivery()
    _create_input()
    _create_immutable_guards()


def downgrade() -> None:
    _drop_immutable_guards()
    for table in ("agent_dialogue_inputs", "agent_dialogue_deliveries", "agent_dialogues", "agent_private_roots"):
        if _table_exists(table):
            for index_name in _indexes(table):
                if index_name.startswith(f"ix_{table}_"):
                    op.drop_index(index_name, table_name=table)
            op.drop_table(table)
