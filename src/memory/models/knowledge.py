"""ナレッジソース・ドキュメント・チャンク・注釈系モデル。"""

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
    UniqueConstraint,
    Index,
    Boolean,
    text,
    Date,
)
from sqlalchemy.dialects.postgresql import CIDR, UUID
from sqlalchemy.orm import relationship

from .base import Base, _encrypted_json_property, _encrypted_text_property


class KnowledgeSource(Base):
    """外部ディレクトリや案件フォルダを扱うナレッジ正本の入口。"""

    __tablename__ = "knowledge_sources"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    root_path = Column(Text, nullable=False)
    source_type = Column(String(40), default="local_dir", nullable=False, index=True)
    # GROWI など外部 Wiki 連携の API トークン（保存時に暗号化）。
    _growi_api_token = Column("growi_api_token", Text, nullable=True)
    growi_api_token = _encrypted_text_property(
        "_growi_api_token", "knowledge_sources.growi_api_token"
    )
    owner_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    access_policy = Column(JSON, default=dict)
    include_patterns = Column(JSON, default=lambda: ["*.md", "*.txt", "*.pdf"])
    exclude_patterns = Column(
        JSON, default=lambda: [".*", "__pycache__", "node_modules"]
    )
    sync_mode = Column(String(20), default="manual", nullable=False)
    write_policy = Column(String(40), default="propose_patch", nullable=False)
    status = Column(String(20), default="created", index=True)
    document_count = Column(Integer, default=0)
    chunk_count = Column(Integer, default=0)
    last_synced_at = Column(DateTime)
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner = relationship(
        "User", foreign_keys=[owner_user_id], backref="knowledge_sources"
    )
    permissions = relationship(
        "KnowledgeSourcePermission",
        back_populates="source",
        cascade="all, delete-orphan",
    )
    documents = relationship(
        "KnowledgeDocument",
        back_populates="source",
        cascade="all, delete-orphan",
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "description": self.description,
            "root_path": self.root_path,
            "source_type": self.source_type,
            "has_api_token": bool(self._growi_api_token),
            "owner_user_id": str(self.owner_user_id) if self.owner_user_id else None,
            "access_policy": self.access_policy or {},
            "include_patterns": self.include_patterns or [],
            "exclude_patterns": self.exclude_patterns or [],
            "sync_mode": self.sync_mode,
            "write_policy": self.write_policy,
            "status": self.status,
            "document_count": self.document_count or 0,
            "chunk_count": self.chunk_count or 0,
            "last_synced_at": (
                self.last_synced_at.isoformat() if self.last_synced_at else None
            ),
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class KnowledgeSourcePermission(Base):
    """ユーザーまたはプロジェクトに対するナレッジソース権限。"""

    __tablename__ = "knowledge_source_permissions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    project_id = Column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE")
    )
    permission = Column(String(20), default="read", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))

    source = relationship("KnowledgeSource", back_populates="permissions")
    user = relationship("User", foreign_keys=[user_id], backref="knowledge_permissions")
    project = relationship("Project", backref="knowledge_permissions")
    creator = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        Index("ix_knowledge_source_permissions_source_user", "source_id", "user_id"),
        Index(
            "ix_knowledge_source_permissions_source_project",
            "source_id",
            "project_id",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "source_id": str(self.source_id),
            "user_id": str(self.user_id) if self.user_id else None,
            "project_id": str(self.project_id) if self.project_id else None,
            "permission": self.permission,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "created_by": str(self.created_by) if self.created_by else None,
        }


