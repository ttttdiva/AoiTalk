"""Add the common durable AgentWork persistence foundation.

The migration intentionally introduces only generic work envelopes and their
bounded lifecycle evidence.  Domain tables remain the source of truth; a
WorkSource materializes rows using the source identity tuple and the
coordinator owns claims/leases in a later runtime layer.

The new links on ``agent_runs`` and ``external_actions`` are nullable and
therefore preserve historical human/conversation rows.  SQLite migrations use
batch table recreation with foreign keys temporarily disabled because
``agent_runs`` contains self/child references and ``external_actions`` has
approval/attempt dependants.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "20260903_0002"
down_revision = "20260903_0001"
branch_labels = None
depends_on = None

_WORK_TABLE = "agent_work_items"
_EVENT_TABLE = "agent_work_events"


def _dialect_name() -> str:
    try:
        bind = op.get_bind()
    except (AttributeError, NameError, RuntimeError):
        return ""
    return str(getattr(getattr(bind, "dialect", None), "name", "")).lower()


def _is_sqlite() -> bool:
    return _dialect_name() == "sqlite"


def _uuid_type() -> sa.types.TypeEngine:
    # SQLite fixtures store UUIDs as canonical text.  Production PostgreSQL
    # keeps native UUID columns and referential integrity.
    return sa.String(length=36) if _is_sqlite() else postgresql.UUID(as_uuid=True)


def _column_copy(column: sa.Column) -> sa.Column:
    return column.copy()


def _table_exists(table_name: str) -> bool:
    try:
        return inspect(op.get_bind()).has_table(table_name)
    except Exception:
        return False


def _table_columns(table_name: str) -> set[str]:
    try:
        return {item["name"] for item in inspect(op.get_bind()).get_columns(table_name)}
    except Exception:
        return set()


def _table_has_columns(table_name: str, columns: tuple[str, ...]) -> bool:
    return bool(_table_exists(table_name)) and set(columns) <= _table_columns(table_name)


def _indexes(table_name: str) -> set[str]:
    try:
        return {item["name"] for item in inspect(op.get_bind()).get_indexes(table_name)}
    except Exception:
        return set()


def _drop_index_if_present(name: str, table_name: str) -> None:
    if name in _indexes(table_name):
        op.drop_index(name, table_name=table_name)


@contextmanager
def _sqlite_batch_foreign_keys() -> Iterator[None]:
    """Permit SQLite batch recreation of tables with existing dependants."""

    if not _is_sqlite():
        yield
        return
    bind = op.get_bind()
    try:
        enabled = bool(bind.exec_driver_sql("PRAGMA foreign_keys").scalar())
    except Exception:
        enabled = False
    if not enabled:
        yield
        return
    raw = getattr(bind, "connection", None)
    if raw is None:
        yield
        return
    # SQLite only changes PRAGMA foreign_keys outside a transaction.  Commit
    # the small DDL unit and restore the setting after the batch operation.
    raw.commit()
    raw.execute("PRAGMA foreign_keys=OFF")
    try:
        yield
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.execute("PRAGMA foreign_keys=ON")


def _create_work_items() -> None:
    op.create_table(
        _WORK_TABLE,
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=255), nullable=False),
        sa.Column("source_revision", sa.String(length=128), nullable=False),
        sa.Column("intent_key", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=64), nullable=False),
        sa.Column("space_id", _uuid_type(), nullable=True),
        sa.Column("project_id", _uuid_type(), nullable=True),
        sa.Column("task_id", _uuid_type(), nullable=True),
        sa.Column("persona_id", _uuid_type(), nullable=True),
        sa.Column("app_id", _uuid_type(), nullable=True),
        sa.Column("assigned_agent_id", _uuid_type(), nullable=True),
        sa.Column("agent_revision_id", _uuid_type(), nullable=True),
        sa.Column(
            "required_capabilities",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "execution_adapter",
            sa.String(length=128),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("'0'")),
        sa.Column("not_before", sa.DateTime(), nullable=True),
        sa.Column("deadline", sa.DateTime(), nullable=True),
        sa.Column(
            "state",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("'0'")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("'3'")),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("concurrency_key", sa.String(length=255), nullable=True),
        sa.Column(
            "budget_reservation",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("root_work_item_id", _uuid_type(), nullable=True),
        sa.Column("parent_work_item_id", _uuid_type(), nullable=True),
        sa.Column("causation_id", sa.String(length=255), nullable=True),
        sa.Column("causal_depth", sa.Integer(), nullable=False, server_default=sa.text("'0'")),
        sa.Column("mutation_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("outcome_classification", sa.String(length=32), nullable=True),
        sa.Column("blocker_code", sa.String(length=128), nullable=True),
        sa.Column("escalation_reason", sa.String(length=512), nullable=True),
        sa.Column("safe_error_code", sa.String(length=96), nullable=True),
        sa.Column("result_summary", sa.String(length=2048), nullable=True),
        sa.Column("result_summary_json", sa.JSON(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("active_agent_run_id", _uuid_type(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_type",
            "source_id",
            "source_revision",
            "intent_key",
            name="uq_agent_work_items_source_identity",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"], ["spaces.id"], name="fk_agent_work_items_space_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="fk_agent_work_items_project_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_agent_work_items_task_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["persona_id"], ["media_personas.id"], name="fk_agent_work_items_persona_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["app_id"], ["apps.id"], name="fk_agent_work_items_app_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["assigned_agent_id"], ["agents.id"], name="fk_agent_work_items_assigned_agent_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["agent_revision_id"], ["agent_revisions.id"], name="fk_agent_work_items_agent_revision_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["root_work_item_id"], [_WORK_TABLE + ".id"], name="fk_agent_work_items_root_work_item_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["parent_work_item_id"], [_WORK_TABLE + ".id"], name="fk_agent_work_items_parent_work_item_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["active_agent_run_id"], ["agent_runs.id"], name="fk_agent_work_items_active_agent_run_id", ondelete="SET NULL"
        ),
        sa.CheckConstraint("length(source_type) BETWEEN 1 AND 64", name="ck_agent_work_items_source_type"),
        sa.CheckConstraint("length(source_id) BETWEEN 1 AND 255", name="ck_agent_work_items_source_id"),
        sa.CheckConstraint("length(source_revision) BETWEEN 1 AND 128", name="ck_agent_work_items_source_revision"),
        sa.CheckConstraint("length(intent_key) BETWEEN 1 AND 255", name="ck_agent_work_items_intent_key"),
        sa.CheckConstraint("length(domain) BETWEEN 1 AND 64", name="ck_agent_work_items_domain"),
        sa.CheckConstraint(
            "state IN ('pending','claimed','running','awaiting_approval','blocked','retry_wait','uncertain','succeeded','failed','cancelled','dead_letter')",
            name="ck_agent_work_items_state",
        ),
        sa.CheckConstraint("attempt_count >= 0 AND attempt_count <= 1000000", name="ck_agent_work_items_attempt_count"),
        sa.CheckConstraint("max_attempts > 0 AND max_attempts <= 1000000", name="ck_agent_work_items_max_attempts"),
        sa.CheckConstraint("causal_depth >= 0 AND causal_depth <= 64", name="ck_agent_work_items_causal_depth"),
        sa.CheckConstraint(
            "mutation_fingerprint IS NULL OR length(mutation_fingerprint) = 64",
            name="ck_agent_work_items_mutation_fingerprint",
        ),
        sa.CheckConstraint(
            "outcome_classification IS NULL OR outcome_classification IN ('succeeded','transient','permanent','blocked','awaiting_approval','uncertain','cancelled')",
            name="ck_agent_work_items_outcome_classification",
        ),
        sa.CheckConstraint("length(execution_adapter) BETWEEN 1 AND 128", name="ck_agent_work_items_execution_adapter"),
        sa.CheckConstraint("priority BETWEEN -1000000 AND 1000000", name="ck_agent_work_items_priority"),
        sa.CheckConstraint(
            "deadline IS NULL OR not_before IS NULL OR deadline >= not_before",
            name="ck_agent_work_items_schedule_window",
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL) OR (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_agent_work_items_lease_triplet",
        ),
        sa.CheckConstraint("length(result_summary) <= 2048", name="ck_agent_work_items_result_summary"),
    )

    indexes = (
        ("ix_agent_work_items_source_type", ["source_type"]),
        ("ix_agent_work_items_source_id", ["source_id"]),
        ("ix_agent_work_items_source_revision", ["source_revision"]),
        ("ix_agent_work_items_domain", ["domain"]),
        ("ix_agent_work_items_space_id", ["space_id"]),
        ("ix_agent_work_items_project_id", ["project_id"]),
        ("ix_agent_work_items_task_id", ["task_id"]),
        ("ix_agent_work_items_persona_id", ["persona_id"]),
        ("ix_agent_work_items_app_id", ["app_id"]),
        ("ix_agent_work_items_assigned_agent_id", ["assigned_agent_id"]),
        ("ix_agent_work_items_agent_revision_id", ["agent_revision_id"]),
        ("ix_agent_work_items_execution_adapter", ["execution_adapter"]),
        ("ix_agent_work_items_priority", ["priority"]),
        ("ix_agent_work_items_deadline", ["deadline"]),
        ("ix_agent_work_items_state", ["state"]),
        ("ix_agent_work_items_attempt_count", ["attempt_count"]),
        ("ix_agent_work_items_lease_owner", ["lease_owner"]),
        ("ix_agent_work_items_lease_expires_at", ["lease_expires_at"]),
        ("ix_agent_work_items_heartbeat_at", ["heartbeat_at"]),
        ("ix_agent_work_items_not_before", ["not_before"]),
        ("ix_agent_work_items_next_attempt_at", ["next_attempt_at"]),
        ("ix_agent_work_items_concurrency_key", ["concurrency_key"]),
        ("ix_agent_work_items_root_work_item_id", ["root_work_item_id"]),
        ("ix_agent_work_items_parent_work_item_id", ["parent_work_item_id"]),
        ("ix_agent_work_items_causation_id", ["causation_id"]),
        ("ix_agent_work_items_mutation_fingerprint", ["mutation_fingerprint"]),
        ("ix_agent_work_items_outcome_classification", ["outcome_classification"]),
        ("ix_agent_work_items_completed_at", ["completed_at"]),
        ("ix_agent_work_items_active_agent_run_id", ["active_agent_run_id"]),
        ("ix_agent_work_items_created_at", ["created_at"]),
        ("ix_agent_work_items_claimable", ["state", "not_before", "priority", "created_at"]),
        ("ix_agent_work_items_lease_recovery", ["state", "lease_expires_at", "heartbeat_at"]),
        ("ix_agent_work_items_context_state", ["domain", "project_id", "task_id", "state"]),
    )
    for name, columns in indexes:
        op.create_index(name, _WORK_TABLE, columns)
    op.create_index(
        "uq_agent_work_items_source_mutation_fingerprint",
        _WORK_TABLE,
        ["source_type", "source_id", "mutation_fingerprint"],
        unique=True,
        postgresql_where=sa.text("mutation_fingerprint IS NOT NULL"),
        sqlite_where=sa.text("mutation_fingerprint IS NOT NULL"),
    )


def _create_work_events() -> None:
    op.create_table(
        _EVENT_TABLE,
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("work_item_id", _uuid_type(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("from_state", sa.String(length=32), nullable=True),
        sa.Column("to_state", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("actor_kind", sa.String(length=16), nullable=False, server_default=sa.text("'service'")),
        sa.Column("actor_id", sa.String(length=255), nullable=True),
        sa.Column("actor_user_id", _uuid_type(), nullable=True),
        sa.Column("actor_agent_id", _uuid_type(), nullable=True),
        sa.Column("actor_service_key", sa.String(length=128), nullable=True),
        sa.Column("agent_run_id", _uuid_type(), nullable=True),
        sa.Column("causation_id", sa.String(length=255), nullable=True),
        sa.Column("causal_depth", sa.Integer(), nullable=False, server_default=sa.text("'0'")),
        sa.Column("mutation_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("safe_error_code", sa.String(length=96), nullable=True),
        sa.Column("message", sa.String(length=512), nullable=True),
        sa.Column("result_summary", sa.String(length=2048), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["work_item_id"], [_WORK_TABLE + ".id"], name="fk_agent_work_events_work_item_id", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], name="fk_agent_work_events_actor_user_id", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["actor_agent_id"], ["agents.id"], name="fk_agent_work_events_actor_agent_id", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], name="fk_agent_work_events_agent_run_id", ondelete="SET NULL"),
        sa.UniqueConstraint("work_item_id", "sequence", name="uq_agent_work_events_sequence"),
        sa.CheckConstraint("sequence >= 1 AND sequence <= 1000000", name="ck_agent_work_events_sequence"),
        sa.CheckConstraint("length(event_type) BETWEEN 1 AND 80", name="ck_agent_work_events_event_type"),
        sa.CheckConstraint("actor_kind IN ('human','agent','service')", name="ck_agent_work_events_actor_kind"),
        sa.CheckConstraint(
            "from_state IS NULL OR from_state IN ('pending','claimed','running','awaiting_approval','blocked','retry_wait','uncertain','succeeded','failed','cancelled','dead_letter')",
            name="ck_agent_work_events_from_state",
        ),
        sa.CheckConstraint(
            "to_state IS NULL OR to_state IN ('pending','claimed','running','awaiting_approval','blocked','retry_wait','uncertain','succeeded','failed','cancelled','dead_letter')",
            name="ck_agent_work_events_to_state",
        ),
        sa.CheckConstraint(
            "status IS NULL OR status IN ('pending','claimed','running','awaiting_approval','blocked','retry_wait','uncertain','succeeded','failed','cancelled','dead_letter','transient','permanent')",
            name="ck_agent_work_events_status",
        ),
        sa.CheckConstraint(
            "NOT (actor_user_id IS NOT NULL AND actor_agent_id IS NOT NULL)",
            name="ck_agent_work_events_actor_user_agent_xor",
        ),
        sa.CheckConstraint(
            "(actor_kind = 'human' AND actor_user_id IS NOT NULL AND actor_agent_id IS NULL AND actor_service_key IS NULL) OR (actor_kind = 'agent' AND actor_agent_id IS NOT NULL AND actor_user_id IS NULL AND actor_service_key IS NULL) OR (actor_kind = 'service' AND actor_service_key IS NOT NULL AND actor_user_id IS NULL AND actor_agent_id IS NULL)",
            name="ck_agent_work_events_actor_kind_fields",
        ),
        sa.CheckConstraint(
            "actor_service_key IS NULL OR actor_service_key IN ('aoitalk.system','aoitalk.agent-harness','aoitalk.migrations','aoitalk.media-adapter')",
            name="ck_agent_work_events_actor_service_key",
        ),
        sa.CheckConstraint("causal_depth >= 0 AND causal_depth <= 64", name="ck_agent_work_events_causal_depth"),
        sa.CheckConstraint(
            "mutation_fingerprint IS NULL OR length(mutation_fingerprint) = 64",
            name="ck_agent_work_events_mutation_fingerprint",
        ),
        sa.CheckConstraint("length(message) <= 512", name="ck_agent_work_events_message"),
        sa.CheckConstraint("length(result_summary) <= 2048", name="ck_agent_work_events_result_summary"),
    )
    for name, columns in (
        ("ix_agent_work_events_work_item_id", ["work_item_id"]),
        ("ix_agent_work_events_sequence", ["sequence"]),
        ("ix_agent_work_events_event_type", ["event_type"]),
        ("ix_agent_work_events_status", ["status"]),
        ("ix_agent_work_events_actor_user_id", ["actor_user_id"]),
        ("ix_agent_work_events_actor_agent_id", ["actor_agent_id"]),
        ("ix_agent_work_events_agent_run_id", ["agent_run_id"]),
        ("ix_agent_work_events_causation_id", ["causation_id"]),
        ("ix_agent_work_events_mutation_fingerprint", ["mutation_fingerprint"]),
        ("ix_agent_work_events_created_at", ["created_at"]),
        ("ix_agent_work_events_work_created", ["work_item_id", "created_at"]),
    ):
        op.create_index(name, _EVENT_TABLE, columns)
    op.create_index(
        "uq_agent_work_events_mutation_fingerprint",
        _EVENT_TABLE,
        ["work_item_id", "mutation_fingerprint"],
        unique=True,
        postgresql_where=sa.text("mutation_fingerprint IS NOT NULL"),
        sqlite_where=sa.text("mutation_fingerprint IS NOT NULL"),
    )


def _agent_run_work_item_column() -> sa.Column:
    return sa.Column("work_item_id", _uuid_type(), nullable=True)


def _agent_run_work_item_attempt_column() -> sa.Column:
    return sa.Column("work_item_attempt", sa.Integer(), nullable=True)


def _add_agent_run_work_item() -> None:
    table = "agent_runs"
    if not _table_exists(table):
        return
    columns = _table_columns(table)
    missing_work_item = "work_item_id" not in columns
    missing_attempt = "work_item_attempt" not in columns
    if not missing_work_item and not missing_attempt:
        if "uq_agent_runs_work_item_attempt" not in _indexes(table):
            op.create_index(
                "uq_agent_runs_work_item_attempt",
                table,
                ["work_item_id", "work_item_attempt"],
                unique=True,
                postgresql_where=sa.text(
                    "work_item_id IS NOT NULL AND work_item_attempt IS NOT NULL"
                ),
                sqlite_where=sa.text(
                    "work_item_id IS NOT NULL AND work_item_attempt IS NOT NULL"
                ),
            )
        return
    if _is_sqlite():
        with _sqlite_batch_foreign_keys():
            with op.batch_alter_table(table, recreate="always") as batch:
                if missing_work_item:
                    batch.add_column(_column_copy(_agent_run_work_item_column()))
                    batch.create_foreign_key(
                        "fk_agent_runs_work_item_id",
                        _WORK_TABLE,
                        ["work_item_id"],
                        ["id"],
                        ondelete="SET NULL",
                    )
                if missing_attempt:
                    batch.add_column(_column_copy(_agent_run_work_item_attempt_column()))
    else:
        if missing_work_item:
            op.add_column(table, _column_copy(_agent_run_work_item_column()))
            op.create_foreign_key(
                "fk_agent_runs_work_item_id",
                table,
                _WORK_TABLE,
                ["work_item_id"],
                ["id"],
                ondelete="SET NULL",
            )
        if missing_attempt:
            op.add_column(table, _column_copy(_agent_run_work_item_attempt_column()))
    if missing_work_item and "ix_agent_runs_work_item_id" not in _indexes(table):
        op.create_index("ix_agent_runs_work_item_id", table, ["work_item_id"])
    if "ix_agent_runs_work_item_attempt" not in _indexes(table):
        op.create_index("ix_agent_runs_work_item_attempt", table, ["work_item_attempt"])
    if "uq_agent_runs_work_item_attempt" not in _indexes(table):
        op.create_index(
            "uq_agent_runs_work_item_attempt",
            table,
            ["work_item_id", "work_item_attempt"],
            unique=True,
            postgresql_where=sa.text(
                "work_item_id IS NOT NULL AND work_item_attempt IS NOT NULL"
            ),
            sqlite_where=sa.text(
                "work_item_id IS NOT NULL AND work_item_attempt IS NOT NULL"
            ),
        )


def _external_action_work_item_column() -> sa.Column:
    return sa.Column("origin_work_item_id", _uuid_type(), nullable=True)


def _add_external_action_work_item() -> None:
    table = "external_actions"
    if not _table_exists(table) or "origin_work_item_id" in _table_columns(table):
        return
    if _is_sqlite():
        with _sqlite_batch_foreign_keys():
            with op.batch_alter_table(table, recreate="always") as batch:
                batch.add_column(_column_copy(_external_action_work_item_column()))
                batch.create_foreign_key(
                    "fk_external_actions_origin_work_item_id",
                    _WORK_TABLE,
                    ["origin_work_item_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
    else:
        op.add_column(table, _column_copy(_external_action_work_item_column()))
        op.create_foreign_key(
            "fk_external_actions_origin_work_item_id",
            table,
            _WORK_TABLE,
            ["origin_work_item_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "ix_external_actions_origin_work_item_id" not in _indexes(table):
        op.create_index("ix_external_actions_origin_work_item_id", table, ["origin_work_item_id"])


def _install_event_immutability_guard() -> None:
    bind = op.get_bind()
    dialect = str(getattr(getattr(bind, "dialect", None), "name", "")).lower()
    if dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION agent_work_events_immutable()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              RAISE EXCEPTION 'agent_work_events rows are immutable';
            END;
            $$;
            """
        )
        op.execute(
            """
            CREATE TRIGGER agent_work_events_no_update
            BEFORE UPDATE OR DELETE ON agent_work_events
            FOR EACH ROW EXECUTE FUNCTION agent_work_events_immutable();
            """
        )
        op.execute(
            """
            CREATE TRIGGER agent_work_events_no_truncate
            BEFORE TRUNCATE ON agent_work_events
            FOR EACH STATEMENT EXECUTE FUNCTION agent_work_events_immutable();
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER agent_work_events_no_update
            BEFORE UPDATE ON agent_work_events
            BEGIN
              SELECT RAISE(ABORT, 'agent_work_events rows are immutable');
            END;
            """
        )
        op.execute(
            """
            CREATE TRIGGER agent_work_events_no_delete
            BEFORE DELETE ON agent_work_events
            BEGIN
              SELECT RAISE(ABORT, 'agent_work_events rows are immutable');
            END;
            """
        )


