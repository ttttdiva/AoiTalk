"""Persist a finite budget owner across successor Dialogue chains.

Revision 0003 fenced pre-existing Dialogues whose usage was not observed.  This
revision adds the server-derived self/root links and a separately locked root
aggregate.  Existing rows are grouped to themselves and therefore retain
their historical identity without gaining authority over another owner.
"""

from __future__ import annotations

import hashlib
import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "20261012_0004"
down_revision = "20261012_0003"
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
        return inspect(op.get_bind()).has_table(name)
    except (AttributeError, RuntimeError):
        return False


def _columns(name: str) -> set[str]:
    try:
        return {str(item["name"]) for item in inspect(op.get_bind()).get_columns(name)}
    except (AttributeError, RuntimeError):
        return set()


def _indexes(name: str) -> set[str]:
    try:
        return {str(item.get("name")) for item in inspect(op.get_bind()).get_indexes(name) if item.get("name")}
    except (AttributeError, RuntimeError):
        return set()


def _check_constraints(name: str) -> set[str]:
    try:
        return {
            str(item.get("name"))
            for item in inspect(op.get_bind()).get_check_constraints(name)
            if item.get("name")
        }
    except (AttributeError, RuntimeError):
        return set()


def _ensure_dialogue_budget_shape() -> None:
    """Repair the deployed early-0003 Dialogue column subset.

    One deployed 0003 variant created the usage ledger before the reservation
    counters were added to ``agent_dialogues``.  0004 reads those counters
    while copying rows into the root owner, so the additive repair must happen
    before that SELECT.  Missing counters are empty reservations; a missing
    usage state is fenced ``unknown`` rather than inferred as zero usage.
    """

    table = "agent_dialogues"
    if not _table_exists(table):
        return
    existing = _columns(table)
    column_types = {
        str(item["name"]): item.get("type")
        for item in inspect(op.get_bind()).get_columns(table)
    }
    additions = (
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("16")),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_tokens", sa.BigInteger(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("cached_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("reserved_cost_micros", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost_currency", sa.String(length=16), nullable=True),
        sa.Column("usage_state", sa.String(length=24), nullable=False, server_default=sa.text("'unknown'")),
    )
    checks = {
        "ck_agent_dialogues_budget_max_attempts": "max_attempts BETWEEN 1 AND 100",
        "ck_agent_dialogues_budget_attempt_count": "attempt_count BETWEEN 0 AND max_attempts",
        "ck_agent_dialogues_budget_max_tokens": "max_tokens IS NULL OR max_tokens BETWEEN 1 AND 1000000",
        "ck_agent_dialogues_budget_token_usage": "input_tokens >= 0 AND output_tokens >= 0 AND cached_tokens >= 0 AND total_tokens >= 0",
        "ck_agent_dialogues_budget_reserved_tokens": "reserved_tokens >= 0",
        "ck_agent_dialogues_budget_max_cost": "max_cost_micros IS NULL OR max_cost_micros BETWEEN 1 AND 1000000000000",
        "ck_agent_dialogues_budget_cost": "cost_micros IS NULL OR cost_micros >= 0",
        "ck_agent_dialogues_budget_reserved_cost": "reserved_cost_micros >= 0",
        "ck_agent_dialogues_budget_usage_state": "usage_state IN ('known','unknown','cost_unknown','token_unknown')",
    }
    existing_checks = _check_constraints(table)
    numeric_types = {
        "max_attempts": sa.Integer(),
        "attempt_count": sa.Integer(),
        "max_tokens": sa.BigInteger(),
        "input_tokens": sa.BigInteger(),
        "output_tokens": sa.BigInteger(),
        "cached_tokens": sa.BigInteger(),
        "total_tokens": sa.BigInteger(),
        "reserved_tokens": sa.BigInteger(),
        "max_cost_micros": sa.BigInteger(),
        "cost_micros": sa.BigInteger(),
        "reserved_cost_micros": sa.BigInteger(),
    }
    with op.batch_alter_table(table) as batch:
        for column in additions:
            if column.name not in existing:
                batch.add_column(column)
        # Some early SQLite bootstrap fixtures declared all budget values as
        # VARCHAR.  Normalize those columns before adding numeric checks so
        # values such as '2' do not fail SQLite's type-sensitive BETWEEN.
        if _is_sqlite():
            for name, type_ in numeric_types.items():
                current = column_types.get(name)
                if name in existing and current is not None and current.__class__ is not type_.__class__:
                    batch.alter_column(name, type_=type_)
        for name, condition in checks.items():
            if name not in existing_checks:
                batch.create_check_constraint(name, condition)
    if "usage_state" in _columns(table):
        op.execute(sa.text("UPDATE agent_dialogues SET usage_state='unknown' WHERE usage_state IS NULL"))


