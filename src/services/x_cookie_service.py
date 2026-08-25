"""Per-user X cookie validation, storage and resolution.

The service deliberately keeps the raw Netscape export in memory only.  The
database stores a small canonical object (``auth_token``, ``ct0`` and their
expiry timestamps) encrypted with an AAD that contains the owning user id.
No public helper in this module serializes the decrypted payload.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import User, UserXCookieCredential
from ..security.field_crypto import (
    decrypt_json_value_if_needed,
    encrypt_json_value,
    is_encrypted_value,
)


X_COOKIE_MAX_BYTES = 2 * 1024 * 1024
_NETSCAPE_HEADER = "# netscape http cookie file"
_X_DOMAINS = ("x.com", "twitter.com")
_REQUIRED_NAMES = ("auth_token", "ct0")


class XCookieValidationError(ValueError):
    """Safe, machine-readable validation failure.

    ``message`` is intentionally a fixed Japanese action, never an input,
    path, header or exception detail.
    """

    def __init__(self, status: str, message: str):
        self.status = status
        self.code = status
        super().__init__(message)


@dataclass(frozen=True)
class XCookieParseResult:
    status: str
    cookies: dict[str, str] = field(default_factory=dict)
    expires: dict[str, int | None] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return self.status == "available" and bool(self.cookies)

    def canonical_payload(self) -> dict[str, Any]:
        # Only these two names and their expiry values are persisted.
        return {
            "auth_token": self.cookies.get("auth_token", ""),
            "ct0": self.cookies.get("ct0", ""),
            "expires": {
                name: self.expires.get(name)
                for name in _REQUIRED_NAMES
            },
        }


@dataclass(frozen=True)
class XCookieResolution:
    """Request-scoped resolution with safe metadata and private cookies."""

    status: str
    source: str
    scope: str
    cookies: dict[str, str] = field(default_factory=dict)
    expires: dict[str, int | None] = field(default_factory=dict)
    updated_at: datetime | None = None
    suppress_global: bool = False

    @property
    def configured(self) -> bool:
        return self.status == "available" and bool(self.cookies)

    def safe_status(self) -> dict[str, Any]:
        return {
            "service": "x",
            "status": self.status,
            "configured": self.configured,
            "source": self.source,
            "scope": self.scope,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


def _safe_message(status: str) -> str:
    return {
        "invalid_format": "Cookieファイルの形式を確認してください（Netscape形式、UTF-8、7項目のタブ区切り）。",
        "missing_required_cookie": "auth_token と ct0 のCookieを含めてください。",
        "expired": "auth_token または ct0 の有効期限が切れています。",
        "too_large": "Cookieファイルが大きすぎます。上限以内のファイルを指定してください。",
    }.get(status, "X Cookieを確認してください。")


def _domain_is_x(domain: str) -> bool:
    normalized = domain.casefold().strip().lstrip(".")
    return any(normalized == root or normalized.endswith("." + root) for root in _X_DOMAINS)


def parse_x_cookie_bytes(payload: bytes, *, now: int | None = None) -> XCookieParseResult:
    """Parse a Netscape cookie export without touching the filesystem."""

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise XCookieValidationError("invalid_format", _safe_message("invalid_format"))
    raw = bytes(payload)
    if len(raw) > X_COOKIE_MAX_BYTES:
        raise XCookieValidationError("invalid_format", _safe_message("too_large"))
    if b"\x00" in raw:
        raise XCookieValidationError("invalid_format", _safe_message("invalid_format"))
    try:
        text_value = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Do not retain the decoder exception as ``__cause__``: its repr can
        # include a bytes fragment containing an uploaded secret.
        raise XCookieValidationError(
            "invalid_format", _safe_message("invalid_format")
        ) from None
    if not text_value.strip():
        raise XCookieValidationError("invalid_format", _safe_message("invalid_format"))

    lines = text_value.splitlines()
    header_seen = False
    cookies: dict[str, str] = {}
    expires: dict[str, int | None] = {}
    expired_names: set[str] = set()
    now_value = int(time.time() if now is None else now)

    for raw_line in lines:
        line = raw_line.strip("\r")
        if not line.strip():
            continue
        lowered = line.casefold().strip()
        if lowered.startswith(_NETSCAPE_HEADER):
            header_seen = True
            continue
        # Netscape's HttpOnly extension prefixes the domain with this marker;
        # it is not a comment and must be parsed as a normal cookie line.
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            raise XCookieValidationError("invalid_format", _safe_message("invalid_format"))
        domain, include_subdomains, path, secure, expiry, name, value = fields
        if include_subdomains.upper() not in {"TRUE", "FALSE"}:
            raise XCookieValidationError("invalid_format", _safe_message("invalid_format"))
        if not path.startswith("/") or "\x00" in path:
            raise XCookieValidationError("invalid_format", _safe_message("invalid_format"))
        if secure.upper() not in {"TRUE", "FALSE"}:
            raise XCookieValidationError("invalid_format", _safe_message("invalid_format"))
        if expiry == "":
            # MozillaCookieJar exports session cookies with an empty expiry
            # field in addition to the Netscape ``0`` convention.
            expiry_value = None
        else:
            if not expiry.isdigit():
                raise XCookieValidationError("invalid_format", _safe_message("invalid_format"))
            expiry_value = int(expiry)
            expiry_value = None if expiry_value == 0 else expiry_value
        if not name or "\x00" in name or "\x00" in value:
            raise XCookieValidationError("invalid_format", _safe_message("invalid_format"))
        if name not in _REQUIRED_NAMES or not _domain_is_x(domain):
            continue
        if not value:
            continue
        if expiry_value is not None and expiry_value <= now_value:
            # A full browser export can contain the same required cookie for
            # both x.com and twitter.com.  Identical duplicate values are
            # harmless; keep the first live value and preserve the longer
            # expiry.  Conflicting values remain ambiguous and fail closed.
            if name in cookies:
                continue
            expired_names.add(name)
            expires[name] = expiry_value
            continue
        if name in cookies:
            if cookies[name] != value:
                raise XCookieValidationError("invalid_format", _safe_message("invalid_format"))
            old_expiry = expires.get(name)
            if old_expiry is None or expiry_value is None:
                expires[name] = old_expiry if old_expiry is None else expiry_value
            else:
                expires[name] = max(old_expiry, expiry_value)
            continue
        cookies[name] = value
        expired_names.discard(name)
        expires[name] = expiry_value

    if not header_seen:
        raise XCookieValidationError("invalid_format", _safe_message("invalid_format"))
    missing = set(_REQUIRED_NAMES) - set(cookies)
    if expired_names:
        return XCookieParseResult("expired", {}, {name: expires.get(name) for name in _REQUIRED_NAMES})
    if missing:
        return XCookieParseResult("missing_required_cookie", {}, {name: expires.get(name) for name in _REQUIRED_NAMES})
    return XCookieParseResult(
        "available",
        {name: cookies[name] for name in _REQUIRED_NAMES},
        {name: expires.get(name) for name in _REQUIRED_NAMES},
    )


def _resolution_from_parse(
    parsed: XCookieParseResult,
    *,
    source: str,
    scope: str,
    updated_at: datetime | None = None,
    suppress_global: bool = False,
) -> XCookieResolution:
    cookies = dict(parsed.cookies) if parsed.status == "available" else {}
    return XCookieResolution(
        status=parsed.status,
        source=source,
        scope=scope,
        cookies=cookies,
        expires=dict(parsed.expires),
        updated_at=updated_at,
        suppress_global=suppress_global,
    )


def load_global_x_cookie() -> XCookieResolution:
    """Load the explicit operator-managed shared fallback safely."""

    configured = str(os.getenv("AOITALK_X_COOKIE_FILE") or "").strip()
    if not configured:
        return XCookieResolution("unconfigured", "none", "none")
    try:
        path = Path(configured).expanduser().resolve(strict=True)
        if not path.is_file():
            return XCookieResolution("unavailable", "server_shared", "server_shared")
        if path.stat().st_size > X_COOKIE_MAX_BYTES:
            return XCookieResolution("invalid_format", "server_shared", "server_shared")
        parsed = parse_x_cookie_bytes(path.read_bytes())
    except XCookieValidationError as exc:
        return XCookieResolution(exc.status, "server_shared", "server_shared")
    except (FileNotFoundError, PermissionError, OSError):
        # A configured path that cannot be read is an unavailable operator
        # fallback.  Never expose the path or OS exception detail.
        return XCookieResolution("unavailable", "server_shared", "server_shared")
    except Exception:
        # Do not expose a configured path or operating-system detail.
        return XCookieResolution("unavailable", "server_shared", "server_shared")
    return _resolution_from_parse(parsed, source="server_shared", scope="server_shared")


def _aad(user_id: UUID) -> str:
    return f"user_x_cookie_credentials.payload:{user_id}"


def _canonical_from_row(row: UserXCookieCredential, user_id: UUID) -> XCookieResolution:
    if bool(row.disabled):
        return XCookieResolution(
            "unconfigured", "personal", "personal", updated_at=row.updated_at, suppress_global=True
        )
    try:
        if not is_encrypted_value(row.encrypted_payload):
            return XCookieResolution(
                "unavailable", "personal", "personal", updated_at=row.updated_at, suppress_global=True
            )
        payload = decrypt_json_value_if_needed(row.encrypted_payload, aad=_aad(user_id))
    except Exception:
        return XCookieResolution(
            "unavailable", "personal", "personal", updated_at=row.updated_at, suppress_global=True
        )
    if not isinstance(payload, dict):
        return XCookieResolution(
            "invalid_format", "personal", "personal", updated_at=row.updated_at, suppress_global=True
        )
    cookies = {name: payload.get(name) for name in _REQUIRED_NAMES}
    expires_payload = payload.get("expires") if isinstance(payload.get("expires"), dict) else {}
    if any(not isinstance(cookies[name], str) or not cookies[name] for name in _REQUIRED_NAMES):
        return XCookieResolution(
            "missing_required_cookie", "personal", "personal", updated_at=row.updated_at, suppress_global=True
        )
    expires: dict[str, int | None] = {}
    for name in _REQUIRED_NAMES:
        value = expires_payload.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            return XCookieResolution(
                "invalid_format", "personal", "personal", updated_at=row.updated_at, suppress_global=True
            )
        expires[name] = value
    if any(value is not None and value <= int(time.time()) for value in expires.values()):
        return XCookieResolution(
            "expired", "personal", "personal", updated_at=row.updated_at, suppress_global=True
        )
    return XCookieResolution(
        "available", "personal", "personal", {str(k): str(v) for k, v in cookies.items()}, expires,
        updated_at=row.updated_at, suppress_global=True,
    )


async def get_personal_x_cookie(
    session: AsyncSession, user_id: UUID
) -> XCookieResolution | None:
    """Return a personal resolution, or ``None`` when no row exists."""

    result = await session.execute(
        select(UserXCookieCredential).where(UserXCookieCredential.user_id == user_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return _canonical_from_row(row, user_id)


async def resolve_x_cookie(
    session: AsyncSession | None, user_id: UUID | None
) -> XCookieResolution:
    """Resolve personal credentials before the shared operator fallback."""

    if session is None or user_id is None:
        return load_global_x_cookie()
    try:
        personal = await get_personal_x_cookie(session, user_id)
    except Exception:
        # A database outage must not accidentally select another credential
        # source for an authenticated request.
        return XCookieResolution("unavailable", "personal", "personal", suppress_global=True)
    if personal is not None:
        return personal
    return load_global_x_cookie()


async def upsert_personal_x_cookie(
    session: AsyncSession, user_id: UUID, parsed: XCookieParseResult
) -> UserXCookieCredential:
    """Atomically replace a valid credential while retaining old data on error."""

    if parsed.status != "available":
        raise XCookieValidationError(parsed.status, _safe_message(parsed.status))
    # Lock the owning user first, then the unique credential row.  This keeps
    # concurrent PUT/DELETE operations serialized even across workers.
    user_result = await session.execute(
        select(User.id).where(User.id == user_id).with_for_update()
    )
    if user_result.scalar_one_or_none() is None:
        raise XCookieValidationError("unavailable", "認証ユーザーを確認できません。")
    row_result = await session.execute(
        select(UserXCookieCredential)
        .where(UserXCookieCredential.user_id == user_id)
        .with_for_update()
    )
    row = row_result.scalar_one_or_none()
    encrypted = encrypt_json_value(parsed.canonical_payload(), aad=_aad(user_id))
    now = datetime.utcnow()
    if row is None:
        row = UserXCookieCredential(
            user_id=user_id,
            encrypted_payload=encrypted,
            disabled=False,
            disabled_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.encrypted_payload = encrypted
        row.disabled = False
        row.disabled_at = None
        row.updated_at = now
    await session.flush()
    return row


async def disable_personal_x_cookie(
    session: AsyncSession, user_id: UUID
) -> UserXCookieCredential:
    """Blank ciphertext and retain a disabled tombstone suppressing fallback."""

    user_result = await session.execute(
        select(User.id).where(User.id == user_id).with_for_update()
    )
    if user_result.scalar_one_or_none() is None:
        raise XCookieValidationError("unavailable", "認証ユーザーを確認できません。")
    row_result = await session.execute(
        select(UserXCookieCredential)
        .where(UserXCookieCredential.user_id == user_id)
        .with_for_update()
    )
    row = row_result.scalar_one_or_none()
    now = datetime.utcnow()
    if row is None:
        row = UserXCookieCredential(
            user_id=user_id,
            encrypted_payload=None,
            disabled=True,
            disabled_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.encrypted_payload = None
        row.disabled = True
        row.disabled_at = now
        row.updated_at = now
    await session.flush()
    return row


__all__ = [
    "X_COOKIE_MAX_BYTES",
    "XCookieParseResult",
    "XCookieResolution",
    "XCookieValidationError",
    "disable_personal_x_cookie",
    "get_personal_x_cookie",
    "load_global_x_cookie",
    "parse_x_cookie_bytes",
    "resolve_x_cookie",
    "upsert_personal_x_cookie",
]
