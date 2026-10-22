"""Add the append-only per-user personal ledger (deadlines and inventory)."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20261022_0001"
down_revision = "20261021_0001"
branch_labels = None
depends_on = None

_KINDS = "'deadline', 'inventory'"
_ENTRY_TYPES = (
    "'deadline_candidate', 'deadline_verification', 'deadline_completion', "
    "'opening_balance', 'purchase', 'consumption', 'void'"
)
_USER_ONLY = "'deadline_verification', 'deadline_completion'"


def upgrade() -> None:
    op.create_table(
        "personal_ledger_entries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("owner_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ledger", sa.String(16), nullable=False),
        sa.Column("entry_type", sa.String(32), nullable=False),
        sa.Column("subject_key", sa.String(64), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("supersedes_entry_id", UUID(as_uuid=True), sa.ForeignKey("personal_ledger_entries.id"), nullable=True),
        sa.Column("refers_to_entry_id", UUID(as_uuid=True), sa.ForeignKey("personal_ledger_entries.id"), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("seq", name="uq_personal_ledger_entries_seq"),
        sa.UniqueConstraint("owner_user_id", "idempotency_key", name="uq_personal_ledger_idempotency"),
        sa.CheckConstraint(f"ledger IN ({_KINDS})", name="ck_personal_ledger_kind"),
        sa.CheckConstraint(f"entry_type IN ({_ENTRY_TYPES})", name="ck_personal_ledger_entry_type"),
        sa.CheckConstraint("actor_kind IN ('user_action', 'agent')", name="ck_personal_ledger_actor_kind"),
        # 保存行の整合のみを守る。actor_kind は保存値で、上流の認証を DB は証明できない
        # （信頼境界は personal_ledger_service の docstring を参照）。
        sa.CheckConstraint(
            f"entry_type NOT IN ({_USER_ONLY}) OR actor_kind = 'user_action'",
            name="ck_personal_ledger_user_only_types",
        ),
        sa.CheckConstraint(
            "(entry_type IN ('deadline_verification', 'deadline_completion')) = (refers_to_entry_id IS NOT NULL)",
            name="ck_personal_ledger_refers_to",
        ),
        sa.CheckConstraint(
            "entry_type <> 'void' OR supersedes_entry_id IS NOT NULL",
            name="ck_personal_ledger_void_supersedes",
        ),
        sa.CheckConstraint(
            "supersedes_entry_id IS NULL OR supersedes_entry_id <> id",
            name="ck_personal_ledger_no_self_supersede",
        ),
    )
    op.create_index("ix_personal_ledger_entries_owner_user_id", "personal_ledger_entries", ["owner_user_id"])
    op.create_index(
        "uq_personal_ledger_single_successor",
        "personal_ledger_entries",
        ["supersedes_entry_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_entry_id IS NOT NULL"),
    )
    op.create_index(
        "ix_personal_ledger_owner_subject",
        "personal_ledger_entries",
        ["owner_user_id", "ledger", "subject_key", "seq"],
    )
    # 追記専用: UPDATE は常に拒否する。DELETE は直接実行だけ拒否し、
    # users 行の削除に伴う ON DELETE CASCADE（トリガ深度 2 以上）は通す。
    op.execute(
        """
        CREATE FUNCTION personal_ledger_entries_append_only() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'personal_ledger_entries is append-only' USING ERRCODE = '23000';
            END IF;
            IF TG_OP = 'DELETE' AND pg_trigger_depth() <= 1 THEN
                RAISE EXCEPTION 'personal_ledger_entries is append-only' USING ERRCODE = '23000';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER personal_ledger_entries_append_only
        BEFORE UPDATE OR DELETE ON personal_ledger_entries
        FOR EACH ROW EXECUTE FUNCTION personal_ledger_entries_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS personal_ledger_entries_append_only ON personal_ledger_entries")
    op.execute("DROP FUNCTION IF EXISTS personal_ledger_entries_append_only()")
    op.drop_table("personal_ledger_entries")
