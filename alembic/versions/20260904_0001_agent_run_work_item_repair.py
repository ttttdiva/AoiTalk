"""Repair the durable AgentRun work-item attempt schema.

The ``20260903_0002`` revision introduced the work-item links on
``agent_runs``.  Some databases were already stamped at that revision while
the ``work_item_attempt`` column and its indexes were still absent.  Alembic
therefore considered those databases current and the startup reconciliation
query failed when SQLAlchemy selected the mapped column.

This revision is intentionally additive and conditional.  It repairs existing
databases without editing the already-published ``20260903_0002`` revision,
and it is a no-op when a database already has the complete contract.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260904_0001"
down_revision = "20260903_0002"
branch_labels = None
depends_on = None


_TABLE = "agent_runs"
_WORK_ITEM = "work_item_id"
_ATTEMPT = "work_item_attempt"
_ATTEMPT_INDEX = "ix_agent_runs_work_item_attempt"
_WORK_ITEM_CREATED_INDEX = "ix_agent_runs_work_item_created"
_ATTEMPT_UNIQUE_INDEX = "uq_agent_runs_work_item_attempt"


def _table_exists() -> bool:
    try:
        return inspect(op.get_bind()).has_table(_TABLE)
    except Exception:
        return False


def _columns() -> set[str]:
    try:
        return {item["name"] for item in inspect(op.get_bind()).get_columns(_TABLE)}
    except Exception:
        return set()


def _indexes() -> set[str]:
    try:
        return {item["name"] for item in inspect(op.get_bind()).get_indexes(_TABLE)}
    except Exception:
        return set()


def _add_attempt_column() -> None:
    if not _table_exists() or _ATTEMPT in _columns():
        return

    # ``work_item_attempt`` is nullable and has no foreign-key or check
    # constraint.  Native ADD COLUMN works on PostgreSQL and SQLite alike and
    # avoids recreating ``agent_runs`` (which has self-referential dependants).
    op.add_column(_TABLE, sa.Column(_ATTEMPT, sa.Integer(), nullable=True))


def _create_indexes() -> None:
    columns = _columns()
    if not {_WORK_ITEM, _ATTEMPT} <= columns:
        # The preceding 0002 revision owns ``work_item_id``.  Do not invent a
        # second table shape here when that prerequisite is missing; leaving
        # the migration failed/visible is safer than creating an unusable
        # partial uniqueness contract.
        return

    indexes = _indexes()
    if _ATTEMPT_INDEX not in indexes:
        op.create_index(_ATTEMPT_INDEX, _TABLE, [_ATTEMPT])

    if "created_at" in columns and _WORK_ITEM_CREATED_INDEX not in indexes:
        op.create_index(_WORK_ITEM_CREATED_INDEX, _TABLE, [_WORK_ITEM, "created_at"])

    if _ATTEMPT_UNIQUE_INDEX not in indexes:
        op.create_index(
            _ATTEMPT_UNIQUE_INDEX,
            _TABLE,
            [_WORK_ITEM, _ATTEMPT],
            unique=True,
            postgresql_where=sa.text(
                f"{_WORK_ITEM} IS NOT NULL AND {_ATTEMPT} IS NOT NULL"
            ),
            sqlite_where=sa.text(
                f"{_WORK_ITEM} IS NOT NULL AND {_ATTEMPT} IS NOT NULL"
            ),
        )


def upgrade() -> None:
    _add_attempt_column()
    _create_indexes()


def _drop_index_if_present(name: str) -> None:
    if name in _indexes():
        op.drop_index(name, table_name=_TABLE)


def downgrade() -> None:
    if not _table_exists():
        return

    _drop_index_if_present(_ATTEMPT_UNIQUE_INDEX)
    _drop_index_if_present(_WORK_ITEM_CREATED_INDEX)
    _drop_index_if_present(_ATTEMPT_INDEX)
    if _ATTEMPT in _columns():
        op.drop_column(_TABLE, _ATTEMPT)
