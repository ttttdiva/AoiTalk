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
    JSON,
    ForeignKey,
    Boolean,
    Index,
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import UUID
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
# 1c. シナリオ（TRPG / インタラクティブストーリー）
# ────────────────────────────────────────────


class Scenario(Base):
    """シナリオ定義。

    scenario_kind="writing" は小説/脚本向けの episode/scene 構造、
    scenario_kind="trpg" は TRPG 卓向けの ruleset/document/character sheet 構造を使う。
    """

    __tablename__ = "scenarios"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(200), nullable=False)
    scenario_kind = Column(String(20), default="writing")
    ruleset = Column(String(50), default="")
    description = Column(Text, default="")
    genre = Column(String(50), default="")
    perspective = Column(
        String(20), default="first_person"
    )  # first_person / third_person
    setting = Column(Text, default="")  # 世界観・舞台設定
    opening_text = Column(Text, default="")  # 冒頭ナレーション
    gm_instructions = Column(Text, default="")  # GM用指示
    tags = Column(JSON, default=list)
    cover_image_path = Column(String(500), default="")
    is_published = Column(Boolean, default=False)
    created_by = Column(UUID(as_uuid=True), nullable=True)

    # 執筆ボイス設定
    voice_tone = Column(Text, default="")
    voice_tense_rules = Column(Text, default="")
    voice_vocabulary_register = Column(Text, default="")
    voice_banned_expressions = Column(JSON, default=list)
    voice_example_passages = Column(Text, default="")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    characters = relationship(
        "ScenarioCharacter", back_populates="scenario", cascade="all, delete-orphan"
    )
    scenes = relationship(
        "ScenarioScene", back_populates="scenario", cascade="all, delete-orphan"
    )
    episodes = relationship(
        "ScenarioEpisode", back_populates="scenario", cascade="all, delete-orphan"
    )
    trpg_documents = relationship(
        "TRPGScenarioDocument", back_populates="scenario", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_scenarios_genre", "genre"),
        Index("ix_scenarios_is_published", "is_published"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "title": self.title,
            "scenario_kind": self.scenario_kind or "writing",
            "ruleset": self.ruleset or "",
            "description": self.description,
            "genre": self.genre,
            "perspective": self.perspective,
            "setting": self.setting,
            "opening_text": self.opening_text,
            "gm_instructions": self.gm_instructions,
            "tags": self.tags or [],
            "cover_image_path": self.cover_image_path or "",
            "is_published": self.is_published,
            "created_by": str(self.created_by) if self.created_by else None,
            # 執筆ボイス設定
            "voice_tone": self.voice_tone or "",
            "voice_tense_rules": self.voice_tense_rules or "",
            "voice_vocabulary_register": self.voice_vocabulary_register or "",
            "voice_banned_expressions": self.voice_banned_expressions or [],
            "voice_example_passages": self.voice_example_passages or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ScenarioCharacter(Base):
    """シナリオ内のキャラクター配置"""

    __tablename__ = "scenario_characters"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scenario_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenarios.id", ondelete="CASCADE"),
        nullable=False,
    )
    character_id = Column(
        UUID(as_uuid=True),
        ForeignKey("characters.id", ondelete="SET NULL"),
        nullable=True,
    )
    role = Column(String(20), default="npc")  # npc, ally, enemy, narrator
    name = Column(String(100), nullable=False)
    description = Column(Text, default="")
    personality_override = Column(Text, default="")  # シナリオ固有の性格上書き
    appearance_tags_override = Column(Text, default="")  # シナリオ固有の外見タグ上書き
    sort_order = Column(Integer, default=0)

    # 執筆拡張フィールド
    backstory = Column(Text, default="")
    psychology = Column(Text, default="")
    speech_patterns = Column(Text, default="")
    relationships = Column(JSON, default=list)  # [{target_id, type, description}]
    character_arc = Column(Text, default="")
    importance = Column(Integer, default=0)  # 0=major, 1=secondary, 2=minor
    example_dialogues = Column(Text, default="")
    trpg_ruleset = Column(String(50), default="")
    trpg_pc_state = Column(JSON, default=dict)

    scenario = relationship("Scenario", back_populates="characters")

    __table_args__ = (Index("ix_scenario_characters_scenario_id", "scenario_id"),)

    def to_dict(self) -> Dict[str, Any]:
        relationships = self.relationships or []
        relationships_text = (
            relationships
            if isinstance(relationships, str)
            else json.dumps(relationships, ensure_ascii=False)
        )
        return {
            "id": str(self.id),
            "scenario_id": str(self.scenario_id),
            "character_id": str(self.character_id) if self.character_id else None,
            "role": self.role,
            "name": self.name,
            "description": self.description,
            "personality_override": self.personality_override or "",
            "appearance_tags_override": self.appearance_tags_override or "",
            "sort_order": self.sort_order,
            "backstory": self.backstory or "",
            "psychology": self.psychology or "",
            "speech_patterns": self.speech_patterns or "",
            "speech_pattern": self.speech_patterns or "",
            "relationships": relationships_text,
            "relationships_data": relationships,
            "character_arc": self.character_arc or "",
            "arc": self.character_arc or "",
            "importance": self.importance or 0,
            "example_dialogues": self.example_dialogues or "",
            "dialogue_samples": self.example_dialogues or "",
            "trpg_ruleset": self.trpg_ruleset or "",
            "trpg_pc_state": self.trpg_pc_state or {},
        }


class TRPGPlayerCharacterSheet(Base):
    """ユーザー所有のTRPGプレイヤーキャラクター保存シート。

    シナリオ定義側のNPC/敵/プリセットキャラとは分け、卓参加時に使う
    ユーザーPCの再利用データを保持する。
    """

    __tablename__ = "trpg_player_character_sheets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scenario_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenarios.id", ondelete="CASCADE"),
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


