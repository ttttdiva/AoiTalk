"""SQLite compare-and-swap journal for native agent-link turns."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from .codes import AgentLinkFailure, LinkErrorCode, normalize_code
from .contracts import Channel, Phase, TERMINAL_PHASES, TurnRef, nonce_sha256

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_JSON_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SENSITIVE_MARKER = re.compile(
    r"(?:token|secret|credential|password|cookie|authorization|bearer|api[_-]?key|url|uri|path|prompt|body|text|nonce)",
    re.IGNORECASE,
)
_BASELINE_KEYS = frozenset({"seq", "entry_id", "body_sha256"})
_PROOF_KEYS = frozenset({"settlement_hash", "settlement_target", "settlement_outcome", "settled_at_ms"})
_REMOTE_ID_KEYS = frozenset({"message_id", "request_id", "root_id", "entry_id"}) | _PROOF_KEYS


def valid_settlement(record: "JournalRecord", proof: Mapping[str, object]) -> bool:
    return bool(
        record.channel is Channel.GROK_BOT
        and proof.get("settlement_hash") == record.nonce_sha256
        and proof.get("settlement_target") == record.thread_ref
        and proof.get("settlement_outcome") in ("success", "cancelled", "error")
        and type(proof.get("settled_at_ms")) is int
        and proof["settled_at_ms"] > 0
    )
_ALLOWED_TRANSITIONS: dict[Phase, frozenset[Phase]] = {
    Phase.PREPARED: frozenset(
        {Phase.SENT, Phase.UNCERTAIN, Phase.REJECTED, Phase.FAILED, Phase.EXPIRED}
    ),
    Phase.SENT: frozenset({Phase.UNCERTAIN, Phase.REPLIED, Phase.FAILED, Phase.EXPIRED}),
    Phase.UNCERTAIN: frozenset({Phase.REPLIED, Phase.FAILED, Phase.EXPIRED}),
    Phase.REPLIED: frozenset(),
    Phase.REJECTED: frozenset(),
    Phase.FAILED: frozenset(),
    Phase.EXPIRED: frozenset(),
}


class JournalError(AgentLinkFailure):
    """Fixed-code journal error with no SQL, path, or input values in its text."""


@dataclass(frozen=True)
class JournalRecord:
    turn_id: str
    channel: Channel
    owner_id: str
    thread_ref: str
    nonce_sha256: str
    phase: Phase
    code: LinkErrorCode | None
    baseline: Mapping[str, int | str]
    remote_ids: Mapping[str, int | str]
    created_at: float
    updated_at: float
    expires_at: float
    delivered_at: float | None


def _time_value(value: object) -> float:
    try:
        rendered = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("journal timestamp is invalid") from None
    if not math.isfinite(rendered):
        raise ValueError("journal timestamp is invalid")
    return rendered


def _safe_identifier(value: object) -> str:
    if (
        not isinstance(value, str)
        or not _IDENTIFIER_RE.fullmatch(value)
        or _SENSITIVE_MARKER.search(value)
    ):
        raise ValueError("journal identifier is invalid")
    return value


def _safe_mapping(
    value: Mapping[str, object] | None,
    *,
    nonce: str | None = None,
    allowed_keys: frozenset[str],
) -> dict[str, int | str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > 32:
        raise ValueError("journal metadata is invalid")
    safe: dict[str, int | str] = {}
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or not _JSON_KEY_RE.fullmatch(key)
            or key not in allowed_keys
        ):
            raise ValueError("journal metadata is invalid")
        if type(item) is int:
            safe[key] = item
        elif (
            isinstance(item, str)
            and _IDENTIFIER_RE.fullmatch(item)
            and not _SENSITIVE_MARKER.search(item)
        ):
            if key == "body_sha256" and not re.fullmatch(r"[0-9a-f]{64}", item):
                raise ValueError("journal metadata is invalid")
            if nonce and item == nonce:
                raise ValueError("journal metadata is invalid")
            safe[key] = item
        else:
            raise ValueError("journal metadata is invalid")
    return safe


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


class SQLiteAgentLinkJournal:
    """Explicitly opened, workspace-confined SQLite turn journal.

    The default database is ``data/agent_link.sqlite3`` under the repository.
    The caller may supply a different absolute path, but it must remain inside
    ``workspace_root`` and every existing path component must be a real
    directory/file rather than a symlink or junction.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        workspace_root: Path | None = None,
    ) -> None:
        if workspace_root is None:
            workspace_root = Path(__file__).resolve().parents[3]
        if not isinstance(workspace_root, Path):
            raise TypeError("workspace_root must be a pathlib.Path")
        try:
            root = workspace_root.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValueError("workspace root is unavailable") from None
        if not root.is_dir() or _is_link(workspace_root.absolute()):
            raise ValueError("workspace root is invalid")

        if path is None:
            path = root / "data" / "agent_link.sqlite3"
        if not isinstance(path, Path):
            raise TypeError("journal path must be a pathlib.Path")
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("journal path must be absolute and normalized")

        try:
            relative = path.relative_to(root)
        except ValueError:
            raise ValueError("journal path must stay inside workspace root") from None
        if not relative.parts:
            raise ValueError("journal path is invalid")

        current = root
        for part in relative.parts[:-1]:
            current = current / part
            if _is_link(current):
                raise ValueError("journal path must not traverse a link")
        if _is_link(path):
            raise ValueError("journal file must not be a symlink or junction")

        try:
            parent = path.parent.resolve(strict=True)
            parent.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            raise ValueError("journal parent must stay inside workspace root") from None
        if parent != path.parent:
            raise ValueError("journal path must not traverse a link")
        if path.exists() and not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("journal path must be a regular file")

        self.path = parent / path.name
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                os.fspath(self.path), timeout=5.0, isolation_level=None
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            return connection
        except sqlite3.Error:
            raise JournalError(LinkErrorCode.LINK_INTERNAL) from None

    def _init_schema(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    turn_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    thread_ref TEXT NOT NULL,
                    nonce_sha256 TEXT NOT NULL CHECK(length(nonce_sha256)=64),
                    phase TEXT NOT NULL,
                    code TEXT,
                    baseline_json TEXT NOT NULL,
                    remote_ids_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    delivered_at REAL
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(turns)").fetchall()
            }
            if "delivered_at" not in columns:
                connection.execute("ALTER TABLE turns ADD COLUMN delivered_at REAL")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_turns_recoverable "
                "ON turns(channel, phase, expires_at, created_at)"
            )
            connection.execute("COMMIT")
        except sqlite3.Error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise JournalError(LinkErrorCode.LINK_INTERNAL) from None
        finally:
            connection.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> JournalRecord:
        try:
            turn_id = _safe_identifier(str(row["turn_id"]))
            owner_id = _safe_identifier(str(row["owner_id"]))
            thread_ref = _safe_identifier(str(row["thread_ref"]))
            nonce_hash = str(row["nonce_sha256"])
            if not re.fullmatch(r"[0-9a-f]{64}", nonce_hash):
                raise ValueError
            baseline = json.loads(row["baseline_json"])
            remote_ids = json.loads(row["remote_ids_json"])
            if not isinstance(baseline, dict) or not isinstance(remote_ids, dict):
                raise ValueError
            safe_baseline = _safe_mapping(
                baseline, allowed_keys=_BASELINE_KEYS
            )
            safe_remote_ids = _safe_mapping(
                remote_ids, allowed_keys=_REMOTE_ID_KEYS
            )
            code = normalize_code(row["code"]) if row["code"] else None
            return JournalRecord(
                turn_id=turn_id,
                channel=Channel(row["channel"]),
                owner_id=owner_id,
                thread_ref=thread_ref,
                nonce_sha256=nonce_hash,
                phase=Phase(row["phase"]),
                code=code,
                baseline=MappingProxyType(safe_baseline),
                remote_ids=MappingProxyType(safe_remote_ids),
                created_at=_time_value(row["created_at"]),
                updated_at=_time_value(row["updated_at"]),
                expires_at=_time_value(row["expires_at"]),
                delivered_at=(
                    _time_value(row["delivered_at"])
                    if row["delivered_at"] is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OverflowError):
            raise JournalError(LinkErrorCode.LINK_INTERNAL) from None

    def get(self, turn_id: str) -> JournalRecord | None:
        try:
            turn_id = str(uuid.UUID(str(turn_id)))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("turn_id must be a UUID") from None
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM turns WHERE turn_id=?", (turn_id,)
            ).fetchone()
            return self._record(row) if row is not None else None
        except sqlite3.Error:
            raise JournalError(LinkErrorCode.LINK_INTERNAL) from None
        finally:
            connection.close()

    @staticmethod
    def matches_turn(record: JournalRecord, turn: TurnRef) -> bool:
        return bool(
            record.turn_id == turn.turn_id
            and record.channel == turn.channel
            and record.owner_id == turn.owner_id
            and record.thread_ref == turn.thread_ref
            and record.nonce_sha256 == nonce_sha256(turn.nonce)
        )

    def create_prepared(
        self,
        turn: TurnRef,
        *,
        baseline: Mapping[str, object],
        now: float,
        expires_at: float,
    ) -> tuple[bool, JournalRecord]:
        owner_id = _safe_identifier(turn.owner_id)
        thread_ref = _safe_identifier(turn.thread_ref)
        safe_baseline = _safe_mapping(
            baseline,
            nonce=turn.nonce,
            allowed_keys=_BASELINE_KEYS,
        )
        now_value = _time_value(now)
        expiry_value = _time_value(expires_at)
        if expiry_value <= now_value:
            raise JournalError(LinkErrorCode.LINK_WINDOW_EXPIRED)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM turns WHERE turn_id=?", (turn.turn_id,)
            ).fetchone()
            if existing is not None:
                record = self._record(existing)
                if not self.matches_turn(record, turn):
                    connection.execute("ROLLBACK")
                    raise JournalError(LinkErrorCode.LINK_JOURNAL_CONFLICT)
                connection.execute("COMMIT")
                return False, record
            connection.execute(
                """
                INSERT INTO turns(
                    turn_id, channel, owner_id, thread_ref, nonce_sha256, phase,
                    code, baseline_json, remote_ids_json, created_at, updated_at,
                    expires_at, delivered_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, '{}', ?, ?, ?, NULL)
                """,
                (
                    turn.turn_id,
                    turn.channel.value,
                    owner_id,
                    thread_ref,
                    nonce_sha256(turn.nonce),
                    Phase.PREPARED.value,
                    _canonical_json(safe_baseline),
                    now_value,
                    now_value,
                    expiry_value,
                ),
            )
            connection.execute("COMMIT")
            row = connection.execute(
                "SELECT * FROM turns WHERE turn_id=?", (turn.turn_id,)
            ).fetchone()
            assert row is not None
            return True, self._record(row)
        except JournalError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except (sqlite3.Error, ValueError, TypeError):
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise JournalError(LinkErrorCode.LINK_INTERNAL) from None
        finally:
            connection.close()

    def transition(
        self,
        turn_id: str,
        *,
        expected: Phase | Iterable[Phase],
        phase: Phase,
        now: float,
        code: object = None,
        remote_ids: Mapping[str, object] | None = None,
    ) -> bool:
        try:
            canonical_turn_id = str(uuid.UUID(str(turn_id)))
            target_phase = Phase(phase)
            expected_phases = (
                (Phase(expected),)
                if isinstance(expected, (Phase, str))
                else tuple(Phase(item) for item in expected)
            )
        except (TypeError, ValueError, AttributeError):
            raise JournalError(LinkErrorCode.LINK_JOURNAL_CONFLICT) from None
        if not expected_phases or any(
            target_phase not in _ALLOWED_TRANSITIONS[item]
            for item in expected_phases
        ):
            raise JournalError(LinkErrorCode.LINK_JOURNAL_CONFLICT)
        timestamp = _time_value(now)
        normalized_code = normalize_code(code).value if code is not None else None
        if remote_ids is not None and any(key in remote_ids for key in _PROOF_KEYS):
            raise JournalError(LinkErrorCode.LINK_JOURNAL_CONFLICT)
        remote_json = (
            _canonical_json(
                _safe_mapping(remote_ids, allowed_keys=_REMOTE_ID_KEYS)
            )
            if remote_ids is not None
            else None
        )
        placeholders = ",".join("?" for _ in expected_phases)
        expiry_guard = "" if target_phase is Phase.EXPIRED else " AND expires_at>?"
        expiry_values: tuple[float, ...] = (
            () if target_phase is Phase.EXPIRED else (timestamp,)
        )
        connection = self._connect()
        try:
            cursor = connection.execute(
                f"""
                UPDATE turns
                SET phase=?, code=COALESCE(?, code),
                    remote_ids_json=COALESCE(?, remote_ids_json), updated_at=?
                WHERE turn_id=? AND phase IN ({placeholders}){expiry_guard}
                """,
                (
                    target_phase.value,
                    normalized_code,
                    remote_json,
                    timestamp,
                    canonical_turn_id,
                    *(item.value for item in expected_phases),
                    *expiry_values,
                ),
            )
            return cursor.rowcount == 1
        except (sqlite3.Error, ValueError, TypeError):
            raise JournalError(LinkErrorCode.LINK_INTERNAL) from None
        finally:
            connection.close()

    def settle_grok(self, turn: TurnRef, record: JournalRecord, *, phase: Phase,
                    proof: Mapping[str, object], now: float, code: object = None) -> bool:
        """CAS the matched settlement and phase together, or supplement legacy REPLIED."""
        if (not self.matches_turn(record, turn) or not valid_settlement(record, proof)
                or set(proof) != _PROOF_KEYS):
            return False
        outcome = proof["settlement_outcome"]
        if phase not in {Phase.REPLIED, Phase.FAILED} or (phase is Phase.REPLIED) != (outcome == "success"):
            return False
        if record.phase is Phase.REPLIED:
            # A valid prior success is immutable. An absent or corrupt proof can
            # be replaced only by a newly validated proof under the CAS below.
            if (valid_settlement(record, record.remote_ids)
                    and record.remote_ids.get("settlement_outcome") == "success"):
                return False
        elif record.phase not in {Phase.SENT, Phase.UNCERTAIN}:
            return False
        timestamp = _time_value(now)
        merged = dict(record.remote_ids)
        merged.update(proof)
        remote_json = _canonical_json(_safe_mapping(merged, allowed_keys=_REMOTE_ID_KEYS))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM turns WHERE turn_id=?", (record.turn_id,)).fetchone()
            current = self._record(row) if row is not None else None
            if (current is None or not self.matches_turn(current, turn)
                    or current.phase is not record.phase or current.remote_ids != record.remote_ids):
                connection.execute("ROLLBACK")
                return False
            cursor = connection.execute(
                "UPDATE turns SET phase=?, code=CASE WHEN ? AND code IN (?, ?, ?, ?, ?) "
                "THEN NULL ELSE COALESCE(?, code) END, remote_ids_json=?, updated_at=? "
                "WHERE turn_id=? AND channel=? AND owner_id=? AND thread_ref=? AND nonce_sha256=? "
                "AND phase=? AND remote_ids_json=? AND expires_at>?",
                (phase.value, phase is Phase.REPLIED,
                 LinkErrorCode.GROK_SEND_UNCERTAIN.value, LinkErrorCode.GROK_REPLY_PENDING.value,
                 LinkErrorCode.GROK_USAGE_LIMIT_EXCEEDED.value, LinkErrorCode.LINK_WINDOW_EXPIRED.value, LinkErrorCode.LINK_INTERNAL.value,
                 normalize_code(code).value if code else None, remote_json, timestamp,
                 record.turn_id, record.channel.value, record.owner_id, record.thread_ref,
                 record.nonce_sha256, record.phase.value, row["remote_ids_json"], timestamp),
            )
            connection.execute("COMMIT")
            return cursor.rowcount == 1
        except (sqlite3.Error, ValueError, TypeError):
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise JournalError(LinkErrorCode.LINK_INTERNAL) from None
        finally:
            connection.close()

    def acknowledge_delivery(self, turn: TurnRef, *, now: float) -> bool:
        """Mark one already-replied, exactly matching turn as delivered once."""

        timestamp = _time_value(now)
        connection = self._connect()
        try:
            cursor = connection.execute(
                """
                UPDATE turns SET delivered_at=?
                WHERE turn_id=? AND channel=? AND owner_id=? AND thread_ref=?
                  AND nonce_sha256=? AND phase=? AND delivered_at IS NULL
                """,
                (
                    timestamp,
                    turn.turn_id,
                    turn.channel.value,
                    turn.owner_id,
                    turn.thread_ref,
                    nonce_sha256(turn.nonce),
                    Phase.REPLIED.value,
                ),
            )
            if cursor.rowcount == 1:
                return True
            row = connection.execute(
                "SELECT * FROM turns WHERE turn_id=?", (turn.turn_id,)
            ).fetchone()
            if row is None:
                raise JournalError(LinkErrorCode.LINK_JOURNAL_CONFLICT)
            existing = self._record(row)
            if not self.matches_turn(existing, turn) or existing.phase is not Phase.REPLIED:
                raise JournalError(LinkErrorCode.LINK_JOURNAL_CONFLICT)
            # Repeated acknowledgment after the successful CAS is idempotent.
            return existing.delivered_at is not None
        except JournalError:
            raise
        except sqlite3.Error:
            raise JournalError(LinkErrorCode.LINK_INTERNAL) from None
        finally:
            connection.close()

    def list_recoverable(
        self,
        now: float,
        *,
        channel: Channel | str | None = None,
        limit: int = 100,
    ) -> list[JournalRecord]:
        return self._list_by_window(
            now,
            channel=channel,
            limit=limit,
            phases=(Phase.SENT, Phase.UNCERTAIN),
            comparison=">",
        )

    def list_expired(
        self,
        now: float,
        *,
        limit: int = 100,
    ) -> list[JournalRecord]:
        return self._list_by_window(
            now,
            channel=None,
            limit=limit,
            phases=(Phase.PREPARED, Phase.SENT, Phase.UNCERTAIN),
            comparison="<=",
        )

    def list_undelivered_replies(
        self,
        now: float,
        *,
        expired: bool = False,
        limit: int = 100,
    ) -> list[JournalRecord]:
        """List REPLIED rows still awaiting delivery, inside or past expiry."""

        timestamp = _time_value(now)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("journal limit is invalid")
        comparison = "<=" if expired else ">"
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM turns WHERE phase=? AND delivered_at IS NULL "
                f"AND expires_at {comparison} ? ORDER BY created_at, turn_id LIMIT ?",
                (Phase.REPLIED.value, timestamp, limit),
            ).fetchall()
            return [self._record(row) for row in rows]
        except sqlite3.Error:
            raise JournalError(LinkErrorCode.LINK_INTERNAL) from None
        finally:
            connection.close()

    def _list_by_window(
        self,
        now: float,
        *,
        channel: Channel | str | None,
        limit: int,
        phases: tuple[Phase, ...],
        comparison: str,
    ) -> list[JournalRecord]:
        timestamp = _time_value(now)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("journal limit is invalid")
        phase_marks = ",".join("?" for _ in phases)
        clauses = [f"phase IN ({phase_marks})", f"expires_at {comparison} ?"]
        values: list[object] = [*(phase.value for phase in phases), timestamp]
        if channel is not None:
            try:
                normalized_channel = Channel(channel)
            except (ValueError, TypeError):
                raise ValueError("journal channel is invalid") from None
            clauses.append("channel=?")
            values.append(normalized_channel.value)
        values.append(limit)
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM turns WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at, turn_id LIMIT ?",
                tuple(values),
            ).fetchall()
            return [self._record(row) for row in rows]
        except sqlite3.Error:
            raise JournalError(LinkErrorCode.LINK_INTERNAL) from None
        finally:
            connection.close()
