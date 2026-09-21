"""Keep immutable credential audit rows independent of actor lifecycle.

The audit ledger intentionally preserves historical actor identifiers.  A
``SET NULL`` foreign key would issue an UPDATE when a user is deleted, which
the append-only audit trigger correctly rejects.  New installs omit the FK;
this migration removes it from already-migrated PostgreSQL deployments.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260902_0020"
down_revision = "20260902_0019"
branch_labels = None
depends_on = None


_TABLE = "media_platform_credential_audit_events"


def _index_exists(bind: sa.Connection, table_name: str, index_name: str) -> bool:
    try:
        return index_name in {
            str(item["name"]) for item in sa.inspect(bind).get_indexes(table_name)
        }
    except (sa.exc.NoSuchTableError, sa.exc.NoInspectionAvailable):
        return False


def _actor_fk_names(bind: sa.Connection) -> list[str]:
    try:
        foreign_keys = sa.inspect(bind).get_foreign_keys(_TABLE)
    except (sa.exc.NoSuchTableError, sa.exc.NoInspectionAvailable):
        return []
    names: list[str] = []
    for item in foreign_keys:
        columns = {str(value) for value in item.get("constrained_columns") or ()}
        name = item.get("name")
        if columns == {"actor_id"}:
            # SQLite commonly reports an unnamed inline FK.  Keep an empty
            # marker so the caller can still detect and rebuild that table.
            names.append(str(name) if name else "")
    return names


def _sqlite_rebuild_without_actor_fk(bind: sa.Connection) -> None:
    """Rebuild a legacy SQLite audit table whose actor FK is unnamed.

    SQLite cannot drop a single foreign-key constraint in place.  Reflect the
    existing table, remove only the actor constraint from the copy metadata,
    and let Alembic's batch recreate preserve every row and named index.  The
    immutable triggers are recreated after the replacement table exists.
    """

    try:
        inspector = sa.inspect(bind)
        indexes = [
            (
                str(item["name"]),
                [str(column) for column in item.get("column_names") or ()],
                bool(item.get("unique")),
            )
            for item in inspector.get_indexes(_TABLE)
            if item.get("name") and item.get("column_names")
        ]
    except (sa.exc.NoSuchTableError, sa.exc.NoInspectionAvailable):
        return

    reflected = sa.Table(_TABLE, sa.MetaData(), autoload_with=bind)
    for constraint in list(reflected.constraints):
        if isinstance(constraint, sa.ForeignKeyConstraint):
            columns = {str(column.name) for column in constraint.columns}
            if columns == {"actor_id"}:
                reflected.constraints.remove(constraint)

    # Triggers belong to the old table definition and must be recreated on
    # the replacement table.  Dropping them also prevents an implementation-
    # specific rename/copy sequence from leaving stale trigger attachments.
    bind.execute(sa.text("DROP TRIGGER IF EXISTS media_platform_credential_audit_no_update"))
    bind.execute(sa.text("DROP TRIGGER IF EXISTS media_platform_credential_audit_no_delete"))
    with op.batch_alter_table(_TABLE, recreate="always", copy_from=reflected):
        pass

    for name, columns, unique in indexes:
        if not _index_exists(bind, _TABLE, name):
            op.create_index(name, _TABLE, columns, unique=unique)
    op.execute(
        """
        CREATE TRIGGER media_platform_credential_audit_no_update
        BEFORE UPDATE ON media_platform_credential_audit_events
        BEGIN
          SELECT RAISE(ABORT, 'media credential audit events are immutable');
        END;
        """
    )
    op.execute(
        """
        CREATE TRIGGER media_platform_credential_audit_no_delete
        BEFORE DELETE ON media_platform_credential_audit_events
        BEGIN
          SELECT RAISE(ABORT, 'media credential audit events are immutable');
        END;
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    actor_fks = _actor_fk_names(bind)
    # PostgreSQL gives unnamed ForeignKeyConstraint objects a deterministic
    # generated name, which the inspector exposes for a safe drop.
    if getattr(bind.dialect, "name", "") == "postgresql":
        for name in actor_fks:
            if not name:
                continue
            op.drop_constraint(name, _TABLE, type_="foreignkey")
    elif getattr(bind.dialect, "name", "") == "sqlite" and actor_fks:
        _sqlite_rebuild_without_actor_fk(bind)


def downgrade() -> None:
    """Canonical audit history is forward-only.

    Re-adding an actor foreign key on downgrade would make deletion of a user
    issue an UPDATE against the append-only audit ledger (and would require
    durable provenance that this migration cannot infer).  Keep actor IDs as
    historical, non-FK values and make the downgrade a safe no-op.
    """

    return None
