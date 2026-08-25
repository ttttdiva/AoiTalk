"""スペース関連エンドポイント（/api/spaces, /api/spaces/{id}/tags）。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from ...uuid_http import parse_uuid_or_400  # noqa: F401  (元 import 面を維持)
from ....memory.models import Space, Tag
from ....memory.project_repository import ProjectRepository
from ._shared import CreateSpacePayload, TaskRouterContext


def register_space_routes(router: APIRouter, ctx: TaskRouterContext) -> None:
    # Space 削除は配下 Project の workspace まで消すため、ロック取得先と削除先の
    # root を一致させる必要がある。TaskRouterContext が実効 root を持っていれば
    # それを使い、無ければ ``None``（= AOITALK_WORKSPACES_DIR 由来の既定 root）を
    # そのまま渡す。どちらの場合も ProjectRepository 側で 1 度だけ解決される。
    workspace_root = ctx.workspace_root
    require_auth_dependency = ctx.require_auth_dependency
    get_db_manager = ctx.get_db_manager
    _get_current_user = ctx.get_current_user
    _space_slug = ctx.space_slug
    _is_inbox_space = ctx.is_inbox_space
    _is_admin_user = ctx.is_admin_user
    _member_space_ids = ctx.member_space_ids
    _get_readable_space = ctx.get_readable_space
    _can_write_space = ctx.can_write_space

    def _serialize_space(space: Space, *, can_write: bool) -> dict:
        """Serialize a Space together with the caller's write capability.

        ``Space.to_dict`` intentionally remains context-free.  The capability
        is attached only at the authenticated API boundary so callers cannot
        accidentally persist one user's authorization decision as model data.
        Every caller obtains ``can_write`` from the existing
        ``_can_write_space`` helper (or the result already returned by it),
        keeping Inbox/owner/admin semantics in one place.
        """
        payload = space.to_dict()
        payload["can_write"] = bool(can_write)
        return payload

    @router.get("/spaces")
    async def list_spaces(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, user_info = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            await ProjectRepository.ensure_user_inbox_setup(session, user_id)
            await session.commit()
            member_space_ids = await _member_space_ids(session, user_id)
            is_admin = _is_admin_user(user_info)
            result = await session.execute(
                select(Space).order_by(Space.sort_order.asc(), Space.created_at.asc())
            )
            spaces = []
            for space in result.scalars().all():
                if _is_inbox_space(space):
                    if space.owner_id == user_id:
                        can_write, _ = await _can_write_space(
                            session,
                            space_id=str(space.id),
                            user_id=user_id,
                            user_info=user_info,
                        )
                        spaces.append(
                            _serialize_space(space, can_write=can_write)
                        )
                elif (
                    space.owner_id == user_id
                    or is_admin
                    or space.id in member_space_ids
                ):
                    can_write, _ = await _can_write_space(
                        session,
                        space_id=str(space.id),
                        user_id=user_id,
                        user_info=user_info,
                    )
                    spaces.append(_serialize_space(space, can_write=can_write))
            return {"spaces": spaces, "total": len(spaces)}
        finally:
            await session.close()

    @router.post("/spaces")
    async def create_space(
        payload: CreateSpacePayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, user_info = await _get_current_user(request)
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        session = await get_db_manager().get_session()
        try:
            base_slug = _space_slug(name)
            slug = base_slug
            counter = 1
            while True:
                existing = await session.execute(
                    select(Space.id).where(Space.owner_id == user_id, Space.slug == slug)
                )
                if not existing.scalar_one_or_none():
                    break
                counter += 1
                slug = f"{base_slug}-{counter}"
            space = Space(
                name=name,
                slug=slug,
                description=payload.description,
                color=payload.color,
                owner_id=user_id,
                sort_order=payload.sort_order or 0,
            )
            session.add(space)
            await session.commit()
            await session.refresh(space)
            can_write, _ = await _can_write_space(
                session,
                space_id=str(space.id),
                user_id=user_id,
                user_info=user_info,
            )
            return {
                "success": True,
                "space": _serialize_space(space, can_write=can_write),
            }
        finally:
            await session.close()

    @router.get("/spaces/{space_id}")
    async def get_space(
        space_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, user_info = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            space = await _get_readable_space(
                session, space_id=space_id, user_id=user_id, user_info=user_info
            )
            if space is None:
                raise HTTPException(
                    status_code=404, detail="スペースが見つかりません"
                )
            can_write, _ = await _can_write_space(
                session,
                space_id=str(space.id),
                user_id=user_id,
                user_info=user_info,
            )
            return _serialize_space(space, can_write=can_write)
        finally:
            await session.close()

    @router.patch("/spaces/{space_id}")
    async def update_space(
        space_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, user_info = await _get_current_user(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        session = await get_db_manager().get_session()
        try:
            allowed, space = await _can_write_space(
                session, space_id=space_id, user_id=user_id, user_info=user_info
            )
            if space is None:
                raise HTTPException(
                    status_code=404, detail="スペースが見つかりません"
                )
            if not allowed:
                raise HTTPException(status_code=403, detail="権限がありません")

            if body.get("name") is not None:
                space.name = str(body["name"])
            if "description" in body:
                space.description = body["description"]
            if "color" in body:
                space.color = body["color"]
            if body.get("sort_order") is not None:
                space.sort_order = float(body["sort_order"])
            space.updated_at = datetime.utcnow()

            await session.commit()
            await session.refresh(space)
            return {
                "success": True,
                "space": _serialize_space(space, can_write=allowed),
            }
        finally:
            await session.close()

    @router.delete("/spaces/{space_id}")
    async def delete_space(
        space_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, user_info = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            allowed, space = await _can_write_space(
                session, space_id=space_id, user_id=user_id, user_info=user_info
            )
            if space is None:
                raise HTTPException(
                    status_code=404, detail="スペースが見つかりません"
                )
            if not allowed:
                raise HTTPException(status_code=403, detail="権限がありません")
            if _is_inbox_space(space):
                raise HTTPException(
                    status_code=400, detail="Inboxスペースは削除できません"
                )

            deleted_project_count = await ProjectRepository.delete_projects_in_space(
                session,
                space.id,
                delete_workspaces=True,
                workspace_root=workspace_root,
            )
            await session.delete(space)
            await session.commit()
            return {"success": True, "deleted_project_count": deleted_project_count}
        finally:
            await session.close()

    @router.get("/spaces/{space_id}/tags")
    async def list_space_tags(
        space_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, user_info = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            space = await _get_readable_space(
                session, space_id=space_id, user_id=user_id, user_info=user_info
            )
            if space is None:
                raise HTTPException(
                    status_code=404, detail="スペースが見つかりません"
                )
            result = await session.execute(
                select(Tag).where(Tag.space_id == space.id).order_by(Tag.name)
            )
            return [tag.to_dict() for tag in result.scalars().all()]
        finally:
            await session.close()

    @router.post("/spaces/{space_id}/tags")
    async def create_space_tag(
        space_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, user_info = await _get_current_user(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        name = str(body.get("name") or "").strip()
        color_raw = body.get("color")
        color = (
            color_raw.strip()
            if isinstance(color_raw, str) and color_raw.strip()
            else None
        )

        session = await get_db_manager().get_session()
        try:
            allowed, space = await _can_write_space(
                session, space_id=space_id, user_id=user_id, user_info=user_info
            )
            if space is None:
                raise HTTPException(
                    status_code=404, detail="スペースが見つかりません"
                )
            if not allowed:
                raise HTTPException(status_code=403, detail="権限がありません")
            if not name:
                raise HTTPException(status_code=400, detail="nameは必須です")

            existing = await session.execute(
                select(Tag).where(Tag.space_id == space.id, Tag.name == name)
            )
            tag = existing.scalar_one_or_none()
            if tag is not None:
                return tag.to_dict()

            tag = Tag(space_id=space.id, name=name, color=color, created_by=user_id)
            session.add(tag)
            await session.commit()
            await session.refresh(tag)
            return tag.to_dict()
        finally:
            await session.close()
