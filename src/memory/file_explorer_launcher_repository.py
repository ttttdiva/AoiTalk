"""Repository for Space-shared and legacy personal Files launchers.

The legacy methods in this module are intentionally personal-only and always
include ``space_id IS NULL``.  Space methods are explicit and include both
``space_id = :space`` and ``user_id IS NULL``.  Keeping the two APIs separate
prevents an item UUID from being used to cross a collection boundary and keeps
old personal data readable while the forward migration is deployed.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import FileExplorerLauncher

SCOPE_SHARED = "shared"
SCOPE_PERSONAL = "personal"
_VALID_SCOPES = frozenset({SCOPE_SHARED, SCOPE_PERSONAL})


class FileExplorerLauncherRepository:
    """Database access for durable scoped file launchers."""

    @staticmethod
    def _resolve_scope(
        user_id: UUID | None,
        *,
        scope: str | None,
        space_id: UUID | None,
    ) -> tuple[str, UUID | None]:
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
        resolved_scope, resolved_space_id = FileExplorerLauncherRepository._resolve_scope(
            user_id, scope=scope, space_id=space_id
        )
        if resolved_scope == SCOPE_SHARED:
            return [
                FileExplorerLauncher.space_id == resolved_space_id,
                FileExplorerLauncher.user_id.is_(None),
            ]
        conditions: list[object] = [FileExplorerLauncher.user_id == user_id]
        space_column = getattr(FileExplorerLauncher, "space_id", None)
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
    ) -> List[FileExplorerLauncher]:
        query = select(FileExplorerLauncher).where(
            *FileExplorerLauncherRepository._scope_conditions(
                user_id, scope=scope, space_id=space_id
            )
        ).order_by(
            FileExplorerLauncher.sort_order.asc(),
            FileExplorerLauncher.created_at.asc(),
        )
        result = await session.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def list_for_space(
        session: AsyncSession, space_id: UUID
    ) -> List[FileExplorerLauncher]:
        return await FileExplorerLauncherRepository.list_for_scope(
            session, scope=SCOPE_SHARED, space_id=space_id
        )

    @staticmethod
    async def list_for_user(
        session: AsyncSession, user_id: UUID
    ) -> List[FileExplorerLauncher]:
        """Compatibility API for personal launchers only."""

        return await FileExplorerLauncherRepository.list_for_scope(
            session, scope=SCOPE_PERSONAL, user_id=user_id
        )

    @staticmethod
    async def get_for_scope(
        session: AsyncSession,
        launcher_id: UUID,
        *,
        scope: str,
        space_id: UUID | None = None,
        user_id: UUID | None = None,
    ) -> Optional[FileExplorerLauncher]:
        result = await session.execute(
            select(FileExplorerLauncher).where(
                FileExplorerLauncher.id == launcher_id,
                *FileExplorerLauncherRepository._scope_conditions(
                    user_id, scope=scope, space_id=space_id
                ),
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_for_space(
        session: AsyncSession, space_id: UUID, launcher_id: UUID
    ) -> Optional[FileExplorerLauncher]:
        return await FileExplorerLauncherRepository.get_for_scope(
            session, launcher_id, scope=SCOPE_SHARED, space_id=space_id
        )

    @staticmethod
    async def get_by_id(
        session: AsyncSession,
        user_id: UUID | None,
        launcher_id: UUID,
        *,
        scope: str | None = None,
        space_id: UUID | None = None,
    ) -> Optional[FileExplorerLauncher]:
        """Compatibility lookup; explicit ``scope=shared`` is also supported."""

        return await FileExplorerLauncherRepository.get_for_scope(
            session,
            launcher_id,
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
    ) -> Optional[FileExplorerLauncher]:
        result = await session.execute(
            select(FileExplorerLauncher).where(
                FileExplorerLauncher.path == path,
                *FileExplorerLauncherRepository._scope_conditions(
                    user_id, scope=scope, space_id=space_id
                ),
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_path_for_space(
        session: AsyncSession, space_id: UUID, path: str
    ) -> Optional[FileExplorerLauncher]:
        return await FileExplorerLauncherRepository.get_by_path_for_scope(
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
    ) -> Optional[FileExplorerLauncher]:
        return await FileExplorerLauncherRepository.get_by_path_for_scope(
            session,
            path,
            scope=scope or (SCOPE_SHARED if space_id is not None else SCOPE_PERSONAL),
            space_id=space_id,
            user_id=user_id,
        )

    @staticmethod
    async def add(
        session: AsyncSession,
        user_id: UUID | None,
        name: str,
        path: str,
        icon: str = "📄",
        *,
        scope: str | None = None,
        space_id: UUID | None = None,
    ) -> FileExplorerLauncher:
        """Create a personal launcher, or dispatch explicit shared scope."""

        resolved_scope, resolved_space_id = FileExplorerLauncherRepository._resolve_scope(
            user_id, scope=scope, space_id=space_id
        )
        if resolved_scope == SCOPE_SHARED:
            return await FileExplorerLauncherRepository._add_in_scope(
                session, None, resolved_scope, resolved_space_id, name, path, icon
            )
        return await FileExplorerLauncherRepository._add_in_scope(
            session, user_id, resolved_scope, None, name, path, icon
        )

    @staticmethod
    async def add_for_space(
        session: AsyncSession,
        space_id: UUID,
        name: str,
        path: str,
        icon: str = "📄",
    ) -> FileExplorerLauncher:
        """Create a Space-owned launcher (``user_id IS NULL``)."""

        return await FileExplorerLauncherRepository._add_in_scope(
            session, None, SCOPE_SHARED, space_id, name, path, icon
        )

    @staticmethod
    async def _add_in_scope(
        session: AsyncSession,
        row_user_id: UUID | None,
        scope: str,
        space_id: UUID | None,
        name: str,
        path: str,
        icon: str,
    ) -> FileExplorerLauncher:
        name = str(name or "").strip()
        path = str(path or "").strip()
        if not name or len(name) > 200:
            raise ValueError("ランチャー名は1〜200文字で指定してください")
        if not path:
            raise ValueError("ランチャーのパスを指定してください")
        if await FileExplorerLauncherRepository.get_by_path_for_scope(
            session,
            path,
            scope=scope,
            space_id=space_id,
            user_id=row_user_id,
        ):
            raise ValueError("このパスは既にランチャーへ登録されています")

        max_sort = (
            await session.execute(
                select(func.max(FileExplorerLauncher.sort_order)).where(
                    *FileExplorerLauncherRepository._scope_conditions(
                        row_user_id, scope=scope, space_id=space_id
                    )
                )
            )
        ).scalar()
        launcher = FileExplorerLauncher(
            user_id=row_user_id,
            name=name,
            path=path,
            icon=str(icon or "📄")[:64],
            sort_order=float(max_sort or 0) + 1,
        )
        if hasattr(FileExplorerLauncher, "space_id"):
            launcher.space_id = space_id
        session.add(launcher)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ValueError("このパスは既にランチャーへ登録されています") from exc
        await session.refresh(launcher)
        return launcher

    @staticmethod
    async def update(
        session: AsyncSession,
        user_id: UUID | None,
        launcher_id: UUID,
        *,
        name: Optional[str] = None,
        icon: Optional[str] = None,
        sort_order: Optional[float] = None,
        scope: str | None = None,
        space_id: UUID | None = None,
    ) -> Optional[FileExplorerLauncher]:
        resolved_scope = scope or (SCOPE_SHARED if space_id is not None else SCOPE_PERSONAL)
        launcher = await FileExplorerLauncherRepository.get_by_id(
            session,
            user_id,
            launcher_id,
            scope=resolved_scope,
            space_id=space_id,
        )
        return await FileExplorerLauncherRepository._update_loaded(
            session, launcher, name=name, icon=icon, sort_order=sort_order
        )

    @staticmethod
    async def update_for_space(
        session: AsyncSession,
        space_id: UUID,
        launcher_id: UUID,
        *,
        name: Optional[str] = None,
        icon: Optional[str] = None,
        sort_order: Optional[float] = None,
    ) -> Optional[FileExplorerLauncher]:
        launcher = await FileExplorerLauncherRepository.get_for_space(
            session, space_id, launcher_id
        )
        return await FileExplorerLauncherRepository._update_loaded(
            session, launcher, name=name, icon=icon, sort_order=sort_order
        )

    @staticmethod
    async def _update_loaded(
        session: AsyncSession,
        launcher: Optional[FileExplorerLauncher],
        *,
        name: Optional[str],
        icon: Optional[str],
        sort_order: Optional[float],
    ) -> Optional[FileExplorerLauncher]:
        if launcher is None:
            return None
        if name is not None:
            name = str(name).strip()
            if not name or len(name) > 200:
                raise ValueError("ランチャー名は1〜200文字で指定してください")
            launcher.name = name
        if icon is not None:
            launcher.icon = str(icon)[:64]
        if sort_order is not None:
            sort_order = float(sort_order)
            if not math.isfinite(sort_order):
                raise ValueError("sort_orderは有限値で指定してください")
            launcher.sort_order = sort_order
        launcher.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(launcher)
        return launcher

    @staticmethod
    async def remove(
        session: AsyncSession,
        user_id: UUID | None,
        launcher_id: UUID,
        *,
        scope: str | None = None,
        space_id: UUID | None = None,
    ) -> bool:
        resolved_scope = scope or (SCOPE_SHARED if space_id is not None else SCOPE_PERSONAL)
        result = await session.execute(
            delete(FileExplorerLauncher).where(
                FileExplorerLauncher.id == launcher_id,
                *FileExplorerLauncherRepository._scope_conditions(
                    user_id, scope=resolved_scope, space_id=space_id
                ),
            )
        )
        await session.commit()
        return bool(result.rowcount)

    @staticmethod
    async def remove_for_space(
        session: AsyncSession, space_id: UUID, launcher_id: UUID
    ) -> bool:
        return await FileExplorerLauncherRepository.remove(
            session, None, launcher_id, scope=SCOPE_SHARED, space_id=space_id
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
        result = await session.execute(
            delete(FileExplorerLauncher).where(
                FileExplorerLauncher.path == path,
                *FileExplorerLauncherRepository._scope_conditions(
                    user_id, scope=resolved_scope, space_id=space_id
                ),
            )
        )
        await session.commit()
        return bool(result.rowcount)

    @staticmethod
    async def remove_by_path_for_space(
        session: AsyncSession, space_id: UUID, path: str
    ) -> bool:
        return await FileExplorerLauncherRepository.remove_by_path(
            session, None, path, scope=SCOPE_SHARED, space_id=space_id
        )
