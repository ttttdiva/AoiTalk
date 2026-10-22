"""Owner-bound immutable byte revisions on existing GenerationOutput IDs."""
from alembic import op
import sqlalchemy as sa

revision = "20261007_0021"
down_revision = "20261007_0020"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("media_generation_outputs", "generation_run_id", existing_type=sa.UUID(), nullable=True)
    op.create_table("media_asset_revisions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("generation_output_id", sa.UUID(), sa.ForeignKey("media_generation_outputs.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("owner_user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.UUID(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True),
        sa.Column("persona_id", sa.UUID(), sa.ForeignKey("media_personas.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content_item_id", sa.UUID(), sa.ForeignKey("media_content_items.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=True),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(120), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("revision_hash", sa.String(64), nullable=False),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("size_bytes > 0 AND size_bytes <= 536870912", name="ck_media_asset_size"),
        sa.CheckConstraint("source_kind IN ('upload','generated_media','library_file','generation_output')", name="ck_media_asset_source"),
        sa.CheckConstraint("length(sha256) = 64 AND length(revision_hash) = 64 AND length(command_hash) = 64", name="ck_media_asset_hash"),
    )
    op.create_index("ix_media_asset_scope", "media_asset_revisions", ["owner_user_id", "project_id", "persona_id"])
    op.create_index("uq_media_asset_command", "media_asset_revisions", ["persona_id", "idempotency_key"], unique=True)


def downgrade():
    # Preserve imported output/approval identities. A downgrade is unsafe while
    # imported outputs exist; fail before removing their binding metadata.
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM media_generation_outputs WHERE generation_run_id IS NULL LIMIT 1")).first():
        raise RuntimeError("Imported media outputs must be retained; downgrade cannot discard their byte binding")
    op.drop_table("media_asset_revisions")
    op.alter_column("media_generation_outputs", "generation_run_id", existing_type=sa.UUID(), nullable=False)
