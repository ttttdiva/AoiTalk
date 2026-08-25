"""生成メディアの永続モデル。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import Column, DateTime, Integer, String, Text, Index
from sqlalchemy.dialects.postgresql import JSON, UUID

from .base import Base


class GeneratedMedia(Base):
    """会話・Story 等で生成された画像の永続メタデータ。"""

    __tablename__ = "generated_media"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(String, nullable=False, index=True)
    context_type = Column(String(32), nullable=False)
    context_id = Column(String, nullable=False, index=True)
    bind_type = Column(String(32), nullable=True)
    bind_id = Column(String, nullable=True, index=True)
    storage_key = Column(String(64), nullable=False, unique=True)
    mime_type = Column(String(120), nullable=False, default="image/png")
    byte_size = Column(Integer, nullable=True)
    relative_path = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default="pending")
    error_message = Column(Text, nullable=True)
    prompt_meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index(
            "ix_generated_media_context",
            "context_type",
            "context_id",
            "created_at",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "owner_user_id": self.owner_user_id,
            "context_type": self.context_type,
            "context_id": self.context_id,
            "bind_type": self.bind_type,
            "bind_id": self.bind_id,
            "storage_key": self.storage_key,
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
            "relative_path": self.relative_path,
            "status": self.status,
            "error_message": self.error_message,
            "prompt_meta": self.prompt_meta or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
