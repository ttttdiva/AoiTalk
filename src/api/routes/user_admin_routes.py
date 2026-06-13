"""ユーザー管理 (管理者専用 CRUD / CSV import・export) 系ルート (server.py から移設)"""

import logging
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from .payloads import ChangePasswordPayload, CreateUserPayload, UpdateUserPayload

# Import user repository (server.py と同じフォールバック付き)
try:
    from ...memory.user_repository import UserRepository

    USER_REPOSITORY_AVAILABLE = True
except ImportError:
    UserRepository = None
    USER_REPOSITORY_AVAILABLE = False

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


def register_user_admin_routes(app: FastAPI, server: "WebChatServer") -> None:
    """管理者専用のユーザー管理ルートを登録する"""

    # ── User Management API Endpoints (Admin only) ────────────────────────

    async def require_admin(request: Request) -> None:
        """Require admin role for the endpoint"""
        server._enforce_cookie_auth(request)

        # Check if user has admin role
        is_admin = await server._is_admin_user(request)
        if not is_admin:
            raise HTTPException(
                status_code=403, detail="Administrator privileges required"
            )

    @app.get("/api/users")
    async def list_users(
        request: Request,
        limit: int = 100,
        offset: int = 0,
        include_inactive: bool = False,
        _: None = Depends(require_admin),
    ):
        """List all users (admin only)"""
        if not USER_REPOSITORY_AVAILABLE or server._db_manager is None:
            raise HTTPException(
                status_code=503,
                detail="User management is not available (database not configured)",
            )

        try:
            session = await server._db_manager.get_session()
            try:
                users, total_count = await UserRepository.list_users(
                    session=session,
                    limit=min(limit, 500),
                    offset=offset,
                    include_inactive=include_inactive,
                )

                return JSONResponse(
                    {
                        "users": [user.to_dict() for user in users],
                        "total_count": total_count,
                        "limit": limit,
                        "offset": offset,
                    }
                )
            finally:
                await session.close()
        except Exception as e:
            logger.error(f"Failed to list users: {e}")
            raise HTTPException(status_code=500, detail="Failed to list users")

    @app.post("/api/users")
    async def create_user(
        payload: CreateUserPayload,
        request: Request,
        _: None = Depends(require_admin),
    ):
        """Create a new user (admin only)"""
        if not USER_REPOSITORY_AVAILABLE or server._db_manager is None:
            raise HTTPException(
                status_code=503,
                detail="User management is not available (database not configured)",
            )

        try:
            session = await server._db_manager.get_session()
            try:
                user = await UserRepository.create_user(
                    session=session,
                    username=payload.username,
                    password=payload.password,
                    email=payload.email,
                    display_name=payload.display_name,
                    role=payload.role,
                    is_password_reset_required=True,  # Always require password change
                )

                logger.info(f"User created: {payload.username} (by admin)")
                return JSONResponse(
                    {
                        "success": True,
                        "user": user.to_dict(),
                        "message": f"User '{payload.username}' created successfully",
                    }
                )
            finally:
                await session.close()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error(f"Failed to create user: {e}")
            raise HTTPException(status_code=500, detail="Failed to create user")

    # ── User CSV Import/Export Endpoints (Admin only) ─────────────────────
    # NOTE: These must be registered BEFORE /api/users/{user_id} routes
    # to prevent FastAPI from matching "export"/"import" as a user_id.

    @app.get("/api/users/export")
    async def export_users_csv(request: Request, _: None = Depends(require_admin)):
        """Export all users as CSV (admin only)"""
        if not USER_REPOSITORY_AVAILABLE or server._db_manager is None:
            raise HTTPException(
                status_code=503,
                detail="User management is not available (database not configured)",
            )

        try:
            import csv
            import io
            from datetime import datetime as dt

            session = await server._db_manager.get_session()
            try:
                users, _ = await UserRepository.list_users(
                    session=session, limit=10000, include_inactive=True
                )

                # Create CSV in memory (with BOM for Excel compatibility)
                output = io.StringIO()
                output.write("\ufeff")  # UTF-8 BOM
                writer = csv.writer(output)

                # Header row
                writer.writerow(
                    [
                        "username",
                        "password",
                        "email",
                        "display_name",
                        "role",
                        "is_active",
                    ]
                )

                # Data rows (password column is empty for security)
                for user in users:
                    writer.writerow(
                        [
                            user.username,
                            "",  # Password is not exported
                            user.email or "",
                            user.display_name or "",
                            user.role or "user",
                            "true" if user.is_active else "false",
                        ]
                    )

                csv_content = output.getvalue()
                output.close()

                # Generate filename with date
                filename = f"users_{dt.now().strftime('%Y%m%d')}.csv"

                from fastapi.responses import Response

                return Response(
                    content=csv_content,
                    media_type="text/csv",
                    headers={
                        "Content-Disposition": f"attachment; filename={filename}"
                    },
                )
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to export users: {e}")
            raise HTTPException(status_code=500, detail="Failed to export users")

    @app.post("/api/users/import")
    async def import_users_csv(
        request: Request,
        file: UploadFile = File(...),
        _: None = Depends(require_admin),
    ):
        """Import users from CSV (admin only)"""
        if not USER_REPOSITORY_AVAILABLE or server._db_manager is None:
            raise HTTPException(
                status_code=503,
                detail="User management is not available (database not configured)",
            )

        if not file.filename or not file.filename.endswith(".csv"):
            raise HTTPException(
                status_code=400, detail="CSVファイルをアップロードしてください"
            )

        try:
            import csv
            import io

            # Read file content
            content = await file.read()
            try:
                text_content = content.decode("utf-8")
            except UnicodeDecodeError:
                # Try Shift-JIS for Japanese Excel files
                text_content = content.decode("shift-jis")

            # Remove BOM if present
            if text_content.startswith("\ufeff"):
                text_content = text_content[1:]

            reader = csv.DictReader(io.StringIO(text_content))

            # Validate headers
            required_headers = {"username"}
            if not required_headers.issubset(set(reader.fieldnames or [])):
                raise HTTPException(
                    status_code=400, detail="CSVにusernameカラムが必要です"
                )

            results = {"created": 0, "updated": 0, "skipped": 0, "errors": []}

            session = await server._db_manager.get_session()
            try:
                for row_num, row in enumerate(
                    reader, start=2
                ):  # Start at 2 (header is row 1)
                    username = row.get("username", "").strip()
                    if not username:
                        results["skipped"] += 1
                        continue

                    password = row.get("password", "").strip()
                    email = row.get("email", "").strip() or None
                    display_name = row.get("display_name", "").strip() or None
                    role = row.get("role", "user").strip().lower()
                    is_active_str = row.get("is_active", "true").strip().lower()
                    is_active = is_active_str in ("true", "1", "yes", "on")

                    # Validate role
                    if role not in ("admin", "user"):
                        role = "user"

                    try:
                        # Check if user exists
                        existing_user = await UserRepository.get_by_username(
                            session, username
                        )

                        if existing_user:
                            # Update existing user
                            update_data = {
                                "email": email,
                                "display_name": display_name,
                                "role": role,
                                "is_active": is_active,
                            }
                            await UserRepository.update_user(
                                session=session,
                                user_id=existing_user.id,
                                **update_data,
                            )

                            # Update password if provided
                            if password:
                                await UserRepository.update_password(
                                    session=session,
                                    user_id=existing_user.id,
                                    new_password=password,
                                    clear_reset_flag=False,
                                )

                            results["updated"] += 1
                        else:
                            # Create new user (password required)
                            if not password:
                                results["errors"].append(
                                    f"行{row_num}: 新規ユーザー '{username}' にはパスワードが必要です"
                                )
                                results["skipped"] += 1
                                continue

                            await UserRepository.create_user(
                                session=session,
                                username=username,
                                password=password,
                                email=email,
                                display_name=display_name,
                                role=role,
                                is_password_reset_required=True,
                            )
                            results["created"] += 1
                    except Exception as e:
                        results["errors"].append(
                            f"行{row_num}: {username} - {str(e)}"
                        )

                logger.info(
                    f"CSV import completed: created={results['created']}, updated={results['updated']}, skipped={results['skipped']}, errors={len(results['errors'])}"
                )

                return JSONResponse(
                    {
                        "success": True,
                        "created": results["created"],
                        "updated": results["updated"],
                        "skipped": results["skipped"],
                        "errors": results["errors"][:10],  # Limit error messages
                        "message": f"インポート完了: {results['created']}件作成, {results['updated']}件更新",
                    }
                )
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to import users: {e}")
            raise HTTPException(
                status_code=500, detail=f"インポートに失敗しました: {str(e)}"
            )

    @app.get("/api/users/{user_id}")
    async def get_user(
        user_id: str, request: Request, _: None = Depends(require_admin)
    ):
        """Get user details (admin only)"""
        if not USER_REPOSITORY_AVAILABLE or server._db_manager is None:
            raise HTTPException(
                status_code=503,
                detail="User management is not available (database not configured)",
            )

        try:
            from uuid import UUID as PyUUID

            uuid_obj = PyUUID(user_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid user ID format")

        try:
            session = await server._db_manager.get_session()
            try:
                user = await UserRepository.get_by_id(session, uuid_obj)
                if not user:
                    raise HTTPException(status_code=404, detail="User not found")

                return JSONResponse({"user": user.to_dict()})
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get user: {e}")
            raise HTTPException(status_code=500, detail="Failed to get user")

    @app.patch("/api/users/{user_id}")
    async def update_user(
        user_id: str,
        payload: UpdateUserPayload,
        request: Request,
        _: None = Depends(require_admin),
    ):
        """Update user details (admin only)"""
        if not USER_REPOSITORY_AVAILABLE or server._db_manager is None:
            raise HTTPException(
                status_code=503,
                detail="User management is not available (database not configured)",
            )

        try:
            from uuid import UUID as PyUUID

            uuid_obj = PyUUID(user_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid user ID format")

        # Build update dict from non-None values
        update_data = {
            k: v for k, v in payload.model_dump().items() if v is not None
        }

        if not update_data:
            raise HTTPException(status_code=400, detail="No update data provided")

        try:
            session = await server._db_manager.get_session()
            try:
                if "user_settings" in update_data:
                    existing = await UserRepository.get_by_id(session, uuid_obj)
                    if existing:
                        merged = dict(existing.user_settings or {})
                        merged.update(update_data["user_settings"])
                        update_data["user_settings"] = merged

                user = await UserRepository.update_user(
                    session=session, user_id=uuid_obj, **update_data
                )

                if not user:
                    raise HTTPException(status_code=404, detail="User not found")

                logger.info(f"User updated: {user.username}")
                return JSONResponse(
                    {
                        "success": True,
                        "user": user.to_dict(),
                        "message": f"User '{user.username}' updated successfully",
                    }
                )
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to update user: {e}")
            raise HTTPException(status_code=500, detail="Failed to update user")

    @app.delete("/api/users/{user_id}")
    async def delete_user(
        user_id: str, request: Request, _: None = Depends(require_admin)
    ):
        """Delete a user (admin only)"""
        if not USER_REPOSITORY_AVAILABLE or server._db_manager is None:
            raise HTTPException(
                status_code=503,
                detail="User management is not available (database not configured)",
            )

        try:
            from uuid import UUID as PyUUID

            uuid_obj = PyUUID(user_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid user ID format")

        try:
            session = await server._db_manager.get_session()
            try:
                # Prevent deleting the last admin
                user = await UserRepository.get_by_id(session, uuid_obj)
                if not user:
                    raise HTTPException(status_code=404, detail="User not found")

                if user.role == "admin":
                    admin_count = await UserRepository.count_admins(session)
                    if admin_count <= 1:
                        raise HTTPException(
                            status_code=400,
                            detail="Cannot delete the last admin user",
                        )

                username = user.username
                deleted = await UserRepository.delete_user(session, uuid_obj)

                if not deleted:
                    raise HTTPException(status_code=404, detail="User not found")

                logger.info(f"User deleted: {username}")
                return JSONResponse(
                    {
                        "success": True,
                        "message": f"User '{username}' deleted successfully",
                    }
                )
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to delete user: {e}")
            raise HTTPException(status_code=500, detail="Failed to delete user")

    @app.post("/api/users/{user_id}/change-password")
    async def admin_change_password(
        user_id: str,
        payload: ChangePasswordPayload,
        request: Request,
        _: None = Depends(require_admin),
    ):
        """Change user password (admin only)"""
        if not USER_REPOSITORY_AVAILABLE or server._db_manager is None:
            raise HTTPException(
                status_code=503,
                detail="User management is not available (database not configured)",
            )

        try:
            from uuid import UUID as PyUUID

            uuid_obj = PyUUID(user_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid user ID format")

        try:
            session = await server._db_manager.get_session()
            try:
                success = await UserRepository.update_password(
                    session=session,
                    user_id=uuid_obj,
                    new_password=payload.new_password,
                    clear_reset_flag=True,
                )

                if not success:
                    raise HTTPException(status_code=404, detail="User not found")

                logger.info(f"Password changed for user ID: {user_id}")
                return JSONResponse(
                    {"success": True, "message": "Password changed successfully"}
                )
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to change password: {e}")
            raise HTTPException(status_code=500, detail="Failed to change password")