class KnowledgeDocument(Base):
    """Knowledge Source配下の正本ファイルの派生メタデータ。"""

    __tablename__ = "knowledge_documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    path = Column(Text, nullable=False)
    resolved_absolute_path = Column(Text)
    title = Column(String(500))
    extension = Column(String(32), index=True)
    mime_type = Column(String(120))
    content_hash = Column(String(64), index=True)
    modified_at = Column(DateTime)
    size_bytes = Column(Integer, default=0)
    frontmatter_json = Column(JSON, default=dict)
    tags = Column(JSON, default=list)
    project_refs = Column(JSON, default=list)
    task_refs = Column(JSON, default=list)
    status = Column(String(20), default="active", index=True)
    last_indexed_at = Column(DateTime)
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    source = relationship("KnowledgeSource", back_populates="documents")
    chunks = relationship(
        "KnowledgeChunk",
        back_populates="document",
        cascade="all, delete-orphan",
    )
    annotations = relationship(
        "KnowledgeAnnotation",
        back_populates="document",
        cascade="all, delete-orphan",
    )
    edit_events = relationship(
        "KnowledgeEditEvent",
        back_populates="document",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("source_id", "path", name="uq_knowledge_document_source_path"),
        Index("ix_knowledge_documents_source_status", "source_id", "status"),
    )

    def to_dict(self, include_source: bool = False) -> Dict[str, Any]:
        data = {
            "id": str(self.id),
            "source_id": str(self.source_id),
            "path": self.path,
            "absolute_path": self.resolved_absolute_path,
            "title": self.title,
            "extension": self.extension,
            "mime_type": self.mime_type,
            "content_hash": self.content_hash,
            "modified_at": self.modified_at.isoformat() if self.modified_at else None,
            "size_bytes": self.size_bytes or 0,
            "frontmatter": self.frontmatter_json or {},
            "tags": self.tags or [],
            "project_refs": self.project_refs or [],
            "task_refs": self.task_refs or [],
            "status": self.status,
            "last_indexed_at": (
                self.last_indexed_at.isoformat() if self.last_indexed_at else None
            ),
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_source and self.__dict__.get("source") is not None:
            data["source"] = self.source.to_dict()
        return data


class KnowledgeChunk(Base):
    """検索用チャンク。本文は正本ではなく再生成可能な派生物。"""

    __tablename__ = "knowledge_chunks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    heading_path = Column(JSON, default=list)
    chunk_index = Column(Integer, nullable=False)
    _text = Column("text", Text, nullable=False)
    text = _encrypted_text_property("_text", "knowledge_chunks.text")
    token_count = Column(Integer, default=0)
    content_hash = Column(String(64), index=True)
    vector_id = Column(String(100))
    metadata_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)

    document = relationship("KnowledgeDocument", back_populates="chunks")

    __table_args__ = (
        UniqueConstraint(
            "document_id", "chunk_index", name="uq_knowledge_chunk_document_index"
        ),
        Index("ix_knowledge_chunks_document_id", "document_id"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "document_id": str(self.document_id),
            "heading_path": self.heading_path or [],
            "chunk_index": self.chunk_index,
            "text": self.text,
            "token_count": self.token_count or 0,
            "content_hash": self.content_hash,
            "vector_id": self.vector_id,
            "metadata": self.metadata_json or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class KnowledgeLink(Base):
    """Markdown/wiki/embed/URLリンクと解決済み参照。"""

    __tablename__ = "knowledge_links"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_document_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_path_or_url = Column(Text, nullable=False)
    link_type = Column(String(20), default="markdown", nullable=False)
    resolved_document_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, default=datetime.utcnow)

    source_document = relationship(
        "KnowledgeDocument", foreign_keys=[source_document_id], backref="outgoing_links"
    )
    resolved_document = relationship(
        "KnowledgeDocument", foreign_keys=[resolved_document_id], backref="incoming_links"
    )

    __table_args__ = (
        Index("ix_knowledge_links_source_document", "source_document_id"),
        Index("ix_knowledge_links_resolved_document", "resolved_document_id"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "source_document_id": str(self.source_document_id),
            "target_path_or_url": self.target_path_or_url,
            "link_type": self.link_type,
            "resolved_document_id": (
                str(self.resolved_document_id) if self.resolved_document_id else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class KnowledgeAnnotation(Base):
    """AI/ユーザー/ルールによる分類・要約・事実候補などの提案。"""

    __tablename__ = "knowledge_annotations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    annotation_type = Column(String(40), nullable=False, index=True)
    content_json = Column(JSON, default=dict)
    confidence = Column(Float)
    source = Column(String(20), default="rule", nullable=False)
    status = Column(String(20), default="proposed", nullable=False, index=True)
    actor_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    document = relationship("KnowledgeDocument", back_populates="annotations")
    actor = relationship("User", foreign_keys=[actor_user_id])

    __table_args__ = (
        Index("ix_knowledge_annotations_document_status", "document_id", "status"),
    )

    def to_dict(self, include_document: bool = False) -> Dict[str, Any]:
        data = {
            "id": str(self.id),
            "document_id": str(self.document_id),
            "annotation_type": self.annotation_type,
            "content": self.content_json or {},
            "confidence": self.confidence,
            "source": self.source,
            "status": self.status,
            "actor_user_id": str(self.actor_user_id) if self.actor_user_id else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_document and self.__dict__.get("document") is not None:
            data["document"] = self.document.to_dict()
        return data


class KnowledgeEditEvent(Base):
    """正本ファイルへ適用する、または適用済みの差分編集イベント。"""

    __tablename__ = "knowledge_edit_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    operation = Column(String(40), nullable=False)
    diff = Column(Text, nullable=False)
    reason = Column(Text)
    status = Column(String(20), default="proposed", nullable=False, index=True)
    pre_hash = Column(String(64))
    post_hash = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)
    applied_at = Column(DateTime)

    document = relationship("KnowledgeDocument", back_populates="edit_events")
    actor = relationship("User", foreign_keys=[actor_user_id])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "document_id": str(self.document_id),
            "actor_user_id": str(self.actor_user_id) if self.actor_user_id else None,
            "operation": self.operation,
            "diff": self.diff,
            "reason": self.reason,
            "status": self.status,
            "pre_hash": self.pre_hash,
            "post_hash": self.post_hash,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "applied_at": self.applied_at.isoformat() if self.applied_at else None,
        }


class KnowledgeWorkspace(Base):
    """AoiTalk DBを正本にするDocsワークスペース。"""

    __tablename__ = "knowledge_workspaces"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    settings_json = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("owner_user_id", name="uq_knowledge_workspaces_owner_user"),
        Index("ix_knowledge_workspaces_owner_user", "owner_user_id"),
    )


class KnowledgeNode(Base):
    """Docs内のTana型アウトライナーノード。"""

    __tablename__ = "knowledge_nodes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_nodes.id", ondelete="CASCADE"))
    root_page_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_nodes.id", ondelete="SET NULL"))
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"))
    system_key = Column(Text)
    title = Column(Text, nullable=False)
    # Web drizzle スキーマと serializeNode が持つ別名配列。DB 列は既存（alembic 済み）で、
    # ここは pull シリアライズ用の ORM ミラー。モバイルは read-only 表示に使う。
    aliases = Column(JSON, default=list, nullable=True)
    description = Column(Text, default="", nullable=False)
    _body_json = Column("body_json", JSON, default=dict, nullable=False)
    body_json = _encrypted_json_property("_body_json", "knowledge_nodes.body_json")
    _body_text = Column("body_text", Text, default="", nullable=False)
    body_text = _encrypted_text_property("_body_text", "knowledge_nodes.body_text")
    node_type = Column(String(40), default="node", nullable=False)
    display_props = Column(JSON, default=dict, nullable=False)
    query_json = Column(JSON)
    view_json = Column(JSON, default=dict, nullable=False)
    day_date = Column(Date)
    sort_order = Column(Float, default=0, nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    updated_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    archived_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "system_key",
            name="uq_knowledge_nodes_workspace_system_key",
        ),
        Index("ix_knowledge_nodes_workspace", "workspace_id"),
        Index("ix_knowledge_nodes_workspace_parent_sort", "workspace_id", "parent_id", "sort_order"),
        Index("ix_knowledge_nodes_workspace_project", "workspace_id", "project_id"),
        Index("ix_knowledge_nodes_workspace_day", "workspace_id", "day_date"),
        Index("ix_knowledge_nodes_root_page", "root_page_id"),
        Index("ix_knowledge_nodes_archived_at", "archived_at"),
    )


