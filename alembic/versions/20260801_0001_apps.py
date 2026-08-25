"""永続 Apps 基盤を追加し、Chat/Agent Run/Docs に App scope を追加する。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260801_0001"
down_revision = "20260731_0001"
branch_labels = None
depends_on = None


def _uuid(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        nullable=nullable,
        primary_key=name == "id",
    )


def upgrade() -> None:
    op.create_table(
        "apps",
        _uuid("id"),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("origin_project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("lifecycle_status", sa.String(length=32), nullable=False, server_default="draft"),
        sa.Column("visibility", sa.String(length=32), nullable=False, server_default="private"),
        sa.Column("default_target_key", sa.String(length=80), nullable=True),
        sa.Column("readme_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["origin_project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["readme_node_id"], ["knowledge_nodes.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("slug", name="uq_apps_slug"),
        sa.CheckConstraint(
            "lifecycle_status IN ('draft','developing','active','maintenance','archived')",
            name="ck_apps_lifecycle_status",
        ),
        sa.CheckConstraint(
            "visibility IN ('private','shared','public')",
            name="ck_apps_visibility",
        ),
    )
    op.create_index("ix_apps_owner_user_id", "apps", ["owner_user_id"])
    op.create_index("ix_apps_origin_project_id", "apps", ["origin_project_id"])
    op.create_index("ix_apps_lifecycle_status", "apps", ["lifecycle_status"])
    op.create_index("ix_apps_visibility", "apps", ["visibility"])
    op.create_index("ix_apps_updated_at", "apps", ["updated_at"])
    op.create_index("ix_apps_archived_at", "apps", ["archived_at"])
    op.create_index("ix_apps_owner_lifecycle", "apps", ["owner_user_id", "lifecycle_status"])

    op.create_table(
        "app_targets",
        _uuid("id"),
        sa.Column("app_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_key", sa.String(length=80), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("surface", sa.String(length=32), nullable=False),
        sa.Column("runtime", sa.String(length=32), nullable=False),
        sa.Column("execution_host", sa.String(length=32), nullable=False),
        sa.Column("entrypoint", sa.Text(), nullable=False),
        sa.Column("manifest_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("app_id", "target_key", name="uq_app_targets_app_target_key"),
        sa.CheckConstraint(
            "surface IN ('embedded_web','standalone_web','desktop_gui','headless','office')",
            name="ck_app_targets_surface",
        ),
        sa.CheckConstraint(
            "runtime IN ('static_web','node','python','powershell','batch','vba','executable')",
            name="ck_app_targets_runtime",
        ),
        sa.CheckConstraint(
            "execution_host IN ('aoitalk','server','client','browser','office','download_only')",
            name="ck_app_targets_execution_host",
        ),
    )
    op.create_index("ix_app_targets_app_id", "app_targets", ["app_id"])
    op.create_index("ix_app_targets_app_surface", "app_targets", ["app_id", "surface"])

    op.create_table(
        "app_releases",
        _uuid("id"),
        sa.Column("app_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.String(length=80), nullable=False),
        sa.Column("git_revision", sa.String(length=80), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("readme_hash", sa.String(length=64), nullable=False),
        sa.Column("changelog", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("app_id", "version", name="uq_app_releases_app_version"),
        sa.CheckConstraint("status IN ('draft','published','deprecated')", name="ck_app_releases_status"),
    )
    op.create_index("ix_app_releases_app_id", "app_releases", ["app_id"])
    op.create_index("ix_app_releases_status", "app_releases", ["status"])
    op.create_index("ix_app_releases_created_at", "app_releases", ["created_at"])
    op.create_index(
        "ix_app_releases_app_status_created",
        "app_releases",
        ["app_id", "status", "created_at"],
    )

    op.create_table(
        "project_apps",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("app_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("binding_mode", sa.String(length=20), nullable=False, server_default="development"),
        sa.Column("installed_release_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("display_alias", sa.String(length=255), nullable=True),
        sa.Column("config_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("capability_grants_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["installed_release_id"], ["app_releases.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("project_id", "app_id"),
        sa.CheckConstraint("binding_mode IN ('development','installed')", name="ck_project_apps_binding_mode"),
    )
    op.create_index("ix_project_apps_installed_release_id", "project_apps", ["installed_release_id"])
    op.create_index("ix_project_apps_project_enabled", "project_apps", ["project_id", "enabled"])

    op.create_table(
        "app_grants",
        _uuid("id"),
        sa.Column("app_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("permission", sa.String(length=20), nullable=False, server_default="viewer"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("(user_id IS NULL) <> (project_id IS NULL)", name="ck_app_grants_exactly_one_subject"),
        sa.CheckConstraint(
            "permission IN ('viewer','runner','developer','maintainer','admin')",
            name="ck_app_grants_permission",
        ),
        sa.UniqueConstraint("app_id", "user_id", "permission", name="uq_app_grants_user_permission"),
        sa.UniqueConstraint("app_id", "project_id", "permission", name="uq_app_grants_project_permission"),
    )
    op.create_index("ix_app_grants_app_id", "app_grants", ["app_id"])
    op.create_index("ix_app_grants_user_id", "app_grants", ["user_id"])
    op.create_index("ix_app_grants_project_id", "app_grants", ["project_id"])

    op.create_table(
        "app_artifacts",
        _uuid("id"),
        sa.Column("release_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_type", sa.String(length=32), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["release_id"], ["app_releases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_id"], ["app_targets.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "release_id", "target_id", "artifact_type", "filename",
            name="uq_app_artifacts_release_target_file",
        ),
    )
    op.create_index("ix_app_artifacts_release_id", "app_artifacts", ["release_id"])
    op.create_index("ix_app_artifacts_target_id", "app_artifacts", ["target_id"])
    op.create_index("ix_app_artifacts_release_target", "app_artifacts", ["release_id", "target_id"])

    op.create_table(
        "app_jobs",
        _uuid("id"),
        sa.Column("app_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("release_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("job_type", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("input_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("result_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("log_path", sa.Text(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("started_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_id"], ["app_targets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["release_id"], ["app_releases.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["started_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("job_type IN ('build','test','run','package')", name="ck_app_jobs_job_type"),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_app_jobs_status",
        ),
    )
    op.create_index("ix_app_jobs_app_id", "app_jobs", ["app_id"])
    op.create_index("ix_app_jobs_target_id", "app_jobs", ["target_id"])
    op.create_index("ix_app_jobs_project_id", "app_jobs", ["project_id"])
    op.create_index("ix_app_jobs_release_id", "app_jobs", ["release_id"])
    op.create_index("ix_app_jobs_agent_run_id", "app_jobs", ["agent_run_id"])
    op.create_index("ix_app_jobs_started_by", "app_jobs", ["started_by"])
    op.create_index("ix_app_jobs_status", "app_jobs", ["status"])
    op.create_index("ix_app_jobs_app_status_started", "app_jobs", ["app_id", "status", "started_at"])

    op.create_table(
        "task_app_links",
        _uuid("id"),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("app_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("relation_type", sa.String(length=20), nullable=False, server_default="related"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_id"], ["app_targets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "relation_type IN ('develops','fixes','tests','releases','uses','related')",
            name="ck_task_app_links_relation_type",
        ),
        sa.UniqueConstraint(
            "task_id", "app_id", "target_id", "relation_type",
            name="uq_task_app_links_target_relation",
        ),
    )
    op.create_index("ix_task_app_links_task_id", "task_app_links", ["task_id"])
    op.create_index("ix_task_app_links_app_id", "task_app_links", ["app_id"])
    op.create_index("ix_task_app_links_target_id", "task_app_links", ["target_id"])
    op.create_index("ix_task_app_links_task_relation", "task_app_links", ["task_id", "relation_type"])
    op.create_index("ix_task_app_links_app_relation", "task_app_links", ["app_id", "relation_type"])

    op.add_column(
        "conversation_sessions",
        sa.Column("app_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "conversation_sessions",
        sa.Column("app_target_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_conversation_sessions_app_id",
        "conversation_sessions",
        "apps",
        ["app_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_conversation_sessions_app_target_id",
        "conversation_sessions",
        "app_targets",
        ["app_target_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_conversation_sessions_app_id", "conversation_sessions", ["app_id"])
    op.create_index("ix_conversation_sessions_app_target_id", "conversation_sessions", ["app_target_id"])

    op.add_column("agent_runs", sa.Column("app_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("agent_runs", sa.Column("app_target_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("agent_runs", sa.Column("base_revision", sa.String(length=80), nullable=True))
    op.add_column("agent_runs", sa.Column("result_revision", sa.String(length=80), nullable=True))
    op.create_foreign_key("fk_agent_runs_app_id", "agent_runs", "apps", ["app_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key(
        "fk_agent_runs_app_target_id",
        "agent_runs",
        "app_targets",
        ["app_target_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_agent_runs_app_id", "agent_runs", ["app_id"])
    op.create_index("ix_agent_runs_app_target_id", "agent_runs", ["app_target_id"])
    op.create_index("ix_agent_runs_base_revision", "agent_runs", ["base_revision"])
    op.create_index("ix_agent_runs_result_revision", "agent_runs", ["result_revision"])

    op.add_column(
        "knowledge_nodes",
        sa.Column("app_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_knowledge_nodes_app_id",
        "knowledge_nodes",
        "apps",
        ["app_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_knowledge_nodes_app_id", "knowledge_nodes", ["app_id"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_nodes_app_id", table_name="knowledge_nodes")
    op.drop_constraint("fk_knowledge_nodes_app_id", "knowledge_nodes", type_="foreignkey")
    op.drop_column("knowledge_nodes", "app_id")

    for name in (
        "ix_agent_runs_result_revision",
        "ix_agent_runs_base_revision",
        "ix_agent_runs_app_target_id",
        "ix_agent_runs_app_id",
    ):
        op.drop_index(name, table_name="agent_runs")
    op.drop_constraint("fk_agent_runs_app_target_id", "agent_runs", type_="foreignkey")
    op.drop_constraint("fk_agent_runs_app_id", "agent_runs", type_="foreignkey")
    for name in ("result_revision", "base_revision", "app_target_id", "app_id"):
        op.drop_column("agent_runs", name)

    op.drop_index("ix_conversation_sessions_app_target_id", table_name="conversation_sessions")
    op.drop_index("ix_conversation_sessions_app_id", table_name="conversation_sessions")
    op.drop_constraint("fk_conversation_sessions_app_target_id", "conversation_sessions", type_="foreignkey")
    op.drop_constraint("fk_conversation_sessions_app_id", "conversation_sessions", type_="foreignkey")
    op.drop_column("conversation_sessions", "app_target_id")
    op.drop_column("conversation_sessions", "app_id")

    for name in (
        "ix_task_app_links_app_relation",
        "ix_task_app_links_task_relation",
        "ix_task_app_links_target_id",
        "ix_task_app_links_app_id",
        "ix_task_app_links_task_id",
    ):
        op.drop_index(name, table_name="task_app_links")
    op.drop_table("task_app_links")

    for name in (
        "ix_app_jobs_app_status_started",
        "ix_app_jobs_status",
        "ix_app_jobs_started_by",
        "ix_app_jobs_agent_run_id",
        "ix_app_jobs_release_id",
        "ix_app_jobs_project_id",
        "ix_app_jobs_target_id",
        "ix_app_jobs_app_id",
    ):
        op.drop_index(name, table_name="app_jobs")
    op.drop_table("app_jobs")

    for name in ("ix_app_artifacts_release_target", "ix_app_artifacts_target_id", "ix_app_artifacts_release_id"):
        op.drop_index(name, table_name="app_artifacts")
    op.drop_table("app_artifacts")

    for name in ("ix_app_grants_project_id", "ix_app_grants_user_id", "ix_app_grants_app_id"):
        op.drop_index(name, table_name="app_grants")
    op.drop_table("app_grants")

    for name in ("ix_project_apps_project_enabled", "ix_project_apps_installed_release_id"):
        op.drop_index(name, table_name="project_apps")
    op.drop_table("project_apps")

    for name in (
        "ix_app_releases_app_status_created",
        "ix_app_releases_created_at",
        "ix_app_releases_status",
        "ix_app_releases_app_id",
    ):
        op.drop_index(name, table_name="app_releases")
    op.drop_table("app_releases")

    for name in ("ix_app_targets_app_surface", "ix_app_targets_app_id"):
        op.drop_index(name, table_name="app_targets")
    op.drop_table("app_targets")

    for name in (
        "ix_apps_owner_lifecycle",
        "ix_apps_archived_at",
        "ix_apps_updated_at",
        "ix_apps_visibility",
        "ix_apps_lifecycle_status",
        "ix_apps_origin_project_id",
        "ix_apps_owner_user_id",
    ):
        op.drop_index(name, table_name="apps")
    op.drop_table("apps")
