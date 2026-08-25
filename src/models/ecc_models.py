"""
ECC (Everything Claude Code) 機能統合用DBモデル
- イベント駆動自動化
- トークン/コスト追跡
- モデルルーティング設定
- スキル拡張（カテゴリ、チェーン、プリセット）
- ワークフローエンジン
- Dreamingメモリは src.memory.models.ContextMemory に統一
"""

import json
import uuid
from datetime import datetime
from typing import Dict, Any
from sqlalchemy import (
    Column,
    String,
    Text,
    Integer,
    DateTime,
    Float,
    Numeric,
    JSON,
    ForeignKey,
    Boolean,
    Index,
    UniqueConstraint,
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import configure_mappers, relationship

from ..memory.models import Base

# ────────────────────────────────────────────
# 1b. 統合キャラクター（キャラクターYAML + カスタムエージェント統合）
# ────────────────────────────────────────────


class Character(Base):
    """統合キャラクターモデル: 音声設定・性格・エージェント機能・画像生成設定を一元管理"""

    __tablename__ = "characters"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)  # 表示名 "琴葉葵"
    slug = Column(String(100), nullable=False, unique=True)  # 機械名 "kotonoha_aoi"
    character_type = Column(
        String(20), default="assistant"
    )  # assistant, roleplay, trpg_npc, gm
    system_prompt = Column(Text, default="")  # LLMシステムプロンプト
    model = Column(String(100), default="")  # LLMモデル指定（空=デフォルト）
    allowed_tools = Column(JSON, default=list)  # ツール制限リスト
    is_enabled = Column(Boolean, default=True)

    # 音声設定（旧YAML voice セクション）
    voice_engine = Column(String(50), default="")  # voicevox, voiceroid, cevio, etc.
    voice_name = Column(String(100), default="")
    voice_id = Column(String(50), default="")
    speaker_id = Column(Integer, nullable=True)
    voice_parameters = Column(JSON, default=dict)  # {volume, pitch, speed, intonation}

    # 性格設定（旧YAML personality セクション）
    greeting = Column(Text, default="")
    invalid_content_reply = Column(Text, default="")
    fallback_reply = Column(Text, default="")
    goodbye_reply = Column(Text, default="")
    recognition_aliases = Column(JSON, default=list)  # ["葵", "あおい", "Aoi"]

    # ロールプレイ設定（Character Card V2相当）
    description = Column(Text, default="")  # キャラの詳細設定（性格/外見/設定）
    personality_summary = Column(Text, default="")  # 性格の短い要約
    first_message = Column(Text, default="")  # 初回グリーティングメッセージ
    alternate_greetings = Column(JSON, default=list)  # 代替グリーティング配列
    example_messages = Column(Text, default="")  # 口調を教える会話サンプル
    scenario = Column(Text, default="")  # シナリオ/状況設定

    # RP画像自動生成設定
    auto_image_gen = Column(Boolean, default=False)  # RP中の自動画像生成ON/OFF
    image_gen_trigger = Column(
        String(20), default="scene_change"
    )  # "scene_change" / "every_n" / "emotion_change"
    image_gen_interval = Column(Integer, default=5)  # every_nの場合のメッセージ間隔

    # 外見・画像生成（新規）
    appearance_tags = Column(
        Text, default=""
    )  # Danbooruタグ "1girl, blue_hair, short_hair"
    negative_tags = Column(Text, default="")  # ネガティブプロンプト
    image_gen_engine = Column(String(20), default="")  # "comfyui", "gemini", ""
    comfyui_config = Column(
        JSON, default=dict
    )  # {workflow_path, checkpoint, lora, ...}
    avatar_image_path = Column(String(500), default="")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_characters_slug", "slug", unique=True),
        Index("ix_characters_type", "character_type"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "slug": self.slug,
            "character_type": self.character_type,
            "system_prompt": self.system_prompt,
            "model": self.model or "",
            "allowed_tools": self.allowed_tools or [],
            "is_enabled": self.is_enabled,
            # 音声
            "voice_engine": self.voice_engine or "",
            "voice_name": self.voice_name or "",
            "voice_id": self.voice_id or "",
            "speaker_id": self.speaker_id,
            "voice_parameters": self.voice_parameters or {},
            # 性格
            "greeting": self.greeting or "",
            "invalid_content_reply": self.invalid_content_reply or "",
            "fallback_reply": self.fallback_reply or "",
            "goodbye_reply": self.goodbye_reply or "",
            "recognition_aliases": self.recognition_aliases or [],
            # ロールプレイ
            "description": self.description or "",
            "personality_summary": self.personality_summary or "",
            "first_message": self.first_message or "",
            "alternate_greetings": self.alternate_greetings or [],
            "example_messages": self.example_messages or "",
            "scenario": self.scenario or "",
            # RP画像自動生成
            "auto_image_gen": self.auto_image_gen or False,
            "image_gen_trigger": self.image_gen_trigger or "scene_change",
            "image_gen_interval": self.image_gen_interval or 5,
            # 外見・画像生成
            "appearance_tags": self.appearance_tags or "",
            "negative_tags": self.negative_tags or "",
            "image_gen_engine": self.image_gen_engine or "",
            "comfyui_config": self.comfyui_config or {},
            "avatar_image_path": self.avatar_image_path or "",
            # タイムスタンプ
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ────────────────────────────────────────────
# 1c. TRPG再利用資産（Story Studioの正本モデルはmemory.models.story）
# ────────────────────────────────────────────


class TRPGPlayerCharacterSheet(Base):
    """ユーザー所有のTRPGプレイヤーキャラクター保存シート。

    シナリオ定義側のNPC/敵/プリセットキャラとは分け、卓参加時に使う
    ユーザーPCの再利用データを保持する。
    """

    __tablename__ = "trpg_player_character_sheets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scenario_id = Column(
        UUID(as_uuid=True),
        nullable=False,
    )
    user_id = Column(UUID(as_uuid=True), nullable=False)
    ruleset = Column(String(50), default="")
    name = Column(String(100), nullable=False)
    description = Column(Text, default="")
    trpg_pc_state = Column(JSON, default=dict)
    sheet_metadata = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_trpg_player_sheets_scenario_user", "scenario_id", "user_id"),
        Index("ix_trpg_player_sheets_user", "user_id"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "scenario_id": str(self.scenario_id),
            "user_id": str(self.user_id),
            "ruleset": self.ruleset or "",
            "name": self.name,
            "description": self.description or "",
            "trpg_ruleset": self.ruleset or "",
            "trpg_pc_state": self.trpg_pc_state or {},
            "sheet_metadata": self.sheet_metadata or {},
            "sheet_source": "trpg_player_character_sheets",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TRPGRulesetProfile(Base):
    """TRPGルールシステム定義。

    シナリオの ruleset 文字列から、GM指示・キャラシート形式・判定方針を解決する。
    ルール資料の親は TRPGReferenceDocument に一本化する。
    """

    __tablename__ = "trpg_ruleset_profiles"

    key = Column(String(50), primary_key=True)
    display_name = Column(String(120), nullable=False)
    edition = Column(String(50), default="")
    system_type = Column(String(50), default="generic")
    description = Column(Text, default="")
    gm_rules_brief = Column(Text, default="")
    character_sheet_schema = Column(JSON, default=dict)
    default_pc_state = Column(JSON, default=dict)
    resource_schema = Column(JSON, default=dict)
    dice_rule_schema = Column(JSON, default=dict)
    skill_resolver = Column(JSON, default=dict)
    profile_metadata = Column(JSON, default=dict)
    is_enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    reference_documents = relationship(
        "TRPGReferenceDocument", back_populates="ruleset_profile", cascade="all, delete-orphan"
    )
    rulebooks = relationship(
        "TRPGRulebookDocument", back_populates="ruleset_profile", cascade="all, delete-orphan"
    )
    rule_items = relationship(
        "TRPGRuleItem", back_populates="ruleset_profile", cascade="all, delete-orphan"
    )
    supplement_documents = relationship(
        "TRPGSupplementDocument", back_populates="ruleset_profile", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_trpg_ruleset_profiles_system_type", "system_type"),
        Index("ix_trpg_ruleset_profiles_enabled", "is_enabled"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "display_name": self.display_name,
            "edition": self.edition or "",
            "system_type": self.system_type or "generic",
            "description": self.description or "",
            "gm_rules_brief": self.gm_rules_brief or "",
            "character_sheet_schema": self.character_sheet_schema or {},
            "default_pc_state": self.default_pc_state or {},
            "resource_schema": self.resource_schema or {},
            "dice_rule_schema": self.dice_rule_schema or {},
            "skill_resolver": self.skill_resolver or {},
            "metadata": self.profile_metadata or {},
            "is_enabled": bool(self.is_enabled),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TRPGReferenceDocument(Base):
    """Unified parent document for TRPG rulebooks, supplements, and aids.

    This is the runtime/source-of-truth parent for structured rule items and
    creature entries. Different game systems are separated by ruleset_key;
    different document kinds are separated by document_type / supplement_kind.
    """

    __tablename__ = "trpg_reference_documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ruleset_key = Column(
        String(50),
        ForeignKey("trpg_ruleset_profiles.key", ondelete="CASCADE"),
        nullable=False,
    )
    title = Column(String(200), nullable=False)
    source_label = Column(Text, default="")
    source_text = Column(Text, default="")
    document_type = Column(String(50), default="rulebook")
    supplement_kind = Column(String(80), default="general")
    structure = Column(JSON, default=dict)
    priority = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    document_metadata = Column(JSON, default=dict)
    import_status = Column(String(40), default="metadata_only")
    legacy_source_table = Column(String(80), default="")
    legacy_source_id = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    ruleset_profile = relationship("TRPGRulesetProfile", back_populates="reference_documents")
    rule_items = relationship("TRPGRuleItem", back_populates="reference_document")
    creature_entries = relationship("TRPGCreatureEntry", back_populates="reference_document")

    __table_args__ = (
        Index("ix_trpg_reference_documents_ruleset_key", "ruleset_key"),
        Index("ix_trpg_reference_documents_active", "ruleset_key", "is_active"),
        Index("ix_trpg_reference_documents_type", "ruleset_key", "document_type"),
        Index("ix_trpg_reference_documents_legacy", "legacy_source_table", "legacy_source_id"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "ruleset_key": self.ruleset_key,
            "title": self.title,
            "source_label": self.source_label or "",
            "source_text": self.source_text or "",
            "document_type": self.document_type or "rulebook",
            "supplement_kind": self.supplement_kind or "general",
            "structure": self.structure or {},
            "priority": self.priority or 0,
            "is_active": bool(self.is_active),
            "metadata": self.document_metadata or {},
            "import_status": self.import_status or "metadata_only",
            "legacy_source_table": self.legacy_source_table or "",
            "legacy_source_id": str(self.legacy_source_id) if self.legacy_source_id else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TRPGRulebookDocument(Base):
    """Deprecated legacy parent for imported rulebook text.

    Runtime code should use TRPGReferenceDocument instead.
    """

    __tablename__ = "trpg_rulebook_documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ruleset_key = Column(
        String(50),
        ForeignKey("trpg_ruleset_profiles.key", ondelete="CASCADE"),
        nullable=False,
    )
    title = Column(String(200), nullable=False)
    source_label = Column(Text, default="")
    source_text = Column(Text, default="")
    structure = Column(JSON, default=dict)
    priority = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    ruleset_profile = relationship("TRPGRulesetProfile", back_populates="rulebooks")
    __table_args__ = (
        Index("ix_trpg_rulebook_documents_ruleset_key", "ruleset_key"),
        Index("ix_trpg_rulebook_documents_active", "ruleset_key", "is_active"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "ruleset_key": self.ruleset_key,
            "title": self.title,
            "source_label": self.source_label or "",
            "source_text": self.source_text or "",
            "structure": self.structure or {},
            "priority": self.priority or 0,
            "is_active": bool(self.is_active),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TRPGRuleItem(Base):
    """Structured, citable rule item extracted from user-provided rule text."""

    __tablename__ = "trpg_rule_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ruleset_key = Column(
        String(50),
        ForeignKey("trpg_ruleset_profiles.key", ondelete="CASCADE"),
        nullable=False,
    )
    source_document_id = Column(
        UUID(as_uuid=True),
        ForeignKey("trpg_rulebook_documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    reference_document_id = Column(
        UUID(as_uuid=True),
        ForeignKey("trpg_reference_documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_kind = Column(String(50), default="rulebook")
    source_title = Column(String(200), default="")
    rule_domain = Column(String(80), default="general")
    mechanic_key = Column(String(120), default="")
    title = Column(String(240), nullable=False)
    normalized_name = Column(String(240), default="")
    source_span = Column(JSON, default=dict)
    raw_excerpt = Column(Text, default="")
    structured_data = Column(JSON, default=dict)
    confidence = Column(Float, default=0.0)
    needs_review = Column(Boolean, default=True)
    tags = Column(JSON, default=list)
    priority = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    ruleset_profile = relationship("TRPGRulesetProfile", back_populates="rule_items")
    reference_document = relationship("TRPGReferenceDocument", back_populates="rule_items")
    mechanic_links = relationship(
        "TRPGMechanicRuleLink", back_populates="rule_item", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_trpg_rule_items_ruleset", "ruleset_key"),
        Index("ix_trpg_rule_items_domain", "ruleset_key", "rule_domain"),
        Index("ix_trpg_rule_items_mechanic", "ruleset_key", "mechanic_key"),
        Index("ix_trpg_rule_items_normalized_name", "ruleset_key", "normalized_name"),
        Index("ix_trpg_rule_items_active", "ruleset_key", "is_active"),
        Index("ix_trpg_rule_items_reference", "reference_document_id"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "ruleset_key": self.ruleset_key,
            "source_document_id": str(self.source_document_id) if self.source_document_id else None,
            "reference_document_id": str(self.reference_document_id) if self.reference_document_id else None,
            "source_kind": self.source_kind or "rulebook",
            "source_title": self.source_title or "",
            "rule_domain": self.rule_domain or "general",
            "mechanic_key": self.mechanic_key or "",
            "title": self.title,
            "normalized_name": self.normalized_name or "",
            "source_span": self.source_span or {},
            "raw_excerpt": self.raw_excerpt or "",
            "structured_data": self.structured_data or {},
            "confidence": float(self.confidence or 0.0),
            "needs_review": bool(self.needs_review),
            "tags": self.tags or [],
            "priority": self.priority or 0,
            "is_active": bool(self.is_active),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TRPGMechanicRuleLink(Base):
    """Link executable CoC mechanics to their supporting structured rule items."""

    __tablename__ = "trpg_mechanic_rule_links"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ruleset_key = Column(
        String(50),
        ForeignKey("trpg_ruleset_profiles.key", ondelete="CASCADE"),
        nullable=False,
    )
    mechanic_key = Column(String(120), nullable=False)
    rule_item_id = Column(
        UUID(as_uuid=True),
        ForeignKey("trpg_rule_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    runtime_module = Column(String(240), default="")
    runtime_function = Column(String(160), default="")
    priority = Column(Integer, default=0)
    link_metadata = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    rule_item = relationship("TRPGRuleItem", back_populates="mechanic_links")

    __table_args__ = (
        Index("ix_trpg_mechanic_rule_links_mechanic", "ruleset_key", "mechanic_key"),
        Index("ix_trpg_mechanic_rule_links_rule_item", "rule_item_id"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "ruleset_key": self.ruleset_key,
            "mechanic_key": self.mechanic_key,
            "rule_item_id": str(self.rule_item_id),
            "runtime_module": self.runtime_module or "",
            "runtime_function": self.runtime_function or "",
            "priority": self.priority or 0,
            "metadata": self.link_metadata or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TRPGSupplementDocument(Base):
    """Deprecated legacy supplement parent.

    Runtime code should use TRPGReferenceDocument instead.
    """

    __tablename__ = "trpg_supplement_documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ruleset_key = Column(
        String(50),
        ForeignKey("trpg_ruleset_profiles.key", ondelete="CASCADE"),
        nullable=False,
    )
    title = Column(String(200), nullable=False)
    source_label = Column(Text, default="")
    source_text = Column(Text, default="")
    document_type = Column(String(50), default="supplement")
    supplement_kind = Column(String(80), default="general")
    priority = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    document_metadata = Column(JSON, default=dict)
    import_status = Column(String(40), default="metadata_only")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    ruleset_profile = relationship("TRPGRulesetProfile", back_populates="supplement_documents")
    creature_entries = relationship(
        "TRPGCreatureEntry", back_populates="supplement_document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_trpg_supplement_documents_ruleset_key", "ruleset_key"),
        Index("ix_trpg_supplement_documents_active", "ruleset_key", "is_active"),
        Index("ix_trpg_supplement_documents_kind", "ruleset_key", "supplement_kind"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "ruleset_key": self.ruleset_key,
            "title": self.title,
            "source_label": self.source_label or "",
            "document_type": self.document_type or "supplement",
            "supplement_kind": self.supplement_kind or "general",
            "priority": self.priority or 0,
            "is_active": bool(self.is_active),
            "metadata": self.document_metadata or {},
            "import_status": self.import_status or "metadata_only",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TRPGCreatureEntry(Base):
    """CoC creature/deity entry extracted from supplement text."""

    __tablename__ = "trpg_creature_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    supplement_document_id = Column(
        UUID(as_uuid=True),
        ForeignKey("trpg_supplement_documents.id", ondelete="CASCADE"),
        nullable=True,
    )
    reference_document_id = Column(
        UUID(as_uuid=True),
        ForeignKey("trpg_reference_documents.id", ondelete="CASCADE"),
        nullable=True,
    )
    ruleset_key = Column(String(50), nullable=False)
    name = Column(String(200), nullable=False)
    normalized_name = Column(String(240), default="")
    entry_type = Column(String(50), default="creature")
    classification = Column(String(100), default="")
    summary = Column(Text, default="")
    source_excerpt = Column(Text, default="")
    char_start = Column(Integer, default=0)
    char_end = Column(Integer, default=0)
    confidence = Column(String(30), default="medium")
    tags = Column(JSON, default=list)
    entry_metadata = Column(JSON, default=dict)
    source_span = Column(JSON, default=dict)
    ocr_status = Column(String(40), default="unreviewed")
    characteristics = Column(JSON, default=dict)
    skills = Column(JSON, default=dict)
    attacks = Column(JSON, default=list)
    armor = Column(Text, default="")
    spells = Column(JSON, default=list)
    abilities = Column(JSON, default=list)
    san_loss = Column(String(80), default="")
    mechanic_links = Column(JSON, default=list)
    needs_review = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    supplement_document = relationship("TRPGSupplementDocument", back_populates="creature_entries")
    reference_document = relationship("TRPGReferenceDocument", back_populates="creature_entries")

    __table_args__ = (
        Index("ix_trpg_creature_entries_ruleset", "ruleset_key"),
        Index("ix_trpg_creature_entries_supplement", "supplement_document_id"),
        Index("ix_trpg_creature_entries_reference", "reference_document_id"),
        Index("ix_trpg_creature_entries_name", "ruleset_key", "normalized_name"),
        Index("ix_trpg_creature_entries_type", "ruleset_key", "entry_type"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "supplement_document_id": str(self.supplement_document_id) if self.supplement_document_id else None,
            "reference_document_id": str(self.reference_document_id) if self.reference_document_id else None,
            "ruleset_key": self.ruleset_key,
            "name": self.name,
            "normalized_name": self.normalized_name or "",
            "entry_type": self.entry_type or "creature",
            "classification": self.classification or "",
            "summary": self.summary or "",
            "raw_excerpt": self.source_excerpt or "",
            "source_excerpt": self.source_excerpt or "",
            "source_span": self.source_span or {"char_start": self.char_start or 0, "char_end": self.char_end or 0},
            "confidence": self.confidence or "medium",
            "tags": self.tags or [],
            "metadata": self.entry_metadata or {},
            "ocr_status": self.ocr_status or "unreviewed",
            "characteristics": self.characteristics or {},
            "skills": self.skills or {},
            "attacks": self.attacks or [],
            "armor": self.armor or "",
            "spells": self.spells or [],
            "abilities": self.abilities or [],
            "san_loss": self.san_loss or "",
            "mechanic_links": self.mechanic_links or [],
            "needs_review": bool(self.needs_review),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# 2. イベント駆動自動化
# ────────────────────────────────────────────


def _num_str(value: Any) -> Any:
    """Numeric列（Decimal）をJSON安全な文字列へ変換する。Noneはそのまま返す。"""
    if value is None:
        return None
    return str(value)


class TokenUsage(Base):
    """LLM API呼び出しごとのトークン使用量

    コスト列の位置づけ:
    - ``input_cost`` / ``output_cost`` / ``total_cost``（DOUBLE PRECISION）は
      **後方互換のための概算値**であり、正本ではない。既存API・既存画面が参照し続け
      られるように残しているだけで、新規の計算・集計の根拠に使ってはならない。
    - 金額の**正本**は ``list_input_cost`` / ``list_output_cost`` / ``list_tool_cost`` /
      ``list_total_cost``（定価換算、NUMERIC）と、プロバイダが実額を返す場合の
      ``provider_reported_cost``（NUMERIC）である。料金計算は Decimal のみで行い、
      float を経由しない。
    - ``model`` は互換のため ``requested_model`` と同値を書き続ける。モデル名の正本は
      ``requested_model``（要求時）と ``resolved_model``（プロバイダが実際に処理した
      モデル）である。
    """

    __tablename__ = "token_usage"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    user_id = Column(String, nullable=True, index=True)
    project_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    # モデル・プロバイダ情報
    provider = Column(String(50), nullable=False)  # openai, gemini, anthropic, etc.
    model = Column(String(100), nullable=False)
    agent_name = Column(String(100), nullable=True)  # specialist名 or custom agent名

    # トークン数
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    cached_tokens = Column(Integer, default=0)
    reasoning_tokens = Column(Integer, default=0)
    cache_read_tokens = Column(Integer, default=0)
    cache_write_tokens = Column(Integer, default=0)
    prompt_eval_tokens = Column(Integer, default=0)
    prompt_eval_ms = Column(Integer, default=0)
    cache_hit_rate = Column(Float, nullable=True)
    cache_evictions = Column(Integer, default=0)
    cache_provider = Column(String(50), nullable=True)
    cache_mode = Column(String(50), nullable=True)
    cache_key = Column(String(128), nullable=True)
    cache_supported = Column(Boolean, nullable=True)
    cache_active = Column(Boolean, nullable=True)
    metrics_source = Column(String(50), nullable=True)

    # コスト（USD）※後方互換の概算値。正本は list_* と provider_reported_cost。
    input_cost = Column(Float, default=0.0)
    output_cost = Column(Float, default=0.0)
    total_cost = Column(Float, default=0.0)

    # 料金計算基盤（正本）
    requested_model = Column(String(200), nullable=True)
    resolved_model = Column(String(200), nullable=True)
    billing_scope_id = Column(String(100), nullable=True)
    pricing_status = Column(String(30), nullable=True)
    pricing_catalog_version = Column(String(50), nullable=True)
    pricing_rule_id = Column(String(200), nullable=True)
    free_incentive_group = Column(String(10), nullable=True)

    # 適用単価（USD per 1M、長文倍率・段階料金の適用後）
    applied_input_rate = Column(Numeric(18, 8), nullable=True)
    applied_cached_input_rate = Column(Numeric(18, 8), nullable=True)
    applied_cache_write_rate = Column(Numeric(18, 8), nullable=True)
    applied_output_rate = Column(Numeric(18, 8), nullable=True)

    # 定価換算コスト（USD）
    list_input_cost = Column(Numeric(20, 10), nullable=True)
    list_output_cost = Column(Numeric(20, 10), nullable=True)
    list_tool_cost = Column(Numeric(20, 10), nullable=True)
    list_total_cost = Column(Numeric(20, 10), nullable=True)

    # プロバイダ申告コスト（OpenRouter等）
    provider_reported_cost = Column(Numeric(20, 10), nullable=True)
    provider_reported_cost_details = Column(JSONB, nullable=True)

    # ツール呼び出し回数 {"web_search": 2}
    tool_invocations = Column(JSONB, nullable=True)

    # メタデータ
    request_type = Column(String(50), default="chat")  # chat, embedding, tts, stt
    latency_ms = Column(Integer, default=0)
    is_streaming = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_token_usage_date_model", "created_at", "model"),
        Index("ix_token_usage_project_date", "project_id", "created_at"),
        Index("ix_token_usage_pricing_status", "pricing_status"),
        Index("ix_token_usage_scope_created", "billing_scope_id", "created_at"),
        Index(
            "ix_token_usage_free_group_created", "free_incentive_group", "created_at"
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "session_id": str(self.session_id) if self.session_id else None,
            "user_id": self.user_id,
            "project_id": str(self.project_id) if self.project_id else None,
            "provider": self.provider,
            "model": self.model,
            "agent_name": self.agent_name,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "prompt_eval_tokens": self.prompt_eval_tokens,
            "prompt_eval_ms": self.prompt_eval_ms,
            "cache_hit_rate": self.cache_hit_rate,
            "cache_evictions": self.cache_evictions,
            "cache_provider": self.cache_provider,
            "cache_mode": self.cache_mode,
            "cache_key": self.cache_key,
            "cache_supported": self.cache_supported,
            "cache_active": self.cache_active,
            "metrics_source": self.metrics_source,
            "input_cost": self.input_cost,
            "output_cost": self.output_cost,
            "total_cost": self.total_cost,
            "requested_model": self.requested_model,
            "resolved_model": self.resolved_model,
            "billing_scope_id": self.billing_scope_id,
            "pricing_status": self.pricing_status,
            "pricing_catalog_version": self.pricing_catalog_version,
            "pricing_rule_id": self.pricing_rule_id,
            "free_incentive_group": self.free_incentive_group,
            "applied_input_rate": _num_str(self.applied_input_rate),
            "applied_cached_input_rate": _num_str(self.applied_cached_input_rate),
            "applied_cache_write_rate": _num_str(self.applied_cache_write_rate),
            "applied_output_rate": _num_str(self.applied_output_rate),
            "list_input_cost": _num_str(self.list_input_cost),
            "list_output_cost": _num_str(self.list_output_cost),
            "list_tool_cost": _num_str(self.list_tool_cost),
            "list_total_cost": _num_str(self.list_total_cost),
            "provider_reported_cost": _num_str(self.provider_reported_cost),
            "provider_reported_cost_details": self.provider_reported_cost_details,
            "tool_invocations": self.tool_invocations,
            "request_type": self.request_type,
            "latency_ms": self.latency_ms,
            "is_streaming": self.is_streaming,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ModelPricing(Base):
    """モデル別料金テーブル"""

    __tablename__ = "model_pricing"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider = Column(String(50), nullable=False)
    model = Column(String(100), nullable=False)
    input_price_per_1m = Column(Float, nullable=False)  # USD per 1M tokens
    output_price_per_1m = Column(Float, nullable=False)
    cached_input_price_per_1m = Column(Float, default=0.0)
    cache_write_input_price_per_1m = Column(Float, default=0.0)
    currency = Column(String(3), default="USD")
    effective_from = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (Index("ix_model_pricing_provider_model", "provider", "model"),)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "provider": self.provider,
            "model": self.model,
            "input_price_per_1m": self.input_price_per_1m,
            "output_price_per_1m": self.output_price_per_1m,
            "cached_input_price_per_1m": self.cached_input_price_per_1m,
            "cache_write_input_price_per_1m": self.cache_write_input_price_per_1m,
            "effective_from": (
                self.effective_from.isoformat() if self.effective_from else None
            ),
        }


class PricingRule(Base):
    """料金カタログの単価ルール（有効期間付き履歴テーブル）

    ``(provider, canonical_model, effective_from)`` が一意。料金改定時は過去行を
    書き換えず、直前行の ``effective_to`` を閉じて新規行を追加する。
    """

    __tablename__ = "pricing_rules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rule_id = Column(String(200), nullable=False)
    provider = Column(String(50), nullable=False)
    canonical_model = Column(String(200), nullable=False)
    # flat_token | tiered_token | provider_reported | subscription | local
    pricing_kind = Column(String(30), nullable=False)

    # 単価（USD per 1M tokens）
    input_price_per_1m = Column(Numeric(18, 8), nullable=True)
    cached_input_price_per_1m = Column(Numeric(18, 8), nullable=True)
    cache_write_price_per_1m = Column(Numeric(18, 8), nullable=True)
    output_price_per_1m = Column(Numeric(18, 8), nullable=True)

    # 長文倍率
    long_context_threshold = Column(Integer, nullable=True)
    long_context_input_multiplier = Column(Numeric(10, 4), nullable=True)
    long_context_output_multiplier = Column(Numeric(10, 4), nullable=True)

    tiers = Column(JSONB, nullable=True)  # tiered_token 用
    tool_rates = Column(JSONB, nullable=True)  # {"web_search": "0.004"}

    currency = Column(String(3), nullable=False, default="USD")
    effective_from = Column(DateTime, nullable=False)
    effective_to = Column(DateTime, nullable=True)  # NULL = 現行
    source = Column(String(300), nullable=True)
    catalog_version = Column(String(50), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "canonical_model",
            "effective_from",
            name="uq_pricing_rules_scope",
        ),
        Index(
            "ix_pricing_rules_lookup",
            "provider",
            "canonical_model",
            "effective_from",
            "effective_to",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "rule_id": self.rule_id,
            "provider": self.provider,
            "canonical_model": self.canonical_model,
            "pricing_kind": self.pricing_kind,
            "input_price_per_1m": _num_str(self.input_price_per_1m),
            "cached_input_price_per_1m": _num_str(self.cached_input_price_per_1m),
            "cache_write_price_per_1m": _num_str(self.cache_write_price_per_1m),
            "output_price_per_1m": _num_str(self.output_price_per_1m),
            "long_context_threshold": self.long_context_threshold,
            "long_context_input_multiplier": _num_str(
                self.long_context_input_multiplier
            ),
            "long_context_output_multiplier": _num_str(
                self.long_context_output_multiplier
            ),
            "tiers": self.tiers,
            "tool_rates": self.tool_rates,
            "currency": self.currency,
            "effective_from": (
                self.effective_from.isoformat() if self.effective_from else None
            ),
            "effective_to": (
                self.effective_to.isoformat() if self.effective_to else None
            ),
            "source": self.source,
            "catalog_version": self.catalog_version,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class PricingModelAlias(Base):
    """料金ルールへのモデル名エイリアス（完全一致解決のみ。prefix一致は禁止）"""

    __tablename__ = "pricing_model_aliases"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rule_uuid = Column(UUID(as_uuid=True), nullable=False)  # pricing_rules.id
    provider = Column(String(50), nullable=False)
    alias = Column(String(200), nullable=False)  # 小文字正規化済み
    canonical_model = Column(String(200), nullable=False)
    effective_from = Column(DateTime, nullable=False)
    effective_to = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "provider", "alias", "effective_from", name="uq_pricing_alias_scope"
        ),
        Index("ix_pricing_alias_lookup", "provider", "alias"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "rule_uuid": str(self.rule_uuid) if self.rule_uuid else None,
            "provider": self.provider,
            "alias": self.alias,
            "canonical_model": self.canonical_model,
            "effective_from": (
                self.effective_from.isoformat() if self.effective_from else None
            ),
            "effective_to": (
                self.effective_to.isoformat() if self.effective_to else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class PricingCatalogState(Base):
    """料金カタログ取り込み元ごとの同期状態（last-known-good 管理）"""

    __tablename__ = "pricing_catalog_state"

    # "catalog_file" | "openrouter" | "manual_import"
    source_key = Column(String(50), primary_key=True)
    catalog_version = Column(String(50), nullable=True)
    rule_count = Column(Integer, nullable=False, default=0)
    last_attempt_at = Column(DateTime, nullable=True)
    last_success_at = Column(DateTime, nullable=True)
    last_status = Column(String(20), nullable=True)  # "ok" | "error" | "skipped"
    last_error = Column(Text, nullable=True)
    payload_digest = Column(String(64), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_key": self.source_key,
            "catalog_version": self.catalog_version,
            "rule_count": self.rule_count,
            "last_attempt_at": (
                self.last_attempt_at.isoformat() if self.last_attempt_at else None
            ),
            "last_success_at": (
                self.last_success_at.isoformat() if self.last_success_at else None
            ),
            "last_status": self.last_status,
            "last_error": self.last_error,
            "payload_digest": self.payload_digest,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ────────────────────────────────────────────
# 4. スキル拡張
# ────────────────────────────────────────────


class SkillCategory(Base):
    """スキルカテゴリ"""

    __tablename__ = "skill_categories"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False, unique=True)
    display_name = Column(String(200), nullable=False)
    description = Column(Text, default="")
    icon = Column(String(10), default="")
    color = Column(String(20), default="")
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "icon": self.icon,
            "color": self.color,
            "sort_order": self.sort_order,
        }


class SkillPreset(Base):
    """プリセットスキルライブラリ"""

    __tablename__ = "skill_presets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False, unique=True)
    display_name = Column(String(200), nullable=False)
    description = Column(Text, default="")
    category = Column(String(100), default="general")
    prompt_template = Column(Text, nullable=False)
    trigger_mode = Column(String(20), default="manual")
    aliases = Column(JSON, default=list)
    bound_tools = Column(JSON, default=list)
    tags = Column(JSON, default=list)
    examples = Column(JSON, default=list)
    parameters = Column(JSON, default=dict)
    is_builtin = Column(Boolean, default=True)  # 組み込みプリセットかユーザー共有か
    install_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "category": self.category,
            "prompt_template": self.prompt_template,
            "trigger_mode": self.trigger_mode,
            "aliases": self.aliases or [],
            "bound_tools": self.bound_tools or [],
            "tags": self.tags or [],
            "examples": self.examples or [],
            "parameters": self.parameters or {},
            "is_builtin": self.is_builtin,
            "install_count": self.install_count,
        }


class SkillChain(Base):
    """スキルチェーン定義"""

    __tablename__ = "skill_chains"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False, unique=True)
    display_name = Column(String(200), nullable=False)
    description = Column(Text, default="")
    # 実行するスキルの順序リスト
    steps = Column(JSON, nullable=False)
    # [{"skill_name": "...", "input_mapping": {"param": "$prev.output"}, "on_error": "skip|abort"}]
    created_by = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "steps": self.steps or [],
            "created_by": str(self.created_by) if self.created_by else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ────────────────────────────────────────────
# 6. ワークフローエンジン — .mdファイルベースに移行済み（DBテーブル廃止）


# ────────────────────────────────────────────
# 10. ワールドブック（ロアブック / World Info）
# ────────────────────────────────────────────


class WorldBook(Base):
    """複数キャラクターで共有可能なワールドブック"""

    __tablename__ = "world_books"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scenario_id = Column(
        UUID(as_uuid=True),
        nullable=True,
    )
    name = Column(String(200), nullable=False)
    description = Column(Text, default="")
    is_enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    entries = relationship(
        "WorldBookEntry", back_populates="world_book", cascade="all, delete-orphan"
    )
    character_links = relationship(
        "CharacterWorldBook", back_populates="world_book", cascade="all, delete-orphan"
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "scenario_id": str(self.scenario_id) if self.scenario_id else None,
            "name": self.name,
            "description": self.description or "",
            "is_enabled": self.is_enabled,
            "entry_count": len(self.entries) if self.entries else 0,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class WorldBookEntry(Base):
    """ワールドブック内のエントリ（キーワードトリガーで動的にコンテキスト注入）"""

    __tablename__ = "world_book_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    world_book_id = Column(
        UUID(as_uuid=True),
        ForeignKey("world_books.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(200), default="")
    keywords = Column(JSON, default=list)  # トリガーキーワード
    secondary_keywords = Column(JSON, default=list)  # セカンダリトリガー
    content = Column(Text, nullable=False)  # 注入されるテキスト
    is_enabled = Column(Boolean, default=True)
    priority = Column(Integer, default=0)  # 高いほど優先
    case_sensitive = Column(Boolean, default=False)
    constant = Column(Boolean, default=False)  # 常時挿入
    insertion_position = Column(String(20), default="before_scenario")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    world_book = relationship("WorldBook", back_populates="entries")

    __table_args__ = (Index("ix_world_book_entries_world_book_id", "world_book_id"),)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "world_book_id": str(self.world_book_id),
            "name": self.name or "",
            "keywords": self.keywords or [],
            "secondary_keywords": self.secondary_keywords or [],
            "content": self.content,
            "is_enabled": self.is_enabled,
            "priority": self.priority,
            "case_sensitive": self.case_sensitive,
            "constant": self.constant,
            "insertion_position": self.insertion_position or "before_scenario",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CharacterWorldBook(Base):
    """キャラクターとワールドブックの多対多リンク"""

    __tablename__ = "character_world_books"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    character_id = Column(
        UUID(as_uuid=True),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
    )
    world_book_id = Column(
        UUID(as_uuid=True),
        ForeignKey("world_books.id", ondelete="CASCADE"),
        nullable=False,
    )

    world_book = relationship("WorldBook", back_populates="character_links")

    __table_args__ = (Index("ix_character_world_books_character_id", "character_id"),)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "character_id": str(self.character_id),
            "world_book_id": str(self.world_book_id),
        }




configure_mappers()
