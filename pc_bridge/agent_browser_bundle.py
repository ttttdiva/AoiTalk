"""Build-time verification for the retired agent-browser helper bundle.

The PC Bridge application no longer imports or packages this module. It is
kept for source/license preparation tooling and offline manifest checks only.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys

from .agent_browser_adapter import REQUIRED_CAPABILITIES
from .agent_browser_supervisor import (
    AgentBrowserPreparationError,
    HelperManifest,
    OFFICIAL_HELPER,
    verify_helper,
)
from .paths import home

PINNED_UPSTREAM_COMMIT = "39a74c70d7759d5a6de7a22c04570bb626bbd081"
PINNED_VERSION = "0.38.2"
PINNED_DAEMON_STAMP = "0.38.2+bridge.4"
PINNED_HELPER_SHA256 = OFFICIAL_HELPER.sha256
PINNED_HELPER_SIZE_BYTES = OFFICIAL_HELPER.size_bytes
MAX_MANIFEST_BYTES = 16384
MAX_BUILD_MANIFEST_BYTES = 1024 * 1024
PINNED_BUILD_MANIFEST_SHA256 = "3a92c1662308f9f4d7269bf52763e59cb5e33245cebb6cacee82812368c663ff"


@dataclass(frozen=True)
class BrowserHelperBundle:
    binary: Path
    manifest: HelperManifest
    daemon_stamp: str
    build_manifest: Path | None = None


def _check_local_path(path: Path, error: str) -> None:
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise AgentBrowserPreparationError(error)
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
            raise AgentBrowserPreparationError(error)


def bundled_helper_directory() -> Path:
    base = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else home()
    return base / "resources" / "agent-browser"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError


def load_helper_bundle(
    directory: Path | None = None, *, require_pinned: bool | None = None
) -> BrowserHelperBundle:
    """Verify fixed helper bytes for offline build/license tooling only."""
    if require_pinned is None:
        require_pinned = directory is None
    root = bundled_helper_directory() if directory is None else Path(directory)
    _check_local_path(root, "agent_browser_bundle_unavailable")
    path = root / "manifest.json"
    _check_local_path(path, "agent_browser_manifest_unavailable")
    try:
        if not path.is_file():
            raise OSError
        with path.open("rb") as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
    except OSError:
        raise AgentBrowserPreparationError("agent_browser_manifest_unavailable") from None
    try:
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        keys = {
            "schema", "platform", "upstream_commit", "bridge_protocol",
            "daemon_stamp", "capabilities", "helper",
        }
        if (
            not isinstance(document, dict)
            or set(document) not in (keys, keys | {"build_manifest"})
            or type(document["schema"]) is not int
            or document["schema"] != 1
            or document["platform"] != "win32-x64"
            or document["upstream_commit"] != PINNED_UPSTREAM_COMMIT
            or type(document["bridge_protocol"]) is not int
            or document["bridge_protocol"] != 1
            or document["daemon_stamp"] != PINNED_DAEMON_STAMP
        ):
            raise ValueError
        capabilities = document["capabilities"]
        if (
            not isinstance(capabilities, list)
            or len(capabilities) != len(REQUIRED_CAPABILITIES)
            or any(not isinstance(item, str) for item in capabilities)
            or frozenset(capabilities) != REQUIRED_CAPABILITIES
        ):
            raise ValueError
        helper = document["helper"]
        if (
            not isinstance(helper, dict)
            or set(helper) != {"filename", "version", "size_bytes", "sha256"}
            or helper["filename"] != "agent-browser.exe"
            or helper["version"] != PINNED_VERSION
            or not isinstance(helper["sha256"], str)
        ):
            raise ValueError
        manifest = HelperManifest(helper["version"], helper["sha256"], helper["size_bytes"])
        if require_pinned and (
            manifest.sha256 != PINNED_HELPER_SHA256
            or manifest.size_bytes != PINNED_HELPER_SIZE_BYTES
        ):
            raise ValueError
        build_manifest = None
        provenance = document.get("build_manifest")
        if require_pinned and provenance is None:
            raise ValueError
        if provenance is not None:
            if (
                not isinstance(provenance, dict)
                or set(provenance) != {"filename", "size_bytes", "sha256"}
                or provenance["filename"] != "build-manifest.json"
                or type(provenance["size_bytes"]) is not int
                or not 1 <= provenance["size_bytes"] <= MAX_BUILD_MANIFEST_BYTES
                or not isinstance(provenance["sha256"], str)
                or (require_pinned and provenance["sha256"] != PINNED_BUILD_MANIFEST_SHA256)
            ):
                raise ValueError
            build_manifest = root / "build-manifest.json"
            _check_local_path(build_manifest, "agent_browser_manifest_invalid")
            with build_manifest.open("rb") as stream:
                build_bytes = stream.read(MAX_BUILD_MANIFEST_BYTES + 1)
            if (
                len(build_bytes) != provenance["size_bytes"]
                or hashlib.sha256(build_bytes).hexdigest() != provenance["sha256"]
            ):
                raise ValueError
    except (
        ValueError, TypeError, UnicodeError, RecursionError,
        AgentBrowserPreparationError, OSError,
    ):
        raise AgentBrowserPreparationError("agent_browser_manifest_invalid") from None
    binary = root / "agent-browser.exe"
    _check_local_path(binary, "agent_browser_binary_path_invalid")
    return BrowserHelperBundle(
        verify_helper(binary, manifest), manifest, document["daemon_stamp"], build_manifest
    )
