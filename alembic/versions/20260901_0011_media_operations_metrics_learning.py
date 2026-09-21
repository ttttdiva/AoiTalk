"""Create MediaOps metrics, experiments, revenue and learning ledgers."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260901_0011"
down_revision = "20260901_0010"
branch_labels = None
depends_on = None


def _uuid():
    return postgresql.UUID(as_uuid=True)


def _fk(columns, references, *, ondelete=None):
    return sa.ForeignKeyConstraint(columns, references, ondelete=ondelete)


def upgrade() -> None:
    op.create_table(
        "media_metric_snapshots",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("persona_ref", sa.String(164), nullable=True),
        sa.Column("platform_account_ref", sa.String(164), nullable=True),
        sa.Column("content_variant_ref", sa.String(164), nullable=True),
        sa.Column("publication_ref", sa.String(164), nullable=True),
        sa.Column("period_start", sa.DateTime(), nullable=True),
        sa.Column("period_end", sa.DateTime(), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(128), nullable=False, server_default="manual"),
        sa.Column("normalized_metrics", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("platform_metrics", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("provenance", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("completeness", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("ingestion_status", sa.String(16), nullable=False, server_default="accepted"),
        sa.Column("correction_of_id", _uuid(), nullable=True),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("source IN ('manual', 'imported', 'api')", name="ck_media_metric_snapshots_source"),
        sa.CheckConstraint("completeness IN ('complete', 'partial', 'unknown')", name="ck_media_metric_snapshots_completeness"),
        sa.CheckConstraint("ingestion_status IN ('accepted', 'rejected', 'superseded')", name="ck_media_metric_snapshots_ingestion_status"),
        sa.CheckConstraint("length(snapshot_hash) = 64", name="ck_media_metric_snapshots_hash"),
        _fk(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        _fk(["project_id"], ["projects.id"], ondelete="CASCADE"),
        _fk(["correction_of_id"], ["media_metric_snapshots.id"], ondelete="SET NULL"),
        _fk(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_media_metric_snapshots_owner_user_id", "media_metric_snapshots", ["owner_user_id"])
    op.create_index("ix_media_metric_snapshots_project_id", "media_metric_snapshots", ["project_id"])
    op.create_index("ix_media_metric_snapshots_persona_ref", "media_metric_snapshots", ["persona_ref"])
    op.create_index("ix_media_metric_snapshots_platform_account_ref", "media_metric_snapshots", ["platform_account_ref"])
    op.create_index("ix_media_metric_snapshots_content_variant_ref", "media_metric_snapshots", ["content_variant_ref"])
    op.create_index("ix_media_metric_snapshots_publication_ref", "media_metric_snapshots", ["publication_ref"])
    op.create_index("ix_media_metric_snapshots_period_start", "media_metric_snapshots", ["period_start"])
    op.create_index("ix_media_metric_snapshots_period_end", "media_metric_snapshots", ["period_end"])
    op.create_index("ix_media_metric_snapshots_observed_at", "media_metric_snapshots", ["observed_at"])
    op.create_index("ix_media_metric_snapshots_source", "media_metric_snapshots", ["source"])
    op.create_index("ix_media_metric_snapshots_correction_of_id", "media_metric_snapshots", ["correction_of_id"])
    op.create_index("ix_media_metric_snapshots_snapshot_hash", "media_metric_snapshots", ["snapshot_hash"])
    op.create_index("ix_media_metric_snapshots_created_at", "media_metric_snapshots", ["created_at"])
    op.create_index("ix_media_metric_snapshots_owner_project", "media_metric_snapshots", ["owner_user_id", "project_id"])
    op.create_index("uq_media_metric_snapshots_personal_idempotency", "media_metric_snapshots", ["owner_user_id", "idempotency_key"], unique=True, postgresql_where=sa.text("project_id IS NULL"), sqlite_where=sa.text("project_id IS NULL"))
    op.create_index("uq_media_metric_snapshots_project_idempotency", "media_metric_snapshots", ["project_id", "idempotency_key"], unique=True, postgresql_where=sa.text("project_id IS NOT NULL"), sqlite_where=sa.text("project_id IS NOT NULL"))

    op.create_table(
        "media_experiments",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("persona_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("account_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("variant_groups", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("primary_metric", sa.String(64), nullable=False),
        sa.Column("secondary_metrics", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("minimum_sample_size", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("create_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('draft', 'running', 'completed', 'inconclusive', 'cancelled')", name="ck_media_experiments_status"),
        sa.CheckConstraint("minimum_sample_size >= 1 AND minimum_sample_size <= 10000000", name="ck_media_experiments_sample_size"),
        sa.CheckConstraint("length(create_hash) = 64", name="ck_media_experiments_create_hash"),
        _fk(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        _fk(["project_id"], ["projects.id"], ondelete="CASCADE"),
        _fk(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_media_experiments_owner_user_id", "media_experiments", ["owner_user_id"])
    op.create_index("ix_media_experiments_project_id", "media_experiments", ["project_id"])
    op.create_index("ix_media_experiments_status", "media_experiments", ["status"])
    op.create_index("ix_media_experiments_create_hash", "media_experiments", ["create_hash"])
    op.create_index("ix_media_experiments_created_at", "media_experiments", ["created_at"])
    op.create_index("ix_media_experiments_owner_project", "media_experiments", ["owner_user_id", "project_id"])
    op.create_index("uq_media_experiments_personal_idempotency", "media_experiments", ["owner_user_id", "idempotency_key"], unique=True, postgresql_where=sa.text("project_id IS NULL"), sqlite_where=sa.text("project_id IS NULL"))
    op.create_index("uq_media_experiments_project_idempotency", "media_experiments", ["project_id", "idempotency_key"], unique=True, postgresql_where=sa.text("project_id IS NOT NULL"), sqlite_where=sa.text("project_id IS NOT NULL"))

    op.create_table(
        "media_experiment_results",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("experiment_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="inconclusive"),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("sample_sizes", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("group_metrics", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("winner_variant_ref", sa.String(164), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("uncertainty", sa.Float(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("conclusion", sa.Text(), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('complete', 'inconclusive')", name="ck_media_experiment_results_status"),
        sa.CheckConstraint("sample_size >= 0 AND sample_size <= 10000000", name="ck_media_experiment_results_sample_size"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_media_experiment_results_confidence"),
        sa.CheckConstraint("uncertainty >= 0 AND uncertainty <= 1", name="ck_media_experiment_results_uncertainty"),
        sa.CheckConstraint("length(result_hash) = 64", name="ck_media_experiment_results_hash"),
        _fk(["experiment_id"], ["media_experiments.id"], ondelete="CASCADE"),
        _fk(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        _fk(["project_id"], ["projects.id"], ondelete="CASCADE"),
        _fk(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", "idempotency_key", name="uq_media_experiment_results_idempotency"),
    )
    for name, columns in {
        "experiment_id": ["experiment_id"],
        "owner_user_id": ["owner_user_id"],
        "project_id": ["project_id"],
        "result_hash": ["result_hash"],
        "created_at": ["created_at"],
        "owner_project": ["owner_user_id", "project_id"],
    }.items():
        op.create_index(f"ix_media_experiment_results_{name}", "media_experiment_results", columns)

    op.create_table(
        "media_revenue_events",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("persona_ref", sa.String(164), nullable=True),
        sa.Column("platform_account_ref", sa.String(164), nullable=True),
        sa.Column("platform", sa.String(32), nullable=True),
        sa.Column("content_ref", sa.String(164), nullable=True),
        sa.Column("publication_ref", sa.String(164), nullable=True),
        sa.Column("product_ref", sa.String(164), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(128), nullable=False, server_default="manual"),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column("gross_amount", sa.Float(), nullable=False),
        sa.Column("net_amount", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("event_at", sa.DateTime(), nullable=False),
        sa.Column("settlement_at", sa.DateTime(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("correction_of_id", _uuid(), nullable=True),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("source IN ('manual', 'imported', 'api')", name="ck_media_revenue_events_source"),
        sa.CheckConstraint("event_type IN ('sale', 'refund', 'chargeback', 'adjustment', 'reversal')", name="ck_media_revenue_events_type"),
        sa.CheckConstraint("length(currency) = 3", name="ck_media_revenue_events_currency"),
        sa.CheckConstraint("length(event_hash) = 64", name="ck_media_revenue_events_hash"),
        _fk(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        _fk(["project_id"], ["projects.id"], ondelete="CASCADE"),
        _fk(["correction_of_id"], ["media_revenue_events.id"], ondelete="SET NULL"),
        _fk(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, columns in {
        "owner_user_id": ["owner_user_id"],
        "project_id": ["project_id"],
        "persona_ref": ["persona_ref"],
        "platform_account_ref": ["platform_account_ref"],
        "platform": ["platform"],
        "content_ref": ["content_ref"],
        "publication_ref": ["publication_ref"],
        "product_ref": ["product_ref"],
        "source": ["source"],
        "event_type": ["event_type"],
        "event_at": ["event_at"],
        "settlement_at": ["settlement_at"],
        "correction_of_id": ["correction_of_id"],
        "event_hash": ["event_hash"],
        "created_at": ["created_at"],
        "owner_project": ["owner_user_id", "project_id"],
    }.items():
        op.create_index(f"ix_media_revenue_events_{name}", "media_revenue_events", columns)
    op.create_index("uq_media_revenue_events_personal_idempotency", "media_revenue_events", ["owner_user_id", "idempotency_key"], unique=True, postgresql_where=sa.text("project_id IS NULL"), sqlite_where=sa.text("project_id IS NULL"))
    op.create_index("uq_media_revenue_events_project_idempotency", "media_revenue_events", ["project_id", "idempotency_key"], unique=True, postgresql_where=sa.text("project_id IS NOT NULL"), sqlite_where=sa.text("project_id IS NOT NULL"))

    op.create_table(
        "media_learning_proposals",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("subject_ref", sa.String(164), nullable=False),
        sa.Column("proposal_type", sa.String(32), nullable=False, server_default="learning"),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("uncertainty", sa.Float(), nullable=False),
        sa.Column("human_review_required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("review_policy", sa.String(64), nullable=False, server_default="human_review_before_apply"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending_review"),
        sa.Column("proposal_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('pending_review', 'rejected', 'accepted', 'stale')", name="ck_media_learning_proposals_status"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_media_learning_proposals_confidence"),
        sa.CheckConstraint("uncertainty >= 0 AND uncertainty <= 1", name="ck_media_learning_proposals_uncertainty"),
        sa.CheckConstraint("human_review_required IS TRUE", name="ck_media_learning_proposals_human_review"),
        sa.CheckConstraint("length(proposal_hash) = 64", name="ck_media_learning_proposals_hash"),
        _fk(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        _fk(["project_id"], ["projects.id"], ondelete="CASCADE"),
        _fk(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_media_learning_proposals_owner_user_id", "media_learning_proposals", ["owner_user_id"])
    op.create_index("ix_media_learning_proposals_project_id", "media_learning_proposals", ["project_id"])
    op.create_index("ix_media_learning_proposals_status", "media_learning_proposals", ["status"])
    op.create_index("ix_media_learning_proposals_proposal_hash", "media_learning_proposals", ["proposal_hash"])
    op.create_index("ix_media_learning_proposals_created_at", "media_learning_proposals", ["created_at"])
    op.create_index("ix_media_learning_proposals_owner_project", "media_learning_proposals", ["owner_user_id", "project_id"])
    op.create_index("uq_media_learning_proposals_personal_idempotency", "media_learning_proposals", ["owner_user_id", "idempotency_key"], unique=True, postgresql_where=sa.text("project_id IS NULL"), sqlite_where=sa.text("project_id IS NULL"))
    op.create_index("uq_media_learning_proposals_project_idempotency", "media_learning_proposals", ["project_id", "idempotency_key"], unique=True, postgresql_where=sa.text("project_id IS NOT NULL"), sqlite_where=sa.text("project_id IS NOT NULL"))


def downgrade() -> None:
    op.drop_table("media_learning_proposals")
    op.drop_table("media_revenue_events")
    op.drop_table("media_experiment_results")
    op.drop_table("media_experiments")
    op.drop_table("media_metric_snapshots")
