"""共有 Space 認可ポリシー。

Task API と Files API が同じ Space の read/write 境界を使えるよう、Space の
アクセス判定をこのモジュールへ集約する。ここで扱う ``write`` は Space 自体の
既存契約に合わせ、Inbox は owner のみ、通常 Space は owner/admin のみを許可
する。ProjectMember の write 権限を Space の write へ拡張しない。

``SpaceAccessPolicy`` は状態を持たないため、API ごとにインスタンス化してもよい。
後方互換性と Files 側からの簡単な再利用のため、モジュールレベルの callable も
同じデフォルトポリシーへ委譲する。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional
from uuid import UUID

from sqlalchemy import and_, select

from ..api.uuid_http import parse_uuid_or_400
from ..memory.models import Project, ProjectMember, Space
from .project_permissions import normalize_project_member_permissions


class SpaceAccessPolicy:
    """既存 Task/Space API と共有する Space 認可判定。

    各メソッドは DB session を呼び出し元から受け取る。認証情報を内部に保持
    しないことで、リクエスト間で認可結果が混ざることを防ぐ。
    """

    @staticmethod
    def is_inbox_space(space: Space) -> bool:
        """ユーザー専用 Inbox Space かどうかを判定する。"""

        return space.slug == f"inbox-{space.owner_id}"

    @staticmethod
    def is_admin_user(user_info: Mapping[str, Any]) -> bool:
        """認証 principal が admin かどうかを判定する。"""

        return str(user_info.get("role") or "") == "admin"

    async def member_space_ids(self, session, user_id: UUID) -> set[UUID]:
        """ユーザーが Project の read 境界から閲覧できる Space ID を返す。

        Space の list API で用いる既存の補助判定であり、Inbox を別途公開する
        ルールはここへ混ぜない。Project owner は membership row がなくても
        read 可能、その他は既存の normalized ``read`` 権限だけを採用する。
        """

        result = await session.execute(
            select(
                Project.space_id,
                Project.owner_id,
                ProjectMember.role,
                ProjectMember.permissions,
            )
            .outerjoin(
                ProjectMember,
                and_(
                    ProjectMember.project_id == Project.id,
                    ProjectMember.user_id == user_id,
                ),
            )
            .where(
                Project.space_id.isnot(None),
                Project.deleted_at.is_(None),
            )
        )
        return {
            space_id
            for space_id, owner_id, _role, permissions in result.all()
            if owner_id == user_id
            or normalize_project_member_permissions(permissions).get("read") is True
        }

    async def load_space(self, session, space_id: str | UUID) -> Optional[Space]:
        """ID を検証して Space を読み込む。未存在なら ``None``。"""

        # API callers normally provide a path string, while repository/task
        # callers may already hold a UUID.  Normalize both forms before
        # delegating to the existing HTTP-compatible parser.
        parsed = parse_uuid_or_400(
            str(space_id) if isinstance(space_id, UUID) else space_id,
            "space_id",
        )
        result = await session.execute(select(Space).where(Space.id == parsed))
        return result.scalar_one_or_none()

    async def get_readable_space(
        self,
        session,
        *,
        space_id: str | UUID,
        user_id: UUID,
        user_info: Mapping[str, Any],
    ) -> Optional[Space]:
        """既存 Space read semantics に従い、読める Space を返す。"""

        space = await self.load_space(session, space_id)
        if space is None:
            return None
        if self.is_inbox_space(space):
            return space if space.owner_id == user_id else None
        if space.owner_id == user_id or self.is_admin_user(user_info):
            return space

        result = await session.execute(
            select(
                Project.id,
                Project.owner_id,
                ProjectMember.role,
                ProjectMember.permissions,
            )
            .select_from(Project)
            .outerjoin(
                ProjectMember,
                and_(
                    ProjectMember.project_id == Project.id,
                    ProjectMember.user_id == user_id,
                ),
            )
            .where(
                Project.space_id == space.id,
                Project.space_id.isnot(None),
                Project.deleted_at.is_(None),
            )
        )
        for _project_id, owner_id, _role, permissions in result.all():
            if owner_id == user_id:
                return space
            if normalize_project_member_permissions(permissions).get("read") is True:
                return space
        return None

    async def can_write_space(
        self,
        session,
        *,
        space_id: str | UUID,
        user_id: UUID,
        user_info: Mapping[str, Any],
    ) -> tuple[bool, Optional[Space]]:
        """既存 Space write semantics に従い ``(allowed, space)`` を返す。"""

        space = await self.load_space(session, space_id)
        if space is None:
            return False, None
        if self.is_inbox_space(space):
            return space.owner_id == user_id, space
        return space.owner_id == user_id or self.is_admin_user(user_info), space


# Shared stateless policy used by lightweight API callers (including Files).
space_access_policy = SpaceAccessPolicy()


def is_inbox_space(space: Space) -> bool:
    """デフォルト共有ポリシーへ委譲する。"""

    return space_access_policy.is_inbox_space(space)


def is_admin_user(user_info: Mapping[str, Any]) -> bool:
    """デフォルト共有ポリシーへ委譲する。"""

    return space_access_policy.is_admin_user(user_info)


async def member_space_ids(session, user_id: UUID) -> set[UUID]:
    """デフォルト共有ポリシーへ委譲する。"""

    return await space_access_policy.member_space_ids(session, user_id)


async def load_space(session, space_id: str | UUID) -> Optional[Space]:
    """デフォルト共有ポリシーへ委譲する。"""

    return await space_access_policy.load_space(session, space_id)


async def get_readable_space(
    session,
    *,
    space_id: str | UUID,
    user_id: UUID,
    user_info: Mapping[str, Any],
) -> Optional[Space]:
    """デフォルト共有ポリシーへ委譲する。"""

    return await space_access_policy.get_readable_space(
        session,
        space_id=space_id,
        user_id=user_id,
        user_info=user_info,
    )


async def can_write_space(
    session,
    *,
    space_id: str | UUID,
    user_id: UUID,
    user_info: Mapping[str, Any],
) -> tuple[bool, Optional[Space]]:
    """デフォルト共有ポリシーへ委譲する。"""

    return await space_access_policy.can_write_space(
        session,
        space_id=space_id,
        user_id=user_id,
        user_info=user_info,
    )


__all__ = [
    "SpaceAccessPolicy",
    "can_write_space",
    "get_readable_space",
    "is_admin_user",
    "is_inbox_space",
    "load_space",
    "member_space_ids",
    "space_access_policy",
]
