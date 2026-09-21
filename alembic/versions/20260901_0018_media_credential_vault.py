"""Add the Media Operations encrypted credential vault and audit ledger.

Credential payloads are ciphertext only.  The audit ledger is append-only and
contains safe status snapshots; PostgreSQL triggers reject updates/deletes.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260901_0018"
down_revision = "20260901_0017"
branch_labels = None
depends_on = None


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def _table_exists(bind: sa.Connection, table_name: str) -> bool:
    try:
        return table_name in sa.inspect(bind).get_table_names()
    except sa.exc.NoInspectionAvailable:
        return False


def _index_exists(bind: sa.Connection, table_name: str, index_name: str) -> bool:
    if not _table_exists(bind, table_name):
        return False
    try:
        inspector = sa.inspect(bind)
        index_names = {
            str(item["name"]) for item in inspector.get_indexes(table_name)
        }
        # PostgreSQL and some legacy SQLite schemas represent a uniqueness
        # guarantee as a named table constraint rather than an index.  Treat
        # either shape as pre-existing so upgrade never attempts to create a
        # duplicate object with the canonical connection-binding name.
        index_names.update(
            str(item["name"])
            for item in inspector.get_unique_constraints(table_name)
            if item.get("name")
        )
        return index_name in index_names
    except (sa.exc.NoInspectionAvailable, sa.exc.NoSuchTableError):
        return False


def _index(name: str, table: str, columns: list[str]) -> None:
    if not _index_exists(op.get_bind(), table, name):
        op.create_index(name, table, columns)


def _assert_unique_connection_bindings(bind: sa.Connection) -> None:
    """Fail with an actionable diagnostic before adding the new unique index.

    Older deployments allowed multiple PlatformAccount rows to point at the
    same ExternalConnection.  Silently choosing a winner would change the
    publication target, so the migration must stop before creating the vault
    schema and let an operator resolve the duplicate bindings explicitly.
    """

    if not _table_exists(bind, "media_platform_accounts"):
        return
    try:
        rows = bind.execute(
            sa.text(
                """
                SELECT connection_id, COUNT(*) AS binding_count
                FROM media_platform_accounts
                WHERE connection_id IS NOT NULL
                GROUP BY connection_id
                HAVING COUNT(*) > 1
                ORDER BY connection_id
                LIMIT 20
                """
            )
        ).fetchall()
    except (sa.exc.DBAPIError, sa.exc.OperationalError) as exc:
        raise RuntimeError(
            "Cannot validate existing PlatformAccount connection bindings "
            "before the credential-vault migration"
        ) from exc
    if rows:
        details = ", ".join(f"{row[0]} ({row[1]} rows)" for row in rows)
        raise RuntimeError(
            "Duplicate PlatformAccount connection bindings block migration; "
            "detach or consolidate these connection_id values, then rerun: "
            f"{details}"
        )


def _external_connection_columns(bind: sa.Connection) -> set[str]:
    """Return the columns available on the legacy connection table.

    The credential vault migration is deployed to installations that may have
    been created by more than one operations-kernel revision.  Build the
    downgrade UPDATE from the columns actually present so clearing vault
    references never fails halfway through (or touches unrelated bindings).
    """

    if not _table_exists(bind, "external_connections"):
        return set()
    try:
        return {
            str(column["name"])
            for column in sa.inspect(bind).get_columns("external_connections")
        }
    except (sa.exc.NoInspectionAvailable, sa.exc.NoSuchTableError):
        return set()


def _clear_vault_connection_refs(bind: sa.Connection) -> None:
    """Detach only vault-owned references and invalidate connection state.

    A credential-vault downgrade removes encrypted rows.  Any surviving
    ``ExternalConnection`` that pointed at those rows must therefore become
    explicitly unknown and advance its optimistic-concurrency version.  The
    vault URI predicate is deliberately narrow; generic/legacy credential
    references are left untouched.
    """

    columns = _external_connection_columns(bind)
    if "credential_ref" not in columns:
        return
    assignments = ["credential_ref = NULL"]
    if "auth_status" in columns:
        assignments.append("auth_status = 'unknown'")
    if "version" in columns:
        assignments.append("version = version + 1")
    if "updated_at" in columns:
        assignments.append("updated_at = CURRENT_TIMESTAMP")
    bind.execute(
        sa.text(
            "UPDATE external_connections SET "
            + ", ".join(assignments)
            + " WHERE credential_ref LIKE :credential_prefix"
        ),
        {"credential_prefix": "credential://media-platform/%"},
    )


def upgrade() -> None:
    bind = op.get_bind()
    # Validate legacy bindings before creating any new table so a duplicate
    # fails atomically and leaves no partial vault schema behind (notably on
    # SQLite, whose DDL transactions vary by driver/version).
    _assert_unique_connection_bindings(bind)
    if not _table_exists(bind, "media_platform_credentials"):
        op.create_table(
            "media_platform_credentials",
            sa.Column("id", _uuid(), nullable=False),
            sa.Column("platform_account_id", _uuid(), nullable=False),
            sa.Column("connection_id", _uuid(), nullable=False),
            sa.Column("owner_user_id", _uuid(), nullable=False),
            sa.Column("project_id", _uuid(), nullable=True),
            sa.Column("connection_type", sa.String(length=24), nullable=False),
            sa.Column("encrypted_payload", sa.Text(), nullable=False),
            sa.Column("encryption_key_id", sa.String(length=128), nullable=False),
            sa.Column("payload_digest", sa.String(length=64), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("state_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'verification_pending'")),
            sa.Column("capabilities", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("verification_code", sa.String(length=64), nullable=True),
            sa.Column("verification_started_at", sa.DateTime(), nullable=True),
            sa.Column("verification_completed_at", sa.DateTime(), nullable=True),
            sa.Column("last_verified_at", sa.DateTime(), nullable=True),
            sa.Column("disabled_at", sa.DateTime(), nullable=True),
            sa.Column("created_by", _uuid(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "connection_type IN ('cookie_export','api_token','oauth')",
                name="ck_media_platform_credentials_connection_type",
            ),
            sa.CheckConstraint(
                "status IN ('verification_pending','verified','invalid','unsupported','disabled','key_unavailable')",
                name="ck_media_platform_credentials_status",
            ),
            sa.CheckConstraint(
                "encrypted_payload LIKE 'enc:v1:aes256gcm:%'",
                name="ck_media_platform_credentials_encrypted_payload_format",
            ),
            sa.CheckConstraint("revision > 0", name="ck_media_platform_credentials_revision"),
            sa.CheckConstraint("length(payload_digest) = 64", name="ck_media_platform_credentials_payload_digest"),
            sa.CheckConstraint("length(state_hash) = 64", name="ck_media_platform_credentials_state_hash"),
            sa.ForeignKeyConstraint(["platform_account_id"], ["media_platform_accounts.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["connection_id"], ["external_connections.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("platform_account_id", name="uq_media_platform_credentials_platform_account"),
            sa.UniqueConstraint("connection_id", name="uq_media_platform_credentials_connection"),
        )
    _index("ix_media_platform_credentials_platform_account_id", "media_platform_credentials", ["platform_account_id"])
    _index("ix_media_platform_credentials_connection_id", "media_platform_credentials", ["connection_id"])
    _index("ix_media_platform_credentials_owner_user_id", "media_platform_credentials", ["owner_user_id"])
    _index("ix_media_platform_credentials_project_id", "media_platform_credentials", ["project_id"])
    _index("ix_media_platform_credentials_status", "media_platform_credentials", ["status"])
    _index("ix_media_platform_credentials_created_at", "media_platform_credentials", ["created_at"])
    _index("ix_media_platform_credentials_updated_at", "media_platform_credentials", ["updated_at"])
    _index("ix_media_platform_credentials_owner_project", "media_platform_credentials", ["owner_user_id", "project_id"])
    _index("ix_media_platform_credentials_status_updated", "media_platform_credentials", ["status", "updated_at"])

    if _table_exists(bind, "media_platform_accounts") and not _index_exists(
        bind, "media_platform_accounts", "uq_media_platform_accounts_connection_id"
    ):
        op.create_index(
            "uq_media_platform_accounts_connection_id",
            "media_platform_accounts",
            ["connection_id"],
            unique=True,
            postgresql_where=sa.text("connection_id IS NOT NULL"),
            sqlite_where=sa.text("connection_id IS NOT NULL"),
        )

    if not _table_exists(bind, "media_platform_credential_audit_events"):
        op.create_table(
            "media_platform_credential_audit_events",
            sa.Column("id", _uuid(), nullable=False),
            sa.Column("credential_id", _uuid(), nullable=False),
            sa.Column("platform_account_id", _uuid(), nullable=False),
            sa.Column("owner_user_id", _uuid(), nullable=False),
            sa.Column("project_id", _uuid(), nullable=True),
            sa.Column("event_type", sa.String(length=16), nullable=False),
            sa.Column("actor_id", _uuid(), nullable=True),
            sa.Column("actor_type", sa.String(length=16), nullable=False, server_default=sa.text("'human'")),
            sa.Column("snapshot_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("request_hash", sa.String(length=64), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("idempotency_scope", sa.String(length=255), nullable=False),
            sa.Column("idempotency_key", sa.String(length=255), nullable=False),
            sa.Column("prev_event_hash", sa.String(length=64), nullable=True),
            sa.Column("event_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "event_type IN ('add','rotate','verify','disable','rekey')",
                name="ck_media_platform_credential_audit_event_type",
            ),
            sa.CheckConstraint(
                "actor_type IN ('human','admin','unknown')",
                name="ck_media_platform_credential_audit_actor_type",
            ),
            sa.CheckConstraint("length(request_hash) = 64", name="ck_media_platform_credential_audit_request_hash"),
            sa.CheckConstraint("length(event_hash) = 64", name="ck_media_platform_credential_audit_event_hash"),
            sa.CheckConstraint("sequence > 0", name="ck_media_platform_credential_audit_sequence"),
            sa.CheckConstraint("prev_event_hash IS NULL OR length(prev_event_hash) = 64", name="ck_media_platform_credential_audit_prev_hash"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("idempotency_scope", "idempotency_key", name="uq_media_platform_credential_audit_scope_key"),
            sa.UniqueConstraint("credential_id", "sequence", name="uq_media_platform_credential_audit_sequence"),
        )
    _index("ix_media_platform_credential_audit_events_credential_id", "media_platform_credential_audit_events", ["credential_id"])
    _index("ix_media_platform_credential_audit_events_platform_account_id", "media_platform_credential_audit_events", ["platform_account_id"])
    _index("ix_media_platform_credential_audit_events_owner_user_id", "media_platform_credential_audit_events", ["owner_user_id"])
    _index("ix_media_platform_credential_audit_events_project_id", "media_platform_credential_audit_events", ["project_id"])
    _index("ix_media_platform_credential_audit_events_created_at", "media_platform_credential_audit_events", ["created_at"])
    _index("ix_media_platform_credential_audit_account_created", "media_platform_credential_audit_events", ["platform_account_id", "created_at"])
    _index("ix_media_platform_credential_audit_owner_project_created", "media_platform_credential_audit_events", ["owner_user_id", "project_id", "created_at"])

    # Install a database-level append-only guard for every supported runtime
    # dialect in addition to the service-level controls.
    if getattr(bind.dialect, "name", "") == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION media_platform_credential_audit_immutable()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              RAISE EXCEPTION 'media credential audit events are immutable';
            END;
            $$;
            """
        )
        op.execute(
            """
            CREATE TRIGGER media_platform_credential_audit_no_update
            BEFORE UPDATE OR DELETE ON media_platform_credential_audit_events
            FOR EACH ROW EXECUTE FUNCTION media_platform_credential_audit_immutable();
            """
        )
        op.execute(
            """
            CREATE TRIGGER media_platform_credential_audit_no_truncate
            BEFORE TRUNCATE ON media_platform_credential_audit_events
            FOR EACH STATEMENT EXECUTE FUNCTION media_platform_credential_audit_immutable();
            """
        )
    elif getattr(bind.dialect, "name", "") == "sqlite":
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


