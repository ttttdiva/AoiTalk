"""Shared profile-aware resolution for authentication signing secrets."""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import TypeVar

from .features import Features
from .security.secret_env import load_secret_environment


_ErrorT = TypeVar("_ErrorT", bound=Exception)


# Authentication can be imported directly by API routes, without first
# constructing Config.  Resolve Docker-style secret files at this boundary as
# well so JWT and Caddy-gate checks never depend on import order.
load_secret_environment()


def auth_secret_required() -> bool:
    return Features.is_enterprise() or os.getenv(
        "AOITALK_REQUIRE_AUTH_SECRET", ""
    ).strip().lower() in {"1", "true", "yes", "on"}


def resolve_auth_secret_env(
    names: Iterable[str],
    *,
    error_type: type[_ErrorT] = RuntimeError,
) -> str | None:
    """Return the first non-blank secret, skipping personal-mode blank samples."""

    strict = auth_secret_required()
    blank_names: list[str] = []
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        if value.strip():
            return value
        # A Docker secret file can exist but be empty when an optional
        # capability is not configured.  Continue to the documented fallback
        # names (for example App Bridge -> JWT) before failing closed.
        blank_names.append(name)
    if strict and blank_names:
        raise error_type(f"{blank_names[0]} must not be blank")
    return None


__all__ = ["auth_secret_required", "resolve_auth_secret_env"]