class KnowledgeSupertag(Base):
    """Tana Supertag相当の型定義。"""

    __tablename__ = "knowledge_supertags"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_supertag_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_supertags.id", ondelete="SET NULL"),
        nullable=True,
    )
    system_key = Column(Text)
    name = Column(String(120), nullable=False)
    base_type = Column(String(40), default="note", nullable=False)
    description = Column(Text)
    icon = Column(String(64))
    color = Column(String(32))
    template_json = Column(JSON, default=dict, nullable=False)
    pinned_field_ids = Column(JSON, default=list, nullable=False)
    config_json = Column(JSON, default=dict, nullable=False)
    title_template = Column(Text)
    ai_instructions = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_knowledge_supertag_name"),
        UniqueConstraint(
            "workspace_id",
            "system_key",
            name="uq_knowledge_supertags_workspace_system_key",
        ),
        Index("ix_knowledge_supertags_workspace", "workspace_id"),
        Index("ix_knowledge_supertags_parent", "parent_supertag_id"),
        Index("ix_knowledge_supertags_system_key", "system_key"),
    )


class KnowledgeNodeSupertag(Base):
    """NodeとSupertagの多対多関連。"""

    __tablename__ = "knowledge_node_supertags"

    node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    supertag_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_supertags.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at = Column(DateTime, default=datetime.utcnow)
    # 同じ関連キーを再作成した場合も、サーバー側の版を比較できるようにする。
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))

    __table_args__ = (
        Index("ix_knowledge_node_supertags_supertag", "supertag_id"),
    )


