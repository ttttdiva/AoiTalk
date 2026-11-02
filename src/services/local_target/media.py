"""Server-local host for the PC Bridge media preparation backend.

This module stays cheap to import. Browser, profile, Windows, and staging
helpers are loaded only when a media operation first needs them.
"""
from __future__ import annotations

import asyncio
import os
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable
from uuid import UUID, uuid4


LOCAL_DEVICE_ID = "local"
_LOCAL_CAPABILITIES = frozenset({"browser", "media_asset", "media_prepare"})
_MEDIA_ASSET_ACTIONS = frozenset({"begin", "append", "finish", "prepare_x_image", "prepare_youtube_video"})
_MEDIA_PREPARE_ERROR = "media_prepare_profile_unverified"
_ASSET_STORAGE_ERROR = "bridge_asset_storage_unavailable"


def _profile_root() -> Path:
    """Return the private local profile root without embedding a machine path."""
    root = os.environ.get("LOCALAPPDATA")
    if not isinstance(root, str) or not root.strip():
        raise RuntimeError("media_prepare_profile_unavailable")
    return Path(root) / "AoiTalk" / "media-profile"


def _pc_bridge_error(code: str) -> RuntimeError:
    from src.services.pc_bridge_store import PcBridgeError

    return PcBridgeError(code)


def _fixed_error(exc: BaseException, *, prefix: str, fallback: str) -> str:
    value = getattr(exc, "code", None)
    if not isinstance(value, str):
        value = str(exc)
    if re.fullmatch(re.escape(prefix) + r"[a-z_]{1,90}", value):
        return value
    return fallback


class LocalMediaHub:
    """Small in-process counterpart to ``PcBridgeHub.call`` for media assets."""

    def __init__(self, bridge: "LocalMediaBridge") -> None:
        self._bridge = bridge

    async def call(
        self,
        owner: str,
        device: str,
        channel: str,
        action: str,
        params: dict[str, Any] | None = None,
        timeout: float = 45,
        *,
        expected_connection: Any = None,
        dispatch_preflight: Any = None,
        dispatch_gate: Any = None,
        jev_relay: Any = None,
    ) -> dict[str, Any]:
        """Dispatch only the closed media_asset action set to the local store."""
        bridge = self._bridge
        if (owner != bridge.owner or device != bridge.device_id or channel != "media_asset"
                or expected_connection is not bridge._connection):
            raise _pc_bridge_error("bridge_connection_changed")
        if "media_asset" not in bridge._connection.capabilities:
            raise _pc_bridge_error("bridge_capability_unavailable")
        if not isinstance(action, str) or action not in _MEDIA_ASSET_ACTIONS or not isinstance(params, dict):
            raise _pc_bridge_error("bridge_asset_params_invalid")
        if dispatch_preflight is not None:
            await dispatch_preflight()
        if dispatch_gate is not None:
            dispatch_gate()
        try:
            store = bridge._get_store()
            return await asyncio.to_thread(store.dispatch, action, dict(params))
        except Exception as exc:
            code = _fixed_error(exc, prefix="bridge_asset_", fallback=_ASSET_STORAGE_ERROR)
            # Store implementations expose fixed codes. Unknown exceptions are
            # intentionally collapsed so filesystem details never cross the API.
            from pc_bridge.media_asset_transfer import MediaAssetTransferError

            raise MediaAssetTransferError(code) from None