class TRPGScenarioDocument(Base):
    """完成済みTRPGシナリオ本文・構造化資料。

    小説/脚本用の episode/scene とは分ける。source_text はインポート本文の
    保全用で、卓実行時は structure と ScenarioCharacter に展開済みの情報を使う。
    """

    __tablename__ = "trpg_scenario_documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scenario_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenarios.id", ondelete="CASCADE"),
        nullable=False,
    )
    ruleset = Column(String(50), default="")
    source_label = Column(Text, default="")
    source_text = Column(Text, default="")
    structure = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    scenario = relationship("Scenario", back_populates="trpg_documents")

    __table_args__ = (
        Index("ix_trpg_scenario_documents_scenario_id", "scenario_id"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "scenario_id": str(self.scenario_id),
            "ruleset": self.ruleset or "",
            "source_label": self.source_label or "",
            "source_text": self.source_text or "",
            "structure": self.structure or {},
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


class ScenarioScene(Base):
    """シナリオ内のシーン定義"""

    __tablename__ = "scenario_scenes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scenario_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenarios.id", ondelete="CASCADE"),
        nullable=False,
    )
    episode_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenario_episodes.id", ondelete="SET NULL"),
        nullable=True,
    )
    title = Column(String(200), nullable=False)
    description = Column(Text, default="")
    scene_type = Column(
        String(20), default="normal"
    )  # normal, combat, dialogue, cutscene
    gm_instructions = Column(Text, default="")
    image_prompt = Column(Text, default="")  # シーン画像生成用プロンプト
    transitions = Column(JSON, default=list)  # [{condition, target_scene_id}]
    sort_order = Column(Integer, default=0)

    # 執筆拡張フィールド
    content = Column(Text, default="")  # 本文テキスト
    content_versions = Column(JSON, default=list)  # [{version, content, created_at}]
    word_count = Column(Integer, default=0)
    status = Column(String(20), default="draft")  # draft/in_progress/completed
    state_snapshot = Column(
        JSON, default=dict
    )  # {situation, character_states, knowledge}

    scenario = relationship("Scenario", back_populates="scenes")
    episode = relationship("ScenarioEpisode", back_populates="scenes")

    __table_args__ = (Index("ix_scenario_scenes_scenario_id", "scenario_id"),)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "scenario_id": str(self.scenario_id),
            "episode_id": str(self.episode_id) if self.episode_id else None,
            "title": self.title,
            "description": self.description,
            "scene_type": self.scene_type,
            "gm_instructions": self.gm_instructions,
            "image_prompt": self.image_prompt or "",
            "transitions": self.transitions or [],
            "sort_order": self.sort_order,
            "order_index": self.sort_order,
            "content": self.content or "",
            "body": self.content or "",
            "content_versions": self.content_versions or [],
            "word_count": self.word_count or 0,
            "status": self.status or "draft",
            "state_snapshot": self.state_snapshot or {},
        }


