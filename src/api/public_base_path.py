"""Public browser prefix shared by HTTP routes, redirects, and cookies."""

from __future__ import annotations

import os
import re

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~-]+$")


def normalize_public_base_path(value: str | None = None) -> str:
    """Return an empty string for root hosting or a validated ``/prefix`` path."""

    raw = (
        os.getenv("AOITALK_PUBLIC_BASE_PATH", "")
        if value is None
        else str(value)
    ).strip()
    if not raw or raw == "/":
        return ""
    if "://" in raw or raw.startswith("//") or "?" in raw or "#" in raw:
        raise ValueError("AOITALK_PUBLIC_BASE_PATH must be an origin-relative path")
    normalized = "/" + raw.strip("/")
    segments = normalized[1:].split("/")
    if not all(_SEGMENT_RE.fullmatch(segment) for segment in segments):
        raise ValueError("AOITALK_PUBLIC_BASE_PATH contains an invalid path segment")
    return normalized


def public_cookie_path(value: str | None = None) -> str:
    """Cookie Path matching the public application mount."""

    return normalize_public_base_path(value) or "/"


def with_public_base_path(path: str, value: str | None = None) -> str:
    """Prefix an origin-relative browser path without applying it twice."""

    base_path = normalize_public_base_path(value)
    if not base_path or not path or not path.startswith("/") or path.startswith("//"):
        return path
    if path == base_path or path.startswith(f"{base_path}/"):
        return path
    return f"{base_path}{path}"
