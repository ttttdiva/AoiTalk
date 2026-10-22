"""Deployment-owned Codex worker bridge for Operations development jobs.

The ordinary local task runner already owns the dynamic tool loop and the
``ToolGuard`` boundary.  This module only binds the authenticated, official
Codex completion client to that runner for the canonical CrowdWorks
``code_worker`` phase.  It deliberately has no public approval operation: a
deployment policy is an operator supplied, read-only JSON document and the
default is disabled.

The policy records the host instruction snapshot and the exact CLI identity
that a human approved.  At every run the current snapshot and executable are
checked again, then a short lived :class:`ReviewedHostInstructions` receipt is
bound to the task fingerprint, owner and service boot.  A changed source or
CLI therefore fails before a model turn or guarded tool call.
"""

from __future__ import annotations

import json
import math
import os
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..llm import codex_instruction_review as _instruction_review
from ..llm.codex_instruction_review import ReviewedHostInstructions, review_digest, snapshot_metadata
from .codex_task_preflight import CodexManagedConfigPreflight
from .local_agent_tasks import LocalAgentTaskLimits, LocalAgentTaskRunner, LocalAgentTaskSpec


POLICY_SCHEMA = "aoitalk-codex-native-worker-policy/v1"
POLICY_ENV = "AOITALK_CODEX_NATIVE_WORKER_POLICY_FILE"
POLICY_ENABLED_ENV = "AOITALK_CODEX_NATIVE_WORKER_ENABLED"
POLICY_OWNER_ENV = "AOITALK_CODEX_NATIVE_WORKER_OWNER_ID"
POLICY_DIGEST_ENV = "AOITALK_CODEX_NATIVE_WORKER_INSTRUCTION_DIGEST"
POLICY_CLI_PATH_ENV = "AOITALK_CODEX_NATIVE_WORKER_CLI_PATH"
POLICY_CLI_VERSION_ENV = "AOITALK_CODEX_NATIVE_WORKER_CLI_VERSION"
POLICY_MAX_BYTES = 64 * 1024
POLICY_RECEIPT_TTL_SECONDS = 300
_HEX64 = "0123456789abcdef"
_DEPLOYMENT_POLICY_SEAL = object()

_NATIVE_CODE_WORKER_CONSTRAINTS = (
    "Delegation authority: the parent/Operations agent explicitly assigned this task to you as the code_worker implementation owner for the assigned customer workspace and files. Complete the implementation yourself with the declared workspace tools; a read-only diagnosis alone does not satisfy this task.",
    "The human-adopted global AGENTS.md remains authoritative and must not be edited, removed, or ignored. Its existing delegation rules explicitly permit this assigned code_worker to implement the requested workspace changes; the parent-editing rule governs orchestration outside this assigned implementation scope.",
    "Ownership and success: make the required file changes inside the assigned workspace, run the relevant tests/builds when available, and report the actual changed files and verification results. Do not stop after inspection when the goal requires implementation.",
    "Security boundary: keep all reads, writes, and commands inside the issued workspace scope; do not access host/private files, secrets, external services, network resources, customer production systems, or push/publish externally.",
)

_NATIVE_TELEMETRY_EVENT_LIMIT = 256
_NATIVE_TELEMETRY_UPDATE_LIMIT = 64
_NATIVE_TELEMETRY_COMMAND_LIMIT = 200
_NATIVE_TELEMETRY_CHECK_LIMIT = 80
_NATIVE_TELEMETRY_FILE_LIMIT = 500
_NATIVE_TELEMETRY_TEXT_LIMIT = 800
_NATIVE_TELEMETRY_PROVENANCE_KEYS = (
    "opportunity_id", "project_id", "work_item_id", "agent_run_id",
    "agent_id", "agent_revision_id", "source_revision", "owner_id",
)
_NATIVE_USAGE_FIELDS = (
    "input_tokens", "output_tokens", "total_tokens", "cached_tokens",
    "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
    "prompt_eval_tokens",
)


def _native_telemetry_redactor():
    from .local_agent_tasks import SecretRedactor

    return SecretRedactor()


def _native_telemetry_text(value: Any, *, limit: int = _NATIVE_TELEMETRY_TEXT_LIMIT, redactor=None) -> str:
    if not isinstance(value, str):
        return ""
    text = value[:limit]
    if not text:
        return ""
    return (redactor or _native_telemetry_redactor()).text(text, limit)


def _native_telemetry_scalar(value: Any, *, redactor=None) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return max(-1_000_000, min(value, 1_000_000))
    if isinstance(value, float) and math.isfinite(value):
        return max(-1_000_000.0, min(value, 1_000_000.0))
    if isinstance(value, str):
        return _native_telemetry_text(value, redactor=redactor)
    return None


def _native_telemetry_provenance(value: Any, *, redactor=None) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, str] = {}
    for key in _NATIVE_TELEMETRY_PROVENANCE_KEYS:
        text = _native_telemetry_text(value.get(key), limit=160, redactor=redactor)
        if text:
            result[key] = text
    return result


