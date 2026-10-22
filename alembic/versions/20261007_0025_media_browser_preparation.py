"""Persist immutable Bridge draft preparation with lease and unknown fences."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20261007_0025"
down_revision = "20261007_0024"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("external_actions", sa.Column("browser_preparation_id", UUID(as_uuid=True), nullable=True))
    op.create_table("media_browser_preparations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("action_id", UUID(as_uuid=True), sa.ForeignKey("external_actions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True),
        sa.Column("action_version", sa.Integer(), nullable=False), sa.Column("action_state_version", sa.Integer(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("platform_account_id", UUID(as_uuid=True), nullable=False), sa.Column("platform_account_revision_id", UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False), sa.Column("device_id", sa.String(128), nullable=False),
        sa.Column("tab_id", sa.Integer(), nullable=False), sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("command_hash", sa.String(64), nullable=False), sa.Column("text_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(40), nullable=False), sa.Column("phase", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("checkpoint_ciphertext", sa.Text(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True), sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("action_id", "action_version", name="uq_media_browser_preparation_action_revision"),
        sa.CheckConstraint("status IN ('preparing','awaiting_final_confirmation','uncertain','blocked')", name="ck_media_browser_preparation_status"),
        sa.CheckConstraint("action_version>0 AND action_state_version>0 AND version>0", name="ck_media_browser_preparation_version"),
        sa.CheckConstraint("length(payload_hash)=64 AND length(command_hash)=64 AND length(text_sha256)=64", name="ck_media_browser_preparation_hash"),
        sa.CheckConstraint("platform IN ('x','youtube')", name="ck_media_browser_preparation_platform"))
    op.create_index("ix_media_browser_preparations_action_id", "media_browser_preparations", ["action_id"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM media_browser_preparations LIMIT 1")).first():
        raise RuntimeError("Browser preparation evidence must be retained")
    op.drop_table("media_browser_preparations")
    op.drop_column("external_actions", "browser_preparation_id")
