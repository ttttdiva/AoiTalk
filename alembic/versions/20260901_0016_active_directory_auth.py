"""Add explicit AD authentication metadata and immutable identity bindings.

Existing ``users`` rows are local accounts and keep their bcrypt hashes.  AD
accounts are created by the authentication service with ``auth_source='ad'``
and a NULL ``password_hash``; the directory remains the sole credential
authority.  The binding table stores only the immutable objectGUID (as a UUID)
and configured authority, never a password, DN, or directory export.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260901_0016"
down_revision = "20260901_0015"
branch_labels = None
depends_on = None


def _is_sqlite() -> bool:
    bind = op.get_bind()
    return str(getattr(getattr(bind, "dialect", None), "name", "")).lower() == "sqlite"


def _uuid_type() -> sa.types.TypeEngine:
    # PostgreSQL is the production dialect.  A bounded text representation
    # keeps the migration executable against the SQLite fixtures used by a few
    # repository tests, while the SQLAlchemy model remains UUID-native.
    return sa.String(length=36) if _is_sqlite() else postgresql.UUID(as_uuid=True)


_AUTH_SOURCE_CHECK = "auth_source IN ('local', 'ad')"
_AUTH_HASH_CHECK = (
    "(auth_source = 'local' AND password_hash IS NOT NULL) "
    "OR (auth_source = 'ad' AND password_hash IS NULL)"
)
_AD_RESET_CHECK = "auth_source = 'local' OR is_password_reset_required IS FALSE"


def _create_user_columns_and_checks() -> None:
    """Make password hashes nullable and add source invariants."""

    if _is_sqlite():
        # SQLite cannot ALTER a column's nullability or add a check constraint
        # in place.  Batch recreation preserves all existing user rows and
        # indexes while applying the same constraints as PostgreSQL.
        with op.batch_alter_table("users", recreate="always") as batch:
            batch.alter_column(
                "password_hash",
                existing_type=sa.String(length=255),
                nullable=True,
            )
            batch.create_check_constraint("ck_users_auth_source", _AUTH_SOURCE_CHECK)
            batch.create_check_constraint(
                "ck_users_auth_source_password_hash", _AUTH_HASH_CHECK
            )
            batch.create_check_constraint(
                "ck_users_ad_password_reset_disabled", _AD_RESET_CHECK
            )
        return

    op.alter_column(
        "users",
        "password_hash",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.create_check_constraint("ck_users_auth_source", "users", _AUTH_SOURCE_CHECK)
    op.create_check_constraint(
        "ck_users_auth_source_password_hash", "users", _AUTH_HASH_CHECK
    )
    op.create_check_constraint(
        "ck_users_ad_password_reset_disabled", "users", _AD_RESET_CHECK
    )


def _drop_user_columns_and_checks() -> None:
    if _is_sqlite():
        with op.batch_alter_table("users", recreate="always") as batch:
            batch.drop_constraint("ck_users_ad_password_reset_disabled", type_="check")
            batch.drop_constraint("ck_users_auth_source_password_hash", type_="check")
            batch.drop_constraint("ck_users_auth_source", type_="check")
            batch.alter_column(
                "password_hash",
                existing_type=sa.String(length=255),
                nullable=False,
            )
            batch.drop_column("auth_source")
        return

    op.drop_constraint("ck_users_ad_password_reset_disabled", "users", type_="check")
    op.drop_constraint("ck_users_auth_source_password_hash", "users", type_="check")
    op.drop_constraint("ck_users_auth_source", "users", type_="check")
    op.alter_column(
        "users",
        "password_hash",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.drop_column("users", "auth_source")


def _install_postgres_guards() -> None:
    """Install fail-closed guards for direct SQL credential mutations."""

    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION aoitalk_guard_ad_user_credentials()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    IF OLD.auth_source = 'ad' THEN
                        RAISE EXCEPTION 'Active Directory users cannot be permanently deleted'
                            USING ERRCODE = 'check_violation';
                    END IF;
                    RETURN OLD;
                END IF;

                IF NEW.auth_source IS DISTINCT FROM OLD.auth_source THEN
                    RAISE EXCEPTION 'Authentication source is immutable'
                        USING ERRCODE = 'check_violation';
                END IF;

                IF OLD.auth_source = 'ad'
                   AND (
                       NEW.password_hash IS DISTINCT FROM OLD.password_hash
                       OR NEW.is_password_reset_required IS DISTINCT FROM OLD.is_password_reset_required
                   ) THEN
                    RAISE EXCEPTION 'Active Directory credentials are managed by the directory'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END;
            $$;

            CREATE TRIGGER trg_users_guard_ad_credentials
            BEFORE UPDATE OF auth_source, password_hash, is_password_reset_required
                OR DELETE ON users
            FOR EACH ROW
            EXECUTE FUNCTION aoitalk_guard_ad_user_credentials();

            CREATE OR REPLACE FUNCTION aoitalk_guard_external_identity_binding()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'External identity bindings are immutable'
                        USING ERRCODE = 'check_violation';
                END IF;
                IF NEW.user_id IS DISTINCT FROM OLD.user_id
                   OR NEW.source IS DISTINCT FROM OLD.source
                   OR NEW.authority IS DISTINCT FROM OLD.authority
                   OR NEW.external_id IS DISTINCT FROM OLD.external_id
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                    RAISE EXCEPTION 'External identity bindings are immutable'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END;
            $$;

            CREATE TRIGGER trg_external_identity_bindings_immutable
            BEFORE UPDATE OR DELETE ON external_identity_bindings
            FOR EACH ROW
            EXECUTE FUNCTION aoitalk_guard_external_identity_binding();
            """
        )
    )