def _legacy_usage_shape() -> None:
    """Bring an older 0003 usage table to the 0003 canonical columns.

    Some deployed 0003 snapshots predate the digest/metadata columns.  They
    must be fenced unknown before root aggregation; their zero values are not
    treated as observed usage.
    """

    table = "agent_dialogue_usage"
    if not _table_exists(table):
        return
    existing = _columns(table)
    additions = (
        sa.Column("state", sa.String(length=24), nullable=True, server_default=sa.text("'unknown'")),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cached_tokens", sa.BigInteger(), nullable=True),
        sa.Column("total_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("cost_currency", sa.String(length=16), nullable=True),
        sa.Column("reason", sa.String(length=128), nullable=True),
        sa.Column("usage_digest", sa.String(length=64), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    with op.batch_alter_table(table) as batch:
        for column in additions:
            if column.name not in existing:
                batch.add_column(column)
    after = _columns(table)
    added_digest = "usage_digest" not in existing
    legacy = added_digest or "metadata" not in existing
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, dialogue_id, run_id, usage_key, state, input_tokens, "
            "output_tokens, cached_tokens, total_tokens, cost_micros, "
            "cost_currency, reason FROM agent_dialogue_usage"
        )
    ).mappings().all()
    if legacy:
        for row in rows:
            state = "unknown"
            reason = row.get("reason") or "legacy_usage_unverified"
            payload = {
                "dialogue_id": str(row.get("dialogue_id") or ""),
                "run_id": str(row.get("run_id") or ""),
                "usage_key": str(row.get("usage_key") or ""),
                "state": state,
                "input_tokens": row.get("input_tokens"),
                "output_tokens": row.get("output_tokens"),
                "cached_tokens": row.get("cached_tokens"),
                "total_tokens": row.get("total_tokens"),
                "cost_micros": row.get("cost_micros"),
                "cost_currency": row.get("cost_currency"),
                "reason": reason,
            }
            digest = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            connection.execute(
                sa.text(
                    "UPDATE agent_dialogue_usage SET state=:state, reason=:reason, usage_digest=:digest "
                    "WHERE id=:id"
                ),
                {"state": state, "reason": reason, "digest": digest, "id": row.get("id")},
            )
        # The old ledger shape has no verifiable digest/metadata contract.
        # Fence the owning Dialogue as well; its zero counters cannot be
        # treated as observed provider usage.
        if _table_exists("agent_dialogues") and "usage_state" in _columns("agent_dialogues"):
            connection.execute(sa.text("UPDATE agent_dialogues SET usage_state='unknown'"))
    if added_digest and "usage_digest" in after:
        with op.batch_alter_table(table) as batch:
            batch.alter_column("usage_digest", nullable=False)
            batch.create_check_constraint("ck_agent_dialogue_usage_digest", "length(usage_digest) = 64")


def _add_dialogue_links() -> None:
    if not _table_exists("agent_dialogues"):
        return
    existing = _columns("agent_dialogues")
    with op.batch_alter_table("agent_dialogues") as batch:
        if "budget_root_dialogue_id" not in existing:
            batch.add_column(sa.Column("budget_root_dialogue_id", _uuid_type(), nullable=True))
        if "parent_dialogue_id" not in existing:
            batch.add_column(sa.Column("parent_dialogue_id", _uuid_type(), nullable=True))
        if "causal_depth" not in existing:
            batch.add_column(sa.Column("causal_depth", sa.Integer(), nullable=False, server_default=sa.text("0")))
        batch.create_foreign_key(
            "fk_agent_dialogues_budget_root_dialogue",
            "agent_dialogues",
            ["budget_root_dialogue_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_agent_dialogues_parent_dialogue",
            "agent_dialogues",
            ["parent_dialogue_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint("ck_agent_dialogues_budget_causal_depth", "causal_depth BETWEEN 0 AND 32")
    op.create_index("ix_agent_dialogues_budget_root", "agent_dialogues", ["budget_root_dialogue_id", "causal_depth"])
    op.create_index("ix_agent_dialogues_parent", "agent_dialogues", ["parent_dialogue_id", "causal_depth"])
    # Old rows are independent chains.  This update is server derived and
    # never copies a client-provided parent/owner claim.
    op.execute(sa.text("UPDATE agent_dialogues SET budget_root_dialogue_id = id WHERE budget_root_dialogue_id IS NULL"))
    op.execute(sa.text("UPDATE agent_dialogues SET causal_depth = 0 WHERE causal_depth IS NULL"))


def _create_budget_root_table() -> None:
    table = "agent_dialogue_budget_roots"
    if _table_exists(table):
        return
    op.create_table(
        table,
        sa.Column("root_dialogue_id", _uuid_type(), nullable=False),
        sa.Column("owner_user_id", _uuid_type(), nullable=False),
        sa.Column("root_session_id", _uuid_type(), nullable=False),
        sa.Column("root_agent_id", _uuid_type(), nullable=False),
        sa.Column("root_agent_revision_id", _uuid_type(), nullable=False),
        sa.Column("human_message_id", _uuid_type(), nullable=True),
        sa.Column("human_run_id", _uuid_type(), nullable=True),
        sa.Column("human_work_item_id", _uuid_type(), nullable=True),
        sa.Column("authorization_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default=sa.text("'active'")),
        sa.Column("max_turns", sa.Integer(), nullable=False, server_default=sa.text("8")),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_depth", sa.Integer(), nullable=False, server_default=sa.text("4")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("16")),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_tokens", sa.BigInteger(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("cached_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("reserved_cost_micros", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost_currency", sa.String(length=16), nullable=True),
        sa.Column("usage_state", sa.String(length=24), nullable=False, server_default=sa.text("'unknown'")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["root_dialogue_id"], ["agent_dialogues.id"], name="fk_agent_dialogue_budget_root_dialogue", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_agent_dialogue_budget_root_owner", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["root_session_id"], ["conversation_sessions.id"], name="fk_agent_dialogue_budget_root_session", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["root_agent_id"], ["agents.id"], name="fk_agent_dialogue_budget_root_agent", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["root_agent_revision_id"], ["agent_revisions.id"], name="fk_agent_dialogue_budget_root_revision", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["human_message_id"], ["conversation_messages.id"], name="fk_agent_dialogue_budget_root_message", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["human_run_id"], ["agent_runs.id"], name="fk_agent_dialogue_budget_root_run", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["human_work_item_id"], ["agent_work_items.id"], name="fk_agent_dialogue_budget_root_work", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("root_dialogue_id"),
        sa.CheckConstraint("length(authorization_hash) = 64", name="ck_agent_dialogue_budget_root_auth_hash"),
        sa.CheckConstraint("state IN ('active','blocked','succeeded','failed','cancelled','expired')", name="ck_agent_dialogue_budget_root_state"),
        sa.CheckConstraint("max_turns BETWEEN 1 AND 100", name="ck_agent_dialogue_budget_root_max_turns"),
        sa.CheckConstraint("turn_count BETWEEN 0 AND max_turns", name="ck_agent_dialogue_budget_root_turn_count"),
        sa.CheckConstraint("max_depth BETWEEN 0 AND 32", name="ck_agent_dialogue_budget_root_max_depth"),
        sa.CheckConstraint("max_attempts BETWEEN 1 AND 100", name="ck_agent_dialogue_budget_root_max_attempts"),
        sa.CheckConstraint("attempt_count BETWEEN 0 AND max_attempts", name="ck_agent_dialogue_budget_root_attempt_count"),
        sa.CheckConstraint("max_tokens IS NULL OR max_tokens BETWEEN 1 AND 1000000", name="ck_agent_dialogue_budget_root_max_tokens"),
        sa.CheckConstraint("input_tokens >= 0 AND output_tokens >= 0 AND cached_tokens >= 0 AND total_tokens >= 0 AND reserved_tokens >= 0", name="ck_agent_dialogue_budget_root_tokens"),
        sa.CheckConstraint("max_cost_micros IS NULL OR max_cost_micros BETWEEN 1 AND 1000000000000", name="ck_agent_dialogue_budget_root_max_cost"),
        sa.CheckConstraint("cost_micros IS NULL OR cost_micros >= 0", name="ck_agent_dialogue_budget_root_cost"),
        sa.CheckConstraint("reserved_cost_micros >= 0", name="ck_agent_dialogue_budget_root_reserved_cost"),
        sa.CheckConstraint("usage_state IN ('known','unknown','cost_unknown','token_unknown')", name="ck_agent_dialogue_budget_root_usage_state"),
    )
    op.create_index("ix_agent_dialogue_budget_root_owner", table, ["owner_user_id", "state"])


def _ensure_budget_root_table_shape() -> None:
    """Align a partially pre-created root table before row population.

    A few QA/bootstrap databases ran ``create_all`` against an intermediate
    model and already have ``agent_dialogue_budget_roots``.  In that case
    ``create_table`` is skipped, so every column needed by the canonical
    aggregate SELECT must be repaired explicitly.
    """

    table = "agent_dialogue_budget_roots"
    if not _table_exists(table):
        return
    existing = _columns(table)
    if "root_dialogue_id" not in existing:
        raise RuntimeError("agent_dialogue_budget_roots exists without root_dialogue_id; cannot preserve owner identity")
    additions = (
        # Identity columns are nullable only during the repair.  They are
        # filled from the authoritative Dialogue row below, then tightened.
        sa.Column("owner_user_id", _uuid_type(), nullable=True),
        sa.Column("root_session_id", _uuid_type(), nullable=True),
        sa.Column("root_agent_id", _uuid_type(), nullable=True),
        sa.Column("root_agent_revision_id", _uuid_type(), nullable=True),
        sa.Column("human_message_id", _uuid_type(), nullable=True),
        sa.Column("human_run_id", _uuid_type(), nullable=True),
        sa.Column("human_work_item_id", _uuid_type(), nullable=True),
        sa.Column("authorization_hash", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False, server_default=sa.text("'active'")),
        sa.Column("max_turns", sa.Integer(), nullable=False, server_default=sa.text("8")),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_depth", sa.Integer(), nullable=False, server_default=sa.text("4")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("16")),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_tokens", sa.BigInteger(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("cached_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("reserved_cost_micros", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost_currency", sa.String(length=16), nullable=True),
        sa.Column("usage_state", sa.String(length=24), nullable=False, server_default=sa.text("'unknown'")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    checks = {
        "ck_agent_dialogue_budget_root_auth_hash": "length(authorization_hash) = 64",
        "ck_agent_dialogue_budget_root_state": "state IN ('active','blocked','succeeded','failed','cancelled','expired')",
        "ck_agent_dialogue_budget_root_max_turns": "max_turns BETWEEN 1 AND 100",
        "ck_agent_dialogue_budget_root_turn_count": "turn_count BETWEEN 0 AND max_turns",
        "ck_agent_dialogue_budget_root_max_depth": "max_depth BETWEEN 0 AND 32",
        "ck_agent_dialogue_budget_root_max_attempts": "max_attempts BETWEEN 1 AND 100",
        "ck_agent_dialogue_budget_root_attempt_count": "attempt_count BETWEEN 0 AND max_attempts",
        "ck_agent_dialogue_budget_root_max_tokens": "max_tokens IS NULL OR max_tokens BETWEEN 1 AND 1000000",
        "ck_agent_dialogue_budget_root_tokens": "input_tokens >= 0 AND output_tokens >= 0 AND cached_tokens >= 0 AND total_tokens >= 0 AND reserved_tokens >= 0",
        "ck_agent_dialogue_budget_root_max_cost": "max_cost_micros IS NULL OR max_cost_micros BETWEEN 1 AND 1000000000000",
        "ck_agent_dialogue_budget_root_cost": "cost_micros IS NULL OR cost_micros >= 0",
        "ck_agent_dialogue_budget_root_reserved_cost": "reserved_cost_micros >= 0",
        "ck_agent_dialogue_budget_root_usage_state": "usage_state IN ('known','unknown','cost_unknown','token_unknown')",
    }
    existing_checks = _check_constraints(table)
    missing_identity = {
        name for name in (
            "owner_user_id", "root_session_id", "root_agent_id", "root_agent_revision_id", "authorization_hash"
        ) if name not in existing
    }
    with op.batch_alter_table(table) as batch:
        for column in additions:
            if column.name not in existing:
                batch.add_column(column)
        for name, condition in checks.items():
            if name not in existing_checks:
                batch.create_check_constraint(name, condition)

    connection = op.get_bind()
    if missing_identity:
        connection.execute(
            sa.text(
                """
                UPDATE agent_dialogue_budget_roots
                   SET owner_user_id = COALESCE(owner_user_id, (SELECT owner_user_id FROM agent_dialogues d WHERE d.id = agent_dialogue_budget_roots.root_dialogue_id)),
                       root_session_id = COALESCE(root_session_id, (SELECT root_session_id FROM agent_dialogues d WHERE d.id = agent_dialogue_budget_roots.root_dialogue_id)),
                       root_agent_id = COALESCE(root_agent_id, (SELECT root_agent_id FROM agent_dialogues d WHERE d.id = agent_dialogue_budget_roots.root_dialogue_id)),
                       root_agent_revision_id = COALESCE(root_agent_revision_id, (SELECT root_agent_revision_id FROM agent_dialogues d WHERE d.id = agent_dialogue_budget_roots.root_dialogue_id)),
                       human_message_id = COALESCE(human_message_id, (SELECT human_message_id FROM agent_dialogues d WHERE d.id = agent_dialogue_budget_roots.root_dialogue_id)),
                       human_run_id = COALESCE(human_run_id, (SELECT human_run_id FROM agent_dialogues d WHERE d.id = agent_dialogue_budget_roots.root_dialogue_id)),
                       human_work_item_id = COALESCE(human_work_item_id, (SELECT human_work_item_id FROM agent_dialogues d WHERE d.id = agent_dialogue_budget_roots.root_dialogue_id)),
                       authorization_hash = COALESCE(authorization_hash, (SELECT authorization_hash FROM agent_dialogues d WHERE d.id = agent_dialogue_budget_roots.root_dialogue_id), :fallback_hash)
                 WHERE owner_user_id IS NULL
                    OR root_session_id IS NULL
                    OR root_agent_id IS NULL
                    OR root_agent_revision_id IS NULL
                    OR authorization_hash IS NULL
                """
            ),
            {"fallback_hash": "0" * 64},
        )
        unresolved = connection.execute(
            sa.text(
                """
                SELECT COUNT(*) FROM agent_dialogue_budget_roots
                 WHERE owner_user_id IS NULL OR root_session_id IS NULL
                    OR root_agent_id IS NULL OR root_agent_revision_id IS NULL
                    OR authorization_hash IS NULL
                """
            )
        ).scalar_one()
        if unresolved:
            raise RuntimeError("agent_dialogue_budget_roots identity repair left NULL owner fields")
        with op.batch_alter_table(table) as batch:
            for name in ("owner_user_id", "root_session_id", "root_agent_id", "root_agent_revision_id", "authorization_hash"):
                if name in missing_identity:
                    batch.alter_column(name, nullable=False)
    if "ix_agent_dialogue_budget_root_owner" not in _indexes(table):
        op.create_index("ix_agent_dialogue_budget_root_owner", table, ["owner_user_id", "state"])


def _populate_budget_roots() -> None:
    if not _table_exists("agent_dialogue_budget_roots"):
        return
    # Existing 0002/0003 rows are each their own chain.  Legacy usage remains
    # fenced unknown and is copied only as identity/budget metadata.
    op.execute(
        sa.text(
            """
            INSERT INTO agent_dialogue_budget_roots (
                root_dialogue_id, owner_user_id, root_session_id, root_agent_id,
                root_agent_revision_id, human_message_id, human_run_id,
                human_work_item_id, authorization_hash, state,
                max_turns, turn_count, max_depth, max_attempts, attempt_count,
                max_tokens, input_tokens, output_tokens, cached_tokens,
                total_tokens, reserved_tokens, max_cost_micros, cost_micros,
                reserved_cost_micros, cost_currency, usage_state, created_at,
                updated_at
            )
            SELECT id, owner_user_id, root_session_id, root_agent_id,
                   root_agent_revision_id, human_message_id, human_run_id,
                   human_work_item_id, authorization_hash,
                   CASE WHEN state IN ('queued','running','waiting') OR state IS NULL THEN 'active' ELSE state END,
                   COALESCE(max_turns,8), COALESCE(turn_count,0), COALESCE(max_depth,4), COALESCE(max_attempts,16), COALESCE(attempt_count,0),
                   max_tokens, COALESCE(input_tokens,0), COALESCE(output_tokens,0), COALESCE(cached_tokens,0),
                   COALESCE(total_tokens,0), COALESCE(reserved_tokens,0), max_cost_micros, cost_micros,
                   COALESCE(reserved_cost_micros,0), cost_currency, COALESCE(usage_state,'unknown'),
                   COALESCE(created_at,CURRENT_TIMESTAMP), COALESCE(updated_at,CURRENT_TIMESTAMP)
            FROM agent_dialogues
            WHERE budget_root_dialogue_id = id
            """
        )
    )


def _create_legacy_reservation_table() -> None:
    """Create reservations when the deployed 0003 variant predates them."""

    table = "agent_dialogue_reservations"
    if _table_exists(table):
        return
    op.create_table(
        table,
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("dialogue_id", _uuid_type(), nullable=False),
        sa.Column("budget_root_dialogue_id", _uuid_type(), nullable=True),
        sa.Column("run_id", _uuid_type(), nullable=True),
        sa.Column("reservation_key", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default=sa.text("'reserved'")),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("reserved_cost_micros", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("charged_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("charged_cost_micros", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["dialogue_id"], ["agent_dialogues.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["budget_root_dialogue_id"], ["agent_dialogue_budget_roots.root_dialogue_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dialogue_id", "reservation_key", name="uq_agent_dialogue_reservation_key"),
        sa.UniqueConstraint("budget_root_dialogue_id", "reservation_key", name="uq_agent_dialogue_reservation_root_key"),
        sa.CheckConstraint("state IN ('reserved','released','charged','overrun')", name="ck_agent_dialogue_reservation_state"),
        sa.CheckConstraint("reserved_tokens >= 0 AND reserved_cost_micros >= 0", name="ck_agent_dialogue_reservation_reserved"),
        sa.CheckConstraint("charged_tokens >= 0 AND charged_cost_micros >= 0", name="ck_agent_dialogue_reservation_charged"),
        sa.CheckConstraint("length(reservation_key) BETWEEN 1 AND 255", name="ck_agent_dialogue_reservation_key"),
    )
    op.create_index("ix_agent_dialogue_reservations_budget_root", table, ["budget_root_dialogue_id", "created_at"])
    op.create_index("ix_agent_dialogue_reservations_state", table, ["state", "created_at"])


def _add_ledger_root_links() -> None:
    for table in ("agent_dialogue_usage", "agent_dialogue_reservations"):
        if not _table_exists(table):
            continue
        existing = _columns(table)
        added_root_link = "budget_root_dialogue_id" not in existing
        with op.batch_alter_table(table) as batch:
            if added_root_link:
                batch.add_column(sa.Column("budget_root_dialogue_id", _uuid_type(), nullable=True))
                batch.create_foreign_key(
                    f"fk_{table}_budget_root",
                    "agent_dialogue_budget_roots",
                    ["budget_root_dialogue_id"],
                    ["root_dialogue_id"],
                    ondelete="CASCADE",
                )
        op.execute(
            sa.text(
                f"UPDATE {table} SET budget_root_dialogue_id = "
                "(SELECT budget_root_dialogue_id FROM agent_dialogues "
                f"WHERE agent_dialogues.id = {table}.dialogue_id) "
                "WHERE budget_root_dialogue_id IS NULL"
            )
        )
        index_name = f"ix_{table}_budget_root"
        if index_name not in _indexes(table):
            op.create_index(index_name, table, ["budget_root_dialogue_id", "created_at"])
        if added_root_link:
            with op.batch_alter_table(table) as batch:
                if table == "agent_dialogue_usage":
                    batch.create_unique_constraint("uq_agent_dialogue_usage_root_run", ["budget_root_dialogue_id", "run_id"])
                else:
                    batch.create_unique_constraint("uq_agent_dialogue_reservation_root_key", ["budget_root_dialogue_id", "reservation_key"])


def upgrade() -> None:
    _ensure_dialogue_budget_shape()
    _add_dialogue_links()
    # Repair the legacy usage shape before copying Dialogue usage state into
    # the root owner.  This also fences old rows whose digest contract was
    # absent in the deployed 0003 variant.
    _legacy_usage_shape()
    _create_budget_root_table()
    _ensure_budget_root_table_shape()
    _populate_budget_roots()
    _create_legacy_reservation_table()
    _add_ledger_root_links()


def downgrade() -> None:
    for table, index_name in (
        ("agent_dialogue_reservations", "ix_agent_dialogue_reservations_budget_root"),
        ("agent_dialogue_usage", "ix_agent_dialogue_usage_budget_root"),
    ):
        if _table_exists(table):
            try:
                op.drop_constraint(
                    "uq_agent_dialogue_reservation_root_key" if table.endswith("reservations") else "uq_agent_dialogue_usage_root_run",
                    table_name=table,
                    type_="unique",
                )
            except Exception:
                pass
            try:
                op.drop_index(index_name, table_name=table)
            except Exception:
                pass
            with op.batch_alter_table(table) as batch:
                try:
                    batch.drop_constraint(f"fk_{table}_budget_root", type_="foreignkey")
                except Exception:
                    pass
                if "budget_root_dialogue_id" in _columns(table):
                    batch.drop_column("budget_root_dialogue_id")
    if _table_exists("agent_dialogue_budget_roots"):
        op.drop_index("ix_agent_dialogue_budget_root_owner", table_name="agent_dialogue_budget_roots")
        op.drop_table("agent_dialogue_budget_roots")
    if _table_exists("agent_dialogues"):
        for index_name in ("ix_agent_dialogues_parent", "ix_agent_dialogues_budget_root"):
            try:
                op.drop_index(index_name, table_name="agent_dialogues")
            except Exception:
                pass
        with op.batch_alter_table("agent_dialogues") as batch:
            for constraint in ("ck_agent_dialogues_budget_causal_depth",):
                try:
                    batch.drop_constraint(constraint, type_="check")
                except Exception:
                    pass
            for constraint in ("fk_agent_dialogues_parent_dialogue", "fk_agent_dialogues_budget_root_dialogue"):
                try:
                    batch.drop_constraint(constraint, type_="foreignkey")
                except Exception:
                    pass
            existing = _columns("agent_dialogues")
            for name in ("causal_depth", "parent_dialogue_id", "budget_root_dialogue_id"):
                if name in existing:
                    batch.drop_column(name)
