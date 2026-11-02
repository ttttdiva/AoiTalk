"""Process-local execution target for operator-approved saved PC actions."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.services.pc_bridge_service import PcBridgeError, RemoteAppsControlBridge

LOCAL_DEVICE_ID = "local"
LOCAL_CONNECTION_EPOCH = "local"


def _load_app_config() -> Mapping[str, Any]:
    from src.app_config_store import load_app_config_sync

    config = load_app_config_sync()
    return config if isinstance(config, Mapping) else {}


def _local_saved_actions(config: Mapping[str, Any]) -> Any:
    apps = config.get("apps", config)
    actions = apps.get("actions", apps) if isinstance(apps, Mapping) else {}
    if not isinstance(actions, Mapping):
        return []
    return actions.get("local_saved_actions", [])


def _runner_config(config: Mapping[str, Any]) -> dict[str, Any]:
    saved_actions = _local_saved_actions(config)
    # ActionCatalog accepts an empty catalog as {"actions": []}; keep the
    # application setting's simpler [] default while preserving the Bridge schema.
    if saved_actions is None or saved_actions == []:
        saved_actions = {"actions": []}
    return {"saved_actions": saved_actions}


def _local_action_root() -> Path:
    try:
        local_app_data = Path(os.environ["LOCALAPPDATA"])
    except (KeyError, TypeError):
        raise PcBridgeError("apps_storage_unavailable") from None
    if not local_app_data.is_absolute():
        raise PcBridgeError("apps_storage_unavailable")
    return local_app_data / "AoiTalk" / "local-actions"


def _create_action_runner(config: Mapping[str, Any], data_root: Path) -> Any:
    # ActionRunner pulls action_supervisor, action_journal, action_protocol and
    # action_identity from the existing Bridge implementation.  Its Windows
    # helpers remain lazy inside action_identity/action_supervisor.
    from pc_bridge.action_runner import ActionRunner

    return ActionRunner(
        config,
        data_root=data_root,
        journal_path=data_root / "journal.sqlite3",
    )


@dataclass
class _LocalConnection:
    owner: str
    device: str
    capabilities: tuple[str, ...]
    epoch: str
    apps_catalog: dict[str, dict[str, Any]]
    machine_id: str
    boot_id: str
    apps_actions: tuple[str, ...]
    task_lock: threading.RLock = field(default_factory=threading.RLock)
    browser_backend: str = "local"
    media_preparation_backend: str | None = None


class LocalHub:
    """The apps-channel subset of PcBridgeHub, dispatching into ActionRunner."""

    def __init__(
        self,
        *,
        runner: Any = None,
        config: Mapping[str, Any] | None = None,
        config_provider: Callable[[], Mapping[str, Any]] | None = None,
        runner_factory: Callable[[Mapping[str, Any], Path], Any] | None = None,
        journal_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self._runner = runner
        self._config = config
        self._config_provider = config_provider or _load_app_config
        self._runner_factory = runner_factory or _create_action_runner
        self._journal_root_override = Path(journal_root) if journal_root is not None else None
        self._lock = threading.RLock()
        self._dispatch_lock = threading.RLock()
        self._connections: dict[tuple[str, str], _LocalConnection] = {}

    def _get_runner(self) -> Any:
        with self._lock:
            if self._runner is not None:
                return self._runner
            try:
                config = self._config if self._config is not None else self._config_provider()
                if not isinstance(config, Mapping):
                    config = {}
                root = self._journal_root_override or _local_action_root()
                if not root.is_absolute():
                    raise PcBridgeError("apps_storage_unavailable")
                self._runner = self._runner_factory(_runner_config(config), root)
            except PcBridgeError:
                raise
            except Exception:
                # Configuration paths and filesystem details are never returned to callers.
                raise PcBridgeError("apps_catalog_unavailable") from None
            return self._runner

    def connection(self, owner: str, device: str) -> _LocalConnection:
        if not isinstance(owner, str) or not owner or len(owner) > 128:
            raise PcBridgeError("bridge_owner_invalid")
        if device != LOCAL_DEVICE_ID:
            raise PcBridgeError("bridge_device_offline")
        key = (owner, device)
        with self._lock:
            existing = self._connections.get(key)
            if existing is not None:
                return existing
            runner = self._get_runner()
            try:
                manifest = runner.catalog.manifest()
                if not isinstance(manifest, list) or any(
                    not isinstance(item, Mapping) or not isinstance(item.get("saved_action_id"), str)
                    for item in manifest
                ):
                    raise ValueError("catalog_manifest_invalid")
                catalog = {item["saved_action_id"]: dict(item) for item in manifest}
                machine_id = str(runner.machine_id)
                boot_id = str(runner.boot_id)
            except Exception:
                raise PcBridgeError("apps_catalog_unavailable") from None
            actions = tuple(sorted(catalog))
            connection = _LocalConnection(
                owner=owner,
                device=device,
                capabilities=("apps",) if bool(getattr(runner, "enabled", actions)) else (),
                epoch=LOCAL_CONNECTION_EPOCH,
                apps_catalog=catalog,
                machine_id=machine_id,
                boot_id=boot_id,
                apps_actions=actions,
                task_lock=self._dispatch_lock,
            )
            self._connections[key] = connection
            return connection

    async def call(
        self,
        owner: str,
        device: str,
        channel: str,
        action: str,
        params: Mapping[str, Any] | None = None,
        timeout: float = 45,
        *,
        expected_connection: _LocalConnection | None = None,
        dispatch_preflight: Any = None,
        dispatch_gate: Any = None,
        jev_relay: Any = None,
    ) -> dict[str, Any]:
        del timeout, dispatch_preflight, dispatch_gate, jev_relay
        connection = self.connection(owner, device)
        if expected_connection is not None and connection is not expected_connection:
            raise PcBridgeError("bridge_connection_changed")
        if channel != "apps":
            raise PcBridgeError("bridge_capability_unavailable")

        from pc_bridge.action_protocol import ActionProtocolError, validate_action_request

        try:
            validated = validate_action_request(action, params or {})
        except ActionProtocolError as exc:
            raise PcBridgeError(str(exc)) from None
        if "apps" not in connection.capabilities:
            raise PcBridgeError("bridge_capability_unavailable")
        if action == "start" and validated["saved_action_id"] not in connection.apps_actions:
            raise PcBridgeError("bridge_saved_action_unavailable")
        if action == "start":
            catalog = connection.apps_catalog.get(validated["saved_action_id"])
            if (catalog is None
                    or catalog.get("sha256") != validated["sha256"]
                    or catalog.get("contract_hash") != validated["contract_hash"]):
                raise PcBridgeError("bridge_saved_action_contract_mismatch")

        try:
            result = await asyncio.to_thread(self._dispatch, connection, action, validated)
        except PcBridgeError:
            raise
        except ActionProtocolError as exc:
            raise PcBridgeError(str(exc)) from None
        except Exception:
            raise PcBridgeError("bridge_command_failed") from None
        if not isinstance(result, dict):
            raise PcBridgeError("bridge_invalid_response")
        return result

    def _dispatch(
        self,
        connection: _LocalConnection,
        action: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        from pc_bridge.action_protocol import ActionProtocolError

        runner = self._get_runner()
        with self._dispatch_lock:
            runner.bind(
                owner_user_id=connection.owner,
                device_id=connection.device,
                connection_epoch=connection.epoch,
            )
            try:
                return runner.dispatch(action, params)
            except ActionProtocolError:
                raise
            except Exception:
                raise PcBridgeError("bridge_command_failed") from None

    def close(self) -> None:
        with self._lock:
            runner, self._runner = self._runner, None
            self._connections.clear()
        close = getattr(runner, "close", None)
        if callable(close):
            close()


class LocalAppsBridge(RemoteAppsControlBridge):
    """Bridge-shaped saved-action API backed by the server's local ActionRunner."""

    def __init__(
        self,
        owner: str = "local",
        *,
        hub: LocalHub | None = None,
        runner: Any = None,
        config: Mapping[str, Any] | None = None,
        config_provider: Callable[[], Mapping[str, Any]] | None = None,
        runner_factory: Callable[[Mapping[str, Any], Path], Any] | None = None,
        journal_root: str | os.PathLike[str] | None = None,
    ) -> None:
        local_hub = hub or LocalHub(
            runner=runner,
            config=config,
            config_provider=config_provider,
            runner_factory=runner_factory,
            journal_root=journal_root,
        )
        super().__init__(str(owner), LOCAL_DEVICE_ID, local_hub)


_shared_hub: LocalHub | None = None
_shared_hub_lock = threading.Lock()


def bridge(owner: str = "local") -> LocalAppsBridge:
    """Return a Bridge-shaped target over the process-wide local action journal."""
    global _shared_hub
    with _shared_hub_lock:
        if _shared_hub is None:
            _shared_hub = LocalHub()
        hub = _shared_hub
    return LocalAppsBridge(owner, hub=hub)


__all__ = ["LOCAL_DEVICE_ID", "LocalAppsBridge", "LocalHub", "bridge"]
