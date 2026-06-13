"""認証 (ログイン/ログアウト/トークン/履歴/パスワード変更) 系ルート (server.py から移設)"""

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ..router_helpers import cookie_auth_dependency
from .payloads import ChangePasswordPayload, LoginPayload

# Import database repositories (server.py と同じフォールバック付き)
try:
    from ...memory.login_log_repository import LoginLogRepository
    from ...memory.user_repository import UserRepository

    USER_REPOSITORY_AVAILABLE = True
except ImportError:
    LoginLogRepository = None
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


def register_auth_routes(app: FastAPI, server: "WebChatServer") -> None:
    """auth status / login / logout / refresh / 履歴 / パスワード変更ルートを登録する"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

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

        # Verify credentials against database
        user = await server._verify_credentials_async(
            payload.username, payload.password
        )

        if not user:
            # Log failed login attempt
            await server._log_login_event(
                username=payload.username,
                action="login",
                request=request,
                success=False,
                failure_reason="invalid_credentials",
            )
            raise HTTPException(status_code=401, detail="Invalid credentials")

        # Check if user is active
        if hasattr(user, "is_active") and not user.is_active:
            await server._log_login_event(
                username=payload.username,
                action="login",
                request=request,
                success=False,
                failure_reason="account_disabled",
            )
            raise HTTPException(status_code=401, detail="Account is disabled")

        # Store login time for session duration calculation
        server._login_sessions[payload.username] = datetime.utcnow()

        # Log successful login
        await server._log_login_event(
            username=payload.username, action="login", request=request, success=True
        )

        session_id = server._sign_session(payload.username)

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

    @app.post("/api/auth/login/mobile")
    async def login_mobile(payload: LoginPayload, request: Request):
        """モバイルアプリ向けログイン — JWTトークンをレスポンスボディで返す"""
        if not server.auth_enabled:
            return JSONResponse({"success": True, "access_token": "no-auth"})

        user = await server._verify_credentials_async(
            payload.username, payload.password
        )

        if not user:
            await server._log_login_event(
                username=payload.username,
                action="login",
                request=request,
                success=False,
                failure_reason="invalid_credentials",
            )
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if hasattr(user, "is_active") and not user.is_active:
            raise HTTPException(status_code=401, detail="Account is disabled")

        # ログイン記録
        server._login_sessions[payload.username] = datetime.utcnow()
        await server._log_login_event(
            username=payload.username, action="login", request=request, success=True
        )

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
            )
            return JSONResponse(result.dict())
        else:
            raise HTTPException(status_code=500, detail="Auth service unavailable")

    @app.post("/api/auth/refresh")
    async def refresh_token(request: Request):
        """アクセストークンのリフレッシュ"""
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer ") or not AUTH_SERVICE_AVAILABLE:
            raise HTTPException(status_code=401, detail="Token required")

        auth_service = get_auth_service()
        new_token = auth_service.refresh_token(auth_header[7:])
        if not new_token:
            raise HTTPException(status_code=401, detail="Token expired or invalid")

        return JSONResponse({"access_token": new_token})

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        """Logout and clear session cookie"""
        if not server.auth_enabled:
            return JSONResponse({"authenticated": False})

        # Try to get username from session to log logout
        username = None
        session_duration = None

        try:
            session_id = request.cookies.get(server.cookie_name)
            if session_id:
                serializer = server._get_serializer()
                if serializer:
                    session_data = serializer.loads(
                        session_id, max_age=server.session_ttl_seconds
                    )
                    username = session_data.get("u")

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

        # Log logout event
        if username:
            await server._log_login_event(
                username=username,
                action="logout",
                request=request,
                success=True,
                session_duration=session_duration,
            )

        response = JSONResponse({"authenticated": False})
        response.delete_cookie(server.cookie_name)
        return response

    @app.get("/api/auth/login-history")
    async def get_login_history(
        request: Request,
        limit: int = 100,
        offset: int = 0,
        action: Optional[str] = None,
        username: Optional[str] = None,
        success: Optional[bool] = None,
        _: None = Depends(require_auth),
    ):
        """Get login/logout history with filtering and pagination"""
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
                    limit=min(limit, 500),  # Cap at 500 records
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
        _: None = Depends(require_auth),
    ):
        """Change own password (requires current password)"""
        if not USER_REPOSITORY_AVAILABLE or server._db_manager is None:
            raise HTTPException(
                status_code=503,
                detail="Password change is not available (database not configured)",
            )

        # Get current user from session
        try:
            session_id = request.cookies.get(server.cookie_name)
            if not session_id:
                raise HTTPException(status_code=401, detail="Not authenticated")

            serializer = server._get_serializer()
            if not serializer:
                raise HTTPException(status_code=500, detail="Auth not configured")

            session_data = serializer.loads(
                session_id, max_age=server.session_ttl_seconds
            )
            username = session_data.get("u")

            if not username:
                raise HTTPException(status_code=401, detail="Invalid session")
        except Exception as e:
            logger.error(f"Failed to get session: {e}")
            raise HTTPException(status_code=401, detail="Invalid session")

        # Require current password for non-admin self-change
        if not payload.current_password:
            raise HTTPException(
                status_code=400, detail="Current password is required"
            )

        try:
            db_session = await server._db_manager.get_session()
            try:
                # Verify current password
                user = await UserRepository.authenticate(
                    session=db_session,
                    username=username,
                    password=payload.current_password,
                )

                if not user:
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

                logger.info(f"User changed own password: {username}")
                return JSONResponse(
                    {"success": True, "message": "Password changed successfully"}
                )
            finally:
                await db_session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to change password: {e}")
            raise HTTPException(status_code=500, detail="Failed to change password")
