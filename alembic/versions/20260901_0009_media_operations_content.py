"""Create MediaOps ContentVariant, QA and rights append-only ledgers."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260901_0009"
down_revision = "20260901_0008"
branch_labels = None
depends_on = None


def _uuid():
    return postgresql.UUID(as_uuid=True)


def _fk(cols, refs, *, ondelete=None):
    return sa.ForeignKeyConstraint(cols, refs, ondelete=ondelete)


# Keep generated assessment names explicit so schema audits and downstream
# migration tooling can inspect the exact append-only contract without
# evaluating Python f-strings.
_ASSESSMENT_CONTRACTS = (
    {
        "table": "media_content_variant_qa_assessments",
        "allowed_results": "passed', 'failed', 'review_required",
        "prefix": "qa",
        "result_check": "ck_media_content_variant_qa_result",
        "revision_check": "ck_media_content_variant_qa_revision_hash",
        "policy_check": "ck_media_content_variant_qa_policy_hash",
        "hash_check": "ck_media_content_variant_qa_hash",
        "assessment_unique": "uq_media_content_variant_qa_assessment_hash",
        "idempotency_unique": "uq_media_content_variant_qa_idempotency",
    },
    {
        "table": "media_content_variant_rights_assessments",
        "allowed_results": "cleared', 'blocked', 'review_required",
        "prefix": "rights",
        "result_check": "ck_media_content_variant_rights_result",
        "revision_check": "ck_media_content_variant_rights_revision_hash",
        "policy_check": "ck_media_content_variant_rights_policy_hash",
        "hash_check": "ck_media_content_variant_rights_hash",
        "assessment_unique": "uq_media_content_variant_rights_assessment_hash",
        "idempotency_unique": "uq_media_content_variant_rights_idempotency",
    },
)


def upgrade() -> None:
    op.create_table(
        "media_content_variants",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("content_item_id", _uuid(), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("create_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("platform IN ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')", name="ck_media_content_variants_platform"),
        sa.CheckConstraint("length(create_hash) = 64", name="ck_media_content_variants_create_hash"),
        _fk(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        _fk(["project_id"], ["projects.id"], ondelete="CASCADE"),
        _fk(["content_item_id"], ["media_content_items.id"], ondelete="CASCADE"),
        _fk(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_media_content_variants_owner_user_id", "media_content_variants", ["owner_user_id"])
    op.create_index("ix_media_content_variants_project_id", "media_content_variants", ["project_id"])
    op.create_index("ix_media_content_variants_content_item_id", "media_content_variants", ["content_item_id"])
    op.create_index("ix_media_content_variants_platform", "media_content_variants", ["platform"])
    op.create_index("ix_media_content_variants_create_hash", "media_content_variants", ["create_hash"])
    op.create_index("ix_media_content_variants_created_at", "media_content_variants", ["created_at"])
    op.create_index("ix_media_content_variants_owner_project", "media_content_variants", ["owner_user_id", "project_id"])
    op.create_index("uq_media_content_variants_personal_identity", "media_content_variants", ["owner_user_id", "content_item_id", "platform"], unique=True, postgresql_where=sa.text("project_id IS NULL"), sqlite_where=sa.text("project_id IS NULL"))
    op.create_index("uq_media_content_variants_project_identity", "media_content_variants", ["project_id", "content_item_id", "platform"], unique=True, postgresql_where=sa.text("project_id IS NOT NULL"), sqlite_where=sa.text("project_id IS NOT NULL"))
    op.create_index("uq_media_content_variants_personal_idempotency", "media_content_variants", ["owner_user_id", "idempotency_key"], unique=True, postgresql_where=sa.text("project_id IS NULL"), sqlite_where=sa.text("project_id IS NULL"))
    op.create_index("uq_media_content_variants_project_idempotency", "media_content_variants", ["project_id", "idempotency_key"], unique=True, postgresql_where=sa.text("project_id IS NOT NULL"), sqlite_where=sa.text("project_id IS NOT NULL"))

    op.create_table(
        "media_content_variant_revisions",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("content_variant_id", _uuid(), nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=False),
        sa.Column("project_id", _uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_item_id", _uuid(), nullable=False),
        sa.Column("content_item_hash", sa.String(64), nullable=False),
        sa.Column("persona_revision_id", _uuid(), nullable=False),
        sa.Column("persona_revision_hash", sa.String(64), nullable=False),
        sa.Column("platform_account_id", _uuid(), nullable=True),
        sa.Column("platform_account_revision_id", _uuid(), nullable=True),
        sa.Column("platform_account_revision_hash", sa.String(64), nullable=True),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("generation_output_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("source_evidence", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("created_by", _uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_media_content_variant_revisions_version"),
        sa.CheckConstraint("platform IN ('x', 'pixiv', 'dlsite', 'patreon', 'youtube', 'instagram')", name="ck_media_content_variant_revisions_platform"),
        sa.CheckConstraint("length(content_item_hash) = 64", name="ck_media_content_variant_revisions_content_item_hash"),
        sa.CheckConstraint("length(persona_revision_hash) = 64", name="ck_media_content_variant_revisions_persona_hash"),
        sa.CheckConstraint("platform_account_revision_hash IS NULL OR length(platform_account_revision_hash) = 64", name="ck_media_content_variant_revisions_account_hash"),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_media_content_variant_revisions_hash"),
        _fk(["content_variant_id"], ["media_content_variants.id"], ondelete="CASCADE"),
        _fk(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        _fk(["project_id"], ["projects.id"], ondelete="CASCADE"),
        _fk(["content_item_id"], ["media_content_items.id"], ondelete="CASCADE"),
        _fk(["persona_revision_id"], ["media_persona_revisions.id"], ondelete="CASCADE"),
        _fk(["platform_account_id"], ["media_platform_accounts.id"], ondelete="CASCADE"),
        _fk(["platform_account_revision_id"], ["media_platform_account_revisions.id"], ondelete="CASCADE"),
        _fk(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_variant_id", "version", name="uq_media_content_variant_revisions_version"),
        sa.UniqueConstraint("content_variant_id", "idempotency_key", name="uq_media_content_variant_revisions_idempotency"),
    )
    # PostgreSQL limits identifiers to 63 bytes.  Keep the names explicit and
    # bounded rather than deriving them from the already-long table name.
    revision_indexes = {
        "owner_user_id": "ix_mcv_rev_owner",
        "project_id": "ix_mcv_rev_project",
        "content_variant_id": "ix_mcv_rev_variant",
        "content_item_id": "ix_mcv_rev_item",
        "persona_revision_id": "ix_mcv_rev_persona",
        "platform_account_id": "ix_mcv_rev_account",
        "platform_account_revision_id": "ix_mcv_rev_account_rev",
        "platform": "ix_mcv_rev_platform",
        "content_item_hash": "ix_mcv_rev_item_hash",
        "persona_revision_hash": "ix_mcv_rev_persona_hash",
        "platform_account_revision_hash": "ix_mcv_rev_account_hash",
        "content_hash": "ix_mcv_rev_hash",
        "created_at": "ix_mcv_rev_created",
        "owner_project": "ix_mcv_rev_owner_project",
    }
    revision_columns = {
        "owner_user_id": ["owner_user_id"],
        "project_id": ["project_id"],
        "content_variant_id": ["content_variant_id"],
        "content_item_id": ["content_item_id"],
        "persona_revision_id": ["persona_revision_id"],
        "platform_account_id": ["platform_account_id"],
        "platform_account_revision_id": ["platform_account_revision_id"],
        "platform": ["platform"],
        "content_item_hash": ["content_item_hash"],
        "persona_revision_hash": ["persona_revision_hash"],
        "platform_account_revision_hash": ["platform_account_revision_hash"],
        "content_hash": ["content_hash"],
        "created_at": ["created_at"],
        "owner_project": ["owner_user_id", "project_id"],
    }
    for key, columns in revision_columns.items():
        op.create_index(revision_indexes[key], "media_content_variant_revisions", columns)

    for contract in _ASSESSMENT_CONTRACTS:
        table = contract["table"]
        result = contract["allowed_results"]
        prefix = contract["prefix"]
        op.create_table(
            table,
            sa.Column("id", _uuid(), nullable=False),
            sa.Column("content_variant_id", _uuid(), nullable=False),
            sa.Column("content_variant_revision_id", _uuid(), nullable=False),
            sa.Column("owner_user_id", _uuid(), nullable=False),
            sa.Column("project_id", _uuid(), nullable=True),
            sa.Column("revision_hash", sa.String(64), nullable=False),
            sa.Column("policy_revision_id", _uuid(), nullable=True),
            sa.Column("policy_revision_hash", sa.String(64), nullable=False),
            sa.Column("result", sa.String(24), nullable=False),
            sa.Column("checks", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("findings", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("evidence", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("assessment_hash", sa.String(64), nullable=False),
            sa.Column("idempotency_key", sa.String(255), nullable=True),
            sa.Column("created_by", _uuid(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(f"result IN ('{result}')", name=contract["result_check"]),
            sa.CheckConstraint("length(revision_hash) = 64", name=contract["revision_check"]),
            sa.CheckConstraint("length(policy_revision_hash) = 64", name=contract["policy_check"]),
            sa.CheckConstraint("length(assessment_hash) = 64", name=contract["hash_check"]),
            _fk(["content_variant_id"], ["media_content_variants.id"], ondelete="CASCADE"),
            _fk(["content_variant_revision_id"], ["media_content_variant_revisions.id"], ondelete="CASCADE"),
            _fk(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
            _fk(["project_id"], ["projects.id"], ondelete="CASCADE"),
            _fk(["created_by"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("content_variant_revision_id", "assessment_hash", name=contract["assessment_unique"]),
            sa.UniqueConstraint("content_variant_revision_id", "idempotency_key", name=contract["idempotency_unique"]),
        )
        assessment_columns = {
            "owner_user_id": ["owner_user_id"],
            "project_id": ["project_id"],
            "content_variant_id": ["content_variant_id"],
            "content_variant_revision_id": ["content_variant_revision_id"],
            "policy_revision_id": ["policy_revision_id"],
            "revision_hash": ["revision_hash"],
            "policy_revision_hash": ["policy_revision_hash"],
            "result": ["result"],
            "assessment_hash": ["assessment_hash"],
            "idempotency_key": ["idempotency_key"],
            "created_at": ["created_at"],
            "owner_project": ["owner_user_id", "project_id"],
        }
        assessment_indexes = {
            "owner_user_id": f"ix_mcv_{prefix}_owner",
            "project_id": f"ix_mcv_{prefix}_project",
            "content_variant_id": f"ix_mcv_{prefix}_variant",
            "content_variant_revision_id": f"ix_mcv_{prefix}_revision",
            "policy_revision_id": f"ix_mcv_{prefix}_policy",
            "revision_hash": f"ix_mcv_{prefix}_revision_hash",
            "policy_revision_hash": f"ix_mcv_{prefix}_policy_hash",
            "result": f"ix_mcv_{prefix}_result",
            "assessment_hash": f"ix_mcv_{prefix}_hash",
            "idempotency_key": f"ix_mcv_{prefix}_idempotency",
            "created_at": f"ix_mcv_{prefix}_created",
            "owner_project": f"ix_mcv_{prefix}_owner_project",
        }
        for name, columns in assessment_columns.items():
            op.create_index(assessment_indexes[name], table, columns)


def downgrade() -> None:
    op.drop_table("media_content_variant_rights_assessments")
    op.drop_table("media_content_variant_qa_assessments")
    op.drop_table("media_content_variant_revisions")
    op.drop_table("media_content_variants")
