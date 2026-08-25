"""Reconcile the Project Docs migration-log contract without replaying 0015.

Some installations applied an earlier 0015 variant before the source migration
was corrected.  Those databases have ``root_node_id NOT NULL`` and a status
check that omits ``archived_duplicate``.  This revision repairs only that table
metadata in place; it never re-runs the data move and never changes the
Alembic version during upgrade.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260808_0017"
down_revision = "20260808_0016"
branch_labels = None
depends_on = None


_STATUS_CHECK = (
    "status IN ('moved', 'already_canonical', 'archived_duplicate', "
    "'conflict', 'missing_root')"
)
_LEGACY_STATUS_CHECK = "status IN ('moved', 'already_canonical', 'conflict', 'missing_root')"
_STATUS_VALUES = (
    "moved",
    "already_canonical",
    "archived_duplicate",
    "conflict",
    "missing_root",
)
_REQUIRED_COLUMNS = {
    "id",
    "project_id",
    "legacy_workspace_id",
    "canonical_workspace_id",
    "root_node_id",
    "moved_count",
    "status",
    "metadata",
    "created_at",
}


def _is_offline() -> bool:
    try:
        sa.inspect(op.get_bind())
    except sa.exc.NoInspectionAvailable:
        return True
    return False


def _table_exists(table_name: str) -> bool:
    if _is_offline():
        # The linear migration chain creates this table in 0015 before 0017.
        return True
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _column_metadata(table_name: str, column_name: str) -> dict[str, object] | None:
    if _is_offline():
        return None
    for column in sa.inspect(op.get_bind()).get_columns(table_name):
        if column.get("name") == column_name:
            return column
    return None


def _validate_expected_schema() -> None:
    """Fail closed instead of advancing the revision over a damaged table."""

    if _is_offline():
        return
    inspector = sa.inspect(op.get_bind())
    if "docs_workspace_migration_log" not in inspector.get_table_names():
        raise RuntimeError(
            "20260808_0017 requires docs_workspace_migration_log; "
            "run the 0015 audit/repair procedure before retrying"
        )
    columns = {
        str(column.get("name"))
        for column in inspector.get_columns("docs_workspace_migration_log")
    }
    missing = sorted(_REQUIRED_COLUMNS - columns)
    if missing:
        raise RuntimeError(
            "20260808_0017 cannot reconcile docs_workspace_migration_log; "
            f"missing required columns: {', '.join(missing)}"
        )


def _status_contract_constraints() -> list[dict[str, object]]:
    """Return every check that enforces the migration status enum.

    PostgreSQL assigns a generated name even for ``CHECK (...)`` declarations
    without an explicit name.  We inspect the expression rather than relying
    on the historical constraint name so renamed/unnamed legacy checks are
    removed too; unrelated checks on the same table are left untouched.
    """

    if _is_offline():
        return [{"name": "ck_docs_workspace_migration_log_status", "sqltext": ""}]
    constraints = sa.inspect(op.get_bind()).get_check_constraints(
        "docs_workspace_migration_log"
    )
    result: list[dict[str, object]] = []
    for constraint in constraints:
        name = constraint.get("name")
        sqltext = str(constraint.get("sqltext") or "").lower()
        marker_count = sum(
            1 for value in _STATUS_VALUES if f"'{value}'" in sqltext
        )
        if name == "ck_docs_workspace_migration_log_status" or (
            "status" in sqltext and marker_count >= 2
        ):
            result.append(constraint)
    return result


def _drop_status_contract_constraints() -> None:
    for constraint in _status_contract_constraints():
        name = constraint.get("name")
        if not name:
            # A PostgreSQL constraint always has a catalog name.  Refuse to
            # continue if a non-PostgreSQL/mock inspector violates that
            # invariant instead of leaving an old rejecting CHECK behind.
            raise RuntimeError(
                "20260808_0017 found an unnamed status CHECK and cannot safely "
                "drop it; inspect pg_constraint and retry"
            )
        op.drop_constraint(
            str(name),
            "docs_workspace_migration_log",
            type_="check",
        )


def _status_constraint() -> dict[str, object] | None:
    """Backward-compatible helper retained for migration fixture tests."""

    constraints = _status_contract_constraints()
    return constraints[0] if constraints else None


def _has_null_root_rows() -> bool:
    if _is_offline():
        return False
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM docs_workspace_migration_log "
                "WHERE root_node_id IS NULL LIMIT 1"
            )
        )
        .first()
    )


def _has_archived_duplicate_rows() -> bool:
    if _is_offline():
        return False
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM docs_workspace_migration_log "
                "WHERE status = 'archived_duplicate' LIMIT 1"
            )
        )
        .first()
    )


def upgrade() -> None:
    _validate_expected_schema()
    if not _table_exists("docs_workspace_migration_log"):
        # Offline mode is the only path where table existence is unknowable;
        # the linear chain guarantees 0015 created it before this revision.
        raise RuntimeError(
            "20260808_0017 requires docs_workspace_migration_log in the 0015 chain"
        )

    root_column = _column_metadata("docs_workspace_migration_log", "root_node_id")
    if _is_offline() or (root_column is not None and not bool(root_column.get("nullable"))):
        op.alter_column(
            "docs_workspace_migration_log",
            "root_node_id",
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=True,
        )

    # Drop every status-enum CHECK (including generated/renamed names) and
    # install one canonical constraint.  Unrelated checks remain untouched.
    _drop_status_contract_constraints()
    op.create_check_constraint(
        "ck_docs_workspace_migration_log_status",
        "docs_workspace_migration_log",
        _STATUS_CHECK,
    )


def downgrade() -> None:
    """Restore the pre-reconciliation contract when the data is compatible.

    ``missing_root`` rows intentionally use NULL ``root_node_id`` and
    ``archived_duplicate`` rows are not representable in the old CHECK.  Refuse
    only when those rows exist, before issuing any DDL; ordinary databases can
    therefore use ``alembic downgrade 20260808_0016`` safely.
    """

    _validate_expected_schema()
    if not _table_exists("docs_workspace_migration_log"):
        raise RuntimeError(
            "20260808_0017 downgrade requires docs_workspace_migration_log"
        )
    if _has_null_root_rows():
        raise RuntimeError(
            "cannot downgrade 20260808_0017: migration-log rows have NULL "
            "root_node_id (missing_root audit data would be lost)"
        )
    if _has_archived_duplicate_rows():
        raise RuntimeError(
            "cannot downgrade 20260808_0017: archived_duplicate rows are not "
            "representable by the 0016 status CHECK"
        )

    _drop_status_contract_constraints()
    op.create_check_constraint(
        "ck_docs_workspace_migration_log_status",
        "docs_workspace_migration_log",
        _LEGACY_STATUS_CHECK,
    )
    root_column = _column_metadata("docs_workspace_migration_log", "root_node_id")
    if _is_offline() or (root_column is not None and bool(root_column.get("nullable"))):
        op.alter_column(
            "docs_workspace_migration_log",
            "root_node_id",
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=False,
        )
