"""会話セッション・参加者・メッセージ・アーカイブ系モデル。"""

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import (
    Column,
    String,
    Text,
    Integer,
    DateTime,
    JSON,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Boolean,
    UniqueConstraint,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .base import Base, _encrypted_text_property


def _public_message_metadata(value: Any) -> Any:
    """Hide provider-internal reasoning while retaining it in persisted metadata."""
    if isinstance(value, dict):
        return {
            key: _public_message_metadata(item)
            for key, item in value.items()
            if key != "reasoning_content"
        }
    if isinstance(value, list):
        return [_public_message_metadata(item) for item in value]
    return value


def public_session_context(value: Any) -> Any:
    """Hide provider-owned continuation handles from API/sync payloads."""
    if not isinstance(value, dict):
        return value
    return {
        key: value_item
        for key, value_item in value.items()
        if key != "cli_native_sessions"
    }


class ConversationSession(Base):
    """Active conversation session"""

    __tablename__ = "conversation_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String, nullable=False, index=True)
    character_name = Column(String, nullable=False)
    title = Column(String(200), default="")  # UI表示用タイトル（LLM自動生成）
    session_start = Column(DateTime, default=datetime.utcnow)
    last_activity = Column(DateTime, default=datetime.utcnow)
    message_count = Column(Integer, default=0)
    context = Column(JSON, default=dict)
    _current_summary = Column("current_summary", Text, default="")
    current_summary = _encrypted_text_property(
        "_current_summary",
        "conversation_sessions.current_summary",
    )
    is_active = Column(Boolean, default=True)
    deleted_at = Column(
        DateTime, nullable=True, index=True
    )  # ソフトデリート用（共通保持期間後に実削除、既定30日）

    # Project association
    project_id = Column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True, index=True
    )
    app_id = Column(
        UUID(as_uuid=True),
        ForeignKey("apps.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Target は (app_id, app_target_id) の複合 FK で App に閉じ込める。
    app_target_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    # App開発チャットだけが持つ進行状態。通常のChatはNULLのままにする。
    development_status = Column(String(32), nullable=True, index=True)
    # ユーザーが最後に開いた時刻。App開発チャットの完了応答を未読判定する。
    last_read_at = Column(DateTime, nullable=True, index=True)
    parent_session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversation_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    forked_from_message_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversation_messages.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )

    # グループチャット
    is_group_chat = Column(Boolean, default=False)
    group_character_names = Column(JSON, default=list)  # 参加キャラslugリスト

    # RPステアリングスライダー
    rp_settings = Column(
        JSON, default=dict
    )  # {"creativity": 0.5, "detail": 0.5, "tempo": 0.5, "emotion": 0.5}

    # Relationships
    messages = relationship(
        "ConversationMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        foreign_keys="ConversationMessage.session_id",
    )
    participants = relationship(
        "ConversationParticipant",
        back_populates="session",
        cascade="all, delete-orphan",
    )
    project = relationship(
        "Project", backref="conversation_sessions", passive_deletes=True
    )
    app = relationship("App", foreign_keys=[app_id], backref="conversation_sessions")
    app_target = relationship(
        "AppTarget",
        primaryjoin="ConversationSession.app_target_id == AppTarget.id",
        foreign_keys=[app_target_id],
    )

    __table_args__ = (
        # 実 DB は ON DELETE SET NULL (app_target_id)。app_id は巻き込まない。
        ForeignKeyConstraint(
            ["app_id", "app_target_id"],
            ["app_targets.app_id", "app_targets.id"],
            name="fk_conversation_sessions_app_target_app",
            ondelete="SET NULL",
        ),
        # 複合 FK は MATCH SIMPLE のため app_id が NULL だと検査されない。
        CheckConstraint(
            "app_target_id IS NULL OR app_id IS NOT NULL",
            name="ck_conversation_sessions_app_target_requires_app",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "user_id": self.user_id,
            "character_name": self.character_name,
            "title": self.title,
            "session_start": (
                self.session_start.isoformat() if self.session_start else None
            ),
            "last_activity": (
                self.last_activity.isoformat() if self.last_activity else None
            ),
            "message_count": self.message_count,
            "context": public_session_context(self.context),
            "current_summary": self.current_summary,
            "is_active": self.is_active,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
            "project_id": str(self.project_id) if self.project_id else None,
            "app_id": str(self.app_id) if self.app_id else None,
            "app_target_id": str(self.app_target_id) if self.app_target_id else None,
            "development_status": self.development_status,
            "last_read_at": (
                self.last_read_at.isoformat() if self.last_read_at else None
            ),
            "parent_session_id": (
                str(self.parent_session_id) if self.parent_session_id else None
            ),
            "forked_from_message_id": (
                str(self.forked_from_message_id)
                if self.forked_from_message_id
                else None
            ),
            "is_group_chat": self.is_group_chat or False,
            "group_character_names": self.group_character_names or [],
            "participants": [
                participant.to_dict()
                for participant in self.__dict__.get("participants", []) or []
            ],
            "rp_settings": self.rp_settings or {},
        }


class ConversationParticipant(Base):
    """A user, AI character, or agent that can access a conversation."""

    __tablename__ = "conversation_participants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversation_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    participant_type = Column(String(32), nullable=False)  # user | character | agent
    participant_id = Column(String(200), nullable=False)
    display_name = Column(String(200), default="")
    role = Column(String(32), default="member")  # owner | member | observer
    status = Column(String(32), default="joined")  # joined | invited | left
    auto_respond = Column(Boolean, default=False)
    participant_metadata = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True
    )

    session = relationship("ConversationSession", back_populates="participants")

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "participant_type",
            "participant_id",
            name="uq_conversation_participant_identity",
        ),
        Index("ix_conversation_participants_lookup", "participant_type", "participant_id"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "session_id": str(self.session_id),
            "participant_type": self.participant_type,
            "participant_id": self.participant_id,
            "display_name": self.display_name,
            "role": self.role,
            "status": self.status,
            "auto_respond": bool(self.auto_respond),
            "metadata": self.participant_metadata or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ConversationMessage(Base):
    """Individual conversation message"""

    __tablename__ = "conversation_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(
        UUID(as_uuid=True), ForeignKey("conversation_sessions.id"), nullable=False
    )
    role = Column(String, nullable=False)  # 'user' or 'assistant'
    _content = Column("content", Text, nullable=False)
    content = _encrypted_text_property("_content", "conversation_messages.content")

    # embedding removed - using Qdrant for vector search instead

    message_metadata = Column(JSON, default=dict)
    sender_type = Column(String(32), nullable=True)
    sender_id = Column(String(200), nullable=True)
    sender_display_name = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True
    )
    deleted_at = Column(DateTime, nullable=True, index=True)  # tombstone for sync
    token_count = Column(Integer)

    # Branching support (like ChatGPT's edit/branch feature)
    parent_message_id = Column(
        UUID(as_uuid=True), ForeignKey("conversation_messages.id"), nullable=True
    )
    branch_index = Column(Integer, default=0)  # Index among sibling branches
    is_active_branch = Column(Boolean, default=True)  # Currently displayed branch

    # Relationship to session
    session = relationship(
        "ConversationSession",
        back_populates="messages",
        foreign_keys=[session_id],
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "session_id": str(self.session_id),
            "role": self.role,
            "content": self.content,
            "metadata": _public_message_metadata(self.message_metadata or {}),
            "sender_type": self.sender_type,
            "sender_id": self.sender_id,
            "sender_display_name": self.sender_display_name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
            "token_count": self.token_count,
            "parent_message_id": (
                str(self.parent_message_id) if self.parent_message_id else None
            ),
            "branch_index": self.branch_index,
            "is_active_branch": self.is_active_branch,
        }


