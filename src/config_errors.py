"""Safe, typed failures raised while loading character configuration.

Character configuration is stored in PostgreSQL in Enterprise deployments.  A
missing row is a normal ``not found`` result, but a database outage must not be
silently converted into the same result.  This module keeps the distinction at
the configuration boundary and gives callers a stable, secret-free error
payload.  The original exception is intentionally not included in ``str`` or
``repr``; callers can use the category and trace ID without exposing a DSN,
password, API key, or bearer token.
"""

from __future__ import annotations

import asyncio
import contextvars
import errno
import hashlib
import os
import re
import uuid
from enum import Enum
from typing import Any, Iterator, Mapping


class CharacterLookupErrorCategory(str, Enum):
    """Machine-readable categories for character database failures."""

    DATABASE_UNAVAILABLE = "database_unavailable"
    DATABASE_AUTHENTICATION_FAILURE = "database_authentication_failure"
    DATABASE_TIMEOUT = "database_timeout"
    DATABASE_PERMISSION_DENIED = "database_permission_denied"
    DATABASE_SCHEMA_MISMATCH = "database_schema_mismatch"
    DATABASE_POOL_UNAVAILABLE = "database_pool_unavailable"
    DATABASE_ERROR = "database_error"


# Database failures that are usually recoverable after the backing service is
# restored (or after a pool connection is returned) are surfaced as 503.  A
# schema/permission/configuration defect is a server-side 500 instead.  Keep
# this mapping in the shared error module so every API boundary applies the
# same contract.
_SERVICE_UNAVAILABLE_CATEGORIES = frozenset(
    {
        CharacterLookupErrorCategory.DATABASE_UNAVAILABLE.value,
        CharacterLookupErrorCategory.DATABASE_AUTHENTICATION_FAILURE.value,
        CharacterLookupErrorCategory.DATABASE_TIMEOUT.value,
        CharacterLookupErrorCategory.DATABASE_POOL_UNAVAILABLE.value,
    }
)


