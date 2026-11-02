"""Lazy Playwright bridge creation over the current dedicated broker endpoint."""

from __future__ import annotations

import threading
from typing import Any, Callable

from .broker import managed_browser_endpoint
from .status import BrowserStatus


class LocalBrowserUnavailable(RuntimeError):
    def __init__(self, code: str = "browser_unavailable") -> None:
        if code != "browser_unavailable":
            code = "browser_unavailable"
        self.code = code
        super().__init__(code)


class LocalBrowserRuntime:
    def __init__(
        self,
        *,
        status_provider: Callable[[], BrowserStatus] | None = None,
        endpoint_provider: Callable[[], str | None] | None = None,
        backend_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._status_provider = status_provider or _managed_status
        self._endpoint_provider = endpoint_provider or managed_browser_endpoint
        self._backend_factory = backend_factory
        self._lock = threading.RLock()
        self._backend: Any = None
        self._endpoint: str | None = None

    def get_backend(self) -> Any:
        status = self._status_provider()
        endpoint = self._endpoint_provider()
        if not status.ready or not status.cdp_connected or not endpoint:
            with self._lock:
                self._close_backend()
                self._backend = None
                self._endpoint = None
            raise LocalBrowserUnavailable()
        with self._lock:
            if self._backend is None or endpoint != self._endpoint or _is_blocked(self._backend):
                # A faulted/closed adapter stays faulted by design (the PC Bridge builds a
                # fresh one on reconnect), so replace it instead of failing every later call.
                self._close_backend()
                self._backend = self._create_backend(endpoint)
                self._endpoint = endpoint
            return self._backend

    def close(self) -> None:
        with self._lock:
            self._close_backend()
            self._backend = None
            self._endpoint = None

    def _create_backend(self, endpoint: str) -> Any:
        factory = self._backend_factory
        if factory is None:
            # Importing this module must not pull in desktop/Windows helpers.
            from pc_bridge.browser_backend import create_browser_backend

            factory = create_browser_backend
        return factory({"browser_backend": "agent_browser", "agent_browser": {"broker_endpoint": endpoint}})

    def _close_backend(self) -> None:
        backend = self._backend
        if backend is None:
            return
        close = getattr(backend, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def _is_blocked(backend: Any) -> bool:
    diagnostics = getattr(backend, "diagnostics", None)
    if not callable(diagnostics):
        return False
    try:
        return diagnostics().get("blocked") is True
    except Exception:
        return False


def _managed_status() -> BrowserStatus:
    from .broker import managed_browser_broker_status

    return managed_browser_broker_status()
