"""ユーザー・認証・ログ・ユーザー単位の設定/連携系モデル。"""

import uuid
from datetime import datetime
from typing import Any, Dict
from urllib.parse import quote
from pathlib import PurePosixPath

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
    CheckConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import backref, relationship

from ...security.field_crypto import decrypt_text_if_needed
from .base import Base, _encrypted_json_property


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
    # Relative reference to the user-scoped avatar file.  The image bytes are
    # kept under workspaces/_users/user_<id>/avatar, never in the database.
    avatar_path = Column(String(512), nullable=True)

    # Role & Status
    role = Column(
        String(20),
        default="user",
        server_default="user",
        nullable=False,
        index=True,
    )  # 'admin', 'user'
    is_active = Column(Boolean, default=True, index=True)
    is_password_reset_required = Column(Boolean, default=True)
    # Incremented whenever credentials or authorization state changes.
    # All browser/JWT/API-token sessions carry this value and are rejected
    # after it changes.
    session_version = Column(Integer, nullable=False, default=1, server_default="1")

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login = Column(DateTime)

    # Settings (JSON for flexibility)
    user_settings = Column(JSON, default=dict)

    __table_args__ = (
        CheckConstraint(
            "role IN ('admin', 'user')",
            name="ck_users_role_admin_user",
        ),
    )

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
            "avatar_url": self._avatar_url(),
            "role": self.role,
            "is_active": self.is_active,
            "is_password_reset_required": self.is_password_reset_required,
            "session_version": self.session_version or 1,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_login": self.last_login.isoformat() if self.last_login else None,
            "settings": self.user_settings,
        }
        if include_sensitive:
            result["password_hash"] = self.password_hash
        return result

    def _avatar_url(self) -> str | None:
        """Return the cache-busting public URL without exposing avatar_path."""
        if not self.avatar_path:
            return None
        normalized = str(self.avatar_path).replace("\\", "/")
        expected_prefix = f"_users/user_{self.id}/avatar/"
        if not normalized.startswith(expected_prefix):
            return None
        file_name = PurePosixPath(normalized).name
        if (
            not file_name
            or file_name in {".", ".."}
            or "/" in file_name
            or "\x00" in file_name
        ):
            return None
        return f"/api/users/{quote(str(self.id), safe='')}/avatar?v={quote(file_name, safe='')}"


