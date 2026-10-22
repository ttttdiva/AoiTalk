"""Keep registered byte-binding evidence outside the deletable asset ledger."""

from alembic import op
import sqlalchemy as sa

revision = "20261007_0022"
down_revision = "20261007_0021"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "media_generation_outputs",
        sa.Column(
            "byte_binding_state",
            sa.String(16),
            nullable=False,
            server_default="external",
        ),
    )
    op.execute(
        sa.text("""
        UPDATE media_generation_outputs SET byte_binding_state = 'managed'
        WHERE generation_run_id IS NULL OR id IN (
            SELECT generation_output_id FROM media_asset_revisions
        )
    """)
    )
    op.create_check_constraint(
        "ck_media_generation_outputs_byte_binding_state",
        "media_generation_outputs",
        "byte_binding_state IN ('external', 'managed')",
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text("""
        SELECT 1 FROM media_generation_outputs WHERE byte_binding_state = 'managed' LIMIT 1
    """)
        )
        .first()
    ):
        raise RuntimeError("Registered media binding evidence must be retained")
    op.drop_constraint(
        "ck_media_generation_outputs_byte_binding_state",
        "media_generation_outputs",
        type_="check",
    )
    op.drop_column("media_generation_outputs", "byte_binding_state")
