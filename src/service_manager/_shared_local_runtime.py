"""Default shared ownership at the existing service_manager lifecycle boundary.

The direct core remains the coordinator's backend and an explicit unit-test seam.
Neither remote status descriptors nor coordinator Popen objects enter child tracking.
"""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
from functools import wraps
import logging
import threading
import time

from src.shared_local_runtime import registry
from src.shared_local_runtime.backend import is_managed
from src.shared_local_runtime.client import (
    Client,
    RemoteProcessStatus,
    RemoteTicket,
    SharedRuntimeAcquisitionSuperseded,
    SharedRuntimeError,
)

log = logging.getLogger(__name__)

_client = None
_lock = threading.RLock()
_route_lock = threading.RLock()
_selections = {}
_routes = {}
# Pending route warm-ups that a newer selection may cancel, keyed by id(entry).
_pending_routes = {}
_SUPERSEDABLE_ROUTE = contextvars.ContextVar(
    "aoitalk_shared_runtime_supersedable_route",
    default=False,
)
# Bounded waits: a released idle runtime normally exits within seconds, and a
# cancelled cold start aborts at the broker's next readiness poll.
_IDLE_STOP_WAIT_SECONDS = 60.0
_PENDING_ROUTE_CANCEL_WAIT_SECONDS = 60.0
_STATUS_POLL_SECONDS = 0.25


def client():
    global _client
    with _lock:
        if _client is None or _client.closed.is_set():
            _client = Client()
        return _client


@contextmanager
def supersedable_route_warmup():
    """Mark the enclosed route ensure as a cancellable selection warm-up.

    A conversation route PUT warms its selected model before any turn exists.
    That warm-up is bridge-owned retention, not a consumer: a later selection
    or a stop of the same scope may cancel it while it is still cold-starting.
    Turn, meeting and Main-switch ensures stay outside this context and are
    never cancelled by another selection.
    """

    token = _SUPERSEDABLE_ROUTE.set(True)
    try:
        yield
    finally:
        _SUPERSEDABLE_ROUTE.reset(token)


