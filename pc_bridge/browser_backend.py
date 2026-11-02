"""Broker-only Playwright backend for existing Edge pages.

The general browser backend receives an ephemeral endpoint emitted by the
Gateway's authenticated Browser.bind broker. It never reads Edge profile files
or imports the agent-browser CDP helper. Owned Playwright profiles stay separate.
"""
from __future__ import annotations

from collections.abc import Mapping
import os

from .agent_browser_adapter import AgentBrowserBridge
from .brokered_playwright import BrokeredPlaywrightRuntime
from .edge import EdgeBridgeError
from .edge_cdp import EdgeCdpError, validate_broker_websocket_endpoint


def create_browser_backend(
    config: Mapping,
    *,
    cancel_event=None,
    runtime_factory=None,
    on_disconnect=None,
):
    """Create a broker-only browser bridge; connection remains lazy until use."""
    if not isinstance(config, Mapping):
        raise EdgeBridgeError("browser_backend_config_invalid")
    backend = config.get("browser_backend", "agent_browser")
    if backend != "agent_browser":
        raise EdgeBridgeError("browser_backend_unknown")
    settings = config.get("agent_browser", {})
    allowed = {"channel", "target_id", "broker_endpoint", "user_data_dir", "snapshot_scope"}
    if not isinstance(settings, Mapping) or set(settings) - allowed:
        if isinstance(settings, Mapping) and "endpoint" in settings:
            raise EdgeBridgeError("direct_edge_cdp_forbidden")
        raise EdgeBridgeError("agent_browser_config_invalid")
    if "endpoint" in settings:
        raise EdgeBridgeError("direct_edge_cdp_forbidden")
    if any(key.casefold() == "agent_browser_auto_connect" for key in os.environ):
        raise EdgeBridgeError("direct_edge_cdp_forbidden")
    if not isinstance(settings.get("channel", "stable"), str) or settings.get("channel", "stable") not in {
        "stable",
        "beta",
        "dev",
        "canary",
    }:
        raise EdgeBridgeError("edge_channel_invalid")
    target = settings.get("target_id")
    broker_endpoint = settings.get("broker_endpoint")
    if broker_endpoint is not None:
        try:
            broker_endpoint = validate_broker_websocket_endpoint(broker_endpoint)
        except EdgeCdpError as exc:
            if str(exc) == "direct_edge_cdp_forbidden":
                raise EdgeBridgeError("direct_edge_cdp_forbidden") from None
            raise EdgeBridgeError("edge_broker_endpoint_invalid") from None
    profile = settings.get("user_data_dir")
    if profile is not None and (
        not isinstance(profile, str) or not profile or "\0" in profile
    ):
        raise EdgeBridgeError("edge_profile_path_invalid")
    default_scope = settings.get("snapshot_scope")
    if default_scope is not None:
        if not isinstance(default_scope, Mapping):
            raise EdgeBridgeError("agent_browser_scope_invalid")
        default_scope = dict(default_scope)
        if "max_depth" in default_scope:
            if "depth" in default_scope:
                raise EdgeBridgeError("agent_browser_scope_invalid")
            default_scope["depth"] = default_scope.pop("max_depth")

    def fresh_runtime():
        if cancel_event is not None and cancel_event.is_set():
            raise EdgeBridgeError("agent_browser_cancelled")
        if not broker_endpoint:
            raise EdgeBridgeError("edge_broker_endpoint_required")
        if runtime_factory is not None:
            # This injection seam is only for fake broker tests.
            runtime = runtime_factory(broker_endpoint)
            set_callback = getattr(runtime, "set_disconnect_callback", None)
            if callable(set_callback) and on_disconnect is not None:
                set_callback(on_disconnect)
            return runtime
        return BrokeredPlaywrightRuntime(broker_endpoint, on_disconnect=on_disconnect)

    return AgentBrowserBridge(
        runtime_factory=fresh_runtime,
        selected_target_id=target,
        cancel_event=cancel_event,
        default_scope=default_scope,
        on_fault=on_disconnect,
    )