def downgrade() -> None:
    bind = op.get_bind()
    # Never touch generic external-connection references.  Only references
    # created by this migration are cleared before dropping vault rows.
    try:
        inspection_available = sa.inspect(bind)
    except sa.exc.NoInspectionAvailable:
        inspection_available = None
    if getattr(bind.dialect, "name", "") == "postgresql":
        _clear_vault_connection_refs(bind)
        if _table_exists(bind, "media_platform_credential_audit_events"):
            op.execute(
                "DROP TRIGGER IF EXISTS media_platform_credential_audit_no_update "
                "ON media_platform_credential_audit_events"
            )
            op.execute(
                "DROP TRIGGER IF EXISTS media_platform_credential_audit_no_truncate "
                "ON media_platform_credential_audit_events"
            )
        op.execute("DROP FUNCTION IF EXISTS media_platform_credential_audit_immutable()")
    elif inspection_available is None or _table_exists(bind, "external_connections"):
        _clear_vault_connection_refs(bind)
    if getattr(bind.dialect, "name", "") == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS media_platform_credential_audit_no_update")
        op.execute("DROP TRIGGER IF EXISTS media_platform_credential_audit_no_delete")
    if inspection_available is None or _table_exists(bind, "media_platform_credential_audit_events"):
        op.drop_table("media_platform_credential_audit_events")
    if inspection_available is None or _table_exists(bind, "media_platform_credentials"):
        op.drop_table("media_platform_credentials")
    # Do not drop ``uq_media_platform_accounts_connection_id`` here.  An
    # earlier deployment may have created an index with this canonical name;
    # after Alembic reload there is no durable ownership marker that can
    # distinguish it from the index created above.  Leaving the index in
    # place is safe and, importantly, never destroys a pre-existing
    # connection-uniqueness guarantee during a vault downgrade.
