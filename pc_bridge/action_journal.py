"""Durable SQLite journal for saved PC action runs.

The journal is the commit boundary for a run.  ``start`` inserts an accepted
row and fsyncs the input file before the supervisor is launched.  A repeated
start with the same run fingerprint returns the original receipt; a changed
fingerprint is rejected.  The journal intentionally records an ``unknown``
state when the spawn boundary cannot be proven, so callers never retry an
action merely because the bridge or API disconnected.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .action_protocol import ActionProtocolError, MAX_LOG_LINE_BYTES, MAX_LOG_LIMIT, MAX_RESULT_BYTES


TERMINAL_STATES = frozenset({"succeeded", "failed", "stopped", "unknown"})
ACTIVE_STATES = frozenset({"accepted", "starting", "running", "ready", "stop_requested"})


def _json(value: Any, *, max_bytes: int = MAX_RESULT_BYTES) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ActionProtocolError("apps_journal_json_invalid") from exc
    if len(text.encode("utf-8")) > max_bytes:
        raise ActionProtocolError("apps_journal_json_too_large")
    return text


class ActionJournal:
    """Thread-safe local SQLite journal.

    The class uses only a path supplied by the local bridge.  It never accepts
    a path from the apps-channel request; callers pass the run-scoped path
    created by :class:`pc_bridge.action_runner.ActionRunner`.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS action_runs (
                run_id TEXT PRIMARY KEY,
                saved_action_id TEXT NOT NULL,
                action_ref TEXT NOT NULL,
                action_sha256 TEXT NOT NULL,
                contract_hash TEXT NOT NULL,
                input_sha256 TEXT NOT NULL,
                input_json TEXT NOT NULL,
                owner_user_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                machine_id TEXT NOT NULL,
                connection_epoch TEXT NOT NULL,
                mode TEXT NOT NULL,
                action_path TEXT NOT NULL,
                dependencies_json TEXT NOT NULL,
                probe_json TEXT NOT NULL,
                input_path TEXT NOT NULL,
                output_path TEXT NOT NULL,
                run_root TEXT NOT NULL,
                status TEXT NOT NULL,
                execution_status TEXT NOT NULL,
                stop_requested INTEGER NOT NULL DEFAULT 0,
                supervisor_pid INTEGER,
                supervisor_started REAL,
                supervisor_boot_id TEXT,
                pid INTEGER,
                process_started REAL,
                boot_id TEXT,
                probe_pid INTEGER,
                probe_process_started REAL,
                probe_boot_id TEXT,
                owned_processes_json TEXT,
                exit_code INTEGER,
                launcher_exit_code INTEGER,
                ready INTEGER NOT NULL DEFAULT 0,
                result_json TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS action_logs (
                run_id TEXT NOT NULL REFERENCES action_runs(run_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                stream TEXT NOT NULL,
                line TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (run_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS action_logs_run_cursor ON action_logs(run_id, sequence);
            """
        )
        # Existing portable data directories may contain a journal created by
        # an earlier bridge build.  Add only these additive evidence columns;
        # never rewrite or discard prior run rows.
        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(action_runs)")}
        for name, definition in (
            ("probe_pid", "INTEGER"), ("probe_process_started", "REAL"),
            ("probe_boot_id", "TEXT"), ("owned_processes_json", "TEXT"),
            ("dependencies_json", "TEXT"),
            ("launcher_exit_code", "INTEGER"),
        ):
            if name not in columns:
                self._connection.execute(f"ALTER TABLE action_runs ADD COLUMN {name} {definition}")
        self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result_present = result.get("result_json") is not None
        for key in ("probe_json", "result_json", "owned_processes_json", "dependencies_json"):
            if result.get(key) is not None:
                try:
                    result[key[:-5] if key.endswith("_json") else key] = json.loads(result[key])
                except (TypeError, ValueError):
                    result[key[:-5] if key.endswith("_json") else key] = None
                result.pop(key, None)
        result["result_present"] = result_present
        for key in ("stop_requested", "ready"):
            result[key] = bool(result[key])
        return result

    @staticmethod
    def _fingerprint(row: Mapping[str, Any]) -> tuple[Any, ...]:
        values = []
        for key in (
            "saved_action_id", "action_ref", "action_sha256", "contract_hash", "input_sha256",
            "owner_user_id", "device_id", "machine_id", "mode", "action_path", "dependencies_json", "probe_json",
        ):
            value = row.get(key)
            if key == "dependencies_json" and value in (None, ""):
                value = "[]"
            values.append(value)
        return tuple(values)

    def prepare(self, record: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        """Insert an accepted row or return an identical duplicate receipt."""
        required = {
            "run_id", "saved_action_id", "action_ref", "action_sha256", "contract_hash", "input_sha256",
            "input_json", "owner_user_id", "device_id", "machine_id", "connection_epoch", "mode",
            "action_path", "dependencies_json", "probe_json", "input_path", "output_path", "run_root",
        }
        if set(record) != required:
            raise ActionProtocolError("apps_journal_record_invalid")
        now = time.time()
        values = dict(record)
        values.update(status="accepted", execution_status="accepted", stop_requested=0,
                      supervisor_pid=None, supervisor_started=None, supervisor_boot_id=None,
                      pid=None, process_started=None, boot_id=None, exit_code=None, ready=0,
                       probe_pid=None, probe_process_started=None, probe_boot_id=None,
                       owned_processes_json=None, launcher_exit_code=None,
                      result_json=None, error=None, created_at=now, updated_at=now)
        values["input_json"] = _json(json.loads(values["input_json"]) if isinstance(values["input_json"], str) else values["input_json"])
        values["probe_json"] = _json(json.loads(values["probe_json"]) if isinstance(values["probe_json"], str) else values["probe_json"])
        with self._lock:
            connection = self._connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute("SELECT * FROM action_runs WHERE run_id = ?",
                                              (values["run_id"],)).fetchone()
                if existing is not None:
                    existing_dict = dict(existing)
                    expected_fp = self._fingerprint(values)
                    actual_fp = self._fingerprint(existing_dict)
                    if expected_fp != actual_fp:
                        connection.rollback()
                        raise ActionProtocolError("apps_run_conflict")
                    connection.commit()
                    return self._row(existing), True  # type: ignore[arg-type]
                columns = tuple(values)
                connection.execute(
                    f"INSERT INTO action_runs ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    tuple(values[column] for column in columns),
                )
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
        return self.get(values["run_id"]) or {}, False

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM action_runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._row(row)

    def update(self, run_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            "status", "execution_status", "stop_requested", "supervisor_pid", "supervisor_started",
            "supervisor_boot_id", "pid", "process_started", "boot_id", "probe_pid", "probe_process_started",
            "probe_boot_id", "owned_processes_json", "dependencies_json", "exit_code", "launcher_exit_code", "ready",
            "result_json", "error",
        }
        if set(fields) - allowed:
            raise ActionProtocolError("apps_journal_update_invalid")
        if "result_json" in fields:
            result = fields["result_json"]
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except ValueError as exc:
                    raise ActionProtocolError("apps_result_invalid") from exc
            fields["result_json"] = _json(result)
        if "owned_processes_json" in fields and fields["owned_processes_json"] is not None:
            owned = fields["owned_processes_json"]
            if isinstance(owned, str):
                try:
                    owned = json.loads(owned)
                except ValueError as exc:
                    raise ActionProtocolError("apps_owned_processes_invalid") from exc
            fields["owned_processes_json"] = _json(owned, max_bytes=64 * 1024)
        if "dependencies_json" in fields and fields["dependencies_json"] is not None:
            dependencies = fields["dependencies_json"]
            if isinstance(dependencies, str):
                try:
                    dependencies = json.loads(dependencies)
                except ValueError as exc:
                    raise ActionProtocolError("apps_dependencies_invalid") from exc
            fields["dependencies_json"] = _json(dependencies, max_bytes=64 * 1024)
        if "error" in fields and fields["error"] is not None:
            if not isinstance(fields["error"], str) or len(fields["error"].encode("utf-8")) > 1024:
                raise ActionProtocolError("apps_error_invalid")
        if "status" in fields and fields["status"] not in ACTIVE_STATES | TERMINAL_STATES:
            raise ActionProtocolError("apps_status_invalid")
        fields["updated_at"] = time.time()
        with self._lock:
            assignments = ",".join(f"{key} = ?" for key in fields)
            values = [fields[key] for key in fields] + [run_id]
            self._connection.execute(f"UPDATE action_runs SET {assignments} WHERE run_id = ?", values)
            self._connection.commit()
        value = self.get(run_id)
        if value is None:
            raise ActionProtocolError("apps_run_unknown")
        return value

    def request_stop(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute("SELECT status FROM action_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise ActionProtocolError("apps_run_unknown")
            status = row[0]
            if status in TERMINAL_STATES:
                return self.get(run_id) or {}
            self._connection.execute(
                "UPDATE action_runs SET stop_requested = 1, status = ?, execution_status = ?, updated_at = ? WHERE run_id = ?",
                ("stop_requested", "stop_requested", time.time(), run_id),
            )
            self._connection.commit()
        return self.get(run_id) or {}

    def append_log(self, run_id: str, stream: str, line: str) -> int:
        if stream not in {"stdout", "stderr", "supervisor"}:
            stream = "supervisor"
        if not isinstance(line, str):
            line = str(line)
        line = line.replace("\x00", "")
        encoded = line.encode("utf-8", "replace")
        if len(encoded) > MAX_LOG_LINE_BYTES:
            encoded = encoded[:MAX_LOG_LINE_BYTES]
            line = encoded.decode("utf-8", "ignore")
        with self._lock:
            current = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM action_logs WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            sequence = int(current) + 1
            self._connection.execute(
                "INSERT INTO action_logs(run_id, sequence, stream, line, created_at) VALUES(?,?,?,?,?)",
                (run_id, sequence, stream, line, time.time()),
            )
            self._connection.commit()
        return sequence

    def logs(self, run_id: str, cursor: int = 0, limit: int = 100) -> dict[str, Any]:
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= MAX_LOG_LIMIT:
            raise ActionProtocolError("apps_logs_params_invalid")
        with self._lock:
            rows = self._connection.execute(
                "SELECT sequence, stream, line, created_at FROM action_logs WHERE run_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
                (run_id, cursor, limit),
            ).fetchall()
            last = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM action_logs WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        return {"run_id": run_id, "cursor": cursor,
                "entries": [dict(row) for row in rows],
                "next_cursor": int(rows[-1][0]) if rows else cursor,
                "latest_cursor": int(last)}

    def mark_unknown_if_unobserved(self, run_id: str, reason: str = "spawn_boundary_unknown") -> dict[str, Any]:
        row = self.get(run_id)
        if row is None:
            raise ActionProtocolError("apps_run_unknown")
        if row["status"] in TERMINAL_STATES:
            return row
        return self.update(run_id, status="unknown", execution_status="unknown", error=reason)

    def receipt(self, run_id: str) -> dict[str, Any]:
        row = self.get(run_id)
        if row is None:
            raise ActionProtocolError("apps_run_unknown")
        # This projection deliberately excludes local paths, input, commands,
        # and credentials.  Callers can safely return it over the API.
        result = {key: row.get(key) for key in (
            "run_id", "saved_action_id", "action_ref", "sha256", "contract_hash", "mode", "machine_id", "device_id",
            "connection_epoch", "status",
            "execution_status", "stop_requested", "supervisor_pid", "supervisor_started",
            "supervisor_boot_id", "pid", "process_started", "boot_id", "probe_pid",
            "probe_process_started", "probe_boot_id", "owned_processes", "result_present", "exit_code", "ready",
            "error", "created_at", "updated_at",
        )}
        if row.get("result_present"):
            result["result"] = row.get("result")
        result["sha256"] = row.get("action_sha256")
        result["input_sha256"] = row.get("input_sha256")
        return result


__all__ = ["ACTIVE_STATES", "ActionJournal", "TERMINAL_STATES"]