class ConversationArchive(Base):
    """Archived conversation summaries for long-term memory"""

    __tablename__ = "conversation_archives"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String, nullable=False, index=True)
    character_name = Column(String, nullable=False)
    original_session_id = Column(String)
    _summary = Column("summary", Text, nullable=False)
    summary = _encrypted_text_property("_summary", "conversation_archives.summary")

    # summary_embedding removed - using Qdrant for vector search instead

    message_count = Column(Integer)
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    message_metadata = Column(JSON, default=dict)
    archived_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "character_name": self.character_name,
            "original_session_id": self.original_session_id,
            "summary": self.summary,
            "message_count": self.message_count,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "metadata": _public_message_metadata(self.message_metadata or {}),
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
        }


class ConversationHistory(Base):
    """Complete conversation history for audit and analysis"""

    __tablename__ = "conversation_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String, nullable=False, index=True)
    session_id = Column(UUID(as_uuid=True))
    character_name = Column(String, nullable=False)
    role = Column(String, nullable=False)
    _content = Column("content", Text, nullable=False)
    content = _encrypted_text_property("_content", "conversation_history.content")
    message_metadata = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    token_count = Column(Integer)
    function_call_data = Column(JSON)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "character_name": self.character_name,
            "role": self.role,
            "content": self.content,
            "metadata": _public_message_metadata(self.message_metadata or {}),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "token_count": self.token_count,
            "function_call_data": self.function_call_data,
        }
