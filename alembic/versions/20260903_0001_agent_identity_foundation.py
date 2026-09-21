"""Add the durable Agent identity and authority foundation.

The identity graph is deliberately additive: existing Users, AgentRuns,
ExternalActions, Spaces, Projects, Tasks, and Media Personas retain their
current semantics while the new Agent relations provide explicit, bounded
links for later autonomous execution.  All AgentRun/ExternalAction links are
nullable so historical human rows remain valid.

The migration is written with the repository's PostgreSQL/SQLite rules in
mind.  SQLite uses Alembic batch recreation when a foreign key or check
constraint must be added to an existing table; PostgreSQL uses native ALTER
operations.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "20260903_0001"
down_revision = "20260902_0027"
branch_labels = None
depends_on = None


_IDENTITY_TABLES = (
    "persona_operator_assignments",
    "agent_task_assignments",
    "agent_project_grants",
    "agent_space_assignments",
    "agent_organization_profiles",
    "agent_revisions",
    "agents",
    "organizations",
)


def _dialect_name() -> str:
    # A few column templates are constructed at module import time.  Alembic
    # has not installed its Operations proxy yet in that phase, so defer to
    # the production (PostgreSQL UUID) type until upgrade() is bound.
    try:
        bind = op.get_bind()
    except (AttributeError, NameError, RuntimeError):
        return ""
    return str(getattr(getattr(bind, "dialect", None), "name", "")).lower()


def _is_sqlite() -> bool:
    return _dialect_name() == "sqlite"


def _uuid_type() -> sa.types.TypeEngine:
    """Use bounded UUID text for SQLite fixtures and native UUID on Postgres."""

    # PostgreSQL is the production dialect.  SQLite does not have a native
    # UUID type, and the repository's SQLite migration tests use canonical
    # 36-character UUID strings.
    return sa.String(length=36) if _is_sqlite() else postgresql.UUID(as_uuid=True)


def _column_copy(column: sa.Column) -> sa.Column:
    """Return a detached Column for use by a second Alembic operation."""

    return column.copy()


def _table_has_columns(table_name: str, columns: tuple[str, ...]) -> bool:
    """Return whether a legacy table can accept a requested index."""

    try:
        available = {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}
    except Exception:
        return False
    return set(columns) <= available


def _drop_index_if_present(name: str, table_name: str) -> None:
    try:
        existing = {item["name"] for item in inspect(op.get_bind()).get_indexes(table_name)}
    except Exception:
        existing = {name}
    if name in existing:
        op.drop_index(name, table_name=table_name)


@contextmanager
def _sqlite_batch_foreign_keys() -> Iterator[None]:
    """Allow SQLite batch recreation of tables that already have dependants.

    SQLite enforces a self/child foreign key while Alembic's batch operation
    temporarily drops the old table.  Historical ``agent_runs`` rows almost
    always have parent/child links, so an enabled ``PRAGMA foreign_keys``
    would otherwise make this additive migration fail at ``DROP TABLE``.
    Toggle the pragma through the DB-API connection (SQLite only permits the
    change outside a transaction), committing the small DDL unit before and
    after the rebuild and restoring the caller's original setting.
    """

    if not _is_sqlite():
        yield
        return

    bind = op.get_bind()
    try:
        enabled = bool(bind.exec_driver_sql("PRAGMA foreign_keys").scalar())
    except Exception:
        enabled = False
    if not enabled:
        yield
        return

    raw_connection = getattr(bind, "connection", None)
    if raw_connection is None:
        # This is only expected for a synthetic Operations recorder.  Let the
        # normal batch operation proceed rather than making static tests
        # depend on DB-API details.
        yield
        return

    raw_connection.commit()
    raw_connection.execute("PRAGMA foreign_keys=OFF")
    try:
        yield
        raw_connection.commit()
    except Exception:
        raw_connection.rollback()
        raise
    finally:
        raw_connection.execute("PRAGMA foreign_keys=ON")


def _create_organizations() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column(
            "singleton_key",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'installation'"),
        ),
        sa.Column(
            "display_name",
            sa.String(length=200),
            nullable=False,
            server_default=sa.text("'AoiTalk'"),
        ),
        sa.Column("legal_name", sa.String(length=200), nullable=True),
        sa.Column(
            "locale",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'ja-JP'"),
        ),
        sa.Column(
            "timezone",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'Asia/Tokyo'"),
        ),
        sa.Column(
            "policy_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("'1'"),
        ),
        sa.Column(
            "autonomy_level",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'disabled'"),
        ),
        sa.Column(
            "policy",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "budget_policy",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "singleton_key",
            name="uq_organizations_singleton_key",
        ),
        sa.CheckConstraint(
            "singleton_key = 'installation'",
            name="ck_organizations_singleton_key",
        ),
        sa.CheckConstraint(
            "policy_version > 0",
            name="ck_organizations_policy_version_positive",
        ),
        sa.CheckConstraint(
            "autonomy_level IN ('disabled','supervised','bounded','autonomous')",
            name="ck_organizations_autonomy_level",
        ),
        sa.CheckConstraint(
            "length(display_name) BETWEEN 1 AND 200",
            name="ck_organizations_display_name",
        ),
    )
    op.create_index(
        "ix_organizations_autonomy_level",
        "organizations",
        ["autonomy_level"],
    )


def _create_agents() -> None:
    op.create_table(
        "agents",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=True),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column("character_id", _uuid_type(), nullable=True),
        sa.Column("create_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_by", _uuid_type(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_agents_idempotency_key",
        ),
        sa.ForeignKeyConstraint(
            ["character_id"],
            ["characters.id"],
            name="fk_agents_character_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_agents_created_by",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "state IN ('draft','active','paused','retired')",
            name="ck_agents_state",
        ),
        sa.CheckConstraint(
            "length(create_hash) = 64",
            name="ck_agents_create_hash_length",
        ),
        sa.CheckConstraint(
            "length(display_name) BETWEEN 1 AND 160",
            name="ck_agents_display_name",
        ),
    )
    # ``slug`` is unique and indexed in the ORM model.  A unique index (rather
    # than a second unique constraint) preserves that exact shape.
    op.create_index("ix_agents_slug", "agents", ["slug"], unique=True)
    for name, columns in (
        ("ix_agents_state", ["state"]),
        ("ix_agents_character_id", ["character_id"]),
        ("ix_agents_create_hash", ["create_hash"]),
        ("ix_agents_created_at", ["created_at"]),
    ):
        op.create_index(name, "agents", columns)


def _create_agent_revisions() -> None:
    op.create_table(
        "agent_revisions",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("agent_id", _uuid_type(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("mission", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("responsibility_summary", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("operational_instructions", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("agent_team_id", sa.String(length=100), nullable=False),
        sa.Column("execution_profile_id", sa.String(length=100), nullable=False),
        sa.Column(
            "allowed_subagent_ids",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "capability_ceiling",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "wake_policy",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "budget_policy",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "concurrency_policy",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_by", _uuid_type(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name="fk_agent_revisions_agent_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_agent_revisions_created_by",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "agent_id",
            "version",
            name="uq_agent_revisions_agent_version",
        ),
        sa.UniqueConstraint(
            "agent_id",
            "idempotency_key",
            name="uq_agent_revisions_agent_idempotency",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_agent_revisions_version_positive",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name="ck_agent_revisions_content_hash_length",
        ),
        sa.CheckConstraint(
            "length(display_name) BETWEEN 1 AND 160",
            name="ck_agent_revisions_display_name",
        ),
    )
    for name, columns in (
        ("ix_agent_revisions_agent_id", ["agent_id"]),
        ("ix_agent_revisions_content_hash", ["content_hash"]),
        ("ix_agent_revisions_created_at", ["created_at"]),
    ):
        op.create_index(name, "agent_revisions", columns)


def _create_agent_organization_profiles() -> None:
    op.create_table(
        "agent_organization_profiles",
        sa.Column("agent_id", _uuid_type(), nullable=False),
        sa.Column(
            "job_title",
            sa.String(length=160),
            nullable=False,
        ),
        sa.Column(
            "responsibility_summary",
            sa.Text(),
            nullable=False,
        ),
        sa.Column("primary_space_id", _uuid_type(), nullable=True),
        sa.Column("manager_user_id", _uuid_type(), nullable=True),
        sa.Column("manager_agent_id", _uuid_type(), nullable=True),
        sa.Column(
            "autonomy_level",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'supervised'"),
        ),
        sa.Column(
            "company_permission_ceiling",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "employment_state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("agent_id"),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name="fk_agent_org_profiles_agent_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["primary_space_id"],
            ["spaces.id"],
            name="fk_agent_org_profiles_primary_space_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["manager_user_id"],
            ["users.id"],
            name="fk_agent_org_profiles_manager_user_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["manager_agent_id"],
            ["agents.id"],
            name="fk_agent_org_profiles_manager_agent_id",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "NOT (manager_user_id IS NOT NULL AND manager_agent_id IS NOT NULL)",
            name="ck_agent_org_profiles_manager_xor",
        ),
        sa.CheckConstraint(
            "autonomy_level IN ('disabled','supervised','bounded','autonomous')",
            name="ck_agent_org_profiles_autonomy_level",
        ),
        sa.CheckConstraint(
            "employment_state IN ('active','on_leave','suspended','terminated','contractor')",
            name="ck_agent_org_profiles_employment_state",
        ),
    )
    for name, columns in (
        (
            "ix_agent_organization_profiles_primary_space_id",
            ["primary_space_id"],
        ),
        (
            "ix_agent_organization_profiles_manager_user_id",
            ["manager_user_id"],
        ),
        (
            "ix_agent_organization_profiles_manager_agent_id",
            ["manager_agent_id"],
        ),
        (
            "ix_agent_organization_profiles_employment_state",
            ["employment_state"],
        ),
    ):
        op.create_index(name, "agent_organization_profiles", columns)


def _create_agent_space_assignments() -> None:
    op.create_table(
        "agent_space_assignments",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("agent_id", _uuid_type(), nullable=False),
        sa.Column("space_id", _uuid_type(), nullable=False),
        sa.Column(
            "assignment_kind",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'supporting'"),
        ),
        sa.Column("role_label", sa.String(length=120), nullable=True),
        sa.Column(
            "policy_ceiling",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("active_from", sa.DateTime(), nullable=True),
        sa.Column("active_until", sa.DateTime(), nullable=True),
        sa.Column("assigned_by", _uuid_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name="fk_agent_space_assignments_agent_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["spaces.id"],
            name="fk_agent_space_assignments_space_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by"],
            ["users.id"],
            name="fk_agent_space_assignments_assigned_by",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "assignment_kind IN ('primary','secondary','supporting')",
            name="ck_agent_space_assignments_kind",
        ),
        sa.CheckConstraint(
            "state IN ('active','revoked','expired')",
            name="ck_agent_space_assignments_state",
        ),
        sa.CheckConstraint(
            "active_until IS NULL OR active_from IS NULL OR active_until >= active_from",
            name="ck_agent_space_assignments_dates",
        ),
    )
    for name, columns in (
        ("ix_agent_space_assignments_agent_id", ["agent_id"]),
        ("ix_agent_space_assignments_space_id", ["space_id"]),
        ("ix_agent_space_assignments_state", ["state"]),
        (
            "ix_agent_space_assignments_space_state",
            ["space_id", "state"],
        ),
    ):
        op.create_index(name, "agent_space_assignments", columns)
    op.create_index(
        "uq_agent_space_assignments_active_equivalent",
        "agent_space_assignments",
        ["agent_id", "space_id", "assignment_kind"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
        sqlite_where=sa.text("state = 'active'"),
    )


def _create_agent_project_grants() -> None:
    op.create_table(
        "agent_project_grants",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("agent_id", _uuid_type(), nullable=False),
        sa.Column("project_id", _uuid_type(), nullable=False),
        sa.Column(
            "role",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'viewer'"),
        ),
        sa.Column(
            "permissions",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("active_from", sa.DateTime(), nullable=True),
        sa.Column("active_until", sa.DateTime(), nullable=True),
        sa.Column("granted_by", _uuid_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name="fk_agent_project_grants_agent_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_agent_project_grants_project_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by"],
            ["users.id"],
            name="fk_agent_project_grants_granted_by",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "role IN ('owner','admin','member','viewer')",
            name="ck_agent_project_grants_role",
        ),
        sa.CheckConstraint(
            "state IN ('active','revoked','expired')",
            name="ck_agent_project_grants_state",
        ),
        sa.CheckConstraint(
            "active_until IS NULL OR active_from IS NULL OR active_until >= active_from",
            name="ck_agent_project_grants_dates",
        ),
    )
    for name, columns in (
        ("ix_agent_project_grants_agent_id", ["agent_id"]),
        ("ix_agent_project_grants_project_id", ["project_id"]),
        ("ix_agent_project_grants_state", ["state"]),
        ("ix_agent_project_grants_project_state", ["project_id", "state"]),
    ):
        op.create_index(name, "agent_project_grants", columns)
    op.create_index(
        "uq_agent_project_grants_active_project",
        "agent_project_grants",
        ["agent_id", "project_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
        sqlite_where=sa.text("state = 'active'"),
    )


def _create_agent_task_assignments() -> None:
    op.create_table(
        "agent_task_assignments",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("agent_id", _uuid_type(), nullable=False),
        sa.Column("task_id", _uuid_type(), nullable=False),
        sa.Column(
            "assignment_role",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'executor'"),
        ),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("active_from", sa.DateTime(), nullable=True),
        sa.Column("active_until", sa.DateTime(), nullable=True),
        sa.Column("assigned_by", _uuid_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name="fk_agent_task_assignments_agent_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_agent_task_assignments_task_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by"],
            ["users.id"],
            name="fk_agent_task_assignments_assigned_by",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "assignment_role IN ('owner','executor','reviewer','observer')",
            name="ck_agent_task_assignments_role",
        ),
        sa.CheckConstraint(
            "state IN ('active','revoked','expired')",
            name="ck_agent_task_assignments_state",
        ),
        sa.CheckConstraint(
            "active_until IS NULL OR active_from IS NULL OR active_until >= active_from",
            name="ck_agent_task_assignments_dates",
        ),
    )
    for name, columns in (
        ("ix_agent_task_assignments_agent_id", ["agent_id"]),
        ("ix_agent_task_assignments_task_id", ["task_id"]),
        ("ix_agent_task_assignments_state", ["state"]),
        ("ix_agent_task_assignments_task_state", ["task_id", "state"]),
    ):
        op.create_index(name, "agent_task_assignments", columns)
    op.create_index(
        "uq_agent_task_assignments_active_equivalent",
        "agent_task_assignments",
        ["agent_id", "task_id", "assignment_role"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
        sqlite_where=sa.text("state = 'active'"),
    )


def _create_persona_operator_assignments() -> None:
    op.create_table(
        "persona_operator_assignments",
        sa.Column("id", _uuid_type(), nullable=False),
        sa.Column("agent_id", _uuid_type(), nullable=False),
        sa.Column("persona_id", _uuid_type(), nullable=False),
        sa.Column(
            "role",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'operator'"),
        ),
        sa.Column(
            "is_primary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "capability_ceiling",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("active_from", sa.DateTime(), nullable=True),
        sa.Column("active_until", sa.DateTime(), nullable=True),
        sa.Column("assigned_by", _uuid_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name="fk_persona_operator_assignments_agent_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["persona_id"],
            ["media_personas.id"],
            name="fk_persona_operator_assignments_persona_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by"],
            ["users.id"],
            name="fk_persona_operator_assignments_assigned_by",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "role IN ('operator','strategist','researcher','creator','analyst','publisher')",
            name="ck_persona_operator_assignments_role",
        ),
        sa.CheckConstraint(
            "state IN ('active','revoked','expired')",
            name="ck_persona_operator_assignments_state",
        ),
        sa.CheckConstraint(
            "active_until IS NULL OR active_from IS NULL OR active_until >= active_from",
            name="ck_persona_operator_assignments_dates",
        ),
    )
    for name, columns in (
        ("ix_persona_operator_assignments_agent_id", ["agent_id"]),
        ("ix_persona_operator_assignments_persona_id", ["persona_id"]),
        ("ix_persona_operator_assignments_state", ["state"]),
        (
            "ix_persona_operator_assignments_persona_state",
            ["persona_id", "state"],
        ),
    ):
        op.create_index(name, "persona_operator_assignments", columns)
    op.create_index(
        "uq_persona_operator_assignments_active_role",
        "persona_operator_assignments",
        ["agent_id", "persona_id", "role"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
        sqlite_where=sa.text("state = 'active'"),
    )


def _agent_run_columns() -> tuple[sa.Column, ...]:
    """Construct altered columns after Alembic has a bound dialect."""

    return (
        sa.Column("agent_id", _uuid_type(), nullable=True),
        sa.Column("agent_revision_id", _uuid_type(), nullable=True),
        sa.Column("task_id", _uuid_type(), nullable=True),
        sa.Column("acting_subagent_id", sa.String(length=100), nullable=True),
        sa.Column("previous_attempt_run_id", _uuid_type(), nullable=True),
        sa.Column("resolved_execution_manifest", sa.JSON(), nullable=True),
        sa.Column("execution_manifest_hash", sa.String(length=64), nullable=True),
    )


def _add_agent_run_columns() -> None:
    checks = (
        (
            "ck_agent_runs_execution_manifest_hash",
            "execution_manifest_hash IS NULL OR length(execution_manifest_hash) = 64",
        ),
        (
            "ck_agent_runs_revision_requires_agent",
            "agent_revision_id IS NULL OR agent_id IS NOT NULL",
        ),
        (
            "ck_agent_runs_manifest_requires_hash",
            "resolved_execution_manifest IS NULL OR execution_manifest_hash IS NOT NULL",
        ),
    )
    if _is_sqlite():
        with _sqlite_batch_foreign_keys():
            with op.batch_alter_table("agent_runs", recreate="always") as batch:
                for column in _agent_run_columns():
                    batch.add_column(_column_copy(column))
                batch.create_foreign_key(
                    "fk_agent_runs_agent_id",
                    "agents",
                    ["agent_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
                batch.create_foreign_key(
                    "fk_agent_runs_agent_revision_id",
                    "agent_revisions",
                    ["agent_revision_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
                batch.create_foreign_key(
                    "fk_agent_runs_task_id",
                    "tasks",
                    ["task_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
                batch.create_foreign_key(
                    "fk_agent_runs_previous_attempt_run_id",
                    "agent_runs",
                    ["previous_attempt_run_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
                for name, expression in checks:
                    batch.create_check_constraint(name, expression)
    else:
        for column in _agent_run_columns():
            op.add_column("agent_runs", _column_copy(column))
        op.create_foreign_key(
            "fk_agent_runs_agent_id",
            "agent_runs",
            "agents",
            ["agent_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            "fk_agent_runs_agent_revision_id",
            "agent_runs",
            "agent_revisions",
            ["agent_revision_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            "fk_agent_runs_task_id",
            "agent_runs",
            "tasks",
            ["task_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            "fk_agent_runs_previous_attempt_run_id",
            "agent_runs",
            "agent_runs",
            ["previous_attempt_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
        for name, expression in checks:
            op.create_check_constraint(name, "agent_runs", expression)

    for name, columns in (
        ("ix_agent_runs_agent_id", ["agent_id"]),
        ("ix_agent_runs_agent_revision_id", ["agent_revision_id"]),
        ("ix_agent_runs_task_id", ["task_id"]),
        ("ix_agent_runs_acting_subagent_id", ["acting_subagent_id"]),
        ("ix_agent_runs_previous_attempt_run_id", ["previous_attempt_run_id"]),
        ("ix_agent_runs_execution_manifest_hash", ["execution_manifest_hash"]),
        (
            "ix_agent_runs_agent_revision_created",
            ["agent_id", "agent_revision_id", "created_at"],
        ),
    ):
        if _table_has_columns("agent_runs", tuple(columns)):
            op.create_index(name, "agent_runs", columns)


def _external_action_columns() -> tuple[sa.Column, ...]:
    """Construct altered columns after Alembic has a bound dialect."""

    return (
        sa.Column("origin_agent_id", _uuid_type(), nullable=True),
        sa.Column("origin_agent_run_id", _uuid_type(), nullable=True),
    )


def _add_external_action_columns() -> None:
    if _is_sqlite():
        with _sqlite_batch_foreign_keys():
            with op.batch_alter_table("external_actions", recreate="always") as batch:
                for column in _external_action_columns():
                    batch.add_column(_column_copy(column))
                batch.create_foreign_key(
                    "fk_external_actions_origin_agent_id",
                    "agents",
                    ["origin_agent_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
                batch.create_foreign_key(
                    "fk_external_actions_origin_agent_run_id",
                    "agent_runs",
                    ["origin_agent_run_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
    else:
        for column in _external_action_columns():
            op.add_column("external_actions", _column_copy(column))
        op.create_foreign_key(
            "fk_external_actions_origin_agent_id",
            "external_actions",
            "agents",
            ["origin_agent_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            "fk_external_actions_origin_agent_run_id",
            "external_actions",
            "agent_runs",
            ["origin_agent_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if _table_has_columns("external_actions", ("origin_agent_id",)):
        op.create_index(
            "ix_external_actions_origin_agent_id",
            "external_actions",
            ["origin_agent_id"],
        )
    if _table_has_columns("external_actions", ("origin_agent_run_id",)):
        op.create_index(
            "ix_external_actions_origin_agent_run_id",
            "external_actions",
            ["origin_agent_run_id"],
        )


def _install_revision_immutability_guard() -> None:
    """Prevent direct UPDATE/DELETE of AgentRevision rows."""

    table = "agent_revisions"
    if _dialect_name() == "postgresql":
        op.execute(
            sa.text(
                """
                CREATE OR REPLACE FUNCTION agent_revisions_immutable()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                  RAISE EXCEPTION 'agent_revisions rows are immutable';
                END;
                $$;
                """
            )
        )
        op.execute(
            sa.text(
                """
                CREATE TRIGGER agent_revisions_no_update
                BEFORE UPDATE OR DELETE ON agent_revisions
                FOR EACH ROW EXECUTE FUNCTION agent_revisions_immutable();
                """
            )
        )
        op.execute(
            sa.text(
                """
                CREATE TRIGGER agent_revisions_no_truncate
                BEFORE TRUNCATE ON agent_revisions
                FOR EACH STATEMENT EXECUTE FUNCTION agent_revisions_immutable();
                """
            )
        )
    elif _dialect_name() == "sqlite":
        for operation, trigger in (("UPDATE", "agent_revisions_no_update"), ("DELETE", "agent_revisions_no_delete")):
            op.execute(
                sa.text(
                    f"""
                    CREATE TRIGGER {trigger}
                    BEFORE {operation} ON {table}
                    BEGIN
                      SELECT RAISE(ABORT, 'agent_revisions rows are immutable');
                    END;
                    """
                )
            )


def _remove_revision_immutability_guard() -> None:
    if _dialect_name() == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS agent_revisions_no_update ON agent_revisions")
        op.execute("DROP TRIGGER IF EXISTS agent_revisions_no_truncate ON agent_revisions")
        op.execute("DROP FUNCTION IF EXISTS agent_revisions_immutable()")
    elif _dialect_name() == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS agent_revisions_no_update")
        op.execute("DROP TRIGGER IF EXISTS agent_revisions_no_delete")


def upgrade() -> None:
    _create_organizations()
    _create_agents()
    _create_agent_revisions()
    _install_revision_immutability_guard()
    _create_agent_organization_profiles()
    _create_agent_space_assignments()
    _create_agent_project_grants()
    _create_agent_task_assignments()
    _create_persona_operator_assignments()
    _add_agent_run_columns()
    _add_external_action_columns()


def _drop_external_action_columns() -> None:
    for name in (
        "ix_external_actions_origin_agent_run_id",
        "ix_external_actions_origin_agent_id",
    ):
        _drop_index_if_present(name, "external_actions")
    if _is_sqlite():
        with _sqlite_batch_foreign_keys():
            with op.batch_alter_table("external_actions", recreate="always") as batch:
                for name in (
                    "fk_external_actions_origin_agent_run_id",
                    "fk_external_actions_origin_agent_id",
                ):
                    try:
                        batch.drop_constraint(name, type_="foreignkey")
                    except Exception:
                        # Older SQLite versions can report inline FKs without
                        # names; the batch reflection still drops the columns.
                        pass
                for column in reversed(_external_action_columns()):
                    batch.drop_column(column.name)
    else:
        for name in (
            "fk_external_actions_origin_agent_run_id",
            "fk_external_actions_origin_agent_id",
        ):
            op.drop_constraint(name, "external_actions", type_="foreignkey")
        for column in reversed(_external_action_columns()):
            op.drop_column("external_actions", column.name)


def _drop_agent_run_columns() -> None:
    for name in (
        "ix_agent_runs_agent_revision_created",
        "ix_agent_runs_execution_manifest_hash",
        "ix_agent_runs_previous_attempt_run_id",
        "ix_agent_runs_acting_subagent_id",
        "ix_agent_runs_task_id",
        "ix_agent_runs_agent_revision_id",
        "ix_agent_runs_agent_id",
    ):
        _drop_index_if_present(name, "agent_runs")
    checks = (
        "ck_agent_runs_execution_manifest_hash",
        "ck_agent_runs_revision_requires_agent",
        "ck_agent_runs_manifest_requires_hash",
    )
    fks = (
        "fk_agent_runs_previous_attempt_run_id",
        "fk_agent_runs_task_id",
        "fk_agent_runs_agent_revision_id",
        "fk_agent_runs_agent_id",
    )
    if _is_sqlite():
        with _sqlite_batch_foreign_keys():
            with op.batch_alter_table("agent_runs", recreate="always") as batch:
                for name in checks:
                    try:
                        batch.drop_constraint(name, type_="check")
                    except Exception:
                        pass
                for name in fks:
                    try:
                        batch.drop_constraint(name, type_="foreignkey")
                    except Exception:
                        pass
                for column in reversed(_agent_run_columns()):
                    batch.drop_column(column.name)
    else:
        for name in checks:
            op.drop_constraint(name, "agent_runs", type_="check")
        for name in fks:
            op.drop_constraint(name, "agent_runs", type_="foreignkey")
        for column in reversed(_agent_run_columns()):
            op.drop_column("agent_runs", column.name)


def _drop_identity_tables() -> None:
    # Partial/ordinary indexes are explicit migration objects; dropping a
    # table in PostgreSQL or SQLite does not reliably remove those indexes
    # when downgrade is run through a direct Operations context.
    index_map = {
        "persona_operator_assignments": (
            "uq_persona_operator_assignments_active_role",
            "ix_persona_operator_assignments_persona_state",
            "ix_persona_operator_assignments_state",
            "ix_persona_operator_assignments_persona_id",
            "ix_persona_operator_assignments_agent_id",
        ),
        "agent_task_assignments": (
            "uq_agent_task_assignments_active_equivalent",
            "ix_agent_task_assignments_task_state",
            "ix_agent_task_assignments_state",
            "ix_agent_task_assignments_task_id",
            "ix_agent_task_assignments_agent_id",
        ),
        "agent_project_grants": (
            "uq_agent_project_grants_active_project",
            "ix_agent_project_grants_project_state",
            "ix_agent_project_grants_state",
            "ix_agent_project_grants_project_id",
            "ix_agent_project_grants_agent_id",
        ),
        "agent_space_assignments": (
            "uq_agent_space_assignments_active_equivalent",
            "ix_agent_space_assignments_space_state",
            "ix_agent_space_assignments_state",
            "ix_agent_space_assignments_space_id",
            "ix_agent_space_assignments_agent_id",
        ),
        "agent_organization_profiles": (
            "ix_agent_organization_profiles_employment_state",
            "ix_agent_organization_profiles_manager_agent_id",
            "ix_agent_organization_profiles_manager_user_id",
            "ix_agent_organization_profiles_primary_space_id",
        ),
        "agent_revisions": (
            "ix_agent_revisions_created_at",
            "ix_agent_revisions_content_hash",
            "ix_agent_revisions_agent_id",
        ),
        "agents": (
            "ix_agents_created_at",
            "ix_agents_create_hash",
            "ix_agents_character_id",
            "ix_agents_state",
            "ix_agents_slug",
        ),
        "organizations": ("ix_organizations_autonomy_level",),
    }
    for table in _IDENTITY_TABLES:
        for name in index_map[table]:
            op.drop_index(name, table_name=table)
        op.drop_table(table)


def downgrade() -> None:
    # Remove links before their Agent targets, then unwind the graph from
    # leaves to the deployment singleton.
    _drop_external_action_columns()
    _drop_agent_run_columns()
    _remove_revision_immutability_guard()
    _drop_identity_tables()