class ScopedMemoryPrincipal(Base):
    """Durable settings for a non-``users`` Scoped Memory principal.

    Integrations such as Discord use an immutable, namespaced principal key
    (for example ``discord:<guild_id>:<user_id>``).  They must not be forced
    through the authenticated ``users`` table or represented by a synthetic
    User row.  The memory rows themselves continue to carry the canonical key
    in their existing string owner column; this small store only keeps
    principal-owned settings and metadata.
    """

    __tablename__ = "scoped_memory_principals"

    principal_key = Column(String(120), primary_key=True)
    provider = Column(String(32), nullable=False, default="external", index=True)
    settings = Column(JSON, nullable=False, default=dict)
    metadata_json = Column("metadata", JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "principal_key": self.principal_key,
            "provider": self.provider,
            "settings": dict(self.settings or {}),
            "metadata": dict(self.metadata_json or {}),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class _UserIntegrationCredentialMixin:
    """Shared safe serialization for per-user integration credentials.

    The payload property is application-encrypted by ``field_crypto``.  API
    callers should only use :meth:`to_safe_dict`; it deliberately never
    exposes the decrypted payload.
    """

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "configured": bool(self._encrypted_payload),
            "settings": self.settings_json or {},
            "enabled": bool(self.enabled),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class UserHfCredential(_UserIntegrationCredentialMixin, Base):
    """Per-user Hugging Face account references and encrypted credentials."""

    __tablename__ = "user_hf_credentials"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    # Store only the field-crypto ciphertext.  The decrypted object is exposed
    # through ``payload`` for service code and is never serialized by default.
    _encrypted_payload = Column("encrypted_payload", Text, nullable=True)
    payload = _encrypted_json_property(
        "_encrypted_payload", "user_hf_credentials.encrypted_payload"
    )
    settings_json = Column(JSON, nullable=False, default=dict, server_default="{}")
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    user = relationship("User", backref="hf_credentials")


class UserHydrusCredential(_UserIntegrationCredentialMixin, Base):
    """Per-user Hydrus Client connection settings and encrypted access key."""

    __tablename__ = "user_hydrus_credentials"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    _encrypted_payload = Column("encrypted_payload", Text, nullable=True)
    payload = _encrypted_json_property(
        "_encrypted_payload", "user_hydrus_credentials.encrypted_payload"
    )
    settings_json = Column(JSON, nullable=False, default=dict, server_default="{}")
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    user = relationship("User", backref="hydrus_credentials")


class UserXCookieCredential(Base):
    """One encrypted X Cookie credential (or an explicit disabled tombstone).

    The raw export is never stored.  ``encrypted_payload`` contains only a
    canonical ``auth_token``/``ct0`` object encrypted by the X-cookie service
    with user-bound AAD.  ``disabled`` rows intentionally remain after DELETE
    so the shared operator fallback cannot silently return.
    """

    __tablename__ = "user_x_cookie_credentials"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    encrypted_payload = Column(Text, nullable=True)
    disabled = Column(Boolean, nullable=False, default=False, server_default="false")
    disabled_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    user = relationship(
        "User",
        backref=backref("x_cookie_credential", uselist=False),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        """Serialize metadata only; never return payload/ciphertext."""

        return {
            "configured": bool(self.encrypted_payload) and not bool(self.disabled),
            "disabled": bool(self.disabled),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


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
    session_version = Column(Integer, nullable=False, default=1, server_default="1")
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
    """File explorer bookmark.

    ``space_id`` is the ownership boundary for project-backed targets.  The
    two ownership columns are exclusive: a Space-owned row has a non-null
    ``space_id`` and a null ``user_id``; a legacy/private row has a non-null
    ``user_id`` and a null ``space_id``.  Existing user rows are retained and
    migration creates separate Space-owned clones where it is safe to do so.
    """

    __tablename__ = "file_explorer_bookmarks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    space_id = Column(
        UUID(as_uuid=True),
        ForeignKey("spaces.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name = Column(String(200), nullable=False)
    path = Column(Text, nullable=False)
    icon = Column(String(64), nullable=True)
    kind = Column(String(16), nullable=False, server_default="bookmark")
    parent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("file_explorer_bookmarks.id", ondelete="CASCADE"),
        nullable=True,
    )
    sort_order = Column(Float, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    user = relationship("User", backref="file_explorer_bookmarks")
    space = relationship(
        "Space",
        backref=backref("file_explorer_bookmarks", passive_deletes=True),
    )
    parent = relationship(
        "FileExplorerBookmark",
        remote_side="FileExplorerBookmark.id",
        backref=backref("children", cascade="all, delete-orphan"),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "path", name="unique_file_explorer_bookmark_path"),
        UniqueConstraint(
            "space_id", "path", name="unique_file_explorer_bookmark_space_path"
        ),
        CheckConstraint(
            "(space_id IS NULL) <> (user_id IS NULL)",
            name="ck_file_explorer_bookmarks_owner_xor",
        ),
        Index("ix_file_explorer_bookmarks_user_sort", "user_id", "sort_order"),
        Index("ix_file_explorer_bookmarks_space_sort", "space_id", "sort_order"),
        Index(
            "ix_file_explorer_bookmarks_user_parent_sort",
            "user_id",
            "parent_id",
            "sort_order",
        ),
        Index(
            "ix_file_explorer_bookmarks_space_parent_sort",
            "space_id",
            "parent_id",
            "sort_order",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "user_id": str(self.user_id) if self.user_id else None,
            "space_id": str(self.space_id) if self.space_id else None,
            "name": self.name,
            "path": self.path,
            "icon": self.icon,
            "kind": self.kind,
            "parent_id": str(self.parent_id) if self.parent_id else None,
            "sort_order": self.sort_order,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class FileExplorerLauncher(Base):
    """File launcher entry for the Files sidebar.

    See :class:`FileExplorerBookmark` for the exclusive Space/user ownership
    contract.
    """

    __tablename__ = "file_explorer_launchers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    space_id = Column(
        UUID(as_uuid=True),
        ForeignKey("spaces.id", ondelete="CASCADE"),
        nullable=True,
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

    user = relationship("User", backref="file_explorer_launchers")
    space = relationship(
        "Space",
        backref=backref("file_explorer_launchers", passive_deletes=True),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "path", name="unique_file_explorer_launcher_path"),
        UniqueConstraint(
            "space_id", "path", name="unique_file_explorer_launcher_space_path"
        ),
        CheckConstraint(
            "(space_id IS NULL) <> (user_id IS NULL)",
            name="ck_file_explorer_launchers_owner_xor",
        ),
        Index("ix_file_explorer_launchers_user_sort", "user_id", "sort_order"),
        Index("ix_file_explorer_launchers_space_sort", "space_id", "sort_order"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "user_id": str(self.user_id) if self.user_id else None,
            "space_id": str(self.space_id) if self.space_id else None,
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


class WebexConnection(Base):
    """ユーザー単位の Webex OAuth 接続。"""

    __tablename__ = "webex_connections"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    webex_person_id = Column(String(255), nullable=True)
    webex_org_id = Column(String(255), nullable=True)
    webex_email = Column(String(255), nullable=True)
    webex_display_name = Column(String(255), nullable=True)
    access_token = Column(Text, nullable=True)
    refresh_token = Column(Text, nullable=True)
    token_type = Column(String(64), nullable=True)
    scope = Column(Text, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    user = relationship("User", backref="webex_connection")
    selected_spaces = relationship(
        "WebexSpaceSelection",
        back_populates="connection",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def to_dict(self, include_tokens: bool = False) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "webex_person_id": self.webex_person_id,
            "webex_org_id": self.webex_org_id,
            "webex_email": self.webex_email,
            "webex_display_name": self.webex_display_name,
            "token_type": self.token_type,
            "scope": self.scope,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_tokens:
            data["access_token"] = decrypt_text_if_needed(
                self.access_token,
                aad=f"webex_connections.access_token:{self.user_id}",
            )
            data["refresh_token"] = decrypt_text_if_needed(
                self.refresh_token,
                aad=f"webex_connections.refresh_token:{self.user_id}",
            )
        return data


class WebexSpaceSelection(Base):
    """AoiTalk から読み取りを許可した Webex スペース。"""

    __tablename__ = "webex_space_selections"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id = Column(
        UUID(as_uuid=True),
        ForeignKey("webex_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    room_id = Column(String(255), nullable=False)
    title = Column(String(500), nullable=False)
    room_type = Column(String(32), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    connection = relationship("WebexConnection", back_populates="selected_spaces")

    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "room_id",
            name="uq_webex_space_selections_connection_room",
        ),
        Index("ix_webex_space_selections_connection_room", "connection_id", "room_id"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "connection_id": str(self.connection_id),
            "room_id": self.room_id,
            "title": self.title,
            "room_type": self.room_type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


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
