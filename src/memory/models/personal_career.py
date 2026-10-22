"""個人のキャリア情報（職務プロフィールと求人source）のモデル。

境界:
- 家計・期限・在庫とはテーブルもFKも共有しない（finance の行を参照しない）。
- プロフィール本文は暗号化JSONで保存する。source は参照・ハッシュ・種別・サイズのみで、
  パス・URL・本文は持たない。
- version / confirmation / source selection は追記のみ。DBトリガで UPDATE/DELETE を拒否する。
- owner_user_id は全テーブルで必須。複合FKで別ownerのsource/versionを参照できない。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID

from .base import Base, _encrypted_json_property

# source DTO の固定境界（docs/design/personal-agents-20261022/tool-contracts.json）。
SOURCE_SHA256_PATTERN = r"^[0-9a-f]{64}$"
MAX_SOURCE_BYTES = 10_485_760
MAX_MIME_TYPE_CHARS = 128
MAX_IDEMPOTENCY_KEY_CHARS = 128
MAX_PROFILE_ITEMS = 32
SKILL_MAX_CHARS = 100
TEXT_MAX_CHARS = 200
MAX_UNCONFIRMED_CLAIMS = 32
MAX_CLAIM_CHARS = 500
MAX_SOURCE_REFS = 16

# 暗号化対象の payload キー。context adapter は authoritative だけを読む。
PROFILE_PAYLOAD_KEYS = ("skills", "preferences", "constraints")


class PersonalCareerSource(Base):
    """owner が選択した求人sourceのメタデータ。本文もパスも保持しない。"""

    __tablename__ = "personal_career_sources"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seq = Column(BigInteger, Identity(always=True), nullable=False)
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_ref = Column(UUID(as_uuid=True), nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    mime_type = Column(String(MAX_MIME_TYPE_CHARS), nullable=False)
    byte_size = Column(BigInteger, nullable=False)
    actor_kind = Column(String(16), nullable=False)
    idempotency_key = Column(String(MAX_IDEMPOTENCY_KEY_CHARS), nullable=False)
    recorded_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("seq", name="uq_personal_career_sources_seq"),
        UniqueConstraint("owner_user_id", "id", name="uq_personal_career_sources_owner_id"),
        UniqueConstraint(
            "owner_user_id",
            "source_ref",
            name="uq_personal_career_sources_owner_ref",
        ),
        UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_personal_career_sources_idempotency",
        ),
        CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_personal_career_sources_sha256",
        ),
        CheckConstraint(
            "byte_size >= 1 AND byte_size <= 10485760",
            name="ck_personal_career_sources_byte_size",
        ),
        CheckConstraint(
            "char_length(mime_type) BETWEEN 1 AND 128",
            name="ck_personal_career_sources_mime_type",
        ),
        CheckConstraint(
            "actor_kind IN ('user_action', 'model')",
            name="ck_personal_career_sources_actor_kind",
        ),
    )


class PersonalCareerSourceSelection(Base):
    """source の選択/解除の履歴。現在の選択は owner ごとの最新行。"""

    __tablename__ = "personal_career_source_selections"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seq = Column(BigInteger, Identity(always=True), nullable=False)
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_id = Column(UUID(as_uuid=True), nullable=False)
    selected = Column(Boolean, nullable=False)
    actor_kind = Column(String(16), nullable=False)
    idempotency_key = Column(String(MAX_IDEMPOTENCY_KEY_CHARS), nullable=False)
    recorded_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("seq", name="uq_personal_career_source_selections_seq"),
        UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_personal_career_source_selections_idempotency",
        ),
        ForeignKeyConstraint(
            ["owner_user_id", "source_id"],
            ["personal_career_sources.owner_user_id", "personal_career_sources.id"],
            name="fk_personal_career_source_selections_owner_source",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "actor_kind = 'user_action'",
            name="ck_personal_career_source_selections_actor_kind",
        ),
        Index(
            "ix_personal_career_source_selections_owner_source",
            "owner_user_id",
            "source_id",
            "seq",
        ),
    )


class PersonalCareerProfileVersion(Base):
    """owner ごとのプロフィール版。payload は暗号化し、行は追記のみ。"""

    __tablename__ = "personal_career_profile_versions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seq = Column(BigInteger, Identity(always=True), nullable=False)
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version = Column(Integer, nullable=False)
    actor_kind = Column(String(16), nullable=False)
    _payload = Column("payload", JSON, nullable=False)
    payload = _encrypted_json_property(
        "_payload", "personal_career_profile_versions.payload"
    )
    idempotency_key = Column(String(MAX_IDEMPOTENCY_KEY_CHARS), nullable=False)
    recorded_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("seq", name="uq_personal_career_profile_versions_seq"),
        UniqueConstraint(
            "owner_user_id",
            "id",
            name="uq_personal_career_profile_versions_owner_id",
        ),
        UniqueConstraint(
            "owner_user_id",
            "version",
            name="uq_personal_career_profile_versions_owner_version",
        ),
        UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_personal_career_profile_versions_idempotency",
        ),
        CheckConstraint(
            "version >= 1", name="ck_personal_career_profile_versions_version"
        ),
        CheckConstraint(
            "actor_kind IN ('user_action', 'model')",
            name="ck_personal_career_profile_versions_actor_kind",
        ),
        Index(
            "ix_personal_career_profile_versions_owner_version",
            "owner_user_id",
            "version",
        ),
    )


class PersonalCareerProfileConfirmation(Base):
    """owner 本人の確認記録。確認済みversionはこの行の有無で決まる（version行は不変）。"""

    __tablename__ = "personal_career_profile_confirmations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seq = Column(BigInteger, Identity(always=True), nullable=False)
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_id = Column(UUID(as_uuid=True), nullable=False)
    confirmed_digest = Column(String(64), nullable=False)
    actor_kind = Column(String(16), nullable=False)
    idempotency_key = Column(String(MAX_IDEMPOTENCY_KEY_CHARS), nullable=False)
    recorded_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("seq", name="uq_personal_career_profile_confirmations_seq"),
        UniqueConstraint(
            "owner_user_id",
            "version_id",
            name="uq_personal_career_profile_confirmations_owner_version",
        ),
        UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_personal_career_profile_confirmations_idempotency",
        ),
        ForeignKeyConstraint(
            ["owner_user_id", "version_id"],
            [
                "personal_career_profile_versions.owner_user_id",
                "personal_career_profile_versions.id",
            ],
            name="fk_personal_career_profile_confirmations_owner_version",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "confirmed_digest ~ '^[0-9a-f]{64}$'",
            name="ck_personal_career_profile_confirmations_digest",
        ),
        CheckConstraint(
            "actor_kind = 'user_action'",
            name="ck_personal_career_profile_confirmations_actor_kind",
        ),
        Index(
            "ix_personal_career_profile_confirmations_owner_version",
            "owner_user_id",
            "version_id",
        ),
    )
