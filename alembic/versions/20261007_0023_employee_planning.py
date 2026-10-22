"""限定社員計画の採用pinと累計attempt予算。"""

from alembic import op
import sqlalchemy as sa

revision = "20261007_0023"
down_revision = "20261007_0022"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("agent_delegations", reflect_kwargs={"resolve_fks": False}) as batch:
        for column in (
            sa.Column("planning_policy_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("planning_input_pins_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("plan_round", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("planning_root_id", sa.UUID(), nullable=True),
            sa.Column("adopted_plan_artifact_id", sa.UUID(), nullable=True),
            sa.Column("adopted_plan_sha256", sa.String(64), nullable=True),
            sa.Column("parent_plan_artifact_id", sa.UUID(), nullable=True),
            sa.Column("parent_plan_sha256", sa.String(64), nullable=True),
            sa.Column("parent_round", sa.Integer(), nullable=True),
            sa.Column("planning_error_code", sa.String(80), nullable=True),
            sa.Column("planning_reserved_output_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("planning_created_children", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_output_tokens", sa.Integer(), nullable=False, server_default="8192"),
            sa.Column("max_attempts_per_phase", sa.Integer(), nullable=False, server_default="3"),
        ):
            batch.add_column(column)
        batch.create_foreign_key("fk_delegation_planning_root", "agent_delegations", ["planning_root_id"], ["id"], ondelete="RESTRICT")
        batch.create_foreign_key("fk_delegation_adopted_plan", "agent_delegation_artifacts", ["adopted_plan_artifact_id"], ["id"], ondelete="RESTRICT")
        batch.create_foreign_key("fk_delegation_parent_plan", "agent_delegation_artifacts", ["parent_plan_artifact_id"], ["id"], ondelete="RESTRICT")
    with op.batch_alter_table("agent_delegation_artifacts", reflect_kwargs={"resolve_fks": False}) as batch:
        batch.drop_constraint("ck_agent_delegation_artifact_phase", type_="check")
        batch.create_check_constraint("ck_agent_delegation_artifact_phase", "phase IN ('plan','work','review','return')")
    op.create_table("agent_delegation_budget_reservations",
        sa.Column("agent_run_id", sa.UUID(), sa.ForeignKey("agent_runs.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("planning_root_id", sa.UUID(), sa.ForeignKey("agent_delegations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("delegation_id", sa.UUID(), sa.ForeignKey("agent_delegations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("work_item_id", sa.UUID(), sa.ForeignKey("agent_work_items.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("output_tokens BETWEEN 256 AND 8192", name="ck_delegation_budget_output_tokens"),
    )
    op.create_index("ix_agent_delegation_budget_reservations_planning_root_id", "agent_delegation_budget_reservations", ["planning_root_id"])


def downgrade():
    raise RuntimeError("Employee plan/attempt evidence is forward-only; restore a verified backup instead")
