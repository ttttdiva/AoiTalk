"""General browser helper and a lazy, manifest-pinned bundle.

Only trusted local configuration selects its target. No WSS payload can supply a
helper path, environment, manifest, or launcher. Construction and availability
checks never start a helper, probe CDP, or read an Edge profile. A missing custom
Windows bundle leaves computer operations usable and fails an explicit browser
run with a fixed error. No stock executable or extension fallback is attempted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from .agent_browser_adapter import AgentBrowserBridge, REQUIRED_CAPABILITIES
from .agent_browser_runtime import AgentBrowserRuntimeError, OwnedHelperRuntime
from .agent_browser_supervisor import (
    AgentBrowserPreparationError,
    HelperManifest,
    OFFICIAL_HELPER,
    verify_helper,
)
from .edge import EdgeBridgeError
from .edge_cdp import EdgeCdpEndpoint, EdgeCdpError, diagnose_edge_cdp
from .paths import data_dir, home
from .windows_edge_identity import EdgeEndpointIdentityVerifier

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


def _reject_constant(value):
    raise ValueError


def load_helper_bundle(
    directory: Path | None = None, *, require_pinned: bool | None = None
) -> BrowserHelperBundle:
    """Read one small explicit custom-build manifest and verify exact bytes.

    ``directory`` is build/test dependency injection, never a config field.
    There is intentionally no fabricated default manifest for the Windows build.
    """
    if require_pinned is None:
        # The packaged helper is production input. Explicit directories are a
        # test/build injection seam and remain independently hash-checked by
        # their manifest unless the caller is running a reviewed gate.
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
        raise AgentBrowserPreparationError(
            "agent_browser_manifest_unavailable"
        ) from None
    try:
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        keys = {
            "schema",
            "platform",
            "upstream_commit",
            "bridge_protocol",
            "daemon_stamp",
            "capabilities",
            "helper",
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
        manifest = HelperManifest(
            helper["version"], helper["sha256"], helper["size_bytes"]
        )
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
        ValueError,
        TypeError,
        UnicodeError,
        RecursionError,
        AgentBrowserPreparationError,
        OSError,
    ):
        raise AgentBrowserPreparationError("agent_browser_manifest_invalid") from None
    binary = root / "agent-browser.exe"
    _check_local_path(binary, "agent_browser_binary_path_invalid")
    return BrowserHelperBundle(
        verify_helper(binary, manifest), manifest, document["daemon_stamp"], build_manifest
    )


class _VerifiedEdgeRuntime:
    """Keep external browser identity separate from helper Job ownership."""

    def __init__(self, runtime, verifier, identity):
        self._runtime, self._verifier, self._identity = runtime, verifier, identity

    @property
    def endpoint(self):
        return self._runtime.endpoint

    @property
    def session_id(self):
        return self._runtime.session_id

    @property
    def attach_target(self):
        return self._runtime.attach_target

    @property
    def discover_existing(self):
        return self._runtime.discover_existing

    def _verify(self, *, outcome_unknown=False):
        try:
            self._verifier.verify(self.endpoint, self._identity)
        except Exception:
            self.stop()
            raise AgentBrowserRuntimeError(
                "edge_process_identity_changed", outcome_unknown=outcome_unknown
            ) from None

    def preflight(self, **kwargs):
        self._verify()
        result = self._runtime.preflight(**kwargs)
        self._verify()
        return result

    def set_attach_target(self, target):
        self._verify()
        self._runtime.set_attach_target(target)

    def run(self, args, **kwargs):
        self._verify()
        result = self._runtime.run(args, **kwargs)
        self._verify(outcome_unknown=True)
        return result

    def stop(self):
        # Never terminate Edge or touch its profile. Only the owned helper stops.
        try:
            return self._runtime.stop() is True
        except Exception:
            return False


def create_browser_backend(
    config: Mapping,
    *,
    cancel_event=None,
    bundle_directory: Path | None = None,
    require_pinned: bool | None = None,
    runtime_factory=None,
    edge_identity_verifier=None,
):
    """Select from trusted local config; postpone all preparation to a run.

    ``agent_browser`` accepts channel, target_id, endpoint, user_data_dir and
    snapshot_scope only. The three injection arguments are internal tests/build
    dependencies and are never populated from registration or command payloads.
    """
    if not isinstance(config, Mapping):
        raise EdgeBridgeError("browser_backend_config_invalid")
    backend = config.get("browser_backend", "agent_browser")
    if backend != "agent_browser":
        raise EdgeBridgeError("browser_backend_unknown")
    settings = config.get("agent_browser", {})
    allowed = {"channel", "target_id", "endpoint", "user_data_dir", "snapshot_scope"}
    if not isinstance(settings, Mapping) or set(settings) - allowed:
        raise EdgeBridgeError("agent_browser_config_invalid")
    channel = settings.get("channel", "stable")
    if not isinstance(channel, str) or channel not in {
        "stable",
        "beta",
        "dev",
        "canary",
    }:
        raise EdgeBridgeError("edge_channel_invalid")
    target = settings.get("target_id")
    explicit_endpoint = settings.get("endpoint")
    if explicit_endpoint is not None:
        # Syntax only. Ownership and reachability remain unverified here.
        EdgeCdpEndpoint(explicit_endpoint, "explicit", channel)
    profile = settings.get("user_data_dir")
    if profile is not None and (
        not isinstance(profile, str) or not profile or "\0" in profile
    ):
        raise EdgeBridgeError("edge_profile_path_invalid")
    profile = Path(profile) if profile is not None else None
    default_scope = settings.get("snapshot_scope")
    if default_scope is not None:
        if not isinstance(default_scope, Mapping):
            raise EdgeBridgeError("agent_browser_scope_invalid")
        default_scope = dict(default_scope)
        if "max_depth" in default_scope:
            if "depth" in default_scope:
                raise EdgeBridgeError("agent_browser_scope_invalid")
            default_scope["depth"] = default_scope.pop("max_depth")
    constructor = OwnedHelperRuntime if runtime_factory is None else runtime_factory
    verifier = (
        EdgeEndpointIdentityVerifier()
        if edge_identity_verifier is None
        else edge_identity_verifier
    )
    diagnostic_cache = {}

    def fresh_runtime():
        if cancel_event is not None and cancel_event.is_set():
            raise EdgeBridgeError("agent_browser_cancelled")
        bundle = load_helper_bundle(
            bundle_directory, require_pinned=require_pinned
        )
        # Observe fixed policy keys only during explicit preparation. These
        # raw platform values are not a resolved effective Edge policy.
        try:
            from .windows_edge_policy import read_edge_policy_snapshot
            diagnostic_cache["edge_policy"] = read_edge_policy_snapshot().summary()
        except Exception:
            diagnostic_cache["edge_policy"] = {
                "effective_policy": "unknown", "remote_debugging": "unreadable",
                "developer_tools": "unreadable", "conflicts": False, "partial_read": True,
                "cloud_precedence": "not_resolved", "url_scope": "not_observed",
                "edge_process_presence": "unknown",
                "platform": "windows" if os.name == "nt" else "unsupported",
            }
        diagnostic = diagnose_edge_cdp(
            user_data_dir=profile, channel=channel, explicit_endpoint=explicit_endpoint
        )
        if diagnostic.endpoint is None:
            raise EdgeCdpError(diagnostic.code)
        endpoint = diagnostic.endpoint
        identity = verifier.capture(endpoint)
        session = "aoi-" + uuid4().hex
        state_root = data_dir() / "agent-browser"
        _check_local_path(state_root, "agent_browser_state_path_invalid")
        if cancel_event is not None and cancel_event.is_set():
            raise EdgeBridgeError("agent_browser_cancelled")
        state_root.mkdir(parents=True, exist_ok=True)
        _check_local_path(state_root, "agent_browser_state_path_invalid")
        # Only these OS roots may reach the helper, never server/token/API keys.
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"SYSTEMROOT", "WINDIR"}
        }
        runtime = constructor(
            binary=bundle.binary,
            manifest=bundle.manifest,
            required_capabilities=REQUIRED_CAPABILITIES,
            expected_daemon_stamp=bundle.daemon_stamp,
            session_id=session,
            state_directory=state_root / session,
            endpoint=endpoint,
            attach_target=target,
            discover_existing=target is None,
            inherited_environment=environment,
        )
        return _VerifiedEdgeRuntime(runtime, verifier, identity)

    return AgentBrowserBridge(
        runtime_factory=fresh_runtime,
        selected_target_id=target,
        cancel_event=cancel_event,
        default_scope=default_scope,
        diagnostic_cache=diagnostic_cache,
    )
