"""Add per-owner career profile versions, confirmations, and job sources.

Append-only triggers reject every UPDATE and direct DELETE; deletes cascaded from
``users`` (trigger depth >= 2) are allowed so account removal still works.

Staged migration (not autoloaded): chain 0002 -> 0003 -> 0102 -> 0201 -> 0104.
See migrations/staged/personal/MANIFEST.md.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261022_0102"
down_revision = "20261022_0003"
branch_labels = None
depends_on = None

_APPEND_ONLY_TABLES = (
    "personal_career_sources",
    "personal_career_source_selections",
    "personal_career_profile_versions",
    "personal_career_profile_confirmations",
)


def upgrade() -> None:
    op.create_table(
        "personal_career_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_ref", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("seq", name="uq_personal_career_sources_seq"),
        sa.UniqueConstraint(
            "owner_user_id", "id", name="uq_personal_career_sources_owner_id"
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "source_ref",
            name="uq_personal_career_sources_owner_ref",
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_personal_career_sources_idempotency",
        ),
        sa.CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_personal_career_sources_sha256",
        ),
        sa.CheckConstraint(
            "byte_size >= 1 AND byte_size <= 10485760",
            name="ck_personal_career_sources_byte_size",
        ),
        sa.CheckConstraint(
            "char_length(mime_type) BETWEEN 1 AND 128",
            name="ck_personal_career_sources_mime_type",
        ),
        sa.CheckConstraint(
            "actor_kind IN ('user_action', 'model')",
            name="ck_personal_career_sources_actor_kind",
        ),
    )
    op.create_index(
        "ix_personal_career_sources_owner_user_id",
        "personal_career_sources",
        ["owner_user_id"],
    )

    op.create_table(
        "personal_career_source_selections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("seq", name="uq_personal_career_source_selections_seq"),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_personal_career_source_selections_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id", "source_id"],
            ["personal_career_sources.owner_user_id", "personal_career_sources.id"],
            name="fk_personal_career_source_selections_owner_source",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "actor_kind = 'user_action'",
            name="ck_personal_career_source_selections_actor_kind",
        ),
    )
    op.create_index(
        "ix_personal_career_source_selections_owner_source",
        "personal_career_source_selections",
        ["owner_user_id", "source_id", "seq"],
    )
    op.create_index(
        "ix_personal_career_source_selections_owner_user_id",
        "personal_career_source_selections",
        ["owner_user_id"],
    )

    op.create_table(
        "personal_career_profile_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("seq", name="uq_personal_career_profile_versions_seq"),
        sa.UniqueConstraint(
            "owner_user_id", "id", name="uq_personal_career_profile_versions_owner_id"
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "version",
            name="uq_personal_career_profile_versions_owner_version",
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_personal_career_profile_versions_idempotency",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_personal_career_profile_versions_version"
        ),
        sa.CheckConstraint(
            "actor_kind IN ('user_action', 'model')",
            name="ck_personal_career_profile_versions_actor_kind",
        ),
    )
    op.create_index(
        "ix_personal_career_profile_versions_owner_version",
        "personal_career_profile_versions",
        ["owner_user_id", "version"],
    )
    op.create_index(
        "ix_personal_career_profile_versions_owner_user_id",
        "personal_career_profile_versions",
        ["owner_user_id"],
    )

    op.create_table(
        "personal_career_profile_confirmations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("confirmed_digest", sa.String(length=64), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("seq", name="uq_personal_career_profile_confirmations_seq"),
        sa.UniqueConstraint(
            "owner_user_id",
            "version_id",
            name="uq_personal_career_profile_confirmations_owner_version",
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_personal_career_profile_confirmations_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id", "version_id"],
            [
                "personal_career_profile_versions.owner_user_id",
                "personal_career_profile_versions.id",
            ],
            name="fk_personal_career_profile_confirmations_owner_version",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "confirmed_digest ~ '^[0-9a-f]{64}$'",
            name="ck_personal_career_profile_confirmations_digest",
        ),
        sa.CheckConstraint(
            "actor_kind = 'user_action'",
            name="ck_personal_career_profile_confirmations_actor_kind",
        ),
    )
    op.create_index(
        "ix_personal_career_profile_confirmations_owner_version",
        "personal_career_profile_confirmations",
        ["owner_user_id", "version_id"],
    )
    op.create_index(
        "ix_personal_career_profile_confirmations_owner_user_id",
        "personal_career_profile_confirmations",
        ["owner_user_id"],
    )

    for table in _APPEND_ONLY_TABLES:
        op.execute(
            f"""
            CREATE FUNCTION {table}_append_only() RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'UPDATE' OR pg_trigger_depth() <= 1 THEN
                    RAISE EXCEPTION '{table} is append-only' USING ERRCODE = '23000';
                END IF;
                RETURN OLD;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION {table}_append_only()
            """
        )


def downgrade() -> None:
    for table in reversed(_APPEND_ONLY_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {table}_append_only()")
    op.drop_table("personal_career_profile_confirmations")
    op.drop_table("personal_career_profile_versions")
    op.drop_table("personal_career_source_selections")
    op.drop_table("personal_career_sources")
