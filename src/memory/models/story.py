"""Scenario Studio の正本モデル。

Story Studio は Docs の投影ではなく、このモジュールの ``story_*`` テーブルを
正本として利用する。本文とリビジョン本文は既存の AES-GCM フィールド
ヘルパーを通して保存する。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    CheckConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from .base import Base, _encrypted_text_property


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class StoryWork(Base):
    """作品（小説または TRPG シナリオ）の正本。"""

    __tablename__ = "story_works"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = Column(Text, nullable=False)
    synopsis = Column(Text, nullable=True)
    plot = Column(Text, nullable=True)
    style_guide = Column(Text, nullable=True)
    kind = Column(String(20), nullable=False, default="novel", server_default="novel")
    status = Column(String(20), nullable=False, default="planning", server_default="planning")
    target_episode_chars = Column(
        Integer, nullable=False, default=6000, server_default="6000"
    )
    planned_episode_count = Column(Integer, nullable=True)
    start_episode_id = Column(
        UUID(as_uuid=True),
        ForeignKey("story_episodes.id", ondelete="SET NULL"),
        nullable=True,
    )
    ui_state = Column(JSONB, nullable=False, default=dict, server_default="{}")
    model_override = Column(JSONB, nullable=False, default=dict, server_default="{}")
    image_settings = Column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    archived_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", foreign_keys=[user_id], backref="story_works")
    episodes = relationship(
        "StoryEpisode",
        back_populates="work",
        foreign_keys="StoryEpisode.work_id",
        cascade="all, delete-orphan",
    )
    start_episode = relationship(
        "StoryEpisode",
        foreign_keys=[start_episode_id],
        post_update=True,
    )
    links = relationship(
        "StoryLink", back_populates="work", cascade="all, delete-orphan"
    )
    notes = relationship(
        "StoryNote", back_populates="work", cascade="all, delete-orphan"
    )
    generation_jobs = relationship(
        "StoryGenerationJob", back_populates="work", cascade="all, delete-orphan"
    )
    illustrations = relationship(
        "StoryIllustration", back_populates="work", cascade="all, delete-orphan"
    )
    writing_sessions = relationship(
        "StoryWritingSession", back_populates="work", cascade="all, delete-orphan"
    )
    work_characters = relationship(
        "StoryWorkCharacter", back_populates="work", cascade="all, delete-orphan"
    )
    work_rulebooks = relationship(
        "StoryWorkRulebook", back_populates="work", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_story_works_user_status", "user_id", "status"),
    )

    def to_dict(
        self,
        *,
        episode_count: int | None = None,
        char_count: int | None = None,
        resolved_model: str | None = None,
        model_layer: str | None = None,
    ) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "title": self.title,
            "synopsis": self.synopsis,
            "plot": self.plot,
            "style_guide": self.style_guide,
            "kind": self.kind,
            "status": self.status,
            "target_episode_chars": self.target_episode_chars,
            "planned_episode_count": self.planned_episode_count,
            "start_episode_id": str(self.start_episode_id) if self.start_episode_id else None,
            "ui_state": self.ui_state or {},
            "model_override": self.model_override or {},
            "image_settings": self.image_settings or {},
            "resolved_model": resolved_model,
            "model_layer": model_layer,
            "episode_count": episode_count,
            "char_count": char_count,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "archived_at": _iso(self.archived_at),
        }


class StoryEpisode(Base):
    """作品を構成する章。本文は暗号化列 ``body`` に保存する。"""

    __tablename__ = "story_episodes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    work_id = Column(
        UUID(as_uuid=True), ForeignKey("story_works.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = Column(Text, nullable=False)
    plot = Column(Text, nullable=True)
    _body = Column("body", Text, nullable=True)
    body = _encrypted_text_property("_body", "story_episodes.body")
    body_etag = Column(String(71), nullable=True)
    summary = Column(Text, nullable=True)
    summary_locked = Column(Boolean, nullable=False, default=False, server_default="false")
    premise_note = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="unwritten", server_default="unwritten")
    target_chars = Column(Integer, nullable=True)
    char_count = Column(Integer, nullable=False, default=0, server_default="0")
    map_x = Column(Float, nullable=True)
    map_y = Column(Float, nullable=True)
    sort_hint = Column(Float, nullable=False, default=0.0, server_default="0")
    current_rev_no = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    archived_at = Column(DateTime(timezone=True), nullable=True)

    work = relationship(
        "StoryWork", back_populates="episodes", foreign_keys=[work_id]
    )
    revisions = relationship(
        "StoryEpisodeRevision",
        back_populates="episode",
        cascade="all, delete-orphan",
        order_by="StoryEpisodeRevision.rev_no",
    )
    search_index = relationship(
        "StorySearchIndex", back_populates="episode", uselist=False, cascade="all, delete-orphan"
    )
    links_from = relationship(
        "StoryLink",
        back_populates="from_episode",
        foreign_keys="StoryLink.from_episode_id",
        cascade="all, delete-orphan",
    )
    links_to = relationship(
        "StoryLink",
        back_populates="to_episode",
        foreign_keys="StoryLink.to_episode_id",
        cascade="all, delete-orphan",
    )
    writing_sessions = relationship(
        "StoryWritingSession", back_populates="episode", foreign_keys="StoryWritingSession.episode_id"
    )
    illustrations = relationship(
        "StoryIllustration", back_populates="episode", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_story_episodes_work_updated", "work_id", "updated_at"),
        Index("ix_story_episodes_work_sort", "work_id", "sort_hint"),
    )

    def to_dict(self, *, include_body: bool = True) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": str(self.id),
            "work_id": str(self.work_id),
            "title": self.title,
            "plot": self.plot,
            "summary": self.summary,
            "summary_locked": bool(self.summary_locked),
            "premise_note": self.premise_note,
            "status": self.status,
            "target_chars": self.target_chars,
            "char_count": self.char_count or 0,
            "body_etag": self.body_etag,
            "map_x": self.map_x,
            "map_y": self.map_y,
            "sort_hint": self.sort_hint,
            "current_rev_no": self.current_rev_no or 0,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "archived_at": _iso(self.archived_at),
        }
        if include_body:
            data["body"] = self.body or ""
        return data


class StoryLink(Base):
    """エピソード間の有向遷移。循環禁止はサービス層で検証する。"""

    __tablename__ = "story_links"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    work_id = Column(
        UUID(as_uuid=True), ForeignKey("story_works.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_episode_id = Column(
        UUID(as_uuid=True), ForeignKey("story_episodes.id", ondelete="CASCADE"), nullable=False
    )
    to_episode_id = Column(
        UUID(as_uuid=True), ForeignKey("story_episodes.id", ondelete="CASCADE"), nullable=False
    )
    choice_label = Column(Text, nullable=True)
    position = Column(Float, nullable=False, default=0.0, server_default="0")
    is_primary = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    work = relationship("StoryWork", back_populates="links")
    from_episode = relationship(
        "StoryEpisode", back_populates="links_from", foreign_keys=[from_episode_id]
    )
    to_episode = relationship(
        "StoryEpisode", back_populates="links_to", foreign_keys=[to_episode_id]
    )

    __table_args__ = (
        UniqueConstraint("from_episode_id", "to_episode_id", name="uq_story_links_from_to"),
        CheckConstraint("from_episode_id <> to_episode_id", name="ck_story_links_no_self_loop"),
        Index("ix_story_links_work_from", "work_id", "from_episode_id"),
        Index("ix_story_links_work_to", "work_id", "to_episode_id"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "work_id": str(self.work_id),
            "from_episode_id": str(self.from_episode_id),
            "to_episode_id": str(self.to_episode_id),
            "choice_label": self.choice_label,
            "position": self.position,
            "is_primary": bool(self.is_primary),
            "created_at": _iso(self.created_at),
        }


class StoryCharacter(Base):
    """作品をまたいで使える登場人物の共有プール。"""

    __tablename__ = "story_characters"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = Column(Text, nullable=False)
    aliases = Column(JSONB, nullable=False, default=list, server_default="[]")
    summary = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    ai_mode = Column(String(20), nullable=False, default="keyword", server_default="keyword")
    keywords = Column(JSONB, nullable=False, default=list, server_default="[]")
    image_path = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    archived_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", foreign_keys=[user_id], backref="story_characters")
    work_links = relationship(
        "StoryWorkCharacter", back_populates="character", cascade="all, delete-orphan"
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "name": self.name,
            "aliases": self.aliases or [],
            "summary": self.summary,
            "description": self.description,
            "notes": self.notes,
            "ai_mode": self.ai_mode,
            "keywords": self.keywords or [],
            "image_path": self.image_path,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "archived_at": _iso(self.archived_at),
        }


class StoryWorkCharacter(Base):
    __tablename__ = "story_work_characters"

    work_id = Column(
        UUID(as_uuid=True), ForeignKey("story_works.id", ondelete="CASCADE"), primary_key=True
    )
    character_id = Column(
        UUID(as_uuid=True), ForeignKey("story_characters.id", ondelete="CASCADE"), primary_key=True
    )
    role_note = Column(Text, nullable=True)
    position = Column(Float, nullable=False, default=0.0, server_default="0")

    work = relationship("StoryWork", back_populates="work_characters")
    character = relationship("StoryCharacter", back_populates="work_links")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "work_id": str(self.work_id),
            "character_id": str(self.character_id),
            "role_note": self.role_note,
            "position": self.position,
        }


class StoryRulebook(Base):
    """作品をまたいで使える文体・ルールの共有プール。"""

    __tablename__ = "story_rulebooks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = Column(Text, nullable=False)
    content = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    archived_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", foreign_keys=[user_id], backref="story_rulebooks")
    work_links = relationship(
        "StoryWorkRulebook", back_populates="rulebook", cascade="all, delete-orphan"
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "name": self.name,
            "content": self.content,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "archived_at": _iso(self.archived_at),
        }


class StoryWorkRulebook(Base):
    __tablename__ = "story_work_rulebooks"

    work_id = Column(
        UUID(as_uuid=True), ForeignKey("story_works.id", ondelete="CASCADE"), primary_key=True
    )
    rulebook_id = Column(
        UUID(as_uuid=True), ForeignKey("story_rulebooks.id", ondelete="CASCADE"), primary_key=True
    )
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    position = Column(Float, nullable=False, default=0.0, server_default="0")

    work = relationship("StoryWork", back_populates="work_rulebooks")
    rulebook = relationship("StoryRulebook", back_populates="work_links")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "work_id": str(self.work_id),
            "rulebook_id": str(self.rulebook_id),
            "enabled": bool(self.enabled),
            "position": self.position,
        }


class StoryNote(Base):
    __tablename__ = "story_notes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    work_id = Column(
        UUID(as_uuid=True), ForeignKey("story_works.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = Column(Text, nullable=False)
    content = Column(Text, nullable=True)
    ai_mode = Column(String(20), nullable=False, default="keyword", server_default="keyword")
    keywords = Column(JSONB, nullable=False, default=list, server_default="[]")
    position = Column(Float, nullable=False, default=0.0, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    work = relationship("StoryWork", back_populates="notes")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "work_id": str(self.work_id),
            "title": self.title,
            "content": self.content,
            "ai_mode": self.ai_mode,
            "keywords": self.keywords or [],
            "position": self.position,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class StoryEpisodeRevision(Base):
    __tablename__ = "story_episode_revisions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    episode_id = Column(
        UUID(as_uuid=True), ForeignKey("story_episodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rev_no = Column(Integer, nullable=False)
    title = Column(Text, nullable=True)
    plot = Column(Text, nullable=True)
    _body = Column("body", Text, nullable=True)
    body = _encrypted_text_property("_body", "story_episode_revisions.body")
    message = Column(Text, nullable=True)
    origin = Column(String(30), nullable=False)
    body_sha256 = Column(String(64), nullable=False)
    char_count = Column(Integer, nullable=False, default=0, server_default="0")
    created_by = Column(String(20), nullable=False, default="user", server_default="user")
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    episode = relationship("StoryEpisode", back_populates="revisions")

    __table_args__ = (
        UniqueConstraint("episode_id", "rev_no", name="uq_story_episode_revisions_episode_rev"),
        # §5.6 の INDEX(episode_id, rev_no DESC)。履歴一覧は rev_no 降順で引くため、
        # 索引自体を降順で作る（名前だけ _desc で実体が昇順、という状態を避ける）。
        Index(
            "ix_story_episode_revisions_episode_rev_desc",
            "episode_id",
            text("rev_no DESC"),
        ),
    )

    def to_dict(self, *, include_body: bool = False) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": str(self.id),
            "episode_id": str(self.episode_id),
            "rev_no": self.rev_no,
            "title": self.title,
            "plot": self.plot,
            "message": self.message,
            "origin": self.origin,
            "body_sha256": self.body_sha256,
            "char_count": self.char_count or 0,
            "created_by": self.created_by,
            "created_at": _iso(self.created_at),
        }
        if include_body:
            data["body"] = self.body or ""
        return data


class StorySearchIndex(Base):
    __tablename__ = "story_search_index"

    episode_id = Column(
        UUID(as_uuid=True), ForeignKey("story_episodes.id", ondelete="CASCADE"), primary_key=True
    )
    work_id = Column(
        UUID(as_uuid=True), ForeignKey("story_works.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = Column(Text, nullable=False)
    body_plain = Column(Text, nullable=False, default="", server_default="")

    episode = relationship("StoryEpisode", back_populates="search_index")
    work = relationship("StoryWork")


class StoryGenerationJob(Base):
    __tablename__ = "story_generation_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    work_id = Column(
        UUID(as_uuid=True), ForeignKey("story_works.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind = Column(String(20), nullable=False)
    payload = Column(JSONB, nullable=False, default=dict, server_default="{}")
    status = Column(String(20), nullable=False, default="queued", server_default="queued")
    progress = Column(JSONB, nullable=False, default=dict, server_default="{}")
    result = Column(JSONB, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    work = relationship("StoryWork", back_populates="generation_jobs")

    __table_args__ = (
        Index("ix_story_generation_jobs_work_status", "work_id", "status"),
    )

    def to_dict(self) -> Dict[str, Any]:
        public_payload = {
            key: value
            for key, value in (self.payload or {}).items()
            if not str(key).startswith("_")
        }
        return {
            "id": str(self.id),
            "work_id": str(self.work_id),
            "kind": self.kind,
            "payload": public_payload,
            "status": self.status,
            "progress": self.progress or {},
            "result": self.result,
            "error": self.error,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
        }


class StoryIllustration(Base):
    """エピソード本文に紐づく挿絵。本文は書き換えず、anchor_quote で位置を解決する。"""

    __tablename__ = "story_illustrations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    work_id = Column(
        UUID(as_uuid=True), ForeignKey("story_works.id", ondelete="CASCADE"), nullable=False, index=True
    )
    episode_id = Column(
        UUID(as_uuid=True), ForeignKey("story_episodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    body_etag = Column(Text, nullable=False)
    rev_no = Column(Integer, nullable=True)
    anchor_kind = Column(String(32), nullable=True)
    anchor_quote = Column(Text, nullable=False)
    offset_hint = Column(Integer, nullable=True)
    ordering = Column(Integer, nullable=False, default=0, server_default="0")
    scene_description = Column(Text, nullable=True)
    visual_prompt = Column(Text, nullable=True)
    status = Column(String(16), nullable=False, default="pending", server_default="pending")
    generated_media_id = Column(UUID(as_uuid=True), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    work = relationship("StoryWork", back_populates="illustrations")
    episode = relationship("StoryEpisode", back_populates="illustrations")

    __table_args__ = (
        Index("ix_story_illustrations_episode_ordering", "episode_id", "ordering"),
    )

    def to_dict(self, *, resolved_index: int | None = None, stale: bool | None = None) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": str(self.id),
            "work_id": str(self.work_id),
            "episode_id": str(self.episode_id),
            "body_etag": self.body_etag,
            "rev_no": self.rev_no,
            "anchor_kind": self.anchor_kind,
            "anchor_quote": self.anchor_quote,
            "offset_hint": self.offset_hint,
            "ordering": self.ordering,
            "scene_description": self.scene_description,
            "visual_prompt": self.visual_prompt,
            "status": self.status,
            "generated_media_id": str(self.generated_media_id) if self.generated_media_id else None,
            "error_message": self.error_message,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if resolved_index is not None:
            data["resolved_index"] = resolved_index
        if stale is not None:
            data["stale"] = stale
        return data


class StoryWritingSession(Base):
    __tablename__ = "story_writing_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    work_id = Column(
        UUID(as_uuid=True), ForeignKey("story_works.id", ondelete="CASCADE"), nullable=False, index=True
    )
    episode_id = Column(
        UUID(as_uuid=True), ForeignKey("story_episodes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    conversation_session_id = Column(
        UUID(as_uuid=True), ForeignKey("conversation_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    work = relationship("StoryWork", back_populates="writing_sessions")
    episode = relationship(
        "StoryEpisode", back_populates="writing_sessions", foreign_keys=[episode_id]
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "work_id": str(self.work_id),
            "episode_id": str(self.episode_id) if self.episode_id else None,
            "conversation_session_id": (
                str(self.conversation_session_id) if self.conversation_session_id else None
            ),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


__all__ = [
    "StoryWork",
    "StoryEpisode",
    "StoryLink",
    "StoryCharacter",
    "StoryWorkCharacter",
    "StoryRulebook",
    "StoryWorkRulebook",
    "StoryNote",
    "StoryEpisodeRevision",
    "StorySearchIndex",
    "StoryGenerationJob",
    "StoryIllustration",
    "StoryWritingSession",
]
