"""One explicitly enabled owned browser, using the existing profile/controller core."""
from __future__ import annotations

import threading
import re
import json
import os
from uuid import uuid4
from uuid import UUID, uuid5

from .browser_profiles import BrowserProfileManager, BrowserProfileScope
from .edge import EdgeBridgeError
from .profile_browser_driver import DEDICATED_COMMANDS, ProfileBrowserDriver, dedicated_runtime_available
from .browser_profiles import _local_path


class DedicatedBrowserBridge:
    def __init__(self, *, manager=None, driver_factory=None, cancel_event=None):
        self.manager = manager or BrowserProfileManager()
        self.driver_factory = driver_factory or ProfileBrowserDriver
        self.cancel_event = cancel_event
        self._lock = threading.RLock()
        self._owner = self._epoch = self._run = None
        self._enabled = False
        self._lease = self._driver = None
        self._cancelled_run = None
        self._paused = False
        self._jev_handler = None
        self._jev_context = None

    def set_jev_handler(self, handler):
        """Answer jev_run's judgment requests and pre-mutation gate (fail closed)."""
        self._jev_handler = handler
        if self._driver is not None and hasattr(self._driver, "set_jev_handler"):
            self._driver.set_jev_handler(handler)

    def jev_context(self):
        """Binding of the jev_run in flight: its tab and the lease's browser context."""
        return self._jev_context

    def bind_connection(self, owner, epoch, *, enabled=False):
        if not isinstance(epoch, str) or not epoch or type(enabled) is not bool:
            raise EdgeBridgeError("profile_driver_connection_invalid")
        try:
            UUID(owner)
        except (TypeError, ValueError, AttributeError):
            raise EdgeBridgeError("profile_driver_owner_unverified") from None
        if self._jev_context is not None and (not enabled or (self._owner, self._epoch) != (owner, epoch)):
            self._enabled = False
            self._driver.cancel()
        with self._lock:
            if (self._owner, self._epoch) != (owner, epoch):
                self.close()
                self._cancelled_run = None
            self._owner, self._epoch, self._enabled = owner, epoch, enabled
            # A handoff keeps the exact paused context for a later explicit
            # resume. OFF revokes all browsing authority; only release can run.
            if not enabled and not (self._run and self._paused):
                released_run = self._run
                self.close()
                if released_run:
                    self._cancelled_run = (owner, epoch, released_run)

    def _guard(self):
        if not self._enabled:
            raise EdgeBridgeError("profile_driver_dedicated_disabled")
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise EdgeBridgeError("profile_driver_cancelled")
        if self._owner is None or self._epoch is None:
            raise EdgeBridgeError("profile_driver_owner_unverified")

    def _open(self):
        if not dedicated_runtime_available():
            raise EdgeBridgeError("profile_driver_dedicated_bundle_unavailable")
        if self._driver is not None:
            if not self._driver.is_alive():
                raise EdgeBridgeError("profile_driver_closed")
            self._lease.verify()
            return
        self._cancelled_run = None
        profile_key = str(uuid5(UUID(self._owner), "aoitalk-dedicated-browser"))
        self._lease = self.manager.acquire(BrowserProfileScope("browser", self._owner, profile_key),
                                          connection_epoch=self._epoch)
        try:
            self._driver = self.driver_factory(self._lease)
            if self._jev_handler is not None and hasattr(self._driver, "set_jev_handler"):
                self._driver.set_jev_handler(self._jev_handler)
        except Exception:
            self._lease.finish_runtime()
            self._lease.release(browser_closed=True)
            self._lease = None
            raise

    def _request(self, command, params):
        if command not in DEDICATED_COMMANDS or not isinstance(params, dict):
            raise EdgeBridgeError("profile_driver_command_rejected")
        run = params.get("run_id")
        if not isinstance(run, str) or re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", run) is None:
            raise EdgeBridgeError("profile_driver_run_invalid")
        with self._lock:
            cleanup = command in {"end_run", "cancel_login"}
            if not self._enabled and not cleanup:
                self._guard()
            # A successful cancel closes the Job and releases its lease. Final
            # cleanup may acknowledge only that exact transport-bound run,
            # without opening another browser or claiming a retained connection.
            if (cleanup and self._run is self._driver is self._lease is None
                    and self._cancelled_run == (self._owner, self._epoch, run)):
                if command == "cancel_login":
                    return {"cancelled": True}
                return {"ended": True, "run_id": run, "refs_invalidated": True,
                        "connection_retained": False, "paused": False}
            if self._enabled:
                self._guard()
            elif self._owner is None or self._epoch is None:
                raise EdgeBridgeError("profile_driver_owner_unverified")
            if command == "checkpoint_load":
                if self._run is not None and self._run != run:
                    raise EdgeBridgeError("profile_driver_run_changed")
                self._open()
                return self._checkpoint(command, params)
            new_run = command == "resolve_tab" and self._run is None
            if new_run:
                self._open()
                self._run = run
            if self._run != run:
                raise EdgeBridgeError("profile_driver_run_changed")
            if self._enabled:
                self._open()
            elif self._driver is None or self._lease is None or not self._driver.is_alive():
                raise EdgeBridgeError("profile_driver_close_unconfirmed")
            else:
                self._lease.verify()
            if command.startswith("checkpoint_"):
                return self._checkpoint(command, params)
            if command == "jev_run":
                tab_id = params.get("tab_id")
                if type(tab_id) is not int:
                    raise EdgeBridgeError("profile_driver_jev_params_invalid")
                self._jev_context = {"run_id": run, "tab_id": tab_id, "browser_context": dict(self._driver.browser_context)}
                try:
                    return self._driver.request(command, params)
                finally:
                    self._jev_context = None
            result = self._driver.request(command, params)
            if command == "pause_login":
                self._paused = True
            elif command == "resume_login":
                self._paused = False
            elif command == "cancel_login":
                self.close()
                if result.get("cancelled") is True:
                    self._cancelled_run = (self._owner, self._epoch, run)
            elif command == "end_run" and result.get("paused") is not True:
                self._run = None
                self._paused = False
            return result

    def _checkpoint(self, command, params):
        # DPAPI uses the current Windows user. Neither plaintext task data nor
        # browser authentication values are written to a file/log/diagnostic.
        import win32crypt
        session = params.get("session_id")
        if not isinstance(session, str) or not session or len(session) > 128:
            raise EdgeBridgeError("profile_driver_session_invalid")
        file = _local_path(self._lease.directory / "browser-task.enc")
        entropy = (self._owner + ":" + session + ":" + params["run_id"]).encode()
        if command == "checkpoint_load":
            try:
                if file.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError()
                _, plain = win32crypt.CryptUnprotectData(file.read_bytes(), entropy, None, None, 1)
                data = json.loads(plain)
                if not isinstance(data, dict):
                    raise ValueError()
                return {"checkpoint": data}
            except Exception:
                raise EdgeBridgeError("profile_driver_checkpoint_unavailable") from None
        if command == "checkpoint_clear":
            # Confirm the same session/run can decrypt before removing it.
            self._checkpoint("checkpoint_load", params)
            file.unlink()
            return {"cleared": True}
        data = params.get("checkpoint")
        if not isinstance(data, dict) or set(data) != {"goal", "start_url", "plan", "cursor", "phase", "state", "tab_id", "completion_text", "completion_url"}:
            raise EdgeBridgeError("profile_driver_checkpoint_invalid")
        plain = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        if len(plain) > 1024 * 1024:
            raise EdgeBridgeError("profile_driver_checkpoint_invalid")
        encrypted = win32crypt.CryptProtectData(plain, None, entropy, None, None, 1)
        temporary = _local_path(file.with_name("browser-task-" + uuid4().hex + ".new"))
        try:
            with temporary.open("xb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, file)
        finally:
            temporary.unlink(missing_ok=True)
        return {"saved": True}

    def available(self):
        return self._enabled and self._owner is not None

    def diagnostics(self):
        return {"backend": "owned_profile", "enabled": self._enabled,
                "connected": self._driver is not None and self._driver.is_alive(), "run_active": self._run is not None}

    def suspend(self):
        # The authenticated transport epoch is revoked; no operation can be
        # resumed until a new ready message rebinds the same owner explicitly.
        self._enabled = False
        if self._jev_context is not None and self._driver is not None:
            self._driver.cancel()
        return True

    def close(self):
        # Cancel the owned Job before waiting for the long-running command's
        # lock. OFF/disconnect must remain operable while a gate is pending.
        if self._jev_context is not None and self._driver is not None:
            self._enabled = False
            self._driver.cancel()
        with self._lock:
            if self._driver is not None:
                if self._driver.close() is not True:
                    raise EdgeBridgeError("profile_driver_close_unconfirmed")
            if self._lease is not None:
                self._lease.release(browser_closed=True)
            self._lease = self._driver = self._run = None
            self._paused = False
            return True
