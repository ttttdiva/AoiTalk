"""Trusted issuance and revalidation of native employee capabilities.

An ordinary employee operation may use the host's native Windows execution
path only when the AoiTalk request identity is explicitly bound to the exact
Windows process token running the operation.  This service is intentionally
fail-closed:

* the deployment must explicitly enable ``native_employee_execution``;
* the current process must be Windows (the Enterprise Docker process never
  qualifies);
* configuration must contain a one-to-one UUID-to-SID binding and endpoint;
* the active authenticated AoiTalk user and current process token SID must
  match that binding; and
* every dispatch revalidates the short-lived capability.

No shell probe is used to determine identity.  The process token is read
through pywin32 when available and a direct Windows API (ctypes) fallback
otherwise.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Mapping
from uuid import UUID

from ..security.native_employee_execution import (
    NativeEmployeeExecutionCapability,
    NativeEmployeeExecutionConfigurationError,
    NativeEmployeeExecutionError,
    NativeEmployeeExecutionViolation,
    _normalise_now,
    _normalise_uuid,
    native_employee_execution_context,
    normalise_windows_sid,
    validate_native_employee_execution_capability as _validate_capability_values,
)

logger = logging.getLogger(__name__)

DEFAULT_CAPABILITY_TTL_SECONDS = 300.0
MAX_CAPABILITY_TTL_SECONDS = 7 * 24 * 60 * 60.0

# Windows constants from winnt.h/WinBase.h.  Kept local so importing this
# service never requires pywin32 and remains safe on Linux Enterprise hosts.
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_ERROR_INSUFFICIENT_BUFFER = 122


def _is_windows_runtime() -> bool:
    """Return whether this Python process is natively hosted on Windows."""

    return os.name == "nt" or sys.platform.lower().startswith("win")


def _is_enterprise_runtime() -> bool:
    """Native employee capability issuance is an explicit Enterprise feature."""

    try:
        from ..features import Features

        return bool(Features.is_enterprise())
    except Exception:
        # A missing/failed profile resolver must never broaden execution.
        return False


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    """Read dotted config values from ``Config`` or plain mappings."""

    if config is None:
        return default
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
        except TypeError:
            value = getter(key)
        if value is not None and value is not default:
            return value

    raw = config.get("config") if isinstance(config, Mapping) else getattr(config, "config", None)
    if raw is not None and raw is not config:
        nested = _config_get(raw, key, default)
        if nested is not default:
            return nested

    if isinstance(config, Mapping):
        current: Any = config
        for part in key.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return default
            current = current[part]
        return current
    return default


def _native_employee_settings(config: Any) -> Mapping[str, Any]:
    value = _config_get(config, "native_employee_execution", None)
    if value is None and isinstance(config, Mapping):
        # Tests/integrations may pass the settings branch directly.
        if any(key in config for key in ("enabled", "endpoint_id", "windows_bindings")):
            value = config
    if not isinstance(value, Mapping):
        return {}
    return value


def _resolve_settings(config: Any) -> tuple[bool, str, float, Mapping[Any, Any]]:
    settings = _native_employee_settings(config)
    if not _as_bool(settings.get("enabled", False)):
        raise NativeEmployeeExecutionConfigurationError(
            "native employee execution is not explicitly enabled"
        )
    endpoint_id = str(settings.get("endpoint_id") or "").strip()
    if not endpoint_id or any(character in endpoint_id for character in "\x00\r\n"):
        raise NativeEmployeeExecutionConfigurationError(
            "native_employee_execution.endpoint_id is required"
        )
    if len(endpoint_id) > 512:
        raise NativeEmployeeExecutionConfigurationError("invalid endpoint_id")

    ttl_raw = settings.get("capability_ttl_seconds", DEFAULT_CAPABILITY_TTL_SECONDS)
    try:
        ttl = float(ttl_raw)
    except (TypeError, ValueError) as exc:
        raise NativeEmployeeExecutionConfigurationError(
            "capability_ttl_seconds must be a finite positive number"
        ) from exc
    if ttl <= 0 or ttl > MAX_CAPABILITY_TTL_SECONDS or ttl != ttl or ttl in {
        float("inf"),
        float("-inf"),
    }:
        raise NativeEmployeeExecutionConfigurationError(
            "capability_ttl_seconds must be finite and within the allowed range"
        )

    bindings = settings.get("windows_bindings", {})
    if not isinstance(bindings, Mapping) or not bindings:
        raise NativeEmployeeExecutionConfigurationError(
            "native_employee_execution.windows_bindings must be a non-empty mapping"
        )
    return True, endpoint_id, ttl, bindings


def _normalise_bindings(bindings: Mapping[Any, Any]) -> dict[UUID, str]:
    """Validate a one-to-one UUID-to-SID map without silently deduplicating."""

    result: dict[UUID, str] = {}
    seen_uuid: set[UUID] = set()
    seen_sid: set[str] = set()
    for raw_user, raw_sid in bindings.items():
        try:
            user_id = _normalise_uuid(raw_user, label="windows_bindings user_id")
            sid = normalise_windows_sid(raw_sid)
        except NativeEmployeeExecutionError as exc:
            raise NativeEmployeeExecutionConfigurationError(
                "native_employee_execution.windows_bindings contains an invalid UUID/SID"
            ) from exc
        sid_key = sid.casefold()
        if user_id in seen_uuid:
            raise NativeEmployeeExecutionConfigurationError(
                "native_employee_execution.windows_bindings contains duplicate UUIDs"
            )
        if sid_key in seen_sid:
            raise NativeEmployeeExecutionConfigurationError(
                "native_employee_execution.windows_bindings must be one-to-one; duplicate SIDs are ambiguous"
            )
        seen_uuid.add(user_id)
        seen_sid.add(sid_key)
        result[user_id] = sid
    if len(result) != len(bindings):
        raise NativeEmployeeExecutionConfigurationError(
            "native_employee_execution.windows_bindings contains ambiguous identities"
        )
    return result


def _context_value(context: Any, key: str) -> Any:
    if isinstance(context, Mapping):
        return context.get(key)
    return getattr(context, key, None)


def get_current_authenticated_user_context() -> Any:
    """Return the request's authenticated user context.

    The import remains lazy to avoid a security-module/tool-module import
    cycle during application startup.  Tests and alternate dispatchers may
    monkeypatch this helper rather than constructing an HTTP request.
    """

    try:
        from ..tools.os_operations.tools import get_current_user_context

        return get_current_user_context()
    except Exception as exc:  # pragma: no cover - defensive startup boundary
        raise NativeEmployeeExecutionViolation(
            "authenticated user context is unavailable"
        ) from exc


def _current_authenticated_user_id(context: Any | None = None) -> UUID:
    active = get_current_authenticated_user_context() if context is None else context
    if active is None:
        raise NativeEmployeeExecutionViolation("authenticated user context is required")
    candidates: list[UUID] = []
    for key in ("user_id", "authenticated_user_id", "active_user_id"):
        raw = _context_value(active, key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            candidate = _normalise_uuid(raw, label=key)
        except NativeEmployeeExecutionError as exc:
            raise NativeEmployeeExecutionViolation(
                "authenticated user identity is invalid"
            ) from exc
        candidates.append(candidate)
    if not candidates:
        raise NativeEmployeeExecutionViolation(
            "anonymous native employee execution is not allowed"
        )
    if any(candidate != candidates[0] for candidate in candidates[1:]):
        raise NativeEmployeeExecutionViolation(
            "authenticated and active user identities do not match"
        )
    return candidates[0]


def _read_sid_with_pywin32() -> str:
    """Read the exact current process token SID through pywin32."""

    import win32api  # type: ignore[import-not-found]
    import win32con  # type: ignore[import-not-found]
    import win32security  # type: ignore[import-not-found]

    process = win32api.GetCurrentProcess()
    token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
    try:
        token_user = win32security.GetTokenInformation(
            token,
            win32security.TokenUser,
        )
        sid = token_user[0] if isinstance(token_user, (tuple, list)) else token_user
        converter = getattr(win32security, "ConvertSidToStringSid", None)
        if not callable(converter):
            raise RuntimeError("pywin32 ConvertSidToStringSid is unavailable")
        converted = converter(sid)
        if isinstance(converted, bytes):
            converted = converted.decode("ascii", "strict")
        return normalise_windows_sid(converted)
    finally:
        close = getattr(token, "Close", None)
        if callable(close):
            close()


def _read_sid_with_ctypes() -> str:
    """Read the current token SID using advapi32/kernel32 directly."""

    if not _is_windows_runtime():
        raise NativeEmployeeExecutionViolation(
            "native employee execution requires a Windows host process"
        )

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    except Exception as exc:  # pragma: no cover - exercised only on Windows
        raise NativeEmployeeExecutionViolation(
            "Windows token APIs are unavailable"
        ) from exc

    get_current_process = kernel32.GetCurrentProcess
    get_current_process.restype = ctypes.c_void_p
    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    open_process_token.restype = ctypes.c_int
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    get_token_information.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    convert_sid.restype = ctypes.c_int
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    token_handle = ctypes.c_void_p()
    process_handle = get_current_process()
    if not open_process_token(process_handle, _TOKEN_QUERY, ctypes.byref(token_handle)):
        error = ctypes.get_last_error()
        raise NativeEmployeeExecutionViolation(
            f"OpenProcessToken failed ({error})"
        )
    try:
        required = ctypes.c_uint32()
        get_token_information(
            token_handle,
            _TOKEN_USER,
            None,
            0,
            ctypes.byref(required),
        )
        if required.value <= 0:
            error = ctypes.get_last_error()
            if error != _ERROR_INSUFFICIENT_BUFFER:
                raise NativeEmployeeExecutionViolation(
                    f"GetTokenInformation size query failed ({error})"
                )
        buffer = ctypes.create_string_buffer(required.value)
        if not get_token_information(
            token_handle,
            _TOKEN_USER,
            ctypes.byref(buffer),
            required.value,
            ctypes.byref(required),
        ):
            error = ctypes.get_last_error()
            raise NativeEmployeeExecutionViolation(
                f"GetTokenInformation failed ({error})"
            )
        # TOKEN_USER starts with SID_AND_ATTRIBUTES; the first field is the
        # pointer to the SID.  A c_void_p read is architecture-correct on both
        # 32- and 64-bit Windows.
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents
        sid_text = ctypes.c_wchar_p()
        if not convert_sid(sid_pointer, ctypes.byref(sid_text)):
            error = ctypes.get_last_error()
            raise NativeEmployeeExecutionViolation(
                f"ConvertSidToStringSidW failed ({error})"
            )
        try:
            return normalise_windows_sid(sid_text.value)
        finally:
            if sid_text:
                local_free(ctypes.cast(sid_text, ctypes.c_void_p))
    finally:
        close_handle(token_handle)


def read_current_windows_process_token_sid() -> str:
    """Return the exact current process token SID, never a shell identity."""

    if not _is_windows_runtime():
        raise NativeEmployeeExecutionViolation(
            "native employee execution requires a Windows host process"
        )
    try:
        return _read_sid_with_pywin32()
    except Exception as pywin_error:
        logger.debug("pywin32 token SID lookup unavailable: %s", pywin_error)
        try:
            return _read_sid_with_ctypes()
        except NativeEmployeeExecutionError:
            raise
        except Exception as ctypes_error:
            raise NativeEmployeeExecutionViolation(
                "could not read current Windows process token SID"
            ) from ctypes_error


# Short alias used by endpoint adapters that omit the platform qualifier.
read_current_process_token_sid = read_current_windows_process_token_sid


def _resolve_now(value: Any | None) -> datetime:
    return _normalise_now(value)


def build_current_native_employee_execution_capability(
    config: Any,
    run_id: Any,
    session_id: Any,
    audit_id: Any,
    token_sid_reader: Callable[[], Any] | None = None,
    now: Any | None = None,
) -> NativeEmployeeExecutionCapability:
    """Issue one short-lived capability for the active authenticated user."""

    _enabled, endpoint_id, ttl_seconds, raw_bindings = _resolve_settings(config)
    if not _is_enterprise_runtime():
        raise NativeEmployeeExecutionViolation(
            "native employee execution capability is restricted to Enterprise runtime"
        )
    if not _is_windows_runtime():
        raise NativeEmployeeExecutionViolation(
            "native employee execution is available only in a native Windows process"
        )
    bindings = _normalise_bindings(raw_bindings)
    user_id = _current_authenticated_user_id()
    expected_sid = bindings.get(user_id)
    if expected_sid is None:
        raise NativeEmployeeExecutionViolation(
            "authenticated user has no configured Windows endpoint binding"
        )
    reader = token_sid_reader or read_current_windows_process_token_sid
    try:
        actual_sid = normalise_windows_sid(reader())
    except NativeEmployeeExecutionError:
        raise
    except Exception as exc:
        raise NativeEmployeeExecutionViolation(
            "current Windows process token SID could not be read"
        ) from exc
    if actual_sid != expected_sid:
        raise NativeEmployeeExecutionViolation(
            "current Windows process identity does not match the authenticated user"
        )

    issued_at = _resolve_now(now)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    return NativeEmployeeExecutionCapability._issue(
        user_id=user_id,
        endpoint_id=endpoint_id,
        windows_sid=actual_sid,
        run_id=run_id,
        session_id=session_id,
        audit_id=audit_id,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def validate_current_native_employee_execution_capability(
    capability: NativeEmployeeExecutionCapability,
    config: Any,
    *,
    token_sid_reader: Callable[[], Any] | None = None,
    now: Any | None = None,
) -> NativeEmployeeExecutionCapability:
    """Revalidate config, authenticated user, process SID, and expiry."""

    _enabled, endpoint_id, _ttl_seconds, raw_bindings = _resolve_settings(config)
    if not _is_enterprise_runtime():
        raise NativeEmployeeExecutionViolation(
            "native employee execution capability is restricted to Enterprise runtime"
        )
    if not _is_windows_runtime():
        raise NativeEmployeeExecutionViolation(
            "native employee execution is available only in a native Windows process"
        )
    if not isinstance(capability, NativeEmployeeExecutionCapability):
        raise NativeEmployeeExecutionViolation("invalid native employee capability")
    if capability.endpoint_id != endpoint_id:
        raise NativeEmployeeExecutionViolation(
            "native employee capability endpoint is not configured"
        )
    bindings = _normalise_bindings(raw_bindings)
    expected_sid = bindings.get(capability.user_id)
    if expected_sid is None or expected_sid != capability.windows_sid:
        raise NativeEmployeeExecutionViolation(
            "native employee capability mapping is no longer valid"
        )
    user_id = _current_authenticated_user_id()
    reader = token_sid_reader or read_current_windows_process_token_sid
    try:
        current_sid = normalise_windows_sid(reader())
    except NativeEmployeeExecutionError:
        raise
    except Exception as exc:
        raise NativeEmployeeExecutionViolation(
            "current Windows process token SID could not be read"
        ) from exc
    return validate_native_employee_execution_capability(
        capability,
        current_process_sid=current_sid,
        authenticated_user_id=user_id,
        now=now,
    )


# Short name used by executor integrations.
validate_native_employee_execution_capability_current = (
    validate_current_native_employee_execution_capability
)


@contextmanager
def current_dispatch_native_employee_execution_context(
    config: Any,
    run_id: Any,
    session_id: Any,
    audit_id: Any,
    token_sid_reader: Callable[[], Any] | None = None,
    now: Any | None = None,
) -> Iterator[NativeEmployeeExecutionCapability | None]:
    """Issue/revalidate/bind one capability, or yield ``None`` fail-closed.

    Native capability is an optional acceleration for dispatch.  A Personal,
    Linux, disabled, malformed, anonymous, or mismatched request should keep
    ordinary chat alive while simply declining the native endpoint.  Explicit
    ``issue``/``validate`` calls remain raising APIs for callers that need to
    distinguish configuration and identity failures.
    """

    try:
        capability = build_current_native_employee_execution_capability(
            config,
            run_id,
            session_id,
            audit_id,
            token_sid_reader=token_sid_reader,
            now=now,
        )
        validate_current_native_employee_execution_capability(
            capability,
            config,
            token_sid_reader=token_sid_reader,
            now=now,
        )
    except Exception as exc:
        logger.info("Native employee dispatch capability unavailable: %s", exc)
        yield None
        return
    with native_employee_execution_context(capability, now=now):
        yield capability


class NativeEmployeeExecutionService:
    """Small object facade for request/dispatch integrations."""

    def __init__(
        self,
        config: Any,
        *,
        token_sid_reader: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config
        self.token_sid_reader = token_sid_reader

    def issue(
        self,
        *,
        run_id: Any,
        session_id: Any,
        audit_id: Any,
        now: Any | None = None,
    ) -> NativeEmployeeExecutionCapability:
        return build_current_native_employee_execution_capability(
            self.config,
            run_id,
            session_id,
            audit_id,
            token_sid_reader=self.token_sid_reader,
            now=now,
        )

    build = issue

    def validate(
        self,
        capability: NativeEmployeeExecutionCapability,
        *,
        now: Any | None = None,
    ) -> NativeEmployeeExecutionCapability:
        return validate_current_native_employee_execution_capability(
            capability,
            self.config,
            token_sid_reader=self.token_sid_reader,
            now=now,
        )

    revalidate = validate

    def dispatch_context(
        self,
        *,
        run_id: Any,
        session_id: Any,
        audit_id: Any,
        now: Any | None = None,
    ) -> Iterator[NativeEmployeeExecutionCapability | None]:
        return current_dispatch_native_employee_execution_context(
            self.config,
            run_id,
            session_id,
            audit_id,
            token_sid_reader=self.token_sid_reader,
            now=now,
        )


def validate_native_employee_execution_capability(
    capability: NativeEmployeeExecutionCapability,
    config: Any | None = None,
    *,
    token_sid_reader: Callable[[], Any] | None = None,
    current_process_sid: Any | None = None,
    authenticated_user_id: Any | None = None,
    now: Any | None = None,
) -> NativeEmployeeExecutionCapability:
    """Public validator used by native executors and dispatch services.

    Supplying ``config`` performs full mapping/configuration revalidation.  A
    low-level executor may omit it when it already holds a trusted capability;
    in that form this helper still requires a native Windows host and checks
    the current process token SID and active authenticated user.  The
    injectable reader is for the audited endpoint adapter and deterministic
    tests; production defaults to direct Windows token APIs.
    """

    if config is not None:
        return validate_current_native_employee_execution_capability(
            capability,
            config,
            token_sid_reader=token_sid_reader,
            now=now,
        )
    if not _is_windows_runtime():
        raise NativeEmployeeExecutionViolation(
            "native employee execution is available only in a native Windows process"
        )
    current_user = (
        _current_authenticated_user_id()
        if authenticated_user_id is None
        else _normalise_uuid(authenticated_user_id, label="authenticated_user_id")
    )
    if current_process_sid is None:
        reader = token_sid_reader or read_current_windows_process_token_sid
        try:
            current_process_sid = reader()
        except NativeEmployeeExecutionError:
            raise
        except Exception as exc:
            raise NativeEmployeeExecutionViolation(
                "current Windows process token SID could not be read"
            ) from exc
    return _validate_capability_values(
        capability,
        current_process_sid=current_process_sid,
        authenticated_user_id=current_user,
        now=now,
    )


# Additional aliases keep call sites descriptive without duplicating policy.
build_native_employee_execution_capability = build_current_native_employee_execution_capability
dispatch_native_employee_execution_context = current_dispatch_native_employee_execution_context


__all__ = [
    "DEFAULT_CAPABILITY_TTL_SECONDS",
    "MAX_CAPABILITY_TTL_SECONDS",
    "NativeEmployeeExecutionService",
    "build_current_native_employee_execution_capability",
    "build_native_employee_execution_capability",
    "current_dispatch_native_employee_execution_context",
    "dispatch_native_employee_execution_context",
    "get_current_authenticated_user_context",
    "get_current_authenticated_user_id",
    "read_current_windows_process_token_sid",
    "read_current_process_token_sid",
    "validate_current_native_employee_execution_capability",
    "validate_native_employee_execution_capability",
    "validate_native_employee_execution_capability_current",
]


def get_current_authenticated_user_id() -> UUID:
    """Public helper returning the canonical active UUID."""

    return _current_authenticated_user_id()
