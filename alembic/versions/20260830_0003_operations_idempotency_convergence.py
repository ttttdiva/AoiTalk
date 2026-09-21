"""Converge legacy Operations idempotency uniqueness to scoped indexes.

Revision 0002 was corrected before merge, but an isolated persistent QA schema
had already recorded its earlier body.  Alembic tracks revision ids rather
than migration-file hashes, so this forward-only schema correction safely
converges both the stale and corrected 0002 physical layouts without changing
business rows.
"""

from __future__ import annotations

import re
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "20260830_0003"
down_revision = "20260830_0002"
branch_labels = None
depends_on = None


_TABLE = "external_actions"
_OLD = "uq_external_actions_idempotency"
_PERSONAL = "uq_external_actions_personal_idempotency"
_PROJECT = "uq_external_actions_project_idempotency"


def _normalize_predicate(value: Any) -> str:
    rendered = re.sub(r"\s+", " ", str(value or "").replace('"', "").strip().lower())
    while rendered.startswith("(") and rendered.endswith(")"):
        rendered = rendered[1:-1].strip()
    return rendered


def _index_matches(
    item: dict[str, Any],
    *,
    columns: list[str],
    predicate: str,
) -> bool:
    dialect_options = item.get("dialect_options") or {}
    actual_predicate = dialect_options.get("postgresql_where")
    return (
        item.get("unique") is True
        and list(item.get("column_names") or []) == columns
        and _normalize_predicate(actual_predicate) == _normalize_predicate(predicate)
    )


def _assert_no_scoped_duplicates(bind: Any) -> None:
    checks = (
        """
        SELECT owner_user_id, idempotency_key
        FROM external_actions
        WHERE project_id IS NULL
        GROUP BY owner_user_id, idempotency_key
        HAVING count(*) > 1
        LIMIT 1
        """,
        """
        SELECT project_id, idempotency_key
        FROM external_actions
        WHERE project_id IS NOT NULL
        GROUP BY project_id, idempotency_key
        HAVING count(*) > 1
        LIMIT 1
        """,
    )
    for statement in checks:
        if bind.execute(sa.text(statement)).first() is not None:
            raise RuntimeError(
                "Operations idempotency convergence found duplicate scoped keys; "
                "manual review is required"
            )


def _physical_state(bind: Any) -> tuple[set[str], dict[str, dict[str, Any]]]:
    inspector = sa.inspect(bind)
    constraints = {
        str(item.get("name"))
        for item in inspector.get_unique_constraints(_TABLE)
        if item.get("name")
    }
    indexes = {
        str(item.get("name")): item
        for item in inspector.get_indexes(_TABLE)
        if item.get("name")
    }
    return constraints, indexes


def _validate_desired_indexes(bind: Any) -> None:
    constraints, indexes = _physical_state(bind)
    if _OLD in constraints or _OLD in indexes:
        raise RuntimeError("legacy Operations idempotency uniqueness still exists")
    expected = {
        _PERSONAL: (["owner_user_id", "idempotency_key"], "project_id IS NULL"),
        _PROJECT: (["project_id", "idempotency_key"], "project_id IS NOT NULL"),
    }
    for name, (columns, predicate) in expected.items():
        item = indexes.get(name)
        if item is None or not _index_matches(
            item,
            columns=columns,
            predicate=predicate,
        ):
            raise RuntimeError(f"Operations idempotency index {name} is missing or malformed")


def upgrade() -> None:
    bind = op.get_bind()
    _assert_no_scoped_duplicates(bind)
    constraints, indexes = _physical_state(bind)

    expected = {
        _PERSONAL: (["owner_user_id", "idempotency_key"], "project_id IS NULL"),
        _PROJECT: (["project_id", "idempotency_key"], "project_id IS NOT NULL"),
    }
    for name, (columns, predicate) in expected.items():
        existing = indexes.get(name)
        if existing is not None and not _index_matches(
            existing,
            columns=columns,
            predicate=predicate,
        ):
            raise RuntimeError(f"Operations idempotency index {name} has an unsafe definition")

    if _OLD in constraints:
        op.drop_constraint(_OLD, _TABLE, type_="unique")
    elif _OLD in indexes:
        op.drop_index(_OLD, table_name=_TABLE)

    if _PERSONAL not in indexes:
        op.create_index(
            _PERSONAL,
            _TABLE,
            ["owner_user_id", "idempotency_key"],
            unique=True,
            postgresql_where=sa.text("project_id IS NULL"),
        )
    if _PROJECT not in indexes:
        op.create_index(
            _PROJECT,
            _TABLE,
            ["project_id", "idempotency_key"],
            unique=True,
            postgresql_where=sa.text("project_id IS NOT NULL"),
        )

    _validate_desired_indexes(bind)


def downgrade() -> None:
    # Canonical 0002 already defines the two scoped partial indexes.  A
    # downgrade of this convergence marker therefore validates and preserves
    # that physical state rather than recreating the unsafe global constraint.
    _validate_desired_indexes(op.get_bind())