def _native_telemetry_payload(kind: str, value: Any, *, redactor=None) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    allowed: dict[str, tuple[str, ...]] = {
        "command_start": ("command", "cwd", "background", "timeout_sec", "tool_call_id"),
        "command_end": ("command", "exit_code", "success", "timed_out", "duration_sec", "tool_call_id", "job_id", "stdout", "stderr"),
        "command_background": ("job_id", "tool_call_id"),
        "tool_start": ("tool", "operation_id", "message"),
        "tool_end": ("tool", "operation_id", "message"),
        "file_changed": ("path", "operation"),
        "tool_blocked": ("tool", "reason"),
        "llm_response": ("call", "tool_calls"),
        "assistant_note": ("text",),
        "status": ("status", "reason"),
    }
    keys = allowed.get(kind, ())
    result: dict[str, Any] = {}
    for key in keys:
        if key not in raw:
            continue
        projected = _native_telemetry_scalar(raw.get(key), redactor=redactor)
        if projected is not None:
            result[key] = projected
    return result


def _native_telemetry_commands(value: Any, *, redactor=None) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in value[:_NATIVE_TELEMETRY_COMMAND_LIMIT]:
        if not isinstance(raw, Mapping):
            continue
        item: dict[str, Any] = {}
        for key in ("command", "exit_code", "success", "timed_out", "duration_sec", "stdout", "stderr"):
            if key not in raw:
                continue
            projected = _native_telemetry_scalar(raw.get(key), redactor=redactor)
            if projected is not None:
                item[key] = projected
        if item:
            result.append(item)
    return result


def _native_telemetry_checks(value: Any, *, redactor=None) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, raw in list(value.items())[:_NATIVE_TELEMETRY_CHECK_LIMIT]:
            name = _native_telemetry_text(str(key), limit=120, redactor=redactor)
            projected = _native_telemetry_scalar(raw, redactor=redactor)
            if name and projected is not None:
                result[name] = projected
        return result
    if isinstance(value, list):
        result = []
        for raw in value[:_NATIVE_TELEMETRY_CHECK_LIMIT]:
            if isinstance(raw, Mapping):
                item = {}
                for key in ("id", "name", "label", "status", "required", "detail", "command"):
                    if key not in raw:
                        continue
                    projected = _native_telemetry_scalar(raw.get(key), redactor=redactor)
                    if projected is not None:
                        item[key] = projected
                if item:
                    result.append(item)
        return result
    return {}


def _native_telemetry_files(value: Any, *, redactor=None) -> list[Any]:
    if not isinstance(value, list):
        return []
    result: list[Any] = []
    for raw in value[:_NATIVE_TELEMETRY_FILE_LIMIT]:
        if isinstance(raw, str):
            text = _native_telemetry_text(raw, limit=400, redactor=redactor)
            if text:
                result.append(text)
            continue
        if not isinstance(raw, Mapping):
            continue
        item = {}
        for key in ("path", "file", "name", "status", "operation", "change", "detail"):
            projected = _native_telemetry_scalar(raw.get(key), redactor=redactor)
            if projected is not None:
                item[key] = projected
        if item:
            result.append(item)
    return result


def _native_telemetry_usage(value: Any, *, redactor=None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"status": "unknown", "reason": "provider_usage_not_returned"}
    result: dict[str, Any] = {}
    for key in _NATIVE_USAGE_FIELDS:
        raw = value.get(key)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            result[key] = min(raw, 2_000_000_000)
    for key in ("provider", "model", "metrics_source", "owner_id"):
        text = _native_telemetry_text(value.get(key), limit=160, redactor=redactor)
        if text:
            result[key] = text
    result["status"] = "reported" if any(key in result for key in _NATIVE_USAGE_FIELDS) else "unknown"
    if result["status"] == "unknown":
        result["reason"] = "provider_usage_not_returned"
    return result


def project_native_worker_telemetry(value: Any) -> dict[str, Any]:
    """Return a bounded, redacted projection safe for AgentRun evidence."""

    if not isinstance(value, Mapping):
        return {}
    redactor = _native_telemetry_redactor()
    result: dict[str, Any] = {
        "schema_version": 1,
        "provenance": _native_telemetry_provenance(value.get("provenance"), redactor=redactor),
        "events": [],
        "updates": [],
        "commands": _native_telemetry_commands(value.get("commands"), redactor=redactor),
        "checks": _native_telemetry_checks(value.get("checks"), redactor=redactor),
        "changed_files": _native_telemetry_files(value.get("changed_files"), redactor=redactor),
        "counters": {},
        "counters_source": "native_event_projection",
        "result": {},
        "usage": _native_telemetry_usage(value.get("usage"), redactor=redactor),
    }
    for raw in value.get("events") or []:
        if not isinstance(raw, Mapping):
            continue
        kind = _native_telemetry_text(raw.get("kind"), limit=80, redactor=redactor)
        payload = _native_telemetry_payload(kind, raw.get("payload"), redactor=redactor)
        if kind:
            item = {"sequence": len(result["events"]) + 1, "kind": kind, "payload": payload}
            tool_call_id = _native_telemetry_text(
                (raw.get("payload") or {}).get("tool_call_id") if isinstance(raw.get("payload"), Mapping) else None,
                limit=120,
                redactor=redactor,
            )
            if tool_call_id:
                item["payload"]["tool_call_id"] = tool_call_id
            result["events"].append(item)
        if len(result["events"]) >= _NATIVE_TELEMETRY_EVENT_LIMIT:
            break
    for raw in value.get("updates") or []:
        if not isinstance(raw, Mapping):
            continue
        fields = {}
        for key in ("current_step", "latest_activity"):
            text = _native_telemetry_text(raw.get(key), redactor=redactor)
            if text:
                fields[key] = text
        if isinstance(raw.get("counters"), Mapping):
            fields["counters"] = {
                str(key)[:80]: max(0, min(int(item), 2_000_000_000))
                for key, item in list(raw["counters"].items())[:32]
                if isinstance(item, int) and not isinstance(item, bool)
            }
        if fields:
            result["updates"].append({"sequence": len(result["updates"]) + 1, "fields": fields})
        if len(result["updates"]) >= _NATIVE_TELEMETRY_UPDATE_LIMIT:
            break
    if isinstance(value.get("counters"), Mapping):
        result["counters"] = {
            str(key)[:80]: max(0, min(int(item), 2_000_000_000))
            for key, item in list(value["counters"].items())[:64]
            if isinstance(item, int) and not isinstance(item, bool)
        }
    counters_source = _native_telemetry_text(value.get("counters_source"), limit=80, redactor=redactor)
    if counters_source:
        result["counters_source"] = counters_source
    raw_result = value.get("result")
    if isinstance(raw_result, Mapping):
        for key in ("status", "failure_reason", "failure_detail", "stopped_reason", "completion_summary", "summary_text", "report_parsed"):
            projected = _native_telemetry_scalar(raw_result.get(key), redactor=redactor)
            if projected is not None:
                result["result"][key] = projected
    return result


