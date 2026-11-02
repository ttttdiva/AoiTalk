"""Broker endpoint validation for the existing Edge browser.

This module intentionally never reads DevToolsActivePort and never connects to
Edge. Existing Edge access is available only through the Gateway's authenticated
edge_cdp_broker and its Browser.bind Playwright WebSocket.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

MAX_ACTIVE_PORT_BYTES = 4096
_CHANNEL_FOLDERS = {
    "stable": "Edge",
    "beta": "Edge Beta",
    "dev": "Edge Dev",
    "canary": "Edge SxS",
}
_BROWSER_PATH = re.compile(r"/devtools/browser(?:/[A-Za-z0-9][A-Za-z0-9._~-]{0,127})?\Z")
_BROKER_PATH = re.compile(r"/[A-Fa-f0-9]{32}\Z")


class EdgeCdpError(RuntimeError):
    """A stable diagnostic code without profile contents or private paths."""


@dataclass(frozen=True)
class EdgeCdpEndpoint:
    url: str = field(repr=False)
    source: str
    channel: str

    def __post_init__(self):
        object.__setattr__(self, "url", validate_loopback_endpoint(self.url))
        if self.source not in {"active_port", "explicit"}:
            raise EdgeCdpError("edge_cdp_source_invalid")
        if self.channel not in _CHANNEL_FOLDERS:
            raise EdgeCdpError("edge_channel_invalid")


@dataclass(frozen=True)
class EdgeCdpDiagnostic:
    status: str
    code: str
    endpoint: EdgeCdpEndpoint | None = field(default=None, repr=False)
    warnings: tuple[str, ...] = ()

    def public(self) -> dict:
        # Never expose a WebSocket debugger URL or infer connectivity here.
        return {
            "status": self.status,
            "code": self.code,
            "endpoint_candidate": self.endpoint is not None,
            "connected": False,
            "edge_process_verified": False,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class BrokerWebSocketEndpoint:
    """An ephemeral loopback Playwright endpoint returned by Browser.bind."""

    url: str = field(repr=False)
    source: str = "gateway_browser_bind"

    def __post_init__(self):
        object.__setattr__(self, "url", validate_broker_websocket_endpoint(self.url))
        if self.source != "gateway_browser_bind":
            raise EdgeCdpError("edge_broker_endpoint_invalid")


def validate_broker_websocket_endpoint(value: str) -> str:
    """Accept only the Gateway broker's ephemeral IPv4 loopback endpoint."""
    if not isinstance(value, str) or len(value) > 256 or any(ord(char) <= 32 for char in value):
        raise EdgeCdpError("edge_broker_endpoint_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise EdgeCdpError("edge_broker_endpoint_invalid") from None
    if port == 9222:
        raise EdgeCdpError("direct_edge_cdp_forbidden")
    if (
        parsed.scheme != "ws"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65_535
        or parsed.username is not None
        or parsed.password is not None
        or not _BROKER_PATH.fullmatch(parsed.path)
        or parsed.query
        or parsed.fragment
    ):
        raise EdgeCdpError("edge_broker_endpoint_invalid")
    return urlunsplit(("ws", f"127.0.0.1:{port}", parsed.path, "", ""))


def validate_loopback_endpoint(value: str) -> str:
    """Accept only literal loopback browser WebSockets without credentials."""
    if not isinstance(value, str) or len(value) > 512 or any(ord(c) <= 32 for c in value):
        raise EdgeCdpError("edge_cdp_endpoint_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        raise EdgeCdpError("edge_cdp_endpoint_invalid") from None
    if (
        parsed.scheme != "ws"
        or hostname not in {"127.0.0.1", "::1"}
        or not port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not _BROWSER_PATH.fullmatch(parsed.path)
    ):
        raise EdgeCdpError("edge_cdp_endpoint_invalid")
    host = "[::1]" if hostname == "::1" else "127.0.0.1"
    return urlunsplit(("ws", f"{host}:{port}", parsed.path, "", ""))


def edge_user_data_directory(
    channel: str = "stable", *, environ: Mapping[str, str] | None = None
) -> Path:
    del channel, environ
    raise EdgeCdpError("direct_edge_cdp_forbidden")


def read_edge_cdp_endpoint(
    user_data_dir: Path, *, channel: str = "stable"
) -> EdgeCdpEndpoint:
    del user_data_dir, channel
    raise EdgeCdpError("direct_edge_cdp_forbidden")


def diagnose_edge_cdp(
    *,
    user_data_dir: Path | None = None,
    channel: str = "stable",
    environ: Mapping[str, str] | None = None,
    explicit_endpoint: str | None = None,
    remote_debugging_allowed: bool | None = None,
    developer_tools_availability: int | None = None,
    observed_http_status: int | None = None,
) -> EdgeCdpDiagnostic:
    del user_data_dir, channel, environ, explicit_endpoint, remote_debugging_allowed
    del developer_tools_availability, observed_http_status
    return EdgeCdpDiagnostic("blocked", "direct_edge_cdp_forbidden")