_current_character_lookup_request_id: contextvars.ContextVar[str | None] = (
    contextvars.ContextVar("character_lookup_request_id", default=None)
)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|dsn)\b\s*[=:]\s*)([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)([^\s,;]+)")
_URL_CREDENTIAL_RE = re.compile(
    r"(?i)(\b(?:postgres(?:ql)?|mysql|mariadb|redis)(?:\+[A-Za-z0-9_.-]+)?://[^\s/@:]+:)([^\s/@]+)(@)"
)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _safe_identifier(value: object | None) -> str | None:
    """Return a log/API-safe correlation identifier or ``None``."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _SAFE_ID_RE.fullmatch(text):
        return text
    # Do not echo malformed/user-controlled IDs.  A short digest preserves
    # correlation usefulness without allowing log injection or secret leakage.
    return f"id-{hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:20]}"


def set_character_lookup_request_id(request_id: object | None) -> contextvars.Token:
    """Set a request ID for subsequent character lookups in this context."""

    return _current_character_lookup_request_id.set(_safe_identifier(request_id))


def reset_character_lookup_request_id(token: contextvars.Token) -> None:
    """Restore the previous character lookup request ID."""

    _current_character_lookup_request_id.reset(token)


def get_character_lookup_request_id() -> str | None:
    """Return the current request ID, if the caller installed one."""

    return _current_character_lookup_request_id.get()


def redact_database_exception_detail(value: object | None, *, limit: int = 512) -> str:
    """Redact credential-like values from a diagnostic exception detail.

    The returned value is for logs only and is deliberately bounded.  Typed
    ``CharacterLookupError`` messages never include this detail.
    """

    if value is None:
        return ""
    text = str(value)
    text = _URL_CREDENTIAL_RE.sub(r"\1<redacted>\3", text)
    text = _SECRET_ASSIGNMENT_RE.sub(r"\1<redacted>", text)
    text = _BEARER_RE.sub(r"\1<redacted>", text)
    text = _CONTROL_CHAR_RE.sub(" ", text)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: max(0, limit - 3)] + "..."
    return text


class CharacterNotFoundError(FileNotFoundError):
    """A true character-row miss (the only normal negative lookup result)."""

    category = "not_found"
    code = "character_not_found"

    def __init__(self, character_name: object):
        self.character_name = str(character_name or "").strip()
        super().__init__(f"Character configuration not found: {self.character_name}")


class CharacterLookupError(RuntimeError):
    """Secret-free typed failure for a character database lookup.

    ``category``, ``trace_id`` and optional ``request_id`` are safe to return
    from an API/diagnostic endpoint.  ``detail`` is redacted and bounded for
    logs; it is not part of the exception message exposed to callers.
    """

    def __init__(
        self,
        category: CharacterLookupErrorCategory | str,
        *,
        trace_id: object | None = None,
        request_id: object | None = None,
        detail: object | None = None,
        original_type: object | None = None,
    ) -> None:
        self.category = _normalize_category(category)
        # ``kind``/``code`` aliases make the contract convenient for existing
        # API error serializers that use either naming convention.
        self.kind = self.category
        self.code = self.category
        self.trace_id = _safe_identifier(trace_id) or uuid.uuid4().hex
        self.request_id = _safe_identifier(request_id) or get_character_lookup_request_id()
        self.detail = redact_database_exception_detail(detail)
        self.original_type = _safe_identifier(original_type)
        super().__init__(self.safe_message)

    @property
    def safe_message(self) -> str:
        message = (
            "Character configuration lookup failed: "
            f"category={self.category}; trace_id={self.trace_id}"
        )
        if self.request_id:
            message += f"; request_id={self.request_id}"
        return message

    @property
    def user_message(self) -> str:
        """Alias used by API layers that distinguish user/technical messages."""

        return self.safe_message

    def to_dict(self) -> dict[str, str]:
        """Return an API-safe representation without technical detail."""

        result = {
            "category": self.category,
            "code": self.code,
            "trace_id": self.trace_id,
        }
        if self.request_id:
            result["request_id"] = self.request_id
        return result


def character_lookup_http_status(error: CharacterLookupError) -> int:
    """Return the safe HTTP status for a typed character lookup failure."""

    return 503 if error.category in _SERVICE_UNAVAILABLE_CATEGORIES else 500


def character_lookup_http_detail(error: CharacterLookupError) -> dict[str, str]:
    """Return a safe API payload (never the redacted technical detail)."""

    payload = error.to_dict()
    payload["message"] = error.safe_message
    return payload


def add_character_lookup_context(
    exc: BaseException,
    *,
    trace_id: object | None = None,
    request_id: object | None = None,
) -> CharacterLookupError:
    """Ensure a typed lookup error has request/trace correlation IDs.

    Existing typed errors retain their original trace ID and technical detail;
    a request ID supplied by an HTTP boundary is added only when absent.  Raw
    exceptions are classified and wrapped through :func:`build_character_lookup_error`.
    """

    if not isinstance(exc, CharacterLookupError):
        return build_character_lookup_error(
            exc,
            trace_id=trace_id,
            request_id=request_id,
        )
    # A typed error already owns the trace ID generated at the database
    # boundary; an HTTP header must not replace it.  Only add a missing
    # request ID so the original failure remains traceable end-to-end.
    if exc.request_id or request_id is None:
        return exc
    return CharacterLookupError(
        exc.category,
        trace_id=exc.trace_id,
        request_id=request_id,
        detail=exc.detail,
        original_type=exc.original_type,
    )


# Compatibility aliases for callers that prefer a database-specific name.
CharacterDatabaseError = CharacterLookupError
DatabaseCharacterLookupError = CharacterLookupError


def _normalize_category(category: CharacterLookupErrorCategory | str) -> str:
    if isinstance(category, CharacterLookupErrorCategory):
        return category.value
    value = str(category or "").strip().lower()
    return value or CharacterLookupErrorCategory.DATABASE_ERROR.value


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield an exception and its DBAPI/SQLAlchemy cause chain once each."""

    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop(0)
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        yield current
        for attribute in ("orig", "__cause__", "__context__"):
            nested = getattr(current, attribute, None)
            if isinstance(nested, BaseException) and id(nested) not in seen:
                pending.append(nested)


def _exception_name(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__name__}".lower()


def _exception_text(exc: BaseException) -> str:
    # ``repr`` may include a DSN in some DBAPI implementations; ``str`` is
    # enough for classification and is redacted separately for diagnostics.
    return redact_database_exception_detail(str(exc), limit=1024).lower()


def classify_character_lookup_exception(exc: BaseException) -> CharacterLookupErrorCategory:
    """Classify DB/network failures without exposing their raw text."""

    chain = list(_exception_chain(exc))
    names = {_exception_name(item) for item in chain}
    text = " ".join(_exception_text(item) for item in chain)

    # SQLAlchemy's pool timeout is distinct from a query/network timeout.
    if any(
        name in {"sqlalchemy.exc.timeouterror", "sqlalchemy.exc.queuepooltimeout"}
        or any(
            marker in name
            for marker in (
                "pooltimeout",
                "poolclosed",
                "poolexhausted",
                "toomanyconnections",
            )
        )
        or any(
            marker in text
            for marker in (
                "pool timeout",
                "queuepool limit",
                "pool exhausted",
                "pool is closed",
                "too many connections",
                "remaining connection slots are reserved",
            )
        )
        for name in names
    ):
        return CharacterLookupErrorCategory.DATABASE_POOL_UNAVAILABLE

    if any(
        isinstance(item, (asyncio.TimeoutError, TimeoutError))
        or "timeout" in _exception_name(item)
        or "timed out" in _exception_text(item)
        or "statement timeout" in _exception_text(item)
        for item in chain
    ):
        return CharacterLookupErrorCategory.DATABASE_TIMEOUT

    if any(
        isinstance(item, PermissionError)
        or getattr(item, "errno", None) in {errno.EACCES, errno.EPERM}
        or any(
            marker in _exception_name(item)
            for marker in (
                "insufficientprivilege",
                "permissiondenied",
                "accessdenied",
            )
        )
        or "permission denied" in _exception_text(item)
        or "insufficient privilege" in _exception_text(item)
        for item in chain
    ):
        return CharacterLookupErrorCategory.DATABASE_PERMISSION_DENIED

    if any(
        any(
            marker in _exception_name(item)
            for marker in (
                "invalidpassword",
                "invalidauthorizationspecification",
                "authenticationerror",
                "authenticationfailed",
            )
        )
        or any(
            marker in _exception_text(item)
            for marker in (
                "password authentication failed",
                "authentication failed",
                "invalid password",
                "no password supplied",
                "authentication method",
            )
        )
        for item in chain
    ):
        return CharacterLookupErrorCategory.DATABASE_AUTHENTICATION_FAILURE

    if any(
        isinstance(item, ConnectionRefusedError)
        or isinstance(item, (ConnectionError, BrokenPipeError))
        # A missing PostgreSQL Unix-domain socket is surfaced by asyncpg as a
        # FileNotFoundError.  At this boundary it is a database-unavailable
        # condition, never a missing character row.
        or isinstance(item, FileNotFoundError)
        or getattr(item, "errno", None) in {
            errno.ECONNREFUSED,
            errno.ECONNRESET,
            errno.ECONNABORTED,
            errno.ENETUNREACH,
            errno.EHOSTUNREACH,
            errno.ENOENT,
        }
        or any(
            marker in _exception_name(item)
            for marker in (
                "cannotconnectnow",
                "connectiondoesnotexist",
                "connectionfailure",
                "postgresconnectionerror",
                "disconnectionerror",
                "interfaceerror",
            )
        )
        or any(
            marker in _exception_text(item)
            for marker in (
                "connection refused",
                "could not connect",
                "connection reset",
                "connection is closed",
                "server is not accepting connections",
                "connection failure",
            )
        )
        for item in chain
    ):
        return CharacterLookupErrorCategory.DATABASE_UNAVAILABLE

    if any(
        any(
            marker in _exception_name(item)
            for marker in (
                "undefinedtable",
                "undefinedcolumn",
                "undefinedobject",
                "nosuchtable",
                "nosuchcolumn",
                "programmingerror",
            )
        )
        or any(
            marker in _exception_text(item)
            for marker in (
                "undefined table",
                "undefined column",
                "relation ",
                "table ",
                "column ",
                "does not exist",
                "schema mismatch",
                "schema version",
                "migration",
                "alembic",
                "revision",
            )
        )
        for item in chain
    ):
        return CharacterLookupErrorCategory.DATABASE_SCHEMA_MISMATCH

    # A SQLAlchemy/asyncpg/psycopg exception that did not match a more precise
    # category is still a database failure and must never become not-found.
    if any(
        _exception_name(item).startswith(
            ("sqlalchemy.", "asyncpg.", "psycopg.", "psycopg2.")
        )
        or any(
            marker in _exception_name(item)
            for marker in ("dbapierror", "operationalerror", "databaseerror")
        )
        for item in chain
    ):
        return CharacterLookupErrorCategory.DATABASE_ERROR

    # Generic RuntimeError/OSError values from the database bootstrap path
    # still need a typed category.  Preserve the broad contract rather than
    # returning ``None`` and creating a false not-found result.
    return CharacterLookupErrorCategory.DATABASE_ERROR


def build_character_lookup_error(
    exc: BaseException,
    *,
    trace_id: object | None = None,
    request_id: object | None = None,
) -> CharacterLookupError:
    """Wrap one raw DB/DBAPI exception in a stable, secret-free error."""

    if isinstance(exc, CharacterLookupError):
        return exc
    category = classify_character_lookup_exception(exc)
    return CharacterLookupError(
        category,
        trace_id=trace_id,
        request_id=request_id,
        detail=str(exc),
        original_type=type(exc).__name__,
    )


__all__ = [
    "add_character_lookup_context",
    "CharacterDatabaseError",
    "CharacterLookupError",
    "CharacterLookupErrorCategory",
    "CharacterNotFoundError",
    "DatabaseCharacterLookupError",
    "build_character_lookup_error",
    "classify_character_lookup_exception",
    "character_lookup_http_detail",
    "character_lookup_http_status",
    "get_character_lookup_request_id",
    "redact_database_exception_detail",
    "reset_character_lookup_request_id",
    "set_character_lookup_request_id",
]
