"""Shared SGLang endpoint resolution for all OpenAI-compatible callers."""

from __future__ import annotations

import os
from typing import Any, Optional


def _config_value(config: Any, key: str) -> Optional[Any]:
    if config is None:
        return None
    try:
        value = config.get(key)
    except (AttributeError, TypeError):
        value = None
    if value not in (None, ""):
        return value

    current = config
    for part in key.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current in (None, ""):
            return None
    return current


def resolve_sglang_base_url(
    config: Any = None,
    *,
    fallback_host: str = "127.0.0.1",
    fallback_port: int = 30000,
) -> str:
    """Resolve one effective SGLang ``/v1`` endpoint."""
    # Persisted runtime/DB configuration is authoritative after first seed.
    # Compose environment variables are deployment defaults for a fresh
    # installation, not immutable overrides that silently defeat hot-switches.
    configured = (
        _config_value(config, "runtime.target_base_url")
        or _config_value(config, "sglang_base_url")
        or _config_value(config, "sglang.base_url")
        or os.getenv("SGLANG_BASE_URL")
    )
    if configured:
        return str(configured).strip().rstrip("/")

    host = _config_value(config, "sglang.host") or fallback_host
    port = _config_value(config, "sglang.port") or fallback_port
    return f"http://{host}:{port}/v1"


def resolve_sglang_model(
    config: Any = None,
    *,
    response_model: Optional[str] = None,
    fallback: str = "default",
) -> str:
    """Resolve one effective SGLang model name.

    Runtime target and persisted config are authoritative.  SGLANG_MODEL
    is only a bootstrap fallback for a fresh deployment, so changing the
    process environment cannot silently override a model selected in the DB.
    """
    configured = (
        _config_value(config, "runtime.target_model")
        or response_model
        or _config_value(config, "sglang.model")
        or _config_value(config, "sglang_model")
        or _config_value(config, "llm_model")
        or os.getenv("SGLANG_MODEL")
        or fallback
    )
    return str(configured).strip() or fallback


def enterprise_sglang_model(config: Any = None) -> str:
    """Return the one model allowed by an Enterprise SGLang deployment.

    Compose's ``SGLANG_MODEL`` is authoritative when present.  Native Linux
    deployments without Compose use the persisted SGLang/main model as their
    explicit contract instead.
    """
    try:
        from ..features import Features

        if not Features.is_enterprise():
            return ""
    except Exception as exc:
        raise RuntimeError(
            "Enterprise SGLang profile could not be determined"
        ) from exc
    return str(
        os.getenv("SGLANG_MODEL")
        or _config_value(config, "sglang.model")
        or _config_value(config, "sglang_model")
        or _config_value(config, "llm_model")
        or ""
    ).strip()


def enforce_enterprise_sglang_model(
    config: Any,
    provider: str,
    model: Any,
) -> str:
    """Reject an Enterprise SGLang target that is not the served model."""
    normalized_provider = str(provider or "").strip().lower()
    normalized_model = str(model or "").strip()
    try:
        expected = enterprise_sglang_model(config)
    except RuntimeError:
        # Do not expose an unvalidated target when the profile boundary itself
        # cannot be read.
        if normalized_provider == "sglang":
            raise
        return normalized_model
    if not expected or normalized_provider != "sglang":
        return normalized_model
    if normalized_model != expected:
        raise ValueError(
            "Enterprise SGLangはComposeで指定された単一モデルのみ使用できます: "
            f"expected={expected!r}, requested={normalized_model!r}"
        )
    return expected
