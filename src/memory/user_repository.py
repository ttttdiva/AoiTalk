"""
Repository for User account management
"""

import bcrypt
import copy
import inspect
import logging
import os
import secrets
from datetime import datetime
from typing import Awaitable, Callable, List, Optional, Dict, Any
from uuid import UUID
from sqlalchemy import select, delete, update, and_, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from .models import App, Project, Space, User

logger = logging.getLogger(__name__)


class UserDeletionBlockedError(RuntimeError):
    """Raised when account deletion would violate ownership/lifecycle rules."""

    def __init__(self, message: str, blocking_relations: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.blocking_relations = list(blocking_relations or [])


class LastAdminError(RuntimeError):
    """Raised when an operation would leave no active administrator."""


class UserConflictError(ValueError):
    """Raised when a username/email uniqueness constraint is hit."""


class UserRepository:
    """Repository for managing user accounts"""

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using bcrypt

        Args:
            password: Plain text password

        Returns:
            str: Hashed password
        """
        salt = bcrypt.gensalt()
        return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        """Verify a password against its hash

        Args:
            password: Plain text password
            password_hash: Stored password hash

        Returns:
            bool: True if password matches
        """
        try:
            return bcrypt.checkpw(
                password.encode('utf-8'),
                password_hash.encode('utf-8')
            )
        except Exception:
            return False

    @staticmethod
    async def create_user(
        session: AsyncSession,
        username: str,
        password: str,
        email: Optional[str] = None,
        display_name: Optional[str] = None,
        role: str = 'user',
        is_password_reset_required: bool = True,
        *,
        commit: bool = True,
        is_active: bool = True,
    ) -> User:
        """Create a new user

        Args:
            session: Database session
            username: Unique username
            password: Plain text password (will be hashed)
            email: Optional email address
            display_name: Optional display name
            role: User role ('admin' or 'user')
            is_password_reset_required: Force password change on first login
            is_active: Whether the account is active at creation time

        Returns:
            User: Created user

        Raises:
            ValueError: If username already exists
        """
        if not isinstance(username, str) or not username.strip() or len(username.strip()) > 100:
            raise ValueError("Username must be 1-100 characters")
        if not isinstance(password, str) or len(password) < 6 or len(password) > 1024:
            raise ValueError("Password must be 6-1024 characters")
        if not isinstance(role, str) or role not in {"admin", "user"}:
            raise ValueError("Role must be 'admin' or 'user'")
        if type(is_active) is not bool:
            raise ValueError("is_active must be a boolean")
        if type(is_password_reset_required) is not bool:
            raise ValueError("is_password_reset_required must be a boolean")
        if email is not None and (not isinstance(email, str) or len(email) > 255):
            raise ValueError("Email must be at most 255 characters")
        if display_name is not None and (
            not isinstance(display_name, str) or len(display_name) > 100
        ):
            raise ValueError("Display name must be at most 100 characters")
        username = username.strip()
        email = email.strip() or None if isinstance(email, str) else email
        display_name = (
            display_name.strip() or None
            if isinstance(display_name, str)
            else display_name
        )
        # Check if username already exists
        existing = await UserRepository.get_by_username(session, username)
        if existing:
            raise UserConflictError(f"Username '{username}' already exists")

        # Check if email already exists
        if email:
            existing_email = await UserRepository.get_by_email(session, email)
            if existing_email:
                raise UserConflictError(f"Email '{email}' already exists")

        user = User(
            username=username,
            password_hash=UserRepository.hash_password(password),
            email=email,
            display_name=display_name or username,
            role=role,
            is_active=is_active,
            is_password_reset_required=is_password_reset_required
        )

        session.add(user)
        try:
            if commit:
                await session.commit()
            else:
                await session.flush()
        except IntegrityError as exc:
            # The preflight lookup is useful for friendly validation, but it
            # cannot close the race between two concurrent creates.  Let the
            # database unique index arbitrate that race and expose the same
            # canonical conflict that the lookup path uses instead of a 500.
            rollback = getattr(session, "rollback", None)
            if callable(rollback):
                await rollback()
            detail = str(exc).lower()
            if "unique" in detail or "duplicate" in detail:
                raise UserConflictError(
                    "Username or email already exists"
                ) from exc
            raise
        await session.refresh(user)

        return user

    @staticmethod
    async def get_by_id(session: AsyncSession, user_id: UUID) -> Optional[User]:
        """Get user by ID

        Args:
            session: Database session
            user_id: User UUID

        Returns:
            User or None
        """
        query = select(User).where(User.id == user_id)
        result = await session.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_username(session: AsyncSession, username: str) -> Optional[User]:
        """Get user by username

        Args:
            session: Database session
            username: Username to search

        Returns:
            User or None
        """
        query = select(User).where(User.username == username)
        result = await session.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_id_locked(
        session: AsyncSession, user_id: UUID
    ) -> Optional[User]:
        """Load one user while holding its row lock for the current transaction."""
        query = select(User).where(User.id == user_id).with_for_update().limit(1)
        execute = getattr(session, "execute", None)
        if not callable(execute):
            scalar = getattr(session, "scalar", None)
            if callable(scalar):
                return await scalar(query)
            return None
        bind = getattr(session, "bind", None)
        dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
        if dialect_name == "sqlite":
            # SQLite ignores SELECT ... FOR UPDATE.  Upgrade the transaction
            # before reading the row so two settings patches cannot both merge
            # from the same stale JSON snapshot.
            await execute(
                update(User)
                .where(User.id == user_id)
                .values(updated_at=User.updated_at)
            )
        result = await execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def lock_active_admins(session: AsyncSession) -> None:
        """Serialize all mutations that can remove an active administrator.

        Locking the complete active-admin set (rather than only the target row)
        makes the count-and-update decision safe when two different admins are
        demoted, disabled, deleted, or restored concurrently.  SQLite ignores
        ``FOR UPDATE`` but still receives the same transaction boundary.
        """
        execute = getattr(session, "execute", None)
        if not callable(execute):
            return
        bind = getattr(session, "bind", None)
        dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
        if dialect_name == "sqlite":
            # SQLite parses FOR UPDATE but does not enforce row locks.  A
            # no-op UPDATE upgrades the transaction to a RESERVED write lock
            # before the count/target decision, so concurrent demotions cannot
            # both observe the same last-admin snapshot.
            await execute(
                update(User)
                .where(and_(User.role == "admin", User.is_active.is_(True)))
                .values(updated_at=User.updated_at)
            )
            return
        result = await execute(
            select(User.id)
            .where(and_(User.role == "admin", User.is_active.is_(True)))
            .order_by(User.id)
            .with_for_update()
        )
        # Consume the result so drivers that defer row-lock acquisition until
        # iteration still acquire every lock before the count is evaluated.
        consume = getattr(result, "all", None)
        if callable(consume):
            consume()

    @staticmethod
    def merge_user_settings(
        current: Optional[Dict[str, Any]], patch: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Return a detached recursive field-level settings merge."""
        if not isinstance(patch, dict):
            raise ValueError("settings patch must be an object")
        merged: Dict[str, Any] = copy.deepcopy(current) if isinstance(current, dict) else {}
        for key, value in patch.items():
            existing = merged.get(key)
            if isinstance(existing, dict) and isinstance(value, dict):
                merged[key] = UserRepository.merge_user_settings(existing, value)
            else:
                merged[key] = copy.deepcopy(value)
        return merged

    @staticmethod
    async def patch_user_settings(
        session: AsyncSession,
        user_id: UUID,
        patch: Dict[str, Any],
        *,
        commit: bool = True,
        transform: Optional[Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]] = None,
    ) -> Optional[User]:
        """Merge settings after locking the current row.

        Callers must not read/merge a detached snapshot before invoking this
        method: doing so reintroduces the lost-update window this helper closes.
        """
        if not isinstance(patch, dict):
            raise ValueError("settings patch must be an object")
        user = await UserRepository.get_by_id_locked(session, user_id)
        if not user:
            return None
        merged = UserRepository.merge_user_settings(user.user_settings, patch)
        if transform is not None:
            transformed = transform(merged)
            merged = (
                await transformed
                if inspect.isawaitable(transformed)
                else transformed
            )
            if not isinstance(merged, dict):
                raise ValueError("settings transform must return an object")
        user.user_settings = merged
        user.updated_at = datetime.utcnow()
        if commit:
            await session.commit()
        else:
            await session.flush()
        await session.refresh(user)
        return user

    @staticmethod
    async def invalidate_sessions_by_username(
        session: AsyncSession, username: str
    ) -> Optional[User]:
        """Revoke all issued sessions/tokens for one account."""
        result = await session.execute(
            update(User)
            .where(User.username == username)
            .values(
                session_version=func.coalesce(User.session_version, 1) + 1,
                updated_at=datetime.utcnow(),
            )
            .returning(User.id)
        )
        user_id = result.scalar_one_or_none()
        if user_id is None:
            return None
        await session.commit()
        return await UserRepository.get_by_id(session, user_id)

    @staticmethod
    async def get_by_email(session: AsyncSession, email: str) -> Optional[User]:
        """Get user by email

        Args:
            session: Database session
            email: Email to search

        Returns:
            User or None
        """
        query = select(User).where(User.email == email)
        result = await session.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def authenticate(
        session: AsyncSession,
        username: str,
        password: str,
        *,
        commit: bool = True,
    ) -> Optional[User]:
        """Authenticate user with username and password

        Args:
            session: Database session
            username: Username
            password: Plain text password

        Returns:
            User if authentication successful, None otherwise
        """
        user = await UserRepository.get_by_username(session, username)

        if not user:
            return None

        if not user.is_active:
            return None

        if not UserRepository.verify_password(password, user.password_hash):
            return None

        # Update last login
        user.last_login = datetime.utcnow()
        if commit:
            await session.commit()
        else:
            await session.flush()

        return user

    @staticmethod
    async def update_password(
        session: AsyncSession,
        user_id: UUID,
        new_password: str,
        clear_reset_flag: bool = True,
        *,
        commit: bool = True,
    ) -> bool:
        """Update user password

        Args:
            session: Database session
            user_id: User UUID
            new_password: New plain text password
            clear_reset_flag: Clear is_password_reset_required flag

        Returns:
            bool: True if successful
        """
        if not isinstance(new_password, str) or not 6 <= len(new_password) <= 1024:
            raise ValueError("Password must be 6-1024 characters")
        values: Dict[str, Any] = {
            "password_hash": UserRepository.hash_password(new_password),
            "session_version": func.coalesce(User.session_version, 1) + 1,
            "updated_at": datetime.utcnow(),
        }
        if clear_reset_flag:
            values["is_password_reset_required"] = False

        result = await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(**values)
            .returning(User.id)
        )
        if result.scalar_one_or_none() is None:
            if commit:
                await session.rollback()
            return False
        if commit:
            await session.commit()
        else:
            await session.flush()
        return True

    @staticmethod
    async def update_user(
        session: AsyncSession,
        user_id: UUID,
        *,
        commit: bool = True,
        **kwargs
    ) -> Optional[User]:
        """Update user fields

        Args:
            session: Database session
            user_id: User UUID
            **kwargs: Fields to update (email, display_name, role, is_active,
                      preferred_character, user_settings)

        Returns:
            Updated User or None
        """
        allowed_fields = {
            "email",
            "display_name",
            "role",
            "is_active",
            "preferred_character",
            "user_settings",
            "is_password_reset_required",
        }
        unknown_fields = set(kwargs).difference(allowed_fields)
        if unknown_fields:
            raise ValueError(
                "Unsupported user fields: " + ", ".join(sorted(unknown_fields))
            )
        if "role" in kwargs and (
            not isinstance(kwargs["role"], str)
            or kwargs["role"] not in {"admin", "user"}
        ):
            raise ValueError("Role must be 'admin' or 'user'")
        if "is_active" in kwargs and type(kwargs["is_active"]) is not bool:
            raise ValueError("is_active must be a boolean")
        if (
            "is_password_reset_required" in kwargs
            and type(kwargs["is_password_reset_required"]) is not bool
        ):
            raise ValueError("is_password_reset_required must be a boolean")
        for field, maximum in (("email", 255), ("display_name", 100), ("preferred_character", 100)):
            if field in kwargs and kwargs[field] is not None and (
                not isinstance(kwargs[field], str) or len(kwargs[field]) > maximum
            ):
                raise ValueError(f"{field} must be at most {maximum} characters")
        if "user_settings" in kwargs and not isinstance(kwargs["user_settings"], dict):
            raise ValueError("user_settings must be an object")
        if "role" in kwargs or "is_active" in kwargs:
            await UserRepository.lock_active_admins(session)
        user = await session.scalar(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .limit(1)
        )
        if not user:
            return None

        # ``account_lifecycle`` is administrator-managed.  A caller may pass
        # a full settings document that preserves the current lifecycle (the
        # admin route performs that field merge under this same row lock), but
        # it must not change or introduce the lifecycle marker through the
        # generic update method.
        requested_settings = kwargs.get("user_settings")
        if isinstance(requested_settings, dict) and "account_lifecycle" in requested_settings:
            current_settings = user.user_settings if isinstance(user.user_settings, dict) else {}
            if requested_settings.get("account_lifecycle") != current_settings.get("account_lifecycle"):
                if commit:
                    await session.rollback()
                raise ValueError("account_lifecycle is managed by the administrator")

        original_role = user.role
        original_active = user.is_active
        if (
            original_role == "admin"
            and bool(original_active)
            and (
                kwargs.get("role", original_role) != "admin"
                or kwargs.get("is_active", original_active) is not True
            )
            and callable(getattr(session, "execute", None))
            and await UserRepository.count_admins(session) <= 1
        ):
            if commit:
                await session.rollback()
            raise LastAdminError("最後の管理者は変更できません")

        # Keep the lifecycle marker in sync with direct FastAPI/CSV updates.
        # A deleted account remains marked ``deleted`` while inactive; only an
        # explicit reactivation changes it back to ``active``.
        if "is_active" in kwargs:
            settings = (
                UserRepository.merge_user_settings(user.user_settings, kwargs["user_settings"])
                if isinstance(kwargs.get("user_settings"), dict)
                else UserRepository.merge_user_settings(user.user_settings, {})
            )
            lifecycle = settings.get("account_lifecycle")
            lifecycle_state = lifecycle.get("state") if isinstance(lifecycle, dict) else None
            if kwargs["is_active"] is True or lifecycle_state != "deleted":
                settings["account_lifecycle"] = {
                    "state": "active" if kwargs["is_active"] else "inactive",
                    "updated_at": datetime.utcnow().isoformat() + "Z",
                }
            kwargs["user_settings"] = settings

        auth_state_changed = False
        for key, value in kwargs.items():
            if key in allowed_fields:
                if key in {"role", "is_active", "is_password_reset_required"}:
                    if getattr(user, key) != value:
                        auth_state_changed = True
                setattr(user, key, value)

        if auth_state_changed:
            user.session_version = (user.session_version or 1) + 1
        user.updated_at = datetime.utcnow()
        if commit:
            await session.commit()
        else:
            await session.flush()
        await session.refresh(user)

        return user

    @staticmethod
    async def request_password_reset(
        session: AsyncSession,
        user_id: UUID,
        *,
        commit: bool = True,
    ) -> Optional[User]:
        """Atomically require a password reset and invalidate old sessions.

        The row lock makes issuing a reset link serialize with password/login
        state changes.  ``session_version`` is incremented even when the reset
        flag was already set so every newly issued link has a fresh token
        version and all previously issued links/sessions are invalidated.
        """
        user = await session.scalar(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .limit(1)
        )
        if not user:
            return None
        if not bool(user.is_active):
            raise ValueError(
                "無効または削除済みのユーザーには再設定リンクを発行できません"
            )
        user.is_password_reset_required = True
        user.session_version = (user.session_version or 1) + 1
        user.updated_at = datetime.utcnow()
        if commit:
            await session.commit()
        else:
            await session.flush()
        await session.refresh(user)
        return user

    @staticmethod
    async def complete_password_reset(
        session: AsyncSession,
        user_id: UUID,
        session_version: int,
        new_password: str,
        *,
        commit: bool = True,
    ) -> Optional[User]:
        """Consume a reset token under the user row lock."""
        if not isinstance(new_password, str) or not 6 <= len(new_password) <= 1024:
            raise ValueError("Password must be 6-1024 characters")
        if not isinstance(session_version, int) or session_version < 1:
            raise ValueError("Invalid password reset session version")
        user = await session.scalar(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .limit(1)
        )
        if (
            not user
            or not bool(user.is_active)
            or not bool(user.is_password_reset_required)
            or int(user.session_version or 1) != session_version
        ):
            return None
        user.password_hash = UserRepository.hash_password(new_password)
        user.is_password_reset_required = False
        user.session_version = session_version + 1
        user.updated_at = datetime.utcnow()
        if commit:
            await session.commit()
        else:
            await session.flush()
        await session.refresh(user)
        return user

    @staticmethod
    async def soft_delete_user(
        session: AsyncSession,
        user_id: UUID,
        *,
        deleted_by: UUID | str | None = None,
        commit: bool = True,
    ) -> Optional[User]:
        """Mark an account deleted without removing its durable history.

        The active-admin set is locked before the target row so this operation
        shares the same serialization boundary as admin role/status updates.
        """
        await UserRepository.lock_active_admins(session)
        user = await UserRepository.get_by_id_locked(session, user_id)
        if not user:
            return None

        if getattr(user, "role", None) == "admin" and bool(getattr(user, "is_active", False)):
            if callable(getattr(session, "execute", None)) and await UserRepository.count_admins(session) <= 1:
                if commit:
                    await session.rollback()
                raise LastAdminError("最後の管理者は削除できません")

        settings = UserRepository.merge_user_settings(getattr(user, "user_settings", None), {})
        previous_lifecycle = settings.get("account_lifecycle")
        settings["account_lifecycle"] = {
            "state": "deleted",
            "deleted_at": datetime.utcnow().isoformat() + "Z",
            "deleted_by": str(deleted_by) if deleted_by is not None else None,
        }
        user.user_settings = settings
        changed = (
            bool(getattr(user, "is_active", False))
            or not bool(getattr(user, "is_password_reset_required", False))
            or not isinstance(previous_lifecycle, dict)
            or previous_lifecycle.get("state") != "deleted"
        )
        user.is_active = False
        user.is_password_reset_required = True
        if changed:
            user.session_version = (getattr(user, "session_version", None) or 1) + 1
        user.updated_at = datetime.utcnow()
        if commit:
            await session.commit()
        else:
            await session.flush()
        await session.refresh(user)
        return user

    @staticmethod
    async def delete_user(
        session: AsyncSession,
        user_id: UUID,
        *,
        workspace_root: str | os.PathLike[str] | None = None,
        commit: bool = True,
        require_deleted: bool = False,
    ) -> bool:
        """Delete a user

        Args:
            session: Database session
            user_id: User UUID

        Returns:
            bool: True if deleted
        """
        # Lock the owner row before taking the ownership snapshot.  This gives
        # account deletion a stable ownership boundary while concurrent App
        # operations are deciding whether the user is still active.
        await UserRepository.lock_active_admins(session)
        user = await session.scalar(
            select(User).where(User.id == user_id).with_for_update().limit(1)
        )
        if not user:
            return False

        if (
            callable(getattr(session, "execute", None))
            and getattr(user, "role", None) == "admin"
            and bool(getattr(user, "is_active", False))
            and await UserRepository.count_admins(session) <= 1
        ):
            if commit:
                await session.rollback()
            raise LastAdminError("最後の管理者は削除できません")

        if require_deleted:
            settings = user.user_settings if isinstance(user.user_settings, dict) else {}
            lifecycle = settings.get("account_lifecycle")
            if (
                not isinstance(lifecycle, dict)
                or lifecycle.get("state") != "deleted"
                or bool(user.is_active)
            ):
                raise ValueError("完全削除できるのは削除済みユーザーだけです")

        owned_project = await session.scalar(
            select(Project.id).where(Project.owner_id == user_id).limit(1)
        )
        owned_space = await session.scalar(
            select(Space.id).where(Space.owner_id == user_id).limit(1)
        )
        if owned_project is not None or owned_space is not None:
            blocking = []
            if owned_project is not None:
                blocking.append({"label": "Project", "count": 1})
            if owned_space is not None:
                blocking.append({"label": "Space", "count": 1})
            raise UserDeletionBlockedError(
                "所有ProjectまたはSpaceを先に移管・削除してください",
                blocking,
            )

        # 所有 App が1件でも残っていればアカウント削除は拒否する。App は DB 行
        # だけでなく workspace / artifacts / Project instance / 実行中 Job を
        # 伴うため、退避も停止もせずに FK cascade で消すと復旧できない。
        # Project / Space と同じく「先に移管・archive・明示削除」を必須にする。
        owned_app = await session.scalar(
            select(App.id).where(App.owner_user_id == user_id).limit(1)
        )
        if owned_app is not None:
            raise UserDeletionBlockedError(
                "所有Appを先にarchiveまたは明示削除してください",
                [{"label": "App", "count": 1}],
            )
        await session.delete(user)
        if commit:
            await session.commit()
        else:
            await session.flush()

        # DB の削除が確定してから個人 workspace を消す。ファイルシステム側の
        # 失敗でアカウント削除を巻き戻すことはできないため、失敗は監査 GC で
        # 再試行できるよう記録する。
        if commit:
            try:
                from ..services.workspace_gc import remove_user_workspace

                remove_user_workspace(user_id, workspace_root=workspace_root)
            except Exception:
                logger.exception("User workspace cleanup failed after user deletion: %s", user_id)
        return True

    @staticmethod
    async def list_users(
        session: AsyncSession,
        limit: int = 100,
        offset: int = 0,
        include_inactive: bool = False,
        role: Optional[str] = None
    ) -> tuple[List[User], int]:
        """List users with pagination

        Args:
            session: Database session
            limit: Maximum users to return
            offset: Number of users to skip
            include_inactive: Include inactive users
            role: Filter by role

        Returns:
            tuple: (list of users, total count)
        """
        conditions = []

        if not include_inactive:
            conditions.append(User.is_active == True)

        if role:
            conditions.append(User.role == role)

        # Get total count
        count_query = select(User)
        if conditions:
            count_query = count_query.where(and_(*conditions))
        count_result = await session.execute(count_query)
        total_count = len(count_result.scalars().all())

        # Get paginated results
        query = select(User)
        if conditions:
            query = query.where(and_(*conditions))
        query = query.order_by(User.created_at.desc())
        query = query.limit(limit).offset(offset)

        result = await session.execute(query)
        users = result.scalars().all()

        return users, total_count

    @staticmethod
    async def count_admins(session: AsyncSession) -> int:
        """Count active admin users

        Args:
            session: Database session

        Returns:
            int: Number of active admins
        """
        query = select(User).where(
            and_(User.role == 'admin', User.is_active == True)
        )
        result = await session.execute(query)
        return len(result.scalars().all())

    @staticmethod
    async def ensure_admin_exists(
        session: AsyncSession,
        default_username: str = 'admin',
        default_password: Optional[str] = None
    ) -> bool:
        """Ensure at least one admin user exists

        Creates a default admin if no admin exists.

        Args:
            session: Database session
            default_username: Default admin username
            default_password: Default admin password

        Returns:
            bool: True if admin was created, False if admin already existed
        """
        admin_count = await UserRepository.count_admins(session)

        if admin_count > 0:
            # Admin already exists
            return False

        # Never ship a fixed bootstrap credential. Operators may provide one
        # through the untracked .env; otherwise emit a one-time random value
        # and force a password change on first login.
        bootstrap_username = os.getenv(
            "AOITALK_BOOTSTRAP_ADMIN_USERNAME", default_username
        ).strip() or default_username
        bootstrap_password = default_password or os.getenv(
            "AOITALK_BOOTSTRAP_ADMIN_PASSWORD"
        )
        from ..features import Features
        enterprise = Features.is_enterprise()
        if enterprise and not bootstrap_password:
            raise RuntimeError(
                "AOITALK_BOOTSTRAP_ADMIN_PASSWORD is required before creating the Enterprise admin"
            )
        generated = not bootstrap_password
        bootstrap_password = bootstrap_password or secrets.token_urlsafe(18)
        await UserRepository.create_user(
            session=session,
            username=bootstrap_username,
            password=bootstrap_password,
            role='admin',
            display_name='Administrator',
            is_password_reset_required=True  # Force password change
        )
        if generated:
            logger.warning(
                "初期管理者をランダム生成しました。パスワードはログへ出力しません。"
                "AOITALK_BOOTSTRAP_ADMIN_PASSWORDを設定して再初期化してください。"
            )
        return True
