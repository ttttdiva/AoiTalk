"""EmbeddingGemma2 master ON/OFF foundation (source-only, read-only).

Two bounded helpers:

``embedding_enabled(config)``
    Pure predicate over an already-loaded config mapping. No I/O, no mutation,
    and no ``Config`` construction.

``runtime_embedding_enabled()``
    Read-only runtime observer for long-lived consumers. It goes through the
    existing ``load_app_config_snapshot_sync`` contract, which performs no
    seeding, migration or default writeback, and it fails closed.

Scope note: this module is the shared source of the predicate and the runtime
observer. The consumers are now wired to it and are gated: the document, task
and knowledge index paths, both index workers and the embedding runtime check
the observer before doing embedding work, and the runtime applies an explicit
OFF decision through ``apply_master_enabled``.
"""

import asyncio
import logging
import threading
import time
from collections.abc import Mapping
from typing import Any, Final

logger = logging.getLogger(__name__)

#: Canonical setting key, identical to the ``ALLOWED_SETTINGS`` entry used by
#: the existing PATCH settings machinery in ``src/api/routes/config_routes.py``.
EMBEDDING_ENABLED_KEY: Final[str] = "search.embedding_enabled"

#: Historical behaviour: absent configuration means "enabled".
EMBEDDING_DEFAULT_ENABLED: Final[bool] = True


def embedding_enabled(config: Any) -> bool:
    """Return the effective master switch from an in-memory config mapping.

    Rules (missing != malformed; only *absence* uses the default):

    * missing ``search`` key -> default enabled;
    * missing ``embedding_enabled`` key inside the ``search`` mapping ->
      default enabled;
    * explicit ``True``/``False`` -> honoured verbatim;
    * explicit malformed setting value (``"false"``, ``1``, ``None``, list,
      ...) -> fail closed (``False``);
    * explicit malformed container -> fail closed (``False``): a non-mapping
      config (``None``, list, string, ...) or a ``search`` section that is
      present but not a mapping (``None``, list, string, ...).

    Pure function: it never reads a database, never touches ``Config`` and
    never mutates the mapping it is given.
    """

    if not isinstance(config, Mapping):
        return False

    if "search" not in config:
        return EMBEDDING_DEFAULT_ENABLED

    search = config.get("search")
    if not isinstance(search, Mapping):
        return False

    if "embedding_enabled" not in search:
        return EMBEDDING_DEFAULT_ENABLED

    value = search.get("embedding_enabled")
    return value if isinstance(value, bool) else False


#: Short TTL of the shared runtime observation (monotonic seconds).
RUNTIME_CACHE_TTL_SECONDS: Final[float] = 1.0

#: Minimum monotonic interval between two fail-closed warnings.
WARNING_INTERVAL_SECONDS: Final[float] = 60.0

#: Reload attempts when an invalidation lands while a load is in flight.
_MAX_LOAD_ATTEMPTS: Final[int] = 3

# Replaceable clock (tests inject a fake one). Always monotonic.
_monotonic = time.monotonic

# The lock only guards the fields below; it is never held across I/O.
_lock = threading.Lock()
_generation = 0
_cached: tuple[bool, float, int] | None = None  # (value, expires_at, generation)
_last_warning_at: float | None = None


def invalidate_runtime_embedding_cache() -> None:
    """Drop the shared observation and bump the generation.

    Loads that started before this call can no longer be stored or returned,
    so a pre-invalidation ON can never outlive a newer OFF.
    """

    global _generation, _cached
    with _lock:
        _generation += 1
        _cached = None


def _reset_runtime_embedding_cache_for_tests() -> None:
    global _generation, _cached, _last_warning_at
    with _lock:
        _generation += 1
        _cached = None
        _last_warning_at = None


def _lookup() -> tuple[bool | None, int]:
    with _lock:
        if _cached is not None:
            value, expires_at, generation = _cached
            if generation == _generation and _monotonic() < expires_at:
                return value, generation
        return None, _generation


def _store(generation: int, value: bool) -> bool:
    global _cached
    with _lock:
        if generation != _generation:
            return False
        _cached = (value, _monotonic() + RUNTIME_CACHE_TTL_SECONDS, generation)
        return True


def _warn_rate_limited(reason: str) -> None:
    global _last_warning_at
    with _lock:
        now = _monotonic()
        if (
            _last_warning_at is not None
            and now - _last_warning_at < WARNING_INTERVAL_SECONDS
        ):
            return
        _last_warning_at = now
    # Only a fixed reason / exception class name: never config values.
    logger.warning("embedding master switch unreadable; failing closed (%s)", reason)


def _load_uncached() -> bool:
    """One read-only snapshot observation. Fails closed, never raises."""

    try:
        # Imported lazily so importing this module stays cheap and side-effect
        # free, and so tests/consumers can substitute the store function.
        from ..app_config_store import load_app_config_snapshot_sync

        snapshot = load_app_config_snapshot_sync()
    except Exception as exc:  # fail closed, never raise into a runtime path
        _warn_rate_limited(type(exc).__name__)
        return False

    if not isinstance(snapshot, Mapping):
        _warn_rate_limited("invalid snapshot")
        return False

    return embedding_enabled(snapshot)


def runtime_embedding_enabled() -> bool:
    """Observe the master switch from the DB-backed config snapshot.

    Strictly read-only: the existing ``load_app_config_snapshot_sync`` is used
    with no arguments, so no ``Config`` bootstrap, no migration and no write
    happens here. Results are shared with the async observer through a short
    monotonic TTL cache.

    Fail closed: a snapshot failure (unavailable/empty snapshot, unexpected
    error) and an invalid snapshot (not a mapping: ``None``, list, string, ...)
    both return ``False``, so an unreadable or malformed snapshot never
    silently enables embedding work.
    """

    for _ in range(_MAX_LOAD_ATTEMPTS):
        cached, generation = _lookup()
        if cached is not None:
            return cached
        value = _load_uncached()
        if _store(generation, value):
            return value
    return False  # kept being invalidated mid-load: fail closed


async def runtime_embedding_enabled_async() -> bool:
    """Async variant: cache misses load in a worker thread (loop stays free)."""

    for _ in range(_MAX_LOAD_ATTEMPTS):
        cached, generation = _lookup()
        if cached is not None:
            return cached
        value = await asyncio.to_thread(_load_uncached)
        if _store(generation, value):
            return value
    return False