class _RouteCancellation:
    """Bridge-owned cancellation capability for one pending warm-up."""

    def __init__(self):
        self.cancel_requested = threading.Event()
        self.superseded = False
        self._lock = threading.Lock()
        self._callbacks = []

    def add_cancel_callback(self, callback):
        with self._lock:
            invoke = self.cancel_requested.is_set()
            if not invoke:
                self._callbacks.append(callback)
        if invoke:
            callback()

        def remove():
            with self._lock:
                self._callbacks = [item for item in self._callbacks if item is not callback]

        return remove

    def request_cancel(self, *, superseded=False):
        with self._lock:
            if superseded:
                self.superseded = True
            self.cancel_requested.set()
            callbacks = list(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                log.debug("route warm-up cancellation callback failed", exc_info=True)


def _client_acquire(profile_id, *, route_cancellation=None, **kwargs):
    """Pass the current Run cancellation capability when one is bound.

    A supersedable warm-up uses its own route capability.  A Run stop bound to
    the same thread is still forwarded to it, so neither owner loses the
    ability to cancel the pending broker request.
    """
    from src.llm.generation_cancellation import get_current_generation_cancellation

    cancel_handle = get_current_generation_cancellation()
    if route_cancellation is None:
        if cancel_handle is not None:
            kwargs["cancel_handle"] = cancel_handle
        return client().acquire(profile_id, **kwargs)
    remove = None
    if cancel_handle is not None:
        remove = cancel_handle.add_cancel_callback(route_cancellation.request_cancel)
    try:
        return client().acquire(profile_id, cancel_handle=route_cancellation, **kwargs)
    finally:
        if remove is not None:
            remove()


def _selection(core, config, model=None, overrides=None):
    model = str(model or core._openai_compatible_local_model_id(config) or "").strip()
    if not is_managed(config, model, core):
        return None
    snapshot_id = registry.register(config, model, overrides, core=core)
    return model, snapshot_id


def _remember(reply, model, snapshot_id):
    with _lock:
        _selections[(reply["base_url"], reply["model"])] = (model, snapshot_id)


def _existing_consumer_ticket(
    holder,
    *,
    endpoint=None,
    model=None,
    snapshot_id=None,
    launch_fingerprint=None,
):
    """Reuse one live remote consumer ticket per target holder.

    ``OpenAICompatibleLocalClient`` asks the service manager to ensure its
    endpoint before every request.  That request-level readiness check must
    not turn a long-lived client into an unbounded sequence of broker
    consumers.  Restrict reuse to this holder, the exact profile, and (when
    known) the normalized endpoint; no ticket owned by another client is
    adopted.
    """

    if holder is None:
        return None
    tickets = getattr(holder, "_llama_cpp_runtime_consumer_lease_tickets", None)
    if not isinstance(tickets, set):
        return None
    normalized_endpoint = None
    if endpoint:
        try:
            normalized_endpoint = str(endpoint).rstrip("/")
        except Exception:
            normalized_endpoint = None
    expected_model = str(model or "").strip()
    try:
        candidates = tuple(tickets)
    except RuntimeError:
        # A release may remove a ticket concurrently.  Failing closed here
        # lets the caller acquire a fresh lease instead of adopting an
        # ambiguous object from a changing holder set.
        return None
    for ticket in candidates:
        if not isinstance(ticket, RemoteTicket) or ticket.kind != "consumer" or ticket._released:
            continue
        if getattr(ticket, "_holder", holder) is not holder:
            continue
        owner = getattr(ticket, "client", None)
        if owner is None:
            continue
        closed = getattr(owner, "closed", False)
        if callable(getattr(closed, "is_set", None)):
            if closed.is_set():
                continue
        elif closed:
            continue
        owned_tickets = getattr(owner, "tickets", None)
        if isinstance(owned_tickets, dict) and owned_tickets.get(ticket.ticket_id) is not ticket:
            continue
        current_broker_id = getattr(owner, "broker_id", None)
        if current_broker_id is not None and ticket.broker_id != current_broker_id:
            continue
        if expected_model and ticket.profile_id != expected_model:
            continue
        if normalized_endpoint and str(ticket.endpoint).rstrip("/") != normalized_endpoint:
            continue
        if snapshot_id is not None and getattr(ticket, "snapshot_id", None) != snapshot_id:
            continue
        if launch_fingerprint is not None and getattr(ticket, "launch_fingerprint", None) != launch_fingerprint:
            continue
        return ticket
    return None


def _refresh_existing_consumer_ticket(
    holder,
    *,
    endpoint=None,
    model=None,
    snapshot_id=None,
    launch_fingerprint=None,
):
    """Revalidate launch identity and broker ownership before reuse."""

    existing = _existing_consumer_ticket(
        holder,
        endpoint=endpoint,
        model=model,
        snapshot_id=snapshot_id,
        launch_fingerprint=launch_fingerprint,
    )
    if existing is None:
        return None
    if launch_fingerprint is None:
        validate = getattr(existing.client, "validate", None)
        if not callable(validate) or snapshot_id is None or not model:
            return None
        result = validate(model, snapshot_id)
        launch_fingerprint = (
            result.get("launch_fingerprint")
            if isinstance(result, dict)
            else None
        )
        if not launch_fingerprint:
            return None
        existing = _existing_consumer_ticket(
            holder,
            endpoint=endpoint,
            model=model,
            snapshot_id=snapshot_id,
            launch_fingerprint=launch_fingerprint,
        )
        if existing is None:
            return None
    verify = getattr(existing.client, "verify_ticket", None)
    if not callable(verify) or not verify(existing):
        return None
    return _existing_consumer_ticket(
        holder,
        endpoint=endpoint,
        model=model,
        snapshot_id=snapshot_id,
        launch_fingerprint=launch_fingerprint,
    )


def _tag_ticket(ticket, snapshot_id, reply=None):
    """Retain frozen identity even with a lightweight client test double."""

    if not isinstance(ticket, RemoteTicket):
        return ticket
    ticket.snapshot_id = snapshot_id
    if isinstance(reply, dict) and reply.get("launch_fingerprint"):
        ticket.launch_fingerprint = reply["launch_fingerprint"]
    return ticket


def _snapshot_endpoint(core, model, snapshot_id):
    """Read the already-frozen endpoint for holder-ticket deduplication."""

    try:
        frozen, _ = registry.read(model, snapshot_id)
        endpoint = frozen.get("openai_compatible_local.base_url")
        if endpoint:
            return core._normalize_llama_cpp_lease_endpoint(endpoint)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def _lookup(core, endpoint, alias):
    endpoint = core._normalize_llama_cpp_lease_endpoint(endpoint)
    with _lock:
        selected = _selections.get((endpoint, alias))
    if selected is None:
        raise SharedRuntimeError("Managed shared runtime must be ensured before acquiring an endpoint lease")
    return selected


def _status_rows(*, required=False):
    try:
        return client().status()["runtimes"]
    except SharedRuntimeError:
        if required:
            raise
        return []


def _descriptor(row):
    return RemoteProcessStatus(
        pid=row["pid"], _client=client(), _fingerprint=row["launch_fingerprint"],
        _aoi_local_runtime=row["runtime"], _aoi_local_model_alias=row["model"],
        _aoi_local_base_url=row["base_url"], _aoi_llama_cpp_base_url=row["base_url"],
        _aoi_local_executable=row.get("executable", ""))


def _release_routes(*, endpoint=None, model=None, runtime=None):
    with _lock:
        pending = list(_routes.items())
    for key, ticket in pending:
        if endpoint and ticket.endpoint != endpoint:
            continue
        if model and model not in {ticket.profile_id, ticket.alias}:
            continue
        if runtime:
            rows = _status_rows()
            if not any(row["base_url"] == ticket.endpoint and row["runtime"] == runtime for row in rows):
                continue
        ticket.release()
        with _lock:
            _routes.pop(key, None)


def _normalized_endpoint(core, endpoint):
    if not endpoint:
        return None
    try:
        return core._normalize_llama_cpp_lease_endpoint(endpoint)
    except Exception:
        return str(endpoint).rstrip("/")


def _cancel_pending_routes(predicate):
    """Cancel matching pending warm-ups and return their completion events.

    The mark happens under ``_lock`` so a warm-up which has just received its
    broker reply either sees the cancellation before publishing its retention
    or is already published (and is then released by the caller's normal
    ``_release_routes`` pass).  Only bridge-owned warm-ups are registered
    here; holder, generation and turn acquisitions are never touched.
    """

    with _lock:
        selected = [entry for entry in _pending_routes.values() if predicate(entry)]
        for entry in selected:
            entry["cancelled"] = True
    for entry in selected:
        entry["cancellation"].request_cancel(superseded=True)
    return [entry["done"] for entry in selected]


def _wait_events(events, timeout):
    deadline = time.monotonic() + timeout
    for event in events:
        if not event.wait(max(0.0, deadline - time.monotonic())):
            log.warning("Cancelled local runtime warm-up did not finish within %.0fs", timeout)
            return


def _wait_for_released_runtimes(core, endpoints, cancel_handle=None):
    """Wait until runtimes whose idle retention was released have stopped.

    A runtime still held by another consumer or generation is never waited
    for: that owner keeps it, and the cold start proceeds as before.
    """

    deadline = time.monotonic() + _IDLE_STOP_WAIT_SECONDS
    while True:
        waiting = [
            row for row in _status_rows()
            if _normalized_endpoint(core, row.get("base_url")) in endpoints
            and row.get("running")
            and not row.get("consumers")
            and not row.get("generations")
        ]
        if not waiting:
            return
        cancelled = cancel_handle is not None and cancel_handle.cancel_requested.is_set()
        if cancelled or time.monotonic() >= deadline:
            if not cancelled:
                log.warning("Idle local runtime did not stop before the next cold start")
            return
        time.sleep(_STATUS_POLL_SECONDS)


def _make_room_for_cold_start(core, endpoint, *, cancel_handle=None):
    """Drop this bridge's idle route retention before loading another model.

    Route retention only keeps the last selected model warm while nothing
    uses it.  When ``endpoint`` is not running, a cold load is about to start;
    releasing retention on other endpoints lets an idle model unload first
    instead of sharing VRAM with the new one.  Consumer and generation
    tickets (conversation turns, meetings, Agent clients, other processes)
    are not owned by this table and keep their runtime alive.  Returns True
    when ``endpoint`` was cold.
    """

    target = _normalized_endpoint(core, endpoint)
    if not target:
        return False
    try:
        rows = client().status()["runtimes"]
    except SharedRuntimeError:
        # No reachable broker means no running worker: the acquire starts it.
        rows = []
    except Exception:
        # A status reply that cannot be read does not prove a cold start;
        # keep the current residency rather than dropping retention blindly.
        log.debug("Shared runtime status was unreadable before acquisition", exc_info=True)
        return False
    if any(
        _normalized_endpoint(core, row.get("base_url")) == target and row.get("running")
        for row in rows
    ):
        return False
    with _lock:
        others = [
            (key, ticket) for key, ticket in _routes.items()
            if _normalized_endpoint(core, ticket.endpoint) != target
        ]
    released = set()
    for key, ticket in others:
        try:
            ticket.release()
        except SharedRuntimeError:
            log.warning("Idle local runtime retention could not be released", exc_info=True)
            continue
        with _lock:
            if _routes.get(key) is ticket:
                _routes.pop(key, None)
        released.add(_normalized_endpoint(core, ticket.endpoint))
    if released:
        _wait_for_released_runtimes(core, released, cancel_handle)
    return True


def install(core):
    core._shared_runtime_direct_backend = False

    def wrap(name, dispatch):
        original = getattr(core, name)

        @wraps(original)
        def forwarded(*args, **kwargs):
            if core._shared_runtime_direct_backend:
                return original(*args, **kwargs)
            return dispatch(original, *args, **kwargs)

        setattr(core, name, forwarded)

    def _route_endpoint(config, model):
        identity = core._llama_cpp_generation_lease_identity(config, model=model)
        return identity[0] if identity else None

    def ensure(original, config, **kwargs):
        selected = _selection(core, config, kwargs.get("model"))
        if selected is None:
            return original(config, **kwargs)
        model, snapshot_id = selected
        generation = kwargs.get("acquire_generation_lease", False)
        if generation:
            from src.llm.generation_cancellation import get_current_generation_cancellation

            cold = _make_room_for_cold_start(
                core,
                _route_endpoint(config, model),
                cancel_handle=get_current_generation_cancellation(),
            )
            ticket, reply = _client_acquire(model, kind="generation", snapshot_id=snapshot_id,
                holder=kwargs.get("lease_holder"), force_restart=kwargs.get("force_restart", False),
                wait_for_model_ready=kwargs.get("wait_for_model_ready", True))
            _tag_ticket(ticket, snapshot_id, reply)
            _remember(reply, model, snapshot_id)
            tickets = kwargs.get("lease_tickets")
            if tickets is not None:
                tickets.append(ticket)
            elif kwargs.get("lease_holder") is None:
                ticket.release()
            if cold:
                retain_loaded_route(config, model, snapshot_id)
            return reply["started"]
        return ensure_route(config, model, snapshot_id, kwargs)

    def retain_loaded_route(config, model, snapshot_id):
        """Keep a model this request just loaded warm until another needs VRAM."""
        token = _SUPERSEDABLE_ROUTE.set(False)
        try:
            ensure_route(config, model, snapshot_id, {})
        except Exception:
            # The request already owns its generation lease; losing only the
            # idle retention means the next request reloads the model.
            log.warning("Loaded local runtime could not be retained as warm", exc_info=True)
        finally:
            _SUPERSEDABLE_ROUTE.reset(token)

    def ensure_route(config, model, snapshot_id, kwargs):
        key = (model, snapshot_id)
        force_restart = bool(kwargs.get("force_restart"))
        endpoint = _route_endpoint(config, model)
        cancellation = _RouteCancellation() if _SUPERSEDABLE_ROUTE.get() else None
        entry = None
        with _route_lock:
            with _lock:
                existing = _routes.get(key)
            # Reuse only our own route retention; the broker checks health on generation.
            if existing and not existing._released and not force_restart:
                client().connect()
                return False
            if cancellation is not None:
                # A newer selection makes older still-loading warm-ups obsolete.
                _cancel_pending_routes(lambda pending: pending["key"] != key)
            if endpoint:
                # Only this bridge's main-route retention is replaceable here. Explicit
                # session/agent/request tickets, including our own, remain protected.
                _release_routes(endpoint=endpoint)
            if cancellation is not None:
                try:
                    runtime = core.managed_local_runtime_for_model(config, model) or "llama_cpp"
                except Exception:
                    runtime = "llama_cpp"
                entry = {"key": key, "model": model, "runtime": runtime,
                         "endpoint": _normalized_endpoint(core, endpoint),
                         "cancellation": cancellation, "cancelled": False,
                         "done": threading.Event()}
                with _lock:
                    _pending_routes[id(entry)] = entry
        try:
            # The broker request runs outside _route_lock so a newer selection
            # or a scoped stop can cancel this cold start instead of queueing.
            _make_room_for_cold_start(core, endpoint, cancel_handle=cancellation)
            try:
                ticket, reply = _client_acquire(model, snapshot_id=snapshot_id,
                    force_restart=force_restart, wait_for_model_ready=kwargs.get("wait_for_model_ready", True),
                    route_cancellation=cancellation)
            except SharedRuntimeError as exc:
                if cancellation is not None and cancellation.superseded:
                    raise SharedRuntimeAcquisitionSuperseded(
                        "Local runtime warm-up was cancelled by a newer selection") from exc
                raise
            _tag_ticket(ticket, snapshot_id, reply)
            _remember(reply, model, snapshot_id)
            with _lock:
                if entry is not None:
                    _pending_routes.pop(id(entry), None)
                cancelled = entry is not None and (entry["cancelled"] or cancellation.cancel_requested.is_set())
                current = _routes.get(key)
                duplicate = current is not None and current is not ticket and not current._released
                if not cancelled and not duplicate:
                    _routes[key] = ticket
            if cancelled or duplicate:
                ticket.release()
            if cancelled:
                raise SharedRuntimeAcquisitionSuperseded(
                    "Local runtime warm-up was cancelled by a newer selection")
            return reply["started"]
        finally:
            if entry is not None:
                with _lock:
                    _pending_routes.pop(id(entry), None)
                entry["done"].set()

    def validate(original, config, **kwargs):
        if kwargs.get("provider") not in {None, "openai_compatible_local"}:
            return original(config, **kwargs)
        selected = _selection(core, config, kwargs.get("model"), kwargs.get("llama_cpp_settings"))
        if selected is None:
            return original(config, **kwargs)
        client().validate(*selected, force_restart=kwargs.get("force_restart", False))

    def route_consumer(original, config, *, provider, model, consumer, holder=None):
        if provider != "openai_compatible_local":
            return None
        selected = _selection(core, config, model)
        if selected is None:
            return original(config, provider=provider, model=model, consumer=consumer, holder=holder)
        existing = _refresh_existing_consumer_ticket(
            holder,
            endpoint=_snapshot_endpoint(core, selected[0], selected[1]),
            model=selected[0],
            snapshot_id=selected[1],
        )
        if existing is not None:
            existing.consumer = consumer
            return existing
        _make_room_for_cold_start(core, _snapshot_endpoint(core, selected[0], selected[1]))
        ticket, reply = _client_acquire(selected[0], snapshot_id=selected[1], holder=holder)
        _tag_ticket(ticket, selected[1], reply)
        ticket.consumer = consumer
        _remember(reply, *selected)
        return ticket

    def acquire_generation(original, endpoint, alias, holder=None):
        model, snapshot_id = _lookup(core, endpoint, alias)
        _make_room_for_cold_start(core, endpoint)
        ticket, reply = _client_acquire(model, snapshot_id=snapshot_id, kind="generation", holder=holder)
        _tag_ticket(ticket, snapshot_id, reply)
        return ticket

    def acquire_consumer(original, endpoint, alias, *, consumer="consumer", holder=None):
        model, snapshot_id = _lookup(core, endpoint, alias)
        existing = _refresh_existing_consumer_ticket(
            holder,
            endpoint=core._normalize_llama_cpp_lease_endpoint(endpoint),
            model=model,
            snapshot_id=snapshot_id,
        )
        if existing is not None:
            existing.consumer = consumer
            return existing
        _make_room_for_cold_start(core, endpoint)
        ticket, reply = _client_acquire(model, snapshot_id=snapshot_id, holder=holder)
        _tag_ticket(ticket, snapshot_id, reply)
        ticket.consumer = consumer
        return ticket

    def release_ticket(original, ticket):
        if isinstance(ticket, RemoteTicket):
            ticket.release()
            return
        return original(ticket)

    def release_legacy(original, endpoint, alias):
        with client().lock:
            candidates = [t for t in client().tickets.values() if t.kind == "generation" and t.endpoint == endpoint and t.alias == alias]
        if candidates:
            candidates[0].release()
        else:
            return original(endpoint, alias)

    def stop(original, **kwargs):
        endpoint = _normalized_endpoint(core, kwargs.get("endpoint"))
        model = kwargs.get("model")
        runtime = kwargs.get("runtime")
        # Stopping a scope also cancels its still-loading selection warm-up;
        # otherwise the broker refuses the stop and the warm-up later publishes
        # retention for a route nobody selects any more.
        _wait_events(
            _cancel_pending_routes(
                lambda pending: (not endpoint or pending["endpoint"] == endpoint)
                and (not model or pending["model"] == model)
                and (not runtime or pending["runtime"] == runtime)
            ),
            _PENDING_ROUTE_CANCEL_WAIT_SECONDS,
        )
        try:
            _release_routes(endpoint=kwargs.get("endpoint"), model=kwargs.get("model"), runtime=kwargs.get("runtime"))
            # Stopping is a read/stop operation; it must never bootstrap a broker.
            client().connect(start_if_missing=False)
            rows = _status_rows(required=True)
        except SharedRuntimeError:
            if kwargs.get("defer_on_timeout"):
                return 0
            raise
        targets = [row for row in rows if (not kwargs.get("endpoint") or kwargs["endpoint"] == row["base_url"])
                   and (not kwargs.get("model") or kwargs["model"] in {row["model"], row["profile_id"]})
                   and (not kwargs.get("runtime") or kwargs["runtime"] == row["runtime"])]
        count = 0
        for row in targets:
            try:
                result = client()._request("/v1/stop", {"client_id": client().client_id,
                    "broker_id": client().broker_id, "client_pid": client().pid, "profile_id": row["profile_id"]})
                count += result["stopped"]
            except SharedRuntimeError:
                if not kwargs.get("defer_on_timeout"):
                    raise
        return count

    def shutdown(original):
        if _client is not None:
            _client.close()
        with _lock:
            _routes.clear()
        return original()

    def owned(original, endpoint, alias=None, *, runtime=None):
        endpoint = core._normalize_llama_cpp_lease_endpoint(endpoint)
        for row in _status_rows():
            if row["base_url"] == endpoint and (alias is None or row["model"] == alias) and (runtime is None or row["runtime"] == runtime) and row.get("running"):
                return _descriptor(row)
        return None

    def peek(original, endpoint, kind):
        endpoint = core._normalize_llama_cpp_lease_endpoint(endpoint)
        for row in _status_rows():
            if row["base_url"] == endpoint:
                return row["model"], row[kind]
        return "", 0

    def processes_for_scope(original, *, runtime=None, model=None, endpoint=None):
        return [_descriptor(row) for row in _status_rows() if row.get("running")
                and (not runtime or row["runtime"] == runtime)
                and (not model or model in {row["model"], row["profile_id"]})
                and (not endpoint or row["base_url"] == core._normalize_llama_cpp_lease_endpoint(endpoint))]

    def terminate(original, process):
        if isinstance(process, RemoteProcessStatus):
            raise SharedRuntimeError("Shared worker termination must go through coordinator leases")
        return original(process)

    def stop_strata(original, config, *, model):
        if not is_managed(config, model, core):
            return original(config, model=model)
        return bool(stop(original, runtime="strata", model=model,
                         endpoint=core.strata_base_url(config, model=model)))

    wrap("_terminate_process_tree", terminate)
    wrap("_owned_processes_for_scope", processes_for_scope)
    wrap("_owned_strata_processes_for_model", lambda original, model: processes_for_scope(original, runtime="strata", model=model))
    wrap("_iter_owned_llama_cpp_lease_endpoints", lambda original: [row["base_url"] for row in _status_rows() if row.get("running")])
    wrap("_stop_owned_openai_compatible_local_servers_if_unleased", lambda original, endpoint: stop(original, endpoint=endpoint))
    wrap("stop_strata_server", stop_strata)
    wrap("ensure_openai_compatible_local_server", ensure)
    wrap("validate_openai_compatible_local_launch_selection", validate)
    wrap("_acquire_managed_local_runtime_consumer_for_route", route_consumer)
    wrap("_acquire_llama_cpp_runtime_consumer_lease", acquire_consumer)
    wrap("_acquire_llama_cpp_generation_lease", acquire_generation)
    wrap("_release_llama_cpp_runtime_consumer_lease", release_ticket)
    wrap("_release_llama_cpp_generation_lease_ticket", release_ticket)
    wrap("_release_llama_cpp_generation_lease", release_legacy)
    wrap("stop_owned_openai_compatible_local_servers_respecting_generation_leases", stop)
    wrap("stop_openai_compatible_local_servers", lambda original: stop(original, defer_on_timeout=True))
    wrap("kill_services", shutdown)
    wrap("_owned_llama_cpp_process_at", lambda original, base_url, expected_alias: owned(original, base_url, expected_alias, runtime="llama_cpp"))
    wrap("_owned_local_process_at", lambda original, base_url, *, runtime: bool(owned(original, base_url, runtime=runtime)))
    wrap("_owned_strata_process_at", lambda original, base_url: owned(original, base_url, runtime="strata"))
    wrap("_peek_llama_cpp_generation_lease", lambda original, endpoint: peek(original, endpoint, "generations"))
    wrap("_peek_llama_cpp_runtime_consumer_lease", lambda original, endpoint: peek(original, endpoint, "consumers"))
    # Shared stops are derived from zero remote tickets rather than a client-local
    # deferred queue. Returning to a route acquires a new consumer atomically.
    wrap("_cancel_deferred_llama_cpp_stops", lambda original, **kwargs: 0)
