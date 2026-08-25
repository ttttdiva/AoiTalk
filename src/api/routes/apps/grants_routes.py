"""App grant 管理 endpoint。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import and_, select

from ....memory.models import AppGrant, Project
from ._shared import AppGrantPayload, AppRouterContext


def register_app_grant_routes(router: APIRouter, ctx: AppRouterContext) -> None:
    """既存順序の位置で App grant endpoint 3件を登録する。"""
    get_db_manager = ctx.get_db_manager
    require_auth_dependency = ctx.require_auth_dependency
    current_user = ctx.current_user
    require_app = ctx.require_app
    _uuid = ctx.uuid
    _user_id = ctx.user_id
    _error = ctx.error

    @router.get("/api/apps/{app_id}/grants")
    async def list_app_grants(
        app_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            app, _ = await require_app(session, app_id, user, required="admin")
            grants = list(
                (
                    await session.scalars(
                        select(AppGrant)
                        .where(AppGrant.app_id == app.id)
                        .order_by(AppGrant.created_at)
                    )
                ).all()
            )
            return {"grants": [grant.to_dict() for grant in grants]}
        finally:
            await session.close()

    @router.post("/api/apps/{app_id}/grants")
    async def create_app_grant(
        app_id: str,
        payload: AppGrantPayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            app, _ = await require_app(session, app_id, user, required="admin")
            if payload.permission not in {
                "viewer",
                "runner",
                "developer",
                "maintainer",
                "admin",
            }:
                raise _error(400, "permission が不正です")
            if bool(payload.user_id) == bool(payload.project_id):
                raise _error(400, "user_id または project_id のどちらか一方が必要です")
            user_uuid = _uuid(payload.user_id, "user_id") if payload.user_id else None
            project_uuid = (
                _uuid(payload.project_id, "project_id") if payload.project_id else None
            )
            if project_uuid:
                # 付与者が対象 Project のメンバーである必要は意図的に求めない。
                # これは「自分の App を任意の Project へ共有する」操作であり、
                # 付与者がその Project のデータへ到達できるようにはならない
                # (require_app は呼び出し側の project_access を別途要求する)。
                # Project の App 一覧へ現れるのは binding であり、grant 単独では
                # 現れないため、Project 側への副作用も生じない。
                project = await session.scalar(
                    select(Project).where(Project.id == project_uuid).limit(1)
                )
                if not project:
                    raise _error(404, "Project not found")
            grant = await session.scalar(
                select(AppGrant)
                .where(
                    and_(
                        AppGrant.app_id == app.id,
                        AppGrant.user_id == user_uuid,
                        AppGrant.project_id == project_uuid,
                    )
                )
                .limit(1)
            )
            if grant is None:
                grant = AppGrant(
                    app_id=app.id,
                    user_id=user_uuid,
                    project_id=project_uuid,
                    created_by=_user_id(user),
                )
                session.add(grant)
            grant.permission = payload.permission
            await session.commit()
            return {"success": True, "grant": grant.to_dict()}
        finally:
            await session.close()

    @router.delete("/api/apps/{app_id}/grants/{grant_id}")
    async def delete_app_grant(
        app_id: str,
        grant_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            app, _ = await require_app(session, app_id, user, required="admin")
            grant = await session.scalar(
                select(AppGrant)
                .where(
                    and_(
                        AppGrant.id == _uuid(grant_id, "grant_id"),
                        AppGrant.app_id == app.id,
                    )
                )
                .limit(1)
            )
            if not grant:
                raise _error(404, "Grant not found")
            await session.delete(grant)
            await session.commit()
            return {"success": True}
        finally:
            await session.close()
