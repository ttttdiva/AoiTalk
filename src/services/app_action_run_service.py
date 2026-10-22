"""Durable App action run creation, dispatch fences, and reconciliation.

This service owns the server side of the App action protocol.  Manifest and
release code resolves an immutable :class:`ResolvedAction` before calling it;
this module never accepts a command or a filesystem path from the request.
The transport is deliberately small so the PC bridge and the local server
supervisor can share the same run state machine.
"""

from __future__ import annotations

import hashlib
import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..memory.models import (
    APP_ACTION_MODES,
    AppActionRun,
)


MAX_INPUT_JSON_BYTES = 256 * 1024
MAX_RESULT_JSON_BYTES = 64 * 1024
MAX_PROBE_JSON_BYTES = 16 * 1024
_ACTION_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ACTION_REF_RE = re.compile(
    r"(?P<release>[0-9a-fA-F-]{36}):(?P<key>[A-Za-z0-9][A-Za-z0-9._-]{0,127})\Z"
)
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_SAFE_CODE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "blocked"})
_STATE_RANK = {
    "queued": 0,
    "dispatching": 0,
    "accepted": 1,
    "starting": 2,
    "running": 3,
    "stopping": 4,
    "succeeded": 5,
    "failed": 5,
    "cancelled": 5,
    "blocked": 5,
}
_PHASES = frozenset(
    {
        "queued",
        "accepted",
        "starting",
        "running",
        "stopping",
        "succeeded",
        "failed",
        "cancelled",
        "blocked",
        "unknown",
    }
)


class AppActionRunError(RuntimeError):
    """Base error with a stable, safe public code."""

    code = "app_action_run_error"

    def __init__(self, message: str | None = None, *, code: str | None = None):
        self.code = _safe_code(code or self.code) or "app_action_run_error"
        super().__init__(message or self.code)


class AppActionValidationError(AppActionRunError):
    code = "app_action_invalid"


class AppActionIdempotencyConflict(AppActionRunError):
    code = "app_action_idempotency_conflict"


class AppActionTransportError(AppActionRunError):
    code = "app_action_transport_error"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        definitely_not_sent: bool = False,
    ):
        super().__init__(message, code=code)
        self.definitely_not_sent = bool(definitely_not_sent)


