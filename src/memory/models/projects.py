"""スペース・プロジェクト・プロジェクト情報(カテゴリ/文書/ファクト)系モデル。"""

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

from .base import Base, _encrypted_text_property


class Space(Base):
    """スペース（プロジェクトを束ねる上位概念）"""

    __tablename__ = "spaces"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)
    slug = Column(String(100), nullable=False, index=True)
    description = Column(Text)
    color = Column(String(64))
    owner_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    sort_order = Column(Float, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner = relationship("User", backref="owned_spaces", foreign_keys=[owner_id])
    projects = relationship("Project", back_populates="space")
    tags = relationship(
        "Tag",
        back_populates="space",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "color": self.color,
            "owner_id": str(self.owner_id),
            "sort_order": self.sort_order,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Project(Base):
    """プロジェクト（共有ストレージ単位）"""

    __tablename__ = "projects"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    slug = Column(String(100), unique=True, nullable=False, index=True)  # URL用の識別子
    aliases = Column(JSON, default=list)  # 検索用エイリアス（例: ["tokyo", "fy25"]）

    # オーナー（作成者）
    owner_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    space_id = Column(UUID(as_uuid=True), ForeignKey("spaces.id"), nullable=True)

    # 設定
    allow_join_requests = Column(Boolean, default=True)  # 参加申請を受け付けるか

    # ストレージ設定
    storage_quota_mb = Column(Integer, default=1000)  # 容量制限（MB）
    storage_used_mb = Column(Float, default=0)

    # タイムスタンプ
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True, index=True)  # tombstone for sync

    # メタデータ
    project_metadata = Column(JSON, default=dict)

    # リレーション
    owner = relationship("User", backref="owned_projects", foreign_keys=[owner_id])
    space = relationship("Space", back_populates="projects")
    members = relationship(
        "ProjectMember", back_populates="project", cascade="all, delete-orphan"
    )
    join_requests = relationship(
        "ProjectJoinRequest", back_populates="project", cascade="all, delete-orphan"
    )
    tasks = relationship(
        "Task",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    local_tasks = relationship(
        "LocalTask",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    notification_settings = relationship(
        "ProjectNotificationSetting",
        back_populates="project",
        cascade="all, delete-orphan",
        uselist=False,
        passive_deletes=True,
    )
    notification_deliveries = relationship(
        "NotificationDelivery",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    record_tables = relationship(
        "RecordTable",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    context_pack = relationship(
        "ProjectContextPack",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    context_memories = relationship(
        "ContextMemory",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    info_categories = relationship(
        "ProjectInfoCategory",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    documents = relationship(
        "ProjectDocument",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    facts = relationship(
        "ProjectFact",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    info_sync_states = relationship(
        "ProjectInfoSyncState",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def to_dict(self) -> Dict[str, Any]:
        from ...services.project_context import normalize_project_metadata

        return {
            "id": str(self.id),
            "name": self.name,
            "description": self.description,
            "slug": self.slug,
            "aliases": self.aliases or [],
            "owner_id": str(self.owner_id),
            "space_id": str(self.space_id) if self.space_id else None,
            "allow_join_requests": self.allow_join_requests,
            "storage_quota_mb": self.storage_quota_mb,
            "storage_used_mb": self.storage_used_mb,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
            "metadata": normalize_project_metadata(self.project_metadata),
        }


class ProjectContextPack(Base):
    """Short canonical prompt context for a project."""

    __tablename__ = "project_context_packs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    summary_md = Column(Text, default="", nullable=False)
    goals = Column(JSON, default=list, nullable=False)
    constraints = Column(JSON, default=list, nullable=False)
    current_status = Column(JSON, default=dict, nullable=False)
    active_task_snapshot = Column(JSON, default=list, nullable=False)
    decisions = Column(JSON, default=list, nullable=False)
    open_questions = Column(JSON, default=list, nullable=False)
    manual_notes = Column(Text, default="", nullable=False)
    generated_from = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    project = relationship("Project", back_populates="context_pack")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "summary_md": self.summary_md or "",
            "goals": self.goals or [],
            "constraints": self.constraints or [],
            "current_status": self.current_status or {},
            "active_task_snapshot": self.active_task_snapshot or [],
            "decisions": self.decisions or [],
            "open_questions": self.open_questions or [],
            "manual_notes": self.manual_notes or "",
            "generated_from": self.generated_from or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ContextMemory(Base):
    """General scoped memory for user, project, task, and session context."""

    __tablename__ = "context_memories"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(100), nullable=True, index=True)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    task_id = Column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversation_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    scope_type = Column(String(32), nullable=False, index=True)
    scope_id = Column(String(120), nullable=True, index=True)
    memory_type = Column(String(32), nullable=False, index=True)
    title = Column(String(200), nullable=True)
    _content = Column("content", Text, nullable=False)
    content = _encrypted_text_property("_content", "context_memories.content")
    structured_data = Column(JSON, default=dict, nullable=False)
    source_type = Column(String(32), default="manual", nullable=False)
    source_ref = Column(Text, nullable=True)
    confidence = Column(Float, default=1.0, nullable=False)
    importance = Column(Integer, default=5, nullable=False)
    status = Column(String(32), default="active", nullable=False, index=True)
    is_pinned = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    last_used_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    project = relationship("Project", back_populates="context_memories")

    __table_args__ = (
        Index("ix_context_memories_user_status", "user_id", "status"),
        Index("ix_context_memories_project_status", "project_id", "status"),
        Index("ix_context_memories_task_status", "task_id", "status"),
        Index("ix_context_memories_session_status", "session_id", "status"),
        Index("ix_context_memories_scope", "scope_type", "scope_id"),
        Index("ix_context_memories_pinned_importance", "is_pinned", "importance"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "user_id": self.user_id,
            "project_id": str(self.project_id) if self.project_id else None,
            "task_id": str(self.task_id) if self.task_id else None,
            "session_id": str(self.session_id) if self.session_id else None,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "memory_type": self.memory_type,
            "title": self.title,
            "content": self.content,
            "structured_data": self.structured_data or {},
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "confidence": self.confidence,
            "importance": self.importance,
            "status": self.status,
            "is_pinned": self.is_pinned,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


class ProjectInfoCategory(Base):
    """Project-specific information shelf/category."""

    __tablename__ = "project_info_categories"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key = Column("category_key", String(120), nullable=False)
    label = Column(String(200), nullable=False)
    description = Column(Text)
    status = Column(String(32), default="active", nullable=False, index=True)
    source = Column(String(32), default="template", nullable=False)
    sort_order = Column(Float, default=0, nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    project = relationship("Project", back_populates="info_categories")
    documents = relationship("ProjectDocument", back_populates="category")
    facts = relationship("ProjectFact", back_populates="category")

    __table_args__ = (
        UniqueConstraint("project_id", "category_key", name="uq_project_info_category_key"),
        Index("ix_project_info_categories_project_sort", "project_id", "sort_order"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "status": self.status,
            "source": self.source,
            "sort_order": self.sort_order,
            "created_by": str(self.created_by) if self.created_by else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ProjectDocument(Base):
    """Important project document/catalog entry."""

    __tablename__ = "project_documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    category_id = Column(
        UUID(as_uuid=True),
        ForeignKey("project_info_categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title = Column(String(255), nullable=False)
    description = Column(Text)
    document_type = Column(String(64), default="document", nullable=False)
    target_kind = Column(String(32), default="file", nullable=False)
    file_path = Column(Text)
    record_table_id = Column(
        UUID(as_uuid=True),
        ForeignKey("record_tables.id", ondelete="SET NULL"),
        nullable=True,
    )
    external_url = Column(Text)
    role = Column(String(64), default="reference", nullable=False)
    is_primary = Column(Boolean, default=False, nullable=False)
    ai_access_level = Column(String(32), default="metadata", nullable=False)
    status = Column(String(32), default="active", nullable=False, index=True)
    notes = Column(Text)
    source_type = Column(String(32), default="manual", nullable=False)
    source_ref = Column(Text)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    deleted_at = Column(DateTime, nullable=True, index=True)

    project = relationship("Project", back_populates="documents")
    category = relationship("ProjectInfoCategory", back_populates="documents")
    facts = relationship("ProjectFact", back_populates="source_document")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "category_id": str(self.category_id) if self.category_id else None,
            "title": self.title,
            "description": self.description,
            "document_type": self.document_type,
            "target_kind": self.target_kind,
            "file_path": self.file_path,
            "record_table_id": str(self.record_table_id) if self.record_table_id else None,
            "external_url": self.external_url,
            "role": self.role,
            "is_primary": self.is_primary,
            "ai_access_level": self.ai_access_level,
            "status": self.status,
            "notes": self.notes,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "created_by": str(self.created_by) if self.created_by else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
        }


class ProjectFact(Base):
    """Project fact extracted from documents, tasks, or manual input."""

    __tablename__ = "project_facts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    category_id = Column(
        UUID(as_uuid=True),
        ForeignKey("project_info_categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_document_id = Column(
        UUID(as_uuid=True),
        ForeignKey("project_documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_task_id = Column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    title = Column(String(255), nullable=False)
    _content = Column("content", Text, nullable=False)
    content = _encrypted_text_property("_content", "project_facts.content")
    fact_type = Column(String(64), default="fact", nullable=False)
    confidence = Column(Float, default=1.0, nullable=False)
    importance = Column(Integer, default=5, nullable=False, index=True)
    status = Column(String(32), default="active", nullable=False, index=True)
    source_type = Column(String(32), default="manual", nullable=False)
    source_ref = Column(Text)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    deleted_at = Column(DateTime, nullable=True, index=True)

    project = relationship("Project", back_populates="facts")
    category = relationship("ProjectInfoCategory", back_populates="facts")
    source_document = relationship("ProjectDocument", back_populates="facts")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "category_id": str(self.category_id) if self.category_id else None,
            "source_document_id": (
                str(self.source_document_id) if self.source_document_id else None
            ),
            "source_task_id": str(self.source_task_id) if self.source_task_id else None,
            "title": self.title,
            "content": self.content,
            "fact_type": self.fact_type,
            "confidence": self.confidence,
            "importance": self.importance,
            "status": self.status,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "created_by": str(self.created_by) if self.created_by else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
        }


class ProjectInfoSyncState(Base):
    """Cursor state for incremental project information extraction."""

    __tablename__ = "project_info_sync_states"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_type = Column(String(64), default="tasks", nullable=False)
    last_synced_at = Column(DateTime)
    last_seen_updated_at = Column(DateTime)
    cursor = Column(JSON, default=dict)
    sync_metadata = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    project = relationship("Project", back_populates="info_sync_states")

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "source_type",
            name="uq_project_info_sync_state_source",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "source_type": self.source_type,
            "last_synced_at": (
                self.last_synced_at.isoformat() if self.last_synced_at else None
            ),
            "last_seen_updated_at": (
                self.last_seen_updated_at.isoformat()
                if self.last_seen_updated_at
                else None
            ),
            "cursor": self.cursor or {},
            "sync_metadata": self.sync_metadata or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ProjectMember(Base):
    """プロジェクトメンバー"""

    __tablename__ = "project_members"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    # 役割: 'owner', 'admin', 'member', 'viewer'
    role = Column(String(20), default="member")

    # 権限（JSONで柔軟に管理）
    permissions = Column(
        JSON,
        default=lambda: {
            "read": True,
            "write": True,
            "delete": False,
            "manage_members": False,
        },
    )

    # タイムスタンプ
    joined_at = Column(DateTime, default=datetime.utcnow)
    invited_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))

    # リレーション
    project = relationship("Project", back_populates="members")
    user = relationship("User", foreign_keys=[user_id], backref="project_memberships")
    inviter = relationship("User", foreign_keys=[invited_by])

    __table_args__ = (
        # 同一プロジェクトに同一ユーザーは1回のみ
        UniqueConstraint("project_id", "user_id", name="unique_project_member"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "user_id": str(self.user_id),
            "role": self.role,
            "permissions": self.permissions,
            "joined_at": self.joined_at.isoformat() if self.joined_at else None,
            "invited_by": str(self.invited_by) if self.invited_by else None,
        }


class ProjectJoinRequest(Base):
    """プロジェクト参加申請"""

    __tablename__ = "project_join_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    # 申請内容
    message = Column(Text)  # 申請メッセージ
    status = Column(
        String(20), default="pending", index=True
    )  # 'pending', 'approved', 'rejected'

    # 処理情報
    processed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    processed_at = Column(DateTime)
    rejection_reason = Column(Text)

    # タイムスタンプ
    created_at = Column(DateTime, default=datetime.utcnow)

    # リレーション
    project = relationship("Project", back_populates="join_requests")
    user = relationship("User", foreign_keys=[user_id], backref="join_requests")
    processor = relationship("User", foreign_keys=[processed_by])

    __table_args__ = (
        # 同一プロジェクトに同一ユーザーは申請中は1件のみ
        UniqueConstraint("project_id", "user_id", name="unique_pending_request"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "user_id": str(self.user_id),
            "message": self.message,
            "status": self.status,
            "processed_by": str(self.processed_by) if self.processed_by else None,
            "processed_at": (
                self.processed_at.isoformat() if self.processed_at else None
            ),
            "rejection_reason": self.rejection_reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
