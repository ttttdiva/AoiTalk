"""Bounded, keyless OpenAlex transport policy.

The research runner and the Imozuru expansion share this small policy module.
It owns only request pacing/retry decisions; actual network I/O remains at the
``UrlIngestService`` public pinned transport boundary.  No API key, ``mailto``
identity, or credential provider is read here.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .outbound_privacy_service import ExternalProviderBlocked, PrivacyError


OPENALEX_ORIGIN = "https://api.openalex.org"
OPENALEX_MAX_REQUESTS_PER_SECOND = 10
OPENALEX_MAX_PER_PAGE = 100


class OpenAlexPolicyError(RuntimeError):
    """A bounded policy decision prevented an OpenAlex request."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = str(code or "openalex_policy_error")
        super().__init__(message or self.code)


class OpenAlexRateLimiter:
    """Async sliding-window limiter suitable for a shared process instance."""

    def __init__(
        self,
        requests_per_second: int = OPENALEX_MAX_REQUESTS_PER_SECOND,
        *,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], Awaitable[Any]] | None = None,
    ) -> None:
        self.requests_per_second = max(1, min(int(requests_per_second), OPENALEX_MAX_REQUESTS_PER_SECOND))
        self._clock = clock or time.monotonic
        self._sleep = sleeper or asyncio.sleep
        self._timestamps: deque[float] = deque()
        # A process-shared limiter can be called by multiple chat worker event
        # loops/threads.  Protect only short deque mutations; never hold this
        # lock while awaiting the sleeper or checkpoint.
        self._lock = threading.Lock()

    async def acquire(
        self,
        *,
        checkpoint: Callable[[], Any] | None = None,
        deadline: float | None = None,
    ) -> None:
        """Wait for one permit, observing cancellation/deadline checkpoints."""

        while True:
            if checkpoint is not None:
                result = checkpoint()
                if inspect.isawaitable(result):
                    await result
            now = float(self._clock())
            with self._lock:
                while self._timestamps and now - self._timestamps[0] >= 1.0:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.requests_per_second:
                    self._timestamps.append(now)
                    return
                wait_for = max(0.001, 1.0 - (now - self._timestamps[0]))
            if deadline is not None:
                remaining = float(deadline) - float(self._clock())
                if remaining <= 0:
                    raise OpenAlexPolicyError("deadline", "OpenAlexの待機期限を超えました")
                wait_for = min(wait_for, remaining)
            await self._sleep(wait_for)

    @property
    def in_window(self) -> int:
        now = float(self._clock())
        with self._lock:
            while self._timestamps and now - self._timestamps[0] >= 1.0:
                self._timestamps.popleft()
            return len(self._timestamps)


_GLOBAL_LIMITERS: dict[str, OpenAlexRateLimiter] = {}
_GLOBAL_LIMITERS_LOCK = threading.Lock()


def get_openalex_limiter(
    key: str = "default",
    *,
    requests_per_second: int = OPENALEX_MAX_REQUESTS_PER_SECOND,
) -> OpenAlexRateLimiter:
    """Return one process-shared keyless limiter for the supplied scope."""

    normalized = str(key or "default").strip() or "default"
    with _GLOBAL_LIMITERS_LOCK:
        limiter = _GLOBAL_LIMITERS.get(normalized)
        if limiter is None:
            limiter = OpenAlexRateLimiter(requests_per_second)
            _GLOBAL_LIMITERS[normalized] = limiter
        return limiter


def normalize_openalex_url(url: str) -> str:
    """Allow only the public OpenAlex API origin and preserve query spelling."""

    parts = urlsplit(str(url or ""))
    if parts.scheme.casefold() != "https" or (parts.hostname or "").casefold() != "api.openalex.org":
        raise OpenAlexPolicyError("endpoint", "OpenAlexの公開API URLではありません")
    if parts.username is not None or parts.password is not None:
        raise OpenAlexPolicyError("endpoint", "OpenAlex URLに認証情報は指定できません")
    try:
        if parts.port not in (None, 443):
            raise OpenAlexPolicyError("endpoint", "OpenAlexのHTTPSポートが不正です")
    except ValueError as exc:
        raise OpenAlexPolicyError("endpoint", "OpenAlexのポートが不正です") from exc
    normalized_query = _normalize_query(parts.query)
    return urlunsplit(("https", "api.openalex.org", parts.path or "/", normalized_query, ""))


def _reject_credential_query(query: str) -> None:
    """Reject credential/identity parameters instead of forwarding them."""

    names = {
        "api_key",
        "apikey",
        "access_token",
        "auth_token",
        "authorization",
        "bearer",
        "key",
        "mail_to",
        "mailto",
        "x_api_key",
        "x_openalex_key",
    }
    for key, _value in parse_qsl(str(query or ""), keep_blank_values=True):
        key = str(key).strip().casefold().replace("-", "_")
        if key in names or key.endswith("_api_key") or key.endswith("_token"):
            raise OpenAlexPolicyError("credential", "OpenAlexの認証情報は利用できません")


