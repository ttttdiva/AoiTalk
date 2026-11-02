"""Add outer Agent Team graph headers and immutable revisions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20261031_0003"
down_revision = "20261031_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    current_fk = [sa.ForeignKeyConstraint(
        ["current_revision_id"], ["agent_team_graph_revisions.id"],
        name="fk_agent_team_graphs_current_revision",
    )] if sqlite else []
    op.create_table(
        "agent_team_graphs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("create_hash", sa.String(64), nullable=False),
        sa.Column("current_revision_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("created_by", "idempotency_key", name="uq_agent_team_graphs_creator_idempotency"),
        sa.CheckConstraint("length(title) BETWEEN 1 AND 160", name="ck_agent_team_graphs_title"),
        sa.CheckConstraint("length(create_hash) = 64", name="ck_agent_team_graphs_create_hash"),
        *current_fk,
    )
    op.create_index("ix_agent_team_graphs_project_id", "agent_team_graphs", ["project_id"])
    op.create_index("ix_agent_team_graphs_created_by", "agent_team_graphs", ["created_by"])
    op.create_table(
        "agent_team_graph_revisions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("graph_id", UUID(as_uuid=True), sa.ForeignKey("agent_team_graphs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("definition_encrypted", sa.Text(), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("graph_id", "version", name="uq_agent_team_graph_revisions_version"),
        sa.CheckConstraint("version > 0", name="ck_agent_team_graph_revisions_version"),
        sa.CheckConstraint("length(title) BETWEEN 1 AND 160", name="ck_agent_team_graph_revisions_title"),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_agent_team_graph_revisions_hash"),
    )
    op.create_index("ix_agent_team_graph_revisions_graph_id", "agent_team_graph_revisions", ["graph_id"])
    if not sqlite:
        op.create_foreign_key("fk_agent_team_graphs_current_revision", "agent_team_graphs", "agent_team_graph_revisions", ["current_revision_id"], ["id"])


def downgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("fk_agent_team_graphs_current_revision", "agent_team_graphs", type_="foreignkey")
    else:
        # Break the cycle before dropping tables with SQLite foreign keys enabled.
        op.execute(sa.text("UPDATE agent_team_graphs SET current_revision_id = NULL"))
    op.drop_table("agent_team_graph_revisions")
    op.drop_table("agent_team_graphs")
