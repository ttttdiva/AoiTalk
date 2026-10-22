"""Bearer credentials for the 73 Generation Studio service boundary.

A fixed ``AOITALK_GENERATION_STUDIO_BEARER_TOKEN`` (alias
``GENERATION_STUDIO_BEARER_TOKEN``) wins unchanged.  Otherwise
``AOITALK_GENERATION_STUDIO_BOOTSTRAP_TOKEN`` is exchanged for a short-lived
73 access token through ``POST /api/v1/auth/session``.  Issued access tokens
live only in process memory, one 73 session per (origin, project); the 73
refresh token is never retained because a fresh bootstrap exchange replaces it.
No credential value is ever placed in logs, exceptions, or API responses.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .media_operations_service import MediaOperationsError

_BOOTSTRAP_RE = re.compile(r"^bst_[A-Za-z0-9_-]{16,256}$")
_ACCESS_RE = re.compile(r"^[A-Za-z0-9_.~+/=-]{8,512}$")
_PROJECT_RE = re.compile(r"^prj_[A-Za-z0-9_-]{8,160}$")
# Renew this long before 73's access expiry so an in-flight call never races it.
_RENEW_MARGIN = timedelta(seconds=60)


class GenerationStudioUnavailableError(MediaOperationsError):
    """73 cannot be used right now; ``str(exc)`` is the stable reason code."""

    status_code = 503

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _IssuedAccess:
    token: str
    expires_at: datetime


class GenerationStudioTokenProvider:
    def __init__(
        self,
        *,
        static_token: str | None = None,
        bootstrap_token: str | None = None,
        http_client_factory: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._static = (static_token or "").strip() or None
        self._bootstrap = (bootstrap_token or "").strip() or None
        self._client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[tuple[str, str], _IssuedAccess] = {}
        self._locks: dict[tuple[int, str, str], asyncio.Lock] = {}

    def __repr__(self) -> str:
        mode = "static" if self._static else "bootstrap" if self._bootstrap else "none"
        return f"GenerationStudioTokenProvider(mode={mode})"

    @property
    def configured(self) -> bool:
        return bool(self._static or self._bootstrap)

    @property
    def renewable(self) -> bool:
        return self._static is None and self._bootstrap is not None

    def _env_identity(self) -> tuple[str | None, str | None]:
        return self._static, self._bootstrap

    async def bearer(self, origin: str, project_id: str | None, *, stale: str | None = None) -> str:
        """Return a usable access token; ``stale`` forces renewal of that token."""

        if self._static:
            return self._static
        if not self._bootstrap:
            raise GenerationStudioUnavailableError("studio_unconfigured")
        key = (origin, project_id or "")
        lock = self._locks.setdefault(
            (id(asyncio.get_running_loop()), *key), asyncio.Lock()
        )
        async with lock:
            cached = self._cache.get(key)
            if (
                cached is not None
                and cached.token != stale
                and cached.expires_at - _RENEW_MARGIN > self._clock()
            ):
                return cached.token
            self._cache.pop(key, None)
            issued = await self._issue(origin, project_id)
            self._cache[key] = issued
            return issued.token

    async def attempts(self, origin: str, project_id: str | None) -> AsyncIterator[str]:
        """Yield a token, then one renewed token if the caller saw a 401."""

        token = await self.bearer(origin, project_id)
        yield token
        if self.renewable:
            yield await self.bearer(origin, project_id, stale=token)

    async def _issue(self, origin: str, project_id: str | None) -> _IssuedAccess:
        if not _BOOTSTRAP_RE.fullmatch(self._bootstrap or ""):
            raise GenerationStudioUnavailableError("studio_auth_failed")
        if project_id is not None and not _PROJECT_RE.fullmatch(project_id):
            raise GenerationStudioUnavailableError("studio_unavailable")
        try:
            async with self._client_factory() as client:
                response = await client.post(
                    f"{origin}/api/v1/auth/session",
                    json={
                        "schema_version": "1.0",
                        "grant_type": "bootstrap_token",
                        "bootstrap_token": self._bootstrap,
                    },
                    headers={"Accept": "application/json"},
                )
                body = _json_body(response)
                session = body.get("session")
                token = body.get("access_token")
                if not isinstance(session, Mapping) or not isinstance(token, str) or not _ACCESS_RE.fullmatch(token):
                    raise GenerationStudioUnavailableError("studio_unavailable")
                expires_at = _expiry(session.get("access_expires_at"))
                if project_id is not None and session.get("project_id") != project_id:
                    # 73 keeps the same access token and moves the persisted
                    # session scope; accept a rotated token if one is returned.
                    switched = _json_body(
                        await client.post(
                            f"{origin}/api/v1/session/project",
                            json={"project_id": project_id},
                            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
                        )
                    )
                    scope = switched.get("scope")
                    if not isinstance(scope, Mapping) or scope.get("project_id") != project_id:
                        raise GenerationStudioUnavailableError("studio_unavailable")
                    rotated = switched.get("access_token")
                    if isinstance(rotated, str) and _ACCESS_RE.fullmatch(rotated):
                        token = rotated
                    switched_expiry = switched.get("access_expires_at") or switched.get("expires_at")
                    if switched_expiry is not None:
                        expires_at = min(expires_at, _expiry(switched_expiry))
        except httpx.HTTPError:
            raise GenerationStudioUnavailableError("studio_unavailable") from None
        return _IssuedAccess(token, expires_at)


def _json_body(response: httpx.Response) -> Mapping[str, Any]:
    if response.status_code in {401, 403, 422}:
        raise GenerationStudioUnavailableError("studio_auth_failed")
    if response.status_code < 200 or response.status_code >= 300:
        raise GenerationStudioUnavailableError("studio_unavailable")
    try:
        body = response.json()
    except (TypeError, ValueError):
        raise GenerationStudioUnavailableError("studio_unavailable") from None
    if not isinstance(body, Mapping):
        raise GenerationStudioUnavailableError("studio_unavailable")
    return body


def _expiry(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise GenerationStudioUnavailableError("studio_unavailable") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_shared: GenerationStudioTokenProvider | None = None


def generation_studio_token_provider() -> GenerationStudioTokenProvider:
    """Process-wide provider; rebuilt only when the configured secrets change."""

    global _shared
    static = (
        os.getenv("AOITALK_GENERATION_STUDIO_BEARER_TOKEN")
        or os.getenv("GENERATION_STUDIO_BEARER_TOKEN")
        or ""
    ).strip() or None
    bootstrap = (os.getenv("AOITALK_GENERATION_STUDIO_BOOTSTRAP_TOKEN") or "").strip() or None
    if _shared is None or _shared._env_identity() != (static, bootstrap):
        _shared = GenerationStudioTokenProvider(static_token=static, bootstrap_token=bootstrap)
    return _shared
