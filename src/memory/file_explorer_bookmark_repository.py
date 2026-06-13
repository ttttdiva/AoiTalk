"""Repository for per-user file explorer bookmarks."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import FileExplorerBookmark


class FileExplorerBookmarkRepository:
    """Database access for file explorer bookmarks."""

    @staticmethod
    async def list_for_user(
        session: AsyncSession, user_id: UUID
    ) -> List[FileExplorerBookmark]:
        query = (
            select(FileExplorerBookmark)
            .where(FileExplorerBookmark.user_id == user_id)
            .order_by(
                FileExplorerBookmark.sort_order.asc(),
                FileExplorerBookmark.created_at.asc(),
            )
        )
        result = await session.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def get_by_path(
        session: AsyncSession, user_id: UUID, path: str
    ) -> Optional[FileExplorerBookmark]:
        query = select(FileExplorerBookmark).where(
            FileExplorerBookmark.user_id == user_id,
            FileExplorerBookmark.path == path,
        )
        result = await session.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def add(
        session: AsyncSession,
        user_id: UUID,
        name: str,
        path: str,
        icon: str = "📁",
    ) -> FileExplorerBookmark:
        existing = await FileExplorerBookmarkRepository.get_by_path(
            session, user_id, path
        )
        if existing:
            raise ValueError("このパスは既にブックマークされています")

        max_sort_query = select(func.max(FileExplorerBookmark.sort_order)).where(
            FileExplorerBookmark.user_id == user_id
        )
        max_sort = (await session.execute(max_sort_query)).scalar()
        next_sort = float(max_sort or 0) + 1

        bookmark = FileExplorerBookmark(
            user_id=user_id,
            name=name,
            path=path,
            icon=icon,
            sort_order=next_sort,
        )
        session.add(bookmark)
        await session.commit()
        await session.refresh(bookmark)
        return bookmark

    @staticmethod
    async def remove_by_path(session: AsyncSession, user_id: UUID, path: str) -> bool:
        stmt = delete(FileExplorerBookmark).where(
            FileExplorerBookmark.user_id == user_id,
            FileExplorerBookmark.path == path,
        )
        result = await session.execute(stmt)
        await session.commit()
        return bool(result.rowcount)

    @staticmethod
    async def update(
        session: AsyncSession,
        user_id: UUID,
        bookmark_id: UUID,
        *,
        name: Optional[str] = None,
        icon: Optional[str] = None,
        sort_order: Optional[float] = None,
    ) -> Optional[FileExplorerBookmark]:
        query = select(FileExplorerBookmark).where(
            FileExplorerBookmark.id == bookmark_id,
            FileExplorerBookmark.user_id == user_id,
        )
        result = await session.execute(query)
        bookmark = result.scalar_one_or_none()
        if not bookmark:
            return None

        if name is not None:
            bookmark.name = name
        if icon is not None:
            bookmark.icon = icon
        if sort_order is not None:
            bookmark.sort_order = sort_order
        bookmark.updated_at = datetime.utcnow()

        await session.commit()
        await session.refresh(bookmark)
        return bookmark
