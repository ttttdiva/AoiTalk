"""X/YouTube preparation boundary for an explicitly opened owned profile.

The local driver must implement the existing media_prepare contract (including
the product DOM account checks). This adapter grants no login, navigation,
publication, or generic browser execution capability. No driver is started here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading

from .browser_profiles import BrowserProfileError, _local_path
from .edge import EdgeBridgeError

_COMMANDS = {"media_prepare_readiness", "media_prepare_text", "media_prepare_youtube",
             *(f"media_prepare_{prefix}asset_{phase}" for prefix in ("", "youtube_")
               for phase in ("begin", "append", "finish", "inspect"))}


class ProfileMediaPreparationBridge:
    """One lease/driver, closed command set, durable no-replay input fence."""
    def __init__(self, lease, driver):
        self.lease, self.driver = lease, driver
        self._lock = threading.Lock()
        self._closed = False
        self._cancelled = threading.Event()
        self._guard()

    def _guard(self):
        try:
            self.lease.verify()
            if (self._closed or self._cancelled.is_set() or self.driver.profile_id != self.lease.profile_id
                    or self.driver.connection_epoch != self.lease.connection_epoch
                    or _local_path(self.driver.user_data_dir) != self.lease.user_data_dir
                    or self.driver.browser_context != self.lease.context
                    or self.driver.is_alive() is not True):
                raise BrowserProfileError("browser_profile_driver_changed")
        except (BrowserProfileError, AttributeError, OSError):
            raise EdgeBridgeError("media_prepare_profile_unverified") from None

    def _capabilities(self):
        self._guard()
        value = self.driver.request("media_prepare_capabilities", {})
        self._guard()
        service = self.lease.scope.service
        if (not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 2
                or value.get("final_confirmation") is not False or value.get("browser_context_pin") is not True
                or value.get("browser_context") != self.lease.context
                or not isinstance(value.get("platforms"), list) or service not in value["platforms"]
                or (service == "x" and value.get("text") is not True)
                or (service == "youtube" and value.get("youtube_private") is not True)):
            raise EdgeBridgeError("media_prepare_driver_capability_unverified")
        return {**value, "platforms": [service], "profile_binding": self.lease.binding()}

    @staticmethod
    def _mutation(command, params):
        return (command in {"media_prepare_text", "media_prepare_youtube"} and params.get("phase") == "fill"
                or "asset_" in command and command.rsplit("_", 1)[1] in {"begin", "append", "finish"})

    def _params(self, command, params):
        service = self.lease.scope.service
        common = {"platform", "tab_id", "expected_account_ref", "expected_browser_context"}
        if command == "media_prepare_readiness":
            allowed = common
        elif command in {"media_prepare_text", "media_prepare_youtube"}:
            allowed = common | {"phase", "run_id", "expected_document_id"}
            allowed |= {"text"} if service == "x" else {"metadata", "metadata_sha256"}
            if params.get("phase") not in {"inspect", "fill", "verify"}:
                raise EdgeBridgeError("media_prepare_params_invalid")
        else:
            allowed = common | {"run_id", "expected_document_id", "text_sha256", "metadata_sha256",
                "scope_hash", "transfer_id", "asset_sha256", "size_bytes", "mime_type", "offset", "base64"}
        account = params.get("expected_account_ref")
        pattern = r"@[A-Za-z0-9_]{1,15}" if service == "x" else r"UC[A-Za-z0-9_-]{22}"
        if (set(params) - allowed or params.get("platform") != service or type(params.get("tab_id")) is not int
                or params["tab_id"] < 1 or not isinstance(account, str) or not re.fullmatch(pattern, account)):
            raise EdgeBridgeError("media_prepare_params_invalid")

    def _fence(self, command, params):
        # Only digests are retained, never text, metadata, filenames, or bytes.
        if not isinstance(params.get("run_id"), str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", params["run_id"]):
            raise EdgeBridgeError("media_prepare_params_invalid")
        # A changed payload/context/document cannot replay the same run phase.
        identity = [command, params["run_id"], params.get("phase"), params.get("offset")]
        digest = hashlib.sha256(json.dumps(identity, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        directory = _local_path(self.lease.directory / "input-fences")
        directory.mkdir(exist_ok=True)
        run_digest = hashlib.sha256(params["run_id"].encode("ascii")).hexdigest()
        pending = _local_path(directory / ("pending-" + run_digest))
        if pending.exists():
            raise EdgeBridgeError("media_prepare_outcome_unknown")
        marker = _local_path(directory / digest)
        try:
            with marker.open("x", encoding="ascii") as output:
                output.write("dispatched-or-unknown\n")
                output.flush()
                os.fsync(output.fileno())
        except FileExistsError:
            raise EdgeBridgeError("media_prepare_input_already_dispatched") from None
        with pending.open("x", encoding="ascii") as output:
            output.write("input-pending-or-unknown\n")
            output.flush()
            os.fsync(output.fileno())
        return pending

    def _request(self, command, params):
        if not isinstance(params, dict) or not self._lock.acquire(blocking=False):
            raise EdgeBridgeError("media_prepare_profile_busy_or_invalid")
        mutated = False
        pending = None
        try:
            self._guard()
            if command in {"media_prepare_capabilities", "media_prepare_context"}:
                if params:
                    raise EdgeBridgeError("media_prepare_params_invalid")
                return self._capabilities() if command.endswith("capabilities") else dict(self.lease.context)
            if command not in _COMMANDS or params.get("expected_browser_context") != self.lease.context:
                raise EdgeBridgeError("media_prepare_context_or_command_rejected")
            service = self.lease.scope.service
            target_service = (params.get("platform") if command == "media_prepare_readiness"
                              else "youtube" if "youtube" in command else "x")
            if target_service != service:
                raise EdgeBridgeError("media_prepare_profile_service_mismatch")
            self._params(command, params)
            self._capabilities()
            if self._mutation(command, params):
                pending = self._fence(command, params)
                mutated = True
            response = self.driver.request(command, dict(params))
            self._guard()
            if (not isinstance(response, dict) or response.get("publication_confirmed", False) is not False
                    or type(response.get("outcome_unknown")) is not bool
                    or "asset_" not in command and response.get("platform") != service):
                raise EdgeBridgeError("media_prepare_response_invalid")
            if pending is not None and response["outcome_unknown"] is False:
                _local_path(pending).unlink()
            return response
        except Exception:
            if mutated:
                # A lost reply/profile change never authorizes another input.
                raise EdgeBridgeError("media_prepare_outcome_unknown") from None
            raise
        finally:
            self._lock.release()

    def available(self):
        try:
            self._guard()
            return True
        except EdgeBridgeError:
            return False

    def close(self):
        self._cancelled.set()
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            cancel = getattr(self.driver, "cancel", None)
            if callable(cancel):
                cancel()
            self._lock.acquire()
        try:
            if self._closed:
                return
            # Input has finished (or become unknown) before the profile can
            # pass to another process. A failed close retains the lease.
            if self.driver.close() is not True or self.driver.is_alive() is not False:
                raise EdgeBridgeError("media_prepare_profile_close_unconfirmed")
            self._closed = True
            self.lease.release(browser_closed=True)
        finally:
            self._lock.release()
