"""Persist Skill execution outcomes and dispatch fences."""

from alembic import op
import sqlalchemy as sa

revision = "20261010_0002"
down_revision = "20261010_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "skill_executions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(200), nullable=False),
        sa.Column("session_id", sa.String(200), nullable=False),
        sa.Column("request_id", sa.String(160), nullable=False),
        sa.Column("skill_name", sa.String(160), nullable=False),
        sa.Column("skill_path", sa.String(512), nullable=False),
        sa.Column("skill_hash", sa.String(64), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("active_key", sa.String(64), unique=True),
        sa.Column("commit_key", sa.String(64), unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(160), nullable=False),
        sa.Column("commit_dispatched", sa.Boolean(), nullable=False),
        sa.Column("steps_completed", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(64)),
        sa.Column("lease_until", sa.DateTime()),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "request_id", name="uq_skill_execution_request"),
        sa.CheckConstraint("status IN ('running','succeeded','failed','interrupted','needs_user','unknown')",
                           name="ck_skill_execution_status"),
    )
    op.create_index("ix_skill_executions_user_id", "skill_executions", ["user_id"])


def downgrade():
    op.drop_index("ix_skill_executions_user_id", table_name="skill_executions")
    op.drop_table("skill_executions")
