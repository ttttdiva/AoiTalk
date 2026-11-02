"""Managed attach/start/recovery lifecycle for AoiTalk's dedicated browser broker."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from collections import deque
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from .notify import OwnerNotifier
from .status import BrowserStatus, LocalBrowserState


_POLL_SECONDS = 1.0
_START_TIMEOUT_SECONDS = 20.0
_WATCH_INTERVAL_SECONDS = 15.0
_RETRY_BACKOFF_SECONDS = (5.0, 15.0, 45.0, 120.0, 300.0)
_RETRY_WINDOW_SECONDS = 600.0
_MAX_RETRIES_IN_WINDOW = 5
_LOGIN_CHECK_INTERVAL_SECONDS = 3600.0
_logger = logging.getLogger(__name__)


def is_enabled(environ: Mapping[str, str] | None = None, *, is_windows: bool | None = None) -> bool:
    env = os.environ if environ is None else environ
    windows = os.name == "nt" if is_windows is None else is_windows
    return windows and str(env.get("AOITALK_LOCAL_BROWSER", "1")).strip() != "0"


class ManagedBrowserBroker:
    """Control a detached dedicated-mode broker without owning its lifetime."""

    def __init__(
        self,
        *,
        state: LocalBrowserState | None = None,
        environ: Mapping[str, str] | None = None,
        is_windows: bool | None = None,
        repository_root: Path | None = None,
        python_executable: str | os.PathLike[str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        unix_time: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        popen: Callable[..., Any] = subprocess.Popen,
        control_factory: Callable[[], Any] | None = None,
        thread_factory: Callable[..., Any] = threading.Thread,
        event_factory: Callable[[], Any] = threading.Event,
        watcher_interval: float = _WATCH_INTERVAL_SECONDS,
        start_timeout: float = _START_TIMEOUT_SECONDS,
        notification_after_seconds: float = 600.0,
        login_notification_after_seconds: float | None = None,
        notifier: Any = None,
        login_service: Any = None,
        login_factory: Callable[[], Any] | None = None,
        notify_runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.state = state or LocalBrowserState()
        self._environ = environ
        self._is_windows = is_windows
        self._repository_root = repository_root or Path(__file__).resolve().parents[3]
        self._python_executable = Path(sys.executable if python_executable is None else python_executable)
        self._monotonic = monotonic
        self._unix_time = unix_time
        self._sleep = sleep
        self._popen = popen
        self._control_factory = control_factory
        self._thread_factory = thread_factory
        self._event_factory = event_factory
        self._watcher_interval = watcher_interval
        self._start_timeout = start_timeout
        if notification_after_seconds < 0:
            raise ValueError("browser_notification_threshold_invalid")
        if login_notification_after_seconds is not None and login_notification_after_seconds < 0:
            raise ValueError("browser_login_notification_threshold_invalid")
        self._notification_after_seconds = float(notification_after_seconds)
        self._login_notification_after_seconds = float(
            notification_after_seconds if login_notification_after_seconds is None else login_notification_after_seconds
        )
        self._notifier = notifier if notifier is not None else OwnerNotifier(
            environ=environ,
            repository_root=self._repository_root,
            unix_time=unix_time,
            runner=notify_runner,
        )
        self._login_service = login_service
        self._login_factory = login_factory
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._endpoint: str | None = None
        self._control: Any = None
        self._watcher: Any = None
        self._watcher_stop: Any = None
        self._restart_count = 0
        self._retry_failures: deque[float] = deque()
        self._consecutive_failures = 0
        self._next_retry_at: float | None = None
        self._last_broker_phase = "stopped"
        self._unhealthy_since: float | None = None
        self._notified_reason_hours: set[tuple[str, int]] = set()
        self._next_login_check_at: float | None = None
        self._login_failure_since: float | None = None
        self.login_failures = 0

    def status(self) -> BrowserStatus:
        return self.state.snapshot()

    def current_endpoint(self) -> str | None:
        """Return the in-memory endpoint only while the published state is ready."""
        with self._lock:
            snapshot = self.state.snapshot()
            return self._endpoint if snapshot.ready and snapshot.cdp_connected else None

    def start(self) -> bool:
        with self._operation_lock:
            if not self._enabled():
                self._clear_endpoint()
                self.state.publish(BrowserStatus(broker_phase="disabled"))
                self._unhealthy_since = self._monotonic()
                try:
                    self._start_watcher()
                except Exception:
                    _logger.warning("local browser watcher failed (%s)", "broker_start_failed")
                return False
            try:
                started = self._try_start(initial=True)
            except Exception:
                self._publish_failure("broker_start_failed", "unavailable", cdp=False)
                started = False
            with self._lock:
                if started:
                    self._retry_failures.clear()
                    self._consecutive_failures = 0
                    self._next_retry_at = None
                else:
                    self._next_retry_at = self._monotonic() + _RETRY_BACKOFF_SECONDS[0]
            try:
                self._start_watcher()
            except Exception:
                self._publish_failure("broker_start_failed", "unavailable", cdp=False)
            return self.status().ready

    def stop(self) -> None:
        """Stop only this process's watcher; the broker and Edge are detached owners."""
        with self._lock:
            stop_event = self._watcher_stop
            watcher = self._watcher
            self._watcher_stop = None
            self._watcher = None
        if stop_event is not None:
            stop_event.set()
        if watcher is not None and watcher is not threading.current_thread():
            join = getattr(watcher, "join", None)
            if callable(join):
                join(timeout=2.0)

    def watcher_tick(self) -> None:
        """One deterministic watcher iteration, also used by fake-only tests."""
        with self._operation_lock:
            now = self._monotonic()
            if not self._enabled():
                self._clear_endpoint()
                if self.status().broker_phase != "disabled":
                    self.state.publish(BrowserStatus(broker_phase="disabled", restart_count=self._restart_count))
            elif self._next_retry_at is None or now >= self._next_retry_at:
                self._next_retry_at = None
                self._prune_retry_window(now)
                if len(self._retry_failures) >= _MAX_RETRIES_IN_WINDOW:
                    self._publish_failure("browser_restart_limit", self._last_broker_phase, cdp=False)
                else:
                    try:
                        if self._reconcile():
                            self._consecutive_failures = 0
                            self._next_retry_at = None
                            self._retry_failures.clear()
                        else:
                            raise _BrowserLifecycleFailure(self.status().error_code or "broker_unavailable")
                    except _BrowserLifecycleFailure as exc:
                        if exc.code == "broker_mode_unknown":
                            self._publish_failure(exc.code, self._last_broker_phase, cdp=False)
                        else:
                            self._record_retry_failure(now)
                            delay_index = min(self._consecutive_failures - 1, len(_RETRY_BACKOFF_SECONDS) - 1)
                            self._next_retry_at = now + _RETRY_BACKOFF_SECONDS[delay_index]
                            self._publish_failure(exc.code, self._last_broker_phase, cdp=False)
                    except Exception:
                        self._record_retry_failure(now)
                        delay_index = min(max(self._consecutive_failures - 1, 0), len(_RETRY_BACKOFF_SECONDS) - 1)
                        self._next_retry_at = now + _RETRY_BACKOFF_SECONDS[delay_index]
                        self._publish_failure("broker_unavailable", self._last_broker_phase, cdp=False)
            self._evaluate_notifications(now)

    def _try_start(self, *, initial: bool) -> bool:
        with self._lock:
            self._control = self._control or self._new_control()
        try:
            broker_status = self._read_status()
        except Exception:
            broker_status = None
        if broker_status is not None:
            mode = getattr(broker_status, "mode", None)
            self._last_broker_phase = getattr(broker_status, "phase", "unknown")
            if mode == "dedicated":
                return self._ensure_endpoint(broker_status)
            self._publish_failure("broker_mode_unknown", self._last_broker_phase, cdp=False)
            return False

        if not self._spawn_broker(initial=initial):
            return False
        deadline = self._monotonic() + self._start_timeout
        while True:
            remaining = deadline - self._monotonic()
            if remaining < 0:
                self._publish_failure("broker_start_timeout", self._last_broker_phase, cdp=False)
                return False
            try:
                broker_status = self._read_status()
            except Exception:
                broker_status = None
            if broker_status is not None:
                self._last_broker_phase = getattr(broker_status, "phase", "unknown")
                mode = getattr(broker_status, "mode", None)
                if mode == "dedicated":
                    return self._ensure_endpoint(broker_status)
                self._publish_failure("broker_mode_unknown", self._last_broker_phase, cdp=False)
                return False
            if remaining <= 0:
                self._publish_failure("broker_start_timeout", self._last_broker_phase, cdp=False)
                return False
            self._sleep(min(_POLL_SECONDS, remaining))

    def _reconcile(self) -> bool:
        with self._lock:
            self._control = self._control or self._new_control()
        try:
            broker_status = self._read_status()
        except Exception:
            return self._try_start(initial=False)
        self._last_broker_phase = getattr(broker_status, "phase", "unknown")
        mode = getattr(broker_status, "mode", None)
        if mode != "dedicated":
            self._publish_failure("broker_mode_unknown", self._last_broker_phase, cdp=False)
            return False
        try:
            endpoint = self._request_endpoint(broker_status)
        except Exception as exc:
            self._publish_failure(self._safe_error_code(exc), self._last_broker_phase, cdp=False)
            return False
        self._publish_ready(endpoint, self._safe_phase(self._last_broker_phase))
        return True

    def _ensure_endpoint(self, broker_status: Any) -> bool:
        self._last_broker_phase = getattr(broker_status, "phase", "unknown")
        try:
            endpoint = self._request_endpoint(broker_status)
        except Exception as exc:
            self._publish_failure(self._safe_error_code(exc), self._last_broker_phase, cdp=False)
            return False
        self._publish_ready(endpoint, self._safe_phase(self._last_broker_phase))
        return True

    def _request_endpoint(self, broker_status: Any = None) -> str:
        if broker_status is not None and not (
            getattr(broker_status, "phase", None) == "ready"
            and getattr(broker_status, "cdp_connected", None) is True
        ):
            # The broker only connects to its Edge when asked (a restarted broker idles, and a
            # dropped CDP connection is only noticed on the next request), and
            # get_browser_endpoint never sends that request itself.
            self._run_control("ensure_browser", timeout_sec=30.0)
        endpoint = self._run_control("get_browser_endpoint", timeout_sec=30.0)
        getter = getattr(endpoint, "for_connection", None)
        value = getter() if callable(getter) else endpoint
        if not isinstance(value, str) or not value:
            raise _BrowserLifecycleFailure("cdp_disconnected")
        return value

    def _read_status(self) -> Any:
        return self._run_control("get_status", timeout_sec=3.0)

    def _run_control(self, method: str, **kwargs: Any) -> Any:
        callback = getattr(self._control, method)
        result = callback(**kwargs)
        if hasattr(result, "__await__"):
            return asyncio.run(result)
        return result

    def _new_control(self) -> Any:
        if self._control_factory is not None:
            return self._control_factory()
        # This client is standard-library-only. Keep its import out of module import.
        from pc_bridge.edge_broker_control import EdgeBrokerControlClient

        return EdgeBrokerControlClient()

    def _spawn_broker(self, *, initial: bool) -> bool:
        python = self._python_executable
        pythonw = python.with_name("pythonw.exe")
        executable = pythonw if pythonw.is_file() else python
        command = [
            str(executable), "-m", "src.services.local_browser.cdp_broker", "--serve",
        ]
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200,
        )
        try:
            self._popen(
                command,
                cwd=str(self._repository_root),
                shell=False,
                creationflags=creationflags,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            self._publish_failure("broker_start_failed", "unavailable", cdp=False)
            return False
        if not initial:
            with self._lock:
                self._restart_count += 1
        return True

    def _publish_ready(self, endpoint: str, phase: str) -> None:
        previous = self.status()
        since = previous.since_ready_unix if previous.ready and previous.cdp_connected else self._unix_time()
        now = self._monotonic()
        should_check_login = not (previous.ready and previous.cdp_connected)
        if self._next_login_check_at is None or now >= self._next_login_check_at:
            should_check_login = True
        with self._lock:
            self._endpoint = endpoint
            self.state.publish(BrowserStatus(
                ready=True,
                cdp_connected=True,
                broker_phase=phase,
                restart_count=self._restart_count,
                since_ready_unix=since,
            ))
        self._unhealthy_since = None
        if should_check_login:
            self._next_login_check_at = now + _LOGIN_CHECK_INTERVAL_SECONDS
            self._check_owner_session(now)

    def _publish_failure(self, code: str, phase: str, *, cdp: bool) -> None:
        previous = self.status()
        if self._unhealthy_since is None:
            self._unhealthy_since = self._monotonic()
        self._login_failure_since = None
        with self._lock:
            self.state.publish(BrowserStatus(
                ready=False,
                cdp_connected=cdp,
                broker_phase=self._safe_phase(phase),
                error_code=code,
                restart_count=self._restart_count,
                since_ready_unix=previous.since_ready_unix,
            ))
            if not cdp:
                self._endpoint = None

    def _clear_endpoint(self) -> None:
        with self._lock:
            self._endpoint = None

    def _enabled(self) -> bool:
        return is_enabled(self._environ, is_windows=self._is_windows)

    def _get_login_service(self) -> Any:
        if self._login_service is not None:
            return self._login_service
        if self._login_factory is not None:
            self._login_service = self._login_factory()
        else:
            from .login import LocalBrowserLogin

            self._login_service = LocalBrowserLogin(
                environ=self._environ,
                repository_root=self._repository_root,
                endpoint_provider=self.current_endpoint,
                unix_time=self._unix_time,
            )
        return self._login_service

    def _check_owner_session(self, now: float) -> None:
        try:
            code = self._get_login_service().ensure_owner_session()
        except Exception:
            code = "login_cookie_set_failed"
        if code in {"login_seeded", "login_not_needed"}:
            self._login_failure_since = None
            return
        if code == "owner_not_configured":
            self._login_failure_since = None
            return
        if code not in {"login_issue_failed", "login_cookie_set_failed"}:
            code = "login_cookie_set_failed"
        self.login_failures += 1
        if self._login_failure_since is None:
            self._login_failure_since = now
        _logger.warning("local browser owner session failed (%s)", code)

    def _evaluate_notifications(self, now: float) -> None:
        snapshot = self.status()
        if snapshot.ready and snapshot.cdp_connected:
            self._unhealthy_since = None
        else:
            if self._unhealthy_since is None:
                self._unhealthy_since = now
            duration = max(0.0, now - self._unhealthy_since)
            if duration >= self._notification_after_seconds:
                reason = snapshot.error_code or "browser_not_ready"
                minutes = max(0, int(duration // 60))
                message = f"理由コード: {reason}。異常が{minutes}分続いています。再起動回数: {snapshot.restart_count}回。"
                self._notify_once(reason, "ローカルブラウザの異常", message)
            self._login_failure_since = None
            return

        if self._login_failure_since is not None:
            duration = max(0.0, now - self._login_failure_since)
            if duration >= self._login_notification_after_seconds:
                minutes = max(0, int(duration // 60))
                message = (
                    f"理由コード: browser_login_required。ログイン処理の失敗が{minutes}分続いています。"
                    f"再起動回数: {snapshot.restart_count}回。"
                )
                self._notify_once("browser_login_required", "ローカルブラウザのログイン確認", message)

    def _notify_once(self, reason: str, title: str, message: str) -> None:
        try:
            hour = int(self._unix_time() // 3600)
        except Exception:
            _logger.warning("local browser notification failed (%s)", "notify_failed")
            return
        key = (reason, hour)
        if key in self._notified_reason_hours:
            return
        self._notified_reason_hours = {
            old_key for old_key in self._notified_reason_hours if old_key[1] >= hour - 1
        }
        self._notified_reason_hours.add(key)
        try:
            self._notifier.notify(reason, title, message)
        except Exception:
            _logger.warning("local browser notification failed (%s)", "notify_failed")

    @staticmethod
    def _safe_phase(value: Any) -> str:
        if isinstance(value, str) and value in {"idle", "connecting", "ready", "failed", "reset_required"}:
            return value
        return "unknown"

    def _start_watcher(self) -> None:
        with self._lock:
            if self._watcher is not None:
                return
            stop_event = self._event_factory()
            watcher = self._thread_factory(
                target=self._watch_loop,
                args=(stop_event,),
                name="aoitalk-local-browser-watcher",
                daemon=True,
            )
            self._watcher_stop = stop_event
            self._watcher = watcher
        watcher.start()

    def _watch_loop(self, stop_event: Any) -> None:
        while True:
            wait_seconds = self._watcher_interval
            with self._lock:
                next_retry_at = self._next_retry_at
            if next_retry_at is not None:
                wait_seconds = min(wait_seconds, max(0.0, next_retry_at - self._monotonic()))
            if stop_event.wait(wait_seconds):
                return
            self.watcher_tick()

    def _record_retry_failure(self, now: float) -> None:
        self._prune_retry_window(now)
        self._retry_failures.append(now)
        self._consecutive_failures += 1

    def _prune_retry_window(self, now: float) -> None:
        while self._retry_failures and now - self._retry_failures[0] >= _RETRY_WINDOW_SECONDS:
            self._retry_failures.popleft()

    @staticmethod
    def _safe_error_code(exc: Exception) -> str:
        if isinstance(exc, _BrowserLifecycleFailure):
            return exc.code
        code = getattr(exc, "code", None)
        if code == "edge_broker_profile_in_use":
            return code
        if code in {"edge_broker_not_ready", "edge_broker_connecting", "edge_broker_browser_disconnected"}:
            return "cdp_disconnected"
        return "broker_unavailable"


class _BrowserLifecycleFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_managed_browser_broker = ManagedBrowserBroker()


def _initial_start() -> None:
    try:
        _managed_browser_broker.start()
    except Exception:
        _managed_browser_broker._publish_failure("broker_start_failed", "unavailable", cdp=False)
        try:
            _managed_browser_broker._start_watcher()
        except Exception:
            pass


def start_managed_browser_broker(*, wait: bool = False) -> bool:
    """Attach to or start the broker without delaying AoiTalk's own startup.

    The first attempt can wait for a broker spawn and an Edge launch, so by default
    it runs on a short-lived thread and the status reports ``ready`` once it
    finishes. ``wait=True`` keeps the synchronous behaviour for callers that need it.
    """
    if wait or not _managed_browser_broker._enabled():
        _initial_start()
        return _managed_browser_broker.status().ready
    _managed_browser_broker.state.publish(BrowserStatus(broker_phase="connecting"))
    try:
        threading.Thread(
            target=_initial_start, name="aoitalk-local-browser-start", daemon=True
        ).start()
    except Exception:
        _managed_browser_broker._publish_failure("broker_start_failed", "unavailable", cdp=False)
    return False


def stop_managed_browser_broker() -> None:
    try:
        _managed_browser_broker.stop()
    except Exception:
        # Stopping this watcher must never take down unrelated child services.
        return


def managed_browser_broker_status() -> BrowserStatus:
    return _managed_browser_broker.status()


def managed_browser_endpoint() -> str | None:
    return _managed_browser_broker.current_endpoint()
