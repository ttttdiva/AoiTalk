"""Local saved-action runner used by the authenticated PC Bridge client."""
from __future__ import annotations

import base64
import hashlib
import re
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from .action_identity import boot_id as current_boot_id
from .action_identity import process_identity, process_identity_matches, stable_machine_id
from .action_journal import ACTIVE_STATES, ActionJournal, TERMINAL_STATES
from .action_protocol import (ActionCatalog, ActionProtocolError, MAX_FILE_BYTES,
                              MAX_FILE_CHUNK_BYTES, canonical_json, validate_action_request,
                              validate_file_ref)
from .paths import data_dir


def _hidden_spawn_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"shell": False, "close_fds": True}
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        kwargs.update(startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW)
    return kwargs


def spawn_supervisor(journal_path: Path, run_id: str, *, python_executable: str | None = None) -> subprocess.Popen[Any]:
    """Start a detached supervisor with no visible Windows console."""
    executable = python_executable or sys.executable
    if getattr(sys, "frozen", False):
        command = [executable, "--action-supervisor", "--journal", str(journal_path), "--run-id", run_id]
    else:
        command = [executable, "-m", "pc_bridge.action_supervisor", "--journal", str(journal_path), "--run-id", run_id]
    env = {str(k): str(v) for k, v in os.environ.items() if isinstance(k, str) and isinstance(v, str)}
    source_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = source_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    kwargs = _hidden_spawn_kwargs()
    kwargs.update(cwd=source_root, env=env, stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return subprocess.Popen(command, **kwargs)


class ActionRunner:
    """Resolve only operator-approved catalog entries and supervise their run."""

    def __init__(self, config: Mapping[str, Any] | None = None, *,
                 catalog: ActionCatalog | None = None, data_root: str | os.PathLike[str] | None = None,
                 journal_path: str | os.PathLike[str] | None = None,
                 machine_id: str | None = None, boot_id_value: str | None = None,
                 supervisor_factory: Callable[[Path, str], Any] | None = None,
                 python_executable: str | None = None):
        self.config = dict(config or {})
        self.catalog = catalog or ActionCatalog.from_config(self.config)
        self.machine_id = machine_id or self.config.get("machine_id") or stable_machine_id()
        if not isinstance(self.machine_id, str) or not self.machine_id:
            raise ActionProtocolError("apps_machine_id_invalid")
        self.boot_id = boot_id_value or current_boot_id()
        root = Path(data_root) if data_root is not None else data_dir() / "actions"
        if not root.is_absolute():
            raise ActionProtocolError("apps_storage_path_invalid")
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink():
            raise ActionProtocolError("apps_storage_path_invalid")
        self.root = root
        self.journal = ActionJournal(journal_path or root / "journal.sqlite3")
        self.connection_epoch = "local"
        self.owner_user_id = "local"
        self.device_id = "local"
        self._lock = threading.RLock()
        self._supervisor_factory = supervisor_factory or (lambda path, run: spawn_supervisor(
            path, run, python_executable=python_executable))
        self.reconcile()

    @property
    def enabled(self) -> bool:
        return self.catalog.enabled

    def bind(self, *, owner_user_id: str, device_id: str, connection_epoch: str) -> None:
        for value, name in ((owner_user_id, "owner_user_id"), (device_id, "device_id"),
                            (connection_epoch, "connection_epoch")):
            if not isinstance(value, str) or not value or len(value) > 128:
                raise ActionProtocolError(f"apps_{name}_invalid")
        self.owner_user_id, self.device_id, self.connection_epoch = owner_user_id, device_id, connection_epoch

    def _assert_row_binding(self, row: Mapping[str, Any]) -> None:
        if (row.get("owner_user_id") != self.owner_user_id
                or row.get("device_id") != self.device_id
                or row.get("machine_id") != self.machine_id):
            raise ActionProtocolError("apps_identity_mismatch")

    def _run_root(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not run_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for char in run_id):
            raise ActionProtocolError("run_id_invalid")
        path = self.root / "runs" / run_id
        try:
            resolved_root = self.root.resolve()
            resolved = path.resolve()
            if resolved != resolved_root / "runs" / run_id:
                raise ActionProtocolError("apps_run_path_invalid")
        except OSError as exc:
            raise ActionProtocolError("apps_run_path_invalid") from exc
        return path

    def _staging_root(self, run_id: str) -> Path:
        """Return the pre-run upload namespace bound to this owner/device."""
        if not self.owner_user_id or not self.device_id:
            raise ActionProtocolError("apps_staging_identity_unbound")
        owner = hashlib.sha256(self.owner_user_id.encode("utf-8", "replace")).hexdigest()[:32]
        device = hashlib.sha256(self.device_id.encode("utf-8", "replace")).hexdigest()[:32]
        path = self.root / "staging" / owner / device / run_id
        try:
            expected = self.root.resolve() / "staging" / owner / device / run_id
            if path.resolve() != expected:
                raise ActionProtocolError("apps_staging_path_invalid")
        except OSError as exc:
            raise ActionProtocolError("apps_staging_path_invalid") from exc
        path.mkdir(parents=True, exist_ok=True)
        for parent in (path, *path.parents):
            if parent == parent.anchor:
                break
            if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
                raise ActionProtocolError("apps_staging_path_invalid")
        return path

    def _staging_file(self, run_id: str, ref: str) -> Path:
        ref = validate_file_ref(ref)
        root = self._staging_root(run_id)
        path = (root / ref).resolve()
        if path != root.resolve() / ref:
            raise ActionProtocolError("apps_file_path_invalid")
        if path.is_symlink():
            raise ActionProtocolError("apps_file_path_invalid")
        return path

    @staticmethod
    def _input_file_requirements(value: Any) -> list[tuple[str, int, str]]:
        """Extract the closed file-ref shape carried inside action input JSON.

        The server may send either an explicit ``ref`` or the verified
        ``file_id`` metadata shape.  Both are still run-scoped opaque refs;
        neither form permits a local path.
        """
        requirements: dict[str, tuple[int, str]] = {}
        file_id_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")

        def visit(node: Any) -> None:
            if isinstance(node, Mapping):
                lower = {str(key).casefold(): key for key in node}
                ref_value = node.get(lower["ref"]) if "ref" in lower else None
                file_id = node.get(lower["file_id"]) if "file_id" in lower else None
                has_integrity = "sha256" in lower and "size_bytes" in lower
                if has_integrity and (ref_value is not None or file_id is not None):
                    if ref_value is None:
                        if not isinstance(file_id, str) or not file_id_pattern.fullmatch(file_id):
                            raise ActionProtocolError("apps_input_file_ref_invalid")
                        ref_value = f"files/{file_id}.bin"
                    ref = validate_file_ref(ref_value)
                    size = node.get(lower["size_bytes"])
                    digest = node.get(lower["sha256"])
                    if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
                        raise ActionProtocolError("apps_input_file_size_invalid")
                    if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}\Z", digest)):
                        raise ActionProtocolError("apps_input_file_hash_invalid")
                    prior = requirements.get(ref)
                    expected = (size, digest)
                    if prior is not None and prior != expected:
                        raise ActionProtocolError("apps_input_file_conflict")
                    requirements[ref] = expected
                    return
                for child in node.values():
                    visit(child)
            elif isinstance(node, list):
                for child in node:
                    visit(child)

        visit(value)
        return [(ref, size, digest) for ref, (size, digest) in sorted(requirements.items())]

    def _materialize_staged_inputs(self, run_id: str, run_root: Path, input_json: Any) -> None:
        requirements = self._input_file_requirements(input_json)
        for ref, expected_size, expected_hash in requirements:
            source = self._staging_file(run_id, ref)
            try:
                info = source.lstat()
            except OSError as exc:
                raise ActionProtocolError("apps_input_file_missing") from exc
            if source.is_symlink() or not source.is_file() or info.st_size != expected_size:
                raise ActionProtocolError("apps_input_file_integrity_mismatch")
            destination = (run_root / ref).resolve()
            if destination != run_root.resolve() / ref or destination.is_symlink():
                raise ActionProtocolError("apps_file_path_invalid")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if any(parent.is_symlink() for parent in (destination.parent, *destination.parent.parents)
                   if parent != parent.anchor):
                raise ActionProtocolError("apps_file_path_invalid")
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                          getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
            digest = hashlib.sha256()
            try:
                with source.open("rb") as source_stream:
                    while block := source_stream.read(MAX_FILE_CHUNK_BYTES):
                        digest.update(block)
                        os.write(fd, block)
                os.fsync(fd)
            except OSError as exc:
                raise ActionProtocolError("apps_input_file_unavailable") from exc
            finally:
                os.close(fd)
            if digest.hexdigest() != expected_hash:
                try:
                    destination.unlink()
                except OSError:
                    pass
                raise ActionProtocolError("apps_input_file_integrity_mismatch")
            try:
                source.unlink()
            except OSError:
                raise ActionProtocolError("apps_input_file_unavailable") from None

    @staticmethod
    def _write_exclusive(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ActionProtocolError("apps_run_file_invalid")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            raise ActionProtocolError("apps_run_file_exists") from None
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("write failed")
                view = view[written:]
            os.fsync(fd)
        except OSError as exc:
            raise ActionProtocolError("apps_run_file_unavailable") from exc
        finally:
            os.close(fd)

    def _record(self, params: dict[str, Any], spec: Any, run_root: Path, input_path: Path, output_path: Path) -> dict[str, Any]:
        return {
            "run_id": params["run_id"], "saved_action_id": spec.saved_action_id,
            "action_ref": params["action_ref"], "action_sha256": spec.sha256,
            "contract_hash": spec.contract_hash, "input_sha256": hashlib.sha256(
                canonical_json(params["input"]).encode("utf-8")).hexdigest(),
            "input_json": canonical_json(params["input"]), "owner_user_id": self.owner_user_id,
            "device_id": self.device_id, "machine_id": self.machine_id,
            "connection_epoch": self.connection_epoch, "mode": spec.mode,
            "action_path": str(spec.path),
            "dependencies_json": canonical_json([
                {"path": str(item.path), "sha256": item.sha256}
                for item in spec.dependencies
            ]),
            "probe_json": canonical_json(spec.probe),
            "input_path": str(input_path), "output_path": str(output_path),
            "run_root": str(run_root),
        }

    def start(self, params: Mapping[str, Any]) -> dict[str, Any]:
        params = validate_action_request("start", dict(params))
        with self._lock:
            existing = self.journal.get(params["run_id"])
            if existing is not None:
                self._assert_row_binding(existing)
                input_hash = hashlib.sha256(canonical_json(params["input"]).encode("utf-8")).hexdigest()
                if (existing.get("saved_action_id") != params["saved_action_id"]
                        or existing.get("action_ref") != params["action_ref"]
                        or existing.get("action_sha256") != params["sha256"]
                        or existing.get("contract_hash") != params["contract_hash"]
                        or existing.get("input_sha256") != input_hash):
                    raise ActionProtocolError("apps_run_conflict")
                # A committed row is authoritative for duplicate starts.  Do
                # not rehash, reopen, or respawn the catalog path here: a
                # changed local file cannot turn a replay into a new spawn.
                return {**self.journal.receipt(params["run_id"]), "accepted": True, "duplicate": True}
        spec = self.catalog.get(params["saved_action_id"])
        if params["sha256"] != spec.sha256:
            raise ActionProtocolError("saved_action_hash_mismatch")
        if params["contract_hash"] != spec.contract_hash:
            raise ActionProtocolError("saved_action_contract_mismatch")
        # Re-read the catalog path at every start.  A BAT/EXE replacement
        # between catalog creation and spawn is rejected before journal commit.
        from .action_protocol import sha256_file
        if sha256_file(spec.path) != spec.sha256:
            raise ActionProtocolError("saved_action_hash_mismatch")
        with self._lock:
            # A second check closes the gap between the initial read and the
            # durable input/journal fence for concurrent duplicate callers.
            existing = self.journal.get(params["run_id"])
            if existing is not None:
                self._assert_row_binding(existing)
                input_hash = hashlib.sha256(canonical_json(params["input"]).encode("utf-8")).hexdigest()
                if (existing.get("saved_action_id") != params["saved_action_id"]
                        or existing.get("action_ref") != params["action_ref"]
                        or existing.get("action_sha256") != params["sha256"]
                        or existing.get("contract_hash") != params["contract_hash"]
                        or existing.get("input_sha256") != input_hash):
                    raise ActionProtocolError("apps_run_conflict")
                return {**self.journal.receipt(params["run_id"]), "accepted": True, "duplicate": True}
            run_root = self._run_root(params["run_id"])
            input_path, output_path = run_root / "input.json", run_root / "result.json"
            encoded = canonical_json(params["input"]).encode("utf-8")
            if len(encoded) > 4 * 1024 * 1024:
                raise ActionProtocolError("apps_input_too_large")
            # File bytes must already be staged and verified before the
            # journal fence is committed; the supervisor never starts a
            # process that can race an input upload.
            self._materialize_staged_inputs(params["run_id"], run_root, params["input"])
            self._write_exclusive(input_path, encoded)
            record = self._record(params, spec, run_root, input_path, output_path)
            row, duplicate = self.journal.prepare(record)
            if duplicate:
                return {**self.journal.receipt(params["run_id"]), "accepted": True, "duplicate": True}
            try:
                self._supervisor_factory(self.journal.path, params["run_id"])
            except Exception as exc:
                # There was no supervisor process; this is known failure.  A
                # later client must not retry the same run_id automatically.
                self.journal.update(params["run_id"], status="failed", execution_status="failed",
                                    error="supervisor_spawn_failed:" + type(exc).__name__)
                raise ActionProtocolError("apps_supervisor_unavailable") from None
            return {**self.journal.receipt(params["run_id"]), "accepted": True, "duplicate": False}

    def _reconcile_row(self, row: dict[str, Any]) -> dict[str, Any]:
        if row["status"] in TERMINAL_STATES:
            return row
        supervisor_pid = row.get("supervisor_pid")
        supervisor_valid = supervisor_pid is not None and process_identity_matches(
            {"pid": supervisor_pid, "created": row.get("supervisor_started")},
            boot=row.get("supervisor_boot_id"), current_boot=current_boot_id())
        if supervisor_pid is not None and not supervisor_valid:
            return self.journal.mark_unknown_if_unobserved(row["run_id"], "supervisor_identity_unknown")
        # The child may exit a few milliseconds before the supervisor commits
        # its terminal row.  While a supervisor identity is still valid, that
        # gap is observable progress rather than proof of PID reuse.
        if supervisor_valid:
            return row
        pid = row.get("pid")
        if pid is not None and not process_identity_matches(
                {"pid": pid, "created": row.get("process_started")},
                boot=row.get("boot_id"), current_boot=current_boot_id()):
            return self.journal.mark_unknown_if_unobserved(row["run_id"], "process_identity_unknown")
        return row

    def reconcile(self) -> None:
        # Journal rows are status evidence only.  No run is ever respawned.
        with self.journal._lock:
            rows = self.journal._connection.execute(
                "SELECT run_id FROM action_runs WHERE status IN ('accepted','starting','running','ready','stop_requested')"
            ).fetchall()
        for row in rows:
            current = self.journal.get(row[0])
            if current:
                if current["status"] == "accepted" and current.get("supervisor_pid") is None:
                    self.journal.mark_unknown_if_unobserved(current["run_id"], "supervisor_spawn_boundary_unknown")
                else:
                    self._reconcile_row(current)

    def status(self, params: Mapping[str, Any]) -> dict[str, Any]:
        params = validate_action_request("status", dict(params))
        row = self.journal.get(params["run_id"])
        if row is None:
            raise ActionProtocolError("apps_run_unknown")
        self._assert_row_binding(row)
        row = self._reconcile_row(row)
        return self.journal.receipt(row["run_id"])

    def logs(self, params: Mapping[str, Any]) -> dict[str, Any]:
        params = validate_action_request("logs", dict(params))
        row = self.journal.get(params["run_id"])
        if row is None:
            raise ActionProtocolError("apps_run_unknown")
        self._assert_row_binding(row)
        result = self.journal.logs(params["run_id"], params["cursor"], params["limit"])
        result["run"] = self.status({"run_id": params["run_id"]})
        return result

    def stop(self, params: Mapping[str, Any]) -> dict[str, Any]:
        params = validate_action_request("stop", dict(params))
        current = self.journal.get(params["run_id"])
        if current is None:
            raise ActionProtocolError("apps_run_unknown")
        self._assert_row_binding(current)
        row = self.journal.request_stop(params["run_id"])
        if row["status"] in TERMINAL_STATES:
            return self.journal.receipt(row["run_id"])
        return self.status({"run_id": params["run_id"]})

    def _file_path(self, row: dict[str, Any], ref: str) -> Path:
        ref = validate_file_ref(ref)
        root = Path(row["run_root"]).resolve()
        path = (root / ref).resolve()
        if path != root / ref or not str(path).startswith(str(root) + os.sep):
            raise ActionProtocolError("apps_file_path_invalid")
        if path.is_symlink():
            raise ActionProtocolError("apps_file_path_invalid")
        return path

    def upload(self, params: Mapping[str, Any]) -> dict[str, Any]:
        params = validate_action_request("upload", dict(params))
        row = self.journal.get(params["run_id"])
        if row is not None:
            self._assert_row_binding(row)
            # The upload channel is input staging.  Once a journal row exists,
            # start has crossed its commit fence and bytes cannot be added or
            # changed underneath the child.
            raise ActionProtocolError("apps_upload_after_start")
        path = self._staging_file(params["run_id"], params["ref"])
        path.parent.mkdir(parents=True, exist_ok=True)
        offset, raw = params["offset"], base64.b64decode(params["data"], validate=True)
        if path.exists() and path.stat().st_size != offset:
            if offset < path.stat().st_size:
                with path.open("rb") as stream:
                    stream.seek(offset)
                    if stream.read(len(raw)) == raw:
                        return {"run_id": params["run_id"], "ref": params["ref"],
                                "offset": offset + len(raw), "size_bytes": path.stat().st_size,
                                "staged": True}
            raise ActionProtocolError("apps_upload_offset_conflict")
        if offset + len(raw) > MAX_FILE_BYTES:
            raise ActionProtocolError("apps_file_too_large")
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags, 0o600)
        try:
            os.lseek(fd, offset, os.SEEK_SET)
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        return {"run_id": params["run_id"], "ref": params["ref"],
                "offset": offset + len(raw), "size_bytes": path.stat().st_size,
                "staged": True}

    def download(self, params: Mapping[str, Any]) -> dict[str, Any]:
        params = validate_action_request("download", dict(params))
        row = self.journal.get(params["run_id"])
        if row is None:
            raise ActionProtocolError("apps_run_unknown")
        self._assert_row_binding(row)
        path = self._file_path(row, params["ref"])
        if not path.is_file() or path.is_symlink():
            raise ActionProtocolError("apps_file_unknown")
        info = path.stat()
        if info.st_size > MAX_FILE_BYTES:
            raise ActionProtocolError("apps_file_too_large")
        with path.open("rb") as stream:
            stream.seek(params["offset"])
            data = stream.read(min(params["limit"], MAX_FILE_CHUNK_BYTES))
        next_offset = params["offset"] + len(data)
        return {"run_id": params["run_id"], "ref": params["ref"],
                "offset": params["offset"], "next_offset": next_offset,
                "eof": next_offset >= info.st_size,
                "size_bytes": info.st_size, "data": base64.b64encode(data).decode("ascii"),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def dispatch(self, action: str, params: Mapping[str, Any]) -> dict[str, Any]:
        params = validate_action_request(action, dict(params))
        if action == "start":
            return self.start(params)
        if action == "status":
            return self.status(params)
        if action == "logs":
            return self.logs(params)
        if action == "stop":
            return self.stop(params)
        if action == "upload":
            return self.upload(params)
        if action == "download":
            return self.download(params)
        raise ActionProtocolError("apps_action_invalid")

    def close(self) -> None:
        # Supervisors intentionally outlive the bridge/client process.
        self.journal.close()


__all__ = ["ActionRunner", "spawn_supervisor"]
