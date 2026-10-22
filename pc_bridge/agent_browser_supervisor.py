"""Side-effect-free preparation for a future owned agent-browser supervisor.

This module verifies packaged bytes and describes public CLI invocations. It
does not start programs, create state files, connect CDP, or change the current
extension backend. A Windows process owner must enforce the lifecycle contract
before executing these plans. Snapshot/action commands are intentionally absent
until strict-ref and value-redaction parity has been implemented.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
from typing import Mapping
from uuid import uuid4

from .edge_cdp import EdgeCdpEndpoint


class AgentBrowserPreparationError(RuntimeError):
    pass


@dataclass(frozen=True)
class HelperManifest:
    version: str
    sha256: str
    size_bytes: int

    def __post_init__(self):
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[+-][A-Za-z0-9.-]+)?", self.version):
            raise AgentBrowserPreparationError("agent_browser_manifest_invalid")
        if not re.fullmatch(r"[a-f0-9]{64}", self.sha256):
            raise AgentBrowserPreparationError("agent_browser_manifest_invalid")
        if type(self.size_bytes) is not int or not 1 <= self.size_bytes <= 64 * 1024 * 1024:
            raise AgentBrowserPreparationError("agent_browser_manifest_invalid")


# The shipped helper is the reviewed bridge.4 custom build from the pinned
# upstream source/patch checkpoint. The stock 0.38.2 binary is deliberately
# not accepted by the default production verifier.
OFFICIAL_HELPER = HelperManifest(
    version="0.38.2",
    sha256="83cacd4103400c1a1ae73c9a1fc0148d9afa1c9c65f4ea7f6e131018ac6d7217",
    size_bytes=20263424,
)


def verify_helper(binary: Path, manifest: HelperManifest = OFFICIAL_HELPER) -> Path:
    """Check exact packaged bytes; no download, extraction, execution, or repair."""
    path = Path(binary)
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise AgentBrowserPreparationError("agent_browser_binary_path_invalid")
    if path.is_symlink():
        raise AgentBrowserPreparationError("agent_browser_binary_path_invalid")
    try:
        if not path.is_file() or path.stat().st_size != manifest.size_bytes:
            raise AgentBrowserPreparationError("agent_browser_binary_size_mismatch")
        digest = hashlib.sha256()
        count = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                count += len(chunk)
                if count > manifest.size_bytes:
                    raise AgentBrowserPreparationError("agent_browser_binary_size_mismatch")
                digest.update(chunk)
    except OSError:
        raise AgentBrowserPreparationError("agent_browser_binary_unavailable") from None
    if count != manifest.size_bytes or digest.hexdigest() != manifest.sha256:
        raise AgentBrowserPreparationError("agent_browser_binary_hash_mismatch")
    return path


@dataclass(frozen=True)
class HelperCommand:
    argv: tuple[str, ...] = field(repr=False)
    cwd: Path = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)
    # A cleanup CLI can itself spawn a daemon. The executor must first verify
    # the exact recorded daemon still exists; never run this plan speculatively.
    requires_existing_owned_daemon: bool = False
    timeout_seconds: float = 10.0


class AgentBrowserSessionPlan:
    """A fresh namespace and a small allowlist of preflight/lifecycle commands."""

    def __init__(
        self,
        *,
        binary: Path,
        state_root: Path,
        endpoint: EdgeCdpEndpoint,
        manifest: HelperManifest = OFFICIAL_HELPER,
        inherited_environment: Mapping[str, str] | None = None,
        session_id: str | None = None,
    ):
        self.binary = verify_helper(binary, manifest)
        root = Path(state_root)
        if not root.is_absolute() or str(root).startswith(("\\\\", "//")):
            raise AgentBrowserPreparationError("agent_browser_state_path_invalid")
        session = session_id if session_id is not None else "aoi-" + uuid4().hex
        if not re.fullmatch(r"aoi-[a-f0-9]{32}", session):
            raise AgentBrowserPreparationError("agent_browser_session_invalid")
        self.session_id = session
        self.state_directory = root / session
        self.config_path = self.state_directory / "agent-browser.json"
        self.endpoint = endpoint
        self.manifest = manifest
        inherited = os.environ if inherited_environment is None else inherited_environment
        # Do not inherit the server's keys, browser providers, restore settings,
        # profiles, plugins, or a user's unrelated agent-browser configuration.
        allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA"}
        self._environment = {
            key: value for key, value in inherited.items() if key.upper() in allowed
        }
        self._environment.update({
            "AGENT_BROWSER_SOCKET_DIR": str(self.state_directory / "ipc"),
            "AGENT_BROWSER_NAMESPACE": "aoitalk-pc-bridge",
            "AGENT_BROWSER_NO_AUTO_DIALOG": "1",
            "AGENT_BROWSER_NO_WEBMCP": "1",
            "AGENT_BROWSER_AUTOSAVE_INTERVAL_MS": "0",
        })

    @property
    def config_contents(self) -> str:
        # Executor writes only this owned config before startup. This class
        # intentionally leaves the filesystem untouched while preparing it.
        return "{}\n"

    def _command(self, args, *, connect=True, cleanup=False) -> HelperCommand:
        argv = [
            str(self.binary), "--config", str(self.config_path),
            "--namespace", "aoitalk-pc-bridge", "--session", self.session_id,
            "--json", "--no-auto-dialog", "--no-webmcp",
        ]
        if connect:
            # Even after daemon loss, a read-only command must never launch a
            # new isolated browser or auto-discover another user's browser.
            argv.extend(("--cdp", self.endpoint.url))
        argv.extend(args)
        return HelperCommand(
            tuple(argv), self.state_directory, dict(self._environment), cleanup
        )

    def version(self) -> HelperCommand:
        return HelperCommand((str(self.binary), "--version"), self.binary.parent,
                             dict(self._environment))

    def connect(self) -> HelperCommand:
        return self._command(("connect", self.endpoint.url))

    def list_tabs(self) -> HelperCommand:
        return self._command(("tab", "list"))

    def disable_stream(self) -> HelperCommand:
        return self._command(("stream", "disable"), connect=False, cleanup=True)

    def close(self) -> HelperCommand:
        # No --cdp on cleanup: it must not reconnect a disconnected browser.
        # No --all: ownership is bounded to our recorded session/daemon.
        return self._command(("close",), connect=False, cleanup=True)
