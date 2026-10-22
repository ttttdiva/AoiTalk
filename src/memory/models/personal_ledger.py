"""ユーザー個人の追記専用台帳（期限・在庫）のモデル。

行は一度書いたら更新も削除もしない（DBトリガで強制）。訂正は ``supersedes_entry_id``
で前の行を指す新しい行として追記し、現在値は行の集合から導出する（保存しない）。
payload は暗号化し、``subject_key`` だけが集計用の平文ラベルになる。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    JSON,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from .base import Base, _encrypted_json_property

PERSONAL_LEDGER_KINDS = ("deadline", "inventory")
PERSONAL_LEDGER_ENTRY_TYPES = (
    "deadline_candidate",
    "deadline_verification",
    "deadline_completion",
    "opening_balance",
    "purchase",
    "consumption",
    "void",
)
PERSONAL_LEDGER_ACTOR_KINDS = ("user_action", "agent")
# 実世界の事実を主張する行。モデル由来の入力だけでは作れない。
PERSONAL_LEDGER_USER_ONLY_ENTRY_TYPES = ("deadline_verification", "deadline_completion")


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class PersonalLedgerEntry(Base):
    """追記専用台帳の1行。owner_user_id で必ず利用者に閉じる。"""

    __tablename__ = "personal_ledger_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 決定的な並び順（recorded_at の同時刻衝突に依存しない）。
    seq = Column(BigInteger, Identity(always=True), nullable=False)
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ledger = Column(String(16), nullable=False)
    entry_type = Column(String(32), nullable=False)
    # 中立なラベル（書類種別・品目）。識別子や番号は入れない。
    subject_key = Column(String(64), nullable=False)
    actor_kind = Column(String(16), nullable=False)
    supersedes_entry_id = Column(
        UUID(as_uuid=True), ForeignKey("personal_ledger_entries.id"), nullable=True
    )
    refers_to_entry_id = Column(
        UUID(as_uuid=True), ForeignKey("personal_ledger_entries.id"), nullable=True
    )
    idempotency_key = Column(String(128), nullable=False)
    _payload = Column("payload", JSON, nullable=False)
    payload = _encrypted_json_property("_payload", "personal_ledger_entries.payload")
    recorded_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("owner_user_id", "id", name="uq_personal_ledger_owner_id"),
        UniqueConstraint("seq", name="uq_personal_ledger_entries_seq"),
        UniqueConstraint("owner_user_id", "idempotency_key", name="uq_personal_ledger_idempotency"),
        # 1つの行を訂正できる行は1つだけ（同時訂正の片方を確実に失敗させる）。
        Index(
            "uq_personal_ledger_single_successor",
            "supersedes_entry_id",
            unique=True,
            postgresql_where=text("supersedes_entry_id IS NOT NULL"),
        ),
        Index("ix_personal_ledger_owner_subject", "owner_user_id", "ledger", "subject_key", "seq"),
        CheckConstraint(f"ledger IN ({_sql_list(PERSONAL_LEDGER_KINDS)})", name="ck_personal_ledger_kind"),
        CheckConstraint(
            f"entry_type IN ({_sql_list(PERSONAL_LEDGER_ENTRY_TYPES)})",
            name="ck_personal_ledger_entry_type",
        ),
        CheckConstraint(
            f"actor_kind IN ({_sql_list(PERSONAL_LEDGER_ACTOR_KINDS)})",
            name="ck_personal_ledger_actor_kind",
        ),
        CheckConstraint(
            f"entry_type NOT IN ({_sql_list(PERSONAL_LEDGER_USER_ONLY_ENTRY_TYPES)}) OR actor_kind = 'user_action'",
            name="ck_personal_ledger_user_only_types",
        ),
        CheckConstraint(
            "(entry_type IN ('deadline_verification', 'deadline_completion')) = (refers_to_entry_id IS NOT NULL)",
            name="ck_personal_ledger_refers_to",
        ),
        CheckConstraint(
            "entry_type <> 'void' OR supersedes_entry_id IS NOT NULL",
            name="ck_personal_ledger_void_supersedes",
        ),
        CheckConstraint(
            "supersedes_entry_id IS NULL OR supersedes_entry_id <> id",
            name="ck_personal_ledger_no_self_supersede",
        ),
    )
