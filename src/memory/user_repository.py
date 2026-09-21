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

from .models import App, ExternalIdentityBinding, Project, Space, User

logger = logging.getLogger(__name__)


async def _ensure_personal_docs_for_user(
    session: AsyncSession,
    user_id: UUID | str | None,
) -> None:
    """Materialize the canonical Personal Docs Guide in the user transaction.

    Production sessions always use PostgreSQL and therefore must propagate a
    Guide lifecycle failure so account creation rolls back atomically. A few
    focused SQLite repository tests intentionally create only the ``users``
    table; skip the hook when the Docs schema is absent rather than changing
    that lightweight fixture contract.
    """

    if user_id is None or not isinstance(session, AsyncSession):
        return
    try:
        bind = session.get_bind()
        dialect = str(getattr(getattr(bind, "dialect", None), "name", ""))
    except Exception:
        dialect = ""
    if dialect == "sqlite":
        try:
            from sqlalchemy import inspect as sqlalchemy_inspect

            has_docs_library = await session.run_sync(
                lambda sync_session: sqlalchemy_inspect(sync_session).has_table(
                    "docs_libraries"
                )
            )
        except Exception:
            # Do not weaken the PostgreSQL production path. If a SQLite
            # fixture cannot be inspected, let the normal lifecycle call
            # surface the real schema error instead of silently succeeding.
            has_docs_library = True
        if not has_docs_library:
            return

    from ..services.docs_workspace import ensure_docs_library

    await ensure_docs_library(session, owner_user_id=UUID(str(user_id)))


