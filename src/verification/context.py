"""Request-local verification provenance context.

Verification writers must obtain their run identity from this context rather
than trusting a request/client supplied metadata object.  The context is
intentionally task-local (``ContextVar``) so concurrent verification runs do
not cross-contaminate one another.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator
from uuid import UUID


VERIFICATION_RUN_HEADER = "X-AoiTalk-Verification-Run-Id"
VERIFICATION_HARNESS_HEADER = "X-AoiTalk-Verification-Harness"
VERIFICATION_SOURCE_HEADER = "X-AoiTalk-Verification-Source"
VERIFICATION_DISPOSABLE_HEADER = "X-AoiTalk-Verification-Disposable"
VERIFICATION_SIGNATURE_HEADER = "X-AoiTalk-Verification-Signature"
VERIFICATION_HARNESS_KEY_ENV = "AOITALK_VERIFICATION_HARNESS_KEY"
_VERIFICATION_HEADER_PREFIX = "x-aoitalk-verification-"


@dataclass(frozen=True, slots=True)
class VerificationRunContext:
    """Trusted, request-local identity for one disposable verification run.

    The context is created by server-side harness code after a durable
    ``VerificationRun`` is opened.  Client payloads are never converted into
    this type by the verification provenance service.
    """

    run_id: UUID
    source: str
    harness: str | None = None
    disposable: bool = True
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, UUID):
            try:
                object.__setattr__(self, "run_id", UUID(str(self.run_id)))
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValueError("verification run_id must be a UUID") from exc
        # Header/context attribution is a strict wire contract.  Do not
        # coerce arbitrary objects (for example mappings) into text because
        # their string representation could smuggle untrusted identity data
        # into the signed payload or durable ledger.
        if not isinstance(self.source, str):
            raise ValueError("verification source must be a string")
        source = self.source.strip()
        if not source:
            raise ValueError("verification source is required")
        if len(source) > 255:
            raise ValueError("verification source is too long")
        if any(ord(char) < 32 or ord(char) == 127 for char in source):
            raise ValueError("verification source contains control characters")
        if self.harness is not None and not isinstance(self.harness, str):
            raise ValueError("verification harness must be a string")
        harness = (self.harness if self.harness is not None else source).strip()
        if not harness:
            raise ValueError("verification harness is required")
        if len(harness) > 255:
            raise ValueError("verification harness is too long")
        if any(ord(char) < 32 or ord(char) == 127 for char in harness):
            raise ValueError("verification harness contains control characters")
        if self.disposable is not True:
            raise ValueError("verification context must be disposable")
        if self.created_at is not None and not isinstance(self.created_at, datetime):
            raise ValueError("verification context created_at must be a datetime")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "harness", harness)
        object.__setattr__(self, "created_at", self.created_at or datetime.utcnow())


_current_verification_context: ContextVar[VerificationRunContext | None] = ContextVar(
    "aoitalk_current_verification_context",
    default=None,
)


def set_current_verification_context(
    context: VerificationRunContext,
) -> Token[VerificationRunContext | None]:
    """Bind a server-created verification context to the current task."""

    if not isinstance(context, VerificationRunContext):
        raise TypeError("a VerificationRunContext is required")
    return _current_verification_context.set(context)


def set_current_verification_run(
    run_id: UUID | str,
    *,
    source: str,
    harness: str | None = None,
    created_at: datetime | None = None,
) -> Token[VerificationRunContext | None]:
    """Convenience binder used by server-side harnesses."""

    return set_current_verification_context(
        VerificationRunContext(
            run_id=UUID(str(run_id)),
            source=source,
            harness=harness,
            created_at=created_at,
        )
    )


# Explicitly named aliases make migration from other request-context helpers
# straightforward while keeping one ContextVar as the authority.
set_current_verification_run_id = set_current_verification_run


def reset_current_verification_context(
    token: Token[VerificationRunContext | None],
) -> None:
    _current_verification_context.reset(token)


reset_current_verification_run = reset_current_verification_context
reset_current_verification_run_id = reset_current_verification_context


def get_current_verification_context() -> VerificationRunContext | None:
    return _current_verification_context.get()


def get_current_verification_run_id() -> str | None:
    context = get_current_verification_context()
    return str(context.run_id) if context is not None else None


def require_current_verification_context() -> VerificationRunContext:
    """Return the bound context or fail closed for an unscoped write."""

    context = get_current_verification_context()
    if context is None:
        raise RuntimeError(
            "verification provenance requires a server-owned run context"
        )
    if context.disposable is not True:
        raise RuntimeError("verification context is not disposable")
    return context


def context_from_run(run: object) -> VerificationRunContext:
    """Build a context from a server-loaded ``VerificationRun`` row.

    This helper deliberately reads only the durable run identity and
    attribution fields.  It is not a parser for request headers or arbitrary
    client metadata.
    """

    return VerificationRunContext(
        run_id=UUID(str(getattr(run, "run_id"))),
        source=str(getattr(run, "source") or ""),
        harness=getattr(run, "harness", None),
        disposable=getattr(run, "disposable", True),
        created_at=getattr(run, "created_at", None),
    )


def _header_value(headers: object, name: str) -> str | None:
    """Read a case-insensitive header mapping without trusting extra fields."""

    if not hasattr(headers, "items"):
        raise ValueError("verification headers must be a mapping")
    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == wanted:
            if not isinstance(value, str):
                raise ValueError(f"verification header {name} must be a string")
            return value.strip()
    return None


def _harness_key(secret: str | None = None) -> bytes:
    value = str(secret if secret is not None else os.getenv(VERIFICATION_HARNESS_KEY_ENV, "")).strip()
    if len(value) < 16:
        raise ValueError("verification harness signing key is not configured")
    return value.encode("utf-8")


def _signed_payload(run_id: UUID | str, harness: str, source: str) -> bytes:
    return f"{UUID(str(run_id))}\n{harness}\n{source}\ntrue".encode("utf-8")


def build_verification_headers(
    run_id: UUID | str,
    *,
    harness: str,
    source: str,
    secret: str | None = None,
) -> dict[str, str]:
    """Build a signed server-to-server verification context header set."""

    context = VerificationRunContext(
        run_id=UUID(str(run_id)),
        source=source,
        harness=harness,
    )
    signature = hmac.new(
        _harness_key(secret),
        _signed_payload(context.run_id, context.harness or context.source, context.source),
        hashlib.sha256,
    ).hexdigest()
    return {
        VERIFICATION_RUN_HEADER: str(context.run_id),
        VERIFICATION_HARNESS_HEADER: context.harness or context.source,
        VERIFICATION_SOURCE_HEADER: context.source,
        VERIFICATION_DISPOSABLE_HEADER: "true",
        VERIFICATION_SIGNATURE_HEADER: signature,
    }


def verify_verification_headers(
    headers: object,
    *,
    secret: str | None = None,
) -> VerificationRunContext | None:
    """Verify signed verification headers, failing closed on partial input.

    A request with no verification headers returns ``None``.  Once any one of
    the reserved headers is present, all five values and a valid HMAC are
    mandatory; unsigned or client-forged run IDs cannot establish context.
    """

    names = (
        VERIFICATION_RUN_HEADER,
        VERIFICATION_HARNESS_HEADER,
        VERIFICATION_SOURCE_HEADER,
        VERIFICATION_DISPOSABLE_HEADER,
        VERIFICATION_SIGNATURE_HEADER,
    )
    # Do not silently ignore a typo/extension in the reserved namespace.  A
    # caller that intends to establish verification provenance must send the
    # complete, known contract; malformed reserved fields fail closed.
    known = {name.casefold() for name in names}
    try:
        reserved_unknown = []
        seen_reserved: dict[str, str] = {}
        for key, raw_value in headers.items():
            normalized_key = str(key).casefold()
            if not normalized_key.startswith(_VERIFICATION_HEADER_PREFIX):
                continue
            if normalized_key not in known:
                reserved_unknown.append(str(key))
                continue
            if not isinstance(raw_value, str):
                raise ValueError("verification header values must be strings")
            normalized_value = raw_value.strip()
            prior = seen_reserved.get(normalized_key)
            if prior is not None and prior != normalized_value:
                raise ValueError("duplicate verification provenance header")
            seen_reserved[normalized_key] = normalized_value
    except AttributeError as exc:
        raise ValueError("verification headers must be a mapping") from exc
    if reserved_unknown:
        raise ValueError("unknown verification provenance header")
    values = {name: _header_value(headers, name) for name in names}
    if not any(value is not None for value in values.values()):
        return None
    if any(not value for value in values.values()):
        raise ValueError("verification headers are incomplete")
    # Keep the wire contract literal.  Marker validation intentionally
    # requires the boolean ``True`` and the signed payload is fixed to the
    # lower-case token ``true``; accepting ``TRUE`` here would make two
    # representations share one signature and weaken strict malformed-input
    # rejection.
    disposable = values[VERIFICATION_DISPOSABLE_HEADER]
    if disposable != "true":
        raise ValueError("verification disposable header must be true")
    try:
        run_id = UUID(values[VERIFICATION_RUN_HEADER])
    except (TypeError, ValueError) as exc:
        raise ValueError("verification run header must be a UUID") from exc
    context = VerificationRunContext(
        run_id=run_id,
        source=values[VERIFICATION_SOURCE_HEADER],
        harness=values[VERIFICATION_HARNESS_HEADER],
    )
    expected = hmac.new(
        _harness_key(secret),
        _signed_payload(context.run_id, context.harness or context.source, context.source),
        hashlib.sha256,
    ).hexdigest()
    supplied = values[VERIFICATION_SIGNATURE_HEADER].casefold()
    if not hmac.compare_digest(expected, supplied):
        raise ValueError("invalid verification header signature")
    return context


# Explicit name for HTTP adapters that load trusted server-to-server context.
context_from_headers = verify_verification_headers


@contextmanager
def signed_verification_context(
    headers: object,
    *,
    secret: str | None = None,
) -> Iterator[VerificationRunContext | None]:
    """Verify and bind a signed header context for one synchronous block."""

    context = verify_verification_headers(headers, secret=secret)
    if context is None:
        yield None
        return
    token = set_current_verification_context(context)
    try:
        yield context
    finally:
        reset_current_verification_context(token)


@contextmanager
def verification_run_scope(
    run_id: UUID | str,
    *,
    source: str,
    harness: str | None = None,
    created_at: datetime | None = None,
) -> Iterator[VerificationRunContext]:
    """Bind a run for a synchronous block and always restore the parent."""

    context = VerificationRunContext(
        run_id=UUID(str(run_id)),
        source=source,
        harness=harness,
        created_at=created_at,
    )
    token = set_current_verification_context(context)
    try:
        yield context
    finally:
        reset_current_verification_context(token)


# Friendly aliases used by fixtures and harness adapters.
verification_context = verification_run_scope
use_verification_run = verification_run_scope
verification_run_context = verification_run_scope


__all__ = [
    "VerificationRunContext",
    "get_current_verification_context",
    "get_current_verification_run_id",
    "context_from_run",
    "context_from_headers",
    "build_verification_headers",
    "require_current_verification_context",
    "reset_current_verification_context",
    "reset_current_verification_run",
    "reset_current_verification_run_id",
    "set_current_verification_context",
    "set_current_verification_run",
    "set_current_verification_run_id",
    "signed_verification_context",
    "verify_verification_headers",
    "VERIFICATION_DISPOSABLE_HEADER",
    "VERIFICATION_HARNESS_HEADER",
    "VERIFICATION_HARNESS_KEY_ENV",
    "VERIFICATION_RUN_HEADER",
    "VERIFICATION_SIGNATURE_HEADER",
    "VERIFICATION_SOURCE_HEADER",
    "verification_run_scope",
    "verification_run_context",
    "verification_context",
    "use_verification_run",
]
