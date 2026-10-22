"""Durable local journal for server-side App Action supervisors.

The API database records the delivery fence, while this small SQLite journal
records the independent server supervisor and its process evidence.  The
journal is deliberately local to the configured workspaces root and accepts
only server-owned, already materialized runtime metadata.  It never stores a
caller supplied command path as an executable request.
"""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID


MAX_RESULT_BYTES = 64 * 1024
MAX_LOG_BYTES = 64 * 1024
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "unknown"})
ACTIVE_STATES = frozenset({"accepted", "starting", "running", "stopping"})


class ServerActionJournalError(RuntimeError):
    """The durable server-action journal rejected an operation."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


def _json(value: Any, *, limit: int = MAX_RESULT_BYTES) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ServerActionJournalError("server_action_json_invalid") from exc
    if len(encoded) > limit:
        raise ServerActionJournalError("server_action_json_too_large")
    return encoded.decode("utf-8")


def _parse_json(value: Any, *, fallback: Any) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return fallback


def _uuid_text(value: Any, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ServerActionJournalError(f"server_action_{label}_invalid") from exc


def runtime_tree_sha256(root: str | os.PathLike[str]) -> str:
    """Hash every regular runtime file in deterministic relative-path order."""

    base = Path(root).resolve()
    if not base.is_dir() or base.is_symlink():
        raise ServerActionJournalError("server_action_runtime_unavailable")
    digest = hashlib.sha256()
    files: list[Path] = []
    for current, directories, names in os.walk(base, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            name for name in directories if not (current_path / name).is_symlink()
        )
        for name in sorted(names):
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise ServerActionJournalError("server_action_runtime_link_invalid")
            files.append(path)
    for path in sorted(files, key=lambda item: item.relative_to(base).as_posix().casefold()):
        relative = path.relative_to(base).as_posix().encode("utf-8")
        data_hash = hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        digest.update(data_hash)
    return digest.hexdigest()


class ServerActionJournal:
    """Thread-safe SQLite state for one server Action run namespace."""

    def __init__(self, path: str | os.PathLike[str], *, journal_epoch: str | None = None):
        raw_path = Path(path)
        if raw_path.exists() and raw_path.is_symlink():
            raise ServerActionJournalError("server_action_journal_link_invalid")
        self.path = raw_path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path), timeout=30, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS server_action_runs (
                run_id TEXT PRIMARY KEY,
                fingerprint_json TEXT NOT NULL,
                app_id TEXT NOT NULL,
                project_id TEXT,
                release_id TEXT NOT NULL,
                action_key TEXT NOT NULL,
                action_ref TEXT NOT NULL,
                action_spec_hash TEXT NOT NULL,
                target_key TEXT NOT NULL,
                target_contract_hash TEXT NOT NULL,
                owner_user_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                machine_id TEXT NOT NULL,
                connection_epoch TEXT NOT NULL,
                mode TEXT NOT NULL,
                runtime_root TEXT NOT NULL,
                runtime_tree_sha256 TEXT NOT NULL,
                command_json TEXT NOT NULL,
                command_sha256 TEXT NOT NULL,
                input_json TEXT NOT NULL,
                input_sha256 TEXT NOT NULL,
                input_files_json TEXT NOT NULL DEFAULT '[]',
                input_path TEXT NOT NULL,
                output_path TEXT NOT NULL,
                output_manifest_path TEXT NOT NULL DEFAULT '',
                log_path TEXT NOT NULL,
                isolation_json TEXT NOT NULL,
                status TEXT NOT NULL,
                execution_status TEXT NOT NULL,
                spawn_claimed INTEGER NOT NULL DEFAULT 0,
                stop_requested INTEGER NOT NULL DEFAULT 0,
                supervisor_pid INTEGER,
                supervisor_started REAL,
                supervisor_boot_id TEXT,
                pid INTEGER,
                process_started REAL,
                process_boot_id TEXT,
                exit_code INTEGER,
                ready INTEGER NOT NULL DEFAULT 0,
                result_json TEXT,
                output_files_json TEXT NOT NULL DEFAULT '[]',
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS server_action_runs_status_created
                ON server_action_runs(status, created_at);
            CREATE TABLE IF NOT EXISTS server_action_reservations (
                run_id TEXT PRIMARY KEY,
                fingerprint_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'preparing',
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS server_action_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._connection.commit()
        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(server_action_runs)")}
        for name, definition in (
            ("input_files_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("input_path", "TEXT NOT NULL DEFAULT ''"),
            ("project_id", "TEXT"),
            ("output_manifest_path", "TEXT NOT NULL DEFAULT ''"),
            ("output_files_json", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            if name not in columns:
                self._connection.execute(
                    f"ALTER TABLE server_action_runs ADD COLUMN {name} {definition}"
                )
            self._connection.commit()
        selected_epoch = str(journal_epoch or f"server-{os.getpid()}-{time.time_ns()}")[:128]
        with self._lock:
            existing_epoch = self._connection.execute(
                "SELECT value FROM server_action_meta WHERE key = 'journal_epoch'"
            ).fetchone()
            if existing_epoch is None:
                self._connection.execute(
                    "INSERT INTO server_action_meta(key,value) VALUES('journal_epoch',?)",
                    (selected_epoch,),
                )
                self._connection.commit()
                self.journal_epoch = selected_epoch
            else:
                self.journal_epoch = str(existing_epoch[0])

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["fingerprint"] = _parse_json(result.pop("fingerprint_json", None), fallback={})
        result["command"] = _parse_json(result.pop("command_json", None), fallback=[])
        result["input"] = _parse_json(result.pop("input_json", None), fallback={})
        result["input_files"] = _parse_json(result.pop("input_files_json", None), fallback=[])
        result["isolation"] = _parse_json(result.pop("isolation_json", None), fallback={})
        raw_result_json = result.pop("result_json", None)
        result["result_present"] = raw_result_json is not None
        result["result"] = _parse_json(raw_result_json, fallback={})
        result["output_files"] = _parse_json(result.pop("output_files_json", None), fallback=[])
        result["spawn_claimed"] = bool(result.get("spawn_claimed"))
        result["stop_requested"] = bool(result.get("stop_requested"))
        result["ready"] = bool(result.get("ready"))
        return result

    @staticmethod
    def _fingerprint(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: record.get(key)
            for key in (
                "app_id",
                "project_id",
                "release_id",
                "action_key",
                "action_ref",
                "action_spec_hash",
                "target_key",
                "target_contract_hash",
                "owner_user_id",
                "device_id",
                "machine_id",
                "connection_epoch",
                "mode",
                "runtime_tree_sha256",
                "command_sha256",
                "input_sha256",
            )
        }

    def prepare(self, record: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        """Insert an accepted row, or return an identical duplicate row."""

        required = {
            "run_id",
            "app_id",
            "project_id",
            "release_id",
            "action_key",
            "action_ref",
            "action_spec_hash",
            "target_key",
            "target_contract_hash",
            "owner_user_id",
            "device_id",
            "machine_id",
            "connection_epoch",
            "mode",
            "runtime_root",
            "runtime_tree_sha256",
            "command",
            "command_sha256",
            "input",
            "input_sha256",
            "input_path",
            "output_path",
            "output_manifest_path",
            "log_path",
            "isolation",
        }
        if not required <= set(record) or set(record) - required - {"input_files"}:
            raise ServerActionJournalError("server_action_record_invalid")
        run_id = _uuid_text(record["run_id"], "run_id")
        values = dict(record)
        for key in ("app_id", "release_id", "owner_user_id"):
            values[key] = _uuid_text(values[key], key)
        if values.get("project_id"):
            values["project_id"] = _uuid_text(values["project_id"], "project_id")
        else:
            values["project_id"] = None
        for key in ("action_key", "action_ref", "target_key", "mode", "runtime_root", "input_path", "output_path", "output_manifest_path", "log_path"):
            if not isinstance(values[key], str) or not values[key].strip() or "\x00" in values[key]:
                raise ServerActionJournalError("server_action_record_invalid")
        now = time.time()
        values.update(
            run_id=run_id,
            fingerprint_json=_json(self._fingerprint(values)),
            command_json=_json(values["command"]),
            input_json=_json(values["input"]),
            input_files_json=_json(values.get("input_files") or [], limit=64 * 1024),
            output_files_json="[]",
            isolation_json=_json(values["isolation"]),
            status="accepted",
            execution_status="accepted",
            spawn_claimed=0,
            stop_requested=0,
            supervisor_pid=None,
            supervisor_started=None,
            supervisor_boot_id=None,
            pid=None,
            process_started=None,
            process_boot_id=None,
            exit_code=None,
            ready=0,
            result_json=None,
            error=None,
            created_at=now,
            updated_at=now,
        )
        values.pop("command", None)
        values.pop("input", None)
        values.pop("input_files", None)
        values.pop("isolation", None)
        with self._lock:
            connection = self._connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM server_action_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if existing is not None:
                    current = self._row(existing) or {}
                    if current.get("fingerprint") != self._fingerprint(record):
                        connection.rollback()
                        raise ServerActionJournalError("server_action_run_conflict")
                    connection.commit()
                    return current, True
                columns = tuple(values)
                connection.execute(
                    f"INSERT INTO server_action_runs ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    tuple(values[column] for column in columns),
                )
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
        return self.get(run_id) or {}, False

    def get(self, run_id: str | UUID) -> dict[str, Any] | None:
        run_text = str(run_id)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM server_action_runs WHERE run_id = ?", (run_text,)
            ).fetchone()
        return self._row(row)

    def reserve_start(self, run_id: str | UUID, fingerprint: Mapping[str, Any]) -> bool:
        """Reserve materialization for one run before touching its runtime tree.

        ``True`` means another caller already owns the reservation.  The
        reservation survives a process crash so a later duplicate cannot
        replace a runtime directory while an earlier supervisor may still be
        using it.
        """

        run_text = _uuid_text(run_id, "run_id")
        encoded = _json(dict(fingerprint), limit=16 * 1024)
        now = time.time()
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT fingerprint_json FROM server_action_reservations WHERE run_id = ?",
                    (run_text,),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != encoded:
                        raise ServerActionJournalError("server_action_run_conflict")
                    connection.commit()
                    return True
                connection.execute(
                    "INSERT INTO server_action_reservations(run_id,fingerprint_json,status,error,created_at,updated_at) "
                    "VALUES(?,?, 'preparing', NULL, ?, ?)",
                    (run_text, encoded, now, now),
                )
                connection.commit()
                return False
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def finish_reservation(self, run_id: str | UUID) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE server_action_reservations SET status = 'ready', updated_at = ? WHERE run_id = ?",
                (time.time(), str(run_id)),
            )
            self._connection.commit()

    def abort_reservation(self, run_id: str | UUID, error: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE server_action_reservations SET status = 'failed', error = ?, updated_at = ? WHERE run_id = ?",
                (str(error)[:128], time.time(), str(run_id)),
            )
            self._connection.commit()

    def pending_snapshot(self, run_id: str | UUID) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT run_id,fingerprint_json,status,error FROM server_action_reservations WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
        if row is None:
            return None
        fingerprint = _parse_json(row[1], fallback={})
        status = "failed" if row[2] == "failed" else "accepted"
        return {
            "run_id": str(row[0]),
            "app_id": fingerprint.get("app_id"),
            "project_id": fingerprint.get("project_id"),
            "release_id": fingerprint.get("release_id"),
            "action_ref": fingerprint.get("action_ref") or "",
            "machine_id": fingerprint.get("machine_id") or "",
            "device_id": fingerprint.get("device_id"),
            "contract_hash": fingerprint.get("target_contract_hash"),
            "input_sha256": fingerprint.get("input_sha256"),
            "phase": status,
            "status": status,
            "execution_status": status,
            "readiness_status": "not_required",
            "process_status": "not_started",
            "exit_code": None,
            "ready": False,
            "result_complete": status == "failed",
            "result": {},
            "error_code": row[3],
            "journal_epoch": self.journal_epoch,
            "remote_journal_epoch": self.journal_epoch,
        }

    def claim_spawn(self, run_id: str | UUID) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE server_action_runs SET spawn_claimed = 1, updated_at = ? "
                "WHERE run_id = ? AND spawn_claimed = 0 AND status = 'accepted'",
                (time.time(), str(run_id)),
            )
            self._connection.commit()
            return cursor.rowcount == 1

    def update(self, run_id: str | UUID, **fields: Any) -> dict[str, Any]:
        allowed = {
            "status",
            "execution_status",
            "stop_requested",
            "supervisor_pid",
            "supervisor_started",
            "supervisor_boot_id",
            "pid",
            "process_started",
            "process_boot_id",
            "exit_code",
            "ready",
            "result_json",
            "output_files_json",
            "error",
        }
        if set(fields) - allowed:
            raise ServerActionJournalError("server_action_update_invalid")
        if "status" in fields and fields["status"] not in ACTIVE_STATES | TERMINAL_STATES:
            raise ServerActionJournalError("server_action_status_invalid")
        if "result_json" in fields:
            fields["result_json"] = _json(fields["result_json"])
        if "output_files_json" in fields and fields["output_files_json"] is not None:
            fields["output_files_json"] = _json(fields["output_files_json"], limit=64 * 1024)
        if "error" in fields and fields["error"] is not None:
            if not isinstance(fields["error"], str) or len(fields["error"].encode("utf-8")) > 1024:
                raise ServerActionJournalError("server_action_error_invalid")
        fields["updated_at"] = time.time()
        with self._lock:
            assignments = ",".join(f"{key} = ?" for key in fields)
            values = [fields[key] for key in fields] + [str(run_id)]
            self._connection.execute(
                f"UPDATE server_action_runs SET {assignments} WHERE run_id = ?", values
            )
            self._connection.commit()
        result = self.get(run_id)
        if result is None:
            raise ServerActionJournalError("server_action_run_unknown")
        return result

    def request_stop(self, run_id: str | UUID) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM server_action_runs WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            if row is None:
                raise ServerActionJournalError("server_action_run_unknown")
            if row[0] in TERMINAL_STATES:
                return self.get(run_id) or {}
            self._connection.execute(
                "UPDATE server_action_runs SET stop_requested = 1, status = 'stopping', "
                "execution_status = 'stopping', updated_at = ? WHERE run_id = ?",
                (time.time(), str(run_id)),
            )
            self._connection.commit()
        return self.get(run_id) or {}

    def mark_unknown_if_active(self, run_id: str | UUID, error: str) -> dict[str, Any] | None:
        with self._lock:
            self._connection.execute(
                "UPDATE server_action_runs SET status = 'unknown', execution_status = 'unknown', "
                "error = ?, updated_at = ? WHERE run_id = ? AND status NOT IN ('succeeded','failed','cancelled','unknown')",
                (str(error)[:1024], time.time(), str(run_id)),
            )
            self._connection.commit()
        return self.get(run_id)

    def logs(self, run_id: str | UUID, *, cursor: int = 0, limit_bytes: int = MAX_LOG_BYTES) -> dict[str, Any]:
        row = self.get(run_id)
        if row is None:
            raise ServerActionJournalError("server_action_run_unknown")
        if type(cursor) is not int or cursor < 0 or type(limit_bytes) is not int or not 1 <= limit_bytes <= MAX_LOG_BYTES:
            raise ServerActionJournalError("server_action_logs_params_invalid")
        path = Path(str(row["log_path"])).resolve()
        try:
            with path.open("rb") as handle:
                handle.seek(cursor)
                data = handle.read(limit_bytes)
                next_cursor = handle.tell()
        except FileNotFoundError:
            data = b""
            next_cursor = cursor
        except OSError as exc:
            raise ServerActionJournalError("server_action_logs_unavailable") from exc
        text = data.decode("utf-8", "replace")
        return {
            "run_id": str(row["run_id"]),
            "action_ref": row["action_ref"],
            "machine_id": row["machine_id"],
            "cursor": next_cursor,
            "data": text,
            "logs": text,
            "status": row["status"],
        }

    def remote_snapshot(self, run_id: str | UUID) -> dict[str, Any]:
        row = self.get(run_id)
        if row is None:
            raise ServerActionJournalError("server_action_run_unknown")
        status = str(row.get("status") or "unknown")
        process_status = "running" if status in {"starting", "running", "stopping"} else (
            "not_started" if status == "accepted" else "exited"
        )
        return {
            "run_id": row["run_id"],
            "app_id": row["app_id"],
            "project_id": row.get("project_id"),
            "release_id": row["release_id"],
            "action_key": row["action_key"],
            "action_ref": row["action_ref"],
            "machine_id": row["machine_id"],
            "device_id": row["device_id"],
            "contract_hash": row["target_contract_hash"],
            "input_sha256": row["input_sha256"],
            "phase": "cancelled" if status == "cancelled" else status,
            "status": status,
            "execution_status": row.get("execution_status") or status,
            "readiness_status": "not_required",
            "process_status": process_status,
            "exit_code": row.get("exit_code"),
            "ready": status == "succeeded",
            "result_complete": status in TERMINAL_STATES,
            "result": row["result"] if row.get("result_present") else {},
            "output_files": row["output_files"] if row.get("output_files") is not None else [],
            "error_code": row.get("error"),
            "journal_epoch": self.journal_epoch,
            "remote_journal_epoch": self.journal_epoch,
            "connection_epoch": row.get("connection_epoch"),
        }


__all__ = [
    "ACTIVE_STATES",
    "MAX_LOG_BYTES",
    "MAX_RESULT_BYTES",
    "ServerActionJournal",
    "ServerActionJournalError",
    "TERMINAL_STATES",
    "runtime_tree_sha256",
]
