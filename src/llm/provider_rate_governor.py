"""Process-wide provider request governor and bounded temporary-429 retry.

Several task workers (each possibly on its own event loop or thread) share one
provider endpoint.  A threading primitive is used, never an asyncio semaphore,
so the limit holds across loops.  The key is a normalized
``provider/endpoint/model`` plus the existing opaque credential digest; no
secret, header, body or query string is ever stored.

Only a structured, non-quota HTTP 429 raised before any response output is
retryable, with at most ``MAX_PHYSICAL_ATTEMPTS`` sender calls in total
(the initial one included).  Every attempt re-enters the caller's privacy
gateway and request budget exactly once; this module never replays tools.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from ..services.provider_failure_metadata import extract_provider_failure

MAX_PHYSICAL_ATTEMPTS = 3
DEFAULT_PROVIDER_CONCURRENCY = 2
MAX_COOLDOWN_SECONDS = 30.0
_DEFAULT_BACKOFF_SECONDS = 1.0
_POLL_SECONDS = 0.05
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

Checkpoint = Callable[[], "float | None"]


def governor_key(provider: Any, endpoint: Any, model: Any, credential_id: Any = "") -> tuple[str, str, str, str]:
    """Normalized key; the endpoint keeps only host, port and path."""

    text = str(endpoint or "").strip()
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        port = f":{parts.port}" if parts.port else ""
        normalized = f"{host}{port}{parts.path.rstrip('/')}" if host else text.split("?", 1)[0].rstrip("/").lower()
    except ValueError:
        normalized = ""
    return (
        str(provider or "").strip().lower(),
        normalized,
        str(model or "").strip().lower(),
        str(credential_id or "").strip(),
    )


def is_loopback_endpoint(endpoint: Any) -> bool:
    try:
        return (urlsplit(str(endpoint or "")).hostname or "").lower() in _LOOPBACK_HOSTS
    except ValueError:
        return False


class ProviderLease:
    """One in-flight slot; ``release`` is idempotent and thread-safe."""

    def __init__(self, governor: "ProviderRateGovernor", key: tuple[str, ...]) -> None:
        self._governor = governor
        self._key = key
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._governor._release(self._key)


class ProviderRateGovernor:
    def __init__(self, max_concurrency: int = DEFAULT_PROVIDER_CONCURRENCY) -> None:
        self.max_concurrency = max_concurrency
        self._cond = threading.Condition()
        self._inflight: dict[tuple[str, ...], int] = {}
        self._cooldown_until: dict[tuple[str, ...], float] = {}

    def inflight(self, key: tuple[str, ...]) -> int:
        with self._cond:
            return self._inflight.get(key, 0)

    def cooldown_remaining(self, key: tuple[str, ...]) -> float:
        with self._cond:
            return max(0.0, self._cooldown_until.get(key, 0.0) - time.monotonic())

    def note_rate_limited(self, key: tuple[str, ...], delay: float) -> None:
        """Share one cooldown; a later, shorter hint never shortens it."""

        with self._cond:
            until = time.monotonic() + max(0.0, delay)
            if until > self._cooldown_until.get(key, 0.0):
                self._cooldown_until[key] = until

    def try_acquire(self, key: tuple[str, ...]) -> ProviderLease | None:
        with self._cond:
            if self._cooldown_until.get(key, 0.0) > time.monotonic():
                return None
            if self._inflight.get(key, 0) >= self.max_concurrency:
                return None
            self._inflight[key] = self._inflight.get(key, 0) + 1
        return ProviderLease(self, key)

    def _release(self, key: tuple[str, ...]) -> None:
        with self._cond:
            count = self._inflight.get(key, 0) - 1
            if count > 0:
                self._inflight[key] = count
            else:
                self._inflight.pop(key, None)
            self._cond.notify_all()

    def acquire(self, key: tuple[str, ...], checkpoint: Checkpoint) -> ProviderLease:
        """Blocking acquire; waits (holding nothing) under the caller's deadline/cancel."""

        while True:
            checkpoint()
            lease = self.try_acquire(key)
            if lease is not None:
                return lease
            with self._cond:
                self._cond.wait(_POLL_SECONDS)

    async def acquire_async(self, key: tuple[str, ...], checkpoint: Checkpoint) -> ProviderLease:
        while True:
            checkpoint()
            lease = self.try_acquire(key)
            if lease is not None:
                return lease
            await asyncio.sleep(_POLL_SECONDS)

    def wait_cooldown(self, key: tuple[str, ...], checkpoint: Checkpoint) -> None:
        while self.cooldown_remaining(key) > 0:
            checkpoint()
            time.sleep(_POLL_SECONDS)

    async def wait_cooldown_async(self, key: tuple[str, ...], checkpoint: Checkpoint) -> None:
        while self.cooldown_remaining(key) > 0:
            checkpoint()
            await asyncio.sleep(_POLL_SECONDS)


