"""One authenticated Bridge connection and one explicitly opened owned profile."""
from __future__ import annotations

import threading
import time
from uuid import UUID

from .browser_profiles import BrowserProfileManager, BrowserProfileScope, BrowserProfileError
from .edge import EdgeBridgeError
from .profile_browser_driver import ProfileBrowserDriver
from .profile_media_preparation import ProfileMediaPreparationBridge
from .browser_commands import MEDIA_PREPARATION_COMMANDS


def profile_catalog(values):
    keys = {"platform_account_id", "service", "display_name", "persona_name", "project_name"}
    if not isinstance(values, list) or len(values) > 256:
        raise EdgeBridgeError("profile_driver_catalog_invalid")
    result, seen = [], set()
    for value in values:
        try:
            if (not isinstance(value, dict) or set(value) != keys or value["service"] not in {"x", "youtube"}
                    or str(UUID(value["platform_account_id"])) != value["platform_account_id"]
                    or not UUID(value["platform_account_id"]).int or value["platform_account_id"] in seen
                    or any(not isinstance(value[key], str) or len(value[key]) > 200
                           or any(ord(char) < 32 for char in value[key]) for key in keys - {"platform_account_id", "service"})):
                raise ValueError()
        except (ValueError, TypeError, AttributeError, KeyError):
            raise EdgeBridgeError("profile_driver_catalog_invalid") from None
        seen.add(value["platform_account_id"])
        result.append(dict(value))
    return result


class ManagedProfileBrowserBridge:
    supported_commands = MEDIA_PREPARATION_COMMANDS

    def __init__(self, *, manager=None, driver_factory=None, cancel_event=None):
        self.manager = manager or BrowserProfileManager()
        self.driver_factory = driver_factory or ProfileBrowserDriver
        self.cancel_event = cancel_event or threading.Event()
        self._owner = self._epoch = None
        self._profiles, self._current, self._lease = [], None, None
        self._lock = threading.RLock()
        self._revoking = threading.Event()
        self._diagnostics = {"connected": False, "selected": False, "blocked": False, "error": "",
                             "last_verified_at": None, "cached": True}

    def bind_connection(self, owner, epoch, profiles):
        try:
            if not isinstance(owner, str) or str(UUID(owner)) != owner or not UUID(owner).int:
                raise ValueError()
        except (ValueError, AttributeError):
            raise EdgeBridgeError("profile_driver_owner_unverified") from None
        if not isinstance(epoch, str) or not epoch or len(epoch) > 128:
            raise EdgeBridgeError("profile_driver_connection_unverified")
        catalog = profile_catalog(profiles)
        with self._lock:
            self.close()
            self._owner, self._epoch, self._profiles = owner, epoch, catalog
            self._revoking.clear()
            self._diagnostics.update(blocked=False, error="")

    def diagnostics(self):
        # GUI poll never opens a profile, checks a process, or reads auth state.
        return {**self._diagnostics, "profiles": [dict(value) for value in self._profiles]}

    def local_setup(self, action, params):
        with self._lock:
            if self.cancel_event.is_set() or self._revoking.is_set() or self._owner is None:
                raise EdgeBridgeError("profile_driver_connection_required")
            if action == "open_profile":
                if not isinstance(params, dict) or set(params) != {"platform_account_id"}:
                    raise EdgeBridgeError("profile_driver_params_invalid")
                profile = next((row for row in self._profiles if row["platform_account_id"] == params["platform_account_id"]), None)
                if profile is None:
                    raise EdgeBridgeError("profile_driver_catalog_target_required")
                self._close_current()
                self._lease = self.manager.acquire(BrowserProfileScope(profile["service"], self._owner,
                    profile["platform_account_id"]), connection_epoch=self._epoch)
                try:
                    driver = self.driver_factory(self._lease)
                    self._current = ProfileMediaPreparationBridge(self._lease, driver)
                    if self.cancel_event.is_set() or self._revoking.is_set():
                        raise EdgeBridgeError("profile_driver_closed")
                    driver.local_setup("open_service", {})
                    targets = driver.local_setup("discover_targets", {})
                except Exception:
                    self._diagnostics.update(connected=False, selected=False, blocked=True, error="profile_driver_start_failed")
                    self._close_current()
                    raise EdgeBridgeError("profile_driver_start_failed") from None
                self._diagnostics.update(connected=True, selected=False, blocked=False, error="", last_verified_at=time.time())
                return {"opened": True, "profile": dict(profile), **targets}
            if self._current is None or not self._current.available():
                raise EdgeBridgeError("profile_driver_profile_selection_required")
            if action not in {"discover_targets", "select_target"}:
                raise EdgeBridgeError("profile_driver_command_rejected")
            result = self._current.driver.local_setup(action, params)
            if action == "select_target":
                self._diagnostics.update(selected=True, last_verified_at=time.time())
            else:
                self._diagnostics.update(last_verified_at=time.time())
            return result

    def _request(self, command, params):
        if command not in MEDIA_PREPARATION_COMMANDS:
            raise EdgeBridgeError("media_prepare_command_rejected")
        current = self._current
        if current is None or self.cancel_event.is_set() or self._revoking.is_set():
            raise EdgeBridgeError("profile_driver_profile_selection_required")
        return current._request(command, params)

    def available(self):
        current = self._current
        return current is not None and not self.cancel_event.is_set() and not self._revoking.is_set() and current.available()

    def _close_current(self):
        try:
            if self._current is not None:
                self._current.close()
                self._current = None
            elif self._lease is not None:
                # Constructor failure is not proof of browser exit.
                self._lease.finish_runtime()
                self._lease.release(browser_closed=True)
            self._lease = None
            self._diagnostics.update(connected=False, selected=False)
        except (EdgeBridgeError, BrowserProfileError):
            self._diagnostics.update(connected=False, selected=False, blocked=True, error="profile_driver_close_unconfirmed")
            raise EdgeBridgeError("profile_driver_close_unconfirmed") from None

    def close(self):
        # Do not take the setup lock before canceling an in-flight preparation.
        self._revoking.set()
        current = self._current
        if current is not None:
            try:
                current.close()
            except EdgeBridgeError:
                self._diagnostics.update(connected=False, selected=False, blocked=True,
                                         error="profile_driver_close_unconfirmed")
                raise EdgeBridgeError("profile_driver_close_unconfirmed") from None
        # Startup holds this lock until its owned driver has either returned
        # or confirmed cleanup. Never release its provisional lease early.
        with self._lock:
            self._close_current()
            self._owner = self._epoch = None
            self._profiles = []

    suspend = close