class ScenarioPlaySession(Base):
    """シナリオプレイセッション（マルチプレイヤー対応）"""

    __tablename__ = "scenario_play_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scenario_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenarios.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_session_id = Column(UUID(as_uuid=True), nullable=True)
    current_scene_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenario_scenes.id", ondelete="SET NULL"),
        nullable=True,
    )
    player_state = Column(JSON, default=dict)  # パーティ共有: inventory, flags, etc.
    perspective = Column(String(20), default="first_person")
    status = Column(String(20), default="in_progress")  # in_progress, paused, completed
    started_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ── マルチプレイヤーTRPG 拡張 ──
    room_code = Column(String(12), nullable=True, unique=True)  # 入室招待コード
    room_title = Column(
        String(200), default=""
    )  # ルーム表示名（未設定ならシナリオ名を使う）
    host_user_id = Column(UUID(as_uuid=True), nullable=True)  # セッション主催者
    max_players = Column(Integer, default=4)
    gm_mode = Column(String(20), default="ai")  # "ai" | "human"
    gm_user_id = Column(UUID(as_uuid=True), nullable=True)  # 人間GMの場合
    is_multiplayer = Column(Boolean, default=False)
    is_public = Column(Boolean, default=False)  # 一覧に公開するか
    turn_order = Column(JSON, default=list)  # [participant_id, ...]
    current_turn_participant_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenario_participants.id", ondelete="SET NULL"),
        nullable=True,
    )
    shared_state = Column(JSON, default=dict)  # 天候/時刻/全体フラグ/BGM情報
    last_gm_activity_at = Column(DateTime, nullable=True)

    participants = relationship(
        "ScenarioParticipant",
        back_populates="play_session",
        cascade="all, delete-orphan",
        foreign_keys="ScenarioParticipant.play_session_id",
    )
    logs = relationship(
        "ScenarioPlayLog",
        back_populates="play_session",
        cascade="all, delete-orphan",
        order_by="ScenarioPlayLog.created_at",
    )
    disclosures = relationship(
        "TRPGRoomDisclosure",
        back_populates="play_session",
        cascade="all, delete-orphan",
        order_by="TRPGRoomDisclosure.created_at",
    )
    private_messages = relationship(
        "TRPGPrivateMessage",
        back_populates="play_session",
        cascade="all, delete-orphan",
        order_by="TRPGPrivateMessage.created_at",
    )

    __table_args__ = (
        Index("ix_scenario_play_sessions_scenario_id", "scenario_id"),
        Index("ix_scenario_play_sessions_status", "status"),
        Index("ix_scenario_play_sessions_room_code", "room_code"),
        Index("ix_scenario_play_sessions_host_user_id", "host_user_id"),
    )

    def to_dict(self, include_children: bool = False) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": str(self.id),
            "scenario_id": str(self.scenario_id),
            "conversation_session_id": (
                str(self.conversation_session_id)
                if self.conversation_session_id
                else None
            ),
            "current_scene_id": (
                str(self.current_scene_id) if self.current_scene_id else None
            ),
            "player_state": self.player_state or {},
            "perspective": self.perspective,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "room_code": self.room_code,
            "room_title": self.room_title or "",
            "host_user_id": (str(self.host_user_id) if self.host_user_id else None),
            "max_players": self.max_players or 4,
            "gm_mode": self.gm_mode or "ai",
            "gm_user_id": str(self.gm_user_id) if self.gm_user_id else None,
            "is_multiplayer": bool(self.is_multiplayer),
            "is_public": bool(self.is_public),
            "turn_order": self.turn_order or [],
            "current_turn_participant_id": (
                str(self.current_turn_participant_id)
                if self.current_turn_participant_id
                else None
            ),
            "shared_state": self.shared_state or {},
            "last_gm_activity_at": (
                self.last_gm_activity_at.isoformat()
                if self.last_gm_activity_at
                else None
            ),
        }
        if include_children:
            data["participants"] = [
                p.to_dict()
                for p in sorted(
                    self.participants or [],
                    key=lambda x: (x.seat_index or 0),
                )
            ]
            data["logs"] = [log.to_dict() for log in (self.logs or [])]
        return data


