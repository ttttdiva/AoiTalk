"""Canonical project Docs, subtree ACLs, integration credentials, and task policy.

This migration is deliberately the single cross-client contract for the
canonical Docs workspace, per-user Hugging Face/Hydrus settings, and the
``auto_close_on_due`` task flag.  Sensitive integration data is stored only as
application field-crypto ciphertext in ``encrypted_payload``.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260808_0013"
down_revision = "20260808_0012"
branch_labels = None
depends_on = None


_NORMALIZE_GLOBAL_ROLE_SQL = """
UPDATE users
SET role = 'user'
WHERE role IN ('member', 'viewer')
"""
# The normalization set is intentionally limited to legacy member/viewer
# accounts; NULL and unknown roles are handled by the explicit rejection below.
_BUMP_SESSION_VERSION_SQL = """
UPDATE users
SET session_version = COALESCE(session_version, 1) + 1
WHERE role IN ('member', 'viewer')
"""

# ``NULL`` and unknown account roles are deliberately *not* guessed here.  A
# deployment containing either value must stop and be repaired explicitly by
# an operator; silently changing it would both hide data corruption and make
# the session invalidation target impossible to audit.  Keep the role predicate
# in this SQL (rather than implementing it in Python) so online and offline
# migration plans describe exactly the same contract.
_REJECT_INVALID_GLOBAL_ROLE_SQL = """
DO $$
DECLARE
    invalid_roles text;
BEGIN
    SELECT string_agg(
        format('%s=%s', id::text, coalesce(role, '<NULL>')),
        ', ' ORDER BY id
    )
    INTO invalid_roles
    FROM users
    WHERE role IS NULL OR role NOT IN ('admin', 'user');

    IF invalid_roles IS NOT NULL THEN
        RAISE EXCEPTION
            'users.role contains unsupported values; repair explicitly before 20260808_0013: %',
            invalid_roles;
    END IF;
