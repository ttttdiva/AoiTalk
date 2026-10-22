"""Session-scoped response-model client cache lifecycle helpers.

The assistant modes keep response-model clients alive across turns so an
explicit selection does not rebuild a provider client for every request.  A
cache entry is still an owner of a managed-local consumer lease, therefore a
session route change must retire only that session's entry and wait for any
generation/title work to finish before releasing the consumer.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Iterable


logger = logging.getLogger(__name__)

_LEGACY_SESSION_KEY = "__legacy__"
_CACHE_SESSION = contextvars.ContextVar(
    "aoitalk_response_model_cache_session",
    default=None,
)


@dataclass
class ResponseModelCacheEntry:
    key: tuple[str, ...]
    session_id: str
    client: Any
    active_uses: int = 0
    pending_titles: int = 0
    retire_requested: bool = False
    release_task: asyncio.Task | None = None
    release_error: str | None = None


class response_model_cache_session:
    """Bind the session ID used by a mode's synchronous cache getter."""

    def __init__(self, session_id: Any):
        self.session_id = normalize_response_model_session_id(session_id)
        self._token = None

    def __enter__(self):
        self._token = _CACHE_SESSION.set(self.session_id)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._token is not None:
            _CACHE_SESSION.reset(self._token)
            self._token = None


def normalize_response_model_session_id(session_id: Any) -> str:
    clean = str(session_id or "").strip()
    return clean or _LEGACY_SESSION_KEY


def current_response_model_cache_session() -> str:
    return normalize_response_model_session_id(_CACHE_SESSION.get())


def response_model_cache_key(
    session_id: Any,
    provider: Any,
    model: Any,
    effort: Any = None,
) -> tuple[str, ...]:
    values = (
        normalize_response_model_session_id(session_id),
        str(provider or "").strip(),
        str(model or "").strip(),
    )
    return (*values, str(effort)) if effort is not None else values


def _cache_entries(owner: Any) -> dict[tuple[str, ...], ResponseModelCacheEntry]:
    entries = getattr(owner, "_response_model_cache_entries", None)
    if not isinstance(entries, dict):
        entries = {}
        setattr(owner, "_response_model_cache_entries", entries)
    clients = getattr(owner, "_response_model_clients", None)
    if not isinstance(clients, dict):
        clients = {}
        setattr(owner, "_response_model_clients", clients)
    # Tests and legacy embeddings may replace the public client map directly.
    # Do not retain metadata for entries that are no longer reachable there.
    for key in tuple(entries):
        if key not in clients:
            entries.pop(key, None)
    return entries


def cached_response_model_client(owner: Any, key: tuple[str, ...]) -> Any:
    clients = getattr(owner, "_response_model_clients", None)
    if not isinstance(clients, dict):
        return None
    entry = _cache_entries(owner).get(key)
    if entry is not None and entry.retire_requested:
        # Do not overwrite a retired entry whose consumer release is still
        # pending.  Returning None here would let a new client replace the
        # metadata for the old ticket and leak that consumer permanently.
        raise RuntimeError("response-model client retirement is still pending")
    return clients.get(key)


def remember_response_model_client(
    owner: Any,
    key: tuple[str, ...],
    client: Any,
    *,
    session_id: Any,
) -> None:
    clients = getattr(owner, "_response_model_clients", None)
    if not isinstance(clients, dict):
        clients = {}
        setattr(owner, "_response_model_clients", clients)
    clients[key] = client
    entries = _cache_entries(owner)
    entries[key] = ResponseModelCacheEntry(
        key=key,
        session_id=normalize_response_model_session_id(session_id),
        client=client,
    )


def _entry_for_client(
    owner: Any,
    client: Any,
    *,
    session_id: Any = None,
) -> ResponseModelCacheEntry | None:
    if client is None:
        return None
    expected_session = (
        normalize_response_model_session_id(session_id)
        if session_id is not None
        else None
    )
    for entry in _cache_entries(owner).values():
        if entry.client is client and (
            expected_session is None or entry.session_id == expected_session
        ):
            return entry
    return None


def begin_response_model_use(owner: Any, client: Any, *, session_id: Any) -> ResponseModelCacheEntry | None:
    entry = _entry_for_client(owner, client, session_id=session_id)
    if entry is not None:
        entry.active_uses += 1
    return entry


def end_response_model_use(owner: Any, entry: ResponseModelCacheEntry | None) -> None:
    if entry is None:
        return
    entry.active_uses = max(0, entry.active_uses - 1)
    if entry.retire_requested:
        schedule_response_model_retire(owner, entry)


def retain_response_model_title(entry: ResponseModelCacheEntry | None) -> None:
    if entry is not None:
        entry.pending_titles += 1


