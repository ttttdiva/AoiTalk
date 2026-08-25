"""Repository for scoped file explorer bookmarks.

Bookmarks have two deliberately different ownership modes:

``shared``
    The collection belongs to a Space.  Shared rows deliberately keep
    ``user_id IS NULL``; that column remains available for legacy personal
    ownership and must not be used as the Space visibility boundary.

``personal``
    The legacy user-owned collection.  Personal rows are identified by the
    authenticated user *and* ``space_id IS NULL``.  The latter predicate is
    important once shared rows are stored in the same table: an item UUID must
    not be enough to reach a row from another scope.

The keyword ``scope`` and ``space_id`` arguments are accepted by every public
operation.  The old ``*_for_user``/positional signatures remain as a
compatibility layer for callers that have not migrated yet.
"""

from __future__ import annotations

from datetime import datetime
import math
from typing import List, Optional
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import FileExplorerBookmark

BOOKMARK_FOLDER_PATH_PREFIX = "aoitalk-bookmark-folder:"
KIND_BOOKMARK = "bookmark"
KIND_FOLDER = "folder"
_VALID_KINDS = frozenset({KIND_BOOKMARK, KIND_FOLDER})
_MAX_PARENT_DEPTH = 512
SCOPE_SHARED = "shared"
SCOPE_PERSONAL = "personal"
_VALID_SCOPES = frozenset({SCOPE_SHARED, SCOPE_PERSONAL})


def bookmark_folder_path(folder_id: UUID) -> str:
    return f"{BOOKMARK_FOLDER_PATH_PREFIX}{folder_id}"