def upgrade() -> None:
    _create_work_items()
    _create_work_events()
    _add_agent_run_work_item()
    _add_external_action_work_item()
    _install_event_immutability_guard()


def _remove_agent_run_work_item() -> None:
    table = "agent_runs"
    if not _table_exists(table):
        return
    columns = _table_columns(table)
    if "work_item_id" not in columns and "work_item_attempt" not in columns:
        return
    _drop_index_if_present("uq_agent_runs_work_item_attempt", table)
    _drop_index_if_present("ix_agent_runs_work_item_attempt", table)
    _drop_index_if_present("ix_agent_runs_work_item_id", table)
    if _is_sqlite():
        with _sqlite_batch_foreign_keys():
            with op.batch_alter_table(table, recreate="always") as batch:
                if "work_item_id" in columns:
                    try:
                        batch.drop_constraint("fk_agent_runs_work_item_id", type_="foreignkey")
                    except Exception:
                        pass
                    batch.drop_column("work_item_id")
                if "work_item_attempt" in columns:
                    batch.drop_column("work_item_attempt")
    else:
        if "work_item_id" in columns:
            try:
                op.drop_constraint("fk_agent_runs_work_item_id", table, type_="foreignkey")
            except Exception:
                pass
            op.drop_column(table, "work_item_id")
        if "work_item_attempt" in columns:
            op.drop_column(table, "work_item_attempt")


