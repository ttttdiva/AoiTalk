"""Add finite Dialogue budgets and idempotent Run usage receipts.

This migration follows the additive AoiDots dialogue migration.  Existing
dialogues receive the finite attempt defaults and an unknown usage fence;
token/cost ceilings remain NULL until a caller explicitly supplies them.
Unknown provider usage is represented by ``usage_state`` and is never
backfilled as zero.  New rows inserted after the migration begin with the
empty ``known`` default and are finalized by the usage receipt hook.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "20261012_0003"
down_revision = "20261012_0002"
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


def _add_dialogue_columns() -> None:
    if not _table_exists("agent_dialogues"):
        return
    existing = _columns("agent_dialogues")
    columns = (
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
        sa.Column("usage_state", sa.String(length=24), nullable=False, server_default=sa.text("'known'")),
    )
    with op.batch_alter_table("agent_dialogues") as batch:
        for column in columns:
            if column.name not in existing:
                batch.add_column(column)
        batch.create_check_constraint("ck_agent_dialogues_budget_max_attempts", "max_attempts BETWEEN 1 AND 100")
        batch.create_check_constraint("ck_agent_dialogues_budget_attempt_count", "attempt_count BETWEEN 0 AND max_attempts")
        batch.create_check_constraint("ck_agent_dialogues_budget_max_tokens", "max_tokens IS NULL OR max_tokens BETWEEN 1 AND 1000000")
        batch.create_check_constraint("ck_agent_dialogues_budget_token_usage", "input_tokens >= 0 AND output_tokens >= 0 AND cached_tokens >= 0 AND total_tokens >= 0")
        batch.create_check_constraint("ck_agent_dialogues_budget_reserved_tokens", "reserved_tokens >= 0")
        batch.create_check_constraint("ck_agent_dialogues_budget_max_cost", "max_cost_micros IS NULL OR max_cost_micros BETWEEN 1 AND 1000000000000")
        batch.create_check_constraint("ck_agent_dialogues_budget_cost", "cost_micros IS NULL OR cost_micros >= 0")
        batch.create_check_constraint("ck_agent_dialogues_budget_reserved_cost", "reserved_cost_micros >= 0")
        batch.create_check_constraint("ck_agent_dialogues_budget_usage_state", "usage_state IN ('known','unknown','cost_unknown','token_unknown')")
    # Every row that existed before this migration has no Dialogue usage
    # receipt ledger.  Its zero-valued token/cost columns are therefore an
    # empty observation, never measured usage.  Fence those rows as unknown;
    # rows inserted after this migration keep the server default ``known``
    # until their first Run receipt is recorded.
    op.execute(sa.text("UPDATE agent_dialogues SET usage_state = 'unknown'"))


def _create_usage_table() -> None:
    table = "agent_dialogue_usage"
    if _table_exists(table):
        return
    op.create_table(
        table,
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("dialogue_id", _uuid_type(), nullable=False),
        sa.Column("run_id", _uuid_type(), nullable=False),
        sa.Column("usage_key", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False, server_default=sa.text("'known'")),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cached_tokens", sa.BigInteger(), nullable=True),
        sa.Column("total_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("cost_currency", sa.String(length=16), nullable=True),
        sa.Column("reason", sa.String(length=128), nullable=True),
        sa.Column("usage_digest", sa.String(length=64), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["dialogue_id"], ["agent_dialogues.id"], name="fk_agent_dialogue_usage_dialogue", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], name="fk_agent_dialogue_usage_run", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dialogue_id", "run_id", name="uq_agent_dialogue_usage_dialogue_run"),
        sa.UniqueConstraint("dialogue_id", "usage_key", name="uq_agent_dialogue_usage_dialogue_key"),
        sa.CheckConstraint("state IN ('known','unknown','cost_unknown','token_unknown')", name="ck_agent_dialogue_usage_state"),
        sa.CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="ck_agent_dialogue_usage_input_tokens"),
        sa.CheckConstraint("output_tokens IS NULL OR output_tokens >= 0", name="ck_agent_dialogue_usage_output_tokens"),
        sa.CheckConstraint("cached_tokens IS NULL OR cached_tokens >= 0", name="ck_agent_dialogue_usage_cached_tokens"),
        sa.CheckConstraint("total_tokens IS NULL OR total_tokens >= 0", name="ck_agent_dialogue_usage_total_tokens"),
        sa.CheckConstraint("cost_micros IS NULL OR cost_micros >= 0", name="ck_agent_dialogue_usage_cost_micros"),
        sa.CheckConstraint("length(usage_key) BETWEEN 1 AND 255", name="ck_agent_dialogue_usage_key"),
        sa.CheckConstraint("length(usage_digest) = 64", name="ck_agent_dialogue_usage_digest"),
    )
    op.create_index("ix_agent_dialogue_usage_dialogue", table, ["dialogue_id", "created_at"])
    op.create_index("ix_agent_dialogue_usage_state", table, ["state", "created_at"])


def _create_reservation_table() -> None:
    table = "agent_dialogue_reservations"
    if _table_exists(table):
        return
    op.create_table(
        table,
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("dialogue_id", _uuid_type(), nullable=False),
        sa.Column("run_id", _uuid_type(), nullable=True),
        sa.Column("reservation_key", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default=sa.text("'reserved'")),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("reserved_cost_micros", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("charged_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("charged_cost_micros", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["dialogue_id"], ["agent_dialogues.id"], name="fk_agent_dialogue_reservation_dialogue", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], name="fk_agent_dialogue_reservation_run", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dialogue_id", "reservation_key", name="uq_agent_dialogue_reservation_key"),
        sa.CheckConstraint("state IN ('reserved','released','charged','overrun')", name="ck_agent_dialogue_reservation_state"),
        sa.CheckConstraint("reserved_tokens >= 0 AND reserved_cost_micros >= 0", name="ck_agent_dialogue_reservation_reserved"),
        sa.CheckConstraint("charged_tokens >= 0 AND charged_cost_micros >= 0", name="ck_agent_dialogue_reservation_charged"),
        sa.CheckConstraint("length(reservation_key) BETWEEN 1 AND 255", name="ck_agent_dialogue_reservation_key"),
    )
    op.create_index("ix_agent_dialogue_reservation_dialogue", table, ["dialogue_id", "created_at"])
    op.create_index("ix_agent_dialogue_reservation_state", table, ["state", "created_at"])


def upgrade() -> None:
    _add_dialogue_columns()
    _create_usage_table()
    _create_reservation_table()


def downgrade() -> None:
    if _table_exists("agent_dialogue_reservations"):
        op.drop_index("ix_agent_dialogue_reservation_state", table_name="agent_dialogue_reservations")
        op.drop_index("ix_agent_dialogue_reservation_dialogue", table_name="agent_dialogue_reservations")
        op.drop_table("agent_dialogue_reservations")
    if _table_exists("agent_dialogue_usage"):
        op.drop_index("ix_agent_dialogue_usage_state", table_name="agent_dialogue_usage")
        op.drop_index("ix_agent_dialogue_usage_dialogue", table_name="agent_dialogue_usage")
        op.drop_table("agent_dialogue_usage")
    if _table_exists("agent_dialogues"):
        with op.batch_alter_table("agent_dialogues") as batch:
            for constraint in (
                "ck_agent_dialogues_budget_usage_state",
                "ck_agent_dialogues_budget_reserved_cost",
                "ck_agent_dialogues_budget_cost",
                "ck_agent_dialogues_budget_max_cost",
                "ck_agent_dialogues_budget_reserved_tokens",
                "ck_agent_dialogues_budget_token_usage",
                "ck_agent_dialogues_budget_max_tokens",
                "ck_agent_dialogues_budget_attempt_count",
                "ck_agent_dialogues_budget_max_attempts",
            ):
                batch.drop_constraint(constraint, type_="check")
            existing = _columns("agent_dialogues")
            for name in (
                "usage_state",
                "cost_currency",
                "cost_micros",
                "reserved_cost_micros",
                "max_cost_micros",
                "total_tokens",
                "reserved_tokens",
                "cached_tokens",
                "output_tokens",
                "input_tokens",
                "max_tokens",
                "attempt_count",
                "max_attempts",
            ):
                if name in existing:
                    batch.drop_column(name)
