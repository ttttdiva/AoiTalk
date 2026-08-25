"""Authenticated per-user X Cookie management endpoints."""

from __future__ import annotations

import inspect
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..services.x_cookie_service import (
    X_COOKIE_MAX_BYTES,
    XCookieValidationError,
    disable_personal_x_cookie,
    parse_x_cookie_bytes,
    resolve_x_cookie,
    upsert_personal_x_cookie,
)


def _safe_validation_detail(exc: XCookieValidationError) -> dict[str, str]:
    return {
        "code": str(exc.status),
        "status": str(exc.status),
        "message": str(exc),
    }


def _private_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


def _http_error(
    status_code: int,
    detail: Any,
    *,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    merged = dict(headers or {})
    merged["Cache-Control"] = "private, no-store"
    return HTTPException(status_code=status_code, detail=detail, headers=merged)


def create_x_cookie_router(
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/users/me/x-cookie", tags=["x-cookie"])

    def _require_auth(request: Request) -> Any:
        """Preserve sync auth dependency execution and harden error headers.

        FastAPI executes a synchronous dependency in its threadpool.  Keep
        this wrapper synchronous so the canonical cookie/JWT resolver cannot
        block the event loop with its synchronous compatibility checks.
        """

        try:
            parameters = inspect.signature(require_auth_dependency).parameters
            accepts_request = any(
                parameter.kind
                in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD, parameter.VAR_POSITIONAL)
                for parameter in parameters.values()
            )
        except (TypeError, ValueError):
            accepts_request = True
        try:
            return (
                require_auth_dependency(request)
                if accepts_request
                else require_auth_dependency()
            )
        except HTTPException as exc:
            raise _http_error(exc.status_code, exc.detail, headers=exc.headers) from None
        except Exception:
            # A dependency failure is not an authentication failure.  Keep
            # backend/DB outages distinct from a missing principal while
            # exposing only a generic, cache-safe detail to the client.
            raise _http_error(
                503,
                {
                    "code": "unavailable",
                    "message": "認証サービスを利用できません。もう一度お試しください。",
                },
            ) from None

    async def _current_user(request: Request) -> UUID:
        try:
            info = await get_user_from_request(request)
            raw_id = info.get("id") if isinstance(info, dict) else None
        except Exception:
            # Authentication failures are deliberately indistinguishable from
            # an absent principal and never expose resolver/DB details.
            raise _http_error(401, "Not authenticated") from None
        if not raw_id:
            raise _http_error(401, "Not authenticated")
        try:
            return UUID(str(raw_id))
        except (TypeError, ValueError):
            # Do not retain attacker-controlled IDs (which may include a
            # secret-looking value) in an exception cause or traceback.
            raise _http_error(401, "Not authenticated") from None

    async def _open_session():
        try:
            return await get_db_manager().get_session()
        except Exception:
            raise _http_error(
                status_code=503,
                detail={"code": "unavailable", "message": "X Cookie設定を利用できません。"},
            ) from None

    def _reject_query(request: Request) -> None:
        if request.query_params:
            raise _http_error(
                status_code=400,
                detail={
                    "code": "invalid_format",
                    "status": "invalid_format",
                    "message": "Cookie本文以直接送信してください。クエリ指定は使用できません。",
                },
            )

    @router.get("")
    async def get_x_cookie_status(
        request: Request,
        response: Response,
        _auth: Any = Depends(_require_auth),
    ):
        _private_no_store(response)
        _reject_query(request)
        user_id = await _current_user(request)
        session = await _open_session()
        try:
            status = await resolve_x_cookie(session, user_id)
            return status.safe_status()
        finally:
            await session.close()

    @router.put("")
    @router.post("")
    async def put_x_cookie(
        request: Request,
        response: Response,
        _auth: Any = Depends(_require_auth),
    ):
        _private_no_store(response)
        _reject_query(request)
        user_id = await _current_user(request)
        content_type = str(request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if content_type.startswith("multipart/") or request.headers.get("content-disposition"):
            raise _http_error(
                status_code=415,
                detail={
                    "code": "invalid_format",
                    "status": "invalid_format",
                    "message": "multipartやファイル名付き送信ではなく、Cookie本文を直接送信してください。",
                },
            )
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > X_COOKIE_MAX_BYTES:
                    raise _http_error(
                        status_code=413,
                        detail={"code": "invalid_format", "status": "invalid_format", "message": "Cookieファイルが大きすぎます。"},
                    )
            except ValueError:
                raise _http_error(
                    status_code=400,
                    detail={"code": "invalid_format", "status": "invalid_format", "message": "リクエスト本文の長さが不正です。"},
                ) from None
        body = await request.body()
        if len(body) > X_COOKIE_MAX_BYTES:
            raise _http_error(
                status_code=413,
                detail={"code": "invalid_format", "status": "invalid_format", "message": "Cookieファイルが大きすぎます。"},
            )
        try:
            parsed = parse_x_cookie_bytes(body)
        except XCookieValidationError as exc:
            raise _http_error(400, _safe_validation_detail(exc)) from None

        session = await _open_session()
        try:
            try:
                row = await upsert_personal_x_cookie(session, user_id, parsed)
                await session.commit()
            except XCookieValidationError as exc:
                await session.rollback()
                raise _http_error(400, _safe_validation_detail(exc)) from None
            except Exception:
                await session.rollback()
                raise _http_error(
                    status_code=503,
                    detail={"code": "unavailable", "message": "X Cookieを保存できませんでした。もう一度お試しください。"},
                ) from None
            # Re-resolve only metadata; the encrypted payload itself is never
            # returned by the endpoint.
            status = await resolve_x_cookie(session, user_id)
            return status.safe_status()
        finally:
            await session.close()

    @router.delete("")
    async def delete_x_cookie(
        request: Request,
        response: Response,
        _auth: Any = Depends(_require_auth),
    ):
        _private_no_store(response)
        _reject_query(request)
        user_id = await _current_user(request)
        session = await _open_session()
        try:
            try:
                await disable_personal_x_cookie(session, user_id)
                await session.commit()
            except XCookieValidationError as exc:
                await session.rollback()
                raise _http_error(404, _safe_validation_detail(exc)) from None
            except Exception:
                await session.rollback()
                raise _http_error(
                    status_code=503,
                    detail={"code": "unavailable", "message": "X Cookieを削除できませんでした。もう一度お試しください。"},
                ) from None
            status = await resolve_x_cookie(session, user_id)
            return status.safe_status()
        finally:
            await session.close()

    return router


__all__ = ["create_x_cookie_router"]
