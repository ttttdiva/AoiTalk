"""通知・ユーザー設定・通知設定のエンドポイント。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ....memory.user_repository import UserRepository
from ....services.task_management_service import TaskManagementError
from ._shared import (
    NotificationSettingsPayload,
    TaskRouterContext,
    UserNotificationPreferencesPayload,
    _deep_merge_settings,
)
from ...uuid_http import parse_uuid_or_400


def register_notification_routes(router: APIRouter, ctx: TaskRouterContext) -> None:
    require_auth_dependency = ctx.require_auth_dependency
    get_db_manager = ctx.get_db_manager
    service = ctx.service
    _get_current_user = ctx.get_current_user
    _translate_service_error = ctx.translate_service_error

    @router.get("/notifications")
    async def list_notifications(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.list_notifications(
                session,
                user_id=user_id,
                unread_only=request.query_params.get("unread_only", "false").lower()
                in {"1", "true", "yes"},
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.get("/users/me/notification-preferences")
    async def get_user_notification_preferences(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.get_user_notification_preferences(
                session, user_id=user_id
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.patch("/users/me/notification-preferences")
    async def update_user_notification_preferences(
        payload: UserNotificationPreferencesPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.update_user_notification_preferences(
                session,
                user_id=user_id,
                task_notification_minutes_before=payload.task_notification_minutes_before,
                task_notifications_default_enabled=payload.task_notifications_default_enabled,
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.get("/users/me/settings")
    async def get_user_settings(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            user = await UserRepository.get_by_id(session, user_id)
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            return {"settings": user.user_settings or {}}
        finally:
            await session.close()

    @router.patch("/users/me/settings")
    async def update_user_settings(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        updates = body if isinstance(body, dict) else {}

        session = await get_db_manager().get_session()
        try:
            user = await UserRepository.get_by_id(session, user_id)
            if not user:
                raise HTTPException(status_code=404, detail="User not found")

            merged = _deep_merge_settings(dict(user.user_settings or {}), updates)
            await UserRepository.update_user(
                session=session,
                user_id=user_id,
                user_settings=merged,
            )
            return {"settings": merged}
        finally:
            await session.close()

    @router.post("/notifications/{notification_id}/read")
    async def mark_notification_read(
        notification_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.mark_notification_read(
                session,
                user_id=user_id,
                notification_id=parse_uuid_or_400(notification_id, "notification_id"),
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.post("/notifications/read-all")
    async def mark_all_notifications_read(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            count = await service.mark_all_notifications_read(
                session, user_id=user_id
            )
            return {"success": True, "count": count}
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.get("/projects/{project_id}/notification-settings")
    async def get_notification_settings(
        project_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            parsed_project_id = parse_uuid_or_400(project_id, "project_id")
            await service.require_project_permission(
                session,
                project_id=parsed_project_id,
                user_id=user_id,
                permission="read",
            )
            setting = await service.get_or_create_notification_setting(
                session, project_id=parsed_project_id
            )
            return setting.to_dict()
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.patch("/projects/{project_id}/notification-settings")
    async def update_notification_settings(
        project_id: str,
        payload: NotificationSettingsPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.update_notification_setting(
                session,
                user_id=user_id,
                project_id=parse_uuid_or_400(project_id, "project_id"),
                discord_webhook_url=payload.discord_webhook_url,
                default_reminder_offsets=payload.default_reminder_offsets,
                notify_overdue=payload.notify_overdue,
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()