GOVERNOR = ProviderRateGovernor()


def temporary_429(error: BaseException) -> dict[str, Any] | None:
    """Safe failure info for a structured temporary 429, else None (quota is excluded)."""

    info = extract_provider_failure(error)
    if info is None or info["http_status"] != 429 or info.get("quota_indicated"):
        return None
    return info


def retry_delay(info: dict[str, Any], attempt: int, remaining: float | None) -> float | None:
    """Cooldown before the next attempt, or None when retrying cannot help."""

    hinted = info.get("retry_after_seconds")
    delay = float(hinted) if hinted is not None else _DEFAULT_BACKOFF_SECONDS * attempt
    if delay > MAX_COOLDOWN_SECONDS:
        return None
    if remaining is not None and delay >= remaining:
        return None
    return delay


def should_retry_429(error: BaseException, attempt: int, key: tuple[str, ...], checkpoint: Checkpoint) -> bool:
    """Record the shared cooldown for a retryable 429 and say whether to retry."""

    info = temporary_429(error)
    if info is None:
        return False
    # Other tasks on the same key must learn the RetryAfter even when this task cannot retry.
    hinted = info.get("retry_after_seconds")
    hint_delay = float(hinted) if hinted is not None else _DEFAULT_BACKOFF_SECONDS * attempt
    GOVERNOR.note_rate_limited(key, min(hint_delay, MAX_COOLDOWN_SECONDS))
    if attempt >= MAX_PHYSICAL_ATTEMPTS:
        return False
    delay = retry_delay(info, attempt, checkpoint())
    return delay is not None


def is_temporary_429_failure(provider_failure: Any) -> bool:
    """True for a stored safe ``provider_failure`` that is a temporary (non-quota) 429."""

    return (
        isinstance(provider_failure, dict)
        and provider_failure.get("http_status") == 429
        and not provider_failure.get("quota_indicated")
    )


def budget_exhausted_after_429(error: BaseException, prior_429: BaseException | None) -> bool:
    """A retry stopped by the request budget must still report the provider's 429."""

    return prior_429 is not None and str(error) == "ephemeral_request_budget_exhausted"


class LeasedSyncStream:
    """Hold a provider slot for the whole life of a plain synchronous stream.

    The slot is released exactly once (``ProviderLease.release`` is idempotent)
    on exhaustion, error, ``close()``/context exit, or garbage collection.
    """

    def __init__(self, stream: Any, lease: ProviderLease) -> None:
        self._stream = stream
        self._lease = lease

    def __iter__(self) -> "LeasedSyncStream":
        return self

    def __next__(self) -> Any:
        try:
            return next(self._stream)
        except BaseException:
            self._lease.release()  # StopIteration, provider error or cancellation
            raise

    def close(self) -> None:
        try:
            closer = getattr(self._stream, "close", None)
            if callable(closer):
                closer()
        finally:
            self._lease.release()

    def __enter__(self) -> "LeasedSyncStream":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self._lease.release()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)
