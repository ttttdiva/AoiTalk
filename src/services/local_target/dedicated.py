"""In-process implementation of the dedicated browser Bridge surface.

The Bridge-owned browser implementation stays the source of truth.  This
adapter creates it lazily in the AoiTalk server process and keeps its blocking
driver calls off the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


LOCAL_DEVICE_ID = "local"
_PROCESS_EPOCH = uuid4().hex
_CLEANUP_COMMANDS = frozenset({"end_run", "cancel_login"})
_CALLBACK_KINDS = frozenset({"jev_ask", "jev_gate", "jev_type"})
_CODE_PATTERN = re.compile(r"(?:profile_driver|browser_profile|agent_browser|bridge_jev)_[a-z0-9_]{1,96}\Z")
_GATE_POLL_SECONDS = 0.05


class _LocalDedicatedState:
    def __init__(self, backend_factory: Callable[[], Any] | None) -> None:
        self.backend_factory = backend_factory
        self.backend: Any = None
        self.operation_lock = threading.Lock()
        self.backend_lock = threading.RLock()
        self.lifecycle_lock = threading.RLock()
        self.callback_lock = threading.Lock()
        self.callback_futures: set[Future[Any]] = set()
        self.connection_epoch = _PROCESS_EPOCH


_STATES_LOCK = threading.RLock()
_STATES: dict[str, _LocalDedicatedState] = {}


def _dedicated_commands() -> frozenset[str]:
    # Keep the closed command contract beside the Bridge protocol.  This import
    # is intentionally deferred along with the rest of the dedicated runtime.
    from pc_bridge.browser_commands import DEDICATED_BROWSER_COMMANDS

    return frozenset(DEDICATED_BROWSER_COMMANDS) - {"jev_run"}


def _edge_error(code: str) -> Exception:
    from pc_bridge.edge import EdgeBridgeError

    return EdgeBridgeError(code)


def _fixed_error(exc: BaseException) -> Exception:
    if isinstance(exc, TimeoutError):
        return _edge_error("profile_driver_outcome_unknown")
    code = getattr(exc, "code", None)
    if not isinstance(code, str):
        # EdgeBridgeError intentionally carries only its fixed code in str().
        code = str(exc)
    if not isinstance(code, str) or _CODE_PATTERN.fullmatch(code) is None:
        code = "profile_driver_command_failed"
    return _edge_error(code)


def _current_dedicated_enabled() -> bool:
    """Read the live server gate, whose route binding reads server.config."""
    try:
        from ..pc_bridge_service import get_pc_bridge_hub

        return get_pc_bridge_hub().dedicated_enabled() is True
    except Exception:
        return False


def _profile_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise _edge_error("profile_driver_profile_root_unavailable")
    base = Path(local_app_data)
    if not base.is_absolute():
        raise _edge_error("profile_driver_profile_root_unavailable")
    return base / "AoiTalk" / "dedicated-profile"


class LocalDedicatedBridge:
    """Dedicated-profile browser with the same consumer-facing surface as the remote bridge."""

    browser_backend = "owned_profile"
    device_id = LOCAL_DEVICE_ID
    hub = None
    _connection = None
    media_preparation_backend = None
    supports_material_state_guard = True

    def __init__(
        self,
        owner: str | None = None,
        *,
        cleanup_only: bool = False,
        enabled_provider: Callable[[], bool] | None = None,
        backend_factory: Callable[[], Any] | None = None,
    ) -> None:
        if owner is None:
            try:
                from ..turn_context import get_turn_context

                owner = get_turn_context().user_id
            except Exception:
                owner = None
        self.owner = str(owner) if owner else ""
        self.cleanup_only = cleanup_only is True
        self._enabled_provider = enabled_provider if enabled_provider is not None else _current_dedicated_enabled
        if not self.owner:
            self._state = None
            self.operation_lock = threading.Lock()
            return
        with _STATES_LOCK:
            state = _STATES.get(self.owner)
            if state is None:
                state = _LocalDedicatedState(backend_factory)
                _STATES[self.owner] = state
            self._state = state
        self.operation_lock = state.operation_lock

    @property
    def connection_epoch(self) -> str:
        self._check_connection()
        return self._state.connection_epoch

    def _check_connection(self) -> None:
        if not self.owner or self._state is None:
            raise _edge_error("profile_driver_owner_unverified")

    def _is_enabled(self) -> bool:
        try:
            return self._enabled_provider() is True
        except Exception:
            return False

    def _get_backend(self) -> Any:
        self._check_connection()
        state = self._state
        with state.backend_lock:
            if state.backend is not None:
                return state.backend
            try:
                if state.backend_factory is not None:
                    backend = state.backend_factory()
                else:
                    backend = self._create_backend()
            except Exception as exc:
                raise _fixed_error(exc) from None
            if backend is None:
                raise _edge_error("profile_driver_backend_unavailable")
            state.backend = backend
            return backend

    def _create_backend(self) -> Any:
        # Importing this module must not import Playwright, DPAPI, the Node
        # runtime, or the Windows Job implementation.
        from pc_bridge.browser_profiles import BrowserProfileManager
        from pc_bridge.dedicated_browser_backend import DedicatedBrowserBridge

        manager = BrowserProfileManager(root=_profile_root())
        return DedicatedBrowserBridge(manager=manager)

    def _bind_backend(self, backend: Any, *, enabled: bool) -> None:
        with self._state.lifecycle_lock:
            backend.bind_connection(self.owner, self._state.connection_epoch, enabled=enabled)

    def _revoke_backend(self, backend: Any) -> None:
        with self._state.lifecycle_lock:
            suspend = getattr(backend, "suspend", None)
            if callable(suspend):
                suspend()
            backend.bind_connection(self.owner, self._state.connection_epoch, enabled=False)

    def _cancel_callback_futures(self) -> None:
        state = self._state
        with state.callback_lock:
            pending = tuple(state.callback_futures)
        for future in pending:
            future.cancel()

    def _register_callback_future(self, future: Future[Any]) -> None:
        with self._state.callback_lock:
            self._state.callback_futures.add(future)

    def _forget_callback_future(self, future: Future[Any]) -> None:
        with self._state.callback_lock:
            self._state.callback_futures.discard(future)

    async def _run_backend_call(self, backend: Any, callback: Callable[[], Any], *, monitor_gate: bool) -> Any:
        task = asyncio.create_task(asyncio.to_thread(callback))
        revoked = False
        try:
            while monitor_gate and not task.done():
                await asyncio.wait({task}, timeout=_GATE_POLL_SECONDS)
                if task.done():
                    break
                if not self._is_enabled():
                    revoked = True
                    self._cancel_callback_futures()
                    try:
                        await asyncio.to_thread(self._revoke_backend, backend)
                    except Exception:
                        pass
                    break
            result = await task
            if revoked:
                raise _edge_error("profile_driver_dedicated_disabled")
            return result
        except asyncio.CancelledError:
            revoked = True
            self._cancel_callback_futures()
            try:
                await asyncio.to_thread(self._revoke_backend, backend)
            except Exception:
                pass
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            if revoked:
                try:
                    await asyncio.to_thread(self._bind_backend, backend, enabled=False)
                except Exception:
                    pass

    async def request(self, command: str, **params: Any) -> dict[str, Any]:
        if (not isinstance(command, str) or command not in _dedicated_commands()
                or self.cleanup_only and command not in _CLEANUP_COMMANDS):
            raise _edge_error("profile_driver_command_rejected")
        self._check_connection()
        enabled = self._is_enabled()
        backend = self._state.backend
        cleanup = command in _CLEANUP_COMMANDS
        if not enabled and backend is not None:
            try:
                await asyncio.to_thread(self._revoke_backend, backend)
            except Exception:
                if not cleanup:
                    raise _edge_error("profile_driver_command_failed") from None
        if not enabled and not cleanup:
            raise _edge_error("profile_driver_dedicated_disabled")
        if cleanup and backend is None:
            if command == "cancel_login":
                return {"cancelled": True}
            return {"ended": True, "run_id": params.get("run_id"), "refs_invalidated": True,
                    "connection_retained": False, "paused": False}
        backend = self._get_backend()
        try:
            await asyncio.to_thread(self._bind_backend, backend, enabled=enabled)
            result = await self._run_backend_call(
                backend,
                lambda: backend._request(command, params),
                monitor_gate=enabled and not cleanup,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _fixed_error(exc) from None
        if not isinstance(result, dict):
            raise _edge_error("profile_driver_response_invalid")
        return result

    async def jev_run(self, *, handler: Callable[[dict[str, Any]], Any], browser_context: dict[str, Any],
                      timeout: int, **params: Any) -> dict[str, Any]:
        if self.cleanup_only:
            raise _edge_error("bridge_jev_runtime_required")
        self._check_connection()
        if (not callable(handler) or not isinstance(browser_context, dict) or type(timeout) is not int
                or timeout <= 0 or type(params.get("tab_id")) is not int
                or not isinstance(params.get("run_id"), str)):
            raise _edge_error("profile_driver_jev_params_invalid")
        if not self._is_enabled():
            backend = self._state.backend
            if backend is not None:
                try:
                    await asyncio.to_thread(self._revoke_backend, backend)
                except Exception:
                    pass
            raise _edge_error("profile_driver_dedicated_disabled")

        backend = self._get_backend()
        await asyncio.to_thread(self._bind_backend, backend, enabled=True)
        loop = asyncio.get_running_loop()
        binding = {"run_id": params["run_id"], "tab_id": params["tab_id"],
                   "browser_context": dict(browser_context), "seen": set(), "busy": False,
                   "lock": threading.Lock()}

        def relay(kind: str, run_id: str, payload: Any) -> dict[str, Any]:
            if kind not in _CALLBACK_KINDS or run_id != binding["run_id"] or not self._is_enabled():
                raise RuntimeError("profile_driver_jev_authority_revoked")
            current = backend.jev_context()
            if (not isinstance(current, dict) or current.get("run_id") != binding["run_id"]
                    or current.get("tab_id") != binding["tab_id"]
                    or current.get("browser_context") != binding["browser_context"]):
                raise RuntimeError("profile_driver_jev_binding_invalid")
            # The driver owns the wire-local event id. Generate a separate
            # local relay id for the process callback contract.
            event_id = uuid4().hex
            with binding["lock"]:
                if event_id in binding["seen"] or len(binding["seen"]) >= 512 or binding["busy"]:
                    raise RuntimeError("profile_driver_jev_event_invalid")
                binding["seen"].add(event_id)
                binding["busy"] = True
            message = {"type": kind, "id": event_id, "run_id": run_id,
                       "owner_user_id": self.owner, "connection_epoch": self._state.connection_epoch,
                       "tab_id": binding["tab_id"], "browser_context": dict(binding["browser_context"]),
                       "version": 1, "info" if kind == "jev_gate" else "request": payload}
            try:
                if not self._is_enabled():
                    raise RuntimeError("profile_driver_jev_authority_revoked")
                async def invoke_handler() -> Any:
                    value = handler(message)
                    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
                        return await value
                    return value

                future = asyncio.run_coroutine_threadsafe(invoke_handler(), loop)
                self._register_callback_future(future)
                try:
                    result = future.result()
                finally:
                    self._forget_callback_future(future)
                if not isinstance(result, dict) or not self._is_enabled():
                    raise RuntimeError("profile_driver_jev_authority_revoked")
                return result
            finally:
                with binding["lock"]:
                    binding["busy"] = False

        # ProfileBrowserDriver calls this synchronous hook from its waiting
        # thread. The callback returns only after the async server handler has
        # answered on the owning event loop.
        def install_handler() -> None:
            setter = getattr(backend, "set_jev_handler", None)
            if not callable(setter):
                raise _edge_error("profile_driver_jev_runtime_required")
            setter(relay)

        await asyncio.to_thread(install_handler)
        try:
            async with asyncio.timeout(timeout):
                result = await self._run_backend_call(
                    backend,
                    lambda: backend._request("jev_run", params),
                    monitor_gate=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _fixed_error(exc) from None
        finally:
            try:
                await asyncio.to_thread(backend.set_jev_handler, None)
            except Exception:
                pass
        if not isinstance(result, dict):
            raise _edge_error("profile_driver_response_invalid")
        return result
