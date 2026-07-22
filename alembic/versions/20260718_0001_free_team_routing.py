"""無料Teamの認証・候補・クォータ・予約台帳を追加

Revision ID: 20260718_0001
Revises: 20260713_0002
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260718_0001"
down_revision = "20260713_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "free_team_credential_profiles",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("display_name", sa.String(160), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("authentication_type", sa.String(40), nullable=False),
        sa.Column("api_key", sa.Text(), nullable=True),
        sa.Column("cli_auth_reference", sa.String(255), nullable=True),
        sa.Column("environment_variable", sa.String(120), nullable=True),
        sa.Column("base_url", sa.String(500), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("billing_mode", sa.String(40), nullable=False),
        sa.Column("privacy_class", sa.String(40), nullable=False, server_default="standard"),
        sa.Column("allow_paid_overage", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(40), nullable=False, server_default="ready"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_free_team_credential_provider", "free_team_credential_profiles", ["provider"])
    op.create_index("ix_free_team_credential_billing", "free_team_credential_profiles", ["billing_mode"])

    op.create_table(
        "free_team_quota_pools",
        sa.Column("id", sa.String(120), primary_key=True),
        sa.Column("credential_profile_id", sa.String(100), sa.ForeignKey("free_team_credential_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("metric_type", sa.String(40), nullable=False),
        sa.Column("limit_value", sa.Numeric(24, 6), nullable=False, server_default="0"),
        sa.Column("consumed", sa.Numeric(24, 6), nullable=False, server_default="0"),
        sa.Column("reserved", sa.Numeric(24, 6), nullable=False, server_default="0"),
        sa.Column("safety_margin_ratio", sa.Numeric(9, 6), nullable=False, server_default="0"),
        sa.Column("safety_margin_units", sa.Numeric(24, 6), nullable=False, server_default="0"),
        sa.Column("window_start", sa.DateTime(), nullable=True),
        sa.Column("window_end", sa.DateTime(), nullable=True),
        sa.Column("reset_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_provider_sync_at", sa.DateTime(), nullable=True),
        sa.Column("provider_observed_usage", sa.Numeric(24, 6), nullable=False, server_default="0"),
        sa.Column("status", sa.String(40), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_free_team_quota_credential", "free_team_quota_pools", ["credential_profile_id"])
    op.create_index("ix_free_team_quota_metric", "free_team_quota_pools", ["metric_type"])
    op.create_index("ix_free_team_quota_window_end", "free_team_quota_pools", ["window_end"])
    op.create_index("ix_free_team_quota_status", "free_team_quota_pools", ["status"])

    op.create_table(
        "free_team_candidate_models",
        sa.Column("id", sa.String(140), primary_key=True),
        sa.Column("credential_profile_id", sa.String(100), sa.ForeignKey("free_team_credential_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("model", sa.String(240), nullable=False),
        sa.Column("effort", sa.String(40), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("quota_pool_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("capabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("quality_class", sa.String(40), nullable=False, server_default="standard"),
        sa.Column("max_input_tokens", sa.Integer(), nullable=False, server_default="32768"),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False, server_default="2048"),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cooldown_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("tool_call_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("privacy_class", sa.String(40), nullable=False, server_default="standard"),
        sa.Column("provider_options", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("cooldown_until", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(40), nullable=False, server_default="ready"),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("selection_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("average_latency_ms", sa.Numeric(16, 3), nullable=True),
        sa.Column("last_selected_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_free_team_candidate_credential", "free_team_candidate_models", ["credential_profile_id"])
    op.create_index("ix_free_team_candidate_provider", "free_team_candidate_models", ["provider"])
    op.create_index("ix_free_team_candidate_priority", "free_team_candidate_models", ["priority"])
    op.create_index("ix_free_team_candidate_enabled", "free_team_candidate_models", ["enabled"])
    op.create_index("ix_free_team_candidate_cooldown", "free_team_candidate_models", ["cooldown_until"])
    op.create_index("ix_free_team_candidate_status", "free_team_candidate_models", ["status"])

    op.create_table(
        "free_team_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("candidate_id", sa.String(140), sa.ForeignKey("free_team_candidate_models.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("routing_profile_id", sa.String(100), nullable=False, server_default="free-team"),
        sa.Column("pool_id", sa.String(100), nullable=False),
        sa.Column("member_key", sa.String(100), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="reserved"),
        sa.Column("estimated_usage", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("actual_usage", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("quota_pool_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("error_class", sa.String(40), nullable=True),
        sa.Column("fallback_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("finalized_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_free_team_reservation_candidate", "free_team_reservations", ["candidate_id"])
    op.create_index("ix_free_team_reservation_pool", "free_team_reservations", ["pool_id"])
    op.create_index("ix_free_team_reservation_status", "free_team_reservations", ["status"])
    op.create_index("ix_free_team_reservation_created", "free_team_reservations", ["created_at"])
    op.create_index("ix_free_team_reservation_expires", "free_team_reservations", ["expires_at"])
    op.create_index("ix_free_team_reservation_pool_status", "free_team_reservations", ["pool_id", "status"])


def downgrade() -> None:
    op.drop_table("free_team_reservations")
    op.drop_table("free_team_candidate_models")
    op.drop_table("free_team_quota_pools")
    op.drop_table("free_team_credential_profiles")
