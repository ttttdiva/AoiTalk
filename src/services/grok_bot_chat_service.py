"""Grok Bot ordinary-Chat boundary using AoiTalk's native transport.

This module owns:

* an explicit per-owner binding read from the ``grok_bot_chat`` setting;
* a local receipt/history store keyed by owner + Chat session;
* owner/chat-scoped operation ids, so a retry never becomes a second send;
* read-only reconciliation for history refresh;
* local-only cancellation;
* bounded, owner-only recovery of correlated replies that arrive after the
  Chat turn stopped waiting (see :mod:`src.services.grok_bot_chat_recovery`).

The transport exposes no remote session creation, thread continuation
(``sendPrompt`` has no ``rootId``), remote terminal state or remote cancel.
Every send therefore opens its own correlated root on the bound target and the
receipts label remote terminal/cancel as ``unsupported``.  Reconcile returns
the currently visible correlated reply texts (no reply ids, no final marker),
so an acknowledgement and a later final reply are kept as separate fragments
and never treated as a remote terminal state.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol

PROVIDER_ID = "grok-bot"
MODEL_ID = "grok-bot"
CONFIG_KEY = "grok_bot_chat"
OPERATION_PREFIX = "aoichat"
REMOTE_TERMINAL = "unsupported"
REMOTE_CANCEL = "unsupported"
MAX_PROMPT_BYTES = 24 * 1024
MAX_CONTEXT_TURNS = 6
# A Grok turn may remain queued remotely for a long time.  Keep the durable
# receipt and native journal alive for the same window used by history
# recovery, so a process restart does not make a still-valid late reply
# unrecoverable after the old thirty-minute default expires.
RECOVERY_WINDOW = timedelta(days=14)
RECOVERY_WINDOW_SECONDS = RECOVERY_WINDOW.total_seconds()

STATE_PENDING = "pending"
STATE_SENT = "sent"
STATE_UNCERTAIN = "uncertain"
STATE_REPLY_OBSERVED = "reply_observed"
STATE_REPLIED = "replied"
STATE_EMPTY_RESPONSE = "empty_response"
STATE_INCOMPLETE_RESPONSE = "incomplete_response"
STATE_REJECTED = "rejected"
STATE_FAILED = "failed"
STATE_MISROUTED = "misrouted"
STATE_CANCELLED_LOCAL = "cancelled_local"
COMPLETION_PENDING = "pending"
COMPLETION_UNKNOWN = "unknown"
COMPLETION_EMPTY = "empty"
COMPLETION_TOOL_ONLY = "tool_only"
COMPLETION_NON_TEXT = "non_text"
COMPLETION_TRUNCATED = "truncated"
COMPLETION_NONTERMINAL = "nonterminal"
COMPLETION_COMPLETED = "completed"
COMPLETION_REJECTED = "rejected"
COMPLETION_CANCELLED = "cancelled"
COMPLETION_NOT_STARTED = "not_started"
COMPLETION_STATUSES = frozenset(
    {
        COMPLETION_PENDING,
        COMPLETION_UNKNOWN,
        COMPLETION_EMPTY,
        COMPLETION_TOOL_ONLY,
        COMPLETION_NON_TEXT,
        COMPLETION_TRUNCATED,
        COMPLETION_NONTERMINAL,
        COMPLETION_COMPLETED,
        COMPLETION_REJECTED,
        COMPLETION_CANCELLED,
        COMPLETION_NOT_STARTED,
    }
)
OPEN_STATES = frozenset({STATE_PENDING, STATE_SENT, STATE_UNCERTAIN, STATE_REPLY_OBSERVED})
# History recovery only looks at turns that may still gain a correlated reply.
# Local cancel, rejection, failure and misrouting stay final.
RECOVERABLE_STATES = frozenset(
    {STATE_PENDING, STATE_SENT, STATE_UNCERTAIN, STATE_REPLY_OBSERVED, STATE_REPLIED}
)
RECOVERY_MIN_INTERVAL_SECONDS = 15.0
MAX_RECOVERY_OPERATIONS = 3
FRAGMENT_SEPARATOR = "\n\n"
CANCELLED_BEFORE_SEND = "CANCELLED_BEFORE_SEND"
_RECOVERY_NAMESPACE = uuid.UUID("6f1c6c3e-7d0b-5b8e-9a51-3c1f0b7a2e44")

_IDENTIFIER_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_LOCAL_KEY_RE = re.compile(r"^[A-Za-z0-9_.:@+\-]{1,200}$")
class GrokBotChatUnavailable(ValueError):
    """Required binding/config/transport precondition is missing."""

    def __init__(self, code: str, message: str):
        super().__init__(f"Grok Bot Chatは利用できません: {message}")
        self.code = code
        self.user_message = message


class GrokBotChatError(RuntimeError):
    """A bound Chat turn was refused or did not produce a correlated reply."""

    def __init__(self, code: str, message: str, *, operation_id: str | None = None):
        super().__init__(f"Grok Bot Chat: {message}")
        self.code = code
        self.user_message = message
        self.operation_id = operation_id


class TransportFailure(RuntimeError):
    """Definitive pre-send failure, or a failure in a read-only operation."""

    def __init__(
        self,
        stage: str,
        code: str,
        status: int | None = None,
        *,
        diagnostics: Mapping[str, Any] | None = None,
    ):
        super().__init__(code)
        self.stage = stage
        self.code = code
        self.status = status
        self.diagnostics = _safe_diagnostic(diagnostics)


class TransportUncertain(RuntimeError):
    """The transport may have sent; reconcile the outcome, never resend."""

    def __init__(
        self,
        stage: str,
        code: str,
        status: int | None = None,
        *,
        diagnostics: Mapping[str, Any] | None = None,
    ):
        super().__init__(code)
        self.stage = stage
        self.code = code
        self.status = status
        self.diagnostics = _safe_diagnostic(diagnostics)


@dataclass(frozen=True)
class BindingProblem:
    code: str
    message: str


@dataclass(frozen=True)
class GrokBotOwnerBinding:
    owner_id: str
    generation: int


@dataclass(frozen=True)
class GrokBotChatSettings:
    target_identity: str
    owners: Mapping[str, GrokBotOwnerBinding]
    receipt_store_path: Path

    def binding_for(self, owner_id: str) -> GrokBotOwnerBinding | None:
        return self.owners.get(str(owner_id or ""))


@dataclass(frozen=True)
class GrokBotTurnResult:
    operation_id: str
    state: str
    text: str
    remote_terminal: str
    error_code: str | None
    response_truncated: bool = False
    remote_terminal_supported: bool = False
    transport_diagnostics: Mapping[str, Any] | None = None
    completion_status: str = COMPLETION_UNKNOWN
    correlation: Mapping[str, str] | None = None


@dataclass(frozen=True)
class GrokBotRecoveredReply:
    """Correlated reply fragments not yet persisted to the Chat history."""

    operation_id: str
    turn_key: str
    fragments: tuple[str, ...]
    fragment_shas: tuple[str, ...]
    response_truncated: bool
    remote_terminal: str = REMOTE_TERMINAL
    remote_terminal_supported: bool = False
    completion_status: str = COMPLETION_UNKNOWN

    @property
    def text(self) -> str:
        return FRAGMENT_SEPARATOR.join(self.fragments)

    @property
    def delivery_key(self) -> str:
        return _sha256(json.dumps([self.operation_id, list(self.fragment_shas)]))

    @property
    def message_id(self) -> str:
        """Deterministic Chat message id, so a reload can never duplicate it."""

        return str(uuid.uuid5(_RECOVERY_NAMESPACE, f"{self.operation_id}:{self.delivery_key}"))


def _valid_identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= 1024
        and not _IDENTIFIER_CONTROL.search(value)
    )


def _default_receipt_store_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "data"
        / "grok_bot_chat"
        / "receipts.sqlite3"
    )


def _read_section(config: Any) -> Any:
    if config is None:
        return None
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(CONFIG_KEY, None)
        except TypeError:
            return None
    return None


def resolve_grok_bot_chat_settings(
    config: Any,
) -> tuple[GrokBotChatSettings | None, BindingProblem | None]:
    """Validate the explicit ``grok_bot_chat`` binding without any I/O to Bot."""

    section = _read_section(config)
    if not isinstance(section, Mapping):
        return None, BindingProblem(
            "BINDING_NOT_CONFIGURED",
            "設定 grok_bot_chat (target_identity / owners) が未設定です。",
        )
    if not any(key in section for key in ("enabled", "target_identity", "owners")):
        return None, BindingProblem(
            "BINDING_NOT_CONFIGURED", "設定 grok_bot_chat (target_identity / owners) が未設定です。"
        )
    if section.get("enabled") is not True:
        return None, BindingProblem(
            "BINDING_DISABLED", "設定 grok_bot_chat.enabled が true ではありません。"
        )

    target_identity = section.get("target_identity")
    if not _valid_identifier(target_identity):
        return None, BindingProblem(
            "TARGET_NOT_CONFIGURED", "binding 対象の target_identity が未設定です。"
        )

    raw_owners = section.get("owners")
    if not isinstance(raw_owners, Mapping) or not raw_owners:
        return None, BindingProblem(
            "OWNER_BINDING_MISSING", "Chat所有者ごとの owners binding が未設定です。"
        )
    owners: dict[str, GrokBotOwnerBinding] = {}
    for owner_id, raw_binding in raw_owners.items():
        generation = raw_binding.get("generation") if isinstance(raw_binding, Mapping) else None
        if (
            not _valid_identifier(owner_id)
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            return None, BindingProblem(
                "OWNER_BINDING_INVALID",
                "owners の各要素には正の整数 generation が必要です。",
            )
        owners[owner_id] = GrokBotOwnerBinding(owner_id=owner_id, generation=generation)

    raw_store = section.get("receipt_store_path")
    if raw_store is None:
        receipt_store_path = _default_receipt_store_path()
    elif isinstance(raw_store, str) and Path(raw_store).is_absolute():
        receipt_store_path = Path(raw_store)
    else:
        return None, BindingProblem(
            "RECEIPT_STORE_INVALID", "receipt_store_path は絶対パスである必要があります。"
        )

    return (
        GrokBotChatSettings(
            target_identity=target_identity,
            owners=owners,
            receipt_store_path=receipt_store_path,
        ),
        None,
    )


def grok_bot_chat_catalog_status(config: Any) -> tuple[bool, BindingProblem | None]:
    settings, problem = resolve_grok_bot_chat_settings(config)
    return settings is not None, problem


def require_grok_bot_chat_settings(config: Any) -> GrokBotChatSettings:
    settings, problem = resolve_grok_bot_chat_settings(config)
    if settings is None:
        assert problem is not None
        raise GrokBotChatUnavailable(problem.code, problem.message)
    return settings


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_operation_id(owner_id: str, chat_id: str, generation: int, turn_key: str) -> str:
    """Owner/chat/generation-scoped id; the raw owner id never leaves AoiTalk."""

    digest = hashlib.sha256(
        json.dumps(
            [str(owner_id), str(chat_id), int(generation), str(turn_key)],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"{OPERATION_PREFIX}-g{int(generation)}-{digest[:40]}"


# --- transport ----------------------------------------------------------------


class GrokBotTransport(Protocol):
    def anchor_target(self) -> str: ...

    def execute(self, operation_id: str, prompt: str) -> dict[str, Any]: ...

    def reconcile(self, operation_id: str) -> dict[str, Any]: ...

    def operation_record(self, operation_id: str) -> dict[str, Any]: ...


def _safe_code(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", value) else fallback


def _safe_status(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599 else None


def _safe_correlation(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    fields = {
        "request_message_id": value.get("request_message_id"),
        "request_id": value.get("request_id"),
        "root_id": value.get("root_id"),
    }
    if any(not _valid_identifier(item) or len(item) > 512 for item in fields.values()):
        return None
    return {key: str(item) for key, item in fields.items()}


def _transport_correlation(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    nested = value.get("correlation")
    return _safe_correlation(nested if isinstance(nested, Mapping) else value)


def _transport_error_fields(error: Any, default_completion: str) -> dict[str, Any]:
    diagnostics = _safe_diagnostic(getattr(error, "diagnostics", None))
    fields: dict[str, Any] = {"completion_status": default_completion}
    delivery = diagnostics.get("delivery_status")
    if delivery in {"accepted", "rejected", "unknown"}:
        fields["delivery_status"] = delivery
    inspection = diagnostics.get("inspection_status")
    if inspection in {"observed", "pending", "unknown"}:
        fields["inspection_status"] = inspection
    completion = diagnostics.get("completion_status")
    if completion in COMPLETION_STATUSES:
        fields["completion_status"] = (
            COMPLETION_NONTERMINAL if completion == COMPLETION_COMPLETED else completion
        )
    fields.update(_transport_correlation(diagnostics) or {})
    return fields


def _safe_diagnostic(value: Any, *, include_nested: bool = True) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    diagnostic: dict[str, Any] = {}
    stage = value.get("stage")
    if isinstance(stage, str) and re.fullmatch(r"[a-z_]{1,32}", stage):
        diagnostic["stage"] = stage
    code = _safe_code(value.get("code"), "")
    if code:
        diagnostic["code"] = code
    status = _safe_status(value.get("status"))
    if status is not None:
        diagnostic["status"] = status
    category = value.get("error_category")
    if isinstance(category, str) and category in {
        "http_status",
        "inspection",
        "ledger",
        "schema",
        "transport",
    }:
        diagnostic["error_category"] = category
    remote_error_code = value.get("remote_error_code")
    if isinstance(remote_error_code, str) and re.fullmatch(
        r"[A-Za-z0-9_.:-]{1,128}", remote_error_code
    ):
        diagnostic["remote_error_code"] = remote_error_code
    remote_request_id = value.get("remote_request_id")
    if isinstance(remote_request_id, str) and re.fullmatch(
        r"[A-Za-z0-9_.:-]{1,256}", remote_request_id
    ):
        diagnostic["remote_request_id"] = remote_request_id
    completion_status = value.get("completion_status")
    if isinstance(completion_status, str) and completion_status in COMPLETION_STATUSES:
        diagnostic["completion_status"] = completion_status
    delivery_status = value.get("delivery_status")
    if isinstance(delivery_status, str) and delivery_status in {
        "accepted",
        "rejected",
        "unknown",
    }:
        diagnostic["delivery_status"] = delivery_status
    inspection_status = value.get("inspection_status")
    if isinstance(inspection_status, str) and inspection_status in {
        "observed",
        "pending",
        "unknown",
    }:
        diagnostic["inspection_status"] = inspection_status
    correlation = _safe_correlation(value.get("correlation"))
    if correlation is not None:
        diagnostic["correlation"] = correlation
    for key in (
        "reply_entries_observed",
        "tool_entries_observed",
        "non_text_entries_observed",
    ):
        count = value.get(key)
        if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 10000:
            diagnostic[key] = count
    for key in ("tool_only", "operation_completed", "send_attempted"):
        flag = value.get(key)
        if isinstance(flag, bool):
            diagnostic[key] = flag
    if include_nested:
        for key in ("send_error", "inspection_error"):
            nested = _safe_diagnostic(value.get(key), include_nested=False)
            if nested:
                diagnostic[key] = nested
    return diagnostic


def _outcome_diagnostics(outcome: Mapping[str, Any]) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    overall = _safe_diagnostic(outcome, include_nested=False)
    if overall:
        diagnostics["overall"] = overall
    for name in ("send_error", "inspection_error"):
        item = _safe_diagnostic(outcome.get(name))
        if item:
            diagnostics[name] = item
    return diagnostics


def _exception_diagnostics(scope: str, error: Any) -> dict[str, Any]:
    value = {
        "stage": getattr(error, "stage", None),
        "code": getattr(error, "code", None),
        "status": getattr(error, "status", None),
        **(
            dict(error.diagnostics)
            if isinstance(getattr(error, "diagnostics", None), Mapping)
            else {}
        ),
    }
    safe = _safe_diagnostic(value)
    if not safe:
        return {}
    result: dict[str, Any] = {}
    nested = {
        key: safe.pop(key)
        for key in ("send_error", "inspection_error")
        if key in safe
    }
    result[scope] = safe
    result.update(nested)
    return result


def _receipt_diagnostics(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = record.get("transport_diagnostics")
    try:
        decoded = json.loads(raw) if isinstance(raw, str) and raw else {}
    except (TypeError, ValueError):
        return {}
    if not isinstance(decoded, Mapping):
        return {}
    result: dict[str, Any] = {}
    for scope in (
        "overall", "execute", "reconcile", "verify_target", "send_error", "inspection_error"
    ):
        item = _safe_diagnostic(decoded.get(scope))
        if item:
            result[scope] = item
    return result


def _diagnostics_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


# --- local receipt / history store ---------------------------------------------


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS chats (
        chat_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        target_identity TEXT NOT NULL,
        generation INTEGER NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS operations (
        operation_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        turn_key TEXT NOT NULL,
        target_identity TEXT NOT NULL,
        generation INTEGER NOT NULL,
        user_text TEXT NOT NULL,
        user_text_sha256 TEXT NOT NULL,
        prompt_sha256 TEXT NOT NULL,
        state TEXT NOT NULL,
        delivery_status TEXT,
        inspection_status TEXT,
        completion_status TEXT NOT NULL DEFAULT 'unknown',
        remote_terminal_status TEXT NOT NULL DEFAULT 'unsupported',
        remote_terminal_supported INTEGER NOT NULL DEFAULT 0,
        transport_diagnostics TEXT NOT NULL DEFAULT '{}',
        client_nonce TEXT,
        request_message_id TEXT,
        request_id TEXT,
        root_id TEXT,
        reply_text TEXT NOT NULL DEFAULT '',
        response_truncated INTEGER NOT NULL DEFAULT 0,
        error_code TEXT,
        reply_fragments TEXT NOT NULL DEFAULT '[]',
        delivered_fragments TEXT NOT NULL DEFAULT '[]',
        last_reconcile_at REAL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (chat_id, turn_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS operations_chat ON operations (chat_id, created_at)",
)
# Columns added after the first receipt schema; older files gain them in place.
_ADDED_OPERATION_COLUMNS = (
    ("reply_fragments", "TEXT NOT NULL DEFAULT '[]'"),
    ("delivered_fragments", "TEXT NOT NULL DEFAULT '[]'"),
    ("last_reconcile_at", "REAL"),
    ("remote_terminal_status", "TEXT NOT NULL DEFAULT 'unsupported'"),
    ("remote_terminal_supported", "INTEGER NOT NULL DEFAULT 0"),
    ("transport_diagnostics", "TEXT NOT NULL DEFAULT '{}'"),
    ("completion_status", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("request_message_id", "TEXT"),
    ("request_id", "TEXT"),
    ("root_id", "TEXT"),
)


def _json_list(value: Any) -> list[str]:
    try:
        loaded = json.loads(value) if isinstance(value, str) and value else []
    except ValueError:
        return []
    return [item for item in loaded if isinstance(item, str)] if isinstance(loaded, list) else []


def _record_fragments(record: Mapping[str, Any]) -> list[str]:
    fragments = _json_list(record.get("reply_fragments"))
    if not fragments and record.get("reply_text"):
        # Receipts written before fragments were tracked hold one joined reply.
        fragments = [str(record["reply_text"])]
    return fragments


class GrokBotReceiptStore:
    """SQLite receipts under ``data/``; holds Chat bodies, never credentials."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time):
        self._path = Path(path)
        self._clock = clock
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self._path), timeout=10)
        connection.row_factory = sqlite3.Row
        for statement in _SCHEMA:
            connection.execute(statement)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(operations)")}
        for name, declaration in _ADDED_OPERATION_COLUMNS:
            if name not in columns:
                connection.execute(f"ALTER TABLE operations ADD COLUMN {name} {declaration}")
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get_chat(self, chat_id: str) -> dict[str, Any] | None:
        with self._lock, self._session() as db:
            row = db.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return dict(row) if row else None

    def ensure_chat(self, chat_id: str, owner_id: str, target: str, generation: int) -> dict[str, Any]:
        with self._lock, self._session() as db:
            db.execute(
                "INSERT OR IGNORE INTO chats (chat_id, owner_id, target_identity, generation, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (chat_id, owner_id, target, generation, self._clock()),
            )
            row = db.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return dict(row)

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self._lock, self._session() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return dict(row) if row else None

    def insert_operation(self, record: dict[str, Any]) -> bool:
        now = self._clock()
        values = {**record, "created_at": now, "updated_at": now}
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with self._lock, self._session() as db:
            try:
                db.execute(
                    f"INSERT INTO operations ({columns}) VALUES ({marks})",
                    tuple(values.values()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    _UPDATABLE = frozenset(
        {
            "state",
            "delivery_status",
            "inspection_status",
            "completion_status",
            "remote_terminal_status",
            "remote_terminal_supported",
            "transport_diagnostics",
            "prompt_sha256",
            "client_nonce",
            "request_message_id",
            "request_id",
            "root_id",
            "reply_text",
            "response_truncated",
            "error_code",
            "reply_fragments",
            "delivered_fragments",
            "last_reconcile_at",
        }
    )

    def update_operation(self, operation_id: str, **fields: Any) -> dict[str, Any]:
        unknown = set(fields) - self._UPDATABLE
        if unknown:
            raise ValueError(f"unsupported receipt fields: {sorted(unknown)}")
        with self._lock, self._session() as db:
            if fields:
                assignments = ", ".join(f"{name} = ?" for name in fields)
                db.execute(
                    f"UPDATE operations SET {assignments}, updated_at = ? WHERE operation_id = ?",
                    (*fields.values(), self._clock(), operation_id),
                )
            row = db.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return dict(row)

    def cancel_if_open(self, operation_id: str) -> dict[str, Any]:
        placeholders = ", ".join("?" for _ in OPEN_STATES)
        with self._lock, self._session() as db:
            db.execute(
                f"UPDATE operations SET state = ?, updated_at = ?"
                f" WHERE operation_id = ? AND state IN ({placeholders})",
                (STATE_CANCELLED_LOCAL, self._clock(), operation_id, *sorted(OPEN_STATES)),
            )
            row = db.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return dict(row)

    def list_operations(self, chat_id: str) -> list[dict[str, Any]]:
        with self._lock, self._session() as db:
            rows = db.execute(
                "SELECT * FROM operations WHERE chat_id = ? ORDER BY created_at, rowid",
                (chat_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_recoverable_chats(
        self, *, since: float, limit: int = 1000
    ) -> list[dict[str, Any]]:
        """List Chat bindings with recent open/replayable receipt rows."""

        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("recovery limit is invalid")
        states = sorted(RECOVERABLE_STATES)
        placeholders = ",".join("?" for _ in states)
        with self._lock, self._session() as db:
            rows = db.execute(
                "SELECT c.chat_id, c.owner_id, c.target_identity, c.generation, "
                "o.state, o.reply_fragments, o.reply_text, o.delivered_fragments "
                "FROM chats c JOIN operations o ON o.chat_id = c.chat_id "
                f"WHERE o.created_at >= ? AND o.state IN ({placeholders}) "
                "ORDER BY c.created_at, c.chat_id, o.created_at LIMIT ?",
                (float(since), *states, limit),
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            record = dict(row)
            state = record.get("state")
            if state in {STATE_REPLY_OBSERVED, STATE_REPLIED}:
                delivered = set(_json_list(record.get("delivered_fragments")))
                fragments = _record_fragments(record)
                if not any(_sha256(item) not in delivered for item in fragments):
                    continue
            result[str(record["chat_id"])] = {
                "chat_id": str(record["chat_id"]),
                "owner_id": str(record["owner_id"]),
                "target_identity": str(record["target_identity"]),
                "generation": int(record["generation"]),
            }
            if len(result) >= limit:
                break
        return list(result.values())


# --- service --------------------------------------------------------------------


_INFLIGHT: set[str] = set()
_INFLIGHT_LOCK = threading.Lock()
# Operations whose history recovery reconcile is running in this process.
_RECOVERING: set[str] = set()


def _cancelled(cancel_event: threading.Event | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _public_receipt(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "operation_id": record["operation_id"],
        "chat_id": record["chat_id"],
        "turn_key": record["turn_key"],
        "generation": record["generation"],
        "state": record["state"],
        "delivery_status": record.get("delivery_status"),
        "inspection_status": record.get("inspection_status"),
        "completion_status": record.get("completion_status") or COMPLETION_UNKNOWN,
        "user_text": record["user_text"],
        "reply_text": record.get("reply_text") or "",
        "response_truncated": bool(record.get("response_truncated")),
        "error_code": record.get("error_code"),
        "remote_terminal": record.get("remote_terminal_status") or REMOTE_TERMINAL,
        "remote_terminal_supported": bool(record.get("remote_terminal_supported")),
        "remote_cancel": REMOTE_CANCEL,
        "remote_cancel_supported": False,
        "transport_diagnostics": _receipt_diagnostics(record),
        "correlation": _safe_correlation(
            {
                "request_message_id": record.get("request_message_id"),
                "request_id": record.get("request_id"),
                "root_id": record.get("root_id"),
            }
        ),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


class GrokBotChatService:
    """Owner/chat-bound Grok Bot turns with persisted, correlated receipts."""

    def __init__(
        self,
        settings: GrokBotChatSettings,
        *,
        transport: GrokBotTransport | None = None,
        store: GrokBotReceiptStore | None = None,
        wall_clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        if transport is not None:
            self.transport = transport
        else:
            from .grok_native_transport import NativeGrokTransport

            self.transport = NativeGrokTransport(settings)
        self.store = store or GrokBotReceiptStore(settings.receipt_store_path)
        self._wall_clock = wall_clock

    def close(self) -> None:
        """Close the native transport when it exposes a close hook."""

        close = getattr(self.transport, "close", None)
        if callable(close):
            close()

    # -- binding checks -------------------------------------------------------

    def _owner_binding(self, owner_id: str) -> GrokBotOwnerBinding:
        if not _valid_identifier(owner_id):
            raise GrokBotChatUnavailable("OWNER_REQUIRED", "Chat所有者を確認できません。")
        binding = self.settings.binding_for(owner_id)
        if binding is None:
            raise GrokBotChatUnavailable(
                "OWNER_NOT_BOUND", "このユーザーにはGrok Bot Chatのbindingがありません。"
            )
        return binding

    @staticmethod
    def _require_key(value: Any, code: str, label: str) -> str:
        if not isinstance(value, str) or not _LOCAL_KEY_RE.fullmatch(value):
            raise GrokBotChatUnavailable(code, f"{label}が不正です。")
        return value

    def _owned_chat(self, owner_id: str, chat_id: str) -> dict[str, Any] | None:
        chat = self.store.get_chat(chat_id)
        if chat is not None and chat["owner_id"] != owner_id:
            raise GrokBotChatError(
                "CHAT_OWNER_MISMATCH", "このChatは別の所有者のGrok Bot会話です。"
            )
        return chat

    def _check_chat_binding(self, chat: Mapping[str, Any] | None, binding: GrokBotOwnerBinding) -> None:
        if chat is None:
            return
        if (
            chat["target_identity"] != self.settings.target_identity
            or int(chat["generation"]) != binding.generation
        ):
            raise GrokBotChatError(
                "STALE_CHAT_BINDING",
                "このChatは以前のGrok Bot bindingに結び付いています。新しいChatで送信してください。",
            )

    def _owned_operation(self, owner_id: str, chat_id: str, operation_id: str) -> dict[str, Any]:
        chat = self._owned_chat(owner_id, chat_id)
        record = self.store.get_operation(operation_id) if chat is not None else None
        if record is None or record["owner_id"] != owner_id or record["chat_id"] != chat_id:
            raise GrokBotChatError("OPERATION_NOT_FOUND", "このChatの送信記録が見つかりません。")
        return record

    # -- prompt -----------------------------------------------------------------

    def _compose_prompt(self, chat_id: str, user_text: str) -> str:
        if len(user_text.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise GrokBotChatError("PROMPT_TOO_LARGE", "メッセージが長すぎます。")
        prior = [
            record
            for record in self.store.list_operations(chat_id)
            if record["state"] == STATE_REPLIED and record.get("reply_text")
        ][-MAX_CONTEXT_TURNS:]
        while prior:
            lines = [
                "以下はAoiTalk Chatの同じ会話の直前のやり取りです（参考情報）。",
            ]
            for record in prior:
                lines.append(f"ユーザー: {record['user_text']}")
                lines.append(f"Grok Bot: {record['reply_text']}")
            lines.extend(["", "今回のメッセージ:", user_text])
            prompt = "\n".join(lines)
            if len(prompt.encode("utf-8")) <= MAX_PROMPT_BYTES:
                return prompt
            prior = prior[1:]
        return user_text

    # -- outcome handling -------------------------------------------------------

    def _set_state(self, operation_id: str, state: str, **fields: Any) -> dict[str, Any]:
        current = self.store.get_operation(operation_id)
        if current is not None and current["state"] == STATE_CANCELLED_LOCAL and state != STATE_MISROUTED:
            # A local cancel stays final; only the diagnostic code is kept.
            return self.store.update_operation(operation_id, **fields)
        return self.store.update_operation(operation_id, state=state, **fields)

    def _verify_target(self, record: dict[str, Any], *, after_send: bool) -> tuple[dict[str, Any], bool]:
        operation_id = record["operation_id"]
        try:
            ledger = self.transport.operation_record(operation_id)
        except TransportFailure as error:
            if error.code == "CORRELATION_MISMATCH":
                return self._set_state(
                    operation_id, STATE_MISROUTED,
                    error_code="CORRELATION_MISMATCH", reply_text="",
                ), False
            with _INFLIGHT_LOCK:
                inflight = operation_id in _INFLIGHT
            if (
                error.code == "OPERATION_NOT_FOUND"
                and not after_send
                and not inflight
                and record["state"]
                in {STATE_PENDING, STATE_SENT, STATE_UNCERTAIN, STATE_REPLY_OBSERVED}
            ):
                # An absent native journal row before send means admission did
                # not happen, so it is safe to record a definitive failure.
                return (
                    self._set_state(
                        operation_id,
                        STATE_FAILED,
                        delivery_status="not_sent",
                        inspection_status="not_attempted",
                        completion_status=COMPLETION_NOT_STARTED,
                        error_code="OPERATION_NOT_ADMITTED",
                        transport_diagnostics=_diagnostics_json(
                            _exception_diagnostics("verify_target", error)
                        ),
                    ),
                    False,
                )
            if after_send:
                return self._set_state(
                    operation_id,
                    STATE_UNCERTAIN,
                    completion_status=COMPLETION_UNKNOWN,
                    error_code="TARGET_UNVERIFIED",
                    transport_diagnostics=_diagnostics_json(
                        _exception_diagnostics("verify_target", error)
                    ),
                ), False
            return self.store.update_operation(
                operation_id,
                error_code="TARGET_UNVERIFIED",
                transport_diagnostics=_diagnostics_json(
                    _exception_diagnostics("verify_target", error)
                ),
            ), False
        if (
            not isinstance(ledger, Mapping)
            or ledger.get("operation_id") != operation_id
            or ledger.get("target_identity") != self.settings.target_identity
        ):
            return (
                self._set_state(operation_id, STATE_MISROUTED, error_code="TARGET_MISMATCH", reply_text=""
                ),
                False,
            )
        stored_correlation = _safe_correlation(
            {
                "request_message_id": record.get("request_message_id"),
                "request_id": record.get("request_id"),
                "root_id": record.get("root_id"),
            }
        )
        ledger_correlation = _transport_correlation(ledger)
        if stored_correlation and ledger_correlation and stored_correlation != ledger_correlation:
            return (
                self._set_state(
                    operation_id,
                    STATE_MISROUTED,
                    error_code="CORRELATION_MISMATCH",
                    reply_text="",
                ),
                False,
            )
        target_fields: dict[str, Any] = {}
        nonce = ledger.get("client_nonce")
        stored_nonce = record.get("client_nonce")
        if stored_nonce and nonce != stored_nonce:
            return (
                self._set_state(
                    operation_id,
                    STATE_MISROUTED,
                    error_code="CORRELATION_MISMATCH",
                    reply_text="",
                ),
                False,
            )
        if _valid_identifier(nonce) and not record.get("client_nonce"):
            target_fields["client_nonce"] = nonce
        if ledger_correlation and not stored_correlation:
            target_fields.update(ledger_correlation)
        if target_fields:
            record = self.store.update_operation(operation_id, **target_fields)
        return record, True

    def _apply_outcome(self, record: dict[str, Any], outcome: Mapping[str, Any]) -> dict[str, Any]:
        operation_id = record["operation_id"]
        current = self.store.get_operation(operation_id) or record
        if not isinstance(outcome, Mapping) or outcome.get("operation_id") != operation_id:
            return self._set_state(operation_id, STATE_MISROUTED, error_code="OPERATION_ECHO_MISMATCH", reply_text=""
            )
        if current["state"] == STATE_CANCELLED_LOCAL:
            # A local cancellation remains final. Recheck old replied receipts
            # against the durable journal before treating them as formal.
            return current
        raw_delivery = outcome.get("delivery_status")
        delivery = (
            raw_delivery
            if isinstance(raw_delivery, str) and raw_delivery in {"accepted", "rejected"}
            else "unknown"
        )
        raw_inspection = outcome.get("inspection_status")
        inspection = (
            raw_inspection
            if isinstance(raw_inspection, str)
            and raw_inspection in {"observed", "pending", "unknown"}
            else "unknown"
        )
        raw_replies = outcome.get("replies")
        replies = [item for item in raw_replies if isinstance(item, str)] if isinstance(raw_replies, list) else []
        observed = [item for item in replies if item.strip()] if inspection == "observed" else []
        response_truncated = outcome.get("response_truncated") is True
        raw_terminal = outcome.get("remote_terminal")
        terminal = (
            raw_terminal
            if isinstance(raw_terminal, str) and re.fullmatch(r"[a-z_]{1,32}", raw_terminal)
            else REMOTE_TERMINAL
        )
        terminal_supported = outcome.get("remote_terminal_supported") is True and terminal != REMOTE_TERMINAL
        remote_completed = terminal_supported and terminal == "completed"
        remote_failed = terminal_supported and terminal in {"cancelled", "error"}
        if delivery == "accepted" and not observed and _record_fragments(current):
            observed = _record_fragments(current)
            response_truncated = response_truncated or bool(current.get("response_truncated"))
            inspection = "observed"

        raw_completion = outcome.get("completion_status")
        completion = (
            raw_completion
            if isinstance(raw_completion, str) and raw_completion in COMPLETION_STATUSES
            else None
        )
        if completion is None:
            if delivery == "rejected":
                completion = COMPLETION_REJECTED
            elif inspection == "unknown":
                completion = COMPLETION_UNKNOWN
            elif inspection == "pending":
                completion = COMPLETION_PENDING
            elif outcome.get("tool_only") is True:
                completion = COMPLETION_TOOL_ONLY
            elif response_truncated:
                completion = COMPLETION_TRUNCATED
            elif observed:
                completion = COMPLETION_NONTERMINAL
            elif inspection == "observed":
                completion = COMPLETION_EMPTY
            else:
                completion = COMPLETION_UNKNOWN
        if delivery == "rejected":
            completion = COMPLETION_REJECTED
        elif remote_completed and delivery == "accepted" and inspection == "observed":
            if response_truncated:
                completion = COMPLETION_TRUNCATED
            elif not observed:
                completion = COMPLETION_EMPTY
            else:
                completion = COMPLETION_COMPLETED
        elif completion == COMPLETION_COMPLETED:
            # A textual "completed" field cannot replace an unsupported
            # remote terminal contract.
            completion = COMPLETION_NONTERMINAL if observed else COMPLETION_PENDING

        if remote_failed:
            state = STATE_FAILED
            completion = COMPLETION_CANCELLED if terminal == "cancelled" else COMPLETION_UNKNOWN
            observed = []
        elif delivery == "rejected":
            state = STATE_REJECTED
        elif remote_completed and delivery == "accepted" and inspection == "observed":
            if response_truncated:
                state = STATE_INCOMPLETE_RESPONSE
            elif not observed:
                state = STATE_EMPTY_RESPONSE
            else:
                state = STATE_REPLIED
        elif delivery == "accepted" and observed:
            state = STATE_REPLY_OBSERVED
        elif delivery == "accepted":
            state = STATE_SENT
        else:
            state = STATE_UNCERTAIN
        error_code = _safe_code(outcome.get("code"), "") or None
        if error_code is None and current.get("error_code") and delivery == "unknown":
            # A read-only reconciliation can report only that delivery is
            # still unknown.  Keep the fixed code from the original
            # admission failure instead of erasing useful durable evidence.
            error_code = str(current["error_code"])
        if state == STATE_INCOMPLETE_RESPONSE:
            error_code = "RESPONSE_TRUNCATED"
        elif state == STATE_EMPTY_RESPONSE:
            error_code = "EMPTY_FINAL_RESPONSE"
        diagnostics = _outcome_diagnostics(outcome)
        # Account availability is current, not evidence about an earlier reply.
        if observed and error_code == "GROK_USAGE_LIMIT_EXCEEDED":
            error_code = None
            for item in diagnostics.values():
                if isinstance(item, dict) and item.get("code") == "GROK_USAGE_LIMIT_EXCEEDED":
                    item.pop("code", None)
        overall = diagnostics.setdefault("overall", {})
        overall.update(
            {
                "delivery_status": delivery,
                "inspection_status": inspection,
                "completion_status": completion,
            }
        )
        fields: dict[str, Any] = {
            "delivery_status": delivery,
            "inspection_status": inspection,
            "completion_status": completion,
            "remote_terminal_status": terminal,
            "remote_terminal_supported": int(terminal_supported),
            "transport_diagnostics": _diagnostics_json(diagnostics),
            "error_code": error_code,
            "response_truncated": int(response_truncated),
        }
        correlation = _transport_correlation(outcome)
        if correlation is not None:
            previous = _safe_correlation(
                {
                    "request_message_id": current.get("request_message_id"),
                    "request_id": current.get("request_id"),
                    "root_id": current.get("root_id"),
                }
            )
            if previous is not None and previous != correlation:
                return self._set_state(
                    operation_id,
                    STATE_MISROUTED,
                    error_code="CORRELATION_MISMATCH",
                    reply_text="",
                )
            fields.update(correlation)
        if observed and not response_truncated:
            # Visible correlated text is only an observation. It becomes a
            # reply after the transport supplies an explicit completed
            # terminal marker and the text is non-empty and not truncated.
            fragments = _record_fragments(current)
            known = {_sha256(item) for item in fragments}
            for item in observed:
                digest = _sha256(item)
                if digest not in known:
                    fragments.append(item)
                    known.add(digest)
            fields["reply_fragments"] = json.dumps(fragments, ensure_ascii=False)
            fields["reply_text"] = FRAGMENT_SEPARATOR.join(fragments)
        fields["state"] = state
        return self.store.update_operation(operation_id, **fields)

    def _reconcile_once(self, record: dict[str, Any]) -> dict[str, Any]:
        try:
            outcome = self.transport.reconcile(record["operation_id"])
        except (TransportFailure, TransportUncertain) as error:
            if isinstance(error, TransportFailure) and error.code == "OPERATION_NOT_FOUND":
                # A missing journal row proves this operation never crossed
                # the native transport's admission boundary.
                return self._set_state(
                    record["operation_id"],
                    STATE_FAILED,
                    delivery_status="not_sent",
                    inspection_status="not_attempted",
                    completion_status=COMPLETION_NOT_STARTED,
                    error_code="OPERATION_NOT_ADMITTED",
                    transport_diagnostics=_diagnostics_json(
                        _exception_diagnostics("reconcile", error)
                    ),
                )
            fields = _transport_error_fields(error, COMPLETION_UNKNOWN)
            fields.update(
                {
                    "error_code": error.code,
                    "transport_diagnostics": _diagnostics_json(
                        _exception_diagnostics("reconcile", error)
                    ),
                }
            )
            return self.store.update_operation(record["operation_id"], **fields)
        except Exception:
            # A read-only inspection failure must leave the admitted turn
            # recoverable.  Never project an arbitrary exception message into
            # a receipt or let it turn a retry into a second send.
            diagnostics = _receipt_diagnostics(record)
            diagnostics["reconcile"] = {
                "stage": "transport",
                "code": "NATIVE_TRANSPORT_EXCEPTION",
            }
            return self._set_state(
                record["operation_id"],
                STATE_UNCERTAIN,
                completion_status=COMPLETION_UNKNOWN,
                error_code=record.get("error_code") or "NATIVE_TRANSPORT_EXCEPTION",
                transport_diagnostics=_diagnostics_json(
                    diagnostics
                ),
            )
        return self._apply_outcome(record, outcome)

    def _reconcile_open_once(
        self,
        record: dict[str, Any],
        cancel_event: threading.Event | None,
        *,
        include_observed: bool = False,
    ) -> dict[str, Any]:
        """Perform at most one foreground read after an admitted send.

        A Chat request is a send-and-return boundary.  The native transport
        itself performs one immediate inspection as part of ``execute``; this
        helper permits one additional read for a pending/uncertain receipt
        (and for an explicit replay), then leaves further observation to
        history recovery.  In particular, there is no sleep/deadline loop on
        the request thread.
        """

        operation_id = record["operation_id"]
        if _cancelled(cancel_event) and record["state"] in OPEN_STATES:
            record = self.store.cancel_if_open(operation_id)
            if record.get("state") == STATE_CANCELLED_LOCAL:
                record = self.store.update_operation(
                    operation_id,
                    completion_status=COMPLETION_CANCELLED,
                )
            return record

        states = {STATE_PENDING, STATE_SENT, STATE_UNCERTAIN}
        if include_observed:
            states.add(STATE_REPLY_OBSERVED)
        if record["state"] in states:
            fresh = self.store.get_operation(operation_id) or record
            if fresh["state"] in states:
                record = self._reconcile_once(fresh)

        if _cancelled(cancel_event) and record["state"] in OPEN_STATES:
            record = self.store.cancel_if_open(operation_id)
            if record.get("state") == STATE_CANCELLED_LOCAL:
                record = self.store.update_operation(
                    operation_id,
                    completion_status=COMPLETION_CANCELLED,
                )
        return record

    @staticmethod
    def _result(record: Mapping[str, Any]) -> GrokBotTurnResult:
        state = record["state"]
        return GrokBotTurnResult(
            operation_id=record["operation_id"],
            state=state,
            text=(record.get("reply_text") or "")
            if state in {STATE_REPLIED, STATE_REPLY_OBSERVED}
            else "",
            remote_terminal=record.get("remote_terminal_status") or REMOTE_TERMINAL,
            error_code=record.get("error_code"),
            response_truncated=bool(record.get("response_truncated")),
            remote_terminal_supported=bool(record.get("remote_terminal_supported")),
            transport_diagnostics=_receipt_diagnostics(record),
            completion_status=record.get("completion_status") or COMPLETION_UNKNOWN,
            correlation=_safe_correlation(
                {
                    "request_message_id": record.get("request_message_id"),
                    "request_id": record.get("request_id"),
                    "root_id": record.get("root_id"),
                }
            ),
        )

    # -- public API -------------------------------------------------------------

    def send_turn(
        self,
        owner_id: str,
        chat_id: str,
        turn_key: str,
        user_text: str,
        *,
        expected_generation: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> GrokBotTurnResult:
        binding = self._owner_binding(owner_id)
        if expected_generation is not None and expected_generation != binding.generation:
            raise GrokBotChatError(
                "STALE_GENERATION",
                "Grok Bot bindingが更新されました。もう一度送信してください。",
            )
        self._require_key(chat_id, "CHAT_SESSION_REQUIRED", "Chat ID")
        self._require_key(turn_key, "TURN_KEY_INVALID", "ターンID")
        if not isinstance(user_text, str) or not user_text.strip() or "\x00" in user_text:
            raise GrokBotChatError("PROMPT_INVALID", "送信するメッセージが空です。")
        self._check_chat_binding(self._owned_chat(owner_id, chat_id), binding)

        operation_id = build_operation_id(owner_id, chat_id, binding.generation, turn_key)
        existing = self.store.get_operation(operation_id)
        if existing is None and _cancelled(cancel_event):
            # Cancelled before admission: record it so a replay cannot send.
            return self._result(
                self._record_cancelled_before_send(owner_id, chat_id, turn_key, user_text, binding)
            )
        if existing is None:
            prompt = self._compose_prompt(chat_id, user_text)
            try:
                anchor = self.transport.anchor_target()
            except TransportFailure as error:
                raise GrokBotChatUnavailable(
                    "ANCHOR_UNAVAILABLE", f"Grok Bot の対象確認ができません ({error.code})。"
                ) from None
            if anchor != self.settings.target_identity:
                raise GrokBotChatUnavailable(
                    "TARGET_MISMATCH", "対象Grok Botがbindingと一致しません。送信していません。"
                )
            chat = self.store.ensure_chat(
                chat_id, owner_id, self.settings.target_identity, binding.generation
            )
            if chat["owner_id"] != owner_id:
                raise GrokBotChatError(
                    "CHAT_OWNER_MISMATCH", "このChatは別の所有者のGrok Bot会話です。"
                )
            self._check_chat_binding(chat, binding)
            with _INFLIGHT_LOCK:
                _INFLIGHT.add(operation_id)
            try:
                inserted = self.store.insert_operation(
                    {
                        "operation_id": operation_id,
                        "owner_id": owner_id,
                        "chat_id": chat_id,
                        "turn_key": turn_key,
                        "target_identity": self.settings.target_identity,
                        "generation": binding.generation,
                        "user_text": user_text,
                        "user_text_sha256": _sha256(user_text),
                        "prompt_sha256": _sha256(prompt),
                        "state": STATE_PENDING,
                        "completion_status": COMPLETION_PENDING,
                    }
                )
                if inserted:
                    if _cancelled(cancel_event):
                        # Cancelled during preflight: nothing has been sent.
                        return self._result(
                            self._set_state(
                                operation_id,
                                STATE_CANCELLED_LOCAL,
                                completion_status=COMPLETION_CANCELLED,
                                error_code=CANCELLED_BEFORE_SEND,
                            )
                        )
                    record = self._execute_new(operation_id, prompt)
                    return self._finish_turn(
                        self._reconcile_open_once(record, cancel_event)
                    )
            finally:
                with _INFLIGHT_LOCK:
                    _INFLIGHT.discard(operation_id)
            existing = self.store.get_operation(operation_id)
            if existing is None:
                raise GrokBotChatError("RECEIPT_WRITE_FAILED", "送信記録を保存できませんでした。")

        if existing["owner_id"] != owner_id or existing["chat_id"] != chat_id:
            raise GrokBotChatError("CHAT_OWNER_MISMATCH", "このChatは別の所有者のGrok Bot会話です。")
        if existing["user_text_sha256"] != _sha256(user_text):
            raise GrokBotChatError(
                "OPERATION_CONFLICT", "同じターンIDで異なる内容は送信できません。",
                operation_id=operation_id,
            )
        with _INFLIGHT_LOCK:
            inflight = operation_id in _INFLIGHT
        if existing["state"] not in OPEN_STATES or inflight:
            return self._result(existing)
        if _cancelled(cancel_event):
            record = self.store.cancel_if_open(operation_id)
            if record.get("state") == STATE_CANCELLED_LOCAL:
                record = self.store.update_operation(
                    operation_id,
                    completion_status=COMPLETION_CANCELLED,
                )
            return self._result(record)
        # Replay of an open turn: never resend; reconcile read-only.
        record, verified = self._verify_target(existing, after_send=False)
        if not verified:
            return self._result(record)
        with _INFLIGHT_LOCK:
            if operation_id in _INFLIGHT:
                return self._result(record)
            _INFLIGHT.add(operation_id)
        try:
            return self._finish_turn(
                self._reconcile_open_once(
                    record,
                    cancel_event,
                    include_observed=True,
                )
            )
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(operation_id)

    def _record_cancelled_before_send(
        self,
        owner_id: str,
        chat_id: str,
        turn_key: str,
        user_text: str,
        binding: GrokBotOwnerBinding,
    ) -> dict[str, Any]:
        operation_id = build_operation_id(owner_id, chat_id, binding.generation, turn_key)
        chat = self.store.ensure_chat(chat_id, owner_id, self.settings.target_identity, binding.generation)
        if chat["owner_id"] != owner_id:
            raise GrokBotChatError("CHAT_OWNER_MISMATCH", "このChatは別の所有者のGrok Bot会話です。")
        self._check_chat_binding(chat, binding)
        self.store.insert_operation(
            {
                "operation_id": operation_id,
                "owner_id": owner_id,
                "chat_id": chat_id,
                "turn_key": turn_key,
                "target_identity": self.settings.target_identity,
                "generation": binding.generation,
                "user_text": user_text,
                "user_text_sha256": _sha256(user_text),
                "prompt_sha256": "",
                "state": STATE_CANCELLED_LOCAL,
                "completion_status": COMPLETION_CANCELLED,
                "error_code": CANCELLED_BEFORE_SEND,
            }
        )
        record = self.store.get_operation(operation_id)
        if record is None:
            raise GrokBotChatError("RECEIPT_WRITE_FAILED", "送信記録を保存できませんでした。")
        if record["state"] in OPEN_STATES:
            record = self.store.cancel_if_open(operation_id)
        return record

    def _finish_turn(self, record: dict[str, Any]) -> GrokBotTurnResult:
        """Return the in-turn result; fragments it carries count as delivered."""

        if record["state"] in {STATE_REPLIED, STATE_REPLY_OBSERVED}:
            shas = [_sha256(item) for item in _record_fragments(record)]
            record = self.store.update_operation(
                record["operation_id"], delivered_fragments=json.dumps(shas)
            )
        return self._result(record)

    def _execute_new(self, operation_id: str, prompt: str) -> dict[str, Any]:
        record = self.store.get_operation(operation_id)
        assert record is not None
        if record["state"] != STATE_PENDING:
            # A local cancel landed between admission and send: never send.
            return record
        try:
            outcome = self.transport.execute(operation_id, prompt)
        except TransportUncertain as error:
            fields = _transport_error_fields(error, COMPLETION_UNKNOWN)
            fields.update(
                {
                    "error_code": error.code,
                    "transport_diagnostics": _diagnostics_json(
                        _exception_diagnostics("execute", error)
                    ),
                }
            )
            return self._set_state(
                operation_id,
                STATE_UNCERTAIN,
                **fields,
            )
        except TransportFailure as error:
            fields = {
                "completion_status": COMPLETION_NOT_STARTED,
                "error_code": error.code,
                "delivery_status": "not_sent",
                "inspection_status": "not_attempted",
                "transport_diagnostics": _diagnostics_json(
                    _exception_diagnostics("execute", error)
                ),
            }
            fields.update(_transport_correlation(getattr(error, "diagnostics", None)) or {})
            return self._set_state(
                operation_id,
                STATE_FAILED,
                **fields,
            )
        except Exception:
            return self._set_state(
                operation_id,
                STATE_UNCERTAIN,
                completion_status=COMPLETION_UNKNOWN,
                error_code="NATIVE_TRANSPORT_EXCEPTION",
                transport_diagnostics=_diagnostics_json(
                    {"execute": {"stage": "transport", "code": "NATIVE_TRANSPORT_EXCEPTION"}}
                ),
            )
        if not isinstance(outcome, Mapping) or outcome.get("operation_id") != operation_id:
            return self._set_state(operation_id, STATE_MISROUTED, error_code="OPERATION_ECHO_MISMATCH", reply_text=""
            )
        record, verified = self._verify_target(record, after_send=True)
        if not verified:
            return record
        return self._apply_outcome(record, outcome)

    def history(self, owner_id: str, chat_id: str) -> list[dict[str, Any]]:
        """Local receipts only; never touches the transport."""

        chat = self._owned_chat(owner_id, chat_id)
        if chat is None:
            return []
        return [
            _public_receipt(record)
            for record in self.store.list_operations(chat_id)
            if record["owner_id"] == owner_id
        ]

    def refresh(self, owner_id: str, chat_id: str, operation_id: str) -> dict[str, Any]:
        """Read-only reconciliation of one open receipt through the native transport."""

        record = self._owned_operation(owner_id, chat_id, operation_id)
        with _INFLIGHT_LOCK:
            inflight = operation_id in _INFLIGHT
        if record["state"] in OPEN_STATES | {STATE_REPLIED} and not inflight:
            record, verified = self._verify_target(record, after_send=False)
            if verified:
                record = self._reconcile_once(record)
        return _public_receipt(record)

    def cancel_local(self, owner_id: str, chat_id: str, operation_id: str) -> dict[str, Any]:
        """Stop waiting locally.  Grok Bot is not interrupted; nothing is resent."""

        self._owned_operation(owner_id, chat_id, operation_id)
        record = self.store.cancel_if_open(operation_id)
        if record.get("state") == STATE_CANCELLED_LOCAL:
            record = self.store.update_operation(
                operation_id,
                completion_status=COMPLETION_CANCELLED,
            )
        receipt = _public_receipt(record)
        receipt["cancelled_locally"] = record["state"] == STATE_CANCELLED_LOCAL
        return receipt

    # -- history recovery -------------------------------------------------------

    def _recovery_scope(self, owner_id: str, chat_id: str) -> GrokBotOwnerBinding | None:
        """Bound, non-stale owner Chat or ``None`` (fail closed, no transport)."""

        if not _valid_identifier(owner_id) or not isinstance(chat_id, str):
            return None
        if not _LOCAL_KEY_RE.fullmatch(chat_id):
            return None
        binding = self.settings.binding_for(owner_id)
        if binding is None:
            return None
        chat = self._owned_chat(owner_id, chat_id)
        if chat is None:
            return None
        try:
            self._check_chat_binding(chat, binding)
        except GrokBotChatError:
            return None
        return binding

    def recover_history(self, owner_id: str, chat_id: str) -> list[GrokBotRecoveredReply]:
        """Bounded read-only reconcile of this owner's recent turns in one Chat.

        Only the newest ``MAX_RECOVERY_OPERATIONS`` turns created within
        ``RECOVERY_WINDOW_SECONDS`` are reconciled, each at most once per
        ``RECOVERY_MIN_INTERVAL_SECONDS``.  Nothing is ever sent or resent.
        Returns reply fragments that the Chat history has not persisted yet.
        """

        binding = self._recovery_scope(owner_id, chat_id)
        if binding is None:
            return []
        now = self._wall_clock()
        candidates = [
            record
            for record in self.store.list_operations(chat_id)
            if record["owner_id"] == owner_id
            and record["state"] in RECOVERABLE_STATES
            and int(record["generation"]) == binding.generation
            and record["target_identity"] == self.settings.target_identity
            and now - float(record["created_at"]) <= RECOVERY_WINDOW_SECONDS
        ][-MAX_RECOVERY_OPERATIONS:]
        for record in candidates:
            last = record.get("last_reconcile_at")
            if last is not None and now - float(last) < RECOVERY_MIN_INTERVAL_SECONDS:
                continue
            operation_id = record["operation_id"]
            with _INFLIGHT_LOCK:
                if operation_id in _INFLIGHT or operation_id in _RECOVERING:
                    continue
                _RECOVERING.add(operation_id)
            try:
                record = self.store.update_operation(operation_id, last_reconcile_at=now)
                if record["state"] in OPEN_STATES:
                    record, verified = self._verify_target(record, after_send=False)
                    if not verified:
                        continue
                elif record["state"] != STATE_REPLIED:
                    continue
                self._reconcile_once(record)
            finally:
                with _INFLIGHT_LOCK:
                    _RECOVERING.discard(operation_id)
        return self.pending_deliveries(owner_id, chat_id)

    def pending_deliveries(self, owner_id: str, chat_id: str) -> list[GrokBotRecoveredReply]:
        """Return observed or terminal text fragments not yet in Chat history.

        Observed nonterminal text is delivered as progress. Its completion and
        remote-terminal metadata remain unchanged; it is not a remote success.
        """

        binding = self._recovery_scope(owner_id, chat_id)
        if binding is None:
            return []
        with _INFLIGHT_LOCK:
            inflight = set(_INFLIGHT)
        pending: list[GrokBotRecoveredReply] = []
        for record in self.store.list_operations(chat_id):
            if (
                record["owner_id"] != owner_id
                or record["state"] not in {STATE_REPLIED, STATE_REPLY_OBSERVED}
                or int(record["generation"]) != binding.generation
                or record["operation_id"] in inflight
            ):
                continue
            delivered = set(_json_list(record.get("delivered_fragments")))
            fresh = [item for item in _record_fragments(record) if _sha256(item) not in delivered]
            if not fresh:
                continue
            pending.append(
                GrokBotRecoveredReply(
                    operation_id=record["operation_id"],
                    turn_key=record["turn_key"],
                    fragments=tuple(fresh),
                    fragment_shas=tuple(_sha256(item) for item in fresh),
                    response_truncated=bool(record.get("response_truncated")),
                    remote_terminal=record.get("remote_terminal_status") or REMOTE_TERMINAL,
                    remote_terminal_supported=bool(record.get("remote_terminal_supported")),
                    completion_status=record.get("completion_status") or COMPLETION_UNKNOWN,
                )
            )
        return pending

    def mark_delivered(
        self, owner_id: str, chat_id: str, operation_id: str, fragment_shas: Any
    ) -> dict[str, Any]:
        """Record that these fragments are now persisted in the Chat history."""

        record = self._owned_operation(owner_id, chat_id, operation_id)
        known = {_sha256(item) for item in _record_fragments(record)}
        delivered = _json_list(record.get("delivered_fragments"))
        for digest in fragment_shas or ():
            if isinstance(digest, str) and digest in known and digest not in delivered:
                delivered.append(digest)
        return self.store.update_operation(operation_id, delivered_fragments=json.dumps(delivered))
