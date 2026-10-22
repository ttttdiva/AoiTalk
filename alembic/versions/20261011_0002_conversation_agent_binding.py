"""Persist the canonical Agent selection and immutable Chat binding snapshots."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "20261011_0002"
down_revision = "20261011_0001"
branch_labels = None
depends_on = None


def _dialect_name() -> str:
    try:
        bind = op.get_bind()
    except (AttributeError, NameError, RuntimeError):
        return ""
    return str(getattr(getattr(bind, "dialect", None), "name", "")).lower()


def _is_sqlite() -> bool:
    return _dialect_name() == "sqlite"


def _uuid_type() -> sa.types.TypeEngine:
    return sa.String(length=36) if _is_sqlite() else postgresql.UUID(as_uuid=True)


def _table_exists(name: str) -> bool:
    try:
        return bool(inspect(op.get_bind()).has_table(name))
    except Exception:
        return False


def _columns(name: str) -> set[str]:
    try:
        return {str(item["name"]) for item in inspect(op.get_bind()).get_columns(name)}
    except Exception:
        return set()


def _indexes(name: str) -> set[str]:
    try:
        return {str(item["name"]) for item in inspect(op.get_bind()).get_indexes(name)}
    except Exception:
        return set()


def _create_index_if_missing(name: str, table: str, columns: list[str]) -> None:
    if name not in _indexes(table):
        op.create_index(name, table, columns)


@contextmanager
def _sqlite_batch_foreign_keys() -> Iterator[None]:
    if not _is_sqlite():
        yield
        return
    bind = op.get_bind()
    try:
        enabled = bool(bind.exec_driver_sql("PRAGMA foreign_keys").scalar())
    except Exception:
        enabled = False
    raw = getattr(bind, "connection", None)
    if not enabled or raw is None:
        yield
        return
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


def _add_session_columns() -> None:
    if not _table_exists("conversation_sessions"):
        return
    existing = _columns("conversation_sessions")
    if {"agent_id", "agent_binding_updated_at"} <= existing:
        return
    if _is_sqlite():
        with _sqlite_batch_foreign_keys():
            with op.batch_alter_table("conversation_sessions", recreate="always") as batch:
                if "agent_id" not in existing:
                    batch.add_column(sa.Column("agent_id", _uuid_type(), nullable=True))
                    batch.create_foreign_key(
                        "fk_conversation_sessions_agent_id",
                        "agents",
                        ["agent_id"],
                        ["id"],
                        ondelete="SET NULL",
                    )
                if "agent_binding_updated_at" not in existing:
                    batch.add_column(sa.Column("agent_binding_updated_at", sa.DateTime(), nullable=True))
    else:
        if "agent_id" not in existing:
            op.add_column(
                "conversation_sessions",
                sa.Column("agent_id", _uuid_type(), nullable=True),
            )
            op.create_foreign_key(
                "fk_conversation_sessions_agent_id",
                "conversation_sessions",
                "agents",
                ["agent_id"],
                ["id"],
                ondelete="SET NULL",
            )
        if "agent_binding_updated_at" not in existing:
            op.add_column(
                "conversation_sessions",
                sa.Column("agent_binding_updated_at", sa.DateTime(), nullable=True),
            )
    _create_index_if_missing(
        "ix_conversation_sessions_agent_id",
        "conversation_sessions",
        ["agent_id"],
    )
    _create_index_if_missing(
        "ix_conversation_sessions_agent_binding_updated_at",
        "conversation_sessions",
        ["agent_binding_updated_at"],
    )


def _add_work_item_column() -> None:
    if not _table_exists("agent_work_items"):
        return
    existing = _columns("agent_work_items")
    if "conversation_session_id" in existing:
        return
    if _is_sqlite():
        with _sqlite_batch_foreign_keys():
            with op.batch_alter_table("agent_work_items", recreate="always") as batch:
                batch.add_column(sa.Column("conversation_session_id", _uuid_type(), nullable=True))
                batch.create_foreign_key(
                    "fk_agent_work_items_conversation_session_id",
                    "conversation_sessions",
                    ["conversation_session_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
    else:
        op.add_column(
            "agent_work_items",
            sa.Column("conversation_session_id", _uuid_type(), nullable=True),
        )
        op.create_foreign_key(
            "fk_agent_work_items_conversation_session_id",
            "agent_work_items",
            "conversation_sessions",
            ["conversation_session_id"],
            ["id"],
            ondelete="SET NULL",
        )
    _create_index_if_missing(
        "ix_agent_work_items_conversation_session_id",
        "agent_work_items",
        ["conversation_session_id"],
    )


def _create_binding_table() -> None:
    if _table_exists("conversation_agent_bindings"):
        return
    op.create_table(
        "conversation_agent_bindings",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("session_id", _uuid_type(), nullable=False),
        sa.Column("agent_id", _uuid_type(), nullable=False),
        sa.Column("agent_revision_id", _uuid_type(), nullable=False),
        sa.Column("revision_version", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False, server_default=sa.text("''")),
        sa.Column("mission", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("responsibility_summary", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("operational_instructions", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("agent_team_id", sa.String(length=100), nullable=False),
        sa.Column("execution_profile_id", sa.String(length=100), nullable=False),
        sa.Column("allowed_subagent_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("capability_ceiling", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("route_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("revision_content_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", _uuid_type(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["conversation_sessions.id"],
            name="fk_conversation_agent_bindings_session_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name="fk_conversation_agent_bindings_agent_id",
        ),
        sa.ForeignKeyConstraint(
            ["agent_revision_id"],
            ["agent_revisions.id"],
            name="fk_conversation_agent_bindings_revision_id",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_conversation_agent_bindings_created_by",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "revision_version > 0",
            name="ck_conversation_agent_bindings_revision_version",
        ),
        sa.CheckConstraint(
            "length(revision_content_hash) = 64",
            name="ck_conversation_agent_bindings_hash_length",
        ),
        sa.CheckConstraint(
            "length(snapshot_hash) = 64",
            name="ck_conversation_agent_bindings_snapshot_hash_length",
        ),
    )
    op.create_index(
        "ix_conversation_agent_bindings_session_id",
        "conversation_agent_bindings",
        ["session_id"],
    )
    op.create_index(
        "ix_conversation_agent_bindings_session_created",
        "conversation_agent_bindings",
        ["session_id", "created_at", "id"],
    )
    op.create_index(
        "ix_conversation_agent_bindings_exact_pair",
        "conversation_agent_bindings",
        ["session_id", "agent_id", "agent_revision_id", "created_at"],
    )
    op.create_index(
        "ix_conversation_agent_bindings_agent_id",
        "conversation_agent_bindings",
        ["agent_id"],
    )
    op.create_index(
        "ix_conversation_agent_bindings_agent_revision_id",
        "conversation_agent_bindings",
        ["agent_revision_id"],
    )
    op.create_index(
        "ix_conversation_agent_bindings_revision_content_hash",
        "conversation_agent_bindings",
        ["revision_content_hash"],
    )
    op.create_index(
        "ix_conversation_agent_bindings_snapshot_hash",
        "conversation_agent_bindings",
        ["snapshot_hash"],
    )
    op.create_index(
        "ix_conversation_agent_bindings_created_at",
        "conversation_agent_bindings",
        ["created_at"],
    )


def _create_binding_update_guard() -> None:
    """Reject snapshot edits while allowing creator nulling and CASCADE deletes."""

    if _is_sqlite():
        op.execute(
            sa.text(
                """
                CREATE TRIGGER conversation_agent_bindings_immutable_update
                BEFORE UPDATE ON conversation_agent_bindings
                BEGIN
                    SELECT CASE WHEN (
                        NEW.id IS NOT OLD.id OR
                        NEW.session_id IS NOT OLD.session_id OR
                        NEW.agent_id IS NOT OLD.agent_id OR
                        NEW.agent_revision_id IS NOT OLD.agent_revision_id OR
                        NEW.revision_version IS NOT OLD.revision_version OR
                        NEW.display_name IS NOT OLD.display_name OR
                        NEW.mission IS NOT OLD.mission OR
                        NEW.responsibility_summary IS NOT OLD.responsibility_summary OR
                        NEW.operational_instructions IS NOT OLD.operational_instructions OR
                        NEW.agent_team_id IS NOT OLD.agent_team_id OR
                        NEW.execution_profile_id IS NOT OLD.execution_profile_id OR
                        NEW.allowed_subagent_ids IS NOT OLD.allowed_subagent_ids OR
                        NEW.capability_ceiling IS NOT OLD.capability_ceiling OR
                        NEW.route_snapshot IS NOT OLD.route_snapshot OR
                        NEW.revision_content_hash IS NOT OLD.revision_content_hash OR
                        NEW.snapshot_hash IS NOT OLD.snapshot_hash OR
                        NEW.created_at IS NOT OLD.created_at
                    ) THEN RAISE(ABORT, 'conversation_agent_binding_immutable') END;
                END
                """
            )
        )
        return
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION conversation_agent_bindings_immutable_guard()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF NEW.id IS DISTINCT FROM OLD.id
                   OR NEW.session_id IS DISTINCT FROM OLD.session_id
                   OR NEW.agent_id IS DISTINCT FROM OLD.agent_id
                   OR NEW.agent_revision_id IS DISTINCT FROM OLD.agent_revision_id
                   OR NEW.revision_version IS DISTINCT FROM OLD.revision_version
                   OR NEW.display_name IS DISTINCT FROM OLD.display_name
                   OR NEW.mission IS DISTINCT FROM OLD.mission
                   OR NEW.responsibility_summary IS DISTINCT FROM OLD.responsibility_summary
                   OR NEW.operational_instructions IS DISTINCT FROM OLD.operational_instructions
                   OR NEW.agent_team_id IS DISTINCT FROM OLD.agent_team_id
                   OR NEW.execution_profile_id IS DISTINCT FROM OLD.execution_profile_id
                   OR to_jsonb(NEW.allowed_subagent_ids) IS DISTINCT FROM to_jsonb(OLD.allowed_subagent_ids)
                   OR to_jsonb(NEW.capability_ceiling) IS DISTINCT FROM to_jsonb(OLD.capability_ceiling)
                   OR to_jsonb(NEW.route_snapshot) IS DISTINCT FROM to_jsonb(OLD.route_snapshot)
                   OR NEW.revision_content_hash IS DISTINCT FROM OLD.revision_content_hash
                   OR NEW.snapshot_hash IS DISTINCT FROM OLD.snapshot_hash
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                    RAISE EXCEPTION 'conversation_agent_binding_immutable';
                END IF;
                RETURN NEW;
            END;
            $$;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER conversation_agent_bindings_immutable_update
            BEFORE UPDATE ON conversation_agent_bindings
            FOR EACH ROW
            EXECUTE FUNCTION conversation_agent_bindings_immutable_guard();
            """
        )
    )


