"""TTS 共通読み辞書と誤読候補の永続モデル。"""

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


class YomiDictionaryEntry(Base):
    """利用者が明示した表記と読みの対応。モデル推測値は保存しない。"""

    __tablename__ = "yomi_dictionary_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    surface = Column(String(255), nullable=False, index=True)
    reading = Column(String(255), nullable=False)
    accent_type = Column(Integer, nullable=True)
    enabled = Column(Boolean, default=True, nullable=False, index=True)
    target_tts = Column(JSON, default=list, nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "surface": self.surface,
            "reading": self.reading,
            "accent_type": self.accent_type,
            "enabled": self.enabled,
            "target_tts": list(self.target_tts or []),
            "notes": self.notes or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class YomiUnresolvedCandidate(Base):
    """読みを推測せず、利用者確認待ちとして保持する検出結果。"""

    __tablename__ = "yomi_unresolved_candidates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    original_text = Column(Text, nullable=False)
    detected_text = Column(String(255), nullable=False, index=True)
    start_offset = Column(Integer, nullable=False)
    end_offset = Column(Integer, nullable=False)
    confidence = Column(Float, nullable=False)
    model_id = Column(String(255), nullable=False)
    tts_engine = Column(String(64), nullable=False, index=True)
    dictionary_applied = Column(Boolean, default=False, nullable=False)
    final_text = Column(Text, nullable=False)
    status = Column(String(24), default="unresolved", nullable=False, index=True)
    occurrence_count = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "original_text": self.original_text,
            "detected_text": self.detected_text,
            "start": self.start_offset,
            "end": self.end_offset,
            "confidence": self.confidence,
            "model_id": self.model_id,
            "tts_engine": self.tts_engine,
            "dictionary_applied": self.dictionary_applied,
            "final_text": self.final_text,
            "status": self.status,
            "occurrence_count": self.occurrence_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class YomiDictionarySync(Base):
    """AoiTalk自身が外部TTS辞書へ追加した語だけを追跡する台帳。"""

    __tablename__ = "yomi_dictionary_syncs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dictionary_entry_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    tts_engine = Column(String(64), nullable=False, index=True)
    base_url = Column(String(500), nullable=False)
    remote_word_uuid = Column(String(64), nullable=False)
    surface = Column(String(255), nullable=False)
    reading = Column(String(255), nullable=False)
    accent_type = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "dictionary_entry_id": str(self.dictionary_entry_id),
            "tts_engine": self.tts_engine,
            "base_url": self.base_url,
            "remote_word_uuid": self.remote_word_uuid,
            "surface": self.surface,
            "reading": self.reading,
            "accent_type": self.accent_type,
        }