def _safe_code(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if _SAFE_CODE_RE.fullmatch(text) else None


def _canonical_json(value: Any, *, label: str, limit: int) -> tuple[dict[str, Any], str]:
    if not isinstance(value, dict):
        raise AppActionValidationError(f"{label} must be an object", code=f"{label}_object_required")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AppActionValidationError(f"{label} is not valid JSON", code=f"{label}_invalid") from exc
    if len(encoded) > limit:
        raise AppActionValidationError(f"{label} is too large", code=f"{label}_too_large")
    return value, hashlib.sha256(encoded).hexdigest()


def canonical_json_hash(value: Mapping[str, Any], *, label: str = "input_json") -> str:
    """Return the stable digest used by the idempotency fence."""

    _normalized, digest = _canonical_json(dict(value), label=label, limit=MAX_INPUT_JSON_BYTES)
    return digest


def _walk_json_values(value: Any):
    """Yield scalar JSON values for the opaque file-id pre-normalizer."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            yield key
            yield from _walk_json_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_values(child)
    else:
        yield value


def parse_action_ref(value: str, *, expected_release_id: UUID | str | None = None) -> tuple[UUID, str]:
    """Parse the only client-visible action identity: ``{release_uuid}:{key}``."""

    match = _ACTION_REF_RE.fullmatch(str(value or "").strip())
    if match is None:
        raise AppActionValidationError("action_ref is invalid", code="app_action_ref_invalid")
    try:
        release_id = UUID(match.group("release"))
    except (TypeError, ValueError) as exc:  # pragma: no cover - regex already narrows this
        raise AppActionValidationError("action_ref release is invalid", code="app_action_ref_invalid") from exc
    if expected_release_id is not None and release_id != UUID(str(expected_release_id)):
        raise AppActionValidationError("action_ref release does not match activation", code="app_action_release_mismatch")
    return release_id, match.group("key")


def _validate_sha256(value: str | None, *, required: bool, label: str) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        if required:
            raise AppActionValidationError(f"{label} is required", code=f"{label}_required")
        return None
    if not _SHA256_RE.fullmatch(text):
        raise AppActionValidationError(f"{label} is invalid", code=f"{label}_invalid")
    return text


@dataclass(frozen=True)
class ResolvedAction:
    """Immutable action/target data produced by the release API boundary."""

    app_id: UUID
    release_id: UUID
    action_ref: str
    action_key: str
    target_key: str
    action_spec_hash: str
    execution_target_id: str
    execution_target_kind: str
    execution_mode: str
    machine_id: str
    device_id: str
    target_contract_hash: str
    owner_user_id: UUID | None = None
    output_schema_json: Mapping[str, Any] = field(default_factory=dict)
    saved_action_id: str | None = None
    target_sha256: str | None = None
    probe_json: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> "ResolvedAction":
        release_id, action_key = parse_action_ref(self.action_ref, expected_release_id=self.release_id)
        if release_id != self.release_id or action_key != self.action_key:
            raise AppActionValidationError("action_ref does not match action key", code="app_action_ref_mismatch")
        if not _ACTION_KEY_RE.fullmatch(self.action_key):
            raise AppActionValidationError("action key is invalid", code="app_action_key_invalid")
        if self.execution_mode not in APP_ACTION_MODES:
            raise AppActionValidationError("execution mode is invalid", code="app_action_mode_invalid")
        if self.execution_target_kind not in {"server", "pc_bridge"}:
            raise AppActionValidationError("execution target kind is invalid", code="app_action_target_kind_invalid")
        if self.execution_target_kind == "pc_bridge" and (
            not str(self.saved_action_id or "").strip() or not self.target_sha256
        ):
            raise AppActionValidationError(
                "PC Bridge action requires saved_action_id and sha256",
                code="app_action_saved_action_contract_invalid",
            )
        if not str(self.target_key or "").strip():
            raise AppActionValidationError("target key is required", code="app_action_target_required")
        for field_name, value, limit in (
            ("execution_target_id", self.execution_target_id, 128),
            ("machine_id", self.machine_id, 128),
            ("device_id", self.device_id, 128),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise AppActionValidationError(
                    f"{field_name} is invalid", code=f"app_action_{field_name}_invalid"
                )
        _validate_sha256(self.action_spec_hash, required=True, label="action_spec_hash")
        _validate_sha256(self.target_contract_hash, required=True, label="target_contract_hash")
        _validate_sha256(self.target_sha256, required=False, label="target_sha256")
        _canonical_json(dict(self.probe_json or {}), label="probe_json", limit=MAX_PROBE_JSON_BYTES)
        _canonical_json(
            dict(self.output_schema_json or {}),
            label="output_schema_json",
            limit=MAX_PROBE_JSON_BYTES,
        )
        return self


class AppActionTransport(Protocol):
    """Transport shared by the PC bridge and the local durable supervisor."""

    async def availability(self, **kwargs: Any) -> Mapping[str, Any] | bool: ...

    async def start(self, **kwargs: Any) -> Mapping[str, Any]: ...

    async def status(self, **kwargs: Any) -> Mapping[str, Any]: ...

    async def logs(self, **kwargs: Any) -> Mapping[str, Any]: ...

    async def stop(self, **kwargs: Any) -> Mapping[str, Any]: ...


class AppActionTransportRouter:
    """Route a persisted run to the transport bound to its target kind.

    A server process owns both the local isolated runner and the optional PC
    Bridge relay.  Keeping the selection here means callers cannot silently
    send a server ``target_command`` through the PC path (or vice versa).
    The run service passes ``execution_target_kind`` on every transport call;
    transports may ignore that keyword when they are already type-specific.
    """

    def __init__(
        self,
        transports: Mapping[str, AppActionTransport],
        *,
        default: AppActionTransport | None = None,
    ) -> None:
        self.transports = dict(transports)
        self.default = default

    def _select(self, kind: Any) -> AppActionTransport:
        key = str(kind or "").strip()
        transport = self.transports.get(key) or self.default
        if transport is None:
            raise AppActionTransportError(
                "action target transport unavailable",
                code="app_action_transport_unavailable",
            )
        return transport

    async def availability(self, **kwargs: Any) -> Mapping[str, Any] | bool:
        return await self._select(kwargs.get("execution_target_kind")).availability(**kwargs)

    async def start(self, **kwargs: Any) -> Mapping[str, Any]:
        return await self._select(kwargs.get("execution_target_kind")).start(**kwargs)

    async def status(self, **kwargs: Any) -> Mapping[str, Any]:
        return await self._select(kwargs.get("execution_target_kind")).status(**kwargs)

    async def logs(self, **kwargs: Any) -> Mapping[str, Any]:
        return await self._select(kwargs.get("execution_target_kind")).logs(**kwargs)

    async def stop(self, **kwargs: Any) -> Mapping[str, Any]:
        return await self._select(kwargs.get("execution_target_kind")).stop(**kwargs)


class FileAwareAppActionTransport:
    """Stage verified opaque Action files before one transport start.

    ``create_run``/``dispatch_run`` already provide the durable start fence.
    This decorator therefore runs only on the first dispatch path; replayed or
    reconciled runs never call ``start`` and consequently never re-transfer
    input bytes.  The underlying transport owns the destination staging
    implementation (PC Bridge upload or server-local staging).
    """

    def __init__(self, transport: AppActionTransport, file_service: Any):
        self.transport = transport
        self.file_service = file_service

    async def availability(self, **kwargs: Any) -> Mapping[str, Any] | bool:
        return await self.transport.availability(**kwargs)

    async def start(self, **kwargs: Any) -> Mapping[str, Any]:
        payload = kwargs.get("payload")
        if not isinstance(payload, Mapping):
            return await self.transport.start(**kwargs)
        input_json = payload.get("input")
        if not isinstance(input_json, Mapping):
            return await self.transport.start(**kwargs)
        collect = getattr(self.file_service, "collect_input_references", None)
        prepare = getattr(self.file_service, "prepare_inputs", None)
        transfer = getattr(self.file_service, "transfer_inputs", None)
        if not callable(collect) or not callable(prepare) or not callable(transfer):
            return await self.transport.start(**kwargs)
        try:
            references = collect(input_json)
        except Exception as exc:
            raise AppActionTransportError(code="app_action_input_file_invalid", definitely_not_sent=True) from exc
        if not references:
            response = await self.transport.start(**kwargs)
            return await self._capture_outputs(response, kwargs)
        try:
            prepared = await asyncio.to_thread(
                prepare,
                input_json,
                run_id=payload["run_id"],
                app_id=payload["app_id"],
                owner_user_id=kwargs.get("actor_user_id") or kwargs["owner_user_id"],
                action_id=payload["action_ref"],
                project_id=payload.get("project_id"),
            )
            manifest = prepared.get("manifest") if isinstance(prepared, Mapping) else None
            refs = prepared.get("references") if isinstance(prepared, Mapping) else None
            normalized_input = prepared.get("input") if isinstance(prepared, Mapping) else None
            if not isinstance(manifest, Mapping) or not isinstance(normalized_input, Mapping):
                raise ValueError("input manifest missing")
            stage = getattr(self.transport, "stage_inputs", None)
            if not callable(stage):
                raise AppActionTransportError(code="app_action_input_transport_unsupported", definitely_not_sent=True)
            chunks = transfer(payload["run_id"], refs or ())
            stage_kwargs = dict(kwargs)
            stage_kwargs.update(
                run_id=str(payload["run_id"]),
                manifest=dict(manifest),
                chunks=chunks,
            )
            await stage(**stage_kwargs)
            next_payload = dict(payload)
            next_payload["input"] = dict(normalized_input)
            next_payload["input_files"] = list(manifest.get("files") or [])
            next_kwargs = dict(kwargs)
            next_kwargs["payload"] = next_payload
            response = await self.transport.start(**next_kwargs)
            return await self._capture_outputs(response, next_kwargs)
        except AppActionTransportError:
            raise
        except Exception as exc:
            raise AppActionTransportError(code="app_action_input_file_unavailable", definitely_not_sent=True) from exc

    async def status(self, **kwargs: Any) -> Mapping[str, Any]:
        response = await self.transport.status(**kwargs)
        return await self._capture_outputs(response, kwargs)

    async def logs(self, **kwargs: Any) -> Mapping[str, Any]:
        return await self.transport.logs(**kwargs)

    async def stop(self, **kwargs: Any) -> Mapping[str, Any]:
        return await self.transport.stop(**kwargs)

    async def _capture_outputs(self, response: Mapping[str, Any], kwargs: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(response, Mapping) or str(response.get("phase") or response.get("status")) not in {"succeeded", "failed"}:
            return response
        result = response.get("result")
        outputs = response.get("output_files") if isinstance(response.get("output_files"), list) else (
            result.get("files") if isinstance(result, Mapping) else None
        )
        capture = getattr(self.transport, "capture_outputs", None)
        download = getattr(self.transport, "download_outputs", None)
        if not isinstance(outputs, list) or (not callable(capture) and not callable(download)):
            return response
        try:
            if callable(download):
                outputs = await download(
                    run_id=kwargs.get("run_id"),
                    owner_user_id=kwargs.get("actor_user_id") or kwargs.get("owner_user_id"),
                    device_id=kwargs.get("device_id"),
                    machine_id=kwargs.get("machine_id"),
                    action_ref=kwargs.get("action_ref"),
                    outputs=outputs,
                )
                metadata = await asyncio.to_thread(
                    self.file_service.capture_outputs,
                    outputs,
                    run_id=kwargs.get("run_id"),
                    app_id=kwargs.get("app_id"),
                    owner_user_id=kwargs.get("owner_user_id"),
                    project_id=kwargs.get("project_id"),
                    action_id=kwargs.get("action_ref"),
                )
                metadata = [item.to_dict() for item in metadata]
            else:
                metadata = await capture(
                    run_id=kwargs.get("run_id"),
                    app_id=kwargs.get("app_id"),
                    owner_user_id=kwargs.get("owner_user_id"),
                    project_id=kwargs.get("project_id"),
                    outputs=outputs,
                    action_ref=kwargs.get("action_ref"),
                )
        except AppActionTransportError:
            raise
        except Exception as exc:
            raise AppActionTransportError(code="app_action_output_file_unavailable") from exc
        if not metadata:
            return response
        enriched = dict(response)
        # Keep raw structured result untouched so its immutable output schema
        # is evaluated independently from opaque file endpoint metadata.
        enriched["output_file_metadata"] = metadata
        return enriched


def _now() -> datetime:
    return datetime.utcnow()


def _normalize_phase(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    aliases = {
        "pending": "queued",
        "ready": "running",
        "started": "running",
        "confirmed": "accepted",
        "stop_requested": "stopping",
        "stopped": "cancelled",
        "unconfirmed": "unknown",
        "complete": "succeeded",
        "completed": "succeeded",
        "error": "failed",
    }
    text = aliases.get(text, text)
    return text if text in _PHASES else None


def _bounded_result(value: Any) -> tuple[Any, str | None]:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return {}, "app_action_result_invalid"
    if len(encoded) > MAX_RESULT_JSON_BYTES:
        return {}, "app_action_result_too_large"
    return value, None


def _result_matches_schema(schema: Mapping[str, Any] | None, value: Any) -> bool:
    """Validate a worker result against the release-bound output schema."""

    if not schema:
        return True
    try:
        from jsonschema import Draft202012Validator, FormatChecker

        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        return not any(validator.iter_errors(value))
    except Exception:
        # A missing/invalid validator must not turn a process exit into a
        # falsely verified action result.
        return False


def _remote_identity_matches(run: AppActionRun, remote: Mapping[str, Any]) -> bool:
    matches = (
        str(remote.get("run_id") or remote.get("id") or "") == str(run.id)
        and str(remote.get("action_ref") or "") == str(run.action_ref)
        and str(remote.get("machine_id") or "") == str(run.machine_id)
        and str(remote.get("contract_hash") or "") == str(run.target_contract_hash)
        and str(remote.get("input_sha256") or "") == str(run.input_hash)
    )
    if run.execution_target_kind == "pc_bridge":
        matches = matches and (
            str(remote.get("saved_action_id") or "") == str(run.saved_action_id or "")
            and str(remote.get("sha256") or "") == str(run.target_sha256 or "")
        )
    else:
        matches = matches and not remote.get("saved_action_id") and not remote.get("sha256")
    return matches


def _snapshot_is_stale(run: AppActionRun, phase: str) -> bool:
    """Reject late transport receipts that would revive an older state."""

    current = str(run.status or "unknown")
    if current == "unknown":
        # Explicit reconciliation is allowed to resolve an earlier unknown.
        return False
    if current in _TERMINAL_STATUSES:
        # A terminal outcome is immutable.  A duplicate receipt with the same
        # phase may still enrich exit/log evidence; every other phase is late.
        return phase != current
    if current == "stopping" and phase in {"queued", "accepted", "starting", "running"}:
        return True
    return _STATE_RANK.get(phase, -1) < _STATE_RANK.get(current, -1)


def apply_remote_snapshot(run: AppActionRun, remote: Mapping[str, Any], *, now: datetime | None = None) -> AppActionRun:
    """Apply a verified status response without turning exit0 into readiness."""

    if not _remote_identity_matches(run, remote):
        if run.status in _TERMINAL_STATUSES:
            return run
        run.status = "unknown"
        run.dispatch_state = "unknown"
        run.error_code = "app_action_identity_mismatch"
        return run

    now = now or _now()
    phase = _normalize_phase(
        remote.get("phase") or remote.get("status") or remote.get("execution_status")
    )
    if phase is not None and _snapshot_is_stale(run, phase):
        return run
    if remote.get("ready") is True:
        readiness = "ready"
    else:
        readiness = str(remote.get("readiness_status") or remote.get("readiness") or "").strip().lower()
    if readiness not in {"not_required", "pending", "ready", "failed", "unknown"}:
        readiness = "unknown" if run.execution_mode != "finite" else "not_required"
    process = str(remote.get("process_status") or remote.get("process") or "").strip().lower()
    if process and process not in {"not_started", "running", "exited", "unknown"}:
        process = "unknown"
    if not process:
        if phase in {"starting", "running"}:
            process = "running"
        elif phase in {"succeeded", "failed", "cancelled"}:
            process = "exited"
        else:
            process = "unknown"
    run.readiness_status = readiness
    run.process_status = process
    result_value = remote["result"] if "result" in remote else remote.get("result_json")
    run.result_json, result_error = _bounded_result(result_value)
    run.error_code = _safe_code(remote.get("error_code") or remote.get("error"))
    run.remote_journal_epoch = str(remote.get("journal_epoch") or remote.get("remote_journal_epoch") or "") or run.remote_journal_epoch
    run.connection_epoch = str(remote.get("connection_epoch") or "") or run.connection_epoch
    try:
        if remote.get("exit_code") is not None:
            run.exit_code = int(remote["exit_code"])
    except (TypeError, ValueError):
        run.exit_code = None
    try:
        if remote.get("log_cursor") is not None:
            run.log_cursor = max(int(run.log_cursor or 0), int(remote["log_cursor"]))
    except (TypeError, ValueError):
        pass

    if phase is None:
        if run.status in _TERMINAL_STATUSES:
            return run
        run.status = "unknown"
        run.dispatch_state = "unknown"
        run.error_code = run.error_code or "app_action_status_unrecognized"
        return run
    if phase == "unknown":
        run.status = "unknown"
        run.dispatch_state = "unknown"
        run.error_code = run.error_code or "app_action_outcome_unknown"
        return run

    if result_error and phase == "succeeded":
        run.status = "failed"
        run.error_code = result_error
        run.ended_at = now
        return run

    if (
        phase == "succeeded"
        and (run.execution_mode == "finite" or remote.get("result_complete") is True)
        and not _result_matches_schema(run.output_schema_json, run.result_json)
    ):
        run.status = "failed"
        run.error_code = "app_action_output_schema_invalid"
        run.ended_at = now
        return run

    if phase in {"accepted", "starting", "running", "stopping", "succeeded", "failed", "cancelled"}:
        run.dispatch_state = "accepted"
        if phase == "accepted":
            run.accepted_at = run.accepted_at or now
        if phase in {"starting", "running", "stopping"}:
            run.started_at = run.started_at or now
            run.heartbeat_at = now

    # ``service`` and ``desktop`` actions are successful launches only after
    # their declared readiness probe.  A process exit code is not readiness.
    if run.execution_mode != "finite" and phase == "succeeded":
        if readiness == "ready" and process == "running":
            run.status = "running"
            run.error_code = None
            run.started_at = run.started_at or now
            run.heartbeat_at = now
        else:
            run.status = "failed"
            run.error_code = run.error_code or "app_action_readiness_not_verified"
            run.ended_at = now
        return run

    run.status = phase
    if phase in _TERMINAL_STATUSES:
        run.ended_at = now
    if phase == "running" and run.execution_mode != "finite" and readiness != "ready":
        run.error_code = run.error_code or "app_action_readiness_pending"
    return run


@dataclass(frozen=True)
class CreateActionRunResult:
    run: AppActionRun
    replayed: bool = False


class AppActionRunService:
    """DB-backed run coordinator with at-most-once start dispatch."""

    def __init__(
        self,
        db_manager: Any,
        *,
        transport: AppActionTransport | None = None,
        transports: Mapping[str, AppActionTransport] | None = None,
        file_service: Any | None = None,
        preflight: Callable[[AppActionRun], Awaitable[Mapping[str, Any] | bool]] | None = None,
        clock: Callable[[], datetime] = _now,
    ):
        self.db_manager = db_manager
        self.transport = transport
        self.file_service = file_service
        if transports:
            # An explicit target map is a closed routing contract.  Falling
            # back to the PC transport for an unknown/server kind would turn
            # a configuration mistake into an unintended remote execution.
            self.transport = AppActionTransportRouter(transports, default=None)
        self.preflight = preflight
        self.clock = clock

    @property
    def has_transport(self) -> bool:
        """Whether at least one execution target transport is configured."""

        return self.transport is not None

    @staticmethod
    def _transport_kind(run: AppActionRun) -> str:
        return str(getattr(run, "execution_target_kind", "") or "").strip()

    def _transport_for_run(self, run: AppActionRun) -> AppActionTransport | None:
        """Resolve the transport without falling back across target kinds."""

        transport = self.transport
        if isinstance(transport, AppActionTransportRouter):
            try:
                return transport._select(self._transport_kind(run))
            except AppActionTransportError:
                return None
        return transport

    async def _session(self) -> Any:
        return await self.db_manager.get_session()

    @staticmethod
    def _transport_owner(run: AppActionRun) -> str:
        return str(getattr(run, "owner_user_id", None) or run.started_by)

    async def create_run(
        self,
        *,
        spec: ResolvedAction,
        started_by: UUID | str,
        owner_user_id: UUID | str | None = None,
        input_json: Mapping[str, Any],
        idempotency_key: str,
        project_id: UUID | str | None = None,
        agent_run_id: UUID | str | None = None,
        blocked_code: str | None = None,
        session: Any | None = None,
    ) -> CreateActionRunResult:
        spec.validate()
        try:
            starter_uuid = UUID(str(started_by))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AppActionValidationError("started_by is invalid", code="app_action_actor_invalid") from exc
        try:
            owner_uuid = UUID(str(owner_user_id if owner_user_id is not None else starter_uuid))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AppActionValidationError("owner_user_id is invalid", code="app_action_owner_invalid") from exc
        project_uuid = UUID(str(project_id)) if project_id is not None else None
        agent_uuid = UUID(str(agent_run_id)) if agent_run_id is not None else None
        key = str(idempotency_key or "").strip()
        if not 1 <= len(key) <= 160 or "\x00" in key:
            raise AppActionValidationError("idempotency_key is invalid", code="app_action_idempotency_invalid")
        # Normalize verified opaque Action file metadata before hashing and
        # persisting the run.  The transport later stages the same normalized
        # projection, so PC/server receipts carry the exact durable input
        # fence even when the caller originally sent only ``file_id``.
        normalized_input_value: Mapping[str, Any] = input_json
        if self.file_service is not None and isinstance(input_json, Mapping):
            try:
                has_opaque = any(
                    isinstance(node, str) and node.startswith("af_")
                    for node in _walk_json_values(input_json)
                )
                if has_opaque:
                    refs = await asyncio.to_thread(
                        self.file_service.validate_input_references,
                        input_json,
                        app_id=spec.app_id,
                        owner_user_id=starter_uuid,
                        action_id=spec.action_ref,
                        project_id=project_uuid,
                    )
                    normalized_input_value = self.file_service.normalize_input_references(input_json, refs)
            except Exception as exc:
                code = getattr(exc, "code", "app_action_input_file_invalid")
                raise AppActionValidationError(str(exc), code=code) from exc
        normalized_input, input_hash = _canonical_json(
            dict(normalized_input_value), label="input_json", limit=MAX_INPUT_JSON_BYTES
        )
        probe, _probe_hash = _canonical_json(
            dict(spec.probe_json or {}), label="probe_json", limit=MAX_PROBE_JSON_BYTES
        )
        output_schema, _output_schema_hash = _canonical_json(
            dict(spec.output_schema_json or {}),
            label="output_schema_json",
            limit=MAX_PROBE_JSON_BYTES,
        )
        now = self.clock()
        initial_status = "blocked" if blocked_code else "queued"
        initial_readiness = "not_required" if spec.execution_mode == "finite" else "pending"
        if blocked_code and spec.execution_mode != "finite":
            initial_readiness = "unknown"

        own_session = session is None
        if own_session:
            session = await self._session()
        try:
            existing = await session.scalar(
                select(AppActionRun)
                .where(
                    AppActionRun.app_id == spec.app_id,
                    AppActionRun.started_by == starter_uuid,
                    AppActionRun.idempotency_key == key,
                )
                .with_for_update()
                .limit(1)
            )
            if existing is not None:
                if (
                    existing.action_ref != spec.action_ref
                    or existing.action_spec_hash != spec.action_spec_hash
                    or existing.execution_target_id != spec.execution_target_id
                    or existing.target_contract_hash != spec.target_contract_hash
                    or existing.machine_id != spec.machine_id
                    or existing.device_id != spec.device_id
                    or existing.execution_target_kind != spec.execution_target_kind
                    or existing.execution_mode != spec.execution_mode
                    or existing.saved_action_id != spec.saved_action_id
                    or existing.target_sha256 != spec.target_sha256
                    or existing.owner_user_id != owner_uuid
                    or existing.input_hash != input_hash
                    or existing.project_id != project_uuid
                ):
                    raise AppActionIdempotencyConflict(
                        "idempotency_key is already bound to another action/input/project"
                    )
                return CreateActionRunResult(existing, replayed=True)

            run = AppActionRun(
                app_id=spec.app_id,
                project_id=project_uuid,
                release_id=spec.release_id,
                action_key=spec.action_key,
                action_ref=spec.action_ref,
                action_spec_hash=spec.action_spec_hash,
                target_key=spec.target_key,
                execution_target_id=spec.execution_target_id,
                execution_target_kind=spec.execution_target_kind,
                execution_mode=spec.execution_mode,
                machine_id=spec.machine_id,
                device_id=spec.device_id,
                saved_action_id=spec.saved_action_id,
                target_sha256=spec.target_sha256,
                target_contract_hash=spec.target_contract_hash,
                probe_json=probe,
                output_schema_json=output_schema,
                input_json=normalized_input,
                input_hash=input_hash,
                idempotency_key=key,
                owner_user_id=owner_uuid,
                started_by=starter_uuid,
                agent_run_id=agent_uuid,
                status=initial_status,
                dispatch_state="not_sent",
                readiness_status=initial_readiness,
                process_status="not_started",
                error_code=_safe_code(blocked_code),
                result_json={"blocked": True, "error_code": _safe_code(blocked_code)}
                if blocked_code
                else {},
                created_at=now,
            )
            session.add(run)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(AppActionRun)
                    .where(
                        AppActionRun.app_id == spec.app_id,
                        AppActionRun.started_by == starter_uuid,
                        AppActionRun.idempotency_key == key,
                    )
                    .limit(1)
                )
                if existing is None:
                    raise
                if (
                    existing.action_ref != spec.action_ref
                    or existing.action_spec_hash != spec.action_spec_hash
                    or existing.execution_target_id != spec.execution_target_id
                    or existing.target_contract_hash != spec.target_contract_hash
                    or existing.machine_id != spec.machine_id
                    or existing.device_id != spec.device_id
                        or existing.execution_target_kind != spec.execution_target_kind
                        or existing.execution_mode != spec.execution_mode
                        or existing.saved_action_id != spec.saved_action_id
                        or existing.target_sha256 != spec.target_sha256
                        or existing.owner_user_id != owner_uuid
                    or existing.input_hash != input_hash
                    or existing.project_id != project_uuid
                ):
                    raise AppActionIdempotencyConflict(
                        "idempotency_key is already bound to another action/input/project"
                    )
                return CreateActionRunResult(existing, replayed=True)
            await session.commit()
            await session.refresh(run)
            return CreateActionRunResult(run, replayed=False)
        finally:
            if own_session:
                await session.close()

    async def create_run_in_session(
        self,
        session: Any,
        *,
        spec: ResolvedAction,
        started_by: UUID | str,
        owner_user_id: UUID | str | None = None,
        input_json: Mapping[str, Any],
        idempotency_key: str,
        project_id: UUID | str | None = None,
        agent_run_id: UUID | str | None = None,
        blocked_code: str | None = None,
    ) -> CreateActionRunResult:
        """Create using an API-owned transaction without closing its session."""

        return await self.create_run(
            spec=spec,
            started_by=started_by,
            owner_user_id=owner_user_id,
            input_json=input_json,
            idempotency_key=idempotency_key,
            project_id=project_id,
            agent_run_id=agent_run_id,
            blocked_code=blocked_code,
            session=session,
        )

    @staticmethod
    def _start_payload(run: AppActionRun) -> dict[str, Any]:
        # The PC worker resolves the saved-action path from its local catalog.
        # Keep the wire shape closed: no path, command, mode or target catalog
        # data is accepted from the server start payload.
        return {
            "app_id": str(run.app_id),
            "release_id": str(run.release_id),
            "action_key": run.action_key,
            "target_key": run.target_key,
            "execution_target_id": run.execution_target_id,
            "execution_target_kind": run.execution_target_kind,
            "execution_mode": run.execution_mode,
            "project_id": str(run.project_id) if run.project_id is not None else None,
            "action_spec_hash": run.action_spec_hash,
            "saved_action_id": run.saved_action_id,
            "sha256": run.target_sha256,
            "contract_hash": run.target_contract_hash,
            "run_id": str(run.id),
            "action_ref": run.action_ref,
            "input": run.input_json or {},
        }

    async def _load_run(self, run_id: UUID | str, *, for_update: bool = False) -> tuple[Any, AppActionRun | None]:
        try:
            run_uuid = UUID(str(run_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AppActionValidationError("run_id is invalid", code="app_action_run_id_invalid") from exc
        session = await self._session()
        statement = select(AppActionRun).where(AppActionRun.id == run_uuid).limit(1)
        if for_update:
            statement = statement.with_for_update()
        run = await session.scalar(statement)
        return session, run

    async def _mark_unknown(self, run_id: UUID | str, code: str) -> AppActionRun | None:
        session, run = await self._load_run(run_id, for_update=True)
        try:
            if run is None:
                return None
            if run.status in _TERMINAL_STATUSES:
                return run
            run.status = "unknown"
            run.dispatch_state = "unknown"
            run.error_code = _safe_code(code) or "app_action_outcome_unknown"
            run.process_status = "unknown"
            if run.execution_mode != "finite":
                run.readiness_status = "unknown"
            await session.commit()
            await session.refresh(run)
            return run
        finally:
            await session.close()

    async def dispatch_run(self, run_id: UUID | str) -> AppActionRun | None:
        """Send one start request after committing the durable dispatch fence."""

        session, run = await self._load_run(run_id, for_update=True)
        try:
            if run is None:
                return None
            if run.status in _TERMINAL_STATUSES or run.status == "unknown":
                return run
            if run.dispatch_state != "not_sent":
                return run
            preflight_run = run
        finally:
            await session.close()

        transport = self._transport_for_run(preflight_run)
        if transport is None:
            return await self._mark_unknown(run_id, "app_action_transport_unavailable")

        expected_connection_epoch: str | None = None
        if self.preflight is not None:
            try:
                decision = await self.preflight(preflight_run)
            except Exception:
                return await self._mark_blocked(run_id, "app_action_authority_unavailable")
            allowed = decision.get("allowed") if isinstance(decision, Mapping) else decision
            if allowed is False:
                code = decision.get("error_code") if isinstance(decision, Mapping) else None
                return await self._mark_blocked(run_id, _safe_code(code) or "app_action_authority_revoked")
            if isinstance(decision, Mapping):
                expected_connection_epoch = str(decision.get("connection_epoch") or "") or None

        # Re-read and claim after authority/availability checks.  A concurrent
        # dispatcher can win this fence while the preflight was running; the
        # loser observes ``dispatch_state != not_sent`` and performs no start.
        session, run = await self._load_run(run_id, for_update=True)
        try:
            if run is None:
                return None
            if run.status in _TERMINAL_STATUSES or run.status == "unknown" or run.dispatch_state != "not_sent":
                return run
            now = self.clock()
            run.status = "dispatching"
            run.dispatch_state = "sent"
            run.dispatch_started_at = now
            if expected_connection_epoch:
                run.connection_epoch = expected_connection_epoch
            # This timestamp is a conservative fence: a server crash after
            # this commit is reconciled with status only, never with start.
            run.dispatch_sent_at = now
            await session.commit()
            await session.refresh(run)
        finally:
            await session.close()

        try:
            availability = getattr(transport, "availability", None)
            if callable(availability):
                available = await availability(
                    owner_user_id=self._transport_owner(run),
                    actor_user_id=str(getattr(run, "started_by", "") or ""),
                    device_id=run.device_id,
                    machine_id=run.machine_id,
                    execution_target_id=run.execution_target_id,
                    execution_target_kind=run.execution_target_kind,
                    execution_mode=run.execution_mode,
                    project_id=run.project_id,
                    saved_action_id=run.saved_action_id,
                    sha256=run.target_sha256,
                    contract_hash=run.target_contract_hash,
                    mode=run.execution_mode,
                    input_sha256=run.input_hash,
                )
                online = available.get("online") if isinstance(available, Mapping) else available
                if online is False:
                    unavailable_code = (
                        _safe_code(available.get("error_code"))
                        if isinstance(available, Mapping)
                        else None
                    )
                    return await self._mark_blocked(
                        run.id, unavailable_code or "app_action_target_offline"
                    )
            response = await transport.start(
                owner_user_id=self._transport_owner(run),
                actor_user_id=str(getattr(run, "started_by", "") or ""),
                app_id=run.app_id,
                device_id=run.device_id,
                machine_id=run.machine_id,
                payload=self._start_payload(run),
                expected_connection_epoch=run.connection_epoch,
                execution_target_id=run.execution_target_id,
                execution_target_kind=run.execution_target_kind,
                execution_mode=run.execution_mode,
                project_id=run.project_id,
                contract_hash=run.target_contract_hash,
                input_sha256=run.input_hash,
                saved_action_id=run.saved_action_id,
                sha256=run.target_sha256,
            )
        except AppActionTransportError as exc:
            if exc.definitely_not_sent:
                return await self._mark_blocked(run.id, exc.code)
            return await self._mark_unknown(run.id, exc.code)
        except Exception:
            return await self._mark_unknown(run.id, "app_action_delivery_unknown")

        return await self._apply_response(run.id, response)

    async def _mark_blocked(self, run_id: UUID | str, code: str) -> AppActionRun | None:
        session, run = await self._load_run(run_id, for_update=True)
        try:
            if run is None:
                return None
            if run.dispatch_state == "accepted" or run.status in {"running", "starting", "accepted"}:
                return run
            run.status = "blocked"
            # The dispatch fence is committed before availability/start so a
            # worker restart can never turn an offline result into a delayed
            # replay.  Preserve a sent/unknown fence even though the terminal
            # result is blocked; only a preflight denial may remain not_sent.
            if run.dispatch_state not in {"sent", "accepted", "unknown"}:
                run.dispatch_state = "not_sent"
            run.error_code = _safe_code(code) or "app_action_blocked"
            run.result_json = {"blocked": True, "error_code": run.error_code}
            run.ended_at = self.clock()
            await session.commit()
            await session.refresh(run)
            return run
        finally:
            await session.close()

    async def _apply_response(self, run_id: UUID | str, response: Mapping[str, Any]) -> AppActionRun | None:
        session, run = await self._load_run(run_id, for_update=True)
        try:
            if run is None:
                return None
            # Terminal outcomes are immutable.  A late accepted/running or a
            # malformed receipt must never reopen a completed run after an
            # API/worker restart.
            if run.status in _TERMINAL_STATUSES:
                return run
            if not isinstance(response, Mapping) or not _remote_identity_matches(run, response):
                run.status = "unknown"
                run.dispatch_state = "unknown"
                run.error_code = "app_action_identity_mismatch"
                run.process_status = "unknown"
            else:
                apply_remote_snapshot(run, response, now=self.clock())
            await session.commit()
            await session.refresh(run)
            return run
        finally:
            await session.close()

    async def reconcile_run(self, run_id: UUID | str) -> AppActionRun | None:
        """Read status for the same run/target; this method never starts work."""

        session, run = await self._load_run(run_id)
        try:
            if run is None:
                return None
        finally:
            await session.close()
        if run.status in _TERMINAL_STATUSES and run.status != "unknown":
            return run
        transport = self._transport_for_run(run)
        if transport is None:
            return await self._mark_unknown(run.id, "app_action_transport_unavailable")
        try:
            response = await transport.status(
                owner_user_id=self._transport_owner(run),
                actor_user_id=str(getattr(run, "started_by", "") or ""),
                app_id=run.app_id,
                device_id=run.device_id,
                machine_id=run.machine_id,
                run_id=str(run.id),
                action_ref=run.action_ref,
                execution_target_id=run.execution_target_id,
                execution_target_kind=run.execution_target_kind,
                execution_mode=run.execution_mode,
                project_id=run.project_id,
                contract_hash=run.target_contract_hash,
                input_sha256=run.input_hash,
                saved_action_id=run.saved_action_id,
                sha256=run.target_sha256,
                expected_connection_epoch=run.connection_epoch,
            )
        except AppActionTransportError as exc:
            return await self._mark_unknown(run.id, exc.code)
        except Exception:
            return await self._mark_unknown(run.id, "app_action_status_unknown")
        if not isinstance(response, Mapping):
            return await self._mark_unknown(run.id, "app_action_status_invalid")
        return await self._apply_response(run.id, response)

    async def recover_pending_runs(self, *, limit: int = 100) -> int:
        """Reconcile persisted runs after a server/worker restart.

        Rows whose dispatch fence was never sent are blocked rather than
        replayed.  Every other row is queried by run_id; no start call is made.
        """

        session = await self._session()
        try:
            rows = list(
                (
                    await session.scalars(
                        select(AppActionRun)
                        .where(
                            AppActionRun.status.in_(
                                {
                                    "queued",
                                    "dispatching",
                                    "accepted",
                                    "starting",
                                    "running",
                                    "stopping",
                                    "unknown",
                                }
                            )
                        )
                        .order_by(AppActionRun.created_at)
                        .limit(max(1, min(int(limit), 500)))
                    )
                ).all()
            )
        finally:
            await session.close()
        changed = 0
        for run in rows:
            if run.dispatch_state == "not_sent":
                await self._mark_blocked(run.id, "app_action_server_restart_before_dispatch")
            else:
                await self.reconcile_run(run.id)
            changed += 1
        return changed

    async def stop_run(self, run_id: UUID | str) -> AppActionRun | None:
        session, run = await self._load_run(run_id, for_update=True)
        try:
            if run is None:
                return None
            if run.status in _TERMINAL_STATUSES:
                return run
            if run.dispatch_state == "not_sent":
                run.status = "cancelled"
                run.error_code = "app_action_cancelled_before_dispatch"
                run.ended_at = self.clock()
                await session.commit()
                await session.refresh(run)
                return run
            run.status = "stopping"
            await session.commit()
            await session.refresh(run)
        finally:
            await session.close()
        transport = self._transport_for_run(run)
        if transport is None:
            return await self._mark_unknown(run.id, "app_action_transport_unavailable")
        try:
            response = await transport.stop(
                owner_user_id=self._transport_owner(run),
                device_id=run.device_id,
                machine_id=run.machine_id,
                run_id=str(run.id),
                action_ref=run.action_ref,
                execution_target_id=run.execution_target_id,
                execution_target_kind=run.execution_target_kind,
                execution_mode=run.execution_mode,
                project_id=run.project_id,
                contract_hash=run.target_contract_hash,
                input_sha256=run.input_hash,
                saved_action_id=run.saved_action_id,
                sha256=run.target_sha256,
            )
        except AppActionTransportError as exc:
            return await self._mark_unknown(run.id, exc.code)
        except Exception:
            return await self._mark_unknown(run.id, "app_action_stop_unknown")
        return await self._apply_response(run.id, response)

    async def get_logs(
        self,
        run_id: UUID | str,
        *,
        cursor: int = 0,
        limit_bytes: int = 64 * 1024,
    ) -> Mapping[str, Any]:
        session, run = await self._load_run(run_id)
        try:
            if run is None:
                raise AppActionValidationError("run not found", code="app_action_run_not_found")
            owner, device, machine, action_ref = (
                self._transport_owner(run), run.device_id, run.machine_id, run.action_ref
            )
        finally:
            await session.close()
        cursor = max(0, int(cursor))
        limit_bytes = max(1, min(int(limit_bytes), MAX_RESULT_JSON_BYTES))
        try:
            transport = self._transport_for_run(run)
            if transport is None:
                raise AppActionTransportError("action transport unavailable", code="app_action_transport_unavailable")
            response = await transport.logs(
                owner_user_id=owner,
                device_id=device,
                machine_id=machine,
                run_id=str(run_id),
                action_ref=action_ref,
                cursor=cursor,
                limit_bytes=limit_bytes,
                execution_target_id=run.execution_target_id,
                execution_target_kind=run.execution_target_kind,
                execution_mode=run.execution_mode,
                project_id=run.project_id,
                contract_hash=run.target_contract_hash,
                input_sha256=run.input_hash,
                saved_action_id=run.saved_action_id,
                sha256=run.target_sha256,
            )
        except AppActionTransportError:
            raise
        except Exception as exc:
            raise AppActionTransportError(code="app_action_logs_unavailable") from exc
        if not isinstance(response, Mapping) or str(response.get("run_id") or run_id) != str(run_id):
            raise AppActionTransportError(code="app_action_logs_identity_mismatch")
        return response


__all__ = [
    "AppActionIdempotencyConflict",
    "AppActionRunError",
    "AppActionRunService",
    "AppActionTransport",
    "AppActionTransportRouter",
    "AppActionTransportError",
    "AppActionValidationError",
    "CreateActionRunResult",
    "ResolvedAction",
    "apply_remote_snapshot",
    "canonical_json_hash",
    "parse_action_ref",
]
