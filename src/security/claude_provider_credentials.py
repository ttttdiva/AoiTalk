"""Resolve the already authenticated Windows Claude Code profile safely.

The scoped Linux CLI cannot use the Windows keychain-backed Claude profile
directly.  Claude Code's supported ``CLAUDE_CODE_OAUTH_TOKEN`` environment
variable is the narrow bridge for that case.  This module reads only the
server user's fixed Claude profile, validates the OAuth record and returns the
short lived access token in memory.  Callers must keep the value out of argv,
files, logs and durable run metadata.
"""

from __future__ import annotations

import json
import math
import os
import stat
import time
from pathlib import Path
from typing import Any


CLAUDE_OAUTH_ENV_NAME = "CLAUDE_CODE_OAUTH_TOKEN"
_PROFILE_DIRECTORY_NAME = ".claude"
_CREDENTIAL_FILE_NAME = ".credentials.json"
_MAX_CREDENTIAL_FILE_BYTES = 64 * 1024
_MIN_TOKEN_LENGTH = 16
_MAX_TOKEN_LENGTH = 4096
_MIN_REMAINING_LIFETIME_SECONDS = 30


class ClaudeProviderCredentialUnavailable(RuntimeError):
    """Raised when the fixed, already authenticated Claude profile is unusable."""


def _default_profile_root() -> Path:
    """Return the current Windows user's canonical Claude profile directory."""

    if os.name != "nt":
        raise ClaudeProviderCredentialUnavailable("Windows Claude profile is unavailable")
    user_profile = str(os.environ.get("USERPROFILE") or "").strip()
    if not user_profile:
        raise ClaudeProviderCredentialUnavailable("Windows Claude profile is unavailable")
    home = Path(user_profile)
    if not home.is_absolute():
        raise ClaudeProviderCredentialUnavailable("Windows Claude profile is unavailable")
    try:
        home = home.resolve(strict=True)
        profile = home / _PROFILE_DIRECTORY_NAME
        resolved = profile.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise ClaudeProviderCredentialUnavailable("Windows Claude profile is unavailable") from None
    if resolved != profile or not profile.is_dir():
        raise ClaudeProviderCredentialUnavailable("Windows Claude profile is unavailable")
    return profile


def _regular_unlinked_file(path: Path) -> None:
    """Reject links/reparse points before reading the credential file."""

    try:
        metadata = path.lstat()
    except (OSError, ValueError):
        raise ClaudeProviderCredentialUnavailable("Claude OAuth profile is unavailable") from None
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)
    ):
        raise ClaudeProviderCredentialUnavailable("Claude OAuth profile is unavailable")
    if int(metadata.st_size) > _MAX_CREDENTIAL_FILE_BYTES:
        raise ClaudeProviderCredentialUnavailable("Claude OAuth profile is unavailable")


def _oauth_record(profile_root: Path) -> dict[str, Any]:
    """Read and parse one fixed profile record without exposing its values."""

    if not profile_root.is_absolute() or profile_root.name != _PROFILE_DIRECTORY_NAME:
        raise ClaudeProviderCredentialUnavailable("Claude OAuth profile is unavailable")
    credentials = profile_root / _CREDENTIAL_FILE_NAME
    _regular_unlinked_file(credentials)
    try:
        raw = credentials.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ClaudeProviderCredentialUnavailable("Claude OAuth profile is unavailable") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("claudeAiOauth"), dict):
        raise ClaudeProviderCredentialUnavailable("Claude OAuth profile is unavailable")
    return payload["claudeAiOauth"]


def _access_token(record: dict[str, Any], *, now: float | None = None) -> str:
    """Validate the OAuth access token and its expiry without refreshing it."""

    token = record.get("accessToken")
    if not isinstance(token, str) or not (_MIN_TOKEN_LENGTH <= len(token) <= _MAX_TOKEN_LENGTH):
        raise ClaudeProviderCredentialUnavailable("Claude OAuth access token is unavailable")
    if any(char in token for char in ("\x00", "\r", "\n")):
        raise ClaudeProviderCredentialUnavailable("Claude OAuth access token is unavailable")
    scopes = record.get("scopes")
    if not isinstance(scopes, list) or "user:inference" not in scopes:
        raise ClaudeProviderCredentialUnavailable("Claude OAuth inference scope is unavailable")
    try:
        expires_at = float(record.get("expiresAt"))
    except (TypeError, ValueError, OverflowError):
        raise ClaudeProviderCredentialUnavailable("Claude OAuth expiry is unavailable") from None
    if not math.isfinite(expires_at) or expires_at <= 0:
        raise ClaudeProviderCredentialUnavailable("Claude OAuth expiry is unavailable")
    # Claude Code stores epoch milliseconds.  Accept seconds only for a
    # narrowly compatible legacy profile representation.
    if expires_at > 100_000_000_000:
        expires_at /= 1000.0
    current = time.time() if now is None else float(now)
    if not math.isfinite(current):
        raise ClaudeProviderCredentialUnavailable("Claude OAuth clock is unavailable")
    if expires_at <= current + _MIN_REMAINING_LIFETIME_SECONDS:
        raise ClaudeProviderCredentialUnavailable("Claude OAuth access token is expired")
    return token


def resolve_claude_oauth_token(*, now: float | None = None) -> str:
    """Resolve the selected Windows Claude profile's usable OAuth token.

    The profile root is intentionally not caller-selectable.  A task, model,
    request or CLI argument cannot redirect this resolver to another account
    or credential file.
    """

    return _access_token(_oauth_record(_default_profile_root()), now=now)


__all__ = [
    "CLAUDE_OAUTH_ENV_NAME",
    "ClaudeProviderCredentialUnavailable",
    "resolve_claude_oauth_token",
]