def _install_sqlite_guards() -> None:
    # SQLite fixtures do not have PostgreSQL trigger functions.  These
    # equivalent triggers keep the same fail-closed semantics for tests and
    # local development.
    statements = (
        """
        CREATE TRIGGER trg_users_guard_ad_credentials
        BEFORE UPDATE OF auth_source, password_hash, is_password_reset_required ON users
        WHEN NEW.auth_source IS NOT OLD.auth_source
          OR (OLD.auth_source = 'ad' AND (
                NEW.password_hash IS NOT OLD.password_hash
                OR NEW.is_password_reset_required IS NOT OLD.is_password_reset_required
          ))
        BEGIN
            SELECT RAISE(ABORT, 'Active Directory credentials are managed by the directory');
        END
        """,
        """
        CREATE TRIGGER trg_users_guard_ad_delete
        BEFORE DELETE ON users
        WHEN OLD.auth_source = 'ad'
        BEGIN
            SELECT RAISE(ABORT, 'Active Directory users cannot be permanently deleted');
        END
        """,
        """
        CREATE TRIGGER trg_external_identity_bindings_immutable_update
        BEFORE UPDATE ON external_identity_bindings
        WHEN NEW.user_id IS NOT OLD.user_id
          OR NEW.source IS NOT OLD.source
          OR NEW.authority IS NOT OLD.authority
          OR NEW.external_id IS NOT OLD.external_id
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'External identity bindings are immutable');
        END
        """,
        """
        CREATE TRIGGER trg_external_identity_bindings_immutable_delete
        BEFORE DELETE ON external_identity_bindings
        BEGIN
            SELECT RAISE(ABORT, 'External identity bindings are immutable');
        END
        """,
    )
    for statement in statements:
        op.execute(sa.text(statement))


def _drop_guards() -> None:
    if _is_sqlite():
        op.execute(sa.text("DROP TRIGGER IF EXISTS trg_external_identity_bindings_immutable_delete"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS trg_external_identity_bindings_immutable_update"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS trg_users_guard_ad_delete"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS trg_users_guard_ad_credentials"))
        return
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_external_identity_bindings_immutable ON external_identity_bindings"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS aoitalk_guard_external_identity_binding()"))
    op.execute(
        sa.text("DROP TRIGGER IF EXISTS trg_users_guard_ad_credentials ON users")
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS aoitalk_guard_ad_user_credentials()"))


def _assert_no_ad_users_before_downgrade() -> None:
    """Refuse a downgrade that would require inventing local password hashes.

    The reverse migration restores ``users.password_hash`` to NOT NULL and
    removes the source/binding contract.  An AD shadow account cannot be
    represented safely by that legacy schema, so preserve the live schema and
    fail before dropping the immutable binding table instead of manufacturing
    a fake credential or partially applying a downgrade.
    """

    bind = op.get_bind()
    result = bind.execute(
        sa.text("SELECT count(*) FROM users WHERE auth_source = 'ad'")
    )
    count = int(result.scalar() or 0)
    if count:
        raise RuntimeError(
            "Cannot downgrade active_directory_auth while AD-managed users exist"
        )


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "auth_source",
            sa.String(length=16),
            nullable=False,
            server_default="local",
        ),
    )
    _create_user_columns_and_checks()
    op.create_index("ix_users_auth_source", "users", ["auth_source"])

    uuid_type = _uuid_type()
    op.create_table(
        "external_identity_bindings",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column(
            "user_id",
            uuid_type,
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="ad",
        ),
        sa.Column("authority", sa.String(length=255), nullable=False),
        sa.Column("external_id", uuid_type, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_external_identity_bindings_user", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_external_identity_bindings"),
        sa.CheckConstraint(
            "source = 'ad'", name="ck_external_identity_bindings_source_ad"
        ),
        sa.CheckConstraint(
            "length(authority) BETWEEN 1 AND 255",
            name="ck_external_identity_bindings_authority_length",
        ),
        sa.UniqueConstraint(
            "source",
            "authority",
            "external_id",
            name="uq_external_identity_bindings_source_authority_external",
        ),
        sa.UniqueConstraint(
            "user_id", "source", name="uq_external_identity_bindings_user_source"
        ),
    )
    op.create_index(
        "ix_external_identity_bindings_user_id",
        "external_identity_bindings",
        ["user_id"],
    )
    op.create_index(
        "ix_external_identity_bindings_source_authority_external",
        "external_identity_bindings",
        ["source", "authority", "external_id"],
    )

    if _is_sqlite():
        _install_sqlite_guards()
    else:
        _install_postgres_guards()


def downgrade() -> None:
    _assert_no_ad_users_before_downgrade()
    _drop_guards()
    op.drop_index(
        "ix_external_identity_bindings_source_authority_external",
        table_name="external_identity_bindings",
    )
    op.drop_index(
        "ix_external_identity_bindings_user_id",
        table_name="external_identity_bindings",
    )
    op.drop_table("external_identity_bindings")
    op.drop_index("ix_users_auth_source", table_name="users")
    _drop_user_columns_and_checks()
