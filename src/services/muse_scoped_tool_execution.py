"""Durable, fail-closed boundary between a Muse turn and AoiTalk's tool router.

Muse itself stays a ``remote_agent_delegate`` provider with
``local_filesystem=False``: nothing in this module gives the remote model
local authority.  Local tool authority belongs to an owner-bound coordinator
that builds a :class:`MuseToolExecutionScope` from *current trusted server
authority* and routes every tool call through :class:`MuseScopedToolExecutor`.
No field of the scope may be accepted from assistant/model text.

This module is a thin wrapper around the EXISTING
``RegistryToolRouter.execute_async(UnifiedToolCall)``.  It implements no file,
shell, or command execution of its own and never bypasses router policy.

Public interface (semantic names are stable)
--------------------------------------------
``MuseToolExecutionScope(owner_id, session_id, run_id, workspace_root,
allowed_tools, authority_revision)``
    Frozen.  ``workspace_root`` must be an absolute, existing, canonical
    directory (equal to ``Path.resolve(strict=True)``) supplied by the trusted
    caller; nothing is discovered from the environment.  ``allowed_tools`` is
    a ``frozenset`` of registered tool names.

``MuseScopedToolExecutor(router, scope, journal_path, *, validate_authority,
validate_call, max_result_chars=16000, max_error_chars=2000)``
    ``router`` must expose ``execute_async``, a ``registry`` supporting ``in``,
    and ``enforce_tool_policy is True`` (an explicitly disabled policy router
    is rejected at construction and again before every dispatch).
    ``journal_path`` is a caller-supplied SQLite file whose parent directory
    already exists inside ``scope.workspace_root`` by default.  A trusted
    internal caller may instead supply ``expected_internal_journal_path``:
    only that exact canonical, link-free file is accepted outside the scope.
    This does not grant tool access to the file or its parent. Both hooks are REQUIRED:
    ``validate_authority(scope)`` and ``validate_call(scope, call)`` may be
    sync or async and must return exactly ``True``; any other value, a
    missing hook, or an exception fails closed.  ``validate_call`` is where
    the coordinator binds an exact file allowlist / fixed pytest argv; this
    wrapper does NOT prove path safety by itself.

``await executor.execute(call, *, turn_nonce, should_cancel, deadline)``
    ``call`` is a ``UnifiedToolCall`` with a non-empty, stable ``call_id``.
    ``turn_nonce`` is the already-correlated provider turn nonce.
    ``should_cancel`` is ``None`` or a zero-argument callable returning truthy
    to request cooperative cancellation.  ``deadline`` is ``None`` or an
    absolute ``time.monotonic()`` value.  Returns :class:`MuseToolReceipt`.

``MuseToolReceipt`` fields: ``status`` (:class:`MuseToolExecutionStatus`),
``operation_id`` (hex or ``""`` when rejected before identity existed),
``replayed`` (True only when a stored terminal receipt is returned),
``result`` (:class:`MuseToolResultProjection` or ``None``), ``reason``
(fixed machine code), and property ``accepted``.

Statuses
--------
``completed``  the router returned a ``UnifiedToolResult`` (its ``success``
               may still be False, e.g. a policy block).
``rejected``   refused before dispatch (validation, authority, allowlist).
``cancelled``  cooperative cancel or deadline before dispatch.
``conflict``   same operation identity with a different tool/arguments.
``fenced``     a prior admitted/started/uncertain receipt exists; never retried.
``uncertain``  dispatch was interrupted (exception, timeout, task
               cancellation); the side effect may or may not have happened.

Operation identity
------------------
``operation_id = sha256(canonical{owner_id, session_id, run_id,
workspace_root, turn_nonce, call_id})``.  The tool name plus canonical JSON
arguments form a separate ``intent_digest``; a stored receipt with the same
``operation_id`` and a different digest yields ``conflict`` and is never
dispatched.  ``authority_revision`` is recorded but is not part of the
identity, so a revision change cannot unlock re-execution.

Journal and fences
------------------
An ``admitted`` row is committed in a ``BEGIN IMMEDIATE`` transaction BEFORE
dispatch, so distinct callers (tasks, executors or processes) sharing a key
race atomically and at most one reaches the router.  The row moves
``admitted -> started -> completed|uncertain`` or ``admitted -> aborted``
(rejected/cancelled at the pre-dispatch recheck).  ``completed`` and
``aborted`` replay their stored projection without calling the router;
``admitted``, ``started`` and ``uncertain`` return ``fenced`` forever,
including after a reopen/crash.  Rows are never deleted or rewritten to
unlock a retry; recovery is an explicit operator decision outside this
module.  Only minimal fields are stored: identifiers, digests (never raw
arguments, nonce or call id), the bounded result projection and codes; no
prompts, credentials or tokens.

Cancellation
------------
Cancellation, deadline, current authority, allowlist/registration and
``validate_call`` are checked before admission and again immediately before
dispatch; failing either returns ``cancelled``/``rejected`` without calling
the router.  An awaitable hook result is bounded by the remaining
``deadline`` (``asyncio.wait_for``); expiry returns ``cancelled`` with reason
``validation_deadline_exceeded`` and never calls the router.  After both
hooks return, cancellation and the deadline are checked again, and once more
right before the router coroutine is created.  A pre-dispatch expiry or
cooperative cancel after admission moves the row to ``aborted``.  If the
calling task itself is cancelled (``asyncio.CancelledError``) while a
post-admission hook is awaited, no journal write is attempted: the row stays
``admitted`` and is fenced forever, and the error is re-raised.

Synchronous hooks are called inline on the event-loop thread; no worker
thread is ever started for validation, so none can be abandoned.  The
consequence is that a synchronous hook CANNOT be interrupted by the
deadline: it must be fast and non-blocking.  If it overruns, the
post-validation recheck still returns ``cancelled`` without dispatching.  An
awaitable hook that suppresses its own cancellation can likewise delay
``wait_for``; hooks must not do that.  Once dispatch starts, nothing is treated as proof that a side
effect stopped: a deadline expiry (``asyncio.wait_for``), an exception, or
``asyncio.CancelledError`` records ``uncertain`` (the CancelledError is
re-raised after the synchronous journal write).  A sync handler the router
runs in a worker thread may keep running after a timeout; the receipt stays
``uncertain`` and fenced, never ``cancelled``.  ``should_cancel`` is not
polled during dispatch.

Results
-------
The stored/returned output is the router's public ``model_output``
projection, capped at ``max_result_chars`` with ``output_truncated=True``
when cut.  ``accepted`` is True only for ``completed`` + ``success`` +
``executed`` + neither output nor error truncated; a truncated output or
error is never acceptance.  Such receipts keep their ``completed`` status and
bounded projection, so the failure stays visible (also on replay).

Nothing is opened at import time.  The journal is opened by explicit
executor construction and per ``execute`` call.  SQLite calls are short and
synchronous on the calling thread.  Stdlib only (Python 3.12, Windows-safe).
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import inspect
import json
import math
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:  # pragma: no cover - typing only, no import-time side effects
    from src.llm.unified_turn_runtime import UnifiedToolCall

__all__ = [
    "MuseScopedToolExecutor",
    "MuseToolExecutionScope",
    "MuseToolExecutionStatus",
    "MuseToolReceipt",
    "MuseToolResultProjection",
    "read_muse_tool_receipt_diagnostics",
]

SCHEMA_VERSION = 1
DEFAULT_MAX_RESULT_CHARS = 16000
DEFAULT_MAX_ERROR_CHARS = 2000
_MAX_ID_CHARS = 256
_SQLITE_TIMEOUT_SECONDS = 5.0
_MIN_PROCESS_RETURN_CODE = -(2**31)
_MAX_PROCESS_RETURN_CODE = (2**32) - 1

_STATE_ADMITTED = "admitted"
_STATE_STARTED = "started"
_STATE_COMPLETED = "completed"
_STATE_UNCERTAIN = "uncertain"
_STATE_ABORTED = "aborted"
_FENCE_STATES = frozenset({_STATE_ADMITTED, _STATE_STARTED, _STATE_UNCERTAIN})


class MuseToolExecutionStatus(str, enum.Enum):
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    CONFLICT = "conflict"
    FENCED = "fenced"
    UNCERTAIN = "uncertain"


def _require_identifier(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    if not value or value != value.strip() or len(value) > _MAX_ID_CHARS:
        raise ValueError(f"{name} must be a non-empty trimmed identifier")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def _valid_identifier(value: Any) -> bool:
    try:
        _require_identifier("value", value)
    except (TypeError, ValueError):
        return False
    return True


def _canonical_root(value: Any) -> Path:
    if not isinstance(value, Path):
        raise TypeError("workspace_root must be a pathlib.Path from trusted authority")
    if not value.is_absolute():
        raise ValueError("workspace_root must be absolute")
    resolved = value.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("workspace_root must be an existing directory")
    if os.path.normcase(str(resolved)) != os.path.normcase(str(value)):
        raise ValueError("workspace_root must already be canonical (resolved)")
    return resolved


@dataclass(frozen=True)
class MuseToolExecutionScope:
    """Immutable authority pin created by the trusted coordinator."""

    owner_id: str
    session_id: str
    run_id: str
    workspace_root: Path
    allowed_tools: frozenset[str]
    authority_revision: str

    def __post_init__(self) -> None:
        for name in ("owner_id", "session_id", "run_id", "authority_revision"):
            _require_identifier(name, getattr(self, name))
        object.__setattr__(self, "workspace_root", _canonical_root(self.workspace_root))
        if not isinstance(self.allowed_tools, frozenset):
            raise TypeError("allowed_tools must be a frozenset")
        for tool in self.allowed_tools:
            _require_identifier("allowed tool", tool)


@dataclass(frozen=True)
class MuseToolResultProjection:
    """Bounded, minimal projection of a router ``UnifiedToolResult``."""

    tool: str
    success: bool
    executed: bool
    output: str
    output_truncated: bool
    error: str = ""
    error_truncated: bool = False
    blocked_reason: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "tool": self.tool,
                "success": self.success,
                "executed": self.executed,
                "output": self.output,
                "output_truncated": self.output_truncated,
                "error": self.error,
                "error_truncated": self.error_truncated,
                "blocked_reason": self.blocked_reason,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> "MuseToolResultProjection":
        data = json.loads(raw)
        return cls(
            tool=str(data["tool"]),
            success=bool(data["success"]),
            executed=bool(data["executed"]),
            output=str(data["output"]),
            output_truncated=bool(data["output_truncated"]),
            error=str(data.get("error", "")),
            error_truncated=bool(data.get("error_truncated", False)),
            blocked_reason=str(data.get("blocked_reason", "")),
        )


@dataclass(frozen=True)
class MuseToolReceipt:
    status: MuseToolExecutionStatus
    operation_id: str
    replayed: bool = False
    result: MuseToolResultProjection | None = None
    reason: str = ""

    @property
    def accepted(self) -> bool:
        """True only for a complete, successful, executed, untruncated result.

        Output OR error truncation means the receipt is not proof of a full,
        clean result, so it is never acceptance.
        """

        r = self.result
        return bool(
            self.status is MuseToolExecutionStatus.COMPLETED
            and r is not None
            and r.success
            and r.executed
            and not r.output_truncated
            and not r.error_truncated
        )


def _bounded(text: Any, limit: int) -> tuple[str, bool]:
    value = text if isinstance(text, str) else str(text or "")
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _digest(payload: Mapping[str, Any]) -> str:
    rendered = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _command_execution_metadata(call: Any, result: Any) -> tuple[int | None, bool | None]:
    """Keep only the structured child exit facts from the server result."""

    if (
        getattr(call, "tool", None) != "execute_command"
        or getattr(result, "executed", None) is not True
        or getattr(getattr(result, "call", None), "tool", None) != call.tool
        or getattr(getattr(result, "call", None), "call_id", None) != call.call_id
    ):
        return None, None
    output = getattr(result, "output", None)
    if not isinstance(output, str):
        return None, None
    try:
        payload = json.loads(output)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, None
    if not isinstance(payload, Mapping):
        return None, None
    return_code = payload.get("return_code")
    safe_return_code = (
        return_code
        if isinstance(return_code, int) and not isinstance(return_code, bool)
        and _MIN_PROCESS_RETURN_CODE <= return_code <= _MAX_PROCESS_RETURN_CODE
        else None
    )
    timed_out = payload.get("timed_out")
    safe_timed_out = timed_out if isinstance(timed_out, bool) else None
    return safe_return_code, safe_timed_out


_HOOK_OK = "ok"
_HOOK_DENIED = "denied"
_HOOK_TIMEOUT = "timeout"


def _discard_awaitable(value: Any) -> None:
    """Drop an awaitable that will not be awaited, without running it."""

    if inspect.iscoroutine(value):
        value.close()
    elif isinstance(value, asyncio.Future):
        value.cancel()


async def _call_hook(hook: Callable[..., Any], deadline: float | None, *args: Any) -> str:
    """Run a validation hook; return ``ok``, ``denied`` or ``timeout``.

    Sync hooks run inline (no thread).  An awaitable result is bounded by the
    remaining deadline.  ``asyncio.CancelledError`` of the calling task
    propagates unchanged.
    """

    try:
        value = hook(*args)
    except Exception:
        return _HOOK_DENIED
    if inspect.isawaitable(value):
        if deadline is None:
            try:
                value = await value
            except Exception:
                return _HOOK_DENIED
        else:
            remaining = float(deadline) - time.monotonic()
            if remaining <= 0:
                _discard_awaitable(value)
                return _HOOK_TIMEOUT
            try:
                value = await asyncio.wait_for(value, timeout=remaining)
            except TimeoutError:
                return _HOOK_TIMEOUT
            except Exception:
                return _HOOK_DENIED
    return _HOOK_OK if value is True else _HOOK_DENIED


class MuseScopedToolExecutor:
    """Fail-closed, journaled execution of one scope's tool calls."""

    def __init__(
        self,
        router: Any,
        scope: MuseToolExecutionScope,
        journal_path: Path,
        *,
        validate_authority: Callable[[MuseToolExecutionScope], Any],
        validate_call: Callable[[MuseToolExecutionScope, Any], Any],
        expected_internal_journal_path: Path | None = None,
        max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
        max_error_chars: int = DEFAULT_MAX_ERROR_CHARS,
    ) -> None:
        if not isinstance(scope, MuseToolExecutionScope):
            raise TypeError("scope must be a MuseToolExecutionScope")
        if not callable(validate_authority) or not callable(validate_call):
            raise TypeError("validate_authority and validate_call are required callables")
        if not callable(getattr(router, "execute_async", None)):
            raise TypeError("router must expose execute_async")
        if getattr(router, "registry", None) is None:
            raise TypeError("router must expose its registry")
        if getattr(router, "enforce_tool_policy", None) is not True:
            raise ValueError("router tool policy must stay enabled")
        if not isinstance(max_result_chars, int) or max_result_chars < 1:
            raise ValueError("max_result_chars must be a positive int")
        if not isinstance(max_error_chars, int) or max_error_chars < 1:
            raise ValueError("max_error_chars must be a positive int")
        self._router = router
        self._scope = scope
        self._validate_authority = validate_authority
        self._validate_call = validate_call
        self._max_result_chars = max_result_chars
        self._max_error_chars = max_error_chars
        self._journal_path = self._check_journal_path(
            journal_path, scope.workspace_root, expected_internal_journal_path=expected_internal_journal_path
        )
        self._init_schema()

    @property
    def scope(self) -> MuseToolExecutionScope:
        return self._scope

    # ------------------------------------------------------------------ journal
    @staticmethod
    def _check_journal_path(
        journal_path: Any, root: Path, *, expected_internal_journal_path: Path | None = None
    ) -> Path:
        if not isinstance(journal_path, Path) or not journal_path.is_absolute():
            raise ValueError("journal_path must be an absolute pathlib.Path")
        if expected_internal_journal_path is not None:
            expected = expected_internal_journal_path
            if (not isinstance(expected, Path) or not expected.is_absolute()
                    or '..' in journal_path.parts or '..' in expected.parts
                    or journal_path != expected):
                raise ValueError("journal must match the exact internal path")
            # Do not resolve through links, junctions, or other Windows reparse points.
            for component in reversed((journal_path, *journal_path.parents)):
                try:
                    metadata = component.lstat()
                except FileNotFoundError:
                    if component != journal_path:
                        raise ValueError("journal parent directory must exist") from None
                    continue
                if component.is_symlink() or getattr(metadata, 'st_file_attributes', 0) & 0x400:
                    raise ValueError("internal journal path must not traverse a link or reparse point")
            parent = journal_path.parent.resolve(strict=True)
            if parent != journal_path.parent or not parent.is_dir():
                raise ValueError("internal journal parent must be canonical")
            if journal_path.exists() and not journal_path.is_file():
                raise ValueError("journal_path must be a regular file")
            return parent / journal_path.name
        parent = journal_path.parent.resolve(strict=True)
        if not parent.is_dir():
            raise ValueError("journal parent directory must exist")
        try:
            parent.relative_to(root)
        except ValueError as exc:
            raise ValueError("journal must live inside the trusted workspace root") from exc
        if journal_path.is_symlink():
            raise ValueError("journal_path must not be a symlink")
        if journal_path.exists() and not journal_path.is_file():
            raise ValueError("journal_path must be a regular file")
        return parent / journal_path.name

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._journal_path), timeout=_SQLITE_TIMEOUT_SECONDS, isolation_level=None
        )
        conn.execute(f"PRAGMA busy_timeout = {int(_SQLITE_TIMEOUT_SECONDS * 1000)}")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS muse_tool_receipts (
                        operation_id TEXT PRIMARY KEY,
                        schema_version INTEGER NOT NULL,
                        owner_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        tool TEXT NOT NULL,
                        intent_digest TEXT NOT NULL,
                        authority_revision TEXT NOT NULL,
                        state TEXT NOT NULL,
                        status TEXT NOT NULL,
                        claim_token TEXT NOT NULL,
                        reason TEXT NOT NULL DEFAULT '',
                        result_json TEXT,
                        call_id_sha256 TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        started_at REAL,
                        command_return_code INTEGER,
                        command_timed_out INTEGER
                    )
                    """
                )
                columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(muse_tool_receipts)").fetchall()
                }
                for name, column_type in (
                    ("call_id_sha256", "TEXT"),
                    ("started_at", "REAL"),
                    ("command_return_code", "INTEGER"),
                    ("command_timed_out", "INTEGER"),
                ):
                    if name not in columns:
                        conn.execute(
                            f"ALTER TABLE muse_tool_receipts ADD COLUMN {name} {column_type}"
                        )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def _admit(
        self,
        operation_id: str,
        tool: str,
        intent_digest: str,
        call_id_sha256: str,
        token: str,
    ) -> tuple[str, sqlite3.Row | None]:
        """Atomically admit a new key; return ("admitted", None) or the prior row."""

        conn = self._connect()
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM muse_tool_receipts WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if row is not None:
                    conn.execute("COMMIT")
                    return "existing", row
                now = time.time()
                conn.execute(
                    """
                    INSERT INTO muse_tool_receipts (
                        operation_id, schema_version, owner_id, session_id, run_id,
                        tool, intent_digest, authority_revision, state, status,
                        claim_token, reason, result_json, call_id_sha256, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', NULL, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        SCHEMA_VERSION,
                        self._scope.owner_id,
                        self._scope.session_id,
                        self._scope.run_id,
                        tool,
                        intent_digest,
                        self._scope.authority_revision,
                        _STATE_ADMITTED,
                        "pending",
                        token,
                        call_id_sha256,
                        now,
                        now,
                    ),
                )
                conn.execute("COMMIT")
                return "admitted", None
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def _transition(
        self,
        operation_id: str,
        token: str,
        *,
        from_state: str,
        to_state: str,
        status: str,
        reason: str = "",
        result_json: str | None = None,
        command_return_code: int | None = None,
        command_timed_out: bool | None = None,
    ) -> bool:
        conn = self._connect()
        try:
            now = time.time()
            cur = conn.execute(
                """
                UPDATE muse_tool_receipts
                   SET state = ?, status = ?, reason = ?, result_json = ?, updated_at = ?,
                       started_at = CASE
                           WHEN ? = ? AND started_at IS NULL THEN ?
                           ELSE started_at
                       END,
                       command_return_code = COALESCE(?, command_return_code),
                       command_timed_out = COALESCE(?, command_timed_out)
                 WHERE operation_id = ? AND claim_token = ? AND state = ?
                """,
                (
                    to_state,
                    status,
                    reason,
                    result_json,
                    now,
                    to_state,
                    _STATE_STARTED,
                    now,
                    command_return_code,
                    None if command_timed_out is None else int(command_timed_out),
                    operation_id,
                    token,
                    from_state,
                ),
            )
            return cur.rowcount == 1
        finally:
            conn.close()

    def _record_terminal(
        self,
        operation_id: str,
        token: str,
        *,
        to_state: str,
        status: str,
        reason: str,
        result_json: str | None,
        command_return_code: int | None = None,
        command_timed_out: bool | None = None,
    ) -> bool:
        return self._transition(
            operation_id,
            token,
            from_state=_STATE_STARTED,
            to_state=to_state,
            status=status,
            reason=reason,
            result_json=result_json,
            command_return_code=command_return_code,
            command_timed_out=command_timed_out,
        )

    # ----------------------------------------------------------------- checks
    @staticmethod
    def _interrupted(should_cancel: Any, deadline: float | None) -> tuple[MuseToolExecutionStatus, str] | None:
        if should_cancel is not None:
            try:
                if should_cancel():
                    return MuseToolExecutionStatus.CANCELLED, "cancel_requested"
            except Exception:
                return MuseToolExecutionStatus.CANCELLED, "cancel_check_failed"
        if deadline is not None and time.monotonic() >= deadline:
            return MuseToolExecutionStatus.CANCELLED, "deadline_exceeded"
        return None

    async def _gate(self, call: Any, should_cancel: Any, deadline: float | None) -> tuple[MuseToolExecutionStatus, str] | None:
        interrupted = self._interrupted(should_cancel, deadline)
        if interrupted is not None:
            return interrupted
        if getattr(self._router, "enforce_tool_policy", None) is not True:
            return MuseToolExecutionStatus.REJECTED, "router_policy_disabled"
        if call.tool not in self._scope.allowed_tools:
            return MuseToolExecutionStatus.REJECTED, "tool_not_allowed"
        try:
            registered = call.tool in self._router.registry
        except Exception:
            registered = False
        if not registered:
            return MuseToolExecutionStatus.REJECTED, "tool_not_registered"
        authority = await _call_hook(self._validate_authority, deadline, self._scope)
        if authority == _HOOK_TIMEOUT:
            return MuseToolExecutionStatus.CANCELLED, "validation_deadline_exceeded"
        if authority != _HOOK_OK:
            return MuseToolExecutionStatus.REJECTED, "authority_denied"
        checked = await _call_hook(self._validate_call, deadline, self._scope, call)
        if checked == _HOOK_TIMEOUT:
            return MuseToolExecutionStatus.CANCELLED, "validation_deadline_exceeded"
        if checked != _HOOK_OK:
            return MuseToolExecutionStatus.REJECTED, "call_denied"
        # Hooks may have taken time (or a sync hook overran): recheck.
        return self._interrupted(should_cancel, deadline)

    def _identity(self, call: Any, turn_nonce: str) -> tuple[str, str, str]:
        call_id_sha256 = hashlib.sha256(call.call_id.encode("utf-8")).hexdigest()
        operation_id = _digest(
            {
                "v": SCHEMA_VERSION,
                "owner_id": self._scope.owner_id,
                "session_id": self._scope.session_id,
                "run_id": self._scope.run_id,
                "workspace_root": str(self._scope.workspace_root),
                "turn_nonce": turn_nonce,
                "call_id": call.call_id,
            }
        )
        intent_digest = _digest({"tool": call.tool, "arguments": dict(call.arguments)})
        return operation_id, intent_digest, call_id_sha256

    def _project(self, call: Any, result: Any) -> MuseToolResultProjection:
        try:
            output = result.model_output
        except Exception:
            output = str(getattr(result, "output", "") or "")
        output_text, output_cut = _bounded(output, self._max_result_chars)
        error_text, error_cut = _bounded(getattr(result, "error", ""), self._max_error_chars)
        blocked, _ = _bounded(getattr(result, "blocked_reason", ""), self._max_error_chars)
        return MuseToolResultProjection(
            tool=call.tool,
            success=bool(result.success),
            executed=bool(getattr(result, "executed", True)),
            output=output_text,
            output_truncated=output_cut,
            error=error_text,
            error_truncated=error_cut,
            blocked_reason=blocked,
        )

    def _replay(self, row: sqlite3.Row, operation_id: str, intent_digest: str) -> MuseToolReceipt:
        if row["intent_digest"] != intent_digest:
            return MuseToolReceipt(MuseToolExecutionStatus.CONFLICT, operation_id, reason="intent_mismatch")
        state = row["state"]
        if state in _FENCE_STATES:
            return MuseToolReceipt(MuseToolExecutionStatus.FENCED, operation_id, reason=f"prior_{state}")
        if state in (_STATE_COMPLETED, _STATE_ABORTED):
            result = None
            if row["result_json"]:
                try:
                    result = MuseToolResultProjection.from_json(row["result_json"])
                except Exception:
                    return MuseToolReceipt(MuseToolExecutionStatus.FENCED, operation_id, reason="receipt_unreadable")
            try:
                status = MuseToolExecutionStatus(row["status"])
            except ValueError:
                return MuseToolReceipt(MuseToolExecutionStatus.FENCED, operation_id, reason="receipt_unreadable")
            return MuseToolReceipt(status, operation_id, replayed=True, result=result, reason=row["reason"])
        return MuseToolReceipt(MuseToolExecutionStatus.FENCED, operation_id, reason="unknown_state")

    # ---------------------------------------------------------------- execute
    async def execute(
        self,
        call: "UnifiedToolCall",
        *,
        turn_nonce: str,
        should_cancel: Callable[[], Any] | None,
        deadline: float | None,
    ) -> MuseToolReceipt:
        from src.llm.unified_turn_runtime import UnifiedToolCall, UnifiedToolResult

        rejected = MuseToolExecutionStatus.REJECTED
        if not isinstance(call, UnifiedToolCall):
            return MuseToolReceipt(rejected, "", reason="invalid_call")
        if not _valid_identifier(call.call_id):
            return MuseToolReceipt(rejected, "", reason="invalid_call_id")
        if not _valid_identifier(turn_nonce):
            return MuseToolReceipt(rejected, "", reason="invalid_turn_nonce")
        if not _valid_identifier(call.tool):
            return MuseToolReceipt(rejected, "", reason="invalid_tool")
        if not isinstance(call.arguments, Mapping):
            return MuseToolReceipt(rejected, "", reason="invalid_arguments")
        if deadline is not None and not isinstance(deadline, (int, float)):
            return MuseToolReceipt(rejected, "", reason="invalid_deadline")
        if should_cancel is not None and not callable(should_cancel):
            return MuseToolReceipt(rejected, "", reason="invalid_should_cancel")
        try:
            operation_id, intent_digest, call_id_sha256 = self._identity(call, turn_nonce)
        except (TypeError, ValueError):
            return MuseToolReceipt(rejected, "", reason="arguments_not_canonical_json")

        gate = await self._gate(call, should_cancel, deadline)
        if gate is not None:
            return MuseToolReceipt(gate[0], operation_id, reason=gate[1])

        token = uuid.uuid4().hex
        outcome, row = self._admit(
            operation_id,
            call.tool,
            intent_digest,
            call_id_sha256,
            token,
        )
        if outcome != "admitted":
            assert row is not None
            return self._replay(row, operation_id, intent_digest)

        # A CancelledError raised here leaves the row ``admitted`` (fenced).
        gate = await self._gate(call, should_cancel, deadline)
        if gate is not None:
            self._transition(
                operation_id, token,
                from_state=_STATE_ADMITTED, to_state=_STATE_ABORTED,
                status=gate[0].value, reason=gate[1],
            )
            return MuseToolReceipt(gate[0], operation_id, reason=gate[1])
        if not self._transition(
            operation_id, token,
            from_state=_STATE_ADMITTED, to_state=_STATE_STARTED, status="started",
        ):
            return MuseToolReceipt(MuseToolExecutionStatus.FENCED, operation_id, reason="claim_lost")

        # Final check before the router coroutine exists: nothing has been
        # dispatched yet, so an expiry here is a certain ``aborted``.
        late = self._interrupted(should_cancel, deadline)
        if late is not None:
            self._transition(
                operation_id, token,
                from_state=_STATE_STARTED, to_state=_STATE_ABORTED,
                status=late[0].value, reason=late[1],
            )
            return MuseToolReceipt(late[0], operation_id, reason=late[1])

        uncertain = MuseToolExecutionStatus.UNCERTAIN
        try:
            dispatch = self._router.execute_async(call)
            if deadline is not None:
                remaining = max(0.0, float(deadline) - time.monotonic())
                result = await asyncio.wait_for(dispatch, timeout=remaining)
            else:
                result = await dispatch
        except asyncio.CancelledError:
            self._record_terminal(
                operation_id, token, to_state=_STATE_UNCERTAIN,
                status=uncertain.value, reason="dispatch_cancelled", result_json=None,
            )
            raise
        except TimeoutError:
            self._record_terminal(
                operation_id, token, to_state=_STATE_UNCERTAIN,
                status=uncertain.value, reason="dispatch_timeout", result_json=None,
            )
            return MuseToolReceipt(uncertain, operation_id, reason="dispatch_timeout")
        except Exception:
            self._record_terminal(
                operation_id, token, to_state=_STATE_UNCERTAIN,
                status=uncertain.value, reason="dispatch_error", result_json=None,
            )
            return MuseToolReceipt(uncertain, operation_id, reason="dispatch_error")

        if not isinstance(result, UnifiedToolResult):
            self._record_terminal(
                operation_id, token, to_state=_STATE_UNCERTAIN,
                status=uncertain.value, reason="invalid_router_result", result_json=None,
            )
            return MuseToolReceipt(uncertain, operation_id, reason="invalid_router_result")
        try:
            projection = self._project(call, result)
            result_json = projection.to_json()
            command_return_code, command_timed_out = _command_execution_metadata(
                call,
                result,
            )
        except Exception:
            self._record_terminal(
                operation_id, token, to_state=_STATE_UNCERTAIN,
                status=uncertain.value, reason="result_projection_failed", result_json=None,
            )
            return MuseToolReceipt(uncertain, operation_id, reason="result_projection_failed")
        completed = MuseToolExecutionStatus.COMPLETED
        if not self._record_terminal(
            operation_id, token, to_state=_STATE_COMPLETED,
            status=completed.value,
            reason="",
            result_json=result_json,
            command_return_code=command_return_code,
            command_timed_out=command_timed_out,
        ):
            return MuseToolReceipt(uncertain, operation_id, reason="receipt_write_lost")
        return MuseToolReceipt(completed, operation_id, result=projection)