END
$$;
"""

def _is_offline() -> bool:
    try:
        sa.inspect(op.get_bind())
    except sa.exc.NoInspectionAvailable:
        return True
    return False


def _has_constraint(table_name: str, constraint_name: str) -> bool:
    try:
        inspector = sa.inspect(op.get_bind())
    except sa.exc.NoInspectionAvailable:
        # Alembic's ``--sql`` mode exposes a mock connection.  The migration
        # is linear and starts from the 0012 schema in that mode, so emitting
        # the create operation is the correct conservative answer.
        return False
    if table_name not in inspector.get_table_names():
        return False
    objects = [
        *inspector.get_unique_constraints(table_name),
        *inspector.get_check_constraints(table_name),
        *inspector.get_foreign_keys(table_name),
    ]
    return any(item.get("name") == constraint_name for item in objects)


def _has_index(table_name: str, index_name: str) -> bool:
    try:
        inspector = sa.inspect(op.get_bind())
    except sa.exc.NoInspectionAvailable:
        return False
    return any(
        item.get("name") == index_name
        for item in inspector.get_indexes(table_name)
    )


def _has_column(table_name: str, column_name: str) -> bool:
    try:
        inspector = sa.inspect(op.get_bind())
    except sa.exc.NoInspectionAvailable:
        return False
    return any(
        item.get("name") == column_name
        for item in inspector.get_columns(table_name)
    )


def upgrade() -> None:
    # Global users.role is intentionally distinct from project_members.role.
    # Project membership keeps owner/admin/member/viewer; the global account
    # role has only admin/user and therefore cannot accidentally grant project
    # privileges.
    # Role normalization is an authorization change: invalidate sessions only
    # for rows that are actually normalized.  The bump must run before the
    # role update, otherwise the target predicate would no longer match.
    op.execute(sa.text(_BUMP_SESSION_VERSION_SQL))
    op.execute(sa.text(_NORMALIZE_GLOBAL_ROLE_SQL))
    # Do not guess how NULL/unknown roles should map.  Abort with the complete
    # set of offending IDs; the transaction rolls back the two statements above
    # and leaves all account data unchanged for explicit operator repair.
    op.execute(sa.text(_REJECT_INVALID_GLOBAL_ROLE_SQL))
    if not _has_constraint("users", "ck_users_role_admin_user"):
        op.create_check_constraint(
            "ck_users_role_admin_user",
            "users",
            "role IN ('admin', 'user')",
        )
    op.alter_column(
        "users",
        "role",
        existing_type=sa.String(length=20),
        nullable=False,
        server_default=sa.text("'user'"),
    )

    # Add the canonical workspace discriminator and project ownership.  The
    # server default makes this safe for existing personal workspaces.
    if not _has_column("knowledge_workspaces", "workspace_type"):
        op.add_column(
            "knowledge_workspaces",
            sa.Column(
                "workspace_type",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'personal'"),
            ),
        )
    if not _has_column("knowledge_workspaces", "project_id"):
        op.add_column(
            "knowledge_workspaces",
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
    if not _has_constraint(
        "knowledge_workspaces", "fk_knowledge_workspaces_project_id"
    ):
        op.create_foreign_key(
            "fk_knowledge_workspaces_project_id",
            "knowledge_workspaces",
            "projects",
            ["project_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.execute(
        sa.text(
            """
            UPDATE knowledge_workspaces
            SET workspace_type = 'personal'
            WHERE workspace_type IS NULL OR workspace_type = ''
            """
        )
    )
    if not _has_constraint(
        "knowledge_workspaces", "ck_knowledge_workspaces_workspace_type"
    ):
        op.create_check_constraint(
            "ck_knowledge_workspaces_workspace_type",
            "knowledge_workspaces",
            "workspace_type IN ('personal', 'project')",
        )
    if not _has_constraint(
        "knowledge_workspaces", "ck_knowledge_workspaces_project_scope"
    ):
        op.create_check_constraint(
            "ck_knowledge_workspaces_project_scope",
            "knowledge_workspaces",
            "(workspace_type = 'personal' AND project_id IS NULL) "
            "OR (workspace_type = 'project' AND project_id IS NOT NULL)",
        )

    # A personal Docs workspace is unique per user; a project Docs workspace
    # is unique per project and is not owned by an individual account.
    if _has_constraint("knowledge_workspaces", "uq_knowledge_workspaces_owner_user") or _is_offline():
        op.drop_constraint(
            "uq_knowledge_workspaces_owner_user",
            "knowledge_workspaces",
            type_="unique",
        )
    if not _has_index("knowledge_workspaces", "uq_knowledge_workspaces_personal_owner"):
        op.create_index(
            "uq_knowledge_workspaces_personal_owner",
            "knowledge_workspaces",
            ["owner_user_id"],
            unique=True,
            postgresql_where=sa.text(
                "workspace_type = 'personal' AND owner_user_id IS NOT NULL"
            ),
        )
    if not _has_index("knowledge_workspaces", "uq_knowledge_workspaces_project"):
        op.create_index(
            "uq_knowledge_workspaces_project",
            "knowledge_workspaces",
            ["project_id"],
            unique=True,
            postgresql_where=sa.text(
                "workspace_type = 'project' AND project_id IS NOT NULL"
            ),
        )
    if not _has_index("knowledge_workspaces", "ix_knowledge_workspaces_project"):
        op.create_index(
            "ix_knowledge_workspaces_project",
            "knowledge_workspaces",
            ["project_id"],
        )

    if not _has_column("tasks", "auto_close_on_due"):
        op.add_column(
            "tasks",
            sa.Column(
                "auto_close_on_due",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    if not _has_index("tasks", "ix_tasks_auto_close_on_due"):
        op.create_index(
            "ix_tasks_auto_close_on_due", "tasks", ["auto_close_on_due"]
        )

    op.create_table(
        "knowledge_node_shares",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "permission",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'read'"),
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["knowledge_nodes.id"],
            name="fk_knowledge_node_shares_node",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_knowledge_node_shares_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_knowledge_node_shares_created_by",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "node_id",
            "user_id",
            name="uq_knowledge_node_shares_node_user",
        ),
        sa.CheckConstraint(
            "permission IN ('read', 'write')",
            name="ck_knowledge_node_shares_permission",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_node_shares_node", "knowledge_node_shares", ["node_id"]
    )
    op.create_index(
        "ix_knowledge_node_shares_user", "knowledge_node_shares", ["user_id"]
    )

    def _credential_columns() -> tuple[sa.Column, ...]:
        return (
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            # This is intentionally Text rather than JSON.  ``field_crypto``
            # produces an ``enc:v1:...`` ciphertext string and plaintext JSON
            # must never be persisted in this column.
            sa.Column("encrypted_payload", sa.Text(), nullable=True),
            sa.Column(
                "settings_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'::json"),
            ),
            sa.Column(
                "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    for table_name, user_index, user_constraint in (
        (
            "user_hf_credentials",
            "ix_user_hf_credentials_user",
            "uq_user_hf_credentials_user",
        ),
        (
            "user_hydrus_credentials",
            "ix_user_hydrus_credentials_user",
            "uq_user_hydrus_credentials_user",
        ),
    ):
        op.create_table(
            table_name,
            *_credential_columns(),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["users.id"],
                name=f"fk_{table_name}_user",
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint("user_id", name=user_constraint),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(user_index, table_name, ["user_id"])


def downgrade() -> None:
    for table_name, user_index in (
        ("user_hydrus_credentials", "ix_user_hydrus_credentials_user"),
        ("user_hf_credentials", "ix_user_hf_credentials_user"),
    ):
        op.drop_index(user_index, table_name=table_name)
        op.drop_table(table_name)

    op.drop_index("ix_knowledge_node_shares_user", table_name="knowledge_node_shares")
    op.drop_index("ix_knowledge_node_shares_node", table_name="knowledge_node_shares")
    op.drop_table("knowledge_node_shares")

    op.drop_index("ix_tasks_auto_close_on_due", table_name="tasks")
    op.drop_column("tasks", "auto_close_on_due")

    for index_name in (
        "ix_knowledge_workspaces_project",
        "uq_knowledge_workspaces_project",
        "uq_knowledge_workspaces_personal_owner",
    ):
        if _has_index("knowledge_workspaces", index_name):
            op.drop_index(index_name, table_name="knowledge_workspaces")
    for constraint_name in (
        "ck_knowledge_workspaces_project_scope",
        "ck_knowledge_workspaces_workspace_type",
    ):
        if _has_constraint("knowledge_workspaces", constraint_name):
            op.drop_constraint(constraint_name, "knowledge_workspaces", type_="check")
    if _has_constraint("knowledge_workspaces", "fk_knowledge_workspaces_project_id"):
        op.drop_constraint(
            "fk_knowledge_workspaces_project_id",
            "knowledge_workspaces",
            type_="foreignkey",
        )
    op.drop_column("knowledge_workspaces", "project_id")
    op.drop_column("knowledge_workspaces", "workspace_type")
    if not _has_constraint("knowledge_workspaces", "uq_knowledge_workspaces_owner_user"):
        op.create_unique_constraint(
            "uq_knowledge_workspaces_owner_user",
            "knowledge_workspaces",
            ["owner_user_id"],
        )

    op.alter_column(
        "users",
        "role",
        existing_type=sa.String(length=20),
        nullable=True,
        server_default=None,
    )
    if _has_constraint("users", "ck_users_role_admin_user"):
        op.drop_constraint("ck_users_role_admin_user", "users", type_="check")
