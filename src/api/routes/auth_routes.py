"""認証 (ログイン/ログアウト/トークン/履歴/パスワード変更) 系ルート (server.py から移設)"""

import logging
import math
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
import jwt

from ...features import Features
from ...memory.enterprise_bootstrap_repository import EnterpriseBootstrapRepository
from ..router_helpers import cookie_auth_dependency
from .payloads import ChangePasswordPayload, LoginPayload, ResetPasswordPayload
from ...security_secret import resolve_auth_secret_env

# Import database repositories (server.py と同じフォールバック付き)
try:
    from ...memory.login_log_repository import (
        LoginLogRepository,
        resolve_login_client_ip,
    )
    from ...memory.user_repository import UserRepository

    USER_REPOSITORY_AVAILABLE = True
except ImportError:
    LoginLogRepository = None
    resolve_login_client_ip = None
    UserRepository = None
    USER_REPOSITORY_AVAILABLE = False

# Import authentication service (server.py と同じフォールバック付き)
try:
    from ..auth_service import get_auth_service

    AUTH_SERVICE_AVAILABLE = True
except ImportError:
    AUTH_SERVICE_AVAILABLE = False
    get_auth_service = None

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)

LOGIN_BACKOFF_WINDOW = timedelta(minutes=10)
LOGIN_BACKOFF_THRESHOLD = 5
LOGIN_BACKOFF_MAX_SECONDS = 60
LOGIN_THROTTLED_FAILURE_REASON = "rate_limited"


def _normalized_login_username(username: str) -> str:
    return str(username or "").strip().lower()


def _trusted_login_client_ip(request: Request) -> str:
    """Resolve an audit/rate-limit address without trusting remote proxy headers."""
    peer = str(request.client.host or "") if request.client else ""
    forwarded_values = request.headers.getlist("x-forwarded-for")
    if resolve_login_client_ip is None:
        return peer or "unknown"
    return resolve_login_client_ip(peer, forwarded_values)


def _retry_after_for_attempts(attempts: list[Any], now: datetime) -> int | None:
    consecutive_failures = 0
    latest_failure_at: datetime | None = None
    for attempt in attempts:
        if bool(getattr(attempt, "success", False)):
            break
        if getattr(attempt, "failure_reason", None) == LOGIN_THROTTLED_FAILURE_REASON:
            continue
        created_at = getattr(attempt, "created_at", None)
        if not isinstance(created_at, datetime):
            continue
        if latest_failure_at is None:
            latest_failure_at = created_at
        consecutive_failures += 1

    if consecutive_failures < LOGIN_BACKOFF_THRESHOLD or latest_failure_at is None:
        return None
    delay_seconds = min(
        LOGIN_BACKOFF_MAX_SECONDS,
        2 ** (consecutive_failures - LOGIN_BACKOFF_THRESHOLD),
    )
    remaining = delay_seconds - max(0.0, (now - latest_failure_at).total_seconds())
    return max(1, math.ceil(remaining)) if remaining > 0 else None


@asynccontextmanager
async def _login_attempt_guard(
    server: "WebChatServer",
    request: Request,
    username: str,
) -> AsyncIterator[tuple[int | None, Any | None]]:
    """Hold a PostgreSQL per-principal lock through verification and audit."""
    db_manager = getattr(server, "_db_manager", None)
    if LoginLogRepository is None or db_manager is None:
        yield None, None
        return
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session = None
    lock_state: bool | None = None
    try:
        session = await db_manager.get_session()
        acquire_lock = getattr(
            LoginLogRepository,
            "acquire_login_throttle_lock",
            None,
        )
        if acquire_lock is not None:
            lock_state = await acquire_lock(
                session=session,
                normalized_username=_normalized_login_username(username),
                ip_address=_trusted_login_client_ip(request),
            )
        if lock_state is False:
            await session.rollback()
            await session.close()
            session = None
            retry_after = 1
        else:
            attempts = (
                await LoginLogRepository.get_recent_login_attempts_for_throttling(
                    session=session,
                    normalized_username=_normalized_login_username(username),
                    ip_address=_trusted_login_client_ip(request),
                    since=now - LOGIN_BACKOFF_WINDOW,
                )
            )
            retry_after = _retry_after_for_attempts(attempts, now)
    except Exception:
        logger.warning("Login backoff history lookup failed", exc_info=True)
        if session is not None:
            try:
                await session.rollback()
            except Exception:
                logger.warning("Login backoff session rollback failed", exc_info=True)
            try:
                await session.close()
            except Exception:
                logger.warning("Login backoff session close failed", exc_info=True)
        yield None, None
        return

    try:
        yield retry_after, session
    finally:
        if session is not None:
            try:
                # The transaction-scoped advisory lock is released here after
                # the authentication outcome has been durably audited.
                await session.rollback()
            except Exception:
                logger.warning("Login backoff session rollback failed", exc_info=True)
            try:
                await session.close()
            except Exception:
                logger.warning("Login backoff session close failed", exc_info=True)


