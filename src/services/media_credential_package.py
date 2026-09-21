"""In-memory parsing for Media Operations credential packages."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .x_cookie_service import parse_x_cookie_bytes


MAX_CREDENTIAL_PACKAGE_BYTES = 2 * 1024 * 1024
SUPPORTED_PLATFORMS = frozenset({"x", "pixiv", "patreon", "youtube", "instagram", "dlsite"})
CONNECTION_TYPES = frozenset({"cookie_export", "api_token", "oauth"})

_PLATFORM_DOMAINS: dict[str, tuple[str, ...]] = {
    "x": ("x.com", "twitter.com"),
    "youtube": ("youtube.com", "google.com"),
    "patreon": ("patreon.com",),
    "instagram": ("instagram.com",),
    "pixiv": ("pixiv.net",),
    "dlsite": ("dlsite.com",),
}
_COOKIE_FIELDS = frozenset({"domain", "include_subdomains", "path", "secure", "expires", "name", "value"})
_API_FIELDS = frozenset(
    {
        "token",
        "token_kind",
        "access_token",
        "api_token",
        "api_key",
        "token_type",
        "expires_at",
    }
)
_OAUTH_FIELDS = frozenset(
    {"access_token", "refresh_token", "token_type", "expires_at", "scope", "scopes"}
)


class CredentialPackageError(ValueError):
    """Safe package validation error; never contains raw input."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class CredentialPackage:
    platform: str
    connection_type: str
    payload: dict[str, Any]
    expires_at: int | None = None
    remote_account_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "connection_type": self.connection_type,
            "expires_at": self.expires_at,
            "remote_account_ref": self.remote_account_ref,
            "metadata": dict(self.metadata),
        }


def _platform(value: Any) -> str:
    rendered = str(value or "").strip().lower()
    if rendered not in SUPPORTED_PLATFORMS:
        raise CredentialPackageError("unsupported_platform", "platform is unsupported")
    return rendered


def _connection_type(value: Any) -> str:
    rendered = str(value or "").strip().lower()
    if rendered not in CONNECTION_TYPES:
        raise CredentialPackageError("invalid_connection_type", "connection_type is invalid")
    return rendered


def _safe_text(value: Any, *, field: str, max_length: int = 4096) -> str:
    if not isinstance(value, str):
        raise CredentialPackageError("invalid_field", f"{field} is invalid")
    if not value or len(value) > max_length or any(ord(c) < 0x20 for c in value):
        raise CredentialPackageError("invalid_field", f"{field} is invalid")
    return value


def _expiry(value: Any, *, allow_expired: bool = False) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise CredentialPackageError("invalid_expiry", "expires_at is invalid")
    if isinstance(value, (int, float)):
        try:
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError
            value = int(value)
        except (ValueError, OverflowError):
            raise CredentialPackageError("invalid_expiry", "expires_at is invalid") from None
    elif isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                value = int(parsed.timestamp())
            except ValueError:
                raise CredentialPackageError("invalid_expiry", "expires_at is invalid") from None
    else:
        raise CredentialPackageError("invalid_expiry", "expires_at is invalid")
    if not allow_expired and value <= int(time.time()):
        raise CredentialPackageError("expired", "credential package is expired")
    return value


def _domain_allowed(domain: str, platform: str) -> bool:
    normalized = domain.casefold().strip().lstrip(".")
    return any(normalized == root or normalized.endswith("." + root) for root in _PLATFORM_DOMAINS[platform])


def _parse_cookie_export(
    raw: bytes,
    platform: str,
    *,
    allow_expired: bool = False,
) -> CredentialPackage:
    # Google/YouTube browser session cookies are deliberately never imported;
    # use the OAuth flow instead.
    if platform == "youtube":
        raise CredentialPackageError(
            "unsupported_connection_type",
            "cookie export is unsupported for this platform",
        )
    if platform == "x":
        try:
            parsed = parse_x_cookie_bytes(raw, now=0 if allow_expired else None)
        except Exception as exc:
            if isinstance(exc, CredentialPackageError):
                raise
            raise CredentialPackageError("invalid_cookie_export", "cookie export is invalid") from None
        if parsed.status != "available":
            raise CredentialPackageError(parsed.status, "cookie export is invalid or expired")
        payload = parsed.canonical_payload()
        expires = [v for v in parsed.expires.values() if isinstance(v, int)]
        return CredentialPackage(platform, "cookie_export", payload, min(expires) if expires else None)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CredentialPackageError("invalid_cookie_export", "cookie export is invalid") from None
    cookies: list[dict[str, Any]] = []
    header_seen = False
    for raw_line in text.splitlines():
        line = raw_line.strip("\r")
        if not line.strip():
            continue
        if line.casefold().strip().startswith("# netscape http cookie file"):
            header_seen = True
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            raise CredentialPackageError("invalid_cookie_export", "cookie export is invalid")
        domain, include_subdomains, path, secure, expiry, name, value = fields
        if not _domain_allowed(domain, platform) or include_subdomains.upper() not in {"TRUE", "FALSE"}:
            raise CredentialPackageError("invalid_cookie_export", "cookie domain is not allowed")
        if not path.startswith("/") or secure.upper() not in {"TRUE", "FALSE"} or not name or not value:
            raise CredentialPackageError("invalid_cookie_export", "cookie export is invalid")
        if expiry == "":
            expires = None
        else:
            try:
                expires = int(expiry)
            except ValueError:
                raise CredentialPackageError("invalid_cookie_export", "cookie expiry is invalid") from None
            if expires and expires <= int(time.time()) and not allow_expired:
                raise CredentialPackageError("expired", "cookie export is expired")
            expires = expires or None
        cookies.append({
            "domain": domain.casefold(),
            "include_subdomains": include_subdomains.upper() == "TRUE",
            "path": path,
            "secure": secure.upper() == "TRUE",
            "expires": expires,
            "name": name,
            "value": value,
        })
    if not header_seen or not cookies:
        raise CredentialPackageError("invalid_cookie_export", "cookie export is invalid")
    return CredentialPackage(platform, "cookie_export", {"cookies": cookies})