def _drop_binding_update_guard() -> None:
    if _is_sqlite():
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS conversation_agent_bindings_immutable_update"
            )
        )
        return
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS conversation_agent_bindings_immutable_update "
            "ON conversation_agent_bindings"
        )
    )
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS conversation_agent_bindings_immutable_guard()"
        )
    )


def upgrade() -> None:
    _add_session_columns()
    _add_work_item_column()
    _create_binding_table()
    _create_binding_update_guard()


def downgrade() -> None:
    if _table_exists("conversation_agent_bindings"):
        _drop_binding_update_guard()
        for name in (
            "ix_conversation_agent_bindings_exact_pair",
            "ix_conversation_agent_bindings_session_created",
            "ix_conversation_agent_bindings_created_at",
            "ix_conversation_agent_bindings_session_id",
            "ix_conversation_agent_bindings_agent_revision_id",
            "ix_conversation_agent_bindings_agent_id",
            "ix_conversation_agent_bindings_revision_content_hash",
            "ix_conversation_agent_bindings_snapshot_hash",
        ):
            if name in _indexes("conversation_agent_bindings"):
                op.drop_index(name, table_name="conversation_agent_bindings")
        op.drop_table("conversation_agent_bindings")

    if _table_exists("agent_work_items") and "conversation_session_id" in _columns("agent_work_items"):
        if _is_sqlite():
            with _sqlite_batch_foreign_keys():
                with op.batch_alter_table("agent_work_items", recreate="always") as batch:
                    batch.drop_constraint(
                        "fk_agent_work_items_conversation_session_id", type_="foreignkey"
                    )
                    batch.drop_column("conversation_session_id")
        else:
            op.drop_constraint(
                "fk_agent_work_items_conversation_session_id",
                "agent_work_items",
                type_="foreignkey",
            )
            op.drop_column("agent_work_items", "conversation_session_id")

    if _table_exists("conversation_sessions"):
        for name in (
            "ix_conversation_sessions_agent_id",
            "ix_conversation_sessions_agent_binding_updated_at",
        ):
            if name in _indexes("conversation_sessions"):
                op.drop_index(name, table_name="conversation_sessions")
        existing = _columns("conversation_sessions")
        if "agent_id" in existing or "agent_binding_updated_at" in existing:
            if _is_sqlite():
                with _sqlite_batch_foreign_keys():
                    with op.batch_alter_table("conversation_sessions", recreate="always") as batch:
                        if "agent_id" in existing:
                            batch.drop_constraint(
                                "fk_conversation_sessions_agent_id", type_="foreignkey"
                            )
                            batch.drop_column("agent_id")
                        if "agent_binding_updated_at" in existing:
                            batch.drop_column("agent_binding_updated_at")
            else:
                if "agent_id" in existing:
                    op.drop_constraint(
                        "fk_conversation_sessions_agent_id",
                        "conversation_sessions",
                        type_="foreignkey",
                    )
                    op.drop_column("conversation_sessions", "agent_id")
                if "agent_binding_updated_at" in existing:
                    op.drop_column(
                        "conversation_sessions", "agent_binding_updated_at"
                    )
