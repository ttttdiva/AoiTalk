"""Short-lived authority for ordinary native employee execution.

The repository/coding execution scopes are intentionally stronger than the
ordinary employee lane.  This module describes the *other* lane: a narrowly
scoped capability that proves that an operation was issued for one
authenticated AoiTalk user and one Windows endpoint.  It is not a bearer
token and it is never reconstructed from model/project JSON.

Only :mod:`src.services.native_employee_execution_service` (or another
trusted server-side factory) may issue a capability.  The capability keeps an
opaque in-process authority marker, while :meth:`to_dict` exposes only audit
metadata.  A caller must revalidate the current authenticated user and the
current Windows process token before using it.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator


class NativeEmployeeExecutionError(RuntimeError):
    """Base error for malformed or unavailable native execution authority."""


class NativeEmployeeExecutionConfigurationError(NativeEmployeeExecutionError):
    """Raised when trusted configuration cannot issue a safe capability."""


class NativeEmployeeExecutionViolation(PermissionError, NativeEmployeeExecutionError):
    """Raised when a capability no longer matches the active identity."""


# Compatibility names used by adapters that predate the longer security
# namespace.  They intentionally refer to the same fail-closed error types.
NativeEmployeeCapabilityError = NativeEmployeeExecutionError
NativeEmployeeCapabilityViolation = NativeEmployeeExecutionViolation


# The marker deliberately has no serialisable representation.  It is an
# identity check rather than a secret string that could leak into logs or
# model-visible metadata.
_CAPABILITY_TOKEN = object()

_SID_RE = re.compile(r"^S-\d+(?:-\d+)+$", re.IGNORECASE)
_MAX_IDENTIFIER_LENGTH = 512


def _clean_identifier(value: Any, *, label: str, required: bool = True) -> str:
    if value is None:
        if required:
            raise NativeEmployeeExecutionConfigurationError(f"{label} is required")
        return ""
    text = str(value).strip()
    if not text and required:
        raise NativeEmployeeExecutionConfigurationError(f"{label} is required")
    if len(text) > _MAX_IDENTIFIER_LENGTH or any(
        character in text for character in "\x00\r\n"
    ):
        raise NativeEmployeeExecutionConfigurationError(f"invalid {label}")
    return text


def _normalise_uuid(value: Any, *, label: str = "user_id") -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        result = value
    else:
        try:
            result = uuid.UUID(str(value).strip())
        except (AttributeError, ValueError, TypeError) as exc:
            raise NativeEmployeeExecutionConfigurationError(
                f"{label} must be a UUID"
            ) from exc
    if result.int == 0:
        raise NativeEmployeeExecutionConfigurationError(f"{label} must not be nil")
    return result


def normalise_windows_sid(value: Any) -> str:
    """Return a canonical string form of a Windows SID.

    SIDs are case-insensitive in their textual prefix.  Requiring the normal
    ``S-<revision>-<identifier authority>-<sub authorities>`` shape avoids
    allowing arbitrary strings to stand in for a process token identity.
    """

    text = _clean_identifier(value, label="windows_sid")
    canonical = text.upper()
    if not _SID_RE.fullmatch(canonical):
        raise NativeEmployeeExecutionConfigurationError("invalid windows_sid")
    return canonical


def _normalise_timestamp(value: Any, *, label: str) -> datetime:
    """Normalise datetime/epoch/ISO values to timezone-aware UTC."""

    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            result = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise NativeEmployeeExecutionConfigurationError(
                f"invalid {label}"
            ) from exc
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise NativeEmployeeExecutionConfigurationError(f"{label} is required")
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise NativeEmployeeExecutionConfigurationError(
                f"invalid {label}"
            ) from exc
    else:
        raise NativeEmployeeExecutionConfigurationError(f"invalid {label}")
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _normalise_now(value: Any | None) -> datetime:
    return _normalise_timestamp(
        datetime.now(timezone.utc) if value is None else value,
        label="now",
    )


@dataclass(frozen=True, slots=True)
class NativeEmployeeExecutionCapability:
    """Immutable server-issued capability for one native employee dispatch.

    ``user_id`` is deliberately a :class:`uuid.UUID`, not an arbitrary model
    string.  The process identity and endpoint/run/session/audit identifiers
    are all part of the signed-by-construction fingerprint.  The private
    authority marker makes a look-alike dataclass or untrusted dictionary
    unusable.
    """

    user_id: uuid.UUID
    endpoint_id: str
    windows_sid: str
    run_id: str
    session_id: str
    audit_id: str
    issued_at: datetime
    expires_at: datetime
    _authority_token: object = field(default=None, repr=False, compare=False)
    fingerprint: str = field(default="", init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._authority_token is not _CAPABILITY_TOKEN:
            raise NativeEmployeeExecutionConfigurationError(
                "NativeEmployeeExecutionCapability can only be issued by a trusted server factory"
            )
        user_id = _normalise_uuid(self.user_id)
        endpoint_id = _clean_identifier(self.endpoint_id, label="endpoint_id")
        windows_sid = normalise_windows_sid(self.windows_sid)
        run_id = _clean_identifier(self.run_id, label="run_id")
        session_id = _clean_identifier(self.session_id, label="session_id")
        audit_id = _clean_identifier(self.audit_id, label="audit_id")
        issued_at = _normalise_timestamp(self.issued_at, label="issued_at")
        expires_at = _normalise_timestamp(self.expires_at, label="expires_at")
        if expires_at <= issued_at:
            raise NativeEmployeeExecutionConfigurationError(
                "expires_at must be after issued_at"
            )

        object.__setattr__(self, "user_id", user_id)
        object.__setattr__(self, "endpoint_id", endpoint_id)
        object.__setattr__(self, "windows_sid", windows_sid)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "audit_id", audit_id)
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "fingerprint", self._compute_fingerprint())

    @classmethod
    def _issue(cls, **kwargs: Any) -> "NativeEmployeeExecutionCapability":
        """Issue a capability from trusted server-side code only."""

        return cls(_authority_token=_CAPABILITY_TOKEN, **kwargs)

    @property
    def windows_user_sid(self) -> str:
        """Compatibility name used by endpoint/executor integrations."""

        return self.windows_sid

    @property
    def token_sid(self) -> str:
        """Alias emphasizing that this is the process-token SID."""

        return self.windows_sid

    @property
    def process_sid(self) -> str:
        """Compatibility alias used by process-execution adapters."""

        return self.windows_sid

    @property
    def expires(self) -> datetime:
        """Compatibility alias for the absolute expiry instant."""

        return self.expires_at

    @property
    def authenticated_user_id(self) -> uuid.UUID:
        return self.user_id

    @property
    def principal_id(self) -> uuid.UUID:
        return self.user_id

    @property
    def is_expired(self) -> bool:
        """Whether the capability is expired at the current UTC instant."""

        return _normalise_now(None) >= self.expires_at

    def expired(self, now: Any | None = None) -> bool:
        """Check expiry at a supplied test/clock instant."""

        return _normalise_now(now) >= self.expires_at

    @property
    def ttl_seconds(self) -> float:
        return max(0.0, (self.expires_at - self.issued_at).total_seconds())

    def _fingerprint_payload(self) -> dict[str, Any]:
        return {
            "user_id": str(self.user_id),
            "endpoint_id": self.endpoint_id,
            "windows_sid": self.windows_sid,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "audit_id": self.audit_id,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }

    def _compute_fingerprint(self) -> str:
        encoded = json.dumps(
            self._fingerprint_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", "surrogatepass")
        return hashlib.sha256(encoded).hexdigest()

    def assert_matches(
        self,
        *,
        current_process_sid: Any | None = None,
        token_sid_reader: Any | None = None,
        authenticated_user_id: Any | None = None,
        now: Any | None = None,
    ) -> "NativeEmployeeExecutionCapability":
        """Validate expiry and optional live identity values.

        Process/user readers live in the service layer.  Keeping this method
        value-oriented makes it easy for a trusted service to revalidate
        without coupling this security module to the request/auth modules.
        """

        if self._authority_token is not _CAPABILITY_TOKEN:
            raise NativeEmployeeExecutionViolation("invalid native execution capability")
        if self.fingerprint != self._compute_fingerprint():
            raise NativeEmployeeExecutionViolation(
                "native execution capability fingerprint is invalid"
            )
        if self.expired(now):
            raise NativeEmployeeExecutionViolation("native execution capability is expired")
        if current_process_sid is None and token_sid_reader is not None:
            if not callable(token_sid_reader):
                raise NativeEmployeeExecutionViolation(
                    "token_sid_reader must be callable"
                )
            try:
                current_process_sid = token_sid_reader()
            except Exception as exc:
                raise NativeEmployeeExecutionViolation(
                    "current Windows process token SID could not be read"
                ) from exc
        elif current_process_sid is None and token_sid_reader is None:
            # Keep the security helper useful to direct executor callers while
            # avoiding a module import cycle at import time.  The service
            # reader performs the native-Windows/non-Windows gate and direct
            # token API lookup.
            try:
                from ..services.native_employee_execution_service import (
                    read_current_windows_process_token_sid,
                )

                current_process_sid = read_current_windows_process_token_sid()
            except NativeEmployeeExecutionError:
                raise
            except Exception as exc:
                raise NativeEmployeeExecutionViolation(
                    "current Windows process token SID could not be read"
                ) from exc
        if current_process_sid is not None:
            try:
                process_sid = normalise_windows_sid(current_process_sid)
            except NativeEmployeeExecutionError as exc:
                raise NativeEmployeeExecutionViolation(
                    "current Windows process SID is invalid"
                ) from exc
            if process_sid != self.windows_sid:
                raise NativeEmployeeExecutionViolation(
                    "current Windows process SID does not match the capability"
                )
        if authenticated_user_id is not None:
            try:
                active_user = _normalise_uuid(
                    authenticated_user_id,
                    label="authenticated_user_id",
                )
            except NativeEmployeeExecutionError as exc:
                raise NativeEmployeeExecutionViolation(
                    "authenticated user identity is invalid"
                ) from exc
            if active_user != self.user_id:
                raise NativeEmployeeExecutionViolation(
                    "authenticated user does not match the capability"
                )
        return self

    # Common naming used by integrations/review tooling.
    validate = assert_matches
    assert_valid = assert_matches

    def to_dict(self) -> dict[str, Any]:
        """Return non-secret audit metadata.

        The opaque authority marker is intentionally absent.  In particular,
        this method never serialises a bearer token, proof object, or process
        handle.
        """

        return {
            "user_id": str(self.user_id),
            "endpoint_id": self.endpoint_id,
            "windows_sid": self.windows_sid,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "audit_id": self.audit_id,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "fingerprint": self.fingerprint,
        }

    as_dict = to_dict

    @classmethod
    def from_dict(cls, value: Any) -> "NativeEmployeeExecutionCapability":
        """Reject untrusted JSON/dict payloads rather than promoting them."""

        raise NativeEmployeeExecutionConfigurationError(
            "native employee execution capability must be issued by the server; dictionaries are untrusted"
        )


_current_native_employee_capability: ContextVar[
    NativeEmployeeExecutionCapability | None
] = ContextVar(
    "aoi_native_employee_execution_capability",
    default=None,
)


def get_current_native_employee_execution_capability() -> NativeEmployeeExecutionCapability | None:
    """Return the capability bound to the current request/task, if any."""

    return _current_native_employee_capability.get()


def require_current_native_employee_execution_capability() -> NativeEmployeeExecutionCapability:
    capability = get_current_native_employee_execution_capability()
    if capability is None:
        raise NativeEmployeeExecutionViolation(
            "no native employee execution capability is bound"
        )
    return capability


def bind_native_employee_execution_capability(
    capability: NativeEmployeeExecutionCapability | None,
) -> Token[NativeEmployeeExecutionCapability | None]:
    if capability is not None:
        if not isinstance(capability, NativeEmployeeExecutionCapability):
            raise TypeError(
                "capability must be a NativeEmployeeExecutionCapability or None"
            )
        if capability._authority_token is not _CAPABILITY_TOKEN:
            raise NativeEmployeeExecutionViolation("invalid native execution capability")
    return _current_native_employee_capability.set(capability)


def reset_native_employee_execution_capability(
    token: Token[NativeEmployeeExecutionCapability | None],
) -> None:
    _current_native_employee_capability.reset(token)


@contextmanager
def native_employee_execution_context(
    capability: NativeEmployeeExecutionCapability,
    *,
    now: Any | None = None,
) -> Iterator[NativeEmployeeExecutionCapability]:
    """Bind a server-issued capability for one synchronous/async task scope."""

    if not isinstance(capability, NativeEmployeeExecutionCapability):
        raise TypeError("capability must be a NativeEmployeeExecutionCapability")
    # The dispatch service performs the live process-token/user revalidation
    # before entering this context.  Re-check the immutable values here while
    # avoiding a second platform-specific token lookup (which would make a
    # bare context manager unusable in deterministic unit tests).
    capability.assert_matches(
        current_process_sid=capability.windows_sid,
        authenticated_user_id=capability.user_id,
        now=now,
    )
    token = bind_native_employee_execution_capability(capability)
    try:
        yield capability
    finally:
        reset_native_employee_execution_capability(token)


def validate_native_employee_execution_capability(
    capability: NativeEmployeeExecutionCapability,
    *,
    current_process_sid: Any | None = None,
    token_sid_reader: Any | None = None,
    authenticated_user_id: Any | None = None,
    now: Any | None = None,
) -> NativeEmployeeExecutionCapability:
    """Validate one capability against optionally supplied live identities."""

    if not isinstance(capability, NativeEmployeeExecutionCapability):
        raise NativeEmployeeExecutionViolation("invalid native execution capability")
    return capability.assert_matches(
        current_process_sid=current_process_sid,
        token_sid_reader=token_sid_reader,
        authenticated_user_id=authenticated_user_id,
        now=now,
    )


def is_native_employee_execution_capability(value: Any) -> bool:
    """Return whether *value* is an issued, non-expired capability."""

    try:
        validate_native_employee_execution_capability(value)
    except (NativeEmployeeExecutionError, TypeError):
        return False
    return True


# Context naming aliases used by dispatch/executor integrations.
current_native_employee_execution_context = native_employee_execution_context
get_current_native_employee_capability = get_current_native_employee_execution_capability
require_current_native_employee_capability = require_current_native_employee_execution_capability
bind_native_employee_capability = bind_native_employee_execution_capability
reset_native_employee_capability = reset_native_employee_execution_capability


__all__ = [
    "NativeEmployeeExecutionCapability",
    "NativeEmployeeExecutionConfigurationError",
    "NativeEmployeeExecutionError",
    "NativeEmployeeExecutionViolation",
    "NativeEmployeeCapabilityError",
    "NativeEmployeeCapabilityViolation",
    "bind_native_employee_capability",
    "bind_native_employee_execution_capability",
    "current_native_employee_execution_context",
    "get_current_native_employee_capability",
    "get_current_native_employee_execution_capability",
    "is_native_employee_execution_capability",
    "native_employee_execution_context",
    "normalise_windows_sid",
    "require_current_native_employee_capability",
    "require_current_native_employee_execution_capability",
    "reset_native_employee_capability",
    "reset_native_employee_execution_capability",
    "validate_native_employee_execution_capability",
]