class NativeWorkerTelemetry:
    """Thread-safe bounded sink for one inline native worker attempt."""

    def __init__(
        self,
        provenance: Mapping[str, Any] | None = None,
        *,
        provider: str = "codex-cli",
        model: str | None = None,
        owner_id: str | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._redactor = _native_telemetry_redactor()
        self._provenance = _native_telemetry_provenance(provenance, redactor=self._redactor)
        self._provider = "codex-cli" if provider == "codex-cli" else "codex-cli"
        self._provider_model = _native_telemetry_text(model, limit=160, redactor=self._redactor)
        self._owner_id = _native_telemetry_text(owner_id, limit=160, redactor=self._redactor)
        self._provider_usage: dict[str, Any] | None = None
        self._events: list[dict[str, Any]] = []
        self._updates: list[dict[str, Any]] = []
        self._active_commands: dict[str, bool] = {}
        self._job_commands: dict[str, str] = {}
        self._command_number = 0
        self._counters: dict[str, int] = {}

    def record_provider_usage(self, payload: Mapping[str, Any]) -> None:
        """Accumulate only usage delivered by the trusted Codex app-server hook."""

        if not isinstance(payload, Mapping):
            return
        values = {
            key: int(payload.get(key) or 0)
            for key in _NATIVE_USAGE_FIELDS
            if isinstance(payload.get(key), int) and not isinstance(payload.get(key), bool)
            and int(payload.get(key) or 0) >= 0
        }
        if not values:
            return
        with self._lock:
            if self._provider_usage is None:
                self._provider_usage = {
                    "status": "reported",
                    "provider": self._provider,
                    "model": self._provider_model,
                    "owner_id": self._owner_id,
                    "metrics_source": "provider_reported:codex_app_server",
                    "receipt_count": 0,
                }
            for key, value in values.items():
                self._provider_usage[key] = min(
                    2_000_000_000,
                    int(self._provider_usage.get(key, 0)) + value,
                )
            self._provider_usage["receipt_count"] = min(
                256, int(self._provider_usage.get("receipt_count", 0)) + 1
            )

    def emit(self, kind: str, payload: Mapping[str, Any]) -> None:
        name = _native_telemetry_text(kind, limit=80, redactor=self._redactor)
        if not name:
            return
        with self._lock:
            projected = _native_telemetry_payload(name, payload, redactor=self._redactor)
            if name in {"tool_start", "tool_end"}:
                operation_id = _native_telemetry_text(
                    (payload or {}).get("operation_id") if isinstance(payload, Mapping) else None,
                    limit=160,
                    redactor=self._redactor,
                )
                if operation_id:
                    projected["tool_call_id"] = operation_id
            if name == "command_start":
                self._command_number += 1
                call_id = projected.get("tool_call_id") or f"native-command-{self._command_number}"
                self._active_commands[str(call_id)] = True
                projected["tool_call_id"] = call_id
                self._counters["commands"] = self._counters.get("commands", 0) + 1
            elif name == "command_background":
                job_id = str(projected.get("job_id") or "")
                if job_id and self._active_commands:
                    call_id = str(projected.get("tool_call_id") or next(reversed(self._active_commands)))
                    self._job_commands[job_id] = call_id
                    projected["tool_call_id"] = call_id
            elif name == "command_end":
                call_id = projected.get("tool_call_id")
                job_id = str(projected.get("job_id") or "")
                if not call_id and job_id:
                    call_id = self._job_commands.get(job_id)
                if not call_id and len(self._active_commands) == 1:
                    call_id = next(iter(self._active_commands))
                projected["tool_call_id"] = call_id or f"native-command-{self._command_number + 1}"
                self._active_commands.pop(str(projected["tool_call_id"]), None)
                if job_id:
                    self._job_commands.pop(job_id, None)
                if projected.get("success") is not True:
                    self._counters["commands_failed"] = self._counters.get("commands_failed", 0) + 1
            elif name == "file_changed":
                self._counters["files_changed"] = self._counters.get("files_changed", 0) + 1
            elif name == "tool_blocked":
                self._counters["blocked_calls"] = self._counters.get("blocked_calls", 0) + 1
            elif name == "llm_response":
                self._counters["llm_calls"] = self._counters.get("llm_calls", 0) + 1
            if len(self._events) < _NATIVE_TELEMETRY_EVENT_LIMIT:
                self._events.append({"kind": name, "payload": projected})

    def update(self, fields: Mapping[str, Any]) -> None:
        if not isinstance(fields, Mapping):
            return
        with self._lock:
            projected: dict[str, Any] = {}
            for key in ("current_step", "latest_activity"):
                text = _native_telemetry_text(fields.get(key), redactor=self._redactor)
                if text:
                    projected[key] = text
            if isinstance(fields.get("counters"), Mapping):
                projected["counters"] = {
                    str(key)[:80]: max(0, min(int(value), 2_000_000_000))
                    for key, value in list(fields["counters"].items())[:32]
                    if isinstance(value, int) and not isinstance(value, bool)
                }
            if projected and len(self._updates) < _NATIVE_TELEMETRY_UPDATE_LIMIT:
                self._updates.append(projected)

    def snapshot(self, result: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = result.get("result") if isinstance(result, Mapping) else None
        raw = {
            "provenance": dict(self._provenance),
            "events": list(self._events),
            "updates": list(self._updates),
            "commands": body.get("commands") if isinstance(body, Mapping) else None,
            "checks": body.get("checks") if isinstance(body, Mapping) else None,
            "changed_files": result.get("changed_files") if isinstance(result, Mapping) else None,
            "counters": (
                dict(result.get("counters"))
                if isinstance(result, Mapping) and isinstance(result.get("counters"), Mapping)
                else dict(self._counters)
            ),
            "counters_source": (
                "runner_result"
                if isinstance(result, Mapping) and isinstance(result.get("counters"), Mapping)
                else "native_event_projection"
            ),
            "result": {
                key: result.get(key)
                for key in ("status", "failure_reason", "failure_detail")
                if isinstance(result, Mapping) and key in result
            },
            # Provider usage is accepted only from ``record_provider_usage``,
            # which is wired to CodexTaskCompletionClient's app-server
            # receipt channel. Runner/model/report JSON usage is untrusted.
            "usage": dict(self._provider_usage) if self._provider_usage else None,
        }
        return project_native_worker_telemetry(raw)


def host_snapshot() -> dict[str, Any]:
    """Resolve the current review snapshot through the shared review module."""

    return _instruction_review.host_snapshot()


def cli_snapshot() -> dict[str, Any]:
    """Resolve the current official CLI identity through the shared review module."""

    return _instruction_review.cli_snapshot()


class CodexDevelopmentRuntimeError(RuntimeError):
    """A deployment or claim binding rejected the dedicated worker."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = str(code)
        self.detail = str(detail or self.code)
        super().__init__(self.code)


def _error(code: str, detail: str | None = None) -> CodexDevelopmentRuntimeError:
    return CodexDevelopmentRuntimeError(code, detail)


def _bool_env(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _uuid(value: Any, code: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise _error(code) from exc


def _digest(value: Any, code: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in _HEX64 for char in result):
        raise _error(code)
    return result


def _canonical_path(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(code)
    try:
        path = Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error(code) from exc
    if not path.is_absolute():
        raise _error(code)
    return str(path)


def _identity(value: Any, code: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or len(value) not in {2, 4}:
        raise _error(code)
    try:
        result = tuple(int(item) for item in value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _error(code) from exc
    if any(item < 0 for item in result):
        raise _error(code)
    return result


def instruction_snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    """Hash only the metadata that ``ReviewedHostInstructions`` monitors."""

    if not isinstance(snapshot, Mapping):
        raise _error("codex_native_worker_instruction_snapshot_invalid")
    try:
        metadata = snapshot_metadata(dict(snapshot)) if any(
            isinstance(item, Mapping) and "text" in item
            for item in (snapshot.get("sources") or ())
        ) else dict(snapshot)
        return review_digest(metadata)
    except (KeyError, TypeError, ValueError) as exc:
        raise _error("codex_native_worker_instruction_snapshot_invalid") from exc


def _selected_sources(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    values = []
    for source in snapshot.get("sources") or ():
        if isinstance(source, Mapping) and source.get("selected"):
            value = str(source.get("path") or "").strip()
            if value:
                values.append(value)
    return tuple(values)


def _read_policy_document(path: Path) -> Mapping[str, Any]:
    try:
        info = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or path.resolve(strict=True) != path
            or info.st_nlink != 1
            or info.st_size > POLICY_MAX_BYTES
        ):
            raise _error("codex_native_worker_policy_file_unverified")
        identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        raw = path.read_bytes()
        after = path.lstat()
        if len(raw) > POLICY_MAX_BYTES:
            raise _error("codex_native_worker_policy_file_too_large")
        if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise _error("codex_native_worker_policy_file_changed")
        value = json.loads(raw.decode("utf-8"))
    except CodexDevelopmentRuntimeError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise _error("codex_native_worker_policy_file_unverified") from exc
    if not isinstance(value, Mapping):
        raise _error("codex_native_worker_policy_invalid")
    return value


@dataclass(frozen=True)
class CodexNativeWorkerPolicy:
    """Read-only deployment approval for the native code worker.

    ``from_deployment`` is the production constructor.  It requires a
    deployment environment flag, a policy path and a separately bound owner
    UUID; application config and request fields are intentionally ignored.
    ``from_document`` exists for unit fixtures and for a future deployment
    provisioning command that writes the same schema.
    """

    enabled: bool
    owner_id: str | None = None
    instruction_digest: str | None = None
    cli_path: str | None = None
    cli_identity: tuple[int, ...] | None = None
    cli_version: str | None = None
    approved_sources: tuple[str, ...] = ()
    host_metadata: Mapping[str, Any] | None = None
    route_context: Mapping[str, str] | None = None
    source: str = "disabled"
    schema: str = POLICY_SCHEMA
    _sealed: bool = field(default=False, init=False, repr=False, compare=False)

    @property
    def sealed(self) -> bool:
        return self._sealed

    @classmethod
    def disabled(cls, reason: str = "unconfigured") -> "CodexNativeWorkerPolicy":
        return cls(enabled=False, source=reason)._seal(_DEPLOYMENT_POLICY_SEAL)

    def _seal(self, token: object) -> "CodexNativeWorkerPolicy":
        if token is not _DEPLOYMENT_POLICY_SEAL:
            raise _error("codex_native_worker_policy_seal_denied")
        object.__setattr__(self, "_sealed", True)
        return self

    @classmethod
    def from_document(cls, document: Mapping[str, Any], *, source: str = "document") -> "CodexNativeWorkerPolicy":
        if not isinstance(document, Mapping):
            raise _error("codex_native_worker_policy_invalid")
        schema = str(document.get("schema") or document.get("version") or POLICY_SCHEMA)
        if schema != POLICY_SCHEMA:
            raise _error("codex_native_worker_policy_schema_unsupported")
        enabled = document.get("enabled", True)
        if enabled is False:
            return cls.disabled("policy_disabled")
        if enabled is not True:
            raise _error("codex_native_worker_policy_enabled_invalid")
        owner = _uuid(document.get("owner_id") or document.get("owner"), "codex_native_worker_policy_owner_invalid")
        snapshot = document.get("instruction_snapshot") or document.get("approved_instruction_snapshot")
        snapshot = snapshot if isinstance(snapshot, Mapping) else {}
        digest = (
            document.get("instruction_digest")
            or document.get("instruction_snapshot_digest")
            or document.get("host_digest")
            or snapshot.get("digest")
        )
        instruction_digest = _digest(digest, "codex_native_worker_policy_instruction_digest_missing")
        raw_cli = document.get("cli") or document.get("approved_cli")
        raw_cli = raw_cli if isinstance(raw_cli, Mapping) else {}
        cli_path = document.get("cli_path") or raw_cli.get("path")
        cli_identity = document.get("cli_identity") or raw_cli.get("identity")
        cli_version = document.get("cli_version") or raw_cli.get("version")
        path = _canonical_path(cli_path, "codex_native_worker_policy_cli_path_invalid")
        identity = _identity(cli_identity, "codex_native_worker_policy_cli_identity_invalid")
        version = str(cli_version or "").strip()
        if not version:
            raise _error("codex_native_worker_policy_cli_version_invalid")
        expected_sources = (
            document.get("approved_sources")
            or document.get("instruction_sources")
            or snapshot.get("sources")
            or ()
        )
        if isinstance(expected_sources, str):
            expected_sources = [expected_sources]
        if not isinstance(expected_sources, (list, tuple)):
            raise _error("codex_native_worker_policy_sources_invalid")
        if any(isinstance(item, Mapping) for item in expected_sources):
            expected_sources = [
                item.get("path")
                for item in expected_sources
                if isinstance(item, Mapping) and item.get("selected")
            ]
        sources = tuple(str(item).strip() for item in expected_sources if str(item).strip())
        raw_host = document.get("host") or snapshot.get("host")
        host = dict(raw_host) if isinstance(raw_host, Mapping) else None
        if host is not None and any(
            isinstance(item, Mapping) and "text" in item for item in (host.get("sources") or ())
        ):
            host = snapshot_metadata(host)
        raw_context = document.get("context") or document.get("execution_context") or {}
        if not isinstance(raw_context, Mapping):
            raise _error("codex_native_worker_policy_context_invalid")
        route_context = {
            str(key): str(value).strip()
            for key, value in raw_context.items()
            if str(key) in {"provider", "model", "effort", "role", "mode"} and str(value).strip()
        }
        return cls(
            enabled=True,
            owner_id=owner,
            instruction_digest=instruction_digest,
            cli_path=path,
            cli_identity=identity,
            cli_version=version,
            approved_sources=sources,
            host_metadata=host,
            route_context=route_context,
            source=source,
            schema=schema,
        )

    @classmethod
    def from_deployment(cls) -> "CodexNativeWorkerPolicy":
        """Load the opt-in policy from deployment-owned process environment."""

        # Never accept a request/config supplied environment mapping.  The
        # launcher process environment is the deployment binding.
        env = dict(os.environ)
        if not _bool_env(env.get(POLICY_ENABLED_ENV)):
            return cls.disabled("deployment_opt_in_missing")
        policy_file = str(env.get(POLICY_ENV) or "").strip()
        owner = str(env.get(POLICY_OWNER_ENV) or "").strip()
        if not policy_file or not owner:
            return cls.disabled("deployment_policy_or_owner_missing")
        document = dict(_read_policy_document(Path(policy_file)))
        # The environment owner is a deployment binding.  A value inside the
        # file cannot self-grant a different Operations user.
        policy = cls.from_document(document, source=policy_file)
        try:
            deployment_owner = _uuid(owner, "codex_native_worker_deployment_owner_invalid")
        except CodexDevelopmentRuntimeError:
            raise
        if policy.owner_id != deployment_owner:
            raise _error("codex_native_worker_deployment_owner_mismatch")
        adoption = document.get("human_adoption") or document.get("adoption")
        if not isinstance(adoption, Mapping) or adoption.get("approved") is not True:
            raise _error("codex_native_worker_human_adoption_required")
        adoption_owner = adoption.get("owner_id") or adoption.get("human_owner_id")
        if adoption_owner is None:
            raise _error("codex_native_worker_human_adoption_owner_required")
        if _uuid(
            adoption_owner, "codex_native_worker_human_adoption_owner_invalid"
        ) != policy.owner_id:
            raise _error("codex_native_worker_human_adoption_owner_mismatch")
        adoption_digest = (
            adoption.get("instruction_digest")
            or adoption.get("approved_instruction_digest")
            or adoption.get("approved_sha256")
        )
        if adoption_digest is None:
            raise _error("codex_native_worker_human_adoption_digest_required")
        if _digest(
            adoption_digest, "codex_native_worker_human_adoption_digest_invalid"
        ) != policy.instruction_digest:
            raise _error("codex_native_worker_human_adoption_digest_mismatch")
        adoption_cli = adoption.get("cli") or adoption.get("approved_cli")
        if not isinstance(adoption_cli, Mapping):
            raise _error("codex_native_worker_human_adoption_cli_required")
        adoption_cli_path = _canonical_path(
            adoption_cli.get("path"),
            "codex_native_worker_human_adoption_cli_path_invalid",
        )
        adoption_cli_identity = _identity(
            adoption_cli.get("identity"),
            "codex_native_worker_human_adoption_cli_identity_invalid",
        )
        adoption_cli_version = str(adoption_cli.get("version") or "").strip()
        if not adoption_cli_version:
            raise _error("codex_native_worker_human_adoption_cli_version_invalid")
        if (
            adoption_cli_path != policy.cli_path
            or adoption_cli_identity != policy.cli_identity
            or adoption_cli_version != policy.cli_version
        ):
            raise _error("codex_native_worker_human_adoption_cli_mismatch")
        for key in (
            "instruction_transmission",
            "trusted_host_assumption",
            "workspace_tool_scope",
            "existing_host_auth",
        ):
            if adoption.get(key) is not True:
                raise _error("codex_native_worker_human_adoption_scope_mismatch")
        expected_context = {"provider": "codex-cli", "role": "code_worker", "mode": "mutate"}
        route_context = policy.route_context or {}
        if any(route_context.get(key) != value for key, value in expected_context.items()):
            raise _error("codex_native_worker_policy_context_mismatch")
        configured_digest = str(env.get(POLICY_DIGEST_ENV) or "").strip().lower()
        if configured_digest and policy.instruction_digest != _digest(
            configured_digest, "codex_native_worker_deployment_digest_invalid"
        ):
            raise _error("codex_native_worker_deployment_digest_mismatch")
        configured_cli = str(env.get(POLICY_CLI_PATH_ENV) or "").strip()
        if configured_cli and policy.cli_path != _canonical_path(
            configured_cli, "codex_native_worker_deployment_cli_path_invalid"
        ):
            raise _error("codex_native_worker_deployment_cli_mismatch")
        configured_version = str(env.get(POLICY_CLI_VERSION_ENV) or "").strip()
        if configured_version and policy.cli_version != configured_version:
            raise _error("codex_native_worker_deployment_cli_version_mismatch")
        return policy._seal(_DEPLOYMENT_POLICY_SEAL)

    def preview(self) -> dict[str, Any]:
        """Return a non-sensitive deployment readiness projection."""

        return {
            "schema": self.schema,
            "enabled": self.enabled,
            "source": self.source,
            "owner_bound": self.owner_id is not None,
            "instruction_snapshot_approved": self.instruction_digest is not None,
            "cli_identity_approved": self.cli_path is not None and self.cli_identity is not None,
            "approved_source_count": len(self.approved_sources),
            "context": dict(self.route_context or {
                "provider": "codex-cli", "role": "code_worker", "mode": "mutate",
            }),
        }

    def _check_context(
        self,
        *,
        owner_id: str,
        provider: str,
        model: str,
        effort: str,
        role: str,
        mode: str,
    ) -> None:
        if not self.enabled:
            raise _error("agent_route_codex_native_worker_policy_disabled")
        if self.owner_id != _uuid(owner_id, "codex_native_worker_owner_invalid"):
            raise _error("agent_route_codex_native_worker_owner_mismatch")
        if provider != "codex-cli" or role != "code_worker" or mode != "mutate":
            raise _error("agent_route_codex_native_worker_context_denied")
        if not str(model).strip() or not str(effort).strip():
            raise _error("agent_route_codex_native_worker_route_unpinned")
        actual = {
            "provider": provider,
            "model": str(model).strip(),
            "effort": str(effort).strip(),
            "role": role,
            "mode": mode,
        }
        for key, expected in (self.route_context or {}).items():
            if actual.get(key) != expected:
                raise _error("agent_route_codex_native_worker_context_mismatch")

    def receipt(
        self,
        *,
        task_id: str,
        run_id: str | None = None,
        owner_id: str,
        task_fingerprint: str,
        boot_id: str,
        now: float | None = None,
        ttl_seconds: float = POLICY_RECEIPT_TTL_SECONDS,
    ) -> ReviewedHostInstructions:
        """Snapshot host state and produce a run-bound review receipt."""

        if not self.enabled:
            raise _error("agent_route_codex_native_worker_policy_disabled")
        if self.owner_id != _uuid(owner_id, "codex_native_worker_owner_invalid"):
            raise _error("agent_route_codex_native_worker_owner_mismatch")
        try:
            ttl = float(ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise _error("codex_native_worker_receipt_ttl_invalid") from exc
        if not math.isfinite(ttl) or ttl <= 0 or ttl > 3600:
            raise _error("codex_native_worker_receipt_ttl_invalid")
        current_host = host_snapshot()
        current_metadata = snapshot_metadata(current_host)
        if self.host_metadata is not None and dict(self.host_metadata) != current_metadata:
            raise _error("agent_route_codex_native_worker_instructions_changed")
        if self.instruction_digest != instruction_snapshot_digest(current_metadata):
            raise _error("agent_route_codex_native_worker_instructions_changed")
        selected = _selected_sources(current_metadata)
        if self.approved_sources and selected != self.approved_sources:
            raise _error("agent_route_codex_native_worker_sources_mismatch")
        current_cli = cli_snapshot()
        if (
            self.cli_path != str(current_cli.get("path") or "")
            or tuple(current_cli.get("identity") or ()) != tuple(self.cli_identity or ())
            or self.cli_version != str(current_cli.get("version") or "")
        ):
            raise _error("agent_route_codex_native_worker_cli_changed")
        timestamp = time.time() if now is None else float(now)
        if not math.isfinite(timestamp):
            raise _error("codex_native_worker_receipt_time_invalid")
        receipt = {
            "nonce": uuid.uuid4().hex,
            "task_id": str(task_id),
            "run_id": str(run_id or task_id),
            "owner": self.owner_id,
            "task_fingerprint": str(task_fingerprint),
            "boot_id": str(boot_id),
            "expires_at": timestamp + min(ttl, 300.0),
            "execution_expires_at": timestamp + ttl,
            "host": current_metadata,
            "cli": {
                "path": str(current_cli["path"]),
                "identity": list(current_cli["identity"]),
                "version": str(current_cli["version"]),
            },
            "review_digest": self.instruction_digest,
        }
        review = ReviewedHostInstructions(
            receipt,
            task_id=str(task_id),
            owner=self.owner_id,
            fingerprint=str(task_fingerprint),
            boot_id=str(boot_id),
        )
        review.check()
        return review


def _scope_path_allowed(path: Any, roots: Sequence[Path]) -> bool:
    try:
        candidate = Path(path).resolve()
        return any(candidate == root or candidate.is_relative_to(root) for root in roots)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def validate_code_worker_scope(
    scope: Any,
    workspace: str | Path,
    *,
    run_id: str,
    owner_id: str,
    agent_id: str,
) -> None:
    """Validate a coordinator-issued scope for the mutate worker phase."""

    try:
        from ..security.harness_execution_scope import HarnessExecutionScope
    except ImportError as exc:  # pragma: no cover - package invariant
        raise _error("agent_route_codex_native_worker_scope_unavailable") from exc
    if not isinstance(scope, HarnessExecutionScope):
        raise _error("agent_route_codex_native_worker_scope_required")
    try:
        root = Path(workspace).resolve(strict=True)
        canonical = Path(scope.canonical_root).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error("agent_route_codex_native_worker_scope_invalid") from exc
    if canonical != root or str(scope.run_id) != str(run_id):
        raise _error("agent_route_codex_native_worker_scope_identity_mismatch")
    if str(scope.user_id) != str(agent_id):
        raise _error("agent_route_codex_native_worker_scope_agent_mismatch")
    network = getattr(getattr(scope, "network_capability", None), "value", None)
    if str(network or "").lower() != "none":
        raise _error("agent_route_codex_native_worker_scope_network_denied")
    if getattr(scope, "external_read_grants", ()) or getattr(scope, "environment_capability_ids", ()) or getattr(scope, "secret_capability_ids", ()):
        raise _error("agent_route_codex_native_worker_scope_grants_denied")
    scratch = tuple(getattr(scope, "scratch_roots", ()) or ())
    roots = (root, *[Path(item).resolve() for item in scratch])
    if any(not _scope_path_allowed(item, (root,)) for item in scratch):
        raise _error("agent_route_codex_native_worker_scope_scratch_denied")
    for name in ("read_roots", "write_roots", "delete_roots", "command_roots"):
        values = tuple(getattr(scope, name, ()) or ())
        if name == "command_roots" and not values:
            raise _error("agent_route_codex_native_worker_scope_commands_missing")
        if any(not _scope_path_allowed(item, roots) for item in values):
            raise _error("agent_route_codex_native_worker_scope_root_denied")
    if not any(Path(item).resolve() == root for item in getattr(scope, "write_roots", ()) or ()):
        raise _error("agent_route_codex_native_worker_scope_write_missing")


class CodexDevelopmentRuntime:
    """Run one pinned Codex code worker through ``LocalAgentTaskRunner``."""

    def __init__(
        self,
        config: Any,
        *,
        policy: CodexNativeWorkerPolicy | None = None,
        preflight: Callable[[Any], None] | None = None,
        runner_factory: Callable[..., Any] | None = None,
        boot_id: str | None = None,
    ) -> None:
        self.config = config
        self.policy = policy if policy is not None else CodexNativeWorkerPolicy.from_deployment()
        if self.policy.enabled and not self.policy.sealed:
            raise _error("codex_native_worker_policy_unsealed")
        self.preflight = (
            preflight
            if preflight is not None
            else CodexManagedConfigPreflight(auto_ensure_daemon=True)
        )
        self.runner_factory = runner_factory
        self.boot_id = str(boot_id or uuid.uuid4().hex)

    def preview(self) -> dict[str, Any]:
        return self.policy.preview()

    def run(
        self,
        *,
        goal: str,
        workspace: str | Path,
        model: str,
        effort: str,
        owner_id: str,
        agent_id: str,
        task_id: str,
        run_id: str,
        project_id: str | None = None,
        scope: Any,
        constraints: Sequence[str] = (),
        completion_criteria: Sequence[str] = (),
        max_tool_rounds: int = 40,
        timeout_sec: int = 1800,
        command_timeout_sec: int = 300,
        limits: LocalAgentTaskLimits | None = None,
        cancel_event: Any | None = None,
        isolated_backend_ready: bool = False,
        telemetry_provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a canonical mutate worker and return the existing task result."""

        owner = _uuid(owner_id, "agent_route_codex_native_worker_owner_invalid")
        self.policy._check_context(
            owner_id=owner,
            provider="codex-cli",
            model=model,
            effort=effort,
            role="code_worker",
            mode="mutate",
        )
        agent = _uuid(agent_id, "agent_route_codex_native_worker_agent_invalid")
        if not isolated_backend_ready:
            raise _error("agent_route_codex_native_worker_isolated_backend_required")
        validate_code_worker_scope(scope, workspace, run_id=run_id, owner_id=owner, agent_id=agent)
        effective_limits = limits or LocalAgentTaskLimits.from_config(self.config)
        # These constraints are deployment-owned task semantics.  They make
        # the existing human-adopted AGENTS delegation rule explicit at the
        # worker boundary without editing or weakening that host instruction.
        effective_constraints = tuple(_NATIVE_CODE_WORKER_CONSTRAINTS) + tuple(
            str(item) for item in constraints
        )
        effective_completion_criteria = tuple(str(item) for item in completion_criteria) + (
            "Implement the assigned workspace changes and leave a concrete changed-file result.",
            "Run relevant verification and report only checks that actually ran.",
        )
        spec = LocalAgentTaskSpec(
            goal=str(goal),
            workspace=str(Path(workspace).resolve(strict=True)),
            constraints=effective_constraints,
            completion_criteria=effective_completion_criteria,
            mode="mutate",
            allow_commands=True,
            allow_adb=False,
            allow_git_commit=False,
            max_tool_rounds=max(1, min(int(max_tool_rounds), effective_limits.max_max_tool_rounds)),
            timeout_sec=max(30, min(int(timeout_sec), effective_limits.max_timeout_sec)),
            command_timeout_sec=max(5, min(int(command_timeout_sec), effective_limits.max_command_timeout_sec)),
            provider="codex-cli",
            model=str(model),
            effort=str(effort),
        )
        review = self.policy.receipt(
            task_id=str(task_id),
            run_id=str(run_id),
            owner_id=owner,
            task_fingerprint=spec.fingerprint(),
            boot_id=self.boot_id,
        )
        telemetry = NativeWorkerTelemetry(
            telemetry_provenance,
            provider="codex-cli",
            model=str(model),
            owner_id=owner,
        )
        if self.runner_factory is None:
            runner = LocalAgentTaskRunner(
                self.config,
                collect_git_metadata=False,
                harness_execution_scope=scope,
                codex_preflight=self.preflight,
            )
        else:
            runner = self.runner_factory(
                config=self.config,
                scope=scope,
                preflight=self.preflight,
                policy=self.policy,
            )
        result = runner.run_inline(
            spec,
            task_id=str(task_id),
            user_id=owner,
            project_id=project_id,
            limits=effective_limits,
            cancel_event=cancel_event,
            _instruction_review=review,
            _instruction_review_boot_id=self.boot_id,
            emit=telemetry.emit,
            update=telemetry.update,
            provider_usage_recorder=telemetry.record_provider_usage,
        )
        if not isinstance(result, Mapping):
            raise _error("agent_route_codex_native_worker_result_invalid")
        output = dict(result)
        output["codex_native_worker"] = {
            "provider": "codex-cli",
            "model": str(model),
            "effort": str(effort),
            "runner": "local_agent_task",
            "collect_git_metadata": False,
            "instruction_review": "deployment_policy_bound",
            "task_id": str(task_id),
            "run_id": str(run_id),
        }
        output["native_telemetry"] = telemetry.snapshot(output)
        return output


__all__ = [
    "CodexDevelopmentRuntime",
    "CodexDevelopmentRuntimeError",
    "CodexNativeWorkerPolicy",
    "NativeWorkerTelemetry",
    "POLICY_ENV",
    "POLICY_ENABLED_ENV",
    "POLICY_OWNER_ENV",
    "POLICY_DIGEST_ENV",
    "POLICY_CLI_PATH_ENV",
    "POLICY_CLI_VERSION_ENV",
    "POLICY_SCHEMA",
    "host_snapshot",
    "cli_snapshot",
    "instruction_snapshot_digest",
    "validate_code_worker_scope",
    "project_native_worker_telemetry",
]