class UserDeletionBlockedError(RuntimeError):
    """Raised when account deletion would violate ownership/lifecycle rules."""

    def __init__(self, message: str, blocking_relations: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.blocking_relations = list(blocking_relations or [])


class LastAdminError(RuntimeError):
    """Raised when an operation would leave no active administrator."""


class UserConflictError(ValueError):
    """Raised when a username/email uniqueness constraint is hit."""


class ExternalIdentityCredentialError(ValueError):
    """Raised when local credential management targets an external account."""


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
    def verify_password(password: str, password_hash: Optional[str]) -> bool:
        """Verify a password against its hash

        Args:
            password: Plain text password
            password_hash: Stored password hash

        Returns:
            bool: True if password matches
        """
        if not isinstance(password_hash, str) or not password_hash:
            # External (AD) users intentionally have no local hash.
            return False
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
            auth_source="local",
            email=email,
            display_name=display_name or username,
            role=role,
            is_active=is_active,
            is_password_reset_required=is_password_reset_required
        )

        session.add(user)
        try:
            # UUID defaults are assigned on flush. Ensure the Guide sees the
            # durable owner id and remains in the same transaction as the
            # account row; ``commit=False`` callers can include both in their
            # existing outer transaction.
            flush = getattr(session, "flush", None)
            if callable(flush):
                await flush()
                await _ensure_personal_docs_for_user(session, user.id)

            from ..services.verification_provenance import register_current_entity
            from ..verification.context import get_current_verification_context

            # Preserve the lightweight/mock session contract for ordinary
            # account creation.  A verification context requires a flush so
            # the generated UUID can be registered in the same transaction.
            if get_current_verification_context() is not None:
                if not callable(flush):
                    raise RuntimeError("verification user creation requires session.flush")
                tagged_settings = await register_current_entity(
                    session,
                    entity_type="user",
                    entity_id=user.id,
                    metadata=user.user_settings,
                )
                if tagged_settings is not None:
                    user.user_settings = tagged_settings
                if commit:
                    await session.commit()
            elif commit:
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
    def normalize_ad_external_id(value: UUID | str | bytes | bytearray) -> UUID:
        """Normalize an AD ``objectGUID`` to a UUID.

        LDAP commonly returns ``objectGUID`` as 16 little-endian bytes; the
        UUID text form uses RFC-4122 byte order.  Accept both forms here so
        every caller persists one canonical identity value.
        """

        if isinstance(value, UUID):
            return value
        if isinstance(value, (bytes, bytearray)):
            if len(value) != 16:
                raise ValueError("AD objectGUID must contain exactly 16 bytes")
            return UUID(bytes_le=bytes(value))
        try:
            return UUID(str(value).strip())
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("AD objectGUID must be a UUID") from exc

    @staticmethod
    def normalize_ad_authority(authority: str) -> str:
        """Return the canonical authority key used by identity bindings."""

        if not isinstance(authority, str):
            raise ValueError("AD authority must be a string")
        normalized = authority.strip().lower()
        if not normalized or len(normalized) > 255:
            raise ValueError("AD authority must be 1-255 characters")
        if any(
            ord(character) < 0x20
            or ord(character) == 0x7F
            or character.isspace()
            for character in normalized
        ):
            raise ValueError("AD authority contains invalid characters")
        return normalized

    @staticmethod
    async def _update_ad_user_profile(
        session: AsyncSession,
        user: User,
        *,
        username: str,
        email: Optional[str],
        display_name: Optional[str],
    ) -> User:
        """Apply safe presentation updates without changing identity source."""

        username = username.strip()
        if not username or len(username) > 100:
            raise ValueError("Username must be 1-100 characters")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in username):
            raise ValueError("Username contains invalid characters")
        if email is not None:
            email = email.strip() or None
            if len(email or "") > 255:
                raise ValueError("Email must be at most 255 characters")
            if email is not None and any(
                ord(character) < 0x20 or ord(character) == 0x7F for character in email
            ):
                raise ValueError("Email contains invalid characters")
        if display_name is not None:
            display_name = display_name.strip() or None
            if len(display_name or "") > 100:
                raise ValueError("Display name must be at most 100 characters")
            if display_name is not None and any(
                ord(character) < 0x20 or ord(character) == 0x7F
                for character in display_name
            ):
                raise ValueError("Display name contains invalid characters")

        if username != user.username:
            collision = await session.scalar(
                select(User).where(
                    and_(
                        func.lower(User.username) == username.lower(),
                        User.id != user.id,
                    )
                )
            )
            if collision is not None:
                # Never attach an AD identity to an unrelated local account
                # merely because the directory username matches it.
                raise UserConflictError(
                    f"Username '{username}' already belongs to another user"
                )
            user.username = username

        if email is not None and email != user.email:
            collision = await session.scalar(
                select(User).where(
                    and_(
                        func.lower(User.email) == email.lower(),
                        User.id != user.id,
                    )
                )
            )
            if collision is not None:
                raise UserConflictError(f"Email '{email}' already belongs to another user")
            user.email = email
        if display_name is not None:
            user.display_name = display_name

        # Keep the local shadow row in the AD invariant state.  These are
        # idempotent assignments for existing AD rows and are also enforced by
        # the database CHECK/trigger.
        user.auth_source = "ad"
        user.password_hash = None
        user.is_password_reset_required = False
        user.last_login = datetime.utcnow()
        user.updated_at = datetime.utcnow()
        return user

    @staticmethod
    async def provision_ad_user(
        session: AsyncSession,
        *,
        authority: str,
        external_id: UUID | str | bytes | bytearray,
        username: str,
        email: Optional[str] = None,
        display_name: Optional[str] = None,
        role: str = "user",
        is_active: bool = True,
        commit: bool = True,
    ) -> User:
        """Get or atomically create the local shadow for one AD identity.

        The only durable external identity is ``(source='ad', authority,
        objectGUID)``.  Existing local rows are never linked by username or
        email; a collision is surfaced as :class:`UserConflictError`.
        ``commit=False`` lets the login route include this mutation in its
        existing transaction (throttle/audit/session handling).
        """

        authority = UserRepository.normalize_ad_authority(authority)
        external_id = UserRepository.normalize_ad_external_id(external_id)
        # ``normalize_ad_external_id`` treats binary objectGUID values as AD's
        # little-endian representation.  Do not probe an RFC-4122/network-byte
        # alternate: two different GUIDs can be byte-swapped counterparts, and
        # an alternate lookup could therefore bind a valid directory identity
        # to another user's local shadow account.
        if not isinstance(username, str) or not username.strip() or len(username.strip()) > 100:
            raise ValueError("Username must be 1-100 characters")
        username = username.strip()
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in username):
            raise ValueError("Username contains invalid characters")
        if role not in {"admin", "user"}:
            raise ValueError("Role must be 'admin' or 'user'")
        if type(is_active) is not bool:
            raise ValueError("is_active must be a boolean")
        if email is not None and (
            not isinstance(email, str) or len(email.strip()) > 255
        ):
            raise ValueError("Email must be at most 255 characters")
        if email is not None and any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in email
        ):
            raise ValueError("Email contains invalid characters")
        if display_name is not None and (
            not isinstance(display_name, str) or len(display_name.strip()) > 100
        ):
            raise ValueError("Display name must be at most 100 characters")
        if display_name is not None and any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in display_name
        ):
            raise ValueError("Display name contains invalid characters")

        binding_query = (
            select(ExternalIdentityBinding)
            .where(
                and_(
                    ExternalIdentityBinding.source == "ad",
                    ExternalIdentityBinding.authority == authority,
                    ExternalIdentityBinding.external_id == external_id,
                )
            )
            .with_for_update()
            .limit(1)
        )
        binding = await session.scalar(binding_query)
        if binding is not None:
            user = await session.scalar(
                select(User)
                .where(User.id == binding.user_id)
                .with_for_update()
                .limit(1)
            )
            if user is None or getattr(user, "auth_source", "local") != "ad":
                raise UserConflictError("External identity binding points to an invalid user")
            user = await UserRepository._update_ad_user_profile(
                session,
                user,
                username=username,
                email=email,
                display_name=display_name,
            )
            await _ensure_personal_docs_for_user(session, user.id)
            if commit:
                await session.commit()
            else:
                await session.flush()
            await session.refresh(user)
            setattr(user, "_ad_created", False)
            return user

        # A same-name local account is intentionally a hard collision.  It is
        # never safe to infer identity from mutable username/email attributes.
        existing_username = await session.scalar(
            select(User)
            .where(func.lower(User.username) == username.lower())
            .limit(1)
        )
        if existing_username is not None:
            raise UserConflictError(
                f"Username '{username}' already belongs to another user"
            )

        user = User(
            username=username,
            email=email.strip() or None if isinstance(email, str) else email,
            display_name=(
                display_name.strip() or username
                if isinstance(display_name, str)
                else username
            ),
            password_hash=None,
            auth_source="ad",
            role=role,
            is_active=is_active,
            is_password_reset_required=False,
            last_login=datetime.utcnow(),
        )
        binding = ExternalIdentityBinding(
            user=user,
            source="ad",
            authority=authority,
            external_id=external_id,
        )

        # Use a SAVEPOINT where available so a concurrent unique-key winner
        # does not abort the caller's outer login/audit transaction.  The
        # second lookup then returns the one canonical row.
        begin_nested = getattr(session, "begin_nested", None)
        try:
            if callable(begin_nested):
                async with begin_nested():
                    session.add(user)
                    session.add(binding)
                    await session.flush()
            else:
                session.add(user)
                session.add(binding)
                await session.flush()
        except IntegrityError as exc:
            if not callable(begin_nested):
                rollback = getattr(session, "rollback", None)
                if callable(rollback):
                    await rollback()
            winner = await session.scalar(binding_query)
            if winner is not None:
                winner_user = await session.scalar(
                    select(User)
                    .where(User.id == winner.user_id)
                    .with_for_update()
                    .limit(1)
                )
                if winner_user is not None and getattr(winner_user, "auth_source", "local") == "ad":
                    winner_user = await UserRepository._update_ad_user_profile(
                        session,
                        winner_user,
                        username=username,
                        email=email,
                        display_name=display_name,
                    )
                    await _ensure_personal_docs_for_user(session, winner_user.id)
                    if commit:
                        await session.commit()
                    else:
                        await session.flush()
                    await session.refresh(winner_user)
                    setattr(winner_user, "_ad_created", False)
                    return winner_user
            raise UserConflictError(
                "AD identity or username already belongs to another user"
            ) from exc

        await session.flush()
        await _ensure_personal_docs_for_user(session, user.id)
        if commit:
            await session.commit()
        await session.refresh(user)
        setattr(user, "_ad_created", True)
        return user

    @staticmethod
    async def get_or_create_ad_user(
        session: AsyncSession,
        *,
        authority: str,
        external_id: UUID | str | bytes | bytearray,
        username: str,
        email: Optional[str] = None,
        display_name: Optional[str] = None,
        role: str = "user",
        is_active: bool = True,
        commit: bool = True,
    ) -> User:
        """Compatibility alias for :meth:`provision_ad_user`."""

        return await UserRepository.provision_ad_user(
            session,
            authority=authority,
            external_id=external_id,
            username=username,
            email=email,
            display_name=display_name,
            role=role,
            is_active=is_active,
            commit=commit,
        )

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

        # This repository is the local bcrypt adapter only.  AD credentials
        # must be verified by the canonical external-auth service and must
        # never fall back to a local hash (which is absent by contract).
        if getattr(user, "auth_source", "local") != "local":
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
        # Reject external identities before doing any bcrypt work.  The
        # submitted value is a local-password concern only; an AD account
        # must never even enter the local credential mutation path.
        scalar = getattr(session, "scalar", None)
        if callable(scalar):
            try:
                target = await scalar(
                    select(User).where(User.id == user_id).limit(1)
                )
            except Exception:
                target = None
            if target is not None and getattr(target, "auth_source", "local") == "ad":
                raise ExternalIdentityCredentialError(
                    "Active Directory users do not have local passwords"
                )
        values: Dict[str, Any] = {
            "password_hash": UserRepository.hash_password(new_password),
            "session_version": func.coalesce(User.session_version, 1) + 1,
            "updated_at": datetime.utcnow(),
        }
        if clear_reset_flag:
            values["is_password_reset_required"] = False

        result = await session.execute(
            update(User)
            # Keep this write path local-only.  The database trigger is the
            # final guard, while the predicate avoids even attempting a
            # password mutation for an AD-owned account.
            .where(and_(User.id == user_id, User.auth_source == "local"))
            .values(**values)
            .returning(User.id)
        )
        if result.scalar_one_or_none() is None:
            # Distinguish a missing user from an external account where
            # callers need a safe, actionable rejection.  Lightweight test
            # doubles may not implement ``scalar``; in that case preserve
            # the historical ``False`` result.
            scalar = getattr(session, "scalar", None)
            if callable(scalar):
                try:
                    target = await scalar(
                        select(User).where(User.id == user_id).limit(1)
                    )
                except Exception:
                    target = None
                if target is not None and getattr(target, "auth_source", "local") == "ad":
                    if commit:
                        await session.rollback()
                    raise ExternalIdentityCredentialError(
                        "Active Directory users do not have local passwords"
                    )
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

        if (
            getattr(user, "auth_source", "local") == "ad"
            and "is_password_reset_required" in kwargs
        ):
            raise ExternalIdentityCredentialError(
                "Active Directory users do not support local password resets"
            )

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
        if getattr(user, "auth_source", "local") == "ad":
            raise ExternalIdentityCredentialError(
                "Active Directory users do not support local password resets"
            )
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
        if user is not None and getattr(user, "auth_source", "local") == "ad":
            raise ExternalIdentityCredentialError(
                "Active Directory users do not support local password resets"
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
            or (
                getattr(user, "auth_source", "local") == "local"
                and not bool(getattr(user, "is_password_reset_required", False))
            )
            or not isinstance(previous_lifecycle, dict)
            or previous_lifecycle.get("state") != "deleted"
        )
        user.is_active = False
        # AD owns the credential lifecycle; disabling the shadow account must
        # not set a local reset marker (and the DB check/trigger rejects it).
        if getattr(user, "auth_source", "local") == "local":
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

        if getattr(user, "auth_source", "local") == "ad":
            # AD identities are durable ownership principals.  Keep the row
            # and immutable binding for audit/ownership continuity; callers
            # may use ``soft_delete_user`` to disable the account instead.
            raise UserDeletionBlockedError(
                "Active Directory users cannot be permanently deleted",
                [{"label": "External identity binding", "count": 1}],
            )

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