def read_muse_tool_receipt_diagnostics(
    journal_path: Path,
    scope: MuseToolExecutionScope,
    *,
    limit: int = 32,
    expected_internal_journal_path: Path | None = None,
) -> dict[str, Any]:
    """Read bounded, owner/run-scoped receipt facts without changing the journal.

    Provider arguments and command output are deliberately excluded.  Legacy
    journals may not contain ``started_at`` or the structured command fields;
    those values stay null rather than being inferred from wrapper success.
    """

    if not isinstance(scope, MuseToolExecutionScope):
        raise TypeError("scope must be a MuseToolExecutionScope")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 32:
        raise ValueError("limit must be between 1 and 32")
    path = MuseScopedToolExecutor._check_journal_path(
        journal_path, scope.workspace_root, expected_internal_journal_path=expected_internal_journal_path
    )
    if not path.is_file() or path.is_symlink():
        return {"status": "journal_missing", "receipts": [], "truncated": False}

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            path.as_uri() + "?mode=ro",
            uri=True,
            timeout=_SQLITE_TIMEOUT_SECONDS,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(_SQLITE_TIMEOUT_SECONDS * 1000)}")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'muse_tool_receipts'"
        ).fetchone()
        if table is None:
            return {"status": "schema_missing", "receipts": [], "truncated": False}
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(muse_tool_receipts)").fetchall()
        }
        selected = [
            "operation_id",
            "tool",
            "state",
            "status",
            "result_json",
            "created_at",
            "updated_at",
        ]
        selected.extend(
            name for name in (
                "call_id_sha256",
                "started_at",
                "command_return_code",
                "command_timed_out",
            )
            if name in columns
        )
        rows = connection.execute(
            f"SELECT {', '.join(selected)} FROM muse_tool_receipts "
            "WHERE owner_id = ? AND session_id = ? AND run_id = ? AND authority_revision = ? "
            "ORDER BY created_at, operation_id LIMIT ?",
            (
                scope.owner_id,
                scope.session_id,
                scope.run_id,
                scope.authority_revision,
                limit + 1,
            ),
        ).fetchall()
    except (OSError, sqlite3.Error, ValueError):
        return {"status": "journal_unavailable", "receipts": [], "truncated": False}
    finally:
        if connection is not None:
            connection.close()

    safe_rows: list[dict[str, Any]] = []
    for row in rows[:limit]:
        operation_id = row["operation_id"]
        tool = row["tool"]
        if not isinstance(operation_id, str) or not re.fullmatch(r"[a-f0-9]{64}", operation_id):
            continue
        if not isinstance(tool, str) or tool not in scope.allowed_tools:
            safe_tool = None
        else:
            safe_tool = tool
        state = row["state"] if row["state"] in {
            _STATE_ADMITTED, _STATE_STARTED, _STATE_COMPLETED, _STATE_UNCERTAIN, _STATE_ABORTED
        } else "unknown"
        status = row["status"]
        if status not in {item.value for item in MuseToolExecutionStatus} | {"pending", "started"}:
            status = "unknown"
        result: dict[str, Any] = {}
        raw_result = row["result_json"]
        parsed: Mapping[str, Any] | None = None
        if isinstance(raw_result, str) and len(raw_result) <= DEFAULT_MAX_RESULT_CHARS + DEFAULT_MAX_ERROR_CHARS + 1024:
            try:
                parsed_value = json.loads(raw_result)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_value = None
            if isinstance(parsed_value, Mapping):
                parsed = parsed_value
                for key in ("success", "executed", "output_truncated", "error_truncated"):
                    value = parsed.get(key)
                    if isinstance(value, bool):
                        result[key] = value
                raw_blocked_reason = parsed.get("blocked_reason", "")
                if isinstance(raw_blocked_reason, str):
                    try:
                        from .outbound_privacy_service import redact_secret_for_local_display

                        safe_blocked_reason = str(
                            redact_secret_for_local_display(raw_blocked_reason)
                        )
                    except Exception:
                        safe_blocked_reason = ""
                    result["blocked_reason"] = safe_blocked_reason[:300]

        return_code = row["command_return_code"] if "command_return_code" in columns else None
        if (
            isinstance(return_code, bool)
            or not isinstance(return_code, int)
            or not _MIN_PROCESS_RETURN_CODE <= return_code <= _MAX_PROCESS_RETURN_CODE
        ):
            return_code = None
        timed_out = row["command_timed_out"] if "command_timed_out" in columns else None
        if isinstance(timed_out, int) and not isinstance(timed_out, bool):
            timed_out = bool(timed_out)
        if not isinstance(timed_out, bool):
            timed_out = None
        if (
            safe_tool == "execute_command"
            and return_code is None
            and parsed is not None
            and parsed.get("output_truncated") is False
            and isinstance(parsed.get("output"), str)
        ):
            try:
                command_result = json.loads(parsed["output"])
            except (TypeError, ValueError, json.JSONDecodeError):
                command_result = None
            if isinstance(command_result, Mapping):
                legacy_code = command_result.get("return_code")
                if (
                    isinstance(legacy_code, int)
                    and not isinstance(legacy_code, bool)
                    and _MIN_PROCESS_RETURN_CODE <= legacy_code <= _MAX_PROCESS_RETURN_CODE
                    and isinstance(command_result.get("success"), bool)
                ):
                    return_code = legacy_code
                    legacy_timeout = command_result.get("timed_out")
                    timed_out = legacy_timeout if isinstance(legacy_timeout, bool) else None
        safe_rows.append(
            {
                "operation_id": operation_id,
                "call_id_sha256": (
                    row["call_id_sha256"]
                    if "call_id_sha256" in columns
                    and isinstance(row["call_id_sha256"], str)
                    and re.fullmatch(r"[a-f0-9]{64}", row["call_id_sha256"])
                    else None
                ),
                "tool": safe_tool,
                "state": state,
                "status": status,
                "admitted_at": _finite_timestamp(row["created_at"]),
                "started_at": _finite_timestamp(row["started_at"])
                if "started_at" in columns
                else None,
                "operation_updated_at": _finite_timestamp(row["updated_at"]),
                "command_return_code": return_code if safe_tool == "execute_command" else None,
                "command_timed_out": timed_out if safe_tool == "execute_command" else None,
                "result_present": isinstance(raw_result, str),
                **result,
            }
        )
    return {
        "status": "available",
        "receipts": safe_rows,
        "truncated": len(rows) > limit,
    }


def _finite_timestamp(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None
