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
    CheckConstraint,
)
from sqlalchemy.dialects.postgresql import CIDR, UUID
from sqlalchemy.orm import relationship, synonym

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


class DocsLibrary(Base):
    """AoiTalk DBを正本にするDocs Library。

    ``KnowledgeWorkspace`` and ``workspace_id`` remain compatibility aliases
    for pre-0019 Python/mobile callers.  New code should use ``DocsLibrary``
    and ``docs_library_id``; the database column names are the canonical
    contract after migration ``20260809_0019``.
    """

    __tablename__ = "docs_libraries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    # ``personal`` is the per-user canonical Docs Library.  Project identity
    # is carried by ``knowledge_nodes.project_id`` and the Projects ACL; a
    # library row is never project-scoped after migration 20260809_0020.
    library_type = Column(
        String(32), nullable=False, default="personal", server_default="personal"
    )
    settings_json = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "library_type <> 'project'",
            name="ck_docs_libraries_library_type_not_project",
        ),
        Index("ix_docs_libraries_owner_user", "owner_user_id"),
        Index(
            "uq_docs_libraries_personal_owner",
            "owner_user_id",
            unique=True,
            postgresql_where=text(
                "library_type = 'personal' AND owner_user_id IS NOT NULL"
            ),
        ),
    )

    # Deprecated Python attribute aliases.  These are synonyms rather than
    # duplicate columns, so writes through either spelling stay consistent.
    workspace_type = synonym("library_type")


# Compatibility import for services and mobile sync code that still names the
# pre-0019 type.  ``DocsLibrary`` remains the canonical class name.
KnowledgeWorkspace = DocsLibrary


class KnowledgeNodeShare(Base):
    """Per-user subtree ACL rooted at a Docs node.

    A share on a node applies to that node and descendants.  The service layer
    resolves the subtree through the node parent chain; this table stores only
    the ACL root so permissions cannot silently drift as nodes move.
    """

    __tablename__ = "knowledge_node_shares"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    permission = Column(String(16), nullable=False, default="read", server_default="read")
    created_by = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    node = relationship("KnowledgeNode", back_populates="shares")
    user = relationship("User", foreign_keys=[user_id], backref="knowledge_node_shares")
    creator = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        UniqueConstraint("node_id", "user_id", name="uq_knowledge_node_shares_node_user"),
        CheckConstraint(
            "permission IN ('read', 'write')",
            name="ck_knowledge_node_shares_permission",
        ),
        Index("ix_knowledge_node_shares_node", "node_id"),
        Index("ix_knowledge_node_shares_user", "user_id"),
    )


