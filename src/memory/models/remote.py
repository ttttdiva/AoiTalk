"""外部AoiTalkサーバー接続プロファイルのモデル。

個人版が登録する外部AoiTalkサーバー（会社版など）の接続情報を保持する。
認証トークンは既存のフィールド暗号化基盤で暗号化して保存し、取得したリモート
データ自体は永続化しない（コネクタ側の短TTLメモリキャッシュのみ）。
"""

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import (
    Column,
    String,
    Text,
    DateTime,
    JSON,
    ForeignKey,
    Boolean,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from ...security.field_crypto import decrypt_text_if_needed
from .base import Base


class RemoteServerProfile(Base):
    """外部AoiTalkサーバーへの接続プロファイル（ユーザー単位）。"""

    __tablename__ = "remote_server_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(200), nullable=False)
    base_url = Column(String(500), nullable=False)
    # 認証トークン（長期APIトークン）の暗号文。aadは user_id でバインドする。
    auth_token = Column(Text, nullable=True)
    display_color = Column(String(32), nullable=True)
    enabled = Column(Boolean, default=True, nullable=False)
    # 最後の接続テスト結果
    last_status = Column(String(32), nullable=True)
    last_checked_at = Column(DateTime, nullable=True)
    last_capabilities = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    user = relationship("User", backref="remote_server_profiles")

    __table_args__ = (
        UniqueConstraint(
            "user_id", "base_url", name="uq_remote_server_profiles_user_base_url"
        ),
    )

    def _token_aad(self) -> str:
        return f"remote_server_profiles.auth_token:{self.user_id}"

    def get_auth_token(self) -> str:
        """復号した認証トークンを返す。"""
        return decrypt_text_if_needed(self.auth_token, aad=self._token_aad())

    def to_dict(self, include_token: bool = False) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "name": self.name,
            "base_url": self.base_url,
            "display_color": self.display_color,
            "enabled": self.enabled,
            "has_token": bool(self.auth_token),
            "last_status": self.last_status,
            "last_checked_at": (
                self.last_checked_at.isoformat() if self.last_checked_at else None
            ),
            "last_capabilities": self.last_capabilities,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_token:
            data["auth_token"] = self.get_auth_token()
        return data
