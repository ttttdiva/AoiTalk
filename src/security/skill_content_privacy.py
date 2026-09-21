"""Fail-closed privacy helpers for durable Skill proposal content."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_REDACTED_SECRET = "[REDACTED_SECRET]"
_CREDENTIAL_KEY_NAMES = frozenset(
    {
        "api_key",
        "access_token",
        "refresh_token",
        "auth_token",
        "bearer_token",
        "token",
        "secret",
        "client_secret",
        "secret_key",
        "private_key",
        "password",
        "passwd",
        "passphrase",
        "credential",
        "credentials",
        "authorization",
    }
)


class SkillContentPrivacyError(ValueError):
    """Durable Skill content contains material classified as secret-like."""


def _classify_sensitivity(value: str) -> tuple[str, str | None]:
    # Lazy import avoids making memory model import order depend on the service
    # module while still reusing the repository's canonical sensitivity rules.
    from ..services.scoped_memory_service import classify_sensitivity

    return classify_sensitivity(value)


def _normalize_credential_key(key: Any) -> str:
    text = str(key or "").strip()
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"[^0-9A-Za-z]+", "_", text)
    return text.strip("_").casefold()


def _is_sensitive_key(key: Any) -> bool:
    """Match credential fields by component boundaries, not substrings.

    Qualified names such as ``provider_access_token`` remain protected while
    suffix-qualified names such as ``token_value`` and ``api_key_id`` are
    protected too. Credential markers may occur anywhere as complete
    underscore-delimited component sequences.

    ordinary Skill parameter names such as ``bypass_cache``, ``compass``, and
    ``tokenizer`` are not treated as credentials.
    """
    normalized = _normalize_credential_key(key)
    padded = f"_{normalized}_"
    return any(
        f"_{marker}_" in padded
        for marker in _CREDENTIAL_KEY_NAMES
    )


def contains_secret_like_skill_content(
    value: Any,
    *,
    key: str | None = None,
) -> bool:
    """Recursively inspect one JSON-like Skill content value."""
    if key and _is_sensitive_key(key) and value not in (None, ""):
        return True

    if value is None or isinstance(value, (bool, int, float)):
        return False

    if isinstance(value, str):
        _sensitivity, rejection_reason = _classify_sensitivity(value)
        return rejection_reason is not None

    if isinstance(value, Mapping):
        return any(
            contains_secret_like_skill_content(child, key=str(child_key))
            for child_key, child in value.items()
        )

    if isinstance(value, (list, tuple, set)):
        return any(
            contains_secret_like_skill_content(child)
            for child in value
        )

    _sensitivity, rejection_reason = _classify_sensitivity(str(value))
    return rejection_reason is not None


def assert_no_secret_like_skill_content(value: Any) -> None:
    """Reject content rather than persisting a redacted Skill definition."""
    if contains_secret_like_skill_content(value):
        raise SkillContentPrivacyError(
            "Skill content contains secret-like material"
        )


def redact_secret_like_skill_content(
    value: Any,
    *,
    key: str | None = None,
) -> Any:
    """Defense-in-depth serializer for pre-C2/externally-created rows."""
    if key and _is_sensitive_key(key) and value not in (None, ""):
        return "[REDACTED]"

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        _sensitivity, rejection_reason = _classify_sensitivity(value)
        return _REDACTED_SECRET if rejection_reason else value

    if isinstance(value, Mapping):
        return {
            str(child_key): redact_secret_like_skill_content(
                child,
                key=str(child_key),
            )
            for child_key, child in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [
            redact_secret_like_skill_content(child)
            for child in value
        ]

    text = str(value)
    _sensitivity, rejection_reason = _classify_sensitivity(text)
    return _REDACTED_SECRET if rejection_reason else text


__all__ = [
    "SkillContentPrivacyError",
    "assert_no_secret_like_skill_content",
    "contains_secret_like_skill_content",
    "redact_secret_like_skill_content",
]