class FileExplorerBookmarkRepository:
    """Database access for file explorer bookmarks."""

    @staticmethod
    def _resolve_scope(
        user_id: UUID | None,
        *,
        scope: str | None,
        space_id: UUID | None,
    ) -> tuple[str, UUID | None]:
        """Validate and normalize the collection identity.

        Omitting ``scope`` preserves the pre-Space API: a supplied ``space_id``
        means shared and otherwise the authenticated user means personal.
        Explicit personal scope rejects a non-null Space so a caller cannot
        accidentally query an ambiguous collection.
        """

        resolved_scope = str(scope or "").strip().lower() if scope is not None else None
        if resolved_scope is None:
            resolved_scope = SCOPE_SHARED if space_id is not None else SCOPE_PERSONAL
        if resolved_scope not in _VALID_SCOPES:
            raise ValueError("scopeは shared または personal を指定してください")
        if resolved_scope == SCOPE_SHARED:
            if space_id is None:
                raise ValueError("shared scopeにはspace_idが必要です")
            return resolved_scope, space_id
        if space_id is not None:
            raise ValueError("personal scopeのspace_idはNULLで指定してください")
        if user_id is None:
            raise ValueError("personal scopeにはauthenticated user_idが必要です")
        return resolved_scope, None

    @staticmethod
    def _scope_conditions(
        user_id: UUID | None,
        *,
        scope: str | None,
        space_id: UUID | None,
    ) -> list[object]:
        resolved_scope, resolved_space_id = FileExplorerBookmarkRepository._resolve_scope(
            user_id, scope=scope, space_id=space_id
        )
        if resolved_scope == SCOPE_SHARED:
            return [
                FileExplorerBookmark.space_id == resolved_space_id,
                FileExplorerBookmark.user_id.is_(None),
            ]
        conditions: list[object] = [FileExplorerBookmark.user_id == user_id]
        # ``space_id`` is present after the forward migration.  Keeping the
        # defensive fallback makes this repository importable while an older
        # application process is being rolled forward.
        space_column = getattr(FileExplorerBookmark, "space_id", None)
        if space_column is not None:
            conditions.append(space_column.is_(None))
        return conditions

    @staticmethod
    async def list_for_scope(
        session: AsyncSession,
        *,
        scope: str,
        space_id: UUID | None = None,
        user_id: UUID | None = None,
    ) -> List[FileExplorerBookmark]:
        """List exactly one shared or personal collection."""

        query = select(FileExplorerBookmark).where(
            *FileExplorerBookmarkRepository._scope_conditions(
                user_id, scope=scope, space_id=space_id
            )
        )
        query = query.order_by(
            FileExplorerBookmark.sort_order.asc(),
            FileExplorerBookmark.created_at.asc(),
        )
        result = await session.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def list_for_space(
        session: AsyncSession, space_id: UUID
    ) -> List[FileExplorerBookmark]:
        """Convenience API for a Space-owned collection."""

        return await FileExplorerBookmarkRepository.list_for_scope(
            session, scope=SCOPE_SHARED, space_id=space_id
        )

    @staticmethod
    async def list_for_user(
        session: AsyncSession, user_id: UUID
    ) -> List[FileExplorerBookmark]:
        """Compatibility wrapper for the legacy personal collection."""

        return await FileExplorerBookmarkRepository.list_for_scope(
            session, scope=SCOPE_PERSONAL, user_id=user_id
        )

    @staticmethod
    async def get_for_scope(
        session: AsyncSession,
        bookmark_id: UUID,
        *,
        scope: str,
        space_id: UUID | None = None,
        user_id: UUID | None = None,
    ) -> Optional[FileExplorerBookmark]:
        query = select(FileExplorerBookmark).where(
            FileExplorerBookmark.id == bookmark_id,
            *FileExplorerBookmarkRepository._scope_conditions(
                user_id, scope=scope, space_id=space_id
            ),
        )
        result = await session.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_for_space(
        session: AsyncSession, space_id: UUID, bookmark_id: UUID
    ) -> Optional[FileExplorerBookmark]:
        return await FileExplorerBookmarkRepository.get_for_scope(
            session, bookmark_id, scope=SCOPE_SHARED, space_id=space_id
        )

    @staticmethod
    async def get(
        session: AsyncSession,
        user_id: UUID | None,
        bookmark_id: UUID,
        *,
        scope: str | None = None,
        space_id: UUID | None = None,
    ) -> Optional[FileExplorerBookmark]:
        """Get an item while enforcing the complete scope boundary."""

        return await FileExplorerBookmarkRepository.get_for_scope(
            session,
            bookmark_id,
            scope=scope or (SCOPE_SHARED if space_id is not None else SCOPE_PERSONAL),
            space_id=space_id,
            user_id=user_id,
        )

    @staticmethod
    async def get_by_path_for_scope(
        session: AsyncSession,
        path: str,
        *,
        scope: str,
        space_id: UUID | None = None,
        user_id: UUID | None = None,
    ) -> Optional[FileExplorerBookmark]:
        query = select(FileExplorerBookmark).where(
            FileExplorerBookmark.path == path,
            *FileExplorerBookmarkRepository._scope_conditions(
                user_id, scope=scope, space_id=space_id
            ),
        )
        result = await session.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_path_for_space(
        session: AsyncSession, space_id: UUID, path: str
    ) -> Optional[FileExplorerBookmark]:
        return await FileExplorerBookmarkRepository.get_by_path_for_scope(
            session, path, scope=SCOPE_SHARED, space_id=space_id
        )

    @staticmethod
    async def get_by_path(
        session: AsyncSession,
        user_id: UUID | None,
        path: str,
        *,
        scope: str | None = None,
        space_id: UUID | None = None,
    ) -> Optional[FileExplorerBookmark]:
        return await FileExplorerBookmarkRepository.get_by_path_for_scope(
            session,
            path,
            scope=scope or (SCOPE_SHARED if space_id is not None else SCOPE_PERSONAL),
            space_id=space_id,
            user_id=user_id,
        )

    @staticmethod
    async def _next_sort_order(
        session: AsyncSession,
        user_id: UUID | None,
        parent_id: UUID | None,
        *,
        scope: str | None = None,
        space_id: UUID | None = None,
    ) -> float:
        conditions = FileExplorerBookmarkRepository._scope_conditions(
            user_id,
            scope=scope or (SCOPE_SHARED if space_id is not None else SCOPE_PERSONAL),
            space_id=space_id,
        )
        if parent_id is None:
            conditions.append(FileExplorerBookmark.parent_id.is_(None))
        else:
            conditions.append(FileExplorerBookmark.parent_id == parent_id)
        max_sort_query = select(func.max(FileExplorerBookmark.sort_order)).where(
            *conditions
        )
        max_sort = (await session.execute(max_sort_query)).scalar()
        return float(max_sort or 0) + 1

    @staticmethod
    async def validate_parent_id(
        session: AsyncSession,
        user_id: UUID | None,
        parent_id: UUID | None,
        *,
        node_id: UUID | None = None,
        scope: str | None = None,
        space_id: UUID | None = None,
    ) -> None:
        if parent_id is None:
            return
        if node_id is not None and parent_id == node_id:
            raise ValueError("自分自身を親フォルダに指定できません")

        resolved_scope = scope or (SCOPE_SHARED if space_id is not None else SCOPE_PERSONAL)
        parent = await FileExplorerBookmarkRepository.get(
            session,
            user_id,
            parent_id,
            scope=resolved_scope,
            space_id=space_id,
        )
        if parent is None:
            raise ValueError("親フォルダが見つかりません")
        if parent.kind != KIND_FOLDER:
            raise ValueError("親にはブックマークフォルダだけを指定できます")

        if node_id is None:
            return

        ancestor = parent
        seen: set[UUID] = set()
        for _ in range(_MAX_PARENT_DEPTH + 1):
            if ancestor is None:
                break
            if ancestor.id == node_id:
                raise ValueError("循環する親フォルダは指定できません")
            if ancestor.id in seen or ancestor.parent_id is None:
                break
            seen.add(ancestor.id)
            ancestor = await FileExplorerBookmarkRepository.get(
                session,
                user_id,
                ancestor.parent_id,
                scope=resolved_scope,
                space_id=space_id,
            )
        else:
            raise ValueError("ブックマーク階層が深すぎるか循環しています")

    @staticmethod
    async def add(
        session: AsyncSession,
        user_id: UUID | None,
        name: str,
        path: str | None = None,
        icon: str = "📁",
        *,
        kind: str = KIND_BOOKMARK,
        parent_id: UUID | None = None,
        scope: str | None = None,
        space_id: UUID | None = None,
    ) -> FileExplorerBookmark:
        resolved_scope, resolved_space_id = FileExplorerBookmarkRepository._resolve_scope(
            user_id, scope=scope, space_id=space_id
        )
        if resolved_scope == SCOPE_PERSONAL and user_id is None:
            raise ValueError("creator user_idが必要です")
        kind = str(kind or KIND_BOOKMARK).strip()
        if kind not in _VALID_KINDS:
            raise ValueError("kindは bookmark または folder を指定してください")

        name = str(name or "").strip()
        if not name or len(name) > 200:
            raise ValueError("ブックマーク名は1〜200文字で指定してください")

        await FileExplorerBookmarkRepository.validate_parent_id(
            session,
            user_id,
            parent_id,
            scope=resolved_scope,
            space_id=resolved_space_id,
        )

        bookmark_id = uuid4()
        if kind == KIND_FOLDER:
            resolved_path = bookmark_folder_path(bookmark_id)
        else:
            resolved_path = str(path or "").strip()
            if not resolved_path:
                raise ValueError("ブックマークのパスを指定してください")
            if resolved_path.startswith(BOOKMARK_FOLDER_PATH_PREFIX):
                raise ValueError("このパスはブックマークに使用できません")
            existing = await FileExplorerBookmarkRepository.get_by_path(
                session,
                user_id,
                resolved_path,
                scope=resolved_scope,
                space_id=resolved_space_id,
            )
            if existing and existing.kind == KIND_BOOKMARK:
                raise ValueError("このパスは既にブックマークされています")

        next_sort = await FileExplorerBookmarkRepository._next_sort_order(
            session,
            user_id,
            parent_id,
            scope=resolved_scope,
            space_id=resolved_space_id,
        )

        bookmark = FileExplorerBookmark(
            id=bookmark_id,
            # Shared rows have no personal owner.  Keep the authenticated
            # identity out of the collection boundary (it is available to the
            # API's audit log when needed).
            user_id=user_id if resolved_scope == SCOPE_PERSONAL else None,
            name=name,
            path=resolved_path,
            icon=icon,
            kind=kind,
            parent_id=parent_id,
            sort_order=next_sort,
        )
        # The forward migration adds this column.  Assigning after construction
        # keeps import compatibility with a process that has not reloaded its
        # model yet, while always populating it on the current model.
        if hasattr(FileExplorerBookmark, "space_id"):
            bookmark.space_id = resolved_space_id
        session.add(bookmark)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            if kind == KIND_BOOKMARK:
                raise ValueError("このパスは既にブックマークされています") from exc
            raise
        await session.refresh(bookmark)
        return bookmark

    @staticmethod
    async def add_for_space(
        session: AsyncSession,
        space_id: UUID,
        name: str,
        path: str | None = None,
        icon: str = "📁",
        *,
        kind: str = KIND_BOOKMARK,
        parent_id: UUID | None = None,
    ) -> FileExplorerBookmark:
        """Create a Space-owned bookmark (``user_id IS NULL``)."""

        return await FileExplorerBookmarkRepository.add(
            session,
            None,
            name,
            path,
            icon,
            kind=kind,
            parent_id=parent_id,
            scope=SCOPE_SHARED,
            space_id=space_id,
        )

    @staticmethod
    async def remove_by_path(
        session: AsyncSession,
        user_id: UUID | None,
        path: str,
        *,
        scope: str | None = None,
        space_id: UUID | None = None,
    ) -> bool:
        resolved_scope = scope or (SCOPE_SHARED if space_id is not None else SCOPE_PERSONAL)
        stmt = delete(FileExplorerBookmark).where(
            FileExplorerBookmark.path == path,
            *FileExplorerBookmarkRepository._scope_conditions(
                user_id, scope=resolved_scope, space_id=space_id
            ),
        )
        result = await session.execute(stmt)
        await session.commit()
        return bool(result.rowcount)

    @staticmethod
    async def remove_by_path_for_space(
        session: AsyncSession, space_id: UUID, path: str
    ) -> bool:
        return await FileExplorerBookmarkRepository.remove_by_path(
            session, None, path, scope=SCOPE_SHARED, space_id=space_id
        )

    @staticmethod
    async def remove(
        session: AsyncSession,
        user_id: UUID | None,
        bookmark_id: UUID,
        *,
        scope: str | None = None,
        space_id: UUID | None = None,
    ) -> bool:
        """Remove an item by id while enforcing the complete scope."""
        resolved_scope = scope or (SCOPE_SHARED if space_id is not None else SCOPE_PERSONAL)
        stmt = delete(FileExplorerBookmark).where(
            FileExplorerBookmark.id == bookmark_id,
            *FileExplorerBookmarkRepository._scope_conditions(
                user_id, scope=resolved_scope, space_id=space_id
            ),
        )
        result = await session.execute(stmt)
        await session.commit()
        return bool(result.rowcount)

    @staticmethod
    async def remove_for_space(
        session: AsyncSession, space_id: UUID, bookmark_id: UUID
    ) -> bool:
        return await FileExplorerBookmarkRepository.remove(
            session, None, bookmark_id, scope=SCOPE_SHARED, space_id=space_id
        )

    @staticmethod
    async def update(
        session: AsyncSession,
        user_id: UUID | None,
        bookmark_id: UUID,
        *,
        name: Optional[str] = None,
        icon: Optional[str] = None,
        sort_order: Optional[float] = None,
        parent_id: object = ...,
        scope: str | None = None,
        space_id: UUID | None = None,
    ) -> Optional[FileExplorerBookmark]:
        resolved_scope = scope or (SCOPE_SHARED if space_id is not None else SCOPE_PERSONAL)
        bookmark = await FileExplorerBookmarkRepository.get(
            session,
            user_id,
            bookmark_id,
            scope=resolved_scope,
            space_id=space_id,
        )
        if not bookmark:
            return None

        if name is not None:
            name = str(name).strip()
            if not name or len(name) > 200:
                raise ValueError("ブックマーク名は1〜200文字で指定してください")
            bookmark.name = name
        if icon is not None:
            bookmark.icon = icon
        if sort_order is not None:
            if not math.isfinite(float(sort_order)):
                raise ValueError("sort_orderは有限値で指定してください")
            bookmark.sort_order = sort_order
        if parent_id is not ...:
            await FileExplorerBookmarkRepository.validate_parent_id(
                session,
                user_id,
                parent_id,  # type: ignore[arg-type]
                node_id=bookmark_id,
                scope=resolved_scope,
                space_id=space_id,
            )
            bookmark.parent_id = parent_id  # type: ignore[assignment]
        bookmark.updated_at = datetime.utcnow()

        await session.commit()
        await session.refresh(bookmark)
        return bookmark

    @staticmethod
    async def update_for_space(
        session: AsyncSession,
        space_id: UUID,
        bookmark_id: UUID,
        *,
        name: Optional[str] = None,
        icon: Optional[str] = None,
        sort_order: Optional[float] = None,
        parent_id: object = ...,
    ) -> Optional[FileExplorerBookmark]:
        return await FileExplorerBookmarkRepository.update(
            session,
            None,
            bookmark_id,
            name=name,
            icon=icon,
            sort_order=sort_order,
            parent_id=parent_id,
            scope=SCOPE_SHARED,
            space_id=space_id,
        )