def register_auth_routes(app: FastAPI, server: "WebChatServer") -> None:
    """auth status / login / logout / refresh / 履歴 / パスワード変更ルートを登録する"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)
    require_password_change_auth = cookie_auth_dependency(
        lambda request: server._enforce_cookie_auth(
            request, allow_password_reset=True
        )
    )

    async def require_admin(request: Request) -> dict:
        """Require an administrator for account-wide audit operations."""
        if not server.auth_enabled:
            return {"id": "default_user", "username": "default_user", "role": "admin"}
        user_info = await server._get_user_info_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Authentication required")
        if str(user_info.get("role", "")).lower() != "admin":
            raise HTTPException(
                status_code=403, detail="Administrator privileges required"
            )
        return user_info

    async def record_auth_event(**kwargs: Any) -> None:
        """認証監査を記録し、Enterpriseでは記録失敗を成功扱いにしない。"""
        recorded = await server._log_login_event(**kwargs)
        if Features.is_enterprise() and recorded is not True:
            raise HTTPException(
                status_code=503,
                detail="Authentication audit logging is unavailable",
            )

    async def enforce_login_backoff(
        payload: LoginPayload,
        request: Request,
        retry_after: int | None,
        session: Any | None,
    ) -> None:
        if retry_after is None:
            return
        await record_auth_event(
            username=payload.username,
            action="login",
            request=request,
            success=False,
            failure_reason=LOGIN_THROTTLED_FAILURE_REASON,
            session=session,
        )
        if session is not None:
            await session.commit()
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Try again shortly.",
            headers={"Retry-After": str(retry_after)},
        )

    async def authenticate_login(payload: LoginPayload, request: Request) -> Any:
        async with _login_attempt_guard(
            server,
            request,
            payload.username,
        ) as (retry_after, session):
            await enforce_login_backoff(payload, request, retry_after, session)

            user = await server._verify_credentials_async(
                payload.username,
                payload.password,
                session=session,
            )
            if not user:
                await record_auth_event(
                    username=payload.username,
                    action="login",
                    request=request,
                    success=False,
                    failure_reason="invalid_credentials",
                    session=session,
                )
                if session is not None:
                    await session.commit()
                raise HTTPException(status_code=401, detail="Invalid credentials")

            if hasattr(user, "is_active") and not user.is_active:
                await record_auth_event(
                    username=payload.username,
                    action="login",
                    request=request,
                    success=False,
                    failure_reason="account_disabled",
                    session=session,
                )
                if session is not None:
                    await session.commit()
                raise HTTPException(status_code=401, detail="Account is disabled")

            await record_auth_event(
                username=payload.username,
                action="login",
                request=request,
                success=True,
                session=session,
            )
            if session is not None:
                await session.commit()
            return user

    @app.get("/internal/enterprise/lan-access", include_in_schema=False)
    async def enterprise_network_access(request: Request):
        """Fail-closed authorization endpoint for the Enterprise public proxy."""
        if not Features.is_enterprise():
            return Response(status_code=404, headers={"Cache-Control": "no-store"})

        expected_key = os.getenv("AOITALK_CADDY_GATE_KEY", "")
        supplied_keys = request.headers.getlist("x-aoitalk-gate-auth")
        if (
            not expected_key
            or len(supplied_keys) != 1
            or not secrets.compare_digest(supplied_keys[0], expected_key)
        ):
            return Response(status_code=403, headers={"Cache-Control": "no-store"})
        if not USER_REPOSITORY_AVAILABLE or server._db_manager is None:
            return Response(status_code=503, headers={"Cache-Control": "no-store"})

        session = None
        try:
            session = await server._db_manager.get_session()
            unlocked = await EnterpriseBootstrapRepository.is_complete(session)
            if unlocked:
                await session.commit()
        except Exception:
            logger.exception("Enterprise public access state lookup failed")
            return Response(status_code=503, headers={"Cache-Control": "no-store"})
        finally:
            if session is not None:
                await session.close()

        if not unlocked:
            return Response(status_code=403, headers={"Cache-Control": "no-store"})
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.get("/api/auth/status")
    async def auth_status(request: Request):
        """Check whether the request is authenticated"""
        return JSONResponse(
            {"authenticated": server._is_request_authenticated(request)}
        )

    @app.post("/api/auth/login")
    async def login(payload: LoginPayload, request: Request):
        """Login and set session cookie (DB-based authentication)"""
        if not server.auth_enabled:
            return JSONResponse({"authenticated": True})

        user = await authenticate_login(payload, request)

        # Store login time for session duration calculation
        server._login_sessions[payload.username] = datetime.utcnow()

        session_id = server._sign_session(
            payload.username,
            session_version=int(getattr(user, "session_version", 1) or 1),
        )

        # Build response with user info
        response_data = {
            "authenticated": True,
            "user": {
                "username": payload.username,
                "role": (
                    getattr(user, "role", "user")
                    if hasattr(user, "role")
                    else "user"
                ),
                "display_name": (
                    getattr(user, "display_name", None)
                    if hasattr(user, "display_name")
                    else None
                ),
                "password_reset_required": (
                    getattr(user, "is_password_reset_required", False)
                    if hasattr(user, "is_password_reset_required")
                    else False
                ),
            },
        }

        response = JSONResponse(response_data)
        server._set_session_cookie(
            response, session_id, request.url.scheme == "https"
        )
        return response

    async def login_mobile(payload: LoginPayload, request: Request):
        """モバイルアプリ向けログイン — JWTトークンをレスポンスボディで返す"""
        if not server.auth_enabled:
            return JSONResponse({"success": True, "access_token": "no-auth"})

        user = await authenticate_login(payload, request)

        # ログイン記録
        server._login_sessions[payload.username] = datetime.utcnow()
        # JWT トークン生成
        if AUTH_SERVICE_AVAILABLE:
            auth_service = get_auth_service()
            result = auth_service.create_auth_result(
                user_id=str(user.id) if hasattr(user, "id") else payload.username,
                username=payload.username,
                role=(
                    getattr(user, "role", "user")
                    if hasattr(user, "role")
                    else "user"
                ),
                is_password_reset_required=bool(
                    getattr(user, "is_password_reset_required", False)
                ),
                session_version=int(getattr(user, "session_version", 1) or 1),
            )
            return JSONResponse(result.dict())
        else:
            raise HTTPException(status_code=500, detail="Auth service unavailable")

    if not Features.is_enterprise():
        app.post("/api/auth/login/mobile")(login_mobile)

    async def refresh_token(request: Request):
        """アクセストークンのリフレッシュ"""
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.lower().startswith("bearer ") or not AUTH_SERVICE_AVAILABLE:
            raise HTTPException(status_code=401, detail="Token required")

        auth_service = get_auth_service()
        token = auth_header[7:]

        # AuthServiceだけではJWT発行後にDB側でreset/active状態が変わった
        # ケースを知れないため、refresh前に署名済みpayloadの主体をDBで再確認する。
        if server._db_manager is None or not USER_REPOSITORY_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database unavailable")
        try:
            raw_payload = jwt.decode(
                token,
                auth_service.secret_key,
                algorithms=[auth_service.algorithm],
                options={"verify_exp": False},
            )
            user_id = UUID(str(raw_payload["user_id"]))
            if raw_payload.get("type") != "access":
                raise HTTPException(status_code=401, detail="Token expired or invalid")
            # Require the standard access-token expiry claim even when the
            # token is being decoded without exp verification below.
            expires_at = datetime.utcfromtimestamp(int(raw_payload["exp"]))
            issued_at = datetime.utcfromtimestamp(int(raw_payload["iat"]))
            token_session_version = max(
                1, int(raw_payload.get("session_version", 1) or 1)
            )
            now = datetime.utcnow()
            token_age = now - issued_at
            # Mobile access tokens may expire while the app is offline. Keep
            # the refresh window bounded by the original issue time, then
            # recheck the user's active/password/session state below.
            if (
                issued_at > now + timedelta(seconds=60)
                or token_age > timedelta(days=30)
            ):
                raise HTTPException(status_code=401, detail="Token expired or invalid")
            db_session = await server._db_manager.get_session()
            try:
                user = await UserRepository.get_by_id(db_session, user_id)
                if user is None or not user.is_active or user.is_password_reset_required:
                    raise HTTPException(status_code=401, detail="Token no longer valid")
                if int(getattr(user, "session_version", 1) or 1) != token_session_version:
                    raise HTTPException(status_code=401, detail="Token no longer valid")
            finally:
                await db_session.close()
        except HTTPException:
            raise
        except (jwt.InvalidTokenError, KeyError, ValueError) as exc:
            logger.info("Refresh token subject validation failed: %s", exc)
            raise HTTPException(status_code=401, detail="Token expired or invalid")

        # Role/username are mutable DB state; do not extend stale JWT claims.
        new_token = auth_service.create_access_token(
            user_id=str(user.id),
            username=user.username,
            role=user.role,
            session_version=int(getattr(user, "session_version", 1) or 1),
        )
        if not new_token:
            raise HTTPException(status_code=401, detail="Token expired or invalid")

        return JSONResponse({"access_token": new_token})

    if not Features.is_enterprise():
        app.post("/api/auth/refresh")(refresh_token)

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        """Logout and clear session cookie"""
        if not server.auth_enabled:
            return JSONResponse({"authenticated": False})

        # Resolve every supported credential type before revocation. Bearer JWT,
        # long-lived API token, and Next.js cookie must also revoke the account
        # generation; deleting only the FastAPI cookie is insufficient.
        username = None
        session_duration = None
        revoked_user_id = None
        revocation_failed = False

        try:
            user_info = await server._get_user_info_from_request(
                request,
                allow_password_reset=True,
                raise_on_db_error=Features.is_enterprise(),
            )
            if user_info:
                username = user_info.get("username")
        except Exception as exc:
            logger.warning("Could not resolve logout principal: %s", exc)
            if Features.is_enterprise():
                revocation_failed = True

        try:
            session_id = server._get_request_cookie(request, server.cookie_name)
            if session_id:
                serializer = server._get_serializer()
                if serializer:
                    session_data = serializer.loads(
                        session_id, max_age=server.session_ttl_seconds
                    )
                    # Calculate session duration
                    if username and username in server._login_sessions:
                        login_time = server._login_sessions[username]
                        session_duration = int(
                            (datetime.utcnow() - login_time).total_seconds()
                        )
                        # Clean up session tracking
                        del server._login_sessions[username]
        except Exception as e:
            logger.debug(
                f"Could not extract username from session for logout logging: {e}"
            )

        global_revocation = False

        # Invalidate every token/session for the account, not only the browser
        # cookie being deleted.  This also makes logout close authenticated
        # WebSockets and invalidate API/JWT/Next.js sessions consistently.
        if username and server._db_manager is not None and USER_REPOSITORY_AVAILABLE:
            try:
                db_session = await server._db_manager.get_session()
                try:
                    user = await UserRepository.invalidate_sessions_by_username(
                        db_session, username
                    )
                    global_revocation = user is not None
                    revoked_user_id = str(user.id) if user is not None else None
                finally:
                    await db_session.close()
                if revoked_user_id and hasattr(server.manager, "disconnect_user"):
                    await server.manager.disconnect_user(revoked_user_id)
            except Exception as exc:
                revocation_failed = True
                global_revocation = False
                logger.error("Failed to revoke all sessions during logout: %s", exc)
        elif username:
            # An Enterprise logout must be able to revoke the account record;
            # clearing only the browser cookie is not an acceptable fallback.
            global_revocation = False
            revocation_failed = True

        # Log logout event
        if username:
            audit_recorded = await server._log_login_event(
                username=username,
                action="logout",
                request=request,
                success=True,
                session_duration=session_duration,
            )
            if Features.is_enterprise() and audit_recorded is not True:
                revocation_failed = True

        status_code = 503 if revocation_failed and Features.is_enterprise() else 200
        response = JSONResponse(
            {
                "authenticated": False,
                "global_revocation": global_revocation,
            },
            status_code=status_code,
        )
        for cookie_name in {
            server.cookie_name,
            server.legacy_cookie_name,
            server.next_cookie_name,
        }:
            response.delete_cookie(cookie_name)
        return response

    @app.get("/api/auth/login-history")
    async def get_login_history(
        request: Request,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        action: Optional[str] = None,
        username: Optional[str] = None,
        success: Optional[bool] = None,
        _: None = Depends(require_auth),
    ):
        """Get login/logout history with filtering and pagination"""
        await require_admin(request)
        if server._db_manager is None or LoginLogRepository is None:
            raise HTTPException(
                status_code=503,
                detail="Login history logging is not available (database not configured)",
            )

        try:
            # Get database session
            session = await server._db_manager.get_session()
            try:
                logs, total_count = await LoginLogRepository.get_login_history(
                    session=session,
                    limit=limit,
                    offset=offset,
                    username=username,
                    action=action,
                    success=success,
                )

                return JSONResponse(
                    {
                        "logs": [log.to_dict() for log in logs],
                        "total_count": total_count,
                        "limit": limit,
                        "offset": offset,
                    }
                )
            finally:
                await session.close()
        except Exception as e:
            logger.error(f"Failed to get login history: {e}")
            raise HTTPException(
                status_code=500, detail="Failed to retrieve login history"
            )

    @app.delete("/api/auth/login-history/clear")
    async def clear_login_history(
        request: Request,
        before_date: Optional[str] = None,
        _: None = Depends(require_auth),
    ):
        """Clear login history logs

        Args:
            before_date: ISO format date string. If provided, delete logs before this date.
                        If not provided, delete all logs.
        """
        await require_admin(request)
        if server._db_manager is None or LoginLogRepository is None:
            raise HTTPException(
                status_code=503,
                detail="Login history logging is not available (database not configured)",
            )

        try:
            session = await server._db_manager.get_session()
            try:
                if before_date:
                    # Parse date string
                    try:
                        before_dt = datetime.fromisoformat(
                            before_date.replace("Z", "+00:00")
                        )
                    except ValueError:
                        raise HTTPException(
                            status_code=400,
                            detail="Invalid date format. Use ISO format.",
                        )

                    deleted_count = await LoginLogRepository.delete_logs_before(
                        session=session, before_date=before_dt
                    )
                    message = (
                        f"Deleted {deleted_count} log entries before {before_date}"
                    )
                else:
                    # Clear all logs
                    deleted_count = await LoginLogRepository.clear_all_logs(
                        session=session
                    )
                    message = f"Deleted all {deleted_count} log entries"

                logger.info(f"Login history cleared: {message}")
                return JSONResponse(
                    {"deleted_count": deleted_count, "message": message}
                )
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to clear login history: {e}")
            raise HTTPException(
                status_code=500, detail="Failed to clear login history"
            )

    @app.post("/api/auth/change-password")
    async def self_change_password(
        payload: ChangePasswordPayload,
        request: Request,
        _: None = Depends(require_password_change_auth),
    ):
        """Change own password (requires current password)"""
        if not USER_REPOSITORY_AVAILABLE or server._db_manager is None:
            raise HTTPException(
                status_code=503,
                detail="Password change is not available (database not configured)",
            )

        # Get current user from FastAPI cookie or mobile Bearer JWT.
        # The Next.js BFF has its own route; this backend path is used by mobile
        # clients and direct FastAPI callers.
        try:
            user = None
            auth_header = request.headers.get("Authorization", "")
            # Next.js BFF requests carry a verified internal key and the
            # canonical user id.  Resolve that id through the repository just
            # like cookie/Bearer callers instead of trusting a username or
            # duplicating password mutation logic in the BFF.
            forwarded_user_id = server._get_next_user_id_from_request(
                request,
                allow_password_reset=True,
                raise_on_db_error=Features.is_enterprise(),
            )
            if forwarded_user_id and server._db_manager is not None:
                db_session = await server._db_manager.get_session()
                try:
                    user = await UserRepository.get_by_id(
                        db_session, UUID(str(forwarded_user_id))
                    )
                finally:
                    await db_session.close()
            elif auth_header.lower().startswith("bearer ") and AUTH_SERVICE_AVAILABLE:
                token_payload = get_auth_service().verify_token(auth_header[7:])
                if token_payload and server._db_manager is not None:
                    db_session = await server._db_manager.get_session()
                    try:
                        user = await UserRepository.get_by_id(
                            db_session, UUID(str(token_payload.user_id))
                        )
                    finally:
                        await db_session.close()
            else:
                session_id = server._get_request_cookie(request, server.cookie_name)
                if not session_id:
                    session_id = server._get_request_cookie(
                        request, server.legacy_cookie_name
                    )
                serializer = server._get_serializer()
                if serializer and session_id and server._db_manager is not None:
                    session_data = serializer.loads(
                        session_id, max_age=server.session_ttl_seconds
                    )
                    username = session_data.get("u")
                    if username:
                        db_session = await server._db_manager.get_session()
                        try:
                            user = await UserRepository.get_by_username(
                                db_session, username
                            )
                        finally:
                            await db_session.close()

            if user is None or not user.is_active:
                raise HTTPException(status_code=401, detail="Not authenticated")
        except Exception as e:
            if isinstance(e, HTTPException):
                raise
            logger.error(f"Failed to get session: {e}")
            raise HTTPException(status_code=401, detail="Invalid session")

        # 初回管理者/Enterprise bootstrapはcurrent passwordなしで変更できる。
        # 通常ユーザーは従来どおりcurrent passwordを要求する。
        if not user.is_password_reset_required and not payload.current_password:
            raise HTTPException(
                status_code=400, detail="Current password is required"
            )

        try:
            db_session = await server._db_manager.get_session()
            try:
                if payload.current_password and not UserRepository.verify_password(
                    payload.current_password, user.password_hash
                ):
                    raise HTTPException(
                        status_code=401, detail="Current password is incorrect"
                    )

                # Update password
                success = await UserRepository.update_password(
                    session=db_session,
                    user_id=user.id,
                    new_password=payload.new_password,
                    clear_reset_flag=True,
                )

                if not success:
                    raise HTTPException(
                        status_code=500, detail="Failed to update password"
                    )

                updated_user = await UserRepository.get_by_id(db_session, user.id)
                if updated_user is None:
                    raise HTTPException(
                        status_code=500, detail="Failed to reload updated user"
                    )
                logger.info(f"User changed own password: {user.username}")
                response_data = {
                    "success": True,
                    "message": "Password changed successfully",
                    "session_version": int(
                        getattr(updated_user, "session_version", 1) or 1
                    ),
                }
                # Mobile clients authenticate with the reset-required JWT. Return
                # a fresh normal token so the client is not locked out by the old
                # claim after the database flag is cleared.
                if (
                    auth_header.lower().startswith("bearer ")
                    and AUTH_SERVICE_AVAILABLE
                ):
                    response_data.update(
                        {
                            "access_token": get_auth_service().create_access_token(
                                user_id=str(user.id),
                                username=updated_user.username,
                                role=updated_user.role,
                                is_password_reset_required=False,
                                session_version=int(
                                    getattr(updated_user, "session_version", 1) or 1
                                ),
                            ),
                            "token_type": "bearer",
                            "expires_in": get_auth_service().access_token_expire_minutes
                            * 60,
                        }
                    )
                response = JSONResponse(response_data)
                response.set_cookie(
                    key=server.cookie_name,
                    value=server._sign_session(
                        user.username,
                        session_version=int(
                            getattr(updated_user, "session_version", 1) or 1
                        ),
                    ),
                    httponly=True,
                    samesite="lax",
                    secure=request.url.scheme == "https",
                    max_age=server.session_ttl_seconds,
                )
                return response
            finally:
                await db_session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to change password: {e}")
            raise HTTPException(status_code=500, detail="Failed to change password")

    @app.post("/api/auth/reset-password")
    async def complete_password_reset(payload: ResetPasswordPayload):
        """Consume a signed one-time reset token through the canonical repo."""
        if not USER_REPOSITORY_AVAILABLE or server._db_manager is None:
            raise HTTPException(
                status_code=503,
                detail="Password reset is not available (database not configured)",
            )
        try:
            secret = resolve_auth_secret_env(
                ("NEXTAUTH_SECRET", "AUTH_SECRET", "AOITALK_JWT_SECRET"),
                error_type=ValueError,
            )
            if not secret:
                raise HTTPException(status_code=503, detail="Password reset secret is not configured")
            claims = jwt.decode(
                payload.token,
                secret,
                algorithms=["HS256"],
                options={"require": ["exp", "sub"]},
            )
            if claims.get("purpose") != "password_reset":
                raise ValueError("Invalid password reset token")
            user_id = UUID(str(claims.get("sub")))
            token_version = claims.get("session_version")
            if not isinstance(token_version, int) or token_version < 1:
                raise ValueError("Invalid password reset token")
        except HTTPException:
            raise
        except (ValueError, TypeError, jwt.PyJWTError) as exc:
            raise HTTPException(
                status_code=400,
                detail="再設定リンクが無効または期限切れです",
            ) from exc

        try:
            session = await server._db_manager.get_session()
            try:
                async with session.begin():
                    user = await UserRepository.complete_password_reset(
                        session,
                        user_id,
                        token_version,
                        payload.password,
                        commit=False,
                    )
                    if not user:
                        raise HTTPException(
                            status_code=400,
                            detail="再設定リンクは既に使用済みか無効です",
                        )
                return JSONResponse({"success": True})
            finally:
                await session.close()
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error(f"Failed to complete password reset: {exc}")
            raise HTTPException(status_code=500, detail="Failed to reset password") from exc
