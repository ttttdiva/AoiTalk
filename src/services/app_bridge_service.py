"""Short-lived capability tokens for sandboxed embedded App hosts."""

from __future__ import annotations

import secrets
from typing import Any

from itsdangerous import BadData, URLSafeTimedSerializer

from ..security_secret import auth_secret_required, resolve_auth_secret_env


class AppBridgeTokenError(ValueError):
    """Invalid or expired embedded App token."""


def _resolve_secret() -> str:
    secret = resolve_auth_secret_env(
        ("AOITALK_APP_BRIDGE_SECRET", "AOITALK_JWT_SECRET", "AUTH_SECRET")
    )
    if secret is not None:
        return secret
    if auth_secret_required():
        raise RuntimeError(
            "AOITALK_APP_BRIDGE_SECRET or AOITALK_JWT_SECRET is required in Enterprise"
        )
    return secrets.token_urlsafe(32)


_SECRET = _resolve_secret()
_SERIALIZER = URLSafeTimedSerializer(_SECRET, salt="aoitalk-app-bridge-v1")
DEFAULT_TTL_SECONDS = 300


def issue_app_bridge_token(
    *,
    app_id: str,
    target_id: str,
    user_id: str,
    project_id: str | None,
    capabilities: list[str],
    release_id: str | None = None,
) -> str:
    return _SERIALIZER.dumps(
        {
            "app_id": app_id,
            "target_id": target_id,
            "user_id": user_id,
            "project_id": project_id,
            "release_id": release_id,
            "capabilities": sorted({str(item) for item in capabilities}),
        }
    )


def verify_app_bridge_token(token: str, *, max_age: int = DEFAULT_TTL_SECONDS) -> dict[str, Any]:
    if not token or len(token) > 4096:
        raise AppBridgeTokenError("App bridge token がありません")
    try:
        payload = _SERIALIZER.loads(token, max_age=max_age)
    except BadData as exc:
        raise AppBridgeTokenError("App bridge token が無効または期限切れです") from exc
    if not isinstance(payload, dict):
        raise AppBridgeTokenError("App bridge token payload が不正です")
    return payload


__all__ = [
    "AppBridgeTokenError",
    "DEFAULT_TTL_SECONDS",
    "issue_app_bridge_token",
    "verify_app_bridge_token",
]
