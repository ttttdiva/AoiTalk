"""ユーザー・認証・ログ・ユーザー単位の設定/連携系モデル。"""

import uuid
from datetime import datetime
from typing import Any, Dict

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
    UniqueConstraint,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from ...security.field_crypto import decrypt_text_if_needed
from .base import Base


class User(Base):
    """User account for multi-user enterprise support"""

    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = Column(String(100), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=True, index=True)
    password_hash = Column(String(255), nullable=False)

    # Profile
    display_name = Column(String(100))
    preferred_character = Column(String(100))

    # Role & Status
    role = Column(String(20), default="user", index=True)  # 'admin', 'user'
    is_active = Column(Boolean, default=True, index=True)
    is_password_reset_required = Column(Boolean, default=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login = Column(DateTime)

    # Settings (JSON for flexibility)
    user_settings = Column(JSON, default=dict)

    def to_dict(self, include_sensitive: bool = False) -> Dict[str, Any]:
        """Convert to dictionary

        Args:
            include_sensitive: Include sensitive fields (password_hash)
        """
        result = {
            "id": str(self.id),
            "username": self.username,
            "email": self.email,
            "display_name": self.display_name,
            "preferred_character": self.preferred_character,
            "role": self.role,
            "is_active": self.is_active,
            "is_password_reset_required": self.is_password_reset_required,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_login": self.last_login.isoformat() if self.last_login else None,
            "settings": self.user_settings,
        }
        if include_sensitive:
            result["password_hash"] = self.password_hash
        return result


class LongLivedApiToken(Base):
    """Long-lived API token for server-to-server access.

    既存のアクセストークン+リフレッシュ方式とは別系統。設定画面から発行・失効し、
    外部AoiTalkサーバーからのAPIアクセスに使う。平文は発行時のみ返却し、
    保存はSHA-256ハッシュのみ（復号不可）。
    """

    __tablename__ = "long_lived_api_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(200), nullable=False)
    token_hash = Column(String(64), nullable=False)
    token_prefix = Column(String(24), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    revoked = Column(Boolean, default=False, nullable=False, index=True)

    user = relationship("User", backref="long_lived_api_tokens")

    __table_args__ = (
        UniqueConstraint(
            "token_hash", name="uq_long_lived_api_tokens_token_hash"
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "name": self.name,
            "token_prefix": self.token_prefix,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_used_at": (
                self.last_used_at.isoformat() if self.last_used_at else None
            ),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked": self.revoked,
        }


class WebUILoginLog(Base):
    """WebUI login/logout activity log for security and audit"""

    __tablename__ = "webui_login_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = Column(String, nullable=False, index=True)
    action = Column(String, nullable=False, index=True)  # 'login' or 'logout'
    ip_address = Column(String)
    user_agent = Column(Text)

    # Success/failure tracking
    success = Column(Boolean, default=True, index=True)
    failure_reason = Column(String)  # e.g., 'invalid_credentials', 'session_expired'

    # Session tracking
    session_duration_seconds = Column(Integer)  # Duration for logout events

    # Timing
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    # Additional metadata
    login_metadata = Column(JSON, default=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "username": self.username,
            "action": self.action,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "success": self.success,
            "failure_reason": self.failure_reason,
            "session_duration_seconds": self.session_duration_seconds,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "metadata": self.login_metadata,
        }


class AppConfigSetting(Base):
    """Global application configuration stored as JSON."""

    __tablename__ = "app_config_settings"

    key = Column(String(100), primary_key=True)
    value = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class FileExplorerBookmark(Base):
    """Per-user file explorer bookmark."""

    __tablename__ = "file_explorer_bookmarks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(200), nullable=False)
    path = Column(Text, nullable=False)
    icon = Column(String(64), nullable=True)
    sort_order = Column(Float, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    user = relationship("User", backref="file_explorer_bookmarks")

    __table_args__ = (
        UniqueConstraint("user_id", "path", name="unique_file_explorer_bookmark_path"),
        Index("ix_file_explorer_bookmarks_user_sort", "user_id", "sort_order"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "name": self.name,
            "path": self.path,
            "icon": self.icon,
            "sort_order": self.sort_order,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class GoogleCalendarConnection(Base):
    """Per-user Google Calendar OAuth tokens and preferences."""

    __tablename__ = "google_calendar_connections"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    google_email = Column(String(255), nullable=True)
    access_token = Column(Text, nullable=True)
    refresh_token = Column(Text, nullable=True)
    token_type = Column(String(64), nullable=True)
    scope = Column(Text, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    calendar_id = Column(String(255), nullable=False, default="primary")
    default_action = Column(String(32), nullable=False, default="open_template")
    default_event_reminder_minutes = Column(Integer, nullable=False, default=10)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    user = relationship("User", backref="google_calendar_connection")

    def to_dict(self, include_tokens: bool = False) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "google_email": self.google_email,
            "token_type": self.token_type,
            "scope": self.scope,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "calendar_id": self.calendar_id,
            "default_action": self.default_action,
            "default_event_reminder_minutes": self.default_event_reminder_minutes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_tokens:
            data["access_token"] = decrypt_text_if_needed(
                self.access_token,
                aad=f"google_calendar_connections.access_token:{self.user_id}",
            )
            data["refresh_token"] = decrypt_text_if_needed(
                self.refresh_token,
                aad=f"google_calendar_connections.refresh_token:{self.user_id}",
            )
        return data


class Feedback(Base):
    """User feedback on AI responses"""

    __tablename__ = "feedback"

    id = Column(String(50), primary_key=True)  # fb_<timestamp>_<uuid>
    session_id = Column(
        String(50), index=True
    )  # Corresponds to app log filename (YYYYMMDD_HHMMSS)

    # Feedback content
    message = Column(Text, nullable=False)  # The AI response that received feedback
    character = Column(String(100))  # Character name that gave the response
    user_input = Column(Text)  # Original user input (if applicable)

    # Feedback details
    category = Column(
        String(50), nullable=False, index=True
    )  # incorrect, incomplete, slow, other
    comment = Column(Text)  # User's detailed comment

    # Status
    resolved = Column(Boolean, default=False, index=True)
    resolved_at = Column(DateTime)
    resolved_by = Column(String(100))

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    # Additional metadata
    feedback_metadata = Column(JSON, default=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "message": self.message,
            "character": self.character,
            "user_input": self.user_input,
            "category": self.category,
            "comment": self.comment,
            "resolved": self.resolved,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_by": self.resolved_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "metadata": self.feedback_metadata,
        }
