"""Webex Messaging の本人 OAuth と読み取り専用検索 API。"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..services.webex_service import WebexService, WebexServiceError


class WebexSpaceSelectionPayload(BaseModel):
    room_ids: list[str] = Field(default_factory=list)


class WebexSearchPayload(BaseModel):
    query: str = ""
    room_ids: list[str] = Field(default_factory=list)
    days: int = Field(default=30, ge=1, le=90)
    max_results: int = Field(default=20, ge=1, le=50)


def create_webex_router(
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/webex", tags=["webex"])
    service = WebexService()

    async def _current_user(request: Request) -> dict[str, Any]:
        user_info = await get_user_from_request(request)
        if not user_info or not user_info.get("id"):
            raise HTTPException(status_code=401, detail="認証が必要です")
        return user_info

    def _actor_id(user_info: dict[str, Any]) -> UUID:
        try:
            return UUID(str(user_info["id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=401, detail="ユーザーIDが不正です") from exc

    def _translate_error(exc: WebexServiceError) -> HTTPException:
        return HTTPException(status_code=exc.status_code, detail=exc.message)

    def _return_origin(request: Request) -> Optional[str]:
        origin = request.headers.get("x-forwarded-origin") or request.headers.get(
            "origin"
        )
        if origin:
            return origin
        referer = request.headers.get("referer")
        if not referer:
            return None
        try:
            parsed = urlparse(referer)
        except ValueError:
            return None
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        return f"{parsed.scheme}://{parsed.netloc}"

    @router.get("/settings")
    async def get_webex_settings(
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user_info = await _current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.get_settings(session, _actor_id(user_info))
        finally:
            await session.close()

    @router.post("/connect")
    async def start_webex_connect(
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user_info = await _current_user(request)
        try:
            authorization_url = service.build_authorization_url(
                user_id=_actor_id(user_info),
                username=str(user_info.get("username") or ""),
                return_origin=_return_origin(request),
            )
            return {"authorization_url": authorization_url}
        except WebexServiceError as exc:
            raise _translate_error(exc)

    @router.get("/oauth/callback")
    async def webex_oauth_callback(
        request: Request,
        code: Optional[str] = None,
        state: Optional[str] = None,
        error: Optional[str] = None,
    ):
        session = await get_db_manager().get_session()
        try:
            user_info = await _current_user(request)
            result = await service.handle_callback(
                session,
                code=code,
                state=state,
                error=error,
                expected_user_id=_actor_id(user_info),
            )
        except HTTPException as exc:
            return HTMLResponse(
                service.render_web_callback_html(
                    success=False,
                    error=str(exc.detail),
                ),
                status_code=exc.status_code,
            )
        except WebexServiceError as exc:
            return HTMLResponse(
                service.render_web_callback_html(
                    success=False,
                    error=exc.message,
                ),
                status_code=exc.status_code,
            )
        finally:
            await session.close()

        return HTMLResponse(
            service.render_web_callback_html(
                success=result.success,
                email=result.email,
                display_name=result.display_name,
                error=result.error,
                target_origin=result.return_origin,
            ),
            status_code=200 if result.success else 400,
        )

    @router.post("/disconnect")
    async def disconnect_webex(
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user_info = await _current_user(request)
        session = await get_db_manager().get_session()
        try:
            user_id = _actor_id(user_info)
            await service.disconnect(session, user_id)
            return await service.get_settings(session, user_id)
        finally:
            await session.close()

    @router.get("/spaces")
    async def list_webex_spaces(
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user_info = await _current_user(request)
        session = await get_db_manager().get_session()
        try:
            return {
                "spaces": await service.list_spaces(
                    session,
                    _actor_id(user_info),
                )
            }
        except WebexServiceError as exc:
            raise _translate_error(exc)
        finally:
            await session.close()

    @router.put("/spaces")
    async def update_webex_spaces(
        payload: WebexSpaceSelectionPayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user_info = await _current_user(request)
        session = await get_db_manager().get_session()
        try:
            spaces = await service.save_selected_spaces(
                session,
                _actor_id(user_info),
                payload.room_ids,
            )
            return {"spaces": spaces}
        except WebexServiceError as exc:
            raise _translate_error(exc)
        finally:
            await session.close()

    @router.post("/search")
    async def search_webex_messages(
        payload: WebexSearchPayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user_info = await _current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.search_messages(
                session,
                _actor_id(user_info),
                query=payload.query,
                room_ids=payload.room_ids or None,
                days=payload.days,
                max_results=payload.max_results,
            )
        except WebexServiceError as exc:
            raise _translate_error(exc)
        finally:
            await session.close()

    return router
