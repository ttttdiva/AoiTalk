"""Add durable managed-tool lineage, revisions, observations and promotion audit."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260830_0001"
down_revision = "20260827_0002"
branch_labels = None
depends_on = None


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    # ``current_revision_id`` is intentionally a nullable pointer.  The
    # migration creates the owning table before immutable revisions so the
    # cycle remains valid on PostgreSQL; ORM-level relationship semantics are
    # still enforced by the lineage/revision unique key and service transaction.
    op.create_table(
        "managed_tool_lineages",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("app_id", _uuid(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("runtime", sa.String(length=32), nullable=False),
        sa.Column("canonical_path", sa.Text(), nullable=False),
        sa.Column("entrypoint", sa.String(length=255), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False, server_default=sa.text("'agent_generated'")),
        sa.Column("source_agent_run_id", _uuid(), nullable=True),
        sa.Column("source_root_run_id", _uuid(), nullable=True),
        sa.Column("current_revision_id", _uuid(), nullable=True),
        sa.Column("current_sha256", sa.String(length=64), nullable=True),
        sa.Column("policy_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("discovery_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'active'")),
        sa.Column("promoted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("runtime IN ('python','powershell','shell','node')", name="ck_managed_tool_lineages_runtime"),
        sa.CheckConstraint("source_kind = 'agent_generated'", name="ck_managed_tool_lineages_source_kind"),
        sa.CheckConstraint("status IN ('active','promoted','archived')", name="ck_managed_tool_lineages_status"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_root_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_user_id", "canonical_path", name="uq_managed_tool_lineages_owner_path"),
    )
    op.create_index("ix_managed_tool_lineages_owner_user_id", "managed_tool_lineages", ["owner_user_id"])
    op.create_index("ix_managed_tool_lineages_project_id", "managed_tool_lineages", ["project_id"])
    op.create_index("ix_managed_tool_lineages_app_id", "managed_tool_lineages", ["app_id"])
    op.create_index("ix_managed_tool_lineages_source_agent_run_id", "managed_tool_lineages", ["source_agent_run_id"])
    op.create_index("ix_managed_tool_lineages_source_root_run_id", "managed_tool_lineages", ["source_root_run_id"])
    op.create_index("ix_managed_tool_lineages_current_revision_id", "managed_tool_lineages", ["current_revision_id"])
    op.create_index("ix_managed_tool_lineages_current_sha256", "managed_tool_lineages", ["current_sha256"])
    op.create_index("ix_managed_tool_lineages_status", "managed_tool_lineages", ["status"])
    op.create_index("ix_managed_tool_lineages_promoted_at", "managed_tool_lineages", ["promoted_at"])
    op.create_index("ix_managed_tool_lineages_created_at", "managed_tool_lineages", ["created_at"])
    op.create_index("ix_managed_tool_lineages_owner_status", "managed_tool_lineages", ["owner_user_id", "status"])

    op.create_table(
        "managed_tool_revisions",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("lineage_id", _uuid(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("runtime", sa.String(length=32), nullable=False),
        sa.Column("entrypoint", sa.String(length=255), nullable=False),
        sa.Column("agent_run_id", _uuid(), nullable=True),
        sa.Column("root_run_id", _uuid(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("runtime IN ('python','powershell','shell','node')", name="ck_managed_tool_revisions_runtime"),
        sa.ForeignKeyConstraint(["lineage_id"], ["managed_tool_lineages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["root_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lineage_id", "sha256", name="uq_managed_tool_revisions_lineage_sha"),
    )
    op.create_index("ix_managed_tool_revisions_lineage_id", "managed_tool_revisions", ["lineage_id"])
    op.create_index("ix_managed_tool_revisions_sha256", "managed_tool_revisions", ["sha256"])
    op.create_index("ix_managed_tool_revisions_agent_run_id", "managed_tool_revisions", ["agent_run_id"])
    op.create_index("ix_managed_tool_revisions_root_run_id", "managed_tool_revisions", ["root_run_id"])
    op.create_index("ix_managed_tool_revisions_created_at", "managed_tool_revisions", ["created_at"])
    op.create_index("ix_managed_tool_revisions_lineage_created", "managed_tool_revisions", ["lineage_id", "created_at"])

    op.create_table(
        "managed_tool_observations",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("lineage_id", _uuid(), nullable=False),
        sa.Column("revision_id", _uuid(), nullable=False),
        sa.Column("agent_run_id", _uuid(), nullable=True),
        sa.Column("root_run_id", _uuid(), nullable=True),
        sa.Column("observation_kind", sa.String(length=64), nullable=False, server_default=sa.text("'execution'")),
        sa.Column("semantic_key", sa.String(length=160), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["lineage_id"], ["managed_tool_lineages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["revision_id"], ["managed_tool_revisions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["root_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_managed_tool_observations_lineage_id", "managed_tool_observations", ["lineage_id"])
    op.create_index("ix_managed_tool_observations_revision_id", "managed_tool_observations", ["revision_id"])
    op.create_index("ix_managed_tool_observations_agent_run_id", "managed_tool_observations", ["agent_run_id"])
    op.create_index("ix_managed_tool_observations_root_run_id", "managed_tool_observations", ["root_run_id"])
    op.create_index("ix_managed_tool_observations_success", "managed_tool_observations", ["success"])
    op.create_index("ix_managed_tool_observations_created_at", "managed_tool_observations", ["created_at"])
    op.create_index("ix_managed_tool_observations_lineage_success", "managed_tool_observations", ["lineage_id", "success"])
    op.create_index("ix_managed_tool_observations_lineage_root", "managed_tool_observations", ["lineage_id", "root_run_id"])

    op.create_table(
        "managed_tool_promotion_audits",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("lineage_id", _uuid(), nullable=False),
        sa.Column("app_id", _uuid(), nullable=True),
        sa.Column("revision_id", _uuid(), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False, server_default=sa.text("'promoted'")),
        sa.Column("actor_user_id", _uuid(), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("policy_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("discovery_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("action IN ('promoted','updated','skipped')", name="ck_managed_tool_promotion_audits_action"),
        sa.ForeignKeyConstraint(["lineage_id"], ["managed_tool_lineages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["revision_id"], ["managed_tool_revisions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_managed_tool_promotion_audits_lineage_id", "managed_tool_promotion_audits", ["lineage_id"])
    op.create_index("ix_managed_tool_promotion_audits_app_id", "managed_tool_promotion_audits", ["app_id"])
    op.create_index("ix_managed_tool_promotion_audits_revision_id", "managed_tool_promotion_audits", ["revision_id"])
    op.create_index("ix_managed_tool_promotion_audits_actor_user_id", "managed_tool_promotion_audits", ["actor_user_id"])
    op.create_index("ix_managed_tool_promotion_audits_created_at", "managed_tool_promotion_audits", ["created_at"])
    op.create_index("ix_managed_tool_promotion_audits_lineage_created", "managed_tool_promotion_audits", ["lineage_id", "created_at"])

    op.create_table(
        "project_storage_operations",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=False),
        sa.Column("principal_id", _uuid(), nullable=False),
        sa.Column("operation_id", sa.String(length=256), nullable=False),
        sa.Column("diff_sha256", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default=sa.text("'prepared'")),
        sa.Column("result_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "state IN ('prepared','committed','failed')",
            name="ck_project_storage_operations_state",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["principal_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "principal_id",
            "operation_id",
            name="uq_project_storage_operations_identity",
        ),
    )
    op.create_index(
        "ix_project_storage_operations_project_id",
        "project_storage_operations",
        ["project_id"],
    )
    op.create_index(
        "ix_project_storage_operations_principal_id",
        "project_storage_operations",
        ["principal_id"],
    )
    op.create_index(
        "ix_project_storage_operations_state",
        "project_storage_operations",
        ["state"],
    )
    op.create_index(
        "ix_project_storage_operations_project_state",
        "project_storage_operations",
        ["project_id", "state"],
    )

    # Complete the nullable lineage -> current revision cycle where the
    # backend supports post-create ALTER constraints.  SQLite cannot add a
    # standalone FK with Alembic, and the pointer remains a safe nullable
    # service-managed reference in its test schema.
    bind = op.get_bind()
    if getattr(getattr(bind, "dialect", None), "name", None) == "postgresql":
        op.create_foreign_key(
            "fk_managed_tool_lineages_current_revision",
            "managed_tool_lineages",
            "managed_tool_revisions",
            ["current_revision_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if getattr(getattr(bind, "dialect", None), "name", None) == "postgresql":
        # Development snapshots may have applied an earlier draft of this
        # revision before the nullable current-revision FK was added.  Keep
        # downgrade recoverable without weakening fresh installs.
        existing = {
            item.get("name")
            for item in sa.inspect(bind).get_foreign_keys("managed_tool_lineages")
        }
        if "fk_managed_tool_lineages_current_revision" in existing:
            op.drop_constraint(
                "fk_managed_tool_lineages_current_revision",
                "managed_tool_lineages",
                type_="foreignkey",
            )
    if "project_storage_operations" in set(sa.inspect(bind).get_table_names()):
        op.drop_table("project_storage_operations")
    op.drop_table("managed_tool_promotion_audits")
    op.drop_table("managed_tool_observations")
    op.drop_table("managed_tool_revisions")
    op.drop_table("managed_tool_lineages")
