"""暗号化した投稿checkpointを既存attemptへ追加する。"""
from alembic import op
import sqlalchemy as sa

revision = "20261007_0020"
down_revision = "20261007_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("external_action_attempts", sa.Column("provider_state_ciphertext", sa.Text(), nullable=True))
    op.add_column("external_action_attempts", sa.Column("provider_lease_expires_at", sa.DateTime(), nullable=True))
    op.create_table("media_platform_oauth_requests",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("owner_user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.UUID(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform_account_id", sa.UUID(), sa.ForeignKey("media_platform_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_revision_id", sa.UUID(), nullable=False),
        sa.Column("credential_revision", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("state_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("encrypted_request", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("platform IN ('x','youtube')", name="ck_media_oauth_platform"),
        sa.CheckConstraint("status IN ('pending','exchanging','completed','failed')", name="ck_media_oauth_status"),
        sa.CheckConstraint("credential_revision >= 0", name="ck_media_oauth_credential_revision"),
    )


def downgrade():
    op.drop_table("media_platform_oauth_requests")
    op.drop_column("external_action_attempts", "provider_lease_expires_at")
    op.drop_column("external_action_attempts", "provider_state_ciphertext")
