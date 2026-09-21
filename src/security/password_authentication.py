
"""Canonical local/Active Directory password authentication service.

Every password login enters this module before a local bcrypt check or an AD
bind.  The selector is explicit: an enabled AD deployment rejects an omitted
source, and selecting one source never falls back to the other after failure.
The service contains no HTTP/cookie concerns; adapters keep their existing
throttle, audit, session-version, and WebSocket behaviour around this seam.
"""

from __future__ import annotations

import inspect
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Mapping
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .active_directory import (
    ActiveDirectoryAccountDisabled,
    ActiveDirectoryClient,
    ActiveDirectoryConfig,
    ActiveDirectoryError,
    ActiveDirectoryIdentity,
    ActiveDirectoryInvalidCredentials,
    normalize_object_guid,
)

logger = logging.getLogger(__name__)


class CredentialSource(str, Enum):
    """Password authority selected for one login attempt."""

    LOCAL = "local"
    # ``ad`` is the persisted/source-contract value.  Human-facing/API input
    # may still use ``active_directory``/``active-directory`` aliases.
    ACTIVE_DIRECTORY = "ad"
    AD = "ad"


class AuthenticationErrorCode(str, Enum):
    CREDENTIAL_SOURCE_REQUIRED = "credential_source_required"
    UNSUPPORTED_CREDENTIAL_SOURCE = "unsupported_credential_source"
    INVALID_CREDENTIALS = "invalid_credentials"
    ACCOUNT_DISABLED = "account_disabled"
    AD_CONFIGURATION_UNAVAILABLE = "ad_configuration_unavailable"
    AD_UNREACHABLE = "ad_unreachable"
    AD_TLS_FAILURE = "ad_tls_failure"
    AD_IDENTITY_LOOKUP_FAILED = "ad_identity_lookup_failed"
    AD_PROVISIONING_CONFLICT = "ad_provisioning_conflict"
    AUTHENTICATION_BACKEND_UNAVAILABLE = "authentication_backend_unavailable"


_SAFE_ERROR_MESSAGES: Mapping[AuthenticationErrorCode, str] = {
    AuthenticationErrorCode.CREDENTIAL_SOURCE_REQUIRED: "Credential source is required",
    AuthenticationErrorCode.UNSUPPORTED_CREDENTIAL_SOURCE: "Credential source is not available",
    AuthenticationErrorCode.INVALID_CREDENTIALS: "Invalid credentials",
    AuthenticationErrorCode.ACCOUNT_DISABLED: "Account is disabled",
    AuthenticationErrorCode.AD_CONFIGURATION_UNAVAILABLE: "Active Directory authentication is unavailable",
    AuthenticationErrorCode.AD_UNREACHABLE: "Active Directory could not be reached",
    AuthenticationErrorCode.AD_TLS_FAILURE: "Active Directory TLS validation failed",
    AuthenticationErrorCode.AD_IDENTITY_LOOKUP_FAILED: "Active Directory identity lookup failed",
    AuthenticationErrorCode.AD_PROVISIONING_CONFLICT: "Active Directory account provisioning conflict",
    AuthenticationErrorCode.AUTHENTICATION_BACKEND_UNAVAILABLE: "Authentication backend is unavailable",
}


class AuthenticationError(RuntimeError):
    """Stable, safe authentication failure returned to HTTP adapters."""

    def __init__(
        self,
        code: AuthenticationErrorCode | str,
        *,
        cause: BaseException | None = None,
    ):
        try:
            self.code = AuthenticationErrorCode(code)
        except ValueError:
            # Keep an unknown extension code available to an adapter while
            # retaining the generic safe message.
            self.code = str(code)
        safe = _SAFE_ERROR_MESSAGES.get(self.code, "Authentication failed")
        self.safe_message = safe
        self.cause = cause
        super().__init__(safe)

    @property
    def code_value(self) -> str:
        return self.code.value if isinstance(self.code, Enum) else str(self.code)


@dataclass(frozen=True, slots=True)
class PasswordAuthenticationResult:
    """Canonical result consumed by FastAPI, Next.js BFF, and mobile routes."""

    user: Any
    credential_source: CredentialSource
    identity: ActiveDirectoryIdentity | None = None
    created: bool = False

    @property
    def source(self) -> CredentialSource:
        return self.credential_source

    @property
    def auth_source(self) -> str:
        return self.credential_source.value

    @property
    def user_id(self) -> str | None:
        value = getattr(self.user, "id", None)
        return str(value) if value is not None else None

    @property
    def is_new_user(self) -> bool:
        return self.created