def _remove_external_action_work_item() -> None:
    table = "external_actions"
    if not _table_exists(table) or "origin_work_item_id" not in _table_columns(table):
        return
    _drop_index_if_present("ix_external_actions_origin_work_item_id", table)
    if _is_sqlite():
        with _sqlite_batch_foreign_keys():
            with op.batch_alter_table(table, recreate="always") as batch:
                try:
                    batch.drop_constraint("fk_external_actions_origin_work_item_id", type_="foreignkey")
                except Exception:
                    pass
                batch.drop_column("origin_work_item_id")
    else:
        try:
            op.drop_constraint("fk_external_actions_origin_work_item_id", table, type_="foreignkey")
        except Exception:
            pass
        op.drop_column(table, "origin_work_item_id")


def _remove_event_guard() -> None:
    dialect = _dialect_name()
    if dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS agent_work_events_no_update ON agent_work_events")
        op.execute("DROP TRIGGER IF EXISTS agent_work_events_no_truncate ON agent_work_events")
        op.execute("DROP FUNCTION IF EXISTS agent_work_events_immutable()")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS agent_work_events_no_update")
        op.execute("DROP TRIGGER IF EXISTS agent_work_events_no_delete")


def _drop_work_event_table() -> None:
    if not _table_exists(_EVENT_TABLE):
        return
    for name in (
        "uq_agent_work_events_mutation_fingerprint",
        "ix_agent_work_events_work_created",
        "ix_agent_work_events_created_at",
        "ix_agent_work_events_mutation_fingerprint",
        "ix_agent_work_events_causation_id",
        "ix_agent_work_events_agent_run_id",
        "ix_agent_work_events_actor_agent_id",
        "ix_agent_work_events_actor_user_id",
        "ix_agent_work_events_status",
        "ix_agent_work_events_event_type",
        "ix_agent_work_events_sequence",
        "ix_agent_work_events_work_item_id",
    ):
        _drop_index_if_present(name, _EVENT_TABLE)
    op.drop_table(_EVENT_TABLE)


