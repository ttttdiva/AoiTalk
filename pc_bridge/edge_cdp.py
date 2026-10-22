"""Read-only Edge CDP endpoint discovery; never enables remote debugging.

An endpoint on disk is only a candidate, not evidence of an authenticated CDP
connection or of Edge process ownership. In particular an HTTP discovery 404
does not rule out Edge's UI-enabled WebSocket-only debugging endpoint.
These helpers are used only by the opt-in ``agent_browser`` backend; the
existing ``extension`` backend remains a separate Native Messaging path.
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
    if channel not in _CHANNEL_FOLDERS:
        raise EdgeCdpError("edge_channel_invalid")
    local = (os.environ if environ is None else environ).get("LOCALAPPDATA", "")
    if not local or not Path(local).is_absolute():
        raise EdgeCdpError("edge_local_app_data_unavailable")
    return Path(local) / "Microsoft" / _CHANNEL_FOLDERS[channel] / "User Data"


def read_edge_cdp_endpoint(
    user_data_dir: Path, *, channel: str = "stable"
) -> EdgeCdpEndpoint:
    """Read the browser-owned file once, with a small bounded read, no probes."""
    directory = Path(user_data_dir)
    if not directory.is_absolute() or str(directory).startswith(("\\\\", "//")):
        raise EdgeCdpError("edge_profile_path_invalid")
    path = directory / "DevToolsActivePort"
    try:
        if path.is_symlink():
            raise EdgeCdpError("edge_active_port_link_rejected")
        with path.open("rb") as stream:
            raw = stream.read(MAX_ACTIVE_PORT_BYTES + 1)
    except FileNotFoundError:
        raise EdgeCdpError("edge_debugging_endpoint_missing") from None
    except PermissionError:
        raise EdgeCdpError("edge_debugging_endpoint_unreadable") from None
    except OSError:
        raise EdgeCdpError("edge_debugging_endpoint_unreadable") from None
    if len(raw) > MAX_ACTIVE_PORT_BYTES:
        raise EdgeCdpError("edge_active_port_invalid")
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeError:
        raise EdgeCdpError("edge_active_port_invalid") from None
    if not 1 <= len(lines) <= 2 or not re.fullmatch(r"[0-9]{1,5}", lines[0]):
        raise EdgeCdpError("edge_active_port_invalid")
    port = int(lines[0])
    if not 1 <= port <= 65535:
        raise EdgeCdpError("edge_active_port_invalid")
    browser_path = lines[1] if len(lines) == 2 else "/devtools/browser"
    try:
        return EdgeCdpEndpoint(
            f"ws://127.0.0.1:{port}{browser_path}", "active_port", channel
        )
    except EdgeCdpError:
        raise EdgeCdpError("edge_active_port_invalid") from None


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
    """Describe locally available evidence without networking or settings writes.

Policy and HTTP evidence are optional caller-observed inputs. Unknown policy
is not represented as permission. No state returned here means 'connected'.
"""
    if remote_debugging_allowed is False:
        return EdgeCdpDiagnostic("blocked", "edge_remote_debugging_policy_blocked")
    warnings = []
    if developer_tools_availability == 2:
        warnings.append("edge_developer_tools_policy_restricted")
    if observed_http_status == 404:
        warnings.append("edge_http_discovery_unavailable_websocket_unverified")
    try:
        if channel not in _CHANNEL_FOLDERS:
            raise EdgeCdpError("edge_channel_invalid")
        if explicit_endpoint is not None:
            endpoint = EdgeCdpEndpoint(explicit_endpoint, "explicit", channel)
        else:
            directory = user_data_dir if user_data_dir is not None else edge_user_data_directory(channel, environ=environ)
            endpoint = read_edge_cdp_endpoint(directory, channel=channel)
    except EdgeCdpError as exc:
        return EdgeCdpDiagnostic("unavailable", str(exc), warnings=tuple(warnings))
    return EdgeCdpDiagnostic(
        "endpoint_candidate", "edge_cdp_connection_unverified", endpoint, tuple(warnings)
    )