class ScenarioParticipant(Base):
    """プレイセッションの参加者（ユーザー / AIキャラ / GM）"""

    __tablename__ = "scenario_participants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    play_session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenario_play_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(UUID(as_uuid=True), nullable=True)  # AI席は null
    character_id = Column(
        UUID(as_uuid=True),
        ForeignKey("characters.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 参加者の表示用情報
    display_name = Column(String(100), nullable=False)
    role = Column(String(20), default="player")
    # "player" | "gm" | "npc" | "observer"
    participant_kind = Column(String(20), default="human")
    # "human" | "ai_character" | "system"
    avatar_url = Column(String(500), default="")
    color = Column(String(20), default="#60a5fa")  # パネル色
    seat_index = Column(Integer, default=0)

    # PC（プレイヤーキャラクター）状態
    pc_state = Column(JSON, default=dict)
    # {hp, max_hp, mp, max_mp, stats:{str,dex,int,...}, conditions:[], notes, items:[]}

    # AI NPC専用の非公開状態。to_dict には含めず、公開APIへ漏らさない。
    private_state = Column(JSON, default=dict)
    last_observed_log_id = Column(UUID(as_uuid=True), nullable=True)

    is_active_participant = Column(Boolean, default=True)
    is_connected = Column(Boolean, default=False)  # WebSocket 接続中か
    joined_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow)

    play_session = relationship(
        "ScenarioPlaySession",
        back_populates="participants",
        foreign_keys=[play_session_id],
    )

    __table_args__ = (
        Index("ix_scenario_participants_play_session_id", "play_session_id"),
        Index("ix_scenario_participants_user_id", "user_id"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "play_session_id": str(self.play_session_id),
            "user_id": str(self.user_id) if self.user_id else None,
            "character_id": (str(self.character_id) if self.character_id else None),
            "display_name": self.display_name,
            "role": self.role or "player",
            "participant_kind": self.participant_kind or "human",
            "avatar_url": self.avatar_url or "",
            "color": self.color or "#60a5fa",
            "seat_index": self.seat_index or 0,
            "pc_state": self.pc_state or {},
            "is_active_participant": bool(self.is_active_participant),
            "is_connected": bool(self.is_connected),
            "joined_at": self.joined_at.isoformat() if self.joined_at else None,
            "last_seen_at": (
                self.last_seen_at.isoformat() if self.last_seen_at else None
            ),
        }


class ScenarioPlayLog(Base):
    """プレイセッションのログ（ナレーション・行動・ダイス・シーン変化など）"""

    __tablename__ = "scenario_play_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    play_session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenario_play_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    participant_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenario_participants.id", ondelete="SET NULL"),
        nullable=True,
    )
    log_type = Column(String(20), nullable=False)
    # "narration" | "speech" | "action" | "dice" | "scene_change"
    # | "system" | "image" | "bgm" | "state_change" | "ooc"
    content = Column(Text, default="")
    log_metadata = Column(JSON, default=dict)
    # dice: {expression, rolls, total, target, success}
    # scene_change: {from_scene_id, to_scene_id, title}
    # image: {prompt, path}
    # bgm: {track_id, action}
    # state_change: {participant_id?, key, before, after}
    created_at = Column(DateTime, default=datetime.utcnow)

    play_session = relationship("ScenarioPlaySession", back_populates="logs")

    __table_args__ = (
        Index("ix_scenario_play_logs_play_session_id", "play_session_id"),
        Index("ix_scenario_play_logs_created_at", "created_at"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "play_session_id": str(self.play_session_id),
            "participant_id": (
                str(self.participant_id) if self.participant_id else None
            ),
            "log_type": self.log_type,
            "content": self.content or "",
            "metadata": self.log_metadata or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class TRPGRoomDisclosure(Base):
    """TRPG卓の開示情報・ハンドアウト・画像・アイテム."""

    __tablename__ = "trpg_room_disclosures"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    play_session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenario_play_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    creator_participant_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenario_participants.id", ondelete="SET NULL"),
        nullable=True,
    )
    disclosure_type = Column(String(30), default="handout")
    visibility = Column(String(20), default="public")
    target_participant_ids = Column(JSON, default=list)
    title = Column(String(200), nullable=False)
    content = Column(Text, default="")
    image_url = Column(String(1000), default="")
    image_path = Column(String(1000), default="")
    tags = Column(JSON, default=list)
    disclosure_metadata = Column(JSON, default=dict)
    is_pinned = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    play_session = relationship("ScenarioPlaySession", back_populates="disclosures")

    __table_args__ = (
        Index("ix_trpg_room_disclosures_play_session_id", "play_session_id"),
        Index("ix_trpg_room_disclosures_visibility", "visibility"),
        Index("ix_trpg_room_disclosures_created_at", "created_at"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "play_session_id": str(self.play_session_id),
            "creator_participant_id": (
                str(self.creator_participant_id) if self.creator_participant_id else None
            ),
            "disclosure_type": self.disclosure_type or "handout",
            "visibility": self.visibility or "public",
            "target_participant_ids": self.target_participant_ids or [],
            "title": self.title,
            "content": self.content or "",
            "image_url": self.image_url or "",
            "image_path": self.image_path or "",
            "tags": self.tags or [],
            "metadata": self.disclosure_metadata or {},
            "is_pinned": bool(self.is_pinned),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TRPGPrivateMessage(Base):
    """TRPG卓の秘匿/個別チャット."""

    __tablename__ = "trpg_private_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    play_session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenario_play_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    sender_participant_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenario_participants.id", ondelete="SET NULL"),
        nullable=True,
    )
    sender_label = Column(String(120), default="")
    target_participant_ids = Column(JSON, default=list)
    message_type = Column(String(20), default="private")
    content = Column(Text, default="")
    message_metadata = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)

    play_session = relationship("ScenarioPlaySession", back_populates="private_messages")

    __table_args__ = (
        Index("ix_trpg_private_messages_play_session_id", "play_session_id"),
        Index("ix_trpg_private_messages_created_at", "created_at"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "play_session_id": str(self.play_session_id),
            "sender_participant_id": (
                str(self.sender_participant_id) if self.sender_participant_id else None
            ),
            "sender_label": self.sender_label or "",
            "target_participant_ids": self.target_participant_ids or [],
            "message_type": self.message_type or "private",
            "content": self.content or "",
            "metadata": self.message_metadata or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ScenarioEpisode(Base):
    """シナリオ内の章/エピソード"""

    __tablename__ = "scenario_episodes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scenario_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenarios.id", ondelete="CASCADE"),
        nullable=False,
    )
    title = Column(String(200), nullable=False)
    synopsis_sentence = Column(Text, default="")  # 1文要約
    synopsis_paragraph = Column(Text, default="")  # 段落要約
    synopsis_full = Column(Text, default="")  # 完全要約
    beat_sheet = Column(JSON, default=list)  # ビート一覧
    status = Column(String(20), default="draft")  # draft/in_progress/completed
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    scenario = relationship("Scenario", back_populates="episodes")
    scenes = relationship("ScenarioScene", back_populates="episode")

    __table_args__ = (Index("ix_scenario_episodes_scenario_id", "scenario_id"),)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "scenario_id": str(self.scenario_id),
            "title": self.title,
            "synopsis_sentence": self.synopsis_sentence or "",
            "one_line_summary": self.synopsis_sentence or "",
            "synopsis_paragraph": self.synopsis_paragraph or "",
            "paragraph_summary": self.synopsis_paragraph or "",
            "synopsis_full": self.synopsis_full or "",
            "full_summary": self.synopsis_full or "",
            "beat_sheet": self.beat_sheet or [],
            "status": self.status or "draft",
            "sort_order": self.sort_order or 0,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ScenarioCanonEntry(Base):
    """シナリオの確定事実DB"""

    __tablename__ = "scenario_canon_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scenario_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenarios.id", ondelete="CASCADE"),
        nullable=False,
    )
    category = Column(
        String(50), nullable=False
    )  # geography/timeline/magic/character_facts/political/cultural/established
    fact = Column(Text, nullable=False)
    source_scene_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenario_scenes.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (Index("ix_scenario_canon_entries_scenario_id", "scenario_id"),)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "scenario_id": str(self.scenario_id),
            "category": self.category,
            "fact": self.fact,
            "source_scene_id": (
                str(self.source_scene_id) if self.source_scene_id else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ScenarioWritingSession(Base):
    """シナリオ執筆セッション"""

    __tablename__ = "scenario_writing_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scenario_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenarios.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversation_sessions.id"),
        nullable=True,
    )
    target_episode_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenario_episodes.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_scene_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scenario_scenes.id", ondelete="SET NULL"),
        nullable=True,
    )
    writing_prompt = Column(Text, default="")
    status = Column(String(20), default="in_progress")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (Index("ix_scenario_writing_sessions_scenario_id", "scenario_id"),)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "scenario_id": str(self.scenario_id),
            "conversation_session_id": (
                str(self.conversation_session_id)
                if self.conversation_session_id
                else None
            ),
            "target_episode_id": (
                str(self.target_episode_id) if self.target_episode_id else None
            ),
            "target_scene_id": (
                str(self.target_scene_id) if self.target_scene_id else None
            ),
            "writing_prompt": self.writing_prompt or "",
            "status": self.status or "in_progress",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ────────────────────────────────────────────
# 2. イベント駆動自動化
# ────────────────────────────────────────────


class TokenUsage(Base):
    """LLM API呼び出しごとのトークン使用量"""

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

    # コスト（USD）
    input_cost = Column(Float, default=0.0)
    output_cost = Column(Float, default=0.0)
    total_cost = Column(Float, default=0.0)

    # メタデータ
    request_type = Column(String(50), default="chat")  # chat, embedding, tts, stt
    latency_ms = Column(Integer, default=0)
    is_streaming = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_token_usage_date_model", "created_at", "model"),
        Index("ix_token_usage_project_date", "project_id", "created_at"),
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
        ForeignKey("scenarios.id", ondelete="CASCADE"),
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
