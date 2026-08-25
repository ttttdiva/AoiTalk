"""ユーザー管理 (管理者専用 CRUD / CSV import・export) 系ルート (server.py から移設)"""

import inspect
import logging
import os
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from .payloads import ChangePasswordPayload, CreateUserPayload, UpdateUserPayload

# Import user repository (server.py と同じフォールバック付き)
try:
    from ...memory.user_repository import (
        LastAdminError,
        UserDeletionBlockedError,
        UserConflictError,
        UserRepository,
    )

    USER_REPOSITORY_AVAILABLE = True
except ImportError:
    UserRepository = None
    LastAdminError = RuntimeError
    UserDeletionBlockedError = RuntimeError
    UserConflictError = ValueError
    USER_REPOSITORY_AVAILABLE = False

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)

MAX_USER_CSV_BYTES = 2 * 1024 * 1024
MAX_USER_CSV_ROWS = 10_000


def _parse_csv_bool(value: object, *, default: bool | None = None) -> bool:
    """Parse the small, explicit CSV boolean vocabulary.

    ``bool('false')`` is intentionally never used: unknown values are rejected
    instead of silently changing account state.
    """
    if not isinstance(value, str):
        raise ValueError("is_activeはtrue/falseで指定してください")
    normalized = value.strip().lower()
    if not normalized and default is not None:
        return default
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError("is_activeはtrue/falseで指定してください")


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

        # Resolve the canonical principal once for lifecycle self-target guards.
        # The admin check above intentionally accepts the existing cookie auth
        # path, while the resolver also understands Next/Bearer credentials.
        # Some test doubles expose only the historical one-argument resolver,
        # hence the narrow TypeError fallback.
        resolver = getattr(server, "_get_user_info_from_request", None)
        state = getattr(request, "state", None)
        if callable(resolver) and not getattr(state, "user_id", None):
            try:
                try:
                    candidate = resolver(request, allow_password_reset=True)
                except TypeError:
                    candidate = resolver(request)
                info = await candidate if inspect.isawaitable(candidate) else candidate
            except Exception:
                info = None
            if isinstance(info, dict) and info.get("id"):
                if state is not None:
                    state.user_id = str(info["id"])

    async def request_actor_id(request: Request) -> str | None:
        """Resolve the acting principal for self-target guards.

        The Next BFF supplies ``x-forwarded-user-id``; direct FastAPI cookie
        clients do not, so fall back to the server's canonical resolver.
        """
        state_actor_id = getattr(getattr(request, "state", None), "user_id", None)
        if state_actor_id:
            return str(state_actor_id)
        internal_key = request.headers.get("x-internal-auth")
        configured_key = os.environ.get("INTERNAL_API_KEY", "")
        if internal_key and configured_key and internal_key == configured_key:
            actor_id = request.headers.get("x-forwarded-user-id")
            if actor_id:
                return actor_id
        resolver = getattr(server, "_get_user_info_from_request", None)
        if callable(resolver):
            try:
                try:
                    candidate = resolver(request, allow_password_reset=True)
                except TypeError:
                    candidate = resolver(request)
                info = await candidate if inspect.isawaitable(candidate) else candidate
            except Exception:
                return None
            if isinstance(info, dict) and info.get("id"):
                return str(info["id"])
        return None

    def is_same_actor(actor_id: str | None, target_id: object, raw_target: str) -> bool:
        """Compare UUID principals canonically, while tolerating test doubles."""
        if not actor_id:
            return False
        try:
            from uuid import UUID as PyUUID

            return PyUUID(str(actor_id)) == target_id
        except (TypeError, ValueError, AttributeError):
            return str(actor_id) == raw_target

    @app.get("/api/users")
    async def list_users(
        request: Request,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
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
                    limit=limit,
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
                    is_password_reset_required=payload.require_password_change,
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
        except UserConflictError as e:
            raise HTTPException(status_code=409, detail=str(e))
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

        if not file.filename or not file.filename.lower().endswith(".csv"):
            raise HTTPException(
                status_code=400, detail="CSVファイルをアップロードしてください"
            )

        try:
            import csv
            import io

            # Read file content
            content = await file.read(MAX_USER_CSV_BYTES + 1)
            if len(content) > MAX_USER_CSV_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="CSVファイルは2MB以下にしてください",
                )
            try:
                text_content = content.decode("utf-8")
            except UnicodeDecodeError:
                # Try Shift-JIS for Japanese Excel files
                try:
                    text_content = content.decode("shift-jis")
                except UnicodeDecodeError as exc:
                    raise HTTPException(
                        status_code=400, detail="CSVの文字コードが不正です"
                    ) from exc

            # Remove BOM if present
            if text_content.startswith("\ufeff"):
                text_content = text_content[1:]

            reader = csv.DictReader(io.StringIO(text_content), strict=True)

            # Validate headers
            required_headers = {"username"}
            try:
                fieldnames = reader.fieldnames or []
            except csv.Error as exc:
                raise HTTPException(status_code=400, detail="CSV形式が不正です") from exc
            if any(
                not isinstance(name, str) or not name.strip()
                for name in fieldnames
            ):
                raise HTTPException(status_code=400, detail="CSVのヘッダーが不正です")
            if not required_headers.issubset(set(fieldnames)):
                raise HTTPException(
                    status_code=400, detail="CSVにusernameカラムが必要です"
                )
            if len(fieldnames) != len(set(fieldnames)):
                raise HTTPException(status_code=400, detail="CSVのヘッダーが重複しています")
            allowed_headers = {
                "username",
                "password",
                "email",
                "display_name",
                "role",
                "is_active",
            }
            unknown_headers = set(fieldnames) - allowed_headers
            if unknown_headers:
                raise HTTPException(
                    status_code=400,
                    detail=f"CSVに不明なカラムがあります: {', '.join(sorted(unknown_headers))}",
                )

            rows = []
            try:
                for row in reader:
                    if len(rows) >= MAX_USER_CSV_ROWS:
                        raise HTTPException(
                            status_code=413,
                            detail=f"CSVは{MAX_USER_CSV_ROWS}行以下にしてください",
                        )
                    if None in row:
                        raise HTTPException(
                            status_code=400,
                            detail="CSVの列数がヘッダーと一致しません",
                        )
                    rows.append(row)
            except csv.Error as exc:
                raise HTTPException(status_code=400, detail="CSV形式が不正です") from exc

            results = {"created": 0, "updated": 0, "skipped": 0, "errors": []}

            session = await server._db_manager.get_session()
            try:
                for row_num, row in enumerate(rows, start=2):
                    username = (row.get("username") or "").strip()
                    if not username:
                        results["skipped"] += 1
                        continue
                    if len(username) > 100:
                        results["errors"].append(f"行{row_num}: usernameが長すぎます")
                        results["skipped"] += 1
                        continue

                    password = (row.get("password") or "").strip()
                    email = (row.get("email") or "").strip() or None
                    display_name = (row.get("display_name") or "").strip() or None
                    if email is not None and len(email) > 255:
                        results["errors"].append(f"行{row_num}: emailが長すぎます")
                        results["skipped"] += 1
                        continue
                    if display_name is not None and len(display_name) > 100:
                        results["errors"].append(f"行{row_num}: display_nameが長すぎます")
                        results["skipped"] += 1
                        continue
                    raw_role = row.get("role")
                    role = "user" if raw_role is None else raw_role.strip().lower()
                    if password and not 6 <= len(password) <= 1024:
                        results["errors"].append(
                            f"行{row_num}: {username} - passwordは6-1024文字で指定してください"
                        )
                        results["skipped"] += 1
                        continue
                    try:
                        if role not in ("admin", "user"):
                            raise ValueError("roleはadminまたはuserで指定してください")
                        raw_is_active = row.get("is_active")
                        is_active = (
                            True
                            if raw_is_active is None
                            else _parse_csv_bool(raw_is_active)
                        )
                    except ValueError as exc:
                        results["errors"].append(f"行{row_num}: {username} - {exc}")
                        results["skipped"] += 1
                        continue

                    if not password and not username:
                        results["skipped"] += 1
                        continue

                    try:
                        result_kind = None
                        # One transaction per row keeps attribute and password
                        # changes atomic while allowing the next row to proceed
                        # after this context rolls a failed row back.
                        async with session.begin():
                            lock_admins = getattr(
                                UserRepository, "lock_active_admins", None
                            )
                            if callable(lock_admins):
                                await lock_admins(session)
                            existing_user = await UserRepository.get_by_username(
                                session, username
                            )

                            if existing_user:
                                if (
                                    getattr(existing_user, "role", None) == "admin"
                                    and bool(getattr(existing_user, "is_active", False))
                                    and (role != "admin" or not is_active)
                                ):
                                    count_admins = getattr(
                                        UserRepository, "count_admins", None
                                    )
                                    if callable(count_admins) and await count_admins(session) <= 1:
                                        raise LastAdminError(
                                            "最後の管理者は変更できません"
                                        )
                                update_data = {
                                    "email": email,
                                    "display_name": display_name,
                                    "role": role,
                                    "is_active": is_active,
                                }
                                updated_user = await UserRepository.update_user(
                                    session=session,
                                    user_id=existing_user.id,
                                    commit=False,
                                    **update_data,
                                )
                                if updated_user is None:
                                    raise ValueError("ユーザー更新に失敗しました")

                                if password:
                                    password_updated = (
                                        await UserRepository.update_password(
                                            session=session,
                                            user_id=existing_user.id,
                                            new_password=password,
                                            clear_reset_flag=False,
                                            commit=False,
                                        )
                                    )
                                    if not password_updated:
                                        raise ValueError(
                                            "パスワード更新に失敗しました"
                                        )

                                result_kind = "updated"
                            else:
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
                                    is_active=is_active,
                                    commit=False,
                                )
                                result_kind = "created"

                        if result_kind == "updated":
                            results["updated"] += 1
                        elif result_kind == "created":
                            results["created"] += 1
                    except Exception as e:
                        results["errors"].append(
                            f"行{row_num}: {username} - {str(e)}"
                        )
                        results["skipped"] += 1

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

        # Keep explicit nulls (email/display_name can be cleared), while
        # ignoring omitted optional fields.
        update_data = payload.model_dump(exclude_unset=True)

        if not update_data:
            raise HTTPException(status_code=400, detail="No update data provided")
        for required_value in ("role", "is_active", "is_password_reset_required"):
            if required_value in update_data and update_data[required_value] is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"{required_value} must not be null",
                )
        if "user_settings" in update_data and update_data["user_settings"] is None:
            raise HTTPException(status_code=422, detail="user_settings must be an object")
        if (
            isinstance(update_data.get("user_settings"), dict)
            and "account_lifecycle" in update_data["user_settings"]
        ):
            raise HTTPException(
                status_code=400,
                detail="account_lifecycle is managed by the administrator",
            )
        if update_data.get("is_active") is False:
            actor_id = await request_actor_id(request)
            if is_same_actor(actor_id, uuid_obj, user_id):
                raise HTTPException(status_code=400, detail="自分自身は無効化できません")

        try:
            session = await server._db_manager.get_session()
            try:
                async with session.begin():
                    lock_admins = getattr(UserRepository, "lock_active_admins", None)
                    if callable(lock_admins):
                        await lock_admins(session)
                    get_locked = getattr(UserRepository, "get_by_id_locked", None)
                    if not callable(get_locked):
                        get_locked = getattr(UserRepository, "get_by_id")
                    existing = await get_locked(session, uuid_obj)
                    if not existing:
                        raise HTTPException(status_code=404, detail="User not found")

                    if "user_settings" in update_data:
                        update_data["user_settings"] = UserRepository.merge_user_settings(
                            existing.user_settings, update_data["user_settings"]
                        )

                    next_role = update_data.get("role", existing.role)
                    next_active = update_data.get("is_active", existing.is_active)
                    if (
                        existing.role == "admin"
                        and bool(existing.is_active)
                        and (next_role != "admin" or next_active is not True)
                    ):
                        count_admins = getattr(UserRepository, "count_admins", None)
                        if callable(count_admins) and await count_admins(session) <= 1:
                            raise LastAdminError("最後の管理者は変更できません")

                    user = await UserRepository.update_user(
                        session=session, user_id=uuid_obj, commit=False, **update_data
                    )

                    if not user:
                        raise HTTPException(status_code=404, detail="User not found")

                    logger.info(f"User updated: {user.username}")
                    response = JSONResponse(
                        {
                            "success": True,
                            "user": user.to_dict(),
                            "message": f"User '{user.username}' updated successfully",
                        }
                    )
                return response
            finally:
                await session.close()
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except LastAdminError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Failed to update user: {e}")
            raise HTTPException(status_code=500, detail="Failed to update user")

    @app.post("/api/users/{user_id}/password-reset-link")
    async def issue_password_reset_link(
        user_id: str,
        request: Request,
        _: None = Depends(require_admin),
    ):
        """Atomically issue a reset-link session version for an active user.

        The Next BFF turns the canonical version into its signed browser reset
        token; this service owns the lock, reset flag and session invalidation.
        """
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
                async with session.begin():
                    request_reset = getattr(
                        UserRepository, "request_password_reset", None
                    )
                    if not callable(request_reset):
                        raise HTTPException(
                            status_code=503,
                            detail="Password reset management is unavailable",
                        )
                    user = await request_reset(
                        session,
                        uuid_obj,
                        commit=False,
                    )
                    if not user:
                        raise HTTPException(status_code=404, detail="User not found")
                    serialized = user.to_dict()
                return JSONResponse(
                    {
                        "success": True,
                        "user": serialized,
                        "session_version": int(user.session_version or 1),
                    }
                )
            finally:
                await session.close()
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error(f"Failed to issue password reset link: {exc}")
            raise HTTPException(
                status_code=500,
                detail="Failed to issue password reset link",
            ) from exc

    @app.delete("/api/users/{user_id}")
    async def delete_user(
        user_id: str, request: Request, _: None = Depends(require_admin)
    ):
        """Soft-delete a user; durable history remains until explicit purge."""
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
                actor_id = await request_actor_id(request)
                if is_same_actor(actor_id, uuid_obj, user_id):
                    raise HTTPException(status_code=400, detail="自分自身は削除できません")
                async with session.begin():
                    soft_delete = getattr(UserRepository, "soft_delete_user", None)
                    if not callable(soft_delete):
                        raise HTTPException(status_code=503, detail="User lifecycle management is unavailable")
                    user = await soft_delete(
                        session,
                        uuid_obj,
                        deleted_by=actor_id,
                        commit=False,
                    )
                    if not user:
                        raise HTTPException(status_code=404, detail="User not found")

                    username = user.username
                    serialized_user = user.to_dict()
                if not user:
                    raise HTTPException(status_code=404, detail="User not found")

                logger.info(f"User deleted: {username}")
                return JSONResponse(
                    {
                        "success": True,
                        "user": serialized_user,
                        "message": f"User '{username}' deleted successfully",
                    }
                )
            finally:
                await session.close()
        except UserDeletionBlockedError as e:
            return JSONResponse(
                {
                    "detail": str(e),
                    "blocking_relations": getattr(e, "blocking_relations", []),
                },
                status_code=409,
            )
        except LastAdminError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to delete user: {e}")
            raise HTTPException(status_code=500, detail="Failed to delete user")

    @app.delete("/api/users/{user_id}/purge")
    async def purge_user(
        user_id: str, request: Request, _: None = Depends(require_admin)
    ):
        """Physically remove only an already soft-deleted account."""
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

        actor_id = await request_actor_id(request)
        if is_same_actor(actor_id, uuid_obj, user_id):
            raise HTTPException(status_code=400, detail="自分自身は完全削除できません")

        try:
            session = await server._db_manager.get_session()
            try:
                lock_admins = getattr(UserRepository, "lock_active_admins", None)
                if callable(lock_admins):
                    await lock_admins(session)
                get_locked = getattr(UserRepository, "get_by_id_locked", None)
                if not callable(get_locked):
                    get_locked = getattr(UserRepository, "get_by_id")
                target = await get_locked(session, uuid_obj)
                if not target:
                    raise HTTPException(status_code=404, detail="User not found")
                if target.role == "admin" and bool(target.is_active):
                    count_admins = getattr(UserRepository, "count_admins", None)
                    if callable(count_admins) and await count_admins(session) <= 1:
                        raise LastAdminError("最後の管理者は削除できません")
                deleted = await UserRepository.delete_user(
                    session,
                    uuid_obj,
                    workspace_root=server._resolve_workspace_root(),
                    require_deleted=True,
                )
                if not deleted:
                    raise HTTPException(status_code=404, detail="User not found")
                return JSONResponse(
                    {"success": True, "user_id": user_id}
                )
            finally:
                await session.close()
        except UserDeletionBlockedError as e:
            return JSONResponse(
                {
                    "detail": str(e),
                    "blocking_relations": getattr(e, "blocking_relations", []),
                },
                status_code=409,
            )
        except LastAdminError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Failed to purge user: {e}")
            raise HTTPException(status_code=500, detail="Failed to purge user")

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
