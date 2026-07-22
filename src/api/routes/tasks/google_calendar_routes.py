"""Google カレンダー連携のエンドポイント。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ....services.google_calendar_service import GoogleCalendarServiceError
from ....services.task_management_service import TaskManagementError
from ._shared import (
    GoogleCalendarConnectPayload,
    GoogleCalendarSettingsPayload,
    TaskRouterContext,
)


def register_google_calendar_routes(router: APIRouter, ctx: TaskRouterContext) -> None:
    require_auth_dependency = ctx.require_auth_dependency
    get_db_manager = ctx.get_db_manager
    google_calendar = ctx.google_calendar
    _get_current_user = ctx.get_current_user
    _translate_service_error = ctx.translate_service_error
    _translate_google_calendar_error = ctx.translate_google_calendar_error
    _load_task_for_google_calendar = ctx.load_task_for_google_calendar
    _sync_google_calendar_warning_only = ctx.sync_google_calendar_warning_only
    _delete_google_calendar_warning_only = ctx.delete_google_calendar_warning_only

    @router.get("/google-calendar/settings")
    async def get_google_calendar_settings(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await google_calendar.get_settings(session, user_id)
        finally:
            await session.close()

    @router.patch("/google-calendar/settings")
    async def update_google_calendar_settings(
        payload: GoogleCalendarSettingsPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await google_calendar.update_settings(
                session,
                user_id,
                default_action=payload.default_action,
                default_event_reminder_minutes=payload.default_event_reminder_minutes,
            )
        except GoogleCalendarServiceError as exc:
            raise _translate_google_calendar_error(exc)
        finally:
            await session.close()

    @router.post("/google-calendar/connect")
    async def start_google_calendar_connect(
        payload: GoogleCalendarConnectPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, user_info = await _get_current_user(request)
        try:
            authorization_url = await google_calendar.build_authorization_url(
                user_id=user_id,
                username=str(user_info.get("username") or ""),
                platform=payload.platform,
                mobile_redirect_uri=payload.mobile_redirect_uri,
            )
            return {"authorization_url": authorization_url}
        except GoogleCalendarServiceError as exc:
            raise _translate_google_calendar_error(exc)

    @router.get("/google-calendar/oauth/callback")
    async def google_calendar_oauth_callback(
        code: Optional[str] = None,
        state: Optional[str] = None,
        error: Optional[str] = None,
    ):
        session = await get_db_manager().get_session()
        try:
            result = await google_calendar.handle_callback(
                session,
                code=code,
                state=state,
                error=error,
            )
        except GoogleCalendarServiceError as exc:
            html = google_calendar.render_web_callback_html(
                success=False, error=exc.message
            )
            return HTMLResponse(html, status_code=exc.status_code)
        finally:
            await session.close()

        if result.platform == "mobile":
            target = google_calendar.build_mobile_redirect_uri(
                result.mobile_redirect_uri,
                "connected" if result.success else "error",
                result.error,
            )
            return RedirectResponse(target, status_code=302)

        html = google_calendar.render_web_callback_html(
            success=result.success,
            email=result.email,
            error=result.error,
        )
        return HTMLResponse(html)

    @router.post("/google-calendar/disconnect")
    async def disconnect_google_calendar(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            await google_calendar.disconnect(session, user_id)
            return await google_calendar.get_settings(session, user_id)
        finally:
            await session.close()

    @router.post("/tasks/{task_id}/google-calendar-event")
    async def create_google_calendar_event(
        task_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            task = await _load_task_for_google_calendar(
                session, user_id=user_id, task_id=task_id
            )
            return await google_calendar.create_event_for_task(
                session, user_id=user_id, task=task
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        except GoogleCalendarServiceError as exc:
            raise _translate_google_calendar_error(exc)
        finally:
            await session.close()

    @router.post("/tasks/{task_id}/google-calendar-auto-sync")
    async def auto_sync_google_calendar_event(
        task_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await _sync_google_calendar_warning_only(
                session, user_id=user_id, task_id=task_id
            )
        finally:
            await session.close()

    @router.post("/tasks/{task_id}/google-calendar-auto-delete")
    async def delete_auto_google_calendar_event(
        task_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await _delete_google_calendar_warning_only(
                session, user_id=user_id, task_id=task_id
            )
        finally:
            await session.close()