class KnowledgeNode(Base):
    """Docs内のTana型アウトライナーノード。"""

    __tablename__ = "knowledge_nodes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    docs_library_id = Column(
        UUID(as_uuid=True),
        ForeignKey("docs_libraries.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id = synonym("docs_library_id")
    parent_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_nodes.id", ondelete="CASCADE"))
    root_page_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_nodes.id", ondelete="SET NULL"))
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"))
    app_id = Column(
        UUID(as_uuid=True),
        ForeignKey("apps.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
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

    shares = relationship(
        "KnowledgeNodeShare",
        back_populates="node",
        cascade="all, delete-orphan",
    )
    project_references = relationship(
        "ProjectKnowledgeRef",
        back_populates="knowledge_node",
    )
    docs_candidates = relationship(
        "DocsCandidate",
        foreign_keys="DocsCandidate.target_node_id",
        back_populates="target_node",
    )

    __table_args__ = (
        UniqueConstraint(
            "docs_library_id",
            "system_key",
            name="uq_knowledge_nodes_docs_library_system_key",
        ),
        Index("ix_knowledge_nodes_docs_library", "docs_library_id"),
        Index("ix_knowledge_nodes_docs_library_parent_sort", "docs_library_id", "parent_id", "sort_order"),
        Index("ix_knowledge_nodes_docs_library_project", "docs_library_id", "project_id"),
        Index("ix_knowledge_nodes_docs_library_day", "docs_library_id", "day_date"),
        Index("ix_knowledge_nodes_root_page", "root_page_id"),
        Index("ix_knowledge_nodes_archived_at", "archived_at"),
    )


class DocsCandidate(Base):
    """Reviewable, bounded suggestion before it becomes canonical Project Docs.

    Candidates intentionally contain only a sanitized structured suggestion;
    raw conversation/assistant transcripts never belong in this table.
    """

    __tablename__ = "docs_candidates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_type = Column(String(64), nullable=False, default="dreaming_auto")
    _content_json = Column("content_json", JSON, default=dict, nullable=False)
    content_json = _encrypted_json_property(
        "_content_json", "docs_candidates.content_json"
    )
    confidence = Column(Float, nullable=False, default=0.0)
    importance = Column(Integer, nullable=False, default=1)
    sensitivity = Column(String(32), nullable=False, default="normal")
    evidence_hash = Column(String(64), nullable=True)
    evidence_span = Column(String(500), nullable=True)
    source_job_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scoped_memory_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Stable retry key for one extractor item.  It is populated only for
    # durable jobs; rejected/superseded rows must not block a later retry.
    dedupe_key = Column(String(128), nullable=True)
    status = Column(String(16), nullable=False, default="proposed")
    version = Column(Integer, nullable=False, default=1)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    project = relationship(
        "Project", foreign_keys=[project_id], backref="docs_candidates"
    )
    target_node = relationship(
        "KnowledgeNode",
        foreign_keys=[target_node_id],
        back_populates="docs_candidates",
    )
    source_job = relationship("ScopedMemoryJob", foreign_keys=[source_job_id])
    creator = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'superseded')",
            name="ck_docs_candidates_status",
        ),
        Index("ix_docs_candidates_project_status", "project_id", "status"),
        Index("ix_docs_candidates_source_job", "source_job_id"),
        Index("ix_docs_candidates_target_status", "target_node_id", "status"),
        Index(
            "uq_docs_candidates_active_dedupe",
            "dedupe_key",
            unique=True,
            postgresql_where=(status == "proposed") & dedupe_key.isnot(None),
            sqlite_where=(status == "proposed") & dedupe_key.isnot(None),
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Return compact metadata and the already-sanitized suggestion only."""

        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "target_node_id": str(self.target_node_id) if self.target_node_id else None,
            "source_type": self.source_type,
            "content": self.content_json or {},
            "confidence": self.confidence,
            "importance": self.importance,
            "sensitivity": self.sensitivity,
            "evidence_hash": self.evidence_hash,
            # The exact evidence span remains an internal validator input;
            # list/create/approve DTOs expose only its presence and hash so a
            # project reader cannot receive raw turn text from this queue.
            "has_evidence": bool(self.evidence_span or self.evidence_hash),
            "source_job_id": str(self.source_job_id) if self.source_job_id else None,
            "status": self.status,
            "version": self.version,
            "created_by": str(self.created_by) if self.created_by else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class KnowledgeSupertag(Base):
    """Tana Supertag相当の型定義。"""

    __tablename__ = "knowledge_supertags"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    docs_library_id = Column(
        UUID(as_uuid=True),
        ForeignKey("docs_libraries.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id = synonym("docs_library_id")
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
        UniqueConstraint("docs_library_id", "name", name="uq_knowledge_supertag_name"),
        UniqueConstraint(
            "docs_library_id",
            "system_key",
            name="uq_knowledge_supertags_docs_library_system_key",
        ),
        Index("ix_knowledge_supertags_docs_library", "docs_library_id"),
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
    docs_library_id = Column(
        UUID(as_uuid=True),
        ForeignKey("docs_libraries.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id = synonym("docs_library_id")
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
        Index("ix_knowledge_fields_docs_library", "docs_library_id"),
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
        # value_text はメール本文や References など数KBになる値も保持するため、
        # 生の列に btree index を張ると index row size 上限(2704 bytes)超過で
        # INSERT 自体が失敗する。先頭のみを対象にした式indexで上限を回避する。
        Index(
            "ix_knowledge_field_values_text",
            text("left(value_text, 500)"),
        ),
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
    docs_library_id = Column(
        UUID(as_uuid=True),
        ForeignKey("docs_libraries.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id = synonym("docs_library_id")
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"))
    title_text = Column(Text, default="", nullable=False)
    body_text_plain = Column(Text, default="", nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_knowledge_search_index_docs_library", "docs_library_id"),
        Index("ix_knowledge_search_index_project", "project_id"),
    )


class KnowledgeSavedView(Base):
    """タグ別・条件別に保存するDocsビュー定義。"""

    __tablename__ = "knowledge_saved_views"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    docs_library_id = Column(
        UUID(as_uuid=True),
        ForeignKey("docs_libraries.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id = synonym("docs_library_id")
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
        Index("ix_knowledge_saved_views_docs_library", "docs_library_id"),
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


class ClipIngestReceipt(Base):
    """Encrypted, per-topic audit receipt for one ClipIngest operation.

    ``topic_node_id`` is the ACL anchor.  ``docs_library_id`` is duplicated as
    a scope fence so a malformed row cannot become readable merely because its
    topic UUID happens to exist.  The three encrypted fields deliberately use
    the canonical storage columns themselves; no alternate raw-source column
    exists.
    """

    __tablename__ = "docs_clip_ingest_receipts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    docs_library_id = Column(
        UUID(as_uuid=True),
        ForeignKey("docs_libraries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    topic_node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action = Column(String(32), nullable=False)

    # The ORM-facing properties decrypt on read and encrypt on assignment.
    # Keep the underlying attribute names private while retaining the exact
    # canonical DB column names required by the wire/data contract.
    _source_text = Column("source_text", Text, nullable=False)
    source_text = _encrypted_text_property(
        "_source_text", "docs_clip_ingest_receipts.source_text"
    )
    source_sha256 = Column(String(64), nullable=False, index=True)
    _request_json = Column("request_json", JSON, nullable=False, default=dict)
    request_json = _encrypted_json_property(
        "_request_json", "docs_clip_ingest_receipts.request_json"
    )
    _result_json = Column("result_json", JSON, nullable=False, default=dict)
    result_json = _encrypted_json_property(
        "_result_json", "docs_clip_ingest_receipts.result_json"
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "action IN ('create', 'append', 'duplicate_skip')",
            name="ck_docs_clip_ingest_receipts_action",
        ),
    )

    library = relationship("DocsLibrary", foreign_keys=[docs_library_id])
    actor = relationship("User", foreign_keys=[actor_user_id])
    target_node = relationship("KnowledgeNode", foreign_keys=[target_node_id])
    topic_node = relationship("KnowledgeNode", foreign_keys=[topic_node_id])

    # Scope aliases are Python compatibility only; they do not add columns or
    # change the canonical table/field names.
    library_id = synonym("docs_library_id")
    target_id = synonym("target_node_id")
    topic_id = synonym("topic_node_id")

    @property
    def user_id(self) -> Any:
        """Deprecated compatibility alias for ``actor_user_id``."""

        return self.actor_user_id

    @user_id.setter
    def user_id(self, value: Any) -> None:
        self.actor_user_id = value

    @property
    def original_source(self) -> str | None:
        """Deprecated Python alias; source is stored only in ``source_text``."""

        return self.source_text

    @original_source.setter
    def original_source(self, value: Any) -> None:
        self.source_text = value

    @property
    def original_text(self) -> str | None:
        """Deprecated Python alias; source is stored only in ``source_text``."""

        return self.source_text

    @original_text.setter
    def original_text(self, value: Any) -> None:
        self.source_text = value

    @property
    def source(self) -> str | None:
        """Deprecated Python alias; source is stored only in ``source_text``."""

        return self.source_text

    @source.setter
    def source(self, value: Any) -> None:
        self.source_text = value

    @property
    def original_sha256(self) -> str | None:
        """Deprecated Python alias for the canonical source hash."""

        return self.source_sha256

    @original_sha256.setter
    def original_sha256(self, value: Any) -> None:
        self.source_sha256 = value

    @property
    def result(self) -> Dict[str, Any]:
        """Compatibility view of the encrypted result snapshot."""

        return self.result_json or {}

    @result.setter
    def result(self, value: Any) -> None:
        self.result_json = value or {}

    def to_dict(self, *, include_source_text: bool = False) -> Dict[str, Any]:
        """Serialize canonical receipt fields without leaking source by default."""

        data: Dict[str, Any] = {
            "id": str(self.id),
            "docs_library_id": str(self.docs_library_id)
            if self.docs_library_id
            else None,
            "topic_node_id": str(self.topic_node_id)
            if self.topic_node_id
            else None,
            "target_node_id": str(self.target_node_id)
            if self.target_node_id
            else None,
            "actor_user_id": str(self.actor_user_id)
            if self.actor_user_id
            else None,
            "action": self.action,
            "source_sha256": self.source_sha256,
            "request_json": dict(self.request_json or {}),
            "result_json": dict(self.result_json or {}),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_source_text:
            data["source_text"] = self.source_text or ""
        return data


class DocsClipIngestJob(Base):
    """Durable server-owned lifecycle for one Docs ClipIngest request.

    The request body is intentionally kept in encrypted columns.  A job row is
    an in-flight operation ledger, not a replacement for
    :class:`ClipIngestReceipt`; the latter remains the immutable success audit
    record.  ``lease_*`` fields fence concurrent/restarted workers while the
    actor/scope columns provide a narrow authorization boundary for the API.
    """

    __tablename__ = "docs_clip_ingest_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    docs_library_id = Column(
        UUID(as_uuid=True), ForeignKey("docs_libraries.id", ondelete="SET NULL"), nullable=True, index=True
    )
    session_id = Column(
        UUID(as_uuid=True), ForeignKey("conversation_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    project_id = Column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_node_id = Column(
        UUID(as_uuid=True), ForeignKey("knowledge_nodes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    retry_of_job_id = Column(
        UUID(as_uuid=True), ForeignKey("docs_clip_ingest_jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    receipt_id = Column(
        UUID(as_uuid=True), ForeignKey("docs_clip_ingest_receipts.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # ``source_text`` and snapshots are field-crypto values.  Never select or
    # serialize their private storage columns directly at an API boundary.
    _source_text = Column("source_text", Text, nullable=False)
    source_text = _encrypted_text_property(
        "_source_text", "docs_clip_ingest_jobs.source_text"
    )
    source_sha256 = Column(String(64), nullable=False, index=True)
    _upload_ids_json = Column("upload_ids_json", JSON, nullable=False, default=list)
    upload_ids_json = _encrypted_json_property(
        "_upload_ids_json", "docs_clip_ingest_jobs.upload_ids_json"
    )
    _request_json = Column("request_json", JSON, nullable=False, default=dict)
    request_json = _encrypted_json_property(
        "_request_json", "docs_clip_ingest_jobs.request_json"
    )
    _result_json = Column("result_json", JSON, nullable=False, default=dict)
    result_json = _encrypted_json_property(
        "_result_json", "docs_clip_ingest_jobs.result_json"
    )
    _error_json = Column("error_json", JSON, nullable=False, default=dict)
    error_json = _encrypted_json_property(
        "_error_json", "docs_clip_ingest_jobs.error_json"
    )

    status = Column(String(16), nullable=False, default="queued", server_default="queued", index=True)
    idempotency_key = Column(String(128), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    retryable = Column(Boolean, nullable=False, default=True, server_default="true")
    lease_owner = Column(String(160), nullable=True)
    lease_token = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    actor = relationship("User", foreign_keys=[actor_user_id])
    library = relationship("DocsLibrary", foreign_keys=[docs_library_id])
    target_node = relationship("KnowledgeNode", foreign_keys=[target_node_id])
    receipt = relationship("ClipIngestReceipt", foreign_keys=[receipt_id])
    retry_of = relationship(
        "DocsClipIngestJob", remote_side=[id], foreign_keys=[retry_of_job_id], uselist=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_docs_clip_ingest_jobs_status",
        ),
        UniqueConstraint(
            "actor_user_id", "idempotency_key",
            name="uq_docs_clip_ingest_jobs_actor_idempotency",
        ),
        Index("ix_docs_clip_ingest_jobs_claim", "status", "lease_expires_at", "created_at"),
    )

    @property
    def error(self) -> dict[str, Any]:
        return dict(self.error_json or {})

    @error.setter
    def error(self, value: Any) -> None:
        self.error_json = value if isinstance(value, dict) else {}

    def to_dict(self) -> Dict[str, Any]:
        """Safe wire snapshot; encrypted source/request bodies stay private."""

        result = self.result_json if isinstance(self.result_json, dict) else {}
        error = self.error_json if isinstance(self.error_json, dict) else {}
        return {
            "id": str(self.id),
            "job_id": str(self.id),
            "actor_user_id": str(self.actor_user_id) if self.actor_user_id else None,
            "docs_library_id": str(self.docs_library_id) if self.docs_library_id else None,
            "session_id": str(self.session_id) if self.session_id else None,
            "project_id": str(self.project_id) if self.project_id else None,
            "target_node_id": str(self.target_node_id) if self.target_node_id else None,
            "retry_of_job_id": str(self.retry_of_job_id) if self.retry_of_job_id else None,
            "receipt_id": str(self.receipt_id) if self.receipt_id else None,
            "source_sha256": self.source_sha256,
            "upload_ids": list(self.upload_ids_json or []),
            "status": self.status,
            "idempotency_key": self.idempotency_key,
            "attempt": int(self.attempt_count or 0),
            "attempt_count": int(self.attempt_count or 0),
            "retryable": bool(self.retryable),
            "result": result,
            "result_json": result,
            "error": error,
            "error_json": error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class KnowledgeAiSuggestion(Base):
    """AI Organizerの未承認提案。"""

    __tablename__ = "knowledge_ai_suggestions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    docs_library_id = Column(
        UUID(as_uuid=True),
        ForeignKey("docs_libraries.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id = synonym("docs_library_id")
    node_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_nodes.id", ondelete="CASCADE"))
    suggestion_type = Column(String(80), nullable=False)
    payload_json = Column(JSON, default=dict, nullable=False)
    status = Column(String(20), default="proposed", nullable=False)
    confidence = Column(Float)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_knowledge_ai_suggestions_docs_library", "docs_library_id"),
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
    docs_library_id = Column(
        UUID(as_uuid=True),
        ForeignKey("docs_libraries.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id = synonym("docs_library_id")
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
        Index("ix_knowledge_import_jobs_docs_library", "docs_library_id"),
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