class KnowledgeField(Base):
    """Supertagに紐づくフィールド定義。"""

    __tablename__ = "knowledge_fields"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    supertag_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_supertags.id", ondelete="CASCADE"),
        nullable=False,
    )
    system_key = Column(Text)
    name = Column(String(120), nullable=False)
    field_type = Column(String(40), default="text", nullable=False)
    required = Column(Boolean, default=False, nullable=False)
    options_json = Column(JSON, default=dict, nullable=False)
    default_value_json = Column(JSON)
    sort_order = Column(Float, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("supertag_id", "name", name="uq_knowledge_field_name"),
        Index("ix_knowledge_fields_workspace", "workspace_id"),
        Index("ix_knowledge_fields_supertag", "supertag_id"),
        Index("ix_knowledge_fields_system_key", "system_key"),
    )


class KnowledgeSupertagField(Base):
    """Supertagと共有フィールド定義の関連。"""

    __tablename__ = "knowledge_supertag_fields"

    supertag_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_supertags.id", ondelete="CASCADE"),
        primary_key=True,
    )
    field_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_fields.id", ondelete="CASCADE"),
        primary_key=True,
    )
    sort_order = Column(Float, default=0, nullable=False)
    required = Column(Boolean, default=False, nullable=False)
    show_in_template = Column(Boolean, default=True, nullable=False)
    optional = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class KnowledgeNodePlacement(Base):
    """同一ノードを別の親配下へ参照配置する関連。"""

    __tablename__ = "knowledge_node_placements"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    sort_order = Column(Float, default=0, nullable=False)
    collapsed = Column(Boolean, default=False, nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("node_id", "parent_node_id", name="uq_knowledge_node_placement_parent"),
        Index("ix_knowledge_node_placements_parent", "parent_node_id", "sort_order"),
    )


class KnowledgeFieldValue(Base):
    """Nodeごとの型付きフィールド値。"""

    __tablename__ = "knowledge_field_values"

    node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    field_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_fields.id", ondelete="CASCADE"),
        primary_key=True,
    )
    value_json = Column(JSON)
    value_text = Column(Text)
    value_number = Column(Float)
    value_datetime = Column(DateTime)
    target_node_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_nodes.id", ondelete="SET NULL"))
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))

    __table_args__ = (
        Index("ix_knowledge_field_values_field", "field_id"),
        Index("ix_knowledge_field_values_target", "target_node_id"),
        Index("ix_knowledge_field_values_text", "value_text"),
        Index("ix_knowledge_field_values_number", "value_number"),
        Index("ix_knowledge_field_values_datetime", "value_datetime"),
    )


class KnowledgeEdge(Base):
    """Node間の明示的な関係。"""

    __tablename__ = "knowledge_edges"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    relation_type = Column(String(80), default="related_to", nullable=False)
    confidence = Column(Float, default=1, nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_knowledge_edges_source", "source_node_id"),
        Index("ix_knowledge_edges_target", "target_node_id"),
        Index("ix_knowledge_edges_relation", "relation_type"),
    )