def _parse_json_secret(
    raw: bytes,
    platform: str,
    connection_type: str,
    *,
    allow_expired: bool = False,
) -> CredentialPackage:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError, MemoryError):
        raise CredentialPackageError("invalid_json", "credential package JSON is invalid") from None
    if not isinstance(decoded, Mapping):
        raise CredentialPackageError("invalid_json", "credential package must be an object")
    value = dict(decoded)
    allowed = _OAUTH_FIELDS if connection_type == "oauth" else _API_FIELDS
    if set(value) - allowed:
        raise CredentialPackageError("unknown_field", "credential package contains an unknown field")
    token_candidates = [
        value.get(name)
        for name in ("token", "access_token", "api_token", "api_key")
        if name in value
    ]
    if len(token_candidates) > 1:
        raise CredentialPackageError("duplicate_token", "credential package token is ambiguous")
    token = token_candidates[0] if token_candidates else None
    if not isinstance(token, str) or not token.strip() or len(token) > 8192:
        raise CredentialPackageError("missing_token", "credential package token is required")
    if any(ord(c) < 0x20 for c in token):
        raise CredentialPackageError("invalid_token", "credential package token is invalid")
    payload: dict[str, Any] = {"access_token": token}
    if connection_type == "oauth" and "refresh_token" in value:
        refresh = value["refresh_token"]
        if not isinstance(refresh, str) or len(refresh) > 8192 or any(ord(c) < 0x20 for c in refresh):
            raise CredentialPackageError("invalid_token", "credential package token is invalid")
        payload["refresh_token"] = refresh
    token_type_value = value.get("token_type", value.get("token_kind"))
    if token_type_value is not None:
        token_type = _safe_text(token_type_value, field="token_type", max_length=32)
        if token_type.casefold() not in {"bearer", "mac"}:
            raise CredentialPackageError("invalid_token_type", "token_type is unsupported")
        payload["token_type"] = token_type
    expires_at = _expiry(value.get("expires_at"), allow_expired=allow_expired)
    if expires_at is not None:
        payload["expires_at"] = expires_at
    if connection_type == "oauth" and ("scope" in value or "scopes" in value):
        scope = value.get("scope", value.get("scopes"))
        if isinstance(scope, str):
            if len(scope) > 4096 or any(ord(c) < 0x20 for c in scope):
                raise CredentialPackageError("invalid_scope", "scope is invalid")
            payload["scope"] = scope
        elif (
            isinstance(scope, list)
            and len(scope) <= 100
            and all(isinstance(item, str) and item and len(item) <= 256 and not any(ord(c) < 0x20 for c in item) for item in scope)
        ):
            payload["scope"] = list(scope)
        else:
            raise CredentialPackageError("invalid_scope", "scope is invalid")
    return CredentialPackage(platform, connection_type, payload, expires_at)


def parse_credential_package(
    payload: bytes | bytearray | memoryview | str,
    *,
    platform: str,
    connection_type: str,
    allow_expired: bool = False,
) -> CredentialPackage:
    """Validate and canonicalize a package entirely in memory."""
    # Parsing is an untrusted-input boundary.  In particular, malformed
    # Unicode (unpaired surrogates), pathological nesting, or allocator
    # exhaustion must become a bounded validation error rather than escaping
    # as a 500 or leaking parser details to callers.
    try:
        platform_value = _platform(platform)
        type_value = _connection_type(connection_type)
        if isinstance(payload, str):
            raw = payload.encode("utf-8")
        elif isinstance(payload, (bytes, bytearray, memoryview)):
            raw = bytes(payload)
        else:
            raise CredentialPackageError("invalid_payload", "credential package payload is invalid")
        if len(raw) == 0 or len(raw) > MAX_CREDENTIAL_PACKAGE_BYTES:
            raise CredentialPackageError("too_large", "credential package exceeds 2 MiB")
        if type_value == "cookie_export":
            return _parse_cookie_export(raw, platform_value, allow_expired=allow_expired)
        return _parse_json_secret(raw, platform_value, type_value, allow_expired=allow_expired)
    except CredentialPackageError:
        raise
    except (UnicodeEncodeError, RecursionError, MemoryError):
        raise CredentialPackageError("invalid_payload", "credential package payload is invalid") from None


__all__ = [
    "CONNECTION_TYPES",
    "CredentialPackage",
    "CredentialPackageError",
    "MAX_CREDENTIAL_PACKAGE_BYTES",
    "MediaCredentialPackageError",
    "SUPPORTED_PLATFORMS",
    "parse_credential_package",
    "parse_media_credential_package",
]

# Terminology aliases used by integrations that call the upload a
# ``media_credential_package``.
parse_media_credential_package = parse_credential_package
MediaCredentialPackageError = CredentialPackageError
