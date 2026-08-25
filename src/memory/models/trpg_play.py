"""TRPG Play 実行系の正本モデル（シナリオ本文は StoryWork にのみ保持）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from .base import Base


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class TrpgPlaySession(Base):
    __tablename__ = "trpg_play_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    work_id = Column(
        UUID(as_uuid=True),
        ForeignKey("story_works.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    host_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = Column(Text, nullable=False)
    gm_mode = Column(String(16), nullable=False, default="human", server_default="human")
    status = Column(String(16), nullable=False, default="lobby", server_default="lobby")
    invite_code = Column(String(32), nullable=True, unique=True)
    snapshot = Column(JSONB, nullable=False, default=dict, server_default="{}")
    image_settings = Column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    ended_at = Column(DateTime(timezone=True), nullable=True)

    work = relationship("StoryWork", foreign_keys=[work_id])
    host_user = relationship("User", foreign_keys=[host_user_id])
    participants = relationship(
        "TrpgPlayParticipant",
        back_populates="session",
        cascade="all, delete-orphan",
    )
    events = relationship(
        "TrpgPlayEvent",
        back_populates="session",
        cascade="all, delete-orphan",
    )
    whispers = relationship(
        "TrpgPlayWhisper",
        back_populates="session",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_trpg_play_sessions_host_status", "host_user_id", "status"),
    )

    def to_dict(
        self,
        *,
        participants: list[Dict[str, Any]] | None = None,
        recent_events: list[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": str(self.id),
            "work_id": str(self.work_id),
            "host_user_id": str(self.host_user_id),
            "title": self.title,
            "gm_mode": self.gm_mode,
            "status": self.status,
            "invite_code": self.invite_code,
            "snapshot": self.snapshot or {},
            "image_settings": self.image_settings or {},
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "ended_at": _iso(self.ended_at),
        }
        if participants is not None:
            payload["participants"] = participants
        if recent_events is not None:
            payload["recent_events"] = recent_events
        return payload


class TrpgPlayParticipant(Base):
    __tablename__ = "trpg_play_participants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("trpg_play_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    display_name = Column(Text, nullable=False)
    role = Column(String(16), nullable=False, default="player", server_default="player")
    story_character_id = Column(
        UUID(as_uuid=True),
        ForeignKey("story_characters.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_npc = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    joined_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    left_at = Column(DateTime(timezone=True), nullable=True)

    session = relationship("TrpgPlaySession", back_populates="participants")
    private_state = relationship(
        "TrpgPlayPrivateState",
        back_populates="participant",
        uselist=False,
        cascade="all, delete-orphan",
    )
    user = relationship("User", foreign_keys=[user_id])
    story_character = relationship("StoryCharacter", foreign_keys=[story_character_id])

    __table_args__ = (
        Index(
            "uq_trpg_play_participants_session_user",
            "session_id",
            "user_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "session_id": str(self.session_id),
            "user_id": str(self.user_id) if self.user_id else None,
            "display_name": self.display_name,
            "role": self.role,
            "story_character_id": str(self.story_character_id) if self.story_character_id else None,
            "is_npc": bool(self.is_npc),
            "joined_at": _iso(self.joined_at),
            "left_at": _iso(self.left_at),
        }


class TrpgPlayEvent(Base):
    __tablename__ = "trpg_play_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("trpg_play_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_participant_id = Column(
        UUID(as_uuid=True),
        ForeignKey("trpg_play_participants.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind = Column(String(16), nullable=False)
    body = Column(Text, nullable=False)
    meta = Column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    session = relationship("TrpgPlaySession", back_populates="events")
    actor = relationship("TrpgPlayParticipant", foreign_keys=[actor_participant_id])

    __table_args__ = (
        Index("ix_trpg_play_events_session_created", "session_id", "created_at"),
    )

    def to_dict(self, *, actor_display_name: str | None = None) -> Dict[str, Any]:
        payload = {
            "id": str(self.id),
            "session_id": str(self.session_id),
            "actor_participant_id": str(self.actor_participant_id) if self.actor_participant_id else None,
            "kind": self.kind,
            "body": self.body,
            "meta": self.meta or {},
            "created_at": _iso(self.created_at),
        }
        if actor_display_name:
            payload["actor_display_name"] = actor_display_name
        return payload


class TrpgPlayWhisper(Base):
    __tablename__ = "trpg_play_whispers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("trpg_play_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sender_participant_id = Column(
        UUID(as_uuid=True),
        ForeignKey("trpg_play_participants.id", ondelete="CASCADE"),
        nullable=False,
    )
    body = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    session = relationship("TrpgPlaySession", back_populates="whispers")
    sender = relationship("TrpgPlayParticipant", foreign_keys=[sender_participant_id])
    recipients = relationship(
        "TrpgPlayWhisperRecipient",
        back_populates="whisper",
        cascade="all, delete-orphan",
    )

    def to_dict(self, *, recipient_participant_ids: list[str] | None = None) -> Dict[str, Any]:
        ids = recipient_participant_ids
        if ids is None:
            ids = [str(item.participant_id) for item in self.recipients]
        return {
            "id": str(self.id),
            "session_id": str(self.session_id),
            "sender_participant_id": str(self.sender_participant_id),
            "body": self.body,
            "recipient_participant_ids": ids,
            "created_at": _iso(self.created_at),
        }


class TrpgPlayPrivateState(Base):
    __tablename__ = "trpg_play_private_states"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("trpg_play_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    participant_id = Column(
        UUID(as_uuid=True),
        ForeignKey("trpg_play_participants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    state = Column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    session = relationship("TrpgPlaySession", foreign_keys=[session_id])
    participant = relationship("TrpgPlayParticipant", back_populates="private_state")

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "participant_id",
            name="uq_trpg_play_private_states_session_participant",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "session_id": str(self.session_id),
            "participant_id": str(self.participant_id),
            "state": self.state or {},
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class TrpgPlayWhisperRecipient(Base):
    __tablename__ = "trpg_play_whisper_recipients"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    whisper_id = Column(
        UUID(as_uuid=True),
        ForeignKey("trpg_play_whispers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    participant_id = Column(
        UUID(as_uuid=True),
        ForeignKey("trpg_play_participants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    whisper = relationship("TrpgPlayWhisper", back_populates="recipients")
    participant = relationship("TrpgPlayParticipant", foreign_keys=[participant_id])

    __table_args__ = (
        UniqueConstraint("whisper_id", "participant_id", name="uq_trpg_play_whisper_recipient"),
    )


__all__ = [
    "TrpgPlaySession",
    "TrpgPlayParticipant",
    "TrpgPlayEvent",
    "TrpgPlayPrivateState",
    "TrpgPlayWhisper",
    "TrpgPlayWhisperRecipient",
]