class KnowledgeSearchIndex(Base):
    """暗号化済みDocs本文から再生成する検索用派生index。"""

    __tablename__ = "knowledge_search_index"

    node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"))
    title_text = Column(Text, default="", nullable=False)
    body_text_plain = Column(Text, default="", nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_knowledge_search_index_workspace", "workspace_id"),
        Index("ix_knowledge_search_index_project", "project_id"),
    )


class KnowledgeSavedView(Base):
    """タグ別・条件別に保存するDocsビュー定義。"""

    __tablename__ = "knowledge_saved_views"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    supertag_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_supertags.id", ondelete="SET NULL"),
        nullable=True,
    )
    name = Column(String(200), nullable=False)
    layout = Column(String(40), default="table", nullable=False)
    config_json = Column(JSON, default=dict, nullable=False)
    sort_order = Column(Float, default=0, nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_knowledge_saved_views_workspace", "workspace_id"),
        Index("ix_knowledge_saved_views_supertag", "supertag_id"),
    )


class KnowledgeRevision(Base):
    """Docs nodeの編集履歴。"""

    __tablename__ = "knowledge_revisions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    title = Column(Text, nullable=False)
    _body_json = Column("body_json", JSON, default=dict, nullable=False)
    body_json = _encrypted_json_property("_body_json", "knowledge_revisions.body_json")
    _body_text = Column("body_text", Text, default="", nullable=False)
    body_text = _encrypted_text_property("_body_text", "knowledge_revisions.body_text")
    change_summary = Column(Text)
    source_refs_json = Column(JSON, default=list, nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (Index("ix_knowledge_revisions_node", "node_id"),)


class KnowledgeAiSuggestion(Base):
    """AI Organizerの未承認提案。"""

    __tablename__ = "knowledge_ai_suggestions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    node_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_nodes.id", ondelete="CASCADE"))
    suggestion_type = Column(String(80), nullable=False)
    payload_json = Column(JSON, default=dict, nullable=False)
    status = Column(String(20), default="proposed", nullable=False)
    confidence = Column(Float)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_knowledge_ai_suggestions_workspace", "workspace_id"),
        Index("ix_knowledge_ai_suggestions_node", "node_id"),
        Index("ix_knowledge_ai_suggestions_status", "status"),
    )


class KnowledgeAttachment(Base):
    """Docs nodeに紐づく添付ファイルメタデータ。"""

    __tablename__ = "knowledge_attachments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_name = Column(String(255), nullable=False)
    file_path = Column(Text, nullable=False)
    mime_type = Column(String(120))
    size_bytes = Column(Integer)
    attachment_metadata = Column(JSON, default=dict, nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (Index("ix_knowledge_attachments_node", "node_id"),)


class KnowledgeImportJob(Base):
    """汎用Docs Importのジョブ。"""

    __tablename__ = "knowledge_import_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"))
    source_type = Column(String(40), nullable=False)
    source_name = Column(Text, nullable=False)
    status = Column(String(20), default="proposed", nullable=False)
    options_json = Column(JSON, default=dict, nullable=False)
    summary_json = Column(JSON, default=dict, nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_knowledge_import_jobs_workspace", "workspace_id"),
        Index("ix_knowledge_import_jobs_project", "project_id"),
        Index("ix_knowledge_import_jobs_status", "status"),
    )


class KnowledgeImportItem(Base):
    """Importジョブ内の取り込み候補。"""

    __tablename__ = "knowledge_import_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_import_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    node_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_nodes.id", ondelete="SET NULL"))
    source_ref = Column(Text, nullable=False)
    title = Column(Text, nullable=False)
    item_type = Column(String(40), default="page", nullable=False)
    status = Column(String(20), default="proposed", nullable=False)
    preview_json = Column(JSON, default=dict, nullable=False)
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_knowledge_import_items_job", "job_id"),
        Index("ix_knowledge_import_items_node", "node_id"),
        Index("ix_knowledge_import_items_status", "status"),
    )
