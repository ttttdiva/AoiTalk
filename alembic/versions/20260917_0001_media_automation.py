"""Add AoiTalk-owned MediaOps Automation control plane."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision="20260917_0001"
down_revision="20260909_0002"
branch_labels=None
depends_on=None


def upgrade()->None:
    op.create_table("media_automation_programs",
        sa.Column("id",postgresql.UUID(as_uuid=True),primary_key=True),
        sa.Column("owner_user_id",postgresql.UUID(as_uuid=True),sa.ForeignKey("users.id",ondelete="CASCADE"),nullable=False),
        sa.Column("project_id",postgresql.UUID(as_uuid=True),sa.ForeignKey("projects.id",ondelete="CASCADE"),nullable=True),
        sa.Column("name",sa.String(255),nullable=False), sa.Column("enabled",sa.Boolean(),nullable=False,server_default=sa.text("true")),
        sa.Column("create_hash",sa.String(64),nullable=False), sa.Column("idempotency_key",sa.String(128),nullable=False),
        sa.Column("created_by",postgresql.UUID(as_uuid=True),sa.ForeignKey("users.id",ondelete="SET NULL"),nullable=True),
        sa.Column("created_at",sa.DateTime(),nullable=False), sa.Column("updated_at",sa.DateTime(),nullable=False),
        sa.CheckConstraint("length(create_hash)=64",name="ck_media_automation_programs_create_hash"))
    op.create_index("ix_media_automation_programs_owner_user_id","media_automation_programs",["owner_user_id"]); op.create_index("ix_media_automation_programs_project_id","media_automation_programs",["project_id"])
    op.create_index("uq_media_automation_programs_global_idem","media_automation_programs",["owner_user_id","idempotency_key"],unique=True,postgresql_where=sa.text("project_id IS NULL"))
    op.create_index("uq_media_automation_programs_project_idem","media_automation_programs",["project_id","idempotency_key"],unique=True,postgresql_where=sa.text("project_id IS NOT NULL"))
    op.create_table("media_automation_program_revisions",
        sa.Column("id",postgresql.UUID(as_uuid=True),primary_key=True), sa.Column("program_id",postgresql.UUID(as_uuid=True),sa.ForeignKey("media_automation_programs.id",ondelete="CASCADE"),nullable=False),
        sa.Column("owner_user_id",postgresql.UUID(as_uuid=True),sa.ForeignKey("users.id",ondelete="CASCADE"),nullable=False), sa.Column("project_id",postgresql.UUID(as_uuid=True),sa.ForeignKey("projects.id",ondelete="CASCADE"),nullable=True),
        sa.Column("version",sa.Integer(),nullable=False), sa.Column("execution_mode",sa.String(32),nullable=False),
        sa.Column("trigger_json",sa.JSON(),nullable=False),sa.Column("discovery_json",sa.JSON(),nullable=False),sa.Column("research_binding_json",sa.JSON(),nullable=False),sa.Column("planning_policy_json",sa.JSON(),nullable=False),sa.Column("generation_action_json",sa.JSON(),nullable=False),sa.Column("fallback_json",sa.JSON(),nullable=False),
        sa.Column("content_hash",sa.String(64),nullable=False),sa.Column("idempotency_key",sa.String(128),nullable=True),sa.Column("created_by",postgresql.UUID(as_uuid=True),sa.ForeignKey("users.id",ondelete="SET NULL"),nullable=True),sa.Column("created_at",sa.DateTime(),nullable=False),
        sa.UniqueConstraint("program_id","version",name="uq_media_automation_program_revisions_version"),sa.UniqueConstraint("program_id","idempotency_key",name="uq_media_automation_program_revisions_idem"),
        sa.CheckConstraint("version>=1",name="ck_media_automation_program_revisions_version"),sa.CheckConstraint("execution_mode IN ('research_only','draft','review_before_generate','auto_generate')",name="ck_media_automation_program_revisions_mode"),sa.CheckConstraint("length(content_hash)=64",name="ck_media_automation_program_revisions_hash"))
    for name in ("program_id","owner_user_id","project_id","content_hash","created_at"): op.create_index(f"ix_media_automation_program_revisions_{name}","media_automation_program_revisions",[name])
    op.create_table("media_automation_runs",
        sa.Column("id",postgresql.UUID(as_uuid=True),primary_key=True),sa.Column("program_id",postgresql.UUID(as_uuid=True),sa.ForeignKey("media_automation_programs.id",ondelete="CASCADE"),nullable=False),sa.Column("program_revision_id",postgresql.UUID(as_uuid=True),sa.ForeignKey("media_automation_program_revisions.id",ondelete="RESTRICT"),nullable=False),
        sa.Column("owner_user_id",postgresql.UUID(as_uuid=True),sa.ForeignKey("users.id",ondelete="CASCADE"),nullable=False),sa.Column("project_id",postgresql.UUID(as_uuid=True),sa.ForeignKey("projects.id",ondelete="CASCADE"),nullable=True),
        sa.Column("execution_key",sa.String(64),nullable=False,unique=True),sa.Column("trigger_kind",sa.String(32),nullable=False,server_default="manual"),sa.Column("state",sa.String(32),nullable=False,server_default="scheduled"),
        sa.Column("observation_json",sa.JSON(),nullable=False),sa.Column("research_run_id",postgresql.UUID(as_uuid=True),sa.ForeignKey("media_research_runs.id",ondelete="SET NULL"),nullable=True),sa.Column("research_brief_json",sa.JSON(),nullable=False),sa.Column("candidates_json",sa.JSON(),nullable=False),sa.Column("selected_candidate_id",sa.String(176),nullable=True),sa.Column("novelty_snapshot_json",sa.JSON(),nullable=False),
        sa.Column("generation_request_json",sa.JSON(),nullable=True),sa.Column("generation_request_hash",sa.String(64),nullable=True),sa.Column("external_idempotency_key",sa.String(256),nullable=True,unique=True),sa.Column("external_run_id",sa.String(176),nullable=True),sa.Column("preset_id",sa.String(176),nullable=True),sa.Column("preset_revision_id",sa.String(176),nullable=True),sa.Column("preset_revision_number",sa.Integer(),nullable=True),sa.Column("preset_checksum",sa.String(64),nullable=True),sa.Column("result_deep_link",sa.String(2000),nullable=True),sa.Column("generation_result_json",sa.JSON(),nullable=False),
        sa.Column("error_code",sa.String(128),nullable=True),sa.Column("error_message",sa.Text(),nullable=True),sa.Column("correlation_id",sa.String(176),nullable=True),sa.Column("started_at",sa.DateTime(),nullable=True),sa.Column("completed_at",sa.DateTime(),nullable=True),sa.Column("created_by",postgresql.UUID(as_uuid=True),sa.ForeignKey("users.id",ondelete="SET NULL"),nullable=True),sa.Column("created_at",sa.DateTime(),nullable=False),sa.Column("updated_at",sa.DateTime(),nullable=False),
        sa.CheckConstraint("length(execution_key)=64",name="ck_media_automation_runs_execution_key"),sa.CheckConstraint("state IN ('scheduled','theme_discovery','research','brief','concept_planning','prompt_planning','waiting_review','generation_submitting','generation_running','complete','failed','uncertain')",name="ck_media_automation_runs_state"),sa.CheckConstraint("generation_request_hash IS NULL OR length(generation_request_hash)=64",name="ck_media_automation_runs_generation_hash"),sa.CheckConstraint("preset_checksum IS NULL OR length(preset_checksum)=64",name="ck_media_automation_runs_preset_checksum"))
    for name in ("program_id","program_revision_id","owner_user_id","project_id","execution_key","state","research_run_id","generation_request_hash","external_idempotency_key","external_run_id","correlation_id","created_at"): op.create_index(f"ix_media_automation_runs_{name}","media_automation_runs",[name])
    op.create_index("ix_media_automation_runs_program_created","media_automation_runs",["program_id","created_at"])

def downgrade()->None:
    op.drop_table("media_automation_runs"); op.drop_table("media_automation_program_revisions"); op.drop_table("media_automation_programs")
