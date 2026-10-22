"""Fixed protocol and local catalog validation for saved PC actions.

The server never sends a command line or a filesystem path to a PC.  A PC
resolves ``saved_action_id`` in its operator maintained catalog and rechecks
the catalog entry before it accepts a run.  Keeping this module independent
from the WebSocket client makes it usable by the journal and the supervisor
without importing the GUI or server runtime.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class ActionProtocolError(ValueError):
    """A fixed saved-action request or catalog entry is invalid."""


ACTION_CHANNEL = "apps"
ACTION_NAMES = frozenset({"start", "status", "logs", "stop", "upload", "download"})
ACTION_MODES = frozenset({"finite", "service", "desktop"})
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
MACHINE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
REF_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,219}\Z")
FILE_REF_PATTERN = re.compile(r"files/[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")

MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024
MAX_LOG_LINE_BYTES = 16 * 1024
MAX_LOG_LIMIT = 200
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_FILE_CHUNK_BYTES = 1024 * 1024
MAX_DEPENDENCIES = 64
MAX_DEPENDENCY_BYTES = 64 * 1024 * 1024

_START_FIELDS = frozenset({
    "saved_action_id", "sha256", "contract_hash", "run_id", "action_ref", "input",
})
_REQUEST_ID_FIELDS = frozenset({"run_id"})
_LOG_FIELDS = frozenset({"run_id", "cursor", "limit"})
_UPLOAD_FIELDS = frozenset({"run_id", "ref", "offset", "data", "chunk_sha256"})
_DOWNLOAD_FIELDS = frozenset({"run_id", "ref", "offset", "limit"})


def canonical_json(value: Any) -> str:
    """Return the one canonical JSON representation used for input hashes."""
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ActionProtocolError("apps_input_invalid") from exc


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def saved_action_contract_hash(*, sha256: str, mode: str, probe: Mapping[str, Any],
                               dependencies: Any = ()) -> str:
    """Compute the stable local saved-action fingerprint.

    The payload deliberately contains code/dependency hashes and execution
    policy, while omitting absolute paths.  Server/runtime code may use this
    helper when it wants a contract that changes with any declared code,
    mode, probe, or dependency update.  Transitive dependencies remain the
    operator's explicit responsibility.
    """
    code_hash = _sha(sha256, "saved_action_sha256")
    if mode not in ACTION_MODES:
        raise ActionProtocolError("saved_action_mode_invalid")
    if not isinstance(probe, Mapping):
        raise ActionProtocolError("saved_action_probe_invalid")
    normalized: list[str] = []
    if not isinstance(dependencies, (list, tuple)):
        raise ActionProtocolError("saved_action_dependencies_invalid")
    for item in dependencies:
        if isinstance(item, Mapping):
            value = item.get("sha256")
        elif hasattr(item, "sha256"):
            value = getattr(item, "sha256")
        else:
            value = item
        normalized.append(_sha(value, "saved_action_dependency_sha256"))
    if len(normalized) > MAX_DEPENDENCIES:
        raise ActionProtocolError("saved_action_dependencies_too_many")
    return sha256_json({"sha256": code_hash, "mode": mode, "probe": dict(probe),
                        "dependencies": normalized})


def sha256_file(path: Path, *, max_bytes: int | None = None) -> str:
    """Hash a regular, non-linked file without following an action link."""
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ActionProtocolError("saved_action_path_unavailable") from exc
    if not path.is_file() or path.is_symlink() or not info.st_nlink == 1:
        raise ActionProtocolError("saved_action_path_invalid")
    if max_bytes is not None and info.st_size > max_bytes:
        raise ActionProtocolError("saved_action_too_large")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise ActionProtocolError("saved_action_path_unavailable") from exc
    return digest.hexdigest()


def _bounded_text(value: Any, name: str, *, max_bytes: int = 4096) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ActionProtocolError(f"{name}_invalid")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ActionProtocolError(f"{name}_invalid") from exc
    if size > max_bytes:
        raise ActionProtocolError(f"{name}_too_long")
    return value


def _identifier(value: Any, name: str, pattern=ID_PATTERN) -> str:
    value = _bounded_text(value, name, max_bytes=128)
    if not pattern.fullmatch(value):
        raise ActionProtocolError(f"{name}_invalid")
    return value


def _sha(value: Any, name: str) -> str:
    value = _bounded_text(value, name, max_bytes=64)
    if not SHA256_PATTERN.fullmatch(value):
        raise ActionProtocolError(f"{name}_invalid")
    return value


def _json_safe(value: Any, *, depth: int = 0) -> None:
    """Validate bounded JSON while rejecting path/command escape objects.

    A saved action receives data through a fixed input file.  File references
    may only point into the current run directory and are validated separately
    by ``validate_file_ref``.  This prevents a caller from smuggling a path,
    command, executable, cwd, or argument list through the input protocol.
    """
    if depth > 32:
        raise ActionProtocolError("apps_input_too_deep")
    if value is None or isinstance(value, (str, int, bool)):
        if isinstance(value, str) and len(value.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ActionProtocolError("apps_input_too_large")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ActionProtocolError("apps_input_invalid")
        return
    if isinstance(value, list):
        if len(value) > 4096:
            raise ActionProtocolError("apps_input_too_large")
        for item in value:
            _json_safe(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 256:
            raise ActionProtocolError("apps_input_too_large")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128 or "\x00" in key:
                raise ActionProtocolError("apps_input_invalid")
            # These are deliberately reserved at every level.  A file ref is
            # represented by ``{"ref": "files/...", ...}`` only.
            if key.lower() in {"path", "command", "commands", "executable", "cwd", "working_directory", "args", "argv"}:
                raise ActionProtocolError("apps_input_reserved_field")
            _json_safe(item, depth=depth + 1)
        return
    raise ActionProtocolError("apps_input_invalid")


def validate_input(value: Any) -> Any:
    _json_safe(value)
    encoded = canonical_json(value).encode("utf-8")
    if len(encoded) > MAX_INPUT_BYTES:
        raise ActionProtocolError("apps_input_too_large")
    return value


def validate_file_ref(value: Any) -> str:
    """Validate a run-scoped reference, never an arbitrary local path."""
    value = _bounded_text(value, "file_ref", max_bytes=256)
    if (not FILE_REF_PATTERN.fullmatch(value) or ".." in value.split("/")
            or "\\" in value or value.endswith("/") or value.startswith("/")):
        raise ActionProtocolError("apps_file_ref_invalid")
    return value


def validate_action_request(action: Any, params: Any) -> dict[str, Any]:
    """Validate the exact apps-channel wire shape and return a detached copy."""
    action = _bounded_text(action, "apps_action", max_bytes=32)
    if action not in ACTION_NAMES:
        raise ActionProtocolError("apps_action_invalid")
    if not isinstance(params, dict):
        raise ActionProtocolError("apps_params_invalid")
    keys = set(params)
    if action == "start":
        if keys != _START_FIELDS:
            raise ActionProtocolError("apps_start_params_invalid")
        result = {
            "saved_action_id": _identifier(params["saved_action_id"], "saved_action_id"),
            "sha256": _sha(params["sha256"], "sha256"),
            "contract_hash": _sha(params["contract_hash"], "contract_hash"),
            "run_id": _identifier(params["run_id"], "run_id"),
            "action_ref": _bounded_text(params["action_ref"], "action_ref", max_bytes=220),
            "input": validate_input(params["input"]),
        }
        if not REF_PATTERN.fullmatch(result["action_ref"]):
            raise ActionProtocolError("action_ref_invalid")
        return result
    if action in {"status", "stop"}:
        if keys != _REQUEST_ID_FIELDS:
            raise ActionProtocolError(f"apps_{action}_params_invalid")
        return {"run_id": _identifier(params["run_id"], "run_id")}
    if action == "logs":
        if not keys <= _LOG_FIELDS or "run_id" not in keys:
            raise ActionProtocolError("apps_logs_params_invalid")
        result = {"run_id": _identifier(params["run_id"], "run_id")}
        cursor = params.get("cursor", 0)
        limit = params.get("limit", 100)
        if type(cursor) is not int or cursor < 0 or cursor > 2**63 - 1:
            raise ActionProtocolError("apps_logs_cursor_invalid")
        if type(limit) is not int or not 1 <= limit <= MAX_LOG_LIMIT:
            raise ActionProtocolError("apps_logs_limit_invalid")
        result.update(cursor=cursor, limit=limit)
        return result
    if action == "upload":
        if keys != _UPLOAD_FIELDS:
            raise ActionProtocolError("apps_upload_params_invalid")
        result = {"run_id": _identifier(params["run_id"], "run_id"),
                  "ref": validate_file_ref(params["ref"]),
                  "offset": params["offset"], "data": params["data"],
                  "chunk_sha256": _sha(params["chunk_sha256"], "chunk_sha256")}
        if type(result["offset"]) is not int or not 0 <= result["offset"] <= MAX_FILE_BYTES:
            raise ActionProtocolError("apps_upload_offset_invalid")
        if not isinstance(result["data"], str) or not result["data"]:
            raise ActionProtocolError("apps_upload_data_invalid")
        try:
            import base64
            raw = base64.b64decode(result["data"], validate=True)
        except (ValueError, TypeError):
            raise ActionProtocolError("apps_upload_data_invalid") from None
        if not 0 < len(raw) <= MAX_FILE_CHUNK_BYTES:
            raise ActionProtocolError("apps_upload_data_invalid")
        if hashlib.sha256(raw).hexdigest() != result["chunk_sha256"]:
            raise ActionProtocolError("apps_upload_hash_mismatch")
        return result
    if action == "download":
        if not keys <= _DOWNLOAD_FIELDS or not {"run_id", "ref"} <= keys:
            raise ActionProtocolError("apps_download_params_invalid")
        result = {"run_id": _identifier(params["run_id"], "run_id"),
                  "ref": validate_file_ref(params["ref"]),
                  "offset": params.get("offset", 0), "limit": params.get("limit", MAX_FILE_CHUNK_BYTES)}
        if type(result["offset"]) is not int or not 0 <= result["offset"] <= MAX_FILE_BYTES:
            raise ActionProtocolError("apps_download_offset_invalid")
        if type(result["limit"]) is not int or not 1 <= result["limit"] <= MAX_FILE_CHUNK_BYTES:
            raise ActionProtocolError("apps_download_limit_invalid")
        return result
    raise ActionProtocolError("apps_action_invalid")


@dataclass(frozen=True)
class SavedActionDependency:
    path: Path
    sha256: str

    def wire(self) -> dict[str, Any]:
        # Dependency paths are local operator data and never cross the wire.
        return {"sha256": self.sha256}


def _validate_local_dependency(value: Any) -> SavedActionDependency:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        raise ActionProtocolError("saved_action_dependency_invalid")
    raw_path = value.get("path")
    if (not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path
            or raw_path.startswith(("\\\\", "//"))
            or any(char in raw_path for char in "&|<>^%!\"\r\n")):
        raise ActionProtocolError("saved_action_dependency_path_invalid")
    path = Path(raw_path)
    if not path.is_absolute() or path.is_symlink():
        raise ActionProtocolError("saved_action_dependency_path_invalid")
    for parent in (path, *path.parents):
        if parent == parent.anchor:
            break
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise ActionProtocolError("saved_action_dependency_path_invalid")
    try:
        info = path.lstat()
    except OSError as exc:
        raise ActionProtocolError("saved_action_dependency_unavailable") from exc
    if not path.is_file() or info.st_nlink != 1:
        raise ActionProtocolError("saved_action_dependency_path_invalid")
    digest = _sha(value.get("sha256"), "saved_action_dependency_sha256")
    if sha256_file(path, max_bytes=MAX_DEPENDENCY_BYTES) != digest:
        raise ActionProtocolError("saved_action_dependency_hash_mismatch")
    return SavedActionDependency(path, digest)


@dataclass(frozen=True)
class SavedAction:
    saved_action_id: str
    path: Path
    sha256: str
    contract_hash: str
    mode: str
    probe: dict[str, Any]
    operator_approved: bool
    dependencies: tuple[SavedActionDependency, ...] = ()
    contract_fingerprint: str | None = None
    description: str = ""

    def wire(self) -> dict[str, Any]:
        # Never expose the local path to a server/API response.
        return {"saved_action_id": self.saved_action_id, "sha256": self.sha256,
                "contract_hash": self.contract_hash, "mode": self.mode,
                "probe": dict(self.probe), "operator_approved": self.operator_approved,
                "dependencies": [item.wire() for item in self.dependencies],
                "contract_fingerprint": self.contract_fingerprint or saved_action_contract_hash(
                    sha256=self.sha256, mode=self.mode, probe=self.probe,
                    dependencies=self.dependencies),
                "description": self.description}


def _validate_probe(value: Any, mode: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value or len(value) > 16:
        raise ActionProtocolError("saved_action_probe_invalid")
    kind = value.get("kind")
    if kind not in {"none", "tcp", "process", "window"}:
        raise ActionProtocolError("saved_action_probe_invalid")
    if mode == "finite" and kind != "none":
        raise ActionProtocolError("saved_action_probe_invalid")
    if mode != "finite" and kind == "none":
        raise ActionProtocolError("saved_action_probe_required")
    result = dict(value)
    if kind == "tcp":
        host, port = result.get("host"), result.get("port")
        if (not isinstance(host, str) or not host or len(host) > 255 or "\x00" in host
                or type(port) is not int or not 1 <= port <= 65535):
            raise ActionProtocolError("saved_action_probe_invalid")
        timeout = result.get("timeout_seconds", 1.0)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0.05 <= timeout <= 30:
            raise ActionProtocolError("saved_action_probe_invalid")
        result["timeout_seconds"] = float(timeout)
        # A TCP listener is accepted only when its owning process can be tied
        # to this action's child process.  This is a policy marker, not a way
        # to assert readiness from a pre-existing listener.
        result["require_child_owner"] = True
    elif kind == "process":
        if "name" in result and (not isinstance(result["name"], str) or not result["name"] or len(result["name"]) > 255):
            raise ActionProtocolError("saved_action_probe_invalid")
        if "exe_sha256" in result:
            result["exe_sha256"] = _sha(result["exe_sha256"], "saved_action_probe_exe_sha256")
        if not result.get("name") and not result.get("exe_sha256"):
            raise ActionProtocolError("saved_action_probe_invalid")
    elif kind == "window":
        title = result.get("title")
        if not isinstance(title, str) or not title or len(title) > 512 or "\x00" in title:
            raise ActionProtocolError("saved_action_probe_invalid")
        if "exe_sha256" in result:
            result["exe_sha256"] = _sha(result["exe_sha256"], "saved_action_probe_exe_sha256")
        # A title alone is never considered sufficient for a newly launched
        # desktop.  Supervisor requires an owned process identity as well.
        result["require_child_owner"] = True
    return result


def validate_catalog_entry(saved_action_id: Any, value: Any) -> SavedAction:
    saved_action_id = _identifier(saved_action_id, "saved_action_id")
    if not isinstance(value, Mapping):
        raise ActionProtocolError("saved_action_entry_invalid")
    allowed = {"path", "sha256", "contract_hash", "mode", "probe", "operator_approved",
               "dependencies", "contract_fingerprint", "description"}
    if set(value) - allowed:
        raise ActionProtocolError("saved_action_entry_invalid")
    raw_path = value.get("path")
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise ActionProtocolError("saved_action_path_invalid")
    # ``cmd.exe`` is used only for cataloged .bat/.cmd files.  Keep UNC and
    # shell metacharacter paths out of that fixed launcher grammar; an
    # operator can still use a local absolute path with a normal filename.
    if (raw_path.startswith(("\\\\", "//"))
            or any(char in raw_path for char in "&|<>^%!\"\r\n")):
        raise ActionProtocolError("saved_action_path_invalid")
    path = Path(raw_path)
    if not path.is_absolute() or path.is_symlink():
        raise ActionProtocolError("saved_action_path_invalid")
    for parent in (path, *path.parents):
        if parent == parent.anchor:
            break
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise ActionProtocolError("saved_action_path_invalid")
    try:
        info = path.lstat()
    except OSError as exc:
        raise ActionProtocolError("saved_action_path_unavailable") from exc
    if not path.is_file() or info.st_nlink != 1:
        raise ActionProtocolError("saved_action_path_invalid")
    suffix = path.suffix.lower()
    if suffix not in {".bat", ".cmd", ".exe", ".com", ".py", ".ps1"}:
        raise ActionProtocolError("saved_action_type_invalid")
    digest = _sha(value.get("sha256"), "saved_action_sha256")
    actual = sha256_file(path)
    if actual != digest:
        raise ActionProtocolError("saved_action_hash_mismatch")
    mode = value.get("mode")
    if mode not in ACTION_MODES:
        raise ActionProtocolError("saved_action_mode_invalid")
    probe = _validate_probe(value.get("probe"), mode)
    raw_dependencies = value.get("dependencies", [])
    if not isinstance(raw_dependencies, (list, tuple)) or len(raw_dependencies) > MAX_DEPENDENCIES:
        raise ActionProtocolError("saved_action_dependencies_invalid")
    dependencies = tuple(_validate_local_dependency(item) for item in raw_dependencies)
    if len({item.path for item in dependencies}) != len(dependencies):
        raise ActionProtocolError("saved_action_dependency_duplicate")
    computed_fingerprint = saved_action_contract_hash(
        sha256=digest, mode=mode, probe=probe, dependencies=dependencies)
    declared_contract = value.get("contract_hash")
    if declared_contract is not None:
        declared_contract = _sha(declared_contract, "saved_action_contract_hash")
        if declared_contract != computed_fingerprint:
            raise ActionProtocolError("saved_action_contract_hash_mismatch")
    declared_fingerprint = value.get("contract_fingerprint")
    if declared_fingerprint is not None:
        declared_fingerprint = _sha(declared_fingerprint, "saved_action_contract_fingerprint")
        if declared_fingerprint != computed_fingerprint:
            raise ActionProtocolError("saved_action_contract_fingerprint_mismatch")
    if value.get("operator_approved") is not True:
        raise ActionProtocolError("saved_action_not_approved")
    description = value.get("description", "")
    if not isinstance(description, str) or len(description.encode("utf-8")) > 512 or "\x00" in description:
        raise ActionProtocolError("saved_action_description_invalid")
    return SavedAction(saved_action_id, path, digest, computed_fingerprint, mode, probe, True,
                       dependencies, declared_fingerprint, description)


class ActionCatalog:
    """Strict, local-only saved-action catalog.

    ``saved_actions`` is intentionally separate from the browser settings.
    No workspace root or manifest restriction is changed by this catalog; an
    action outside a workspace is usable only when an operator explicitly
    records its absolute path and current hash here.
    """

    def __init__(self, value: Any = None):
        if value in (None, {}):
            self.actions: dict[str, SavedAction] = {}
            return
        if not isinstance(value, Mapping):
            raise ActionProtocolError("saved_actions_catalog_invalid")
        if set(value) == {"actions"}:
            entries = value["actions"]
        elif "actions" in value:
            # Reject policy-looking fields such as workspace_root.  The
            # dedicated catalog is never a workspace constraint bypass.
            raise ActionProtocolError("saved_actions_catalog_invalid")
        else:
            entries = value
        if isinstance(entries, list):
            mapped = {}
            for item in entries:
                if not isinstance(item, Mapping) or "saved_action_id" not in item:
                    raise ActionProtocolError("saved_actions_catalog_invalid")
                item = dict(item)
                key = item.pop("saved_action_id")
                if key in mapped:
                    raise ActionProtocolError("saved_actions_catalog_duplicate")
                mapped[key] = item
            entries = mapped
        if not isinstance(entries, Mapping) or len(entries) > 256:
            raise ActionProtocolError("saved_actions_catalog_invalid")
        self.actions = {str(key): validate_catalog_entry(key, entry) for key, entry in entries.items()}

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> "ActionCatalog":
        return cls((config or {}).get("saved_actions"))

    @property
    def enabled(self) -> bool:
        return bool(self.actions)

    def get(self, saved_action_id: str) -> SavedAction:
        try:
            return self.actions[saved_action_id]
        except KeyError:
            raise ActionProtocolError("saved_action_unknown") from None

    def manifest(self) -> list[dict[str, Any]]:
        return [self.actions[key].wire() for key in sorted(self.actions)]


__all__ = [
    "ACTION_CHANNEL", "ACTION_NAMES", "ACTION_MODES", "ActionCatalog", "ActionProtocolError",
    "SavedAction", "SavedActionDependency", "MAX_DEPENDENCIES", "MAX_DEPENDENCY_BYTES",
    "MAX_FILE_BYTES", "MAX_FILE_CHUNK_BYTES", "MAX_INPUT_BYTES", "MAX_LOG_LIMIT",
    "MAX_LOG_LINE_BYTES", "MAX_RESULT_BYTES", "canonical_json", "saved_action_contract_hash",
    "sha256_file", "sha256_json",
    "validate_action_request", "validate_catalog_entry", "validate_file_ref", "validate_input",
]