class LocalMediaBridge:
    """Media preparation and validated asset staging hosted in the server."""

    device_id = LOCAL_DEVICE_ID
    browser_backend = "agent_browser"
    media_preparation_backend = "managed_profile"

    def __init__(
        self,
        owner: str,
        *,
        profiles: Iterable[dict[str, Any]] = (),
        profile_browser: Any = None,
        profile_browser_factory: Callable[[], Any] | None = None,
        store: Any = None,
        store_factory: Callable[..., Any] | None = None,
    ) -> None:
        try:
            if not isinstance(owner, str) or str(UUID(owner)) != owner or UUID(owner).int == 0:
                raise ValueError()
        except (ValueError, AttributeError):
            raise _pc_bridge_error("bridge_owner_invalid") from None
        self.owner = owner
        self._connection = SimpleNamespace(
            owner=owner,
            device=self.device_id,
            epoch=uuid4().hex,
            capabilities=set(_LOCAL_CAPABILITIES),
        )
        self.operation_lock = threading.Lock()
        self.hub = LocalMediaHub(self)
        self._profiles = list(profiles)
        self._profile_browser = profile_browser
        self._profile_browser_factory = profile_browser_factory
        self._store = store
        self._store_factory = store_factory
        self._closed = False
        self._lifecycle_lock = threading.RLock()

    @property
    def connection_epoch(self) -> str:
        return self._connection.epoch

    def _check_connection(self) -> None:
        if self._closed:
            raise _pc_bridge_error("bridge_connection_changed")

    def _get_profile_browser(self) -> Any:
        with self._lifecycle_lock:
            self._check_connection()
            if self._profile_browser is not None:
                return self._profile_browser
            if self._profile_browser_factory is not None:
                self._profile_browser = self._profile_browser_factory()
                return self._profile_browser

            try:
                from pc_bridge.browser_profiles import BrowserProfileManager
                from pc_bridge.managed_profile_backend import ManagedProfileBrowserBridge
                from pc_bridge.purpose_browser_bridge import PurposeBrowserBridge
                from pc_bridge.edge import EdgeBridgeError

                root = _profile_root()
                preparation = ManagedProfileBrowserBridge(
                    manager=BrowserProfileManager(root=root),
                )
                preparation.bind_connection(self.owner, self.connection_epoch, self._profiles)
                purpose = PurposeBrowserBridge(_MediaOnlyGeneralBrowser(), preparation)
                self._profile_browser = purpose
                self._preparation_browser = preparation
                return purpose
            except Exception as exc:
                code = _fixed_error(exc, prefix="media_prepare_", fallback=_MEDIA_PREPARE_ERROR)
                raise EdgeBridgeError(code) from None

    def _get_preparation_browser(self) -> Any:
        browser = self._get_profile_browser()
        return getattr(self, "_preparation_browser", browser)

    def _get_store(self) -> Any:
        with self._lifecycle_lock:
            self._check_connection()
            if self._store is not None:
                return self._store
            if self._store_factory is not None:
                self._store = self._store_factory(
                    device_id=self.device_id,
                    connection_epoch=self.connection_epoch,
                    browser=self._get_preparation_browser(),
                )
                return self._store

            try:
                from pc_bridge.media_asset_transfer import MediaAssetTransferStore

                root = _profile_root() / "asset-transfers"
                self._store = MediaAssetTransferStore(
                    device_id=self.device_id,
                    connection_epoch=self.connection_epoch,
                    root=root,
                    browser=self._get_preparation_browser(),
                )
                return self._store
            except Exception as exc:
                from pc_bridge.media_asset_transfer import MediaAssetTransferError

                code = _fixed_error(exc, prefix="bridge_asset_", fallback=_ASSET_STORAGE_ERROR)
                raise MediaAssetTransferError(code) from None

    async def request(self, command: str, **params: Any) -> dict[str, Any]:
        """Run a closed ``media_prepare_*`` operation through the PC Bridge backend."""
        from pc_bridge.browser_commands import MEDIA_PREPARATION_COMMANDS
        from pc_bridge.edge import EdgeBridgeError

        if not isinstance(command, str) or command not in MEDIA_PREPARATION_COMMANDS:
            raise EdgeBridgeError("media_prepare_command_rejected")
        try:
            browser = await asyncio.to_thread(self._get_profile_browser)
            request = getattr(browser, "_request")
            return await asyncio.to_thread(request, command, dict(params))
        except EdgeBridgeError:
            raise
        except Exception as exc:
            code = _fixed_error(exc, prefix="media_prepare_", fallback=_MEDIA_PREPARE_ERROR)
            raise EdgeBridgeError(code) from None

    def local_setup(self, action: str, params: dict[str, Any]) -> Any:
        """Expose the hosted backend's explicit local profile setup seam."""
        from pc_bridge.edge import EdgeBridgeError

        try:
            browser = self._get_profile_browser()
            setup = getattr(browser, "local_setup")
            return setup(action, dict(params), purpose="media")
        except EdgeBridgeError:
            raise
        except Exception as exc:
            code = _fixed_error(exc, prefix="profile_driver_", fallback=_MEDIA_PREPARE_ERROR)
            raise EdgeBridgeError(code) from None

    def close(self) -> None:
        """Release staged files and close only resources created by this bridge."""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            store, browser = self._store, self._profile_browser
            self._store = None
            self._profile_browser = None
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
        if browser is not None:
            close = getattr(browser, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        with _bridge_lock:
            if _bridges.get(self.owner) is self:
                _bridges.pop(self.owner, None)


class _MediaOnlyGeneralBrowser:
    """PurposeBrowserBridge requires a general route, which this target denies."""

    def _request(self, command: str, params: dict[str, Any]) -> Any:
        from pc_bridge.edge import EdgeBridgeError

        raise EdgeBridgeError("agent_browser_command_unsupported")

    def available(self) -> bool:
        return False

    def local_setup(self, action: str, params: dict[str, Any]) -> Any:
        from pc_bridge.edge import EdgeBridgeError

        raise EdgeBridgeError("agent_browser_command_unsupported")


_bridge_lock = threading.Lock()
_bridges: dict[str, LocalMediaBridge] = {}


def get_local_media_bridge(owner: str, **bridge_options: Any) -> LocalMediaBridge:
    """Return one process-local epoch/store/profile host for the owner."""
    with _bridge_lock:
        bridge = _bridges.get(owner)
        if bridge is None:
            bridge = LocalMediaBridge(owner, **bridge_options)
            _bridges[owner] = bridge
        return bridge
