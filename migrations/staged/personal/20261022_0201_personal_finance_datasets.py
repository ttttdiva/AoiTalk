"""Add append-only owner-confirmed finance entries and immutable accepted datasets.

Staged (not autoloaded): parent is 20261022_0102. Sole creator of personal_finance_entries
(supersedes the former 0103). Application and activation are separate steps.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20261022_0201"
down_revision = "20261022_0102"
branch_labels = None
depends_on = None

_TABLES = ("personal_finance_entries", "personal_finance_datasets", "personal_finance_dataset_members")


def upgrade() -> None:
    op.create_table(
        "personal_finance_entries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("row_ref", UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("confirmed_by_kind", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("owner_user_id", "idempotency_key", name="uq_personal_finance_entries_owner_idempotency_key"),
        sa.UniqueConstraint("owner_user_id", "row_ref", name="uq_personal_finance_entries_owner_row_ref"),
        sa.UniqueConstraint("owner_user_id", "id", name="uq_personal_finance_entries_owner_id"),
        sa.CheckConstraint("confirmed_by_kind = 'user_action'", name="ck_personal_finance_entries_owner_confirmed"),
    )
    op.create_index("ix_personal_finance_entries_owner_user_id", "personal_finance_entries", ["owner_user_id"])

    op.create_table(
        "personal_finance_datasets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("members_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("owner_user_id", "id", name="uq_personal_finance_datasets_owner_id"),
        sa.UniqueConstraint("owner_user_id", "idempotency_key", name="uq_personal_finance_datasets_owner_idempotency_key"),
        sa.CheckConstraint("member_count BETWEEN 1 AND 1000", name="ck_personal_finance_datasets_member_count"),
    )
    op.create_index("ix_personal_finance_datasets_owner_user_id", "personal_finance_datasets", ["owner_user_id"])

    op.create_table(
        "personal_finance_dataset_members",
        sa.Column("dataset_id", UUID(as_uuid=True), nullable=False),
        sa.Column("row_ref", UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entry_id", UUID(as_uuid=True), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("dataset_id", "row_ref"),
        sa.ForeignKeyConstraint(
            ["owner_user_id", "dataset_id"],
            ["personal_finance_datasets.owner_user_id", "personal_finance_datasets.id"],
            name="fk_personal_finance_members_owned_dataset",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id", "entry_id"],
            ["personal_finance_entries.owner_user_id", "personal_finance_entries.id"],
            name="fk_personal_finance_members_owned_entry",
        ),
    )

    # Append only: UPDATE always rejected; direct DELETE rejected, while the
    # ON DELETE CASCADE from users (trigger depth >= 2) is allowed.
    op.execute(
        """
        CREATE FUNCTION personal_finance_append_only() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' OR (TG_OP = 'DELETE' AND pg_trigger_depth() <= 1) THEN
                RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '23000';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in _TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION personal_finance_append_only()
            """
        )

    # Snapshot freeze: the member set must equal the header's member_count at commit,
    # so members can neither be missing nor added later.
    op.execute(
        """
        CREATE FUNCTION personal_finance_dataset_complete() RETURNS trigger AS $$
        DECLARE
            ds uuid;
            expected integer;
            actual integer;
        BEGIN
            IF TG_TABLE_NAME = 'personal_finance_datasets' THEN
                ds := NEW.id;
            ELSE
                ds := NEW.dataset_id;
            END IF;
            SELECT member_count INTO expected FROM personal_finance_datasets WHERE id = ds;
            SELECT count(*) INTO actual FROM personal_finance_dataset_members WHERE dataset_id = ds;
            IF expected IS NULL OR expected <> actual THEN
                RAISE EXCEPTION 'personal finance dataset % is not an exact frozen snapshot', ds
                    USING ERRCODE = '23000';
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in ("personal_finance_datasets", "personal_finance_dataset_members"):
        op.execute(
            f"""
            CREATE CONSTRAINT TRIGGER {table}_frozen AFTER INSERT ON {table}
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION personal_finance_dataset_complete()
            """
        )


def downgrade() -> None:
    for table in ("personal_finance_datasets", "personal_finance_dataset_members"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_frozen ON {table}")
    op.execute("DROP FUNCTION IF EXISTS personal_finance_dataset_complete()")
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_table("personal_finance_dataset_members")
    op.drop_table("personal_finance_datasets")
    op.drop_table("personal_finance_entries")
    op.execute("DROP FUNCTION IF EXISTS personal_finance_append_only()")