class PasswordAuthenticationService:
    """Single authority-selection seam for every password login surface."""

    def __init__(
        self,
        *,
        repository: Any | None = None,
        ad_client: ActiveDirectoryClient | Any | None = None,
        ad_config: ActiveDirectoryConfig | None = None,
        enterprise: bool | None = None,
    ):
        self.repository = repository if repository is not None else _default_repository()
        self.enterprise = enterprise
        self.ad_config = ad_config
        self.ad_client = ad_client
        self._ad_config_error: ActiveDirectoryError | None = None
        self._ad_requested = str(os.getenv("AOITALK_AD_ENABLED", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if self.ad_config is None and ad_client is None:
            try:
                self.ad_config = ActiveDirectoryConfig.from_env(enterprise=enterprise)
            except ActiveDirectoryError as exc:
                # Keep the service constructible so a local-only login can
                # still return a stable ``unsupported`` error for AD instead
                # of crashing app startup.  Explicit config validation occurs
                # when AD is selected.
                self._ad_config_error = exc
                self.ad_config = None

    @property
    def ad_enabled(self) -> bool:
        if self.ad_config is not None:
            return bool(self.ad_config.enabled)
        # Injected clients are used by tests and by controlled adapters; they
        # represent an explicitly configured AD backend.
        return self.ad_client is not None or self._ad_requested

    async def authenticate(
        self,
        session: Any,
        username: str,
        password: str,
        credential_source: CredentialSource | str | None = None,
        *,
        commit: bool = False,
    ) -> PasswordAuthenticationResult:
        """Authenticate one login attempt without source fallback.

        ``commit=False`` is the normal adapter mode: the caller's existing
        login-throttle transaction records the audit outcome and commits once.
        AD JIT provisioning and ``last_login`` are flushed in that same
        transaction.  A standalone caller can request ``commit=True``.
        """

        normalized_username = _validate_username(username)
        _validate_password(password)
        source = self._select_source(credential_source)
        if source is CredentialSource.LOCAL:
            return await self._authenticate_local(
                session,
                normalized_username,
                password,
                commit=commit,
            )
        return await self._authenticate_ad(
            session,
            normalized_username,
            password,
            commit=commit,
        )

    def _select_source(
        self,
        requested: CredentialSource | str | None,
    ) -> CredentialSource:
        if requested is None:
            if self.ad_enabled:
                raise AuthenticationError(AuthenticationErrorCode.CREDENTIAL_SOURCE_REQUIRED)
            return CredentialSource.LOCAL
        if isinstance(requested, CredentialSource):
            source = requested
        elif isinstance(requested, str):
            value = requested.strip().lower().replace("-", "_")
            aliases = {
                "local": CredentialSource.LOCAL,
                "ad": CredentialSource.ACTIVE_DIRECTORY,
                "active_directory": CredentialSource.ACTIVE_DIRECTORY,
                "activedirectory": CredentialSource.ACTIVE_DIRECTORY,
                "active_directory_domain": CredentialSource.ACTIVE_DIRECTORY,
            }
            source = aliases.get(value)
            if source is None:
                raise AuthenticationError(AuthenticationErrorCode.UNSUPPORTED_CREDENTIAL_SOURCE)
        else:
            raise AuthenticationError(AuthenticationErrorCode.UNSUPPORTED_CREDENTIAL_SOURCE)
        if source is CredentialSource.ACTIVE_DIRECTORY and self.enterprise is False:
            # AD is an Enterprise-only capability.  This explicit guard also
            # covers dependency-injected clients/configuration so a Personal
            # adapter cannot bypass the profile boundary.
            raise AuthenticationError(
                AuthenticationErrorCode.AD_CONFIGURATION_UNAVAILABLE
            )
        if source is CredentialSource.ACTIVE_DIRECTORY and not self.ad_enabled:
            raise AuthenticationError(AuthenticationErrorCode.UNSUPPORTED_CREDENTIAL_SOURCE)
        return source

    async def _authenticate_local(
        self,
        session: Any,
        username: str,
        password: str,
        *,
        commit: bool,
    ) -> PasswordAuthenticationResult:
        if self.repository is None:
            raise AuthenticationError(
                AuthenticationErrorCode.AUTHENTICATION_BACKEND_UNAVAILABLE
            )
        repository = self.repository
        user = await _repo_call(repository, "get_by_username", session, username)
        if user is None:
            raise AuthenticationError(AuthenticationErrorCode.INVALID_CREDENTIALS)
        if not bool(getattr(user, "is_active", True)):
            raise AuthenticationError(AuthenticationErrorCode.ACCOUNT_DISABLED)
        if _user_source(user) is not CredentialSource.LOCAL:
            # Explicit local authentication never tests an AD password or
            # silently links a same-name account.
            raise AuthenticationError(AuthenticationErrorCode.INVALID_CREDENTIALS)
        password_hash = getattr(user, "password_hash", None)
        if not password_hash:
            raise AuthenticationError(AuthenticationErrorCode.INVALID_CREDENTIALS)

        # Keep the repository's canonical bcrypt implementation, including
        # any future cost/algorithm upgrades.  ``authenticate`` also updates
        # last_login, while the fallback below supports small test fakes.
        authenticate = getattr(repository, "authenticate", None)
        if callable(authenticate):
            result = await _invoke_repository_authenticate(
                authenticate,
                session,
                username,
                password,
                commit=commit,
            )
            if result is None:
                raise AuthenticationError(AuthenticationErrorCode.INVALID_CREDENTIALS)
            # A repository adapter is not allowed to silently return an
            # externally managed row for an explicitly local attempt.  Keep
            # this invariant at the service boundary as well as in the
            # canonical repository implementation.
            if (
                _user_source(result) is not CredentialSource.LOCAL
                or not bool(getattr(result, "is_active", True))
                or not getattr(result, "password_hash", None)
            ):
                raise AuthenticationError(AuthenticationErrorCode.INVALID_CREDENTIALS)
            user = result
        else:
            verify = getattr(repository, "verify_password", None)
            valid = bool(verify(password, password_hash)) if callable(verify) else False
            if not valid:
                raise AuthenticationError(AuthenticationErrorCode.INVALID_CREDENTIALS)
            await _touch_user_login(session, user, commit=commit)
        return PasswordAuthenticationResult(user=user, credential_source=CredentialSource.LOCAL)

    async def _authenticate_ad(
        self,
        session: Any,
        username: str,
        password: str,
        *,
        commit: bool,
    ) -> PasswordAuthenticationResult:
        client = self.ad_client
        if client is None:
            config = self.ad_config
            if config is None:
                if self._ad_config_error is not None:
                    raise _map_ad_error(self._ad_config_error)
                raise AuthenticationError(AuthenticationErrorCode.AD_CONFIGURATION_UNAVAILABLE)
            try:
                config.validate(enterprise=self.enterprise)
                client = ActiveDirectoryClient(config)
                self.ad_client = client
            except ActiveDirectoryError as exc:
                raise _map_ad_error(exc) from exc
        elif self.ad_config is not None and self.ad_config.enabled:
            # Even an injected transport is subject to the Enterprise-only
            # configuration boundary; dependency injection must not become a
            # production bypass for profile enforcement.
            try:
                self.ad_config.validate(enterprise=self.enterprise)
            except ActiveDirectoryError as exc:
                raise _map_ad_error(exc) from exc
        try:
            identity = await _maybe_await(client.authenticate(username, password))
        except AuthenticationError:
            raise
        except ActiveDirectoryError as exc:
            raise _map_ad_error(exc) from exc
        except Exception as exc:
            # A transport implementation must not leak backend details to the
            # caller.  Treat unknown client failures as unavailable.
            # Do not attach the exception text/traceback: third-party LDAP
            # errors and injected adapters are not guaranteed to redact the
            # submitted password or bind name.
            logger.warning("Active Directory authentication backend failed")
            raise AuthenticationError(
                AuthenticationErrorCode.AUTHENTICATION_BACKEND_UNAVAILABLE,
                cause=exc,
            ) from exc
        if not isinstance(identity, ActiveDirectoryIdentity):
            # Accept structurally compatible fakes while requiring the
            # immutable GUID and canonical login attribute.
            try:
                identity = _coerce_identity(identity, authority=getattr(self.ad_config, "authority", None))
            except Exception as exc:
                raise AuthenticationError(
                    AuthenticationErrorCode.AD_IDENTITY_LOOKUP_FAILED,
                    cause=exc,
                ) from exc
        try:
            _validate_directory_identity(
                identity,
                configured_authority=getattr(self.ad_config, "authority", None),
            )
        except Exception as exc:
            raise AuthenticationError(
                AuthenticationErrorCode.AD_IDENTITY_LOOKUP_FAILED,
                cause=exc,
            ) from exc
        if not identity.is_active:
            raise AuthenticationError(AuthenticationErrorCode.ACCOUNT_DISABLED)
        if not identity.authority and not getattr(self.ad_config, "authority", None):
            raise AuthenticationError(AuthenticationErrorCode.AD_CONFIGURATION_UNAVAILABLE)

        try:
            user, created = await self._get_or_provision_ad_user(
                session,
                identity,
                commit=commit,
            )
        except AuthenticationError:
            raise
        except (ValueError, IntegrityError) as exc:
            # Repository validation/unique-key failures are safe, actionable
            # provisioning conflicts (never an implicit local fallback).
            raise AuthenticationError(
                AuthenticationErrorCode.AD_PROVISIONING_CONFLICT,
                cause=exc,
            ) from exc
        except SQLAlchemyError as exc:
            logger.warning("Active Directory shadow database operation failed")
            raise AuthenticationError(
                AuthenticationErrorCode.AUTHENTICATION_BACKEND_UNAVAILABLE,
                cause=exc,
            ) from exc
        except Exception as exc:
            logger.warning("Active Directory local shadow provisioning failed")
            raise AuthenticationError(
                AuthenticationErrorCode.AUTHENTICATION_BACKEND_UNAVAILABLE,
                cause=exc,
            ) from exc
        if user is None:
            raise AuthenticationError(AuthenticationErrorCode.AD_PROVISIONING_CONFLICT)
        if not bool(getattr(user, "is_active", True)):
            raise AuthenticationError(AuthenticationErrorCode.ACCOUNT_DISABLED)
        if _user_source(user) is not CredentialSource.ACTIVE_DIRECTORY:
            raise AuthenticationError(AuthenticationErrorCode.AD_PROVISIONING_CONFLICT)
        # ``provision_ad_user`` owns identity binding/profile state but does
        # not own login audit timestamps.  Keep last_login semantics aligned
        # with the local bcrypt path in the caller's existing transaction.
        await _touch_user_login(session, user, commit=commit)
        return PasswordAuthenticationResult(
            user=user,
            credential_source=CredentialSource.ACTIVE_DIRECTORY,
            identity=identity,
            created=created,
        )

    async def _get_or_provision_ad_user(
        self,
        session: Any,
        identity: ActiveDirectoryIdentity,
        *,
        commit: bool,
    ) -> tuple[Any, bool]:
        repository = self.repository
        # Preferred seam: the repository owns row-locking, binding uniqueness,
        # and the one-transaction JIT operation.  Support both names during
        # rollout so adapters can land independently of the schema migration.
        for method_name in (
            "provision_ad_user",
            "get_or_create_ad_user",
            "provision_external_identity",
            "get_or_provision_external_identity",
        ):
            method = getattr(repository, method_name, None)
            if not callable(method):
                continue
            authority = identity.authority or getattr(self.ad_config, "authority", None)
            if not authority:
                raise AuthenticationError(
                    AuthenticationErrorCode.AD_CONFIGURATION_UNAVAILABLE
                )
            value = await _invoke_provision_method(
                method,
                session,
                identity,
                authority=authority,
                commit=commit,
            )
            return _parse_provision_result(value)
        return await self._provision_with_sqlalchemy(session, identity, commit=commit)

    async def _provision_with_sqlalchemy(
        self,
        session: Any,
        identity: ActiveDirectoryIdentity,
        *,
        commit: bool,
    ) -> tuple[Any, bool]:
        """Fallback JIT implementation for repositories without the seam.

        This path is intentionally strict and only runs when the migration's
        ``ExternalIdentityBinding`` model is importable.  It links exclusively
        on ``(source, authority, objectGUID)``; username/email are collision
        checks, never identity keys.
        """

        try:
            from ..memory import models as models_module
            from ..memory.models import User
        except Exception as exc:
            raise AuthenticationError(
                AuthenticationErrorCode.AUTHENTICATION_BACKEND_UNAVAILABLE,
                cause=exc,
            ) from exc
        binding_model = _find_binding_model(models_module)
        if binding_model is None:
            raise AuthenticationError(
                AuthenticationErrorCode.AUTHENTICATION_BACKEND_UNAVAILABLE
            )

        source = CredentialSource.ACTIVE_DIRECTORY.value
        authority = identity.authority or getattr(self.ad_config, "authority", None)
        if not authority:
            raise AuthenticationError(AuthenticationErrorCode.AD_CONFIGURATION_UNAVAILABLE)
        # Repository persistence canonicalizes the binding namespace.  Apply
        # the same normalization in this fallback path so an injected/legacy
        # adapter cannot miss an existing binding merely due to casing.
        authority = str(authority).strip().lower()
        bound_user = await self._find_binding_user(session, User, binding_model, source, authority, identity.object_guid)
        if bound_user is not None:
            if not bool(getattr(bound_user, "is_active", True)):
                raise AuthenticationError(AuthenticationErrorCode.ACCOUNT_DISABLED)
            if _user_source(bound_user) is not CredentialSource.ACTIVE_DIRECTORY:
                raise AuthenticationError(AuthenticationErrorCode.AD_PROVISIONING_CONFLICT)
            await _touch_user_login(session, bound_user, commit=commit)
            return bound_user, False

        repository = self.repository
        same_username = None
        scalar = getattr(session, "scalar", None)
        if callable(scalar):
            same_username = await scalar(
                select(User)
                .where(func.lower(User.username) == identity.username.lower())
                .limit(1)
            )
        else:
            same_username = await _repo_call(
                repository, "get_by_username", session, identity.username
            )
        if same_username is not None:
            # A local same-name row is not proof of identity.  Never link it.
            raise AuthenticationError(AuthenticationErrorCode.AD_PROVISIONING_CONFLICT)
        if identity.email:
            if callable(scalar):
                same_email = await scalar(
                    select(User)
                    .where(func.lower(User.email) == identity.email.lower())
                    .limit(1)
                )
            else:
                same_email = await _repo_call(
                    repository, "get_by_email", session, identity.email
                )
            if same_email is not None:
                raise AuthenticationError(AuthenticationErrorCode.AD_PROVISIONING_CONFLICT)

        user_kwargs = _model_kwargs(
            User,
            {
                "username": identity.username,
                "email": identity.email,
                "display_name": identity.display_name or identity.username,
                "password_hash": None,
                "auth_source": source,
                "role": "user",
                "is_active": True,
                "is_password_reset_required": False,
                "session_version": 1,
            },
        )
        user = User(**user_kwargs)
        begin_nested = getattr(session, "begin_nested", None)
        savepoint_used = False

        async def _insert_shadow() -> None:
            session.add(user)
            await _flush(session)
            binding_kwargs = _model_kwargs(
                binding_model,
                {
                    "user_id": user.id,
                    "source": source,
                    "authority": authority,
                    "external_id": identity.object_guid,
                    "external_subject": str(identity.object_guid),
                    "object_guid": identity.object_guid,
                },
            )
            binding = binding_model(**binding_kwargs)
            session.add(binding)
            await _flush(session)

        try:
            if callable(begin_nested):
                transaction = begin_nested()
                if inspect.isawaitable(transaction):
                    transaction = await transaction
                if hasattr(transaction, "__aenter__"):
                    async with transaction:
                        # Mark the savepoint only after its context has been
                        # entered.  If begin_nested itself fails, a normal
                        # rollback below is still required.
                        savepoint_used = True
                        await _insert_shadow()
                else:
                    # Tiny compatibility fakes may expose begin_nested as a
                    # marker rather than an async context manager.
                    await _insert_shadow()
            else:
                await _insert_shadow()
        except IntegrityError as exc:
            # A concurrent first login may win the immutable binding unique
            # key.  A SAVEPOINT keeps the caller's outer audit/throttle
            # transaction usable; re-read the winner and reuse its local
            # principal rather than manufacturing a duplicate or falling
            # back to a same-name account.  Sessions without savepoints are
            # rolled back before surfacing the safe conflict.
            if not savepoint_used:
                rollback = getattr(session, "rollback", None)
                if callable(rollback):
                    await rollback()
            winner = await self._find_binding_user(
                session,
                User,
                binding_model,
                source,
                authority,
                identity.object_guid,
            )
            if winner is not None:
                if not bool(getattr(winner, "is_active", True)):
                    raise AuthenticationError(AuthenticationErrorCode.ACCOUNT_DISABLED)
                if _user_source(winner) is not CredentialSource.ACTIVE_DIRECTORY:
                    raise AuthenticationError(
                        AuthenticationErrorCode.AD_PROVISIONING_CONFLICT
                    )
                await _touch_user_login(session, winner, commit=commit)
                return winner, False
            raise AuthenticationError(
                AuthenticationErrorCode.AD_PROVISIONING_CONFLICT,
                cause=exc,
            ) from exc
        if commit:
            await _commit(session)
        return user, True

    async def _find_binding_user(
        self,
        session: Any,
        user_model: Any,
        binding_model: Any,
        source: str,
        authority: str,
        external_id: UUID,
    ) -> Any | None:
        authority = str(authority).strip().lower()
        external_id = normalize_object_guid(external_id)
        keys = _model_column_keys(binding_model)
        source_col = getattr(binding_model, "source", None)
        ext_col = getattr(binding_model, "external_id", None)
        if ext_col is None:
            ext_col = getattr(binding_model, "external_subject", None)
        auth_col = getattr(binding_model, "authority", None)
        user_id_col = getattr(binding_model, "user_id", None)
        if source_col is None or ext_col is None or user_id_col is None:
            raise AuthenticationError(AuthenticationErrorCode.AUTHENTICATION_BACKEND_UNAVAILABLE)
        conditions = [source_col == source]
        value = external_id if "external_id" in keys else str(external_id)
        conditions.append(ext_col == value)
        if auth_col is not None and "authority" in keys:
            conditions.append(auth_col == authority)
        query = select(user_model).join(binding_model, user_id_col == user_model.id).where(*conditions).limit(1)
        scalar = getattr(session, "scalar", None)
        if callable(scalar):
            return await scalar(query)
        execute = getattr(session, "execute", None)
        if callable(execute):
            result = await execute(query)
            return result.scalar_one_or_none()
        raise AuthenticationError(AuthenticationErrorCode.AUTHENTICATION_BACKEND_UNAVAILABLE)


def _default_repository() -> Any:
    try:
        from ..memory.user_repository import UserRepository

        return UserRepository
    except Exception:
        return None


def _validate_username(value: str) -> str:
    if not isinstance(value, str):
        raise AuthenticationError(AuthenticationErrorCode.INVALID_CREDENTIALS)
    normalized = value.strip()
    if not normalized or len(normalized) > 255:
        raise AuthenticationError(AuthenticationErrorCode.INVALID_CREDENTIALS)
    # Directory login names may contain punctuation, but never embedded
    # controls.  Rejecting NUL/CR/LF and other controls at the shared seam
    # prevents them from reaching bind-name construction or audit adapters.
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized):
        raise AuthenticationError(AuthenticationErrorCode.INVALID_CREDENTIALS)
    return normalized


def _validate_password(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise AuthenticationError(AuthenticationErrorCode.INVALID_CREDENTIALS)


def _user_source(user: Any) -> CredentialSource:
    raw = getattr(user, "auth_source", None)
    if raw is None or raw == "":
        return CredentialSource.LOCAL
    raw = getattr(raw, "value", raw)
    normalized = str(raw).strip().lower().replace("-", "_")
    if normalized in {"ad", "active_directory", "activedirectory", "active directory"}:
        return CredentialSource.ACTIVE_DIRECTORY
    if normalized == "local":
        return CredentialSource.LOCAL
    return CredentialSource.LOCAL


async def _repo_call(repository: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    if repository is None:
        return None
    method = getattr(repository, name, None)
    if not callable(method):
        return None
    try:
        value = method(*args, **kwargs)
    except TypeError as exc:
        # Compatibility with minimal test fakes that do not accept kwargs.
        if kwargs and any(token in str(exc) for token in ("unexpected keyword", "positional argument")):
            value = method(*args)
        else:
            raise
    return await _maybe_await(value)


async def _invoke_repository_authenticate(
    method: Callable[..., Any],
    session: Any,
    username: str,
    password: str,
    *,
    commit: bool,
) -> Any:
    try:
        signature = inspect.signature(method)
        accepts_commit = "commit" in signature.parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
    except (TypeError, ValueError):
        accepts_commit = True
    kwargs = {"commit": commit} if accepts_commit else {}
    return await _maybe_await(method(session, username, password, **kwargs))


async def _invoke_provision_method(
    method: Callable[..., Any],
    session: Any,
    identity: ActiveDirectoryIdentity,
    *,
    authority: str | None = None,
    commit: bool,
) -> Any:
    # The preferred repository seam receives the identity object.  A fallback
    # keyword form keeps adapters decoupled from this module's dataclass.
    try:
        signature = inspect.signature(method)
        params = signature.parameters
    except (TypeError, ValueError):
        params = {}
    kwargs = {}
    accepts_var_kwargs = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if "commit" in params or accepts_var_kwargs:
        kwargs["commit"] = commit
    # The repository's canonical seam uses explicit immutable identity
    # keyword arguments.  Passing those rather than a positional dataclass
    # keeps the model/repository layer independent of this security module.
    identity_kwargs = {
        "authority": authority or identity.authority,
        "external_id": identity.object_guid,
        "external_subject": str(identity.object_guid),
        "object_guid": identity.object_guid,
        "username": identity.username,
        "email": identity.email,
        "display_name": identity.display_name,
        "role": "user",
        "is_active": identity.is_active,
        "source": CredentialSource.ACTIVE_DIRECTORY.value,
    }
    named_identity = {
        key: value
        for key, value in identity_kwargs.items()
        if key in params or accepts_var_kwargs
    }
    if "identity" in params:
        return await _maybe_await(method(session, identity=identity, **kwargs))
    if named_identity:
        return await _maybe_await(method(session, **named_identity, **kwargs))
    if len(params) >= 2:
        return await _maybe_await(method(session, identity, **kwargs))
    return await _maybe_await(method(identity, **kwargs))


def _parse_provision_result(value: Any) -> tuple[Any, bool]:
    if isinstance(value, PasswordAuthenticationResult):
        return value.user, value.created
    if isinstance(value, tuple) and len(value) == 2:
        return value[0], bool(value[1])
    if isinstance(value, Mapping) and "user" in value:
        return value.get("user"), bool(
            value.get("created", value.get("is_new_user", False))
        )
    # The canonical SQLAlchemy repository returns the User row directly.  It
    # annotates that transient instance with the creation outcome so adapters
    # can expose a reliable first-login/JIT signal without changing the
    # repository's long-standing return type or persisting extra state.
    return value, bool(getattr(value, "_ad_created", False))


async def _touch_user_login(session: Any, user: Any, *, commit: bool) -> None:
    if hasattr(user, "last_login"):
        user.last_login = datetime.utcnow()
    # Minimal repository test doubles may intentionally omit transaction
    # methods; the production AsyncSession always exposes one of these.
    if not callable(getattr(session, "commit" if commit else "flush", None)):
        return
    await (_commit(session) if commit else _flush(session))


def _find_binding_model(models_module: Any) -> Any | None:
    for name in (
        "ExternalIdentityBinding",
        "UserExternalIdentity",
        "ExternalIdentity",
        "UserIdentityBinding",
    ):
        model = getattr(models_module, name, None)
        if model is not None:
            return model
    return None


def _model_column_keys(model: Any) -> set[str]:
    try:
        from sqlalchemy import inspect as sa_inspect

        return {column.key for column in sa_inspect(model).columns}
    except Exception:
        table = getattr(model, "__table__", None)
        columns = getattr(table, "columns", ())
        return {getattr(column, "key", "") for column in columns}


def _model_kwargs(model: Any, values: Mapping[str, Any]) -> dict[str, Any]:
    keys = _model_column_keys(model)
    return {key: value for key, value in values.items() if key in keys}


async def _flush(session: Any) -> None:
    flush = getattr(session, "flush", None)
    if not callable(flush):
        raise AuthenticationError(AuthenticationErrorCode.AUTHENTICATION_BACKEND_UNAVAILABLE)
    await _maybe_await(flush())


async def _commit(session: Any) -> None:
    commit = getattr(session, "commit", None)
    if not callable(commit):
        raise AuthenticationError(AuthenticationErrorCode.AUTHENTICATION_BACKEND_UNAVAILABLE)
    await _maybe_await(commit())


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _coerce_identity(value: Any, *, authority: str | None) -> ActiveDirectoryIdentity:
    if isinstance(value, Mapping):
        guid = value.get("object_guid", value.get("objectGUID", value.get("external_id")))
        username = value.get("username", value.get("sAMAccountName"))
        if not isinstance(username, str):
            raise ValueError("identity missing canonical login attribute")
        return ActiveDirectoryIdentity(
            object_guid=normalize_object_guid(guid),
            username=username,
            display_name=value.get("display_name", value.get("displayName")),
            email=value.get("email", value.get("mail")),
            authority=value.get("authority") or authority,
            is_active=bool(value.get("is_active", True)),
        )
    guid = getattr(value, "object_guid", getattr(value, "objectGUID", None))
    username = getattr(value, "username", getattr(value, "sAMAccountName", None))
    if guid is None:
        raise ValueError("identity missing immutable objectGUID")
    if not isinstance(username, str):
        raise ValueError("identity missing canonical login attribute")
    return ActiveDirectoryIdentity(
        object_guid=normalize_object_guid(guid),
        username=username,
        display_name=getattr(value, "display_name", None),
        email=getattr(value, "email", None),
        authority=getattr(value, "authority", None) or authority,
        is_active=bool(getattr(value, "is_active", True)),
    )


def _validate_directory_identity(
    identity: ActiveDirectoryIdentity,
    *,
    configured_authority: str | None,
) -> None:
    """Validate the small identity envelope before it reaches persistence.

    The real LDAP client already applies these checks.  Repeating them for
    injected/alternate adapters keeps a malformed provider response from
    becoming a durable shadow account or an unexpected binding namespace.
    """

    normalize_object_guid(identity.object_guid)
    if not isinstance(identity.username, str):
        raise ValueError("directory identity username is invalid")
    username = identity.username.strip()
    if not username or len(username) > 100:
        raise ValueError("directory identity username is invalid")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in username):
        raise ValueError("directory identity username is invalid")
    for value, maximum, label in (
        (identity.display_name, 100, "display name"),
        (identity.email, 255, "email"),
    ):
        if value is None:
            continue
        if not isinstance(value, str) or len(value.strip()) > maximum:
            raise ValueError(f"directory identity {label} is invalid")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            raise ValueError(f"directory identity {label} is invalid")
    for value, label in (
        (identity.authority, "authority"),
        (configured_authority, "authority"),
    ):
        if value is not None:
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > 255:
                raise ValueError(f"directory identity {label} is invalid")
            if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
                raise ValueError(f"directory identity {label} is invalid")
    if configured_authority and identity.authority:
        if identity.authority.strip().lower() != configured_authority.strip().lower():
            raise ValueError("directory identity authority does not match configuration")
    if type(identity.is_active) is not bool:
        raise ValueError("directory identity active state is invalid")


def _map_ad_error(exc: ActiveDirectoryError) -> AuthenticationError:
    code = getattr(exc, "code", AuthenticationErrorCode.AD_CONFIGURATION_UNAVAILABLE)
    try:
        mapped = AuthenticationErrorCode(code)
    except ValueError:
        mapped = AuthenticationErrorCode.AD_CONFIGURATION_UNAVAILABLE
    return AuthenticationError(mapped, cause=exc)


# Compatibility aliases for adapters/tests using shorter names.
AuthResult = PasswordAuthenticationResult
PasswordAuthenticationError = AuthenticationError
AuthErrorCode = AuthenticationErrorCode
ADCredentialSource = CredentialSource


def get_password_authentication_service(**kwargs: Any) -> PasswordAuthenticationService:
    """Construct the canonical service for an adapter/DI container."""

    return PasswordAuthenticationService(**kwargs)


__all__ = [
    "ADCredentialSource",
    "AuthErrorCode",
    "AuthResult",
    "AuthenticationError",
    "AuthenticationErrorCode",
    "CredentialSource",
    "PasswordAuthenticationError",
    "PasswordAuthenticationResult",
    "PasswordAuthenticationService",
    "get_password_authentication_service",
]