def _normalize_query(query: str) -> str:
    pairs = parse_qsl(str(query or ""), keep_blank_values=True)
    _reject_credential_query(query)
    normalized: list[tuple[str, str]] = []
    for key, value in pairs:
        key_text = str(key)
        key_folded = key_text.casefold().replace("_", "-")
        if key_folded in {"per-page", "per-page"}:
            try:
                value = str(max(1, min(int(value), OPENALEX_MAX_PER_PAGE)))
            except (TypeError, ValueError) as exc:
                raise OpenAlexPolicyError("params", "per-pageが不正です") from exc
        normalized.append((key_text, str(value)))
    return urlencode(normalized, doseq=True)


def keyless_openalex_headers(headers: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Return safe headers with credential/identity hints removed."""

    result: dict[str, str] = {}
    for key, value in (headers or {}).items():
        name = str(key).casefold().replace("_", "-")
        if name in {
            "authorization",
            "x-api-key",
            "api-key",
            "x-openalex-key",
            "mailto",
            "x-mailto",
        }:
            continue
        result[str(key)] = str(value)
    # A neutral user agent keeps the request attributable without creating a
    # researcher identity or copying an operator email into outbound payloads.
    result.setdefault("User-Agent", "AoiTalkOpenAlex/1.0")
    return result


def bounded_openalex_params(params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Copy query parameters while enforcing OpenAlex's finite page size."""

    result = dict(params or {})
    _reject_credential_query(urlencode([(str(key), "") for key in result]))
    if "per-page" in result:
        try:
            result["per-page"] = max(1, min(int(result["per-page"]), OPENALEX_MAX_PER_PAGE))
        except (TypeError, ValueError) as exc:
            raise OpenAlexPolicyError("params", "per-pageが不正です") from exc
    if "per_page" in result:
        try:
            result["per_page"] = max(1, min(int(result["per_page"]), OPENALEX_MAX_PER_PAGE))
        except (TypeError, ValueError) as exc:
            raise OpenAlexPolicyError("params", "per_pageが不正です") from exc
    return result


def append_query(url: str, params: Mapping[str, Any] | None = None) -> str:
    """Build one API URL without adding a key, mailto, or hidden defaults."""

    normalized = normalize_openalex_url(url)
    if not params:
        return normalized
    parts = urlsplit(normalized)
    encoded = urlencode(
        [(str(key), str(value)) for key, value in bounded_openalex_params(params).items() if value is not None],
        doseq=True,
    )
    query = "&".join(part for part in (parts.query, encoded) if part)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


@dataclass
class OpenAlexTransportResult:
    """Normalized result expected from a public URL fetcher."""

    ok: bool
    status_code: int = 0
    payload: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    error: str = ""
    url: str = ""

    @property
    def retry_after(self) -> float | None:
        value = self.headers.get("retry-after") or self.headers.get("Retry-After")
        if value is None:
            return None
        try:
            return max(0.0, min(float(value), 30.0))
        except (TypeError, ValueError):
            try:
                parsed = parsedate_to_datetime(str(value))
                return max(0.0, min(parsed.timestamp() - time.time(), 30.0))
            except (TypeError, ValueError, OverflowError, OSError):
                return None


def normalize_openalex_fetch_result(value: Any, *, url: str) -> OpenAlexTransportResult:
    """Accept UrlFetchResult-like objects and tiny deterministic test fakes."""

    if isinstance(value, OpenAlexTransportResult):
        if not value.url:
            value.url = url
        return value
    if isinstance(value, Mapping):
        status = int(value.get("status_code", value.get("status", 0)) or 0)
        headers_raw = value.get("response_headers", value.get("headers", {}))
        headers = {str(k).lower(): str(v) for k, v in dict(headers_raw or {}).items()}
        payload = value.get("json_data", value.get("payload", value.get("json")))
        if payload is None and isinstance(value.get("body"), (dict, list)):
            payload = value.get("body")
        ok = bool(value.get("ok", value.get("success", status in range(200, 300))))
        status_name = str(value.get("acquisition_status", "") or "")
        if status == 0 and status_name == "rate_limited":
            status = 429
        if status == 0 and ok:
            status = 200
        return OpenAlexTransportResult(
            ok=ok and status < 400,
            status_code=status,
            payload=payload,
            headers=headers,
            error=str(value.get("error", "") or ""),
            url=str(value.get("final_url", value.get("url", "")) or url),
        )
    status = int(getattr(value, "status_code", 0) or 0)
    headers_raw = getattr(value, "response_headers", None)
    if headers_raw is None:
        headers_raw = getattr(value, "headers", None)
    headers = {str(k).lower(): str(v) for k, v in dict(headers_raw or {}).items()}
    ok = bool(getattr(value, "success", False))
    payload = getattr(value, "json_data", None)
    if payload is None and hasattr(value, "json") and callable(value.json):
        try:
            payload = value.json()
        except Exception:
            payload = None
    if payload is None:
        body = getattr(value, "body", None)
        if isinstance(body, (dict, list)):
            payload = body
        elif isinstance(body, str) and body.strip():
            import json

            try:
                payload = json.loads(body)
            except (TypeError, ValueError):
                payload = None
    status_name = str(getattr(value, "acquisition_status", "") or "")
    if status == 0 and status_name == "rate_limited":
        status = 429
    if status == 0 and ok:
        status = 200
    return OpenAlexTransportResult(
        ok=ok and status < 400,
        status_code=status,
        payload=payload,
        headers=headers,
        error=str(getattr(value, "error", "") or ""),
        url=str(getattr(value, "final_url", "") or url),
    )


class OpenAlexPolicy:
    """Finite request/retry budget shared by expansion and search workers."""

    def __init__(
        self,
        *,
        limiter: OpenAlexRateLimiter | None = None,
        max_requests: int = 8,
        max_retries: int = 2,
        retry_base_seconds: float = 0.25,
    ) -> None:
        self.limiter = limiter or get_openalex_limiter()
        self.max_requests = max(0, int(max_requests))
        self.max_retries = max(0, min(int(max_retries), 3))
        self.retry_base_seconds = max(0.01, min(float(retry_base_seconds), 5.0))
        self.requests = 0
        self.retries = 0

    async def request_json(
        self,
        fetcher: Any,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        checkpoint: Callable[[], Any] | None = None,
        deadline: float | None = None,
    ) -> OpenAlexTransportResult:
        """Fetch JSON using a public URL fetcher and finite 429 backoff."""

        target = append_query(url, params)
        attempt = 0
        while True:
            if deadline is not None and float(deadline) - time.monotonic() <= 0:
                raise OpenAlexPolicyError("deadline", "OpenAlexの待機期限を超えました")
            if checkpoint is not None:
                check = checkpoint()
                if inspect.isawaitable(check):
                    await check
            if self.requests >= self.max_requests:
                raise OpenAlexPolicyError("request_budget", "OpenAlexリクエスト予算に達しました")
            await self.limiter.acquire(checkpoint=checkpoint, deadline=deadline)
            self.requests += 1
            try:
                value = await _call_public_json(fetcher, target)
            except (OpenAlexPolicyError, ExternalProviderBlocked, PrivacyError):
                raise
            except asyncio.CancelledError:
                raise
            except Exception:
                # Transport implementations return typed failure envelopes at
                # their own boundary.  Propagating other exceptions preserves
                # runner ACL/cancellation decisions instead of converting them
                # into a retryable-looking provider failure.
                raise
            result = normalize_openalex_fetch_result(value, url=target)
            if result.status_code != 429 or attempt >= self.max_retries:
                return result
            attempt += 1
            self.retries += 1
            wait_for = result.retry_after
            if wait_for is None:
                wait_for = self.retry_base_seconds * (2 ** (attempt - 1))
            wait_for = min(30.0, max(0.01, float(wait_for)))
            if deadline is not None:
                remaining = float(deadline) - time.monotonic()
                if remaining <= 0 or wait_for > remaining:
                    raise OpenAlexPolicyError("deadline", "OpenAlexの再試行期限を超えました")
            await _bounded_sleep(wait_for, checkpoint=checkpoint, deadline=deadline)


async def _call_public_json(fetcher: Any, url: str) -> Any:
    method = getattr(fetcher, "fetch_public_json", None)
    if callable(method):
        value = method(url)
    else:
        method = getattr(fetcher, "fetch_public", None)
        if callable(method):
            value = method(url, kind="json")
        elif callable(fetcher):
            value = fetcher(url)
        else:
            raise OpenAlexPolicyError("fetcher", "公開JSON取得器がありません")
    return await value if inspect.isawaitable(value) else value


async def _bounded_sleep(
    seconds: float,
    *,
    checkpoint: Callable[[], Any] | None = None,
    deadline: float | None = None,
) -> None:
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if checkpoint is not None:
            value = checkpoint()
            if inspect.isawaitable(value):
                await value
        if deadline is not None:
            until_deadline = float(deadline) - time.monotonic()
            if until_deadline <= 0:
                raise OpenAlexPolicyError("deadline", "OpenAlexの再試行期限を超えました")
            chunk = min(remaining, until_deadline, 0.25)
        else:
            chunk = min(remaining, 0.25)
        await asyncio.sleep(chunk)
        remaining -= chunk


__all__ = [
    "OPENALEX_MAX_PER_PAGE",
    "OPENALEX_MAX_REQUESTS_PER_SECOND",
    "OPENALEX_ORIGIN",
    "OpenAlexPolicy",
    "OpenAlexPolicyError",
    "OpenAlexRateLimiter",
    "OpenAlexTransportResult",
    "append_query",
    "bounded_openalex_params",
    "get_openalex_limiter",
    "keyless_openalex_headers",
    "normalize_openalex_url",
]
