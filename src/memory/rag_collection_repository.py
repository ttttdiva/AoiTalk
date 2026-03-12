"""
Repository for RAG Collection management
"""

import uuid as uuid_mod
import logging
from datetime import datetime
from typing import List, Optional, Dict, Any
from uuid import UUID
from sqlalchemy import select, delete, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from .models import RagCollection, ProjectRagCollection, UserRagCollection, ProjectMember

logger = logging.getLogger(__name__)


def _generate_collection_name() -> str:
    """Generate unique Qdrant collection name."""
    return f"col_{uuid_mod.uuid4().hex[:12]}_documents"


class RagCollectionRepository:
    """Repository for managing RAG collections and project linkages"""

    # ─── Collection CRUD ─────────────────────────────────────────────────

    @staticmethod
    async def create_collection(
        session: AsyncSession,
        created_by: UUID,
        name: str,
        description: Optional[str] = None,
        source_directory: Optional[str] = None,
        include_patterns: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
    ) -> RagCollection:
        """Create a new RAG collection record."""
        collection = RagCollection(
            name=name,
            description=description,
            collection_name=_generate_collection_name(),
            source_directory=source_directory,
            include_patterns=include_patterns or ["*.md", "*.txt", "*.pdf"],
            exclude_patterns=exclude_patterns or [".*", "__pycache__"],
            created_by=created_by,
        )
        session.add(collection)
        await session.commit()
        await session.refresh(collection)
        return collection

    @staticmethod
    async def get_by_id(
        session: AsyncSession, collection_id: UUID
    ) -> Optional[RagCollection]:
        """Get collection by ID."""
        result = await session.execute(
            select(RagCollection).where(RagCollection.id == collection_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_collection_name(
        session: AsyncSession, collection_name: str
    ) -> Optional[RagCollection]:
        """Get collection by Qdrant collection name."""
        result = await session.execute(
            select(RagCollection).where(RagCollection.collection_name == collection_name)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def list_all(session: AsyncSession) -> List[Dict[str, Any]]:
        """List all RAG collections."""
        result = await session.execute(
            select(RagCollection).order_by(RagCollection.created_at.desc())
        )
        return [c.to_dict() for c in result.scalars().all()]

    @staticmethod
    async def update_collection(
        session: AsyncSession, collection_id: UUID, **kwargs
    ) -> Optional[RagCollection]:
        """Update collection fields (name, description only)."""
        collection = await RagCollectionRepository.get_by_id(session, collection_id)
        if not collection:
            return None

        allowed_fields = {"name", "description"}
        for key, value in kwargs.items():
            if key in allowed_fields and value is not None:
                setattr(collection, key, value)

        collection.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(collection)
        return collection

    @staticmethod
    async def delete_collection(
        session: AsyncSession, collection_id: UUID
    ) -> bool:
        """Delete a collection record (does NOT delete the Qdrant collection)."""
        collection = await RagCollectionRepository.get_by_id(session, collection_id)
        if not collection:
            return False

        await session.delete(collection)
        await session.commit()
        return True

    @staticmethod
    async def update_status(
        session: AsyncSession,
        collection_id: UUID,
        status: str,
        points_count: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> bool:
        """Update collection indexing status."""
        collection = await RagCollectionRepository.get_by_id(session, collection_id)
        if not collection:
            return False

        collection.status = status
        if points_count is not None:
            collection.points_count = points_count
        if status == "ready":
            collection.last_indexed_at = datetime.utcnow()
            collection.error_message = None
        elif status == "error":
            collection.error_message = error_message
        collection.updated_at = datetime.utcnow()

        await session.commit()
        return True

    # ─── Project Linkage ─────────────────────────────────────────────────

    @staticmethod
    async def link_to_project(
        session: AsyncSession,
        collection_id: UUID,
        project_id: UUID,
        linked_by: Optional[UUID] = None,
    ) -> Optional[ProjectRagCollection]:
        """Link a collection to a project."""
        # Check for existing link
        result = await session.execute(
            select(ProjectRagCollection).where(
                and_(
                    ProjectRagCollection.project_id == project_id,
                    ProjectRagCollection.collection_id == collection_id,
                )
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing  # Already linked

        link = ProjectRagCollection(
            project_id=project_id,
            collection_id=collection_id,
            linked_by=linked_by,
        )
        session.add(link)
        await session.commit()
        await session.refresh(link)
        return link

    @staticmethod
    async def unlink_from_project(
        session: AsyncSession, collection_id: UUID, project_id: UUID
    ) -> bool:
        """Remove linkage between collection and project."""
        result = await session.execute(
            delete(ProjectRagCollection).where(
                and_(
                    ProjectRagCollection.project_id == project_id,
                    ProjectRagCollection.collection_id == collection_id,
                )
            )
        )
        await session.commit()
        return result.rowcount > 0

    @staticmethod
    async def get_project_collections(
        session: AsyncSession, project_id: UUID
    ) -> List[Dict[str, Any]]:
        """Get all collections linked to a project."""
        result = await session.execute(
            select(RagCollection, ProjectRagCollection)
            .join(
                ProjectRagCollection,
                RagCollection.id == ProjectRagCollection.collection_id,
            )
            .where(ProjectRagCollection.project_id == project_id)
            .order_by(RagCollection.name)
        )
        rows = result.all()
        collections = []
        for collection, link in rows:
            d = collection.to_dict()
            d["is_active"] = link.is_active
            d["linked_at"] = link.linked_at.isoformat() if link.linked_at else None
            collections.append(d)
        return collections

    @staticmethod
    async def get_active_collection_names_for_project(
        session: AsyncSession, project_id: UUID
    ) -> List[str]:
        """Get active Qdrant collection names for a project (async)."""
        result = await session.execute(
            select(RagCollection.collection_name)
            .join(
                ProjectRagCollection,
                RagCollection.id == ProjectRagCollection.collection_id,
            )
            .where(
                and_(
                    ProjectRagCollection.project_id == project_id,
                    ProjectRagCollection.is_active == True,
                )
            )
        )
        return [row[0] for row in result.all()]

    @staticmethod
    def get_active_collection_names_for_project_sync(
        session: Session, project_id: str
    ) -> List[str]:
        """Get active Qdrant collection names for a project (sync, for search_rag)."""
        result = session.execute(
            select(RagCollection.collection_name)
            .join(
                ProjectRagCollection,
                RagCollection.id == ProjectRagCollection.collection_id,
            )
            .where(
                and_(
                    ProjectRagCollection.project_id == UUID(project_id),
                    ProjectRagCollection.is_active == True,
                )
            )
        )
        return [row[0] for row in result.all()]

    # ─── User Linkage ────────────────────────────────────────────────────

    @staticmethod
    async def link_to_user(
        session: AsyncSession,
        collection_id: UUID,
        user_id: UUID,
        linked_by: Optional[UUID] = None,
        permission: str = "read",
    ) -> Optional[UserRagCollection]:
        """ユーザーにコレクションを直接紐付け"""
        result = await session.execute(
            select(UserRagCollection).where(
                and_(
                    UserRagCollection.user_id == user_id,
                    UserRagCollection.collection_id == collection_id,
                )
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.permission = permission
            await session.commit()
            await session.refresh(existing)
            return existing

        link = UserRagCollection(
            user_id=user_id,
            collection_id=collection_id,
            linked_by=linked_by,
            permission=permission,
        )
        session.add(link)
        await session.commit()
        await session.refresh(link)
        return link

    @staticmethod
    async def unlink_from_user(
        session: AsyncSession, collection_id: UUID, user_id: UUID
    ) -> bool:
        """ユーザーからコレクションの直接紐付けを解除"""
        result = await session.execute(
            delete(UserRagCollection).where(
                and_(
                    UserRagCollection.user_id == user_id,
                    UserRagCollection.collection_id == collection_id,
                )
            )
        )
        await session.commit()
        return result.rowcount > 0

    @staticmethod
    async def get_collection_user_links(
        session: AsyncSession, collection_id: UUID
    ) -> List[Dict[str, Any]]:
        """コレクションに直接紐付いたユーザー一覧"""
        from .models import User
        result = await session.execute(
            select(UserRagCollection, User)
            .join(User, UserRagCollection.user_id == User.id)
            .where(UserRagCollection.collection_id == collection_id)
            .order_by(User.username)
        )
        links = []
        for link, user in result.all():
            d = link.to_dict()
            d["username"] = user.username
            d["display_name"] = user.display_name
            links.append(d)
        return links

    @staticmethod
    async def get_user_linked_collections(
        session: AsyncSession, user_id: UUID
    ) -> List[Dict[str, Any]]:
        """ユーザーに直接紐付いたコレクション一覧（紐付け管理UI用）"""
        result = await session.execute(
            select(RagCollection, UserRagCollection)
            .join(UserRagCollection, RagCollection.id == UserRagCollection.collection_id)
            .where(UserRagCollection.user_id == user_id)
            .order_by(RagCollection.name)
        )
        collections = []
        for collection, link in result.all():
            d = collection.to_dict()
            d["permission"] = link.permission
            d["linked_at"] = link.linked_at.isoformat() if link.linked_at else None
            collections.append(d)
        return collections

    # ─── Access Control ──────────────────────────────────────────────────

    @staticmethod
    async def list_accessible_collections(
        session: AsyncSession, user_id: UUID
    ) -> List[Dict[str, Any]]:
        """ユーザーがアクセス可能な全コレクションを取得（3パスのUNION）"""
        user_linked = (
            select(UserRagCollection.collection_id)
            .where(UserRagCollection.user_id == user_id)
        )
        project_linked = (
            select(ProjectRagCollection.collection_id)
            .join(ProjectMember, ProjectRagCollection.project_id == ProjectMember.project_id)
            .where(
                and_(
                    ProjectMember.user_id == user_id,
                    ProjectRagCollection.is_active == True,
                )
            )
        )
        query = (
            select(RagCollection)
            .where(
                or_(
                    RagCollection.created_by == user_id,
                    RagCollection.id.in_(user_linked),
                    RagCollection.id.in_(project_linked),
                )
            )
            .order_by(RagCollection.created_at.desc())
        )
        result = await session.execute(query)
        return [c.to_dict() for c in result.scalars().unique().all()]

    @staticmethod
    async def can_user_access_collection(
        session: AsyncSession, user_id: UUID, collection_id: UUID
    ) -> bool:
        """ユーザーがコレクションにアクセス可能か（3パスいずれかでTrue）"""
        # パス1: 作成者
        creator_check = await session.execute(
            select(RagCollection.id).where(
                and_(RagCollection.id == collection_id, RagCollection.created_by == user_id)
            )
        )
        if creator_check.scalar_one_or_none():
            return True

        # パス2: 直接紐付け
        user_link_check = await session.execute(
            select(UserRagCollection.id).where(
                and_(
                    UserRagCollection.collection_id == collection_id,
                    UserRagCollection.user_id == user_id,
                )
            )
        )
        if user_link_check.scalar_one_or_none():
            return True

        # パス3: プロジェクト経由
        project_check = await session.execute(
            select(ProjectRagCollection.id)
            .join(ProjectMember, ProjectRagCollection.project_id == ProjectMember.project_id)
            .where(
                and_(
                    ProjectRagCollection.collection_id == collection_id,
                    ProjectMember.user_id == user_id,
                    ProjectRagCollection.is_active == True,
                )
            )
        )
        if project_check.scalar_one_or_none():
            return True

        return False

    @staticmethod
    async def has_write_permission(
        session: AsyncSession, user_id: UUID, collection_id: UUID
    ) -> bool:
        """ユーザーがコレクションへのwrite権限を持つか"""
        # パス1: 作成者は常にwrite
        creator_check = await session.execute(
            select(RagCollection.id).where(
                and_(RagCollection.id == collection_id, RagCollection.created_by == user_id)
            )
        )
        if creator_check.scalar_one_or_none():
            return True

        # パス2: 直接紐付けでpermission='write'
        user_link_check = await session.execute(
            select(UserRagCollection.id).where(
                and_(
                    UserRagCollection.collection_id == collection_id,
                    UserRagCollection.user_id == user_id,
                    UserRagCollection.permission == "write",
                )
            )
        )
        if user_link_check.scalar_one_or_none():
            return True

        # パス3: プロジェクト経由でロールがowner/admin
        project_check = await session.execute(
            select(ProjectRagCollection.id)
            .join(ProjectMember, ProjectRagCollection.project_id == ProjectMember.project_id)
            .where(
                and_(
                    ProjectRagCollection.collection_id == collection_id,
                    ProjectMember.user_id == user_id,
                    ProjectRagCollection.is_active == True,
                    ProjectMember.role.in_(["owner", "admin"]),
                )
            )
        )
        if project_check.scalar_one_or_none():
            return True

        return False