def _drop_work_item_table() -> None:
    if not _table_exists(_WORK_TABLE):
        return
    names = (
        "uq_agent_work_items_source_mutation_fingerprint",
        "ix_agent_work_items_context_state",
        "ix_agent_work_items_lease_recovery",
        "ix_agent_work_items_claimable",
        "ix_agent_work_items_created_at",
        "ix_agent_work_items_completed_at",
        "ix_agent_work_items_active_agent_run_id",
        "ix_agent_work_items_outcome_classification",
        "ix_agent_work_items_mutation_fingerprint",
        "ix_agent_work_items_causation_id",
        "ix_agent_work_items_parent_work_item_id",
        "ix_agent_work_items_root_work_item_id",
        "ix_agent_work_items_concurrency_key",
        "ix_agent_work_items_next_attempt_at",
        "ix_agent_work_items_not_before",
        "ix_agent_work_items_heartbeat_at",
        "ix_agent_work_items_lease_expires_at",
        "ix_agent_work_items_lease_owner",
        "ix_agent_work_items_attempt_count",
        "ix_agent_work_items_state",
        "ix_agent_work_items_deadline",
        "ix_agent_work_items_priority",
        "ix_agent_work_items_execution_adapter",
        "ix_agent_work_items_agent_revision_id",
        "ix_agent_work_items_assigned_agent_id",
        "ix_agent_work_items_app_id",
        "ix_agent_work_items_persona_id",
        "ix_agent_work_items_task_id",
        "ix_agent_work_items_project_id",
        "ix_agent_work_items_space_id",
        "ix_agent_work_items_domain",
        "ix_agent_work_items_source_revision",
        "ix_agent_work_items_source_id",
        "ix_agent_work_items_source_type",
    )
    for name in names:
        _drop_index_if_present(name, _WORK_TABLE)
    op.drop_table(_WORK_TABLE)


def downgrade() -> None:
    _remove_event_guard()
    _remove_external_action_work_item()
    _remove_agent_run_work_item()
    _drop_work_event_table()
    _drop_work_item_table()