def release_response_model_title(owner: Any, entry: ResponseModelCacheEntry | None) -> None:
    if entry is None:
        return
    entry.pending_titles = max(0, entry.pending_titles - 1)
    if entry.retire_requested:
        schedule_response_model_retire(owner, entry)


def wrap_response_model_title_work(
    owner: Any,
    client: Any,
    session_id: Any,
    work: Any,
) -> Any:
    """Keep a cached client retained until deferred title work finishes."""

    entry = _entry_for_client(owner, client, session_id=session_id)
    retain_response_model_title(entry)

    async def wrapped():
        try:
            result = work() if callable(work) else work
            if inspect.isawaitable(result):
                return await result
            return result
        finally:
            release_response_model_title(owner, entry)

    return wrapped


def _active_ticket_values(client: Any, name: str) -> Iterable[Any]:
    values = getattr(client, name, ())
    if not isinstance(values, (set, tuple, list, frozenset)):
        return ()
    return tuple(
        value
        for value in values
        if not bool(getattr(value, "_released", False))
    )


def response_model_client_is_idle(client: Any) -> bool:
    if client is None:
        return True
    try:
        # Provider completion/finally code can discard a ticket on another
        # thread while this snapshot is being taken.  A concurrent-set error
        # means busy for this poll; the drain loop retries safely.
        if tuple(_active_ticket_values(client, "_llama_cpp_generation_lease_tickets")):
            return False
        workers = getattr(client, "_ephemeral_workers", ())
        if isinstance(workers, (set, tuple, list, frozenset)) and any(
            not bool(getattr(event, "is_set", lambda: True)()) for event in workers
        ):
            return False
    except RuntimeError:
        return False
    return True


async def _drain_and_release(owner: Any, entry: ResponseModelCacheEntry) -> None:
    try:
        # Counter state is owned by the assistant event loop.  A route change
        # may race the turn/title setup before a provider generation ticket
        # exists, so ticket-idle alone is insufficient for safe retirement.
        while (
            entry.active_uses > 0
            or entry.pending_titles > 0
            or not response_model_client_is_idle(entry.client)
        ):
            await asyncio.sleep(0.05)
        # This is the single lifecycle boundary for a cached local client.
        # It marks the client retired before releasing only its consumer
        # tickets, so a later provider-preparation pass cannot re-pin it.
        releaser = getattr(
            entry.client,
            "_retire_managed_local_runtime_consumer_lease",
            None,
        )
        if callable(releaser):
            if inspect.iscoroutinefunction(releaser):
                await releaser()
            else:
                result = await asyncio.to_thread(releaser)
                if inspect.isawaitable(result):
                    await result
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        entry.release_error = str(exc)
        logger.warning("Response-model consumer retirement failed", exc_info=True)
        return

    entries = _cache_entries(owner)
    current = entries.get(entry.key)
    if current is entry:
        entries.pop(entry.key, None)
        clients = getattr(owner, "_response_model_clients", None)
        if isinstance(clients, dict) and clients.get(entry.key) is entry.client:
            clients.pop(entry.key, None)


def schedule_response_model_retire(owner: Any, entry: ResponseModelCacheEntry) -> None:
    if entry.release_task is not None and not entry.release_task.done():
        return
    try:
        entry.release_task = asyncio.create_task(
            _drain_and_release(owner, entry),
            name="response-model-consumer-retire",
        )
    except RuntimeError:
        # No running loop means a synchronous test/embedding caller cannot
        # drain an active client. Keep the retired entry fail-closed.
        entry.release_task = None


async def retire_response_model_session(owner: Any, session_id: Any) -> dict[str, int]:
    target = normalize_response_model_session_id(session_id)
    entries = _cache_entries(owner)
    selected = [entry for entry in entries.values() if entry.session_id == target]
    for entry in selected:
        entry.retire_requested = True
        schedule_response_model_retire(owner, entry)
    await asyncio.gather(
        *(
            entry.release_task
            for entry in selected
            if (
                entry.release_task is not None
                and entry.active_uses == 0
                and entry.pending_titles == 0
                and response_model_client_is_idle(entry.client)
            )
        ),
        return_exceptions=True,
    )
    return {
        "retired": sum(
            1
            for entry in selected
            if entry.key not in _cache_entries(owner)
        ),
        "deferred": sum(
            1
            for entry in selected
            if entry.key in _cache_entries(owner)
        ),
        "errors": sum(1 for entry in selected if entry.release_error),
    }


__all__ = [
    "ResponseModelCacheEntry",
    "begin_response_model_use",
    "cached_response_model_client",
    "current_response_model_cache_session",
    "end_response_model_use",
    "normalize_response_model_session_id",
    "remember_response_model_client",
    "release_response_model_title",
    "response_model_cache_key",
    "response_model_cache_session",
    "response_model_client_is_idle",
    "retire_response_model_session",
    "retain_response_model_title",
    "wrap_response_model_title_work",
]
