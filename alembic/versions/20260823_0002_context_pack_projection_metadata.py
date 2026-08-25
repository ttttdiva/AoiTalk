"""Track ProjectContextPack projection freshness metadata."""

from alembic import op
import sqlalchemy as sa


revision = "20260823_0002"
down_revision = "20260823_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_context_packs",
        sa.Column("source_digest", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "project_context_packs",
        sa.Column("generated_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "project_context_packs",
        sa.Column(
            "generation_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "project_context_packs",
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="fresh",
        ),
    )
    op.create_index(
        "ix_project_context_packs_project_status",
        "project_context_packs",
        ["project_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_context_packs_project_status",
        table_name="project_context_packs",
    )
    op.drop_column("project_context_packs", "status")
    op.drop_column("project_context_packs", "generation_version")
    op.drop_column("project_context_packs", "generated_at")
    op.drop_column("project_context_packs", "source_digest")
