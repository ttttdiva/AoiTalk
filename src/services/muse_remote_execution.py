"""Offline-safe Muse CLI remote-agent execution boundary.

This module intentionally stops at the provider boundary.  The common
ExecutionProfile catalog, role builder, and durable AgentWork/session stores
are owned by another workstream and are not imported here.  Callers inject
small ports for credential resolution, the provider state root, and the
side-thread binding store.

Muse is a remote native agent reached through a local ``muse-cli`` client.  A
successful local process therefore never proves that a remote turn was
accepted or completed.  The adapter only reports completion after a strict
history correlation has observed the nonce-bearing user event, a related
assistant response, and an explicit terminal event.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import subprocess
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, TypeAlias

from ..utils.subprocess_env import build_aoitalk_subprocess_env
from .muse_cli_diagnostics import (
    build_cause,
    classify_cli_stderr,
    closed_exception_type,
    command_label,
    sanitize_cause,
)

try:  # The module is useful in isolated fixture imports as well.
    from .agent_work_runtime import ExecutionOutcome
except Exception:  # pragma: no cover - only used by minimal import harnesses.
    @dataclass
    class ExecutionOutcome:  # type: ignore[no-redef]
        classification: str = "succeeded"
        success: bool | None = None
        result_summary: str | None = None
        error_code: str | None = None
        error_message: str | None = None
        domain_ref: str | None = None
        domain_status: str | None = None
        result: Mapping[str, Any] | None = None
        evidence: Sequence[Mapping[str, Any]] = ()
        usage: Mapping[str, Any] | None = None
        retry_after_seconds: float | None = None
        metadata: Mapping[str, Any] = field(default_factory=dict)


logger = logging.getLogger(__name__)


MUSE_CLI_EXECUTABLE = "muse-cli"
MUSE_CLI_VERSION = "0.3.2"
MUSE_PROVIDER = "muse-remote"
MUSE_EXECUTION_TARGET = "remote_muse"
MUSE_ADAPTER_KEY = "remote_agent_turn"
REMOTE_AGENT_DELEGATE = "remote_agent_delegate"

MAX_MUSE_TEXT_CHARS = 24_000
MAX_WINDOWS_ARG_CHARS = 32_000
MAX_HISTORY_EVENTS = 4_096
MAX_ROLE_TEXT_CHARS = 16_000
MAX_WAIT_SECONDS = 600
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,256}$")

# v0.3.2's main and v0.3.2 tag have different ``is_reply`` implementations.
# Neither CLI helper proves completion: the tag may accept display_text_ready
# without a status, and main only checks that status is truthy.  The adapter
# requires the raw history payload to carry the exact terminal value.
_TERMINAL_STATES = frozenset({"completed"})
_NON_TERMINAL_STATES = frozenset(
    {"pending", "queued", "running", "started", "streaming", "in_progress", "processing"}
)
_MUSE_EVENT_NAMES = frozenset({"message.user", "message.assistant"})

# Durable binding states.  ``thread_create_pending`` is written before the
# remote session-start call, and the two turn states are written before the
# corresponding irreversible boundary.  A new adapter instance treats either
# pending state as read-only recovery work.
_THREAD_CREATE_PENDING = "thread_create_pending"
_TURN_PINNED = "turn_pinned"
_TURN_ATTEMPTED = "send_attempted"
_PENDING_THREAD_PREFIX = "pending-thread:"


class MuseError(RuntimeError):
    """Base class whose public representation is always a safe error code."""

    code = "muse_error"

    def __init__(self, code: str | None = None, *, timed_out: bool = False) -> None:
        self.code = _safe_code(code or self.code)
        self.timed_out = bool(timed_out)
        # Closed-vocabulary evidence about the failing CLI process (never raw output).  It is
        # filled in at the process boundary and only ever read through ``_cause_from_exception``.
        self.command: str | None = None
        self.returncode: int | None = None
        self.duration_ms: int | None = None
        self.diagnostics: Mapping[str, Any] | None = None
        self.cause_type: str | None = None
        super().__init__(self.code)


class MuseInputError(MuseError):
    code = "invalid_input"


class MuseContractError(MuseError):
    code = "cli_contract_mismatch"


class MuseCredentialError(MuseError):
    code = "credential_unavailable"


class MuseAuthorizationError(MuseError):
    code = "remote_authentication_failed"


class MuseGatewayError(MuseError):
    code = "remote_gateway_error"


class MuseTimeoutError(MuseError):
    code = "remote_timeout"

    def __init__(self, code: str | None = None) -> None:
        super().__init__(code, timed_out=True)


class MuseProtocolError(MuseError):
    code = "invalid_provider_response"


class MuseBindingError(MuseError):
    code = "thread_binding_invalid"


class MuseProcessError(MuseError):
    code = "cli_process_failed"


class MuseErrorCode:
    """Stable safe code names used by adapter outcomes."""

    INVALID_INPUT = "invalid_input"
    CLI_CONTRACT_MISMATCH = "cli_contract_mismatch"
    CLI_UNAVAILABLE = "cli_unavailable"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    REMOTE_TARGET_DISABLED = "remote_target_disabled"
    REMOTE_AUTHENTICATION_FAILED = "remote_authentication_failed"
    REMOTE_VM_UNBOUND = "remote_vm_unbound"
    REMOTE_CAPABILITY_REQUIRED = "remote_capability_required"
    REMOTE_LOCAL_CAPABILITY = "remote_local_capability_rejected"
    BINDING_STORE_UNAVAILABLE = "binding_store_unavailable"
    THREAD_BINDING_MISSING = "thread_binding_missing"
    THREAD_BINDING_MISMATCH = "thread_binding_mismatch"
    THREAD_BINDING_UNSAFE = "thread_binding_unsafe"
    THREAD_CREATE_UNCERTAIN = "thread_create_uncertain"
    THREAD_CREATE_FAILED = "thread_create_failed"
    BINDING_PERSIST_FAILED = "binding_persist_failed"
    BASELINE_HISTORY_UNAVAILABLE = "baseline_history_unavailable"
    INPUT_MISSING = "input_missing"
    INPUT_TOO_LONG = "input_too_long"
    SEND_ACCEPTANCE_UNCONFIRMED = "send_acceptance_unconfirmed"
    SEND_RESPONSE_UNCORRELATED = "send_response_uncorrelated"
    SEND_CANCELLED_LOCAL = "send_cancelled_local"
    OUTBOUND_GATE_REQUIRED = "outbound_privacy_gate_required"
    OUTBOUND_GATE_DENIED = "outbound_privacy_denied"
    DEDICATED_HOME_REQUIRED = "dedicated_home_required"
    ROLE_CONTEXT_INVALID = "role_context_invalid"
    ROLE_REVISION_MISMATCH = "role_revision_mismatch"
    CURRENT_AUTH_MISMATCH = "current_authorization_mismatch"


class MuseTurnStatus:
    """Stable turn-state values; connection health is reported separately."""

    NOT_SENT = "not_sent"
    BLOCKED = "blocked"
    UNCERTAIN = "uncertain"
    CONFIRMED = "confirmed"
    LOCAL_STOPPED = "local_stopped"


@dataclass(frozen=True)
class MuseProviderDescriptor:
    """Provider facts exposed to the common resolver when it is integrated."""

    provider: str = MUSE_PROVIDER
    adapter_key: str = MUSE_ADAPTER_KEY
    execution_target: str = MUSE_EXECUTION_TARGET
    version: str = MUSE_CLI_VERSION
    required_capabilities: tuple[str, ...] = (REMOTE_AGENT_DELEGATE,)
    local_filesystem: bool = False
    sandbox: str = "remote_uncontrolled"
    model_control: bool = False
    effort_control: bool = False
    cwd_control: bool = False
    command_execution: bool = False
    workspace_read: bool = False
    workspace_write: bool = False
    tool_free_control: bool = False
    native_persona_replacement: bool = False
    managed_role_delivery: str = "first_turn_user_prefix"
    role_adherence_guaranteed: bool = False
    supported_operations: tuple[str, ...] = ("session-start", "send", "history")

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "adapter_key": self.adapter_key,
            "execution_target": self.execution_target,
            "version": self.version,
            "required_capabilities": list(self.required_capabilities),
            "local_filesystem": self.local_filesystem,
            "sandbox": self.sandbox,
            "model_control": self.model_control,
            "effort_control": self.effort_control,
            "cwd_control": self.cwd_control,
            "command_execution": self.command_execution,
            "workspace_read": self.workspace_read,
            "workspace_write": self.workspace_write,
            "tool_free_control": self.tool_free_control,
            "native_persona_replacement": self.native_persona_replacement,
            "managed_role_delivery": self.managed_role_delivery,
            "role_adherence_guaranteed": self.role_adherence_guaranteed,
            "supported_operations": list(self.supported_operations),
        }


MUSE_PROVIDER_DESCRIPTOR = MuseProviderDescriptor()
MUSE_REMOTE_PROVIDER_DESCRIPTOR = MUSE_PROVIDER_DESCRIPTOR
MUSE_EGRESS_DESCRIPTOR: Mapping[str, str] = {
    "action": "muse.session.send",
    "transport": "muse-cli",
    "destination": MUSE_EXECUTION_TARGET,
    "provider": MUSE_PROVIDER,
    "tool": MUSE_CLI_EXECUTABLE,
    "model": "",
}


@dataclass(frozen=True)
class MuseCredentialResolution:
    """Non-secret credential/target proof returned by the server-owned port.

    The resolver must already have opened or provisioned the provider config
    under ``state_root``.  This object never carries cookie contents or a
    cookie-file path.  Session integration supplies ``owner_id``, agent and
    revision identity, and a ConversationSession-backed target key to the
    resolver; the resolver returns the opaque ``credential_ref`` and private
    state-root/VM/node/auth identity used by this boundary.
    """

    owner_id: str
    credential_ref: str
    state_root: Path | str
    vm_id: str
    node_id: str | None = None
    auth_fingerprint: str = ""
    authenticated: bool = False
    remote_target_enabled: bool = False
    # These values are server-owned execution identity metadata.  They are
    # optional for old fixture ports, but when present they bind a persisted
    # thread to the selected agent/revision and target key.
    agent_id: str | None = None
    revision_id: str | None = None
    remote_target_key: str | None = None

    def normalized(self) -> "MuseCredentialResolution":
        owner = _bounded_identifier(self.owner_id)
        ref = _bounded_identifier(self.credential_ref)
        vm = _bounded_identifier(self.vm_id)
        node = _bounded_identifier(self.node_id) if self.node_id else None
        fingerprint = _safe_identity(self.auth_fingerprint)
        agent = _bounded_identifier(self.agent_id) if self.agent_id else None
        revision = _bounded_identifier(self.revision_id) if self.revision_id else None
        target_key = _bounded_identifier(self.remote_target_key) if self.remote_target_key else None
        if not owner or not ref or not vm or not fingerprint:
            raise MuseCredentialError()
        root = Path(self.state_root)
        if not root.is_absolute() or not root.is_dir():
            raise MuseCredentialError()
        return replace(
            self,
            owner_id=owner,
            credential_ref=ref,
            state_root=root,
            vm_id=vm,
            node_id=node,
            auth_fingerprint=fingerprint,
            authenticated=bool(self.authenticated),
            remote_target_enabled=bool(self.remote_target_enabled),
            agent_id=agent,
            revision_id=revision,
            remote_target_key=target_key,
        )


MuseRemoteTarget = MuseCredentialResolution


@dataclass(frozen=True)
class ManagedRoleContext:
    """Read-only output of the common role context builder."""

    mode: str
    context_schema_version: int
    agent_id: str
    revision_id: str
    role_context_sha256: str
    text: str

    @property
    def agent_revision_id(self) -> str:
        """Shared role-context wire name for the pinned revision."""

        return self.revision_id

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ManagedRoleContext":
        if str(value.get("mode") or "") != "agent_role":
            raise MuseInputError(MuseErrorCode.ROLE_CONTEXT_INVALID)
        try:
            version = int(value.get("context_schema_version"))
        except (TypeError, ValueError):
            raise MuseInputError(MuseErrorCode.ROLE_CONTEXT_INVALID) from None
        if version != 1:
            raise MuseInputError(MuseErrorCode.ROLE_CONTEXT_INVALID)
        agent_id = _bounded_identifier(value.get("agent_id"))
        # The shared immutable role-context wire uses ``agent_revision_id``.
        # Do not silently reinterpret a caller-selected ``revision_id`` alias
        # as authority for managed role delivery.
        revision_id = _bounded_identifier(value.get("agent_revision_id"))
        digest = _safe_hash(value.get("role_context_sha256"))
        text = str(value.get("text") or "")
        if not agent_id or not revision_id or not digest or not text or len(text) > MAX_ROLE_TEXT_CHARS:
            raise MuseInputError(MuseErrorCode.ROLE_CONTEXT_INVALID)
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
            raise MuseInputError(MuseErrorCode.ROLE_CONTEXT_INVALID)
        return cls(
            mode="agent_role",
            context_schema_version=version,
            agent_id=agent_id,
            revision_id=revision_id,
            role_context_sha256=digest,
            text=text,
        )


@dataclass(frozen=True)
class MuseBinding:
    """Pinned AoiTalk to Muse side-thread association."""

    owner_id: str
    credential_ref: str
    vm_id: str
    node_id: str | None
    thread_id: str
    auth_fingerprint: str
    role_mode: str = "raw"
    role_revision_id: str | None = None
    role_context_sha256: str | None = None
    turn_count: int = 0
    current_turn_nonce: str | None = None
    safe_to_send: bool = True
    state: str = "bound"
    agent_id: str | None = None
    revision_id: str | None = None
    remote_target_key: str | None = None
    # First recorded reason a binding became unsafe, as closed labels/numbers only.  Persisted so a
    # later reader can tell *why* without the (never stored) raw CLI output.
    unsafe_reason: str | None = None
    unsafe_cause: Mapping[str, Any] | None = None
    current_run_id: str | None = None

    def normalized(self) -> "MuseBinding":
        owner = _bounded_identifier(self.owner_id)
        ref = _bounded_identifier(self.credential_ref)
        vm = _bounded_identifier(self.vm_id)
        node = _bounded_identifier(self.node_id) if self.node_id else None
        thread = _bounded_identifier(self.thread_id)
        auth = _safe_identity(self.auth_fingerprint)
        role_mode = str(self.role_mode or "raw")
        revision = _bounded_identifier(self.role_revision_id) if self.role_revision_id else None
        digest = _safe_hash(self.role_context_sha256) if self.role_context_sha256 else None
        nonce = str(self.current_turn_nonce or "").strip() or None
        agent_id = _bounded_identifier(self.agent_id) if self.agent_id else None
        binding_revision_id = _bounded_identifier(self.revision_id) if self.revision_id else None
        target_key = _bounded_identifier(self.remote_target_key) if self.remote_target_key else None
        run_id = _bounded_identifier(self.current_run_id) if self.current_run_id else None
        if not all((owner, ref, vm, thread, auth)):
            raise MuseBindingError()
        if role_mode not in {"raw", "managed_role"}:
            raise MuseBindingError()
        if role_mode == "managed_role" and (not revision or not digest):
            raise MuseBindingError()
        try:
            count = max(0, int(self.turn_count))
        except (TypeError, ValueError):
            count = 0
        return replace(
            self,
            owner_id=owner,
            credential_ref=ref,
            vm_id=vm,
            node_id=node,
            thread_id=thread,
            auth_fingerprint=auth,
            role_mode=role_mode,
            role_revision_id=revision,
            role_context_sha256=digest,
            turn_count=count,
            current_turn_nonce=nonce,
            safe_to_send=bool(self.safe_to_send),
            state=_bounded_identifier(self.state) or "bound",
            agent_id=agent_id,
            revision_id=binding_revision_id,
            remote_target_key=target_key,
            current_run_id=run_id,
            unsafe_reason=_safe_code(self.unsafe_reason) if self.unsafe_reason else None,
            unsafe_cause=sanitize_cause(self.unsafe_cause),
        )


@dataclass(frozen=True)
class MuseProcessResult:
    argv: tuple[str, ...]
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = 0
    # Closed-vocabulary reduction of stderr (see muse_cli_diagnostics); stderr itself is never kept.
    diagnostics: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class MuseSessionStart:
    thread_id: str
    vm_id: str | None = None
    node_id: str | None = None


@dataclass(frozen=True)
class MuseSendReceipt:
    send_attempted: bool
    acceptance: str = "acceptance_unconfirmed"
    returncode: int | None = None


@dataclass(frozen=True)
class MuseHistorySnapshot:
    events: tuple[Mapping[str, Any], ...]
    cursor: str | None = None

    @property
    def max_sequence(self) -> int | None:
        values = [_event_sequence(item) for item in self.events]
        values = [value for value in values if value is not None]
        return max(values) if values else None

    @property
    def signatures(self) -> tuple[str, ...]:
        return tuple(_event_signature(item) for item in self.events)


class MuseBindingStore(Protocol):
    """Small port for the common durable side-thread binding store."""

    async def get_binding(self, binding_key: str) -> MuseBinding | Mapping[str, Any] | None:
        ...

    async def persist_binding(self, binding_key: str, binding: MuseBinding) -> None:
        ...

    async def mark_unsafe(self, binding_key: str, *, reason: str, cause: Mapping[str, Any] | None = None) -> None:
        ...

    async def dispose(self, binding_key: str) -> None:
        ...


CredentialResolver: TypeAlias = Callable[..., Awaitable[Any] | Any]
StateRootResolver: TypeAlias = Callable[..., Awaitable[Any] | Any]
ProcessRunner: TypeAlias = Callable[..., Awaitable[MuseProcessResult] | MuseProcessResult]
EgressGate: TypeAlias = Callable[..., Awaitable[Any] | Any]


class InMemoryMuseBindingStore:
    """Fixture store only; production must inject the common durable store."""

    def __init__(self) -> None:
        self.bindings: dict[str, MuseBinding] = {}
        self.unsafe: dict[str, str] = {}

    async def get_binding(self, binding_key: str) -> MuseBinding | None:
        return self.bindings.get(str(binding_key))

    async def persist_binding(self, binding_key: str, binding: MuseBinding) -> None:
        self.bindings[str(binding_key)] = binding.normalized()

    async def mark_unsafe(self, binding_key: str, *, reason: str, cause: Mapping[str, Any] | None = None) -> None:
        key = str(binding_key)
        self.unsafe.setdefault(key, _safe_code(reason) or "unsafe")
        if key in self.bindings:
            current = self.bindings[key]
            # Only ever tightens: state/safe_to_send become unsafe, and the first recorded
            # reason/cause is kept (later failures do not overwrite the original evidence).
            self.bindings[key] = replace(
                current,
                safe_to_send=False,
                state="unsafe",
                unsafe_reason=current.unsafe_reason or _safe_code(reason),
                unsafe_cause=current.unsafe_cause or sanitize_cause(cause),
            )

    async def dispose(self, binding_key: str) -> None:
        key = str(binding_key)
        self.unsafe.pop(key, None)
        self.bindings.pop(key, None)


@dataclass
class _TurnRuntime:
    binding_key: str
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[Any] | None = None
    process_started: bool = False


class MuseRemoteClient:
    """Shell-free client for the narrow, version-pinned Muse CLI surface."""

    def __init__(
        self,
        *,
        executable: str = MUSE_CLI_EXECUTABLE,
        expected_version: str = MUSE_CLI_VERSION,
        process_runner: ProcessRunner | None = None,
        max_text_chars: int = MAX_MUSE_TEXT_CHARS,
        max_argv_chars: int = MAX_WINDOWS_ARG_CHARS,
        verify_contract: bool = True,
    ) -> None:
        self.executable = str(executable or MUSE_CLI_EXECUTABLE)
        self.expected_version = str(expected_version or MUSE_CLI_VERSION)
        self.process_runner = process_runner or _default_process_runner
        self.max_text_chars = max(1, int(max_text_chars))
        self.max_argv_chars = max(1024, int(max_argv_chars))
        self.verify_contract = bool(verify_contract)
        self._validated_roots: set[str] = set()

    async def validate_contract(self, state_root: Path | str) -> dict[str, Any]:
        root = _validate_state_root(state_root)
        root_key = _path_key(root)
        if root_key in self._validated_roots:
            return {"version": self.expected_version, "help_validated": True}
        version_result = await self._invoke_raw([self.executable, "--version"], root, timeout=20.0)
        if version_result.returncode != 0:
            raise _error_from_process(version_result)
        version = _extract_version(version_result.stdout)
        if version != self.expected_version:
            raise MuseContractError()
        help_result = await self._invoke_raw([self.executable, "--help"], root, timeout=20.0)
        if help_result.returncode != 0:
            raise _error_from_process(help_result)
        root_help = help_result.stdout.casefold()
        # The v0.3.2 root parser advertises subcommands only.  The options
        # used by this adapter live on the corresponding subcommand parsers.
        if any(token not in root_help for token in ("session-start", "send", "history")):
            raise MuseContractError()
        if any(token in root_help for token in ("--thread", "--raw", "--wait")):
            raise MuseContractError()
        history_help = await self._invoke_raw([self.executable, "history", "--help"], root, timeout=20.0)
        send_help = await self._invoke_raw([self.executable, "send", "--help"], root, timeout=20.0)
        if history_help.returncode != 0 or send_help.returncode != 0:
            raise MuseContractError()
        history_text = history_help.stdout.casefold()
        send_text = send_help.stdout.casefold()
        if any(token not in history_text for token in ("--thread", "--raw")):
            raise MuseContractError()
        if any(token not in send_text for token in ("--thread", "--wait")):
            raise MuseContractError()
        self._validated_roots.add(root_key)
        return {"version": version, "help_validated": True}

    async def session_start(self, *, state_root: Path | str, timeout_seconds: float = 30.0) -> MuseSessionStart:
        root = _validate_state_root(state_root)
        await self._ensure_contract(root)
        result = await self._invoke([self.executable, "session-start"], root, timeout_seconds)
        try:
            payload = _parse_json_payload(result.stdout, allow_empty=False)
            thread = _find_string(payload, ("session_id", "sessionId", "id"))
            if not thread:
                raise MuseProtocolError("thread_create_failed")
        except MuseError as exc:
            # The process exited 0 but gave no usable thread: keep the process facts as closed labels.
            raise _annotate(exc, command="session-start", result=result)
        vm = _find_string(payload, ("vm_id", "vmId"))
        node = _find_string(payload, ("node_id", "nodeId"))
        return MuseSessionStart(thread_id=_bounded_identifier(thread) or "", vm_id=_bounded_identifier(vm), node_id=_bounded_identifier(node))

    async def send(
        self,
        text: str,
        *,
        thread_id: str,
        state_root: Path | str,
        wait_seconds: float = 0.0,
    ) -> MuseSendReceipt:
        argv, root, wait = self.preflight_send(
            text,
            thread_id=thread_id,
            state_root=state_root,
            wait_seconds=wait_seconds,
        )
        await self._ensure_contract(root)
        result = await self._invoke(argv, root, timeout_seconds=max(30.0, float(wait) + 30.0), map_errors=False)
        if result.timed_out or result.returncode == 4:
            raise _annotate(MuseTimeoutError(), command=command_label(argv), result=result)
        if result.returncode == 2:
            raise _annotate(MuseAuthorizationError(), command=command_label(argv), result=result)
        if result.returncode == 3:
            raise _annotate(MuseGatewayError(), command=command_label(argv), result=result)
        if result.returncode not in (0, None):
            raise _annotate(MuseProcessError(), command=command_label(argv), result=result)
        # The provider may emit {"sent": true}; that is retained only as an
        # internal process fact.  It is never surfaced as an ACK/completion.
        if result.stdout.strip():
            _parse_json_payload(result.stdout, allow_empty=False)
        return MuseSendReceipt(send_attempted=True, acceptance="acceptance_unconfirmed", returncode=result.returncode)

    def preflight_send(
        self,
        text: str,
        *,
        thread_id: str,
        state_root: Path | str,
        wait_seconds: float = 0.0,
    ) -> tuple[list[str], Path, int]:
        """Validate the complete send argv without starting a process.

        The adapter calls this before persisting a turn nonce.  Keeping the
        check side effect free means a local command-line rejection remains a
        retryable ``not_sent`` result after a restart.
        """

        value = str(text)
        thread = _bounded_identifier(thread_id)
        if not thread:
            raise MuseInputError("thread_binding_missing")
        if not value or len(value) > self.max_text_chars:
            raise MuseInputError(MuseErrorCode.INPUT_TOO_LONG if value else MuseErrorCode.INPUT_MISSING)
        try:
            wait = max(0, min(int(float(wait_seconds)), MAX_WAIT_SECONDS))
        except (TypeError, ValueError):
            raise MuseInputError() from None
        root = _validate_state_root(state_root)
        argv = [self.executable, "send", value, "--thread", thread, "--wait", str(wait)]
        _validate_allowed_argv(argv)
        _validate_argv_length(argv, self.max_argv_chars)
        return argv, root, wait

    async def history(self, *, thread_id: str, state_root: Path | str, timeout_seconds: float = 30.0) -> MuseHistorySnapshot:
        thread = _bounded_identifier(thread_id)
        if not thread:
            raise MuseInputError("thread_binding_missing")
        root = _validate_state_root(state_root)
        await self._ensure_contract(root)
        argv = [self.executable, "history", "--thread", thread, "--raw"]
        result = await self._invoke(argv, root, timeout_seconds)
        payload = _parse_json_payload(result.stdout, allow_empty=False)
        events = _extract_history_events(payload)
        cursor = _find_string(payload, ("cursor", "next_cursor", "nextCursor"))
        return MuseHistorySnapshot(events=tuple(events[:MAX_HISTORY_EVENTS]), cursor=_bounded_identifier(cursor))

    async def health(self, *, state_root: Path | str) -> dict[str, Any]:
        """Return connection/CLI health only; this is not turn completion."""
        try:
            contract = await self.validate_contract(state_root)
        except MuseError as exc:
            return {
                "health": "unavailable",
                "connection": "unavailable",
                "connected": False,
                "contract_validated": False,
                "error_code": exc.code,
                "execution_target": MUSE_EXECUTION_TARGET,
                "local_filesystem": False,
                "sandbox": "remote_uncontrolled",
                "provider": MUSE_PROVIDER,
                "turn_status": "not_evaluated",
            }
        return {
            "health": "healthy",
            "connection": "connected",
            "connected": True,
            "contract_validated": bool(contract.get("help_validated")),
            "version": contract.get("version"),
            "execution_target": MUSE_EXECUTION_TARGET,
            "local_filesystem": False,
            "sandbox": "remote_uncontrolled",
            "provider": MUSE_PROVIDER,
            "turn_status": "not_evaluated",
        }

    async def _ensure_contract(self, root: Path) -> None:
        if self.verify_contract:
            await self.validate_contract(root)

    async def _invoke(
        self,
        argv: Sequence[str],
        root: Path,
        timeout_seconds: float,
        *,
        map_errors: bool = True,
    ) -> MuseProcessResult:
        _validate_allowed_argv(argv)
        _validate_argv_length(argv, self.max_argv_chars)
        result = await self._invoke_raw(argv, root, timeout=timeout_seconds)
        if map_errors and result.returncode not in (0, None):
            raise _error_from_process(result)
        return result

    async def _invoke_raw(self, argv: Sequence[str], root: Path, *, timeout: float) -> MuseProcessResult:
        environment = _child_environment(root)
        _prove_dedicated_home(root, environment)
        started = time.monotonic()
        command = command_label(argv)
        try:
            runner = self.process_runner if callable(self.process_runner) else getattr(self.process_runner, "run", None)
            if not callable(runner):
                raise MuseProcessError(MuseErrorCode.CLI_UNAVAILABLE)
            value = runner(
                tuple(str(item) for item in argv),
                env=environment,
                cwd=str(root),
                timeout_seconds=max(0.1, float(timeout)),
            )
            if inspect.isawaitable(value):
                value = await value
        except FileNotFoundError:
            raise _annotate(MuseProcessError(MuseErrorCode.CLI_UNAVAILABLE), command=command, started=started, cause_type="FileNotFoundError") from None
        except (asyncio.TimeoutError, TimeoutError):
            raise _annotate(MuseTimeoutError(), command=command, started=started, cause_type="TimeoutError") from None
        except MuseError as exc:
            raise _annotate(exc, command=command, started=started)
        except Exception as exc:
            raise _annotate(
                MuseProcessError(MuseErrorCode.CLI_UNAVAILABLE), command=command, started=started,
                cause_type=closed_exception_type(type(exc).__name__),
            ) from None
        if isinstance(value, MuseProcessResult):
            if value.diagnostics is None:
                # A runner that did not classify (custom/fake) may hand back stderr text: reduce it to
                # closed labels and drop the text.
                value = replace(value, stderr="", diagnostics=classify_cli_stderr(value.stderr, timed_out=value.timed_out))
            return value
        if isinstance(value, Mapping):
            return MuseProcessResult(
                argv=tuple(str(item) for item in argv),
                returncode=_int_or_none(value.get("returncode")),
                stdout=str(value.get("stdout") or ""),
                stderr="",  # stderr is never retained in public evidence.
                timed_out=bool(value.get("timed_out")),
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        raise MuseProcessError()


class MuseRemoteExecutionAdapter:
    """One remote Muse turn for the common AgentWork coordinator."""

    name = MUSE_ADAPTER_KEY
    adapter_key = MUSE_ADAPTER_KEY
    execution_adapter = MUSE_ADAPTER_KEY
    descriptor = MUSE_PROVIDER_DESCRIPTOR
    required_capabilities = (REMOTE_AGENT_DELEGATE,)
    capabilities = frozenset({REMOTE_AGENT_DELEGATE})

    def __init__(
        self,
        *,
        client: MuseRemoteClient | None = None,
        credential_resolver: CredentialResolver | Any | None = None,
        state_root_resolver: StateRootResolver | Any | None = None,
        target_resolver: CredentialResolver | Any | None = None,
        binding_store: MuseBindingStore | Any | None = None,
        privacy_gateway: Any | None = None,
        egress_gateway: Any | None = None,
        egress_gate: Any | None = None,
        wait_seconds: float = 0.0,
        reconcile_timeout_seconds: float = 0.0,
        poll_interval_seconds: float = 0.0,
    ) -> None:
        self.client = client or MuseRemoteClient()
        self.credential_resolver = credential_resolver
        self.state_root_resolver = state_root_resolver
        self.target_resolver = target_resolver
        self.binding_store = binding_store
        self.privacy_gateway = privacy_gateway if privacy_gateway is not None else egress_gateway if egress_gateway is not None else egress_gate
        self.wait_seconds = max(0.0, min(float(wait_seconds), MAX_WAIT_SECONDS))
        self.reconcile_timeout_seconds = max(0.0, min(float(reconcile_timeout_seconds), 600.0))
        self.poll_interval_seconds = max(0.0, min(float(poll_interval_seconds), 60.0))
        self._active: dict[str, _TurnRuntime] = {}
        self._unsafe_keys: set[str] = set()
        self._settled: dict[str, ExecutionOutcome] = {}
        self._work_binding_keys: dict[str, str] = {}

    def required_capabilities(self, claim: Any) -> tuple[str, ...]:
        del claim
        return (REMOTE_AGENT_DELEGATE,)

    def supports(self, claim: Any) -> bool:
        return str(_claim_value(claim, "execution_adapter", "") or "") == self.adapter_key

    def execution_manifest(self, claim: Any, **_: Any) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "provider": MUSE_PROVIDER,
            "adapter_key": MUSE_ADAPTER_KEY,
            "execution_target": MUSE_EXECUTION_TARGET,
            "capabilities": [REMOTE_AGENT_DELEGATE],
            "local_filesystem": False,
            "sandbox": "remote_uncontrolled",
            "model": None,
            "effort": None,
        }

    async def execute(
        self,
        claim: Any,
        *,
        coordinator: Any | None = None,
        session: Any | None = None,
        run_id: str | None = None,
        run: Any | None = None,
        authority: Any | None = None,
        current_authority: Any | None = None,
        current_agent_authority: Any | None = None,
        **_: Any,
    ) -> ExecutionOutcome:
        del coordinator, session, run_id
        work_id = _bounded_identifier(_claim_value(claim, "work_item_id", "")) or uuid.uuid4().hex
        current_authority = current_authority if current_authority is not None else current_agent_authority if current_agent_authority is not None else authority if authority is not None else run
        previous = self._settled.get(work_id)
        if previous is not None:
            # A settled result is only a local memoization.  A durable nonce
            # or create intent always wins over that memo so a fresh adapter
            # instance can perform read-only recovery.
            binding = None
            if self.binding_store is not None:
                try:
                    binding = await self._get_binding(self._binding_key(claim))
                except MuseError:
                    binding = None
            if binding is None or not _binding_needs_recovery(binding):
                return previous
        runtime = _TurnRuntime(binding_key=self._binding_key(claim))
        self._work_binding_keys[work_id] = runtime.binding_key
        runtime.task = asyncio.current_task()
        self._active[work_id] = runtime
        try:
            outcome = await self._execute_one(claim, runtime, authority=current_authority)
            if outcome.classification == "succeeded":
                self._settled[work_id] = outcome
            return outcome
        except asyncio.CancelledError:
            await self._mark_unsafe(runtime.binding_key, reason=MuseErrorCode.SEND_CANCELLED_LOCAL)
            return self._outcome(
                "cancelled",
                MuseErrorCode.SEND_CANCELLED_LOCAL,
                turn_status="local_stopped",
                send_attempted=runtime.process_started,
                safe_to_send=False,
                acceptance="acceptance_unconfirmed",
            )
        finally:
            self._active.pop(work_id, None)

    async def cancel(self, work_item_id: str) -> Mapping[str, Any]:
        """Stop local polling/process work; Muse remote cancellation is unsupported."""

        key = _bounded_identifier(work_item_id) or ""
        runtime = self._active.get(key)
        if runtime is not None:
            runtime.cancel_event.set()
            current = asyncio.current_task()
            if runtime.task is not None and runtime.task is not current and not runtime.task.done():
                runtime.task.cancel()
        binding_key = await self._cancel_binding_key(key, runtime)
        self._unsafe_keys.add(binding_key)
        await self._mark_unsafe(binding_key, reason=MuseErrorCode.SEND_CANCELLED_LOCAL)
        return {
            "status": "local_stopped",
            "remote_status": "remote_unconfirmed",
            "safe_to_send": False,
            "remote_cancel_called": False,
            "execution_target": MUSE_EXECUTION_TARGET,
            "local_filesystem": False,
            "sandbox": "remote_uncontrolled",
        }

    async def _cancel_binding_key(self, work_item_id: str, runtime: _TurnRuntime | None) -> str:
        """Resolve the binding currently owned by a work item before poisoning it."""

        if runtime is not None:
            return runtime.binding_key
        mapped = self._work_binding_keys.get(work_item_id)
        if mapped:
            return mapped
        # Durable integrations may expose a work-item lookup even after this
        # adapter instance lost its in-memory runtime.  Probe only optional
        # read ports; cancellation never creates, sends, or deletes a remote
        # session.
        store = self.binding_store
        if store is not None:
            for name in (
                "get_active_binding_key",
                "get_binding_key_for_work_item",
                "find_active_binding",
                "lookup_active_binding",
            ):
                callback = getattr(store, name, None)
                if not callable(callback):
                    continue
                try:
                    value = await _call_port(callback, work_item_id=work_item_id, binding_key=work_item_id, key=work_item_id)
                except Exception:
                    continue
                if isinstance(value, str):
                    candidate = _bounded_identifier(value)
                    if candidate:
                        return candidate
                if isinstance(value, Mapping):
                    for field_name in ("binding_key", "remote_target_key", "thread_binding_key", "key"):
                        candidate = _bounded_identifier(value.get(field_name))
                        if candidate:
                            return candidate
        return work_item_id

    async def dispose_thread(self, binding_key: str) -> Mapping[str, Any]:
        """Explicitly forget a poisoned binding; this never deletes a remote session."""

        key = _bounded_identifier(binding_key) or ""
        self._unsafe_keys.discard(key)
        for work_id, work_binding_key in tuple(self._work_binding_keys.items()):
            if work_binding_key == key:
                self._settled.pop(work_id, None)
                self._work_binding_keys.pop(work_id, None)
        if self.binding_store is not None:
            await _store_call(self.binding_store, ("dispose",), key)
        return {
            "status": "disposed_local_binding",
            "remote_session_deleted": False,
            "safe_to_send": False,
            "execution_target": MUSE_EXECUTION_TARGET,
            "local_filesystem": False,
            "sandbox": "remote_uncontrolled",
        }

    async def reconcile(
        self,
        claim: Any,
        *,
        authority: Any | None = None,
        current_authority: Any | None = None,
        current_agent_authority: Any | None = None,
        run: Any | None = None,
    ) -> ExecutionOutcome:
        """Read-only recovery for an existing nonce; never resends.

        Identity and authority are revalidated before the thread history is
        opened.  This ordering prevents a stale credential or revoked agent
        run from reading an old remote conversation.
        """

        current_authority = current_authority if current_authority is not None else current_agent_authority if current_agent_authority is not None else authority if authority is not None else run
        binding_key = self._binding_key(claim)
        try:
            validation = self._validate_claim(claim, authority=current_authority)
            if validation is not None:
                return validation
            role = self._role_context(_claim_metadata(claim))
            context = await self._resolve_context(claim, authority=current_authority)
            binding = await self._get_binding(binding_key)
            if binding is None or not binding.current_turn_nonce:
                # No nonce means there is nothing to correlate.  Do not guess that a send happened:
                # report only what the durable record proves (False / True / unknown).
                attempted = _binding_send_attempted(binding)
                return self._outcome(
                    "uncertain",
                    MuseErrorCode.THREAD_CREATE_UNCERTAIN if attempted is False else MuseErrorCode.SEND_RESPONSE_UNCORRELATED,
                    turn_status="uncertain",
                    send_attempted=attempted,
                    safe_to_send=False,
                    acceptance="acceptance_unconfirmed",
                )
            mismatch = _binding_mismatch(binding, context, role)
            if mismatch:
                return self._outcome(
                    "blocked",
                    mismatch,
                    turn_status="uncertain",
                    send_attempted=binding.state == _TURN_ATTEMPTED or binding.state == "unsafe",
                    safe_to_send=False,
                    acceptance="acceptance_unconfirmed",
                    role_delivery=_role_receipt(binding),
                )
            return await self._reconcile_binding(
                claim,
                binding_key=binding_key,
                binding=binding,
                context=context,
            )
        except MuseError as exc:
            attempted: bool | None = False
            try:
                binding = await self._get_binding(binding_key)
                if binding is not None:
                    attempted = _attempted_for_recovery(binding)
            except Exception:
                pass
            classification = (
                "blocked"
                if exc.code
                in {
                    MuseErrorCode.CREDENTIAL_UNAVAILABLE,
                    MuseErrorCode.REMOTE_TARGET_DISABLED,
                    MuseErrorCode.REMOTE_AUTHENTICATION_FAILED,
                    MuseErrorCode.REMOTE_VM_UNBOUND,
                    MuseErrorCode.CURRENT_AUTH_MISMATCH,
                    MuseErrorCode.THREAD_BINDING_MISMATCH,
                    MuseErrorCode.ROLE_REVISION_MISMATCH,
                    MuseErrorCode.REMOTE_CAPABILITY_REQUIRED,
                    MuseErrorCode.REMOTE_LOCAL_CAPABILITY,
                }
                else "uncertain"
            )
            return self._outcome(
                classification,
                exc.code,
                turn_status="not_sent" if attempted is False else "uncertain",
                send_attempted=attempted,
                safe_to_send=attempted is False,
                acceptance="acceptance_unconfirmed",
            )
        except Exception:
            return self._outcome(
                "uncertain",
                MuseErrorCode.SEND_RESPONSE_UNCORRELATED,
                turn_status="uncertain",
                send_attempted=True,
                safe_to_send=False,
                acceptance="acceptance_unconfirmed",
            )

    async def _reconcile_binding(
        self,
        claim: Any,
        *,
        binding_key: str,
        binding: MuseBinding,
        context: MuseCredentialResolution,
    ) -> ExecutionOutcome:
        """Reconcile one persisted nonce using history only."""

        attempted = binding.state in {_TURN_ATTEMPTED, "unsafe"}
        if binding.state == _THREAD_CREATE_PENDING:
            return self._outcome(
                "uncertain",
                MuseErrorCode.THREAD_CREATE_UNCERTAIN,
                turn_status="uncertain",
                send_attempted=False,
                safe_to_send=False,
                acceptance="acceptance_unconfirmed",
            )
        if _binding_blocks_recovery(binding, claim):
            return self._outcome(
                "blocked",
                MuseErrorCode.THREAD_BINDING_UNSAFE,
                turn_status="blocked",
                send_attempted=attempted,
                safe_to_send=False,
                acceptance="acceptance_unconfirmed",
                role_delivery=_role_receipt(binding),
            )
        try:
            history = await self.client.history(thread_id=binding.thread_id, state_root=context.state_root)
        except MuseError as exc:
            # A pinned nonce has not crossed the send boundary yet.  Preserve
            # that fact in the public result; the history read is retryable and
            # never triggers a send.
            return self._outcome(
                "transient" if not attempted else "uncertain",
                MuseErrorCode.BASELINE_HISTORY_UNAVAILABLE if not attempted else exc.code,
                turn_status="not_sent" if not attempted else "uncertain",
                send_attempted=attempted,
                safe_to_send=not attempted,
                acceptance="acceptance_unconfirmed",
                role_delivery=_role_receipt(binding),
            )
        correlation = _correlate_history(history, MuseHistorySnapshot(events=()), binding.current_turn_nonce or "")
        if correlation.status == "confirmed":
            updated = replace(
                binding,
                current_turn_nonce=None,
                current_run_id=None,
                turn_count=binding.turn_count + 1,
                safe_to_send=True,
                state="bound",
            )
            try:
                await self._persist_binding(binding_key, updated)
            except MuseError:
                self._unsafe_keys.add(binding_key)
                return self._outcome(
                    "uncertain",
                    MuseErrorCode.BINDING_PERSIST_FAILED,
                    turn_status="uncertain",
                    send_attempted=True,
                    safe_to_send=False,
                    acceptance="acceptance_unconfirmed",
                    role_delivery=_role_receipt(binding),
                )
            self._unsafe_keys.discard(binding_key)
            outcome = self._outcome(
                "succeeded",
                turn_status="confirmed",
                send_attempted=True,
                safe_to_send=True,
                acceptance="acceptance_confirmed",
                role_delivery=_role_receipt(updated),
                assistant_text=correlation.assistant_text,
                assistant_message_id=correlation.assistant_message_id,
            )
            work_id = _bounded_identifier(_claim_value(claim, "work_item_id", ""))
            if work_id:
                self._settled[work_id] = outcome
            return outcome
        return self._outcome(
            "uncertain",
            MuseErrorCode.SEND_RESPONSE_UNCORRELATED,
            turn_status="uncertain" if attempted else "not_sent",
            send_attempted=attempted,
            safe_to_send=False,
            acceptance="acceptance_unconfirmed",
            role_delivery=_role_receipt(binding),
        )

    async def _execute_one(self, claim: Any, runtime: _TurnRuntime, *, authority: Any | None = None) -> ExecutionOutcome:
        validation = self._validate_claim(claim, authority=authority)
        if validation is not None:
            return validation
        metadata = _claim_metadata(claim)
        try:
            role = self._role_context(metadata)
        except MuseError as exc:
            return self._outcome("blocked", exc.code, turn_status="not_sent", send_attempted=False, safe_to_send=True, acceptance="not_attempted")
        prompt = _input_text(metadata)
        if not prompt:
            return self._outcome("blocked", MuseErrorCode.INPUT_MISSING, turn_status="not_sent", send_attempted=False, safe_to_send=True, acceptance="not_attempted")
        try:
            context = await self._resolve_context(claim, authority=authority)
        except MuseError as exc:
            classification = "blocked" if exc.code in {
                MuseErrorCode.REMOTE_TARGET_DISABLED,
                MuseErrorCode.REMOTE_CAPABILITY_REQUIRED,
                MuseErrorCode.REMOTE_LOCAL_CAPABILITY,
                MuseErrorCode.CREDENTIAL_UNAVAILABLE,
                MuseErrorCode.REMOTE_AUTHENTICATION_FAILED,
                MuseErrorCode.REMOTE_VM_UNBOUND,
                MuseErrorCode.CURRENT_AUTH_MISMATCH,
            } else "transient"
            return self._outcome(classification, exc.code, turn_status="not_sent", send_attempted=False, safe_to_send=True, acceptance="not_attempted")
        binding_key = self._binding_key(claim)
        try:
            binding = await self._get_binding(binding_key)
        except MuseError as exc:
            return self._outcome("blocked", exc.code, turn_status="not_sent", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
        if binding is not None and _binding_needs_recovery(binding):
            # A persisted nonce or create intent is authoritative across
            # adapter instances.  Recovery is history-only and must happen
            # before the in-process unsafe-key shortcut.
            if binding.state == _THREAD_CREATE_PENDING or binding.thread_id.startswith(_PENDING_THREAD_PREFIX):
                return self._outcome(
                    "blocked" if binding.state == "unsafe" else "uncertain",
                    MuseErrorCode.THREAD_BINDING_UNSAFE if binding.state == "unsafe" else MuseErrorCode.THREAD_CREATE_UNCERTAIN,
                    turn_status="blocked" if binding.state == "unsafe" else "uncertain",
                    send_attempted=False,
                    safe_to_send=False,
                    acceptance="acceptance_unconfirmed",
                )
            if _binding_blocks_recovery(binding, claim):
                return self._outcome(
                    "blocked",
                    MuseErrorCode.THREAD_BINDING_UNSAFE,
                    turn_status="blocked",
                    send_attempted=_attempted_for_recovery(binding),
                    safe_to_send=False,
                    acceptance="acceptance_unconfirmed",
                    role_delivery=_role_receipt(binding),
                )
            # Resolve the server-owned identity again immediately before the
            # recovery history read.  A credential, VM, node, or auth change
            # between admission and recovery must never open the old thread.
            current_context = await self._resolve_context(claim, authority=authority)
            mismatch = _binding_mismatch(binding, current_context, role)
            if mismatch:
                return self._outcome("blocked", mismatch, turn_status="uncertain", send_attempted=_attempted_for_recovery(binding), safe_to_send=False, acceptance="acceptance_unconfirmed", role_delivery=_role_receipt(binding))
            return await self._reconcile_binding(claim, binding_key=binding_key, binding=binding, context=current_context)
        if binding_key in self._unsafe_keys:
            return self._outcome("blocked", MuseErrorCode.THREAD_BINDING_UNSAFE, turn_status="blocked", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
        if self.privacy_gateway is None:
            # The provider boundary cannot send raw role/user text.  Session
            # integration must inject the server-owned privacy gateway; an
            # omitted gate fails closed before thread creation or send.
            return self._outcome("blocked", MuseErrorCode.OUTBOUND_GATE_REQUIRED, turn_status="not_sent", send_attempted=False, safe_to_send=True, acceptance="not_attempted")
        try:
            if binding is None:
                # Persist an opaque create intent before the provider can make
                # a remote side effect.  A process loss after session-start
                # therefore cannot look like a missing binding on restart.
                create_intent = MuseBinding(
                    owner_id=context.owner_id,
                    credential_ref=context.credential_ref,
                    vm_id=context.vm_id,
                    node_id=context.node_id,
                    thread_id=f"{_PENDING_THREAD_PREFIX}{uuid.uuid4().hex}",
                    auth_fingerprint=context.auth_fingerprint,
                    role_mode="managed_role" if role else "raw",
                    role_revision_id=role.revision_id if role else None,
                    role_context_sha256=role.role_context_sha256 if role else None,
                    state=_THREAD_CREATE_PENDING,
                    safe_to_send=False,
                    agent_id=context.agent_id,
                    revision_id=context.revision_id,
                    remote_target_key=context.remote_target_key or binding_key,
                ).normalized()
                try:
                    await self._persist_binding(binding_key, create_intent)
                except MuseError:
                    self._unsafe_keys.add(binding_key)
                    return self._outcome("uncertain", MuseErrorCode.BINDING_PERSIST_FAILED, turn_status="uncertain", send_attempted=False, safe_to_send=False, acceptance="acceptance_unconfirmed")
                binding = create_intent
                start = await self.client.session_start(state_root=context.state_root)
                if start.vm_id and start.vm_id != context.vm_id:
                    raise MuseBindingError(MuseErrorCode.THREAD_BINDING_MISMATCH)
                if start.node_id and context.node_id and start.node_id != context.node_id:
                    raise MuseBindingError(MuseErrorCode.THREAD_BINDING_MISMATCH)
                binding = MuseBinding(
                    owner_id=context.owner_id,
                    credential_ref=context.credential_ref,
                    vm_id=context.vm_id,
                    node_id=context.node_id,
                    thread_id=start.thread_id,
                    auth_fingerprint=context.auth_fingerprint,
                    role_mode="managed_role" if role else "raw",
                    role_revision_id=role.revision_id if role else None,
                    role_context_sha256=role.role_context_sha256 if role else None,
                    agent_id=context.agent_id,
                    revision_id=context.revision_id,
                    remote_target_key=context.remote_target_key or binding_key,
                ).normalized()
                try:
                    await self._persist_binding(binding_key, binding)
                except MuseError:
                    self._unsafe_keys.add(binding_key)
                    return self._outcome("uncertain", MuseErrorCode.BINDING_PERSIST_FAILED, turn_status="uncertain", send_attempted=False, safe_to_send=False, acceptance="acceptance_unconfirmed")
            else:
                mismatch = _binding_mismatch(binding, context, role)
                if mismatch:
                    return self._outcome("blocked", mismatch, turn_status="not_sent", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
                if not binding.safe_to_send or binding.state == "unsafe":
                    return self._outcome("blocked", MuseErrorCode.THREAD_BINDING_UNSAFE, turn_status="blocked", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
        except MuseError as exc:
            # The specific failure is collapsed into thread_create_uncertain below; keep it as closed
            # labels/numbers (code, command, rc, timing, stage/phase) so the cause is not lost.
            cause = _cause_from_exception(exc)
            if exc.code == MuseErrorCode.THREAD_CREATE_UNCERTAIN:
                self._unsafe_keys.add(binding_key)
                await self._mark_unsafe(binding_key, reason=exc.code, cause=cause)
                return self._outcome("uncertain", exc.code, turn_status="uncertain", send_attempted=False, safe_to_send=False, acceptance="acceptance_unconfirmed", cause=cause)
            if isinstance(exc, MuseTimeoutError) or exc.code in {
                "remote_gateway_error",
                "cli_process_failed",
                "cli_unavailable",
                "invalid_provider_response",
                "thread_create_failed",
                "remote_timeout",
            }:
                self._unsafe_keys.add(binding_key)
                await self._mark_unsafe(binding_key, reason=exc.code, cause=cause)
                return self._outcome("uncertain", MuseErrorCode.THREAD_CREATE_UNCERTAIN, turn_status="uncertain", send_attempted=False, safe_to_send=False, acceptance="acceptance_unconfirmed", cause=cause)
            return self._outcome("blocked", exc.code, turn_status="not_sent", send_attempted=False, safe_to_send=False, acceptance="not_attempted", cause=cause)
        except Exception as exc:
            cause = _cause_from_exception(exc)
            self._unsafe_keys.add(binding_key)
            await self._mark_unsafe(binding_key, reason=MuseErrorCode.THREAD_CREATE_UNCERTAIN, cause=cause)
            return self._outcome("uncertain", MuseErrorCode.THREAD_CREATE_UNCERTAIN, turn_status="uncertain", send_attempted=False, safe_to_send=False, acceptance="acceptance_unconfirmed", cause=cause)

        try:
            baseline = await self.client.history(thread_id=binding.thread_id, state_root=context.state_root)
        except MuseError as exc:
            return self._outcome("transient", MuseErrorCode.BASELINE_HISTORY_UNAVAILABLE, turn_status="not_sent", send_attempted=False, safe_to_send=True, acceptance="not_attempted")
        except Exception:
            return self._outcome("transient", MuseErrorCode.BASELINE_HISTORY_UNAVAILABLE, turn_status="not_sent", send_attempted=False, safe_to_send=True, acceptance="not_attempted")

        # Re-resolve the server-owned credential/target immediately before the
        # irreversible provider invocation.  A check made only at admission
        # could otherwise use a revoked credential or a changed VM binding.
        try:
            current_context = await self._resolve_context(claim, authority=authority)
        except MuseError as exc:
            return self._outcome("blocked", exc.code, turn_status="not_sent", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
        mismatch = _binding_mismatch(binding, current_context, role)
        if mismatch:
            return self._outcome("blocked", mismatch, turn_status="not_sent", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
        if _path_key(context.state_root) != _path_key(current_context.state_root):
            return self._outcome("blocked", MuseErrorCode.CURRENT_AUTH_MISMATCH, turn_status="not_sent", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
        context = current_context

        nonce = str(uuid.uuid4())
        try:
            body = _compose_turn_text(prompt, nonce=nonce, role=role, first_turn=binding.turn_count == 0)
            body = await self._protect_outbound(body, nonce=nonce)
            # The Win32 serialized command line check must happen before the
            # nonce becomes durable.  A local length rejection is therefore
            # still a clean, retryable not-sent result.
            self._preflight_send(
                body,
                thread_id=binding.thread_id,
                state_root=context.state_root,
                wait_seconds=self.wait_seconds,
            )
            # Persist the nonce before any send attempt.  A process-success
            # result is never used as proof that this pin was accepted remotely.
            binding = replace(
                binding,
                current_turn_nonce=nonce,
                current_run_id=_claim_run_id(claim),
                state=_TURN_PINNED,
                safe_to_send=False,
            )
            await self._persist_binding(binding_key, binding)
        except MuseInputError as exc:
            return self._outcome("blocked", exc.code, turn_status="not_sent", send_attempted=False, safe_to_send=True, acceptance="not_attempted")
        except MuseError as exc:
            if exc.code in {MuseErrorCode.OUTBOUND_GATE_REQUIRED, MuseErrorCode.OUTBOUND_GATE_DENIED}:
                return self._outcome("blocked", exc.code, turn_status="not_sent", send_attempted=False, safe_to_send=True, acceptance="not_attempted")
            self._unsafe_keys.add(binding_key)
            return self._outcome("uncertain", MuseErrorCode.BINDING_PERSIST_FAILED, turn_status="uncertain", send_attempted=False, safe_to_send=False, acceptance="acceptance_unconfirmed")
        except Exception:
            self._unsafe_keys.add(binding_key)
            return self._outcome("uncertain", MuseErrorCode.BINDING_PERSIST_FAILED, turn_status="uncertain", send_attempted=False, safe_to_send=False, acceptance="acceptance_unconfirmed")

        # Record the second irreversible boundary before invoking the child
        # process.  A crash after this write is recovered by history only.
        try:
            attempted_binding = replace(binding, state=_TURN_ATTEMPTED, safe_to_send=False)
            await self._persist_binding(binding_key, attempted_binding)
            binding = attempted_binding
        except MuseError:
            self._unsafe_keys.add(binding_key)
            await self._mark_unsafe(binding_key, reason=MuseErrorCode.BINDING_PERSIST_FAILED)
            return self._outcome("uncertain", MuseErrorCode.BINDING_PERSIST_FAILED, turn_status="uncertain", send_attempted=False, safe_to_send=False, acceptance="acceptance_unconfirmed", role_delivery=_role_receipt(binding))
        except Exception:
            self._unsafe_keys.add(binding_key)
            await self._mark_unsafe(binding_key, reason=MuseErrorCode.BINDING_PERSIST_FAILED)
            return self._outcome("uncertain", MuseErrorCode.BINDING_PERSIST_FAILED, turn_status="uncertain", send_attempted=False, safe_to_send=False, acceptance="acceptance_unconfirmed", role_delivery=_role_receipt(binding))

        runtime.process_started = True
        try:
            receipt = await self.client.send(body, thread_id=binding.thread_id, state_root=context.state_root, wait_seconds=self.wait_seconds)
        except MuseError as exc:
            cause = _cause_from_exception(exc)
            self._unsafe_keys.add(binding_key)
            await self._mark_unsafe(binding_key, reason=exc.code, cause=cause)
            classification = "blocked" if isinstance(exc, MuseAuthorizationError) else "uncertain"
            return self._outcome(classification, exc.code, turn_status="uncertain", send_attempted=True, safe_to_send=False, acceptance="acceptance_unconfirmed", cause=cause)
        except Exception as exc:
            cause = _cause_from_exception(exc)
            self._unsafe_keys.add(binding_key)
            await self._mark_unsafe(binding_key, reason=MuseErrorCode.SEND_ACCEPTANCE_UNCONFIRMED, cause=cause)
            return self._outcome("uncertain", MuseErrorCode.SEND_ACCEPTANCE_UNCONFIRMED, turn_status="uncertain", send_attempted=True, safe_to_send=False, acceptance="acceptance_unconfirmed", cause=cause)

        deadline = time.monotonic() + self.reconcile_timeout_seconds
        while True:
            if runtime.cancel_event.is_set():
                self._unsafe_keys.add(binding_key)
                await self._mark_unsafe(binding_key, reason=MuseErrorCode.SEND_CANCELLED_LOCAL)
                return self._outcome("cancelled", MuseErrorCode.SEND_CANCELLED_LOCAL, turn_status="local_stopped", send_attempted=True, safe_to_send=False, acceptance="acceptance_unconfirmed", role_delivery=_role_receipt(binding))
            try:
                current = await self.client.history(thread_id=binding.thread_id, state_root=context.state_root)
            except MuseError:
                current = None
            if current is not None:
                correlation = _correlate_history(current, baseline, nonce)
                if correlation.status == "confirmed":
                    confirmed = replace(
                        binding,
                        current_turn_nonce=None,
                        current_run_id=None,
                        turn_count=binding.turn_count + 1,
                        safe_to_send=True,
                        state="bound",
                    )
                    try:
                        await self._persist_binding(binding_key, confirmed)
                    except MuseError:
                        self._unsafe_keys.add(binding_key)
                        return self._outcome("uncertain", MuseErrorCode.BINDING_PERSIST_FAILED, turn_status="uncertain", send_attempted=True, safe_to_send=False, acceptance="acceptance_unconfirmed", role_delivery=_role_receipt(binding))
                    return self._outcome(
                        "succeeded",
                        turn_status="confirmed",
                        send_attempted=receipt.send_attempted,
                        safe_to_send=True,
                        acceptance="acceptance_confirmed",
                        role_delivery=_role_receipt(confirmed),
                        assistant_text=correlation.assistant_text,
                        assistant_message_id=correlation.assistant_message_id,
                    )
            if time.monotonic() >= deadline:
                break
            try:
                await asyncio.wait_for(runtime.cancel_event.wait(), timeout=max(0.01, self.poll_interval_seconds))
            except asyncio.TimeoutError:
                pass
        self._unsafe_keys.add(binding_key)
        await self._mark_unsafe(binding_key, reason=MuseErrorCode.SEND_ACCEPTANCE_UNCONFIRMED)
        return self._outcome("uncertain", MuseErrorCode.SEND_RESPONSE_UNCORRELATED, turn_status="uncertain", send_attempted=True, safe_to_send=False, acceptance="acceptance_unconfirmed", role_delivery=_role_receipt(binding))

    def _preflight_send(self, body: str, *, thread_id: str, state_root: Path, wait_seconds: float) -> None:
        callback = getattr(self.client, "preflight_send", None)
        if callable(callback):
            callback(body, thread_id=thread_id, state_root=state_root, wait_seconds=wait_seconds)
            return
        # Keep injected offline clients on the same exact argv contract as the
        # production client.  This fallback performs no process or network
        # work and exists only for narrow test/port doubles.
        try:
            wait = max(0, min(int(float(wait_seconds)), MAX_WAIT_SECONDS))
        except (TypeError, ValueError):
            raise MuseInputError() from None
        executable = str(getattr(self.client, "executable", MUSE_CLI_EXECUTABLE) or MUSE_CLI_EXECUTABLE)
        max_argv_chars = int(getattr(self.client, "max_argv_chars", MAX_WINDOWS_ARG_CHARS))
        argv = [executable, "send", str(body), "--thread", _bounded_identifier(thread_id), "--wait", str(wait)]
        _validate_allowed_argv(argv)
        _validate_argv_length(argv, max_argv_chars)

    async def _protect_outbound(self, body: str, *, nonce: str) -> str:
        """Run one injected privacy decision for the exact Muse payload."""

        gateway = self.privacy_gateway
        callback = gateway if callable(gateway) else getattr(gateway, "protect", None) if gateway is not None else None
        if not callable(callback):
            raise MuseError(MuseErrorCode.OUTBOUND_GATE_REQUIRED)
        try:
            value = await _call_port(
                callback,
                body,
                provider=MUSE_PROVIDER,
                descriptor=dict(MUSE_EGRESS_DESCRIPTOR),
                base_url=MUSE_EXECUTION_TARGET,
                source_kind="muse_remote_turn",
            )
        except MuseError:
            raise
        except Exception:
            # Do not expose privacy implementation details or raw payloads in
            # the public outcome.  The adapter is fail closed.
            raise MuseError(MuseErrorCode.OUTBOUND_GATE_DENIED) from None
        if isinstance(value, str):
            outbound = value
        else:
            outbound = getattr(value, "final_payload", None)
            if outbound is None:
                outbound = getattr(value, "payload", None)
            if isinstance(value, Mapping) and outbound is None:
                outbound = value.get("final_payload", value.get("payload"))
        if not isinstance(outbound, str) or not outbound or nonce not in outbound:
            raise MuseError(MuseErrorCode.OUTBOUND_GATE_DENIED)
        return outbound

    def _validate_claim(self, claim: Any, *, authority: Any | None = None) -> ExecutionOutcome | None:
        if not self.supports(claim):
            return self._outcome("blocked", "adapter_mismatch", turn_status="not_sent", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
        metadata = _claim_metadata(claim)
        if authority is not None:
            authority_agent = _bounded_identifier(_authority_value(authority, "agent_id", "assigned_agent_id", "target_agent_id"))
            claim_agent = _bounded_identifier(
                metadata.get("agent_id")
                or metadata.get("assigned_agent_id")
                or _claim_value(claim, "assigned_agent_id", None)
                or _claim_value(claim, "agent_id", None)
            )
            if authority_agent and claim_agent and authority_agent != claim_agent:
                return self._outcome("blocked", MuseErrorCode.CURRENT_AUTH_MISMATCH, turn_status="not_sent", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
            authority_revision = _bounded_identifier(_authority_value(authority, "revision_id", "agent_revision_id", "assigned_revision_id"))
            claim_revision = _bounded_identifier(
                metadata.get("agent_revision_id")
                or _claim_value(claim, "agent_revision_id", None)
            )
            if authority_revision and claim_revision and authority_revision != claim_revision:
                return self._outcome("blocked", MuseErrorCode.ROLE_REVISION_MISMATCH, turn_status="not_sent", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
            authority_target = _bounded_identifier(_authority_value(authority, "remote_target_key", "target_key", "session_id"))
            claim_target = _bounded_identifier(
                metadata.get("remote_target_key")
                or metadata.get("remote_thread_binding_key")
                or _claim_value(claim, "conversation_session_id", None)
                or _claim_value(claim, "session_id", None)
            )
            if authority_target and claim_target and authority_target != claim_target:
                return self._outcome("blocked", MuseErrorCode.CURRENT_AUTH_MISMATCH, turn_status="not_sent", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
            allowed_adapters = _authority_allowlist(
                authority,
                "allowed_adapters",
                "allowed_execution_adapters",
                "adapter_allowlist",
                "allowed_adapter_keys",
                "allowlisted_adapters",
                "execution_allowlist",
                "allowlist",
            )
            if allowed_adapters is not None and self.adapter_key.casefold() not in {item.casefold() for item in allowed_adapters}:
                return self._outcome("blocked", MuseErrorCode.CURRENT_AUTH_MISMATCH, turn_status="not_sent", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
            allowed_targets = _authority_allowlist(
                authority,
                "allowed_targets",
                "allowed_execution_targets",
                "target_allowlist",
                "allowlisted_targets",
            )
            if allowed_targets is not None and MUSE_EXECUTION_TARGET.casefold() not in {item.casefold() for item in allowed_targets}:
                return self._outcome("blocked", MuseErrorCode.CURRENT_AUTH_MISMATCH, turn_status="not_sent", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
        caps = {str(item) for item in (_claim_value(claim, "required_capabilities", ()) or ())}
        authority_caps = None
        if authority is not None:
            authority_caps = _claim_value(authority, "required_capabilities", None)
            if authority_caps is None:
                authority_caps = _claim_value(authority, "capabilities", None)
            if authority_caps is None:
                authority_caps = _claim_value(authority, "allowed_capabilities", None)
        if authority_caps is not None:
            caps.update(str(item) for item in (authority_caps or ()))
            pinned_authority_caps = {str(item) for item in (authority_caps or ())}
            if REMOTE_AGENT_DELEGATE not in pinned_authority_caps:
                return self._outcome("blocked", MuseErrorCode.REMOTE_CAPABILITY_REQUIRED, turn_status="not_sent", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
            if pinned_authority_caps.intersection({"command_execute", "workspace_read", "workspace_write"}):
                return self._outcome("blocked", MuseErrorCode.REMOTE_LOCAL_CAPABILITY, turn_status="not_sent", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
        if REMOTE_AGENT_DELEGATE not in caps:
            return self._outcome("blocked", MuseErrorCode.REMOTE_CAPABILITY_REQUIRED, turn_status="not_sent", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
        if caps.intersection({"command_execute", "workspace_read", "workspace_write"}):
            return self._outcome("blocked", MuseErrorCode.REMOTE_LOCAL_CAPABILITY, turn_status="not_sent", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
        if str(metadata.get("execution_target") or "") != MUSE_EXECUTION_TARGET:
            return self._outcome("blocked", "execution_target_mismatch", turn_status="not_sent", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
        if "provider" in metadata and str(metadata.get("provider") or "") != MUSE_PROVIDER:
            return self._outcome("blocked", MuseErrorCode.INVALID_INPUT, turn_status="not_sent", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
        # The remote schema intentionally has no local cwd/permission/model
        # knobs.  Even an empty caller-supplied field is invalid because it
        # would make the remote route look like a local provider contract.
        unsupported_fields = (
            "working_directory",
            "cwd",
            "permission_mode",
            "allowed_capabilities",
            "sandbox",
            "model",
            "effort",
            "reasoning_effort",
            "tool_free",
            "native_persona",
        )
        if any(field_name in metadata for field_name in unsupported_fields):
            return self._outcome("blocked", MuseErrorCode.INVALID_INPUT, turn_status="not_sent", send_attempted=False, safe_to_send=False, acceptance="not_attempted")
        return None

    def _role_context(self, metadata: Mapping[str, Any]) -> ManagedRoleContext | None:
        raw = metadata.get("managed_role_context")
        if raw is None and metadata.get("managed_role") is not None:
            raw = metadata.get("managed_role")
        if raw is None:
            if str(metadata.get("role_mode") or "raw") not in {"", "raw"}:
                raise MuseInputError(MuseErrorCode.ROLE_CONTEXT_INVALID)
            return None
        if not isinstance(raw, Mapping):
            raise MuseInputError(MuseErrorCode.ROLE_CONTEXT_INVALID)
        return ManagedRoleContext.from_mapping(raw)

    async def _resolve_context(self, claim: Any, *, authority: Any | None = None) -> MuseCredentialResolution:
        metadata = _claim_metadata(claim)
        authority_owner = _bounded_identifier(_authority_value(authority, "owner_id", "user_id"))
        metadata_owner = _bounded_identifier(metadata.get("owner_id") or metadata.get("user_id"))
        if authority_owner and metadata_owner and authority_owner != metadata_owner:
            raise MuseError(MuseErrorCode.CURRENT_AUTH_MISMATCH)
        owner_id = authority_owner or metadata_owner
        agent_id = _bounded_identifier(
            _authority_value(authority, "agent_id", "assigned_agent_id", "target_agent_id")
            or metadata.get("agent_id")
            or metadata.get("assigned_agent_id")
            or _claim_value(claim, "assigned_agent_id", None)
            or _claim_value(claim, "agent_id", None)
        ) or None
        revision_id = _bounded_identifier(
            _authority_value(authority, "revision_id", "agent_revision_id", "assigned_revision_id")
            or metadata.get("agent_revision_id")
            or _claim_value(claim, "agent_revision_id", None)
        ) or None
        binding_key = self._binding_key(claim)
        remote_target_key = _bounded_identifier(
            _authority_value(authority, "remote_target_key", "target_key", "session_id")
            or metadata.get("remote_target_key")
            or _claim_value(claim, "conversation_session_id", None)
            or _claim_value(claim, "session_id", None)
            or binding_key
        ) or None
        if not owner_id or self.credential_resolver is None:
            raise MuseCredentialError()
        # The credential reference is intentionally absent from this port.
        # It is an opaque value returned by the server-owned resolver, never a
        # selector copied from WorkItem metadata.
        resolver_kwargs = {
            "owner_id": owner_id,
            "agent_id": agent_id,
            "revision_id": revision_id,
            "agent_revision_id": revision_id,
            "remote_target_key": remote_target_key,
            "session_id": remote_target_key,
            "required_capabilities": _claim_value(claim, "required_capabilities", ()),
            "current_authority": authority,
            "authority": authority,
        }
        try:
            raw = await _call_port(self.credential_resolver, **resolver_kwargs)
        except MuseError:
            raise
        except Exception:
            raise MuseCredentialError() from None
        if raw is None:
            raise MuseCredentialError()
        resolved = _coerce_resolution(raw, owner_id=owner_id)
        if self.target_resolver is not None:
            try:
                target_raw = await _call_port(self.target_resolver, **resolver_kwargs, credential=raw)
            except MuseError:
                raise
            except Exception:
                raise MuseCredentialError() from None
            if target_raw is None:
                raise MuseCredentialError()
            resolved = _merge_resolution(resolved, target_raw)
        if self.state_root_resolver is not None:
            try:
                root_raw = await _call_port(self.state_root_resolver, **resolver_kwargs, credential=resolved)
            except MuseError:
                raise
            except Exception:
                raise MuseCredentialError() from None
            if root_raw in (None, ""):
                raise MuseCredentialError()
            if resolved.state_root in (None, ""):
                resolved = replace(resolved, state_root=Path(root_raw))
            elif Path(root_raw) != Path(resolved.state_root):
                raise MuseCredentialError()
        # Authority supplied by the session service is safe to pin onto the
        # normalized context when the resolver intentionally omits duplicate
        # identity fields.  Claim metadata alone is never elevated this way.
        if authority is not None:
            resolved = replace(
                resolved,
                agent_id=resolved.agent_id or agent_id,
                revision_id=resolved.revision_id or revision_id,
                remote_target_key=resolved.remote_target_key or remote_target_key,
            )
        try:
            resolved = resolved.normalized()
        except MuseError:
            raise
        except Exception:
            raise MuseCredentialError() from None
        if resolved.owner_id != owner_id:
            raise MuseCredentialError()
        if authority_owner and resolved.owner_id != authority_owner:
            raise MuseError(MuseErrorCode.CURRENT_AUTH_MISMATCH)
        if agent_id and resolved.agent_id and resolved.agent_id != agent_id:
            raise MuseError(MuseErrorCode.CURRENT_AUTH_MISMATCH)
        if revision_id and resolved.revision_id and resolved.revision_id != revision_id:
            raise MuseError(MuseErrorCode.ROLE_REVISION_MISMATCH)
        if resolved.remote_target_key and remote_target_key and resolved.remote_target_key != remote_target_key:
            raise MuseError(MuseErrorCode.CURRENT_AUTH_MISMATCH)
        if not resolved.remote_target_enabled:
            raise MuseError(MuseErrorCode.REMOTE_TARGET_DISABLED)
        if not resolved.authenticated:
            raise MuseAuthorizationError()
        if not resolved.vm_id:
            raise MuseError(MuseErrorCode.REMOTE_VM_UNBOUND)
        # Caller metadata can pin/verify identity but can never select a VM.
        if metadata.get("vm_id") not in (None, "", resolved.vm_id) or metadata.get("node_id") not in (None, "", resolved.node_id):
            raise MuseError(MuseErrorCode.CURRENT_AUTH_MISMATCH)
        return resolved

    def _binding_key(self, claim: Any) -> str:
        metadata = _claim_metadata(claim)
        raw = (
            metadata.get("remote_target_key")
            or metadata.get("remote_thread_binding_key")
            or metadata.get("thread_binding_key")
            or _claim_value(claim, "conversation_session_id", None)
            or _claim_value(claim, "session_id", None)
            or _claim_value(claim, "concurrency_key", None)
            or _claim_value(claim, "work_item_id", None)
        )
        return _bounded_identifier(raw) or "binding-missing"

    async def _get_binding(self, key: str) -> MuseBinding | None:
        if self.binding_store is None:
            raise MuseError(MuseErrorCode.BINDING_STORE_UNAVAILABLE)
        try:
            value = await _store_call(self.binding_store, ("get_binding", "get"), key)
        except MuseError:
            raise
        except Exception:
            raise MuseError(MuseErrorCode.BINDING_STORE_UNAVAILABLE) from None
        if value is None:
            return None
        if isinstance(value, MuseBinding):
            return value.normalized()
        if isinstance(value, Mapping):
            try:
                return MuseBinding(**dict(value)).normalized()
            except Exception:
                raise MuseBindingError() from None
        raise MuseBindingError()

    async def _persist_binding(self, key: str, binding: MuseBinding) -> None:
        if self.binding_store is None:
            raise MuseError(MuseErrorCode.BINDING_STORE_UNAVAILABLE)
        try:
            await _store_call(self.binding_store, ("persist_binding", "save", "put"), key, binding.normalized())
        except MuseError:
            raise
        except Exception:
            raise MuseError(MuseErrorCode.BINDING_PERSIST_FAILED) from None

    async def _mark_unsafe(self, key: str, *, reason: str, cause: Mapping[str, Any] | None = None) -> None:
        if self.binding_store is None:
            return
        try:
            # ``_store_call`` drops keyword arguments a custom store does not declare, so a store
            # without ``cause`` support still receives the unchanged ``reason`` call.
            await _store_call(self.binding_store, ("mark_unsafe", "mark_unusable"), key, reason=reason, cause=cause)
        except Exception:
            logger.debug("Muse binding safety mark failed")

    @staticmethod
    def _outcome(
        classification: str,
        error_code: str | None = None,
        *,
        turn_status: str,
        send_attempted: bool | None,
        safe_to_send: bool,
        acceptance: str,
        role_delivery: Mapping[str, Any] | None = None,
        assistant_text: str | None = None,
        assistant_message_id: str | None = None,
        cause: Mapping[str, Any] | None = None,
    ) -> ExecutionOutcome:
        delivery = dict(role_delivery or _raw_role_receipt())
        # ``send_attempted`` is tri-state: True / False are backed by durable evidence; ``None`` means
        # the record cannot prove either way and must never be read as "attempted".
        attempted = None if send_attempted is None else bool(send_attempted)
        safe_result = {
            "execution_target": MUSE_EXECUTION_TARGET,
            "local_filesystem": False,
            "sandbox": "remote_uncontrolled",
            "turn_status": _safe_code(turn_status),
            "send_attempted": attempted,
            "send_attempted_known": attempted is not None,
            "acceptance": _safe_code(acceptance),
            "safe_to_send": bool(safe_to_send),
            "delivery": delivery,
        }
        truncated = (
            classification == "succeeded"
            and isinstance(assistant_text, str)
            and len(assistant_text) > MAX_MUSE_TEXT_CHARS
        )
        original_length = len(assistant_text) if truncated and isinstance(assistant_text, str) else None
        safe_cause = sanitize_cause(cause)
        if safe_cause:
            safe_result["cause"] = safe_cause
        if classification == "succeeded":
            if isinstance(assistant_text, str) and assistant_text:
                safe_result["assistant_text"] = assistant_text[:MAX_MUSE_TEXT_CHARS]
            if truncated:
                safe_result["assistant_text_truncated"] = True
                safe_result["assistant_text_original_length"] = original_length
            if isinstance(assistant_message_id, str) and assistant_message_id:
                safe_result["assistant_message_id"] = assistant_message_id[:256]
        metadata = {
            "provider": MUSE_PROVIDER,
            "adapter_key": MUSE_ADAPTER_KEY,
            "execution_target": MUSE_EXECUTION_TARGET,
            "local_filesystem": False,
            "sandbox": "remote_uncontrolled",
            "turn_status": _safe_code(turn_status),
            "send_attempted": attempted,
            "send_attempted_known": attempted is not None,
            "acceptance": _safe_code(acceptance),
            "safe_to_send": bool(safe_to_send),
            "role_delivery": delivery,
        }
        if safe_cause:
            metadata["cause"] = safe_cause
        if truncated:
            metadata["assistant_text_truncated"] = True
            metadata["assistant_text_original_length"] = original_length
        return ExecutionOutcome(
            classification=classification,
            success=classification == "succeeded",
            result_summary=("Muse remote turn completed" if classification == "succeeded" else "Muse remote turn requires reconciliation" if classification == "uncertain" else "Muse remote turn was not sent"),
            error_code=_safe_code(error_code) if error_code else None,
            result=safe_result,
            evidence=({"kind": "muse_delivery", "status": _safe_code(turn_status)},),
            metadata=metadata,
        )


MuseExecutionAdapter = MuseRemoteExecutionAdapter
MuseRemoteAdapter = MuseRemoteExecutionAdapter


@dataclass(frozen=True)
class _Correlation:
    status: str
    assistant_text: str | None = None
    assistant_message_id: str | None = None


async def _default_process_runner(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str,
    timeout_seconds: float,
) -> MuseProcessResult:
    started = time.monotonic()
    kwargs: dict[str, Any] = {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "env": dict(env),
        "cwd": cwd,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        process = await asyncio.create_subprocess_exec(*tuple(argv), **kwargs)
    except FileNotFoundError:
        raise
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=max(0.1, timeout_seconds))
    except asyncio.CancelledError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.communicate()
        raise
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.communicate()
        return MuseProcessResult(
            tuple(argv), process.returncode, "", "", True, int((time.monotonic() - started) * 1000),
            classify_cli_stderr("", timed_out=True),
        )
    return MuseProcessResult(
        tuple(argv),
        process.returncode,
        stdout.decode("utf-8", errors="replace"),
        # stderr text is reduced to closed labels here and is never retained or returned.
        "",
        False,
        int((time.monotonic() - started) * 1000),
        classify_cli_stderr(stderr.decode("utf-8", errors="replace") if stderr else ""),
    )


def _child_environment(state_root: Path) -> dict[str, str]:
    environment = build_aoitalk_subprocess_env()
    # Muse's config/cookie lookup is intentionally scoped to this child.
    environment["HOME"] = str(state_root)
    environment["USERPROFILE"] = str(state_root)
    if state_root.drive:
        environment["HOMEDRIVE"] = state_root.drive
        environment["HOMEPATH"] = str(state_root)[len(state_root.drive) :] or "\\"
    for key in tuple(environment):
        if key.upper() in {
            "MUSE_VM_ID",
            "MUSE_NODE_ID",
            "MUSE_COOKIE",
            "MUSE_COOKIES_FILE",
            "AOITALK_SHARED_HOME",
        }:
            environment.pop(key, None)
    return environment


def _prove_dedicated_home(state_root: Path, environment: Mapping[str, str]) -> None:
    """Gate provider execution on a private, state-root-backed home.

    Setting ``HOME`` alone is not sufficient on Windows: Python and the
    upstream CLI can consult ``USERPROFILE`` or the drive/path pair instead.
    Validate every relevant spelling before the first CLI process starts, and
    reject using the operator's actual home as the provider state root.
    """

    expected = _path_key(state_root)
    for key in ("HOME", "USERPROFILE"):
        value = environment.get(key)
        if not value or _path_key(Path(value)) != expected:
            raise MuseCredentialError(MuseErrorCode.DEDICATED_HOME_REQUIRED)
    if state_root.drive:
        drive = str(environment.get("HOMEDRIVE") or "")
        path = str(environment.get("HOMEPATH") or "")
        if drive.casefold() != state_root.drive.casefold() or _path_key(Path(drive + path)) != expected:
            raise MuseCredentialError(MuseErrorCode.DEDICATED_HOME_REQUIRED)
    try:
        if _path_key(Path.home()) == expected:
            raise MuseCredentialError(MuseErrorCode.DEDICATED_HOME_REQUIRED)
    except MuseError:
        raise
    except Exception:
        raise MuseCredentialError(MuseErrorCode.DEDICATED_HOME_REQUIRED) from None


def _validate_state_root(value: Path | str) -> Path:
    try:
        root = Path(value)
    except (TypeError, ValueError):
        raise MuseCredentialError() from None
    if not root.is_absolute() or not root.is_dir():
        raise MuseCredentialError()
    return root


def _validate_allowed_argv(argv: Sequence[str]) -> None:
    # The executable is deployment configuration (tests may inject an
    # absolute fake path); only the subcommand/argument shape is admitted.
    if not argv:
        raise MuseInputError()
    command = str(argv[1]) if len(argv) > 1 else ""
    if command in {"--version", "--help"}:
        return
    if len(argv) == 3 and argv[2] == "--help" and command in {"session-start", "history", "send"}:
        return
    if command == "session-start" and len(argv) == 2:
        return
    if command == "send" and len(argv) == 7 and argv[3] == "--thread" and argv[5] == "--wait":
        return
    if command == "history" and len(argv) == 5 and argv[2] == "--thread" and argv[4] == "--raw":
        return
    raise MuseInputError("unsupported_cli_route")


def _validate_argv_length(argv: Sequence[str], limit: int) -> None:
    try:
        # ``CreateProcessW`` consumes one serialized UTF-16 command line.  A
        # Python character count underestimates astral characters and ignores
        # the quotes/backslash escaping added by Windows argv parsing.
        serialized = subprocess.list2cmdline([str(item) for item in argv])
        serialized_units = len(serialized.encode("utf-16-le", errors="strict")) // 2
    except (UnicodeEncodeError, UnicodeError):
        raise MuseInputError(MuseErrorCode.INPUT_TOO_LONG) from None
    # Include the terminating UTF-16 NUL used by CreateProcessW.  This is a
    # conservative preflight against the documented 32,767 unit limit.
    if serialized_units + 1 > int(limit):
        raise MuseInputError(MuseErrorCode.INPUT_TOO_LONG)


def _annotate(
    exc: MuseError,
    *,
    command: str | None = None,
    started: float | None = None,
    result: MuseProcessResult | None = None,
    cause_type: str | None = None,
) -> MuseError:
    """Attach closed-vocabulary process evidence to an error without overwriting earlier evidence."""

    if exc.command is None and command:
        exc.command = command
    if exc.duration_ms is None:
        if result is not None:
            exc.duration_ms = max(0, int(result.duration_ms or 0))
        elif started is not None:
            exc.duration_ms = max(0, int((time.monotonic() - started) * 1000))
    if result is not None:
        if exc.returncode is None and result.returncode is not None:
            exc.returncode = int(result.returncode)
        if exc.diagnostics is None and result.diagnostics is not None:
            exc.diagnostics = dict(result.diagnostics)
        if result.timed_out:
            exc.timed_out = True
    if cause_type and exc.cause_type is None:
        exc.cause_type = closed_exception_type(cause_type)
    return exc


def _error_from_process(result: MuseProcessResult) -> MuseError:
    if result.timed_out or result.returncode == 4:
        error: MuseError = MuseTimeoutError()
    elif result.returncode == 2:
        error = MuseAuthorizationError()
    elif result.returncode == 3:
        error = MuseGatewayError()
    elif result.returncode in (None, 127):
        error = MuseProcessError(MuseErrorCode.CLI_UNAVAILABLE)
    else:
        error = MuseProcessError()
    return _annotate(error, command=command_label(result.argv), result=result)


def _cause_from_exception(exc: BaseException) -> dict[str, Any]:
    """Closed cause record for an exception raised at the CLI boundary (no message text)."""

    if isinstance(exc, MuseError):
        return build_cause(
            cause_code=exc.code, command=exc.command, returncode=exc.returncode, duration_ms=exc.duration_ms,
            timed_out=exc.timed_out, diagnostics=exc.diagnostics, cause_type=exc.cause_type,
        )
    return build_cause(cause_code="unhandled_exception", cause_type=type(exc).__name__)


def _parse_json_payload(text: str, *, allow_empty: bool) -> Any:
    value = str(text or "").strip()
    if not value:
        if allow_empty:
            return {}
        raise MuseProtocolError()
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        lines: list[Any] = []
        for line in value.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                raise MuseProtocolError() from None
        if not lines:
            raise MuseProtocolError()
        return lines[0] if len(lines) == 1 else lines


def _extract_history_events(payload: Any) -> list[Mapping[str, Any]]:
    # The v0.3.2 CLI returns the raw ``chat.history`` object.  Accepting the
    # old generic ``events``/``messages`` wrappers here makes a malformed or
    # unrelated provider response look like a valid history snapshot.
    if not isinstance(payload, Mapping):
        raise MuseProtocolError()
    value = payload.get("chat_events")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise MuseProtocolError()
    if any(not isinstance(item, Mapping) for item in value):
        raise MuseProtocolError()
    return [dict(item) for item in value[:MAX_HISTORY_EVENTS]]


def _event_signature(event: Mapping[str, Any]) -> str:
    try:
        raw = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        raw = repr(sorted((str(key), str(value)) for key, value in event.items()))
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _event_sequence(event: Mapping[str, Any]) -> int | None:
    for key in ("seq", "sequence", "event_seq", "index"):
        value = event.get(key)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _events_after_baseline(current: MuseHistorySnapshot, baseline: MuseHistorySnapshot) -> tuple[Mapping[str, Any], ...]:
    if baseline.max_sequence is not None and any(_event_sequence(item) is not None for item in current.events):
        # Once the provider supplies a durable sequence, unsequenced entries
        # cannot be safely placed after the baseline.  Dropping them avoids
        # treating an old/proactive event as this turn's response.
        return tuple(item for item in current.events if _event_sequence(item) is not None and int(_event_sequence(item) or -1) > baseline.max_sequence)
    remaining = Counter(baseline.signatures)
    selected: list[Mapping[str, Any]] = []
    for event in current.events:
        signature = _event_signature(event)
        if remaining.get(signature, 0):
            remaining[signature] -= 1
        else:
            selected.append(event)
    return tuple(selected)


def _correlate_history(current: MuseHistorySnapshot, baseline: MuseHistorySnapshot, nonce: str) -> _Correlation:
    events = _events_after_baseline(current, baseline)
    user_indexes = [index for index, event in enumerate(events) if _event_is_user(event) and _event_contains(event, nonce)]
    if len(user_indexes) != 1:
        return _Correlation("uncertain")
    user_index = user_indexes[0]
    user = events[user_index]
    user_id = _event_id(user)
    competing = False
    ambiguous_assistant = False
    nonterminal_assistant = False
    assistant_candidates: list[tuple[int, str, str | None]] = []
    for index in range(user_index + 1, len(events)):
        event = events[index]
        if _event_is_user(event):
            competing = True
            break
        if not _event_is_assistant(event):
            continue
        # v0.3.2 marks proactive/background assistant events by making them
        # self-referential.  Ignore those events, while treating every other
        # non-empty reply_to value as an ambiguous competing response.
        if _event_is_proactive(event):
            continue
        if _event_reply_to(event):
            ambiguous_assistant = True
            continue
        text = _event_text(event)
        if not text:
            continue
        if not _assistant_is_terminal(event):
            status = _assistant_status(event)
            if status in {None, "nonterminal"}:
                nonterminal_assistant = True
            else:
                # Empty or unknown provider state must never become success.
                ambiguous_assistant = True
            continue
        assistant_candidates.append((index, text, _event_id(event)))
    if competing:
        return _Correlation("uncertain")
    if ambiguous_assistant or len(assistant_candidates) != 1:
        if nonterminal_assistant and not assistant_candidates and not ambiguous_assistant:
            return _Correlation("nonterminal")
        if not assistant_candidates and not ambiguous_assistant and not nonterminal_assistant:
            return _Correlation("nonterminal")
        return _Correlation("uncertain")
    _assistant_index, assistant_text, assistant_message_id = assistant_candidates[0]
    return _Correlation(
        "confirmed",
        assistant_text=assistant_text,
        assistant_message_id=assistant_message_id,
    )


def _assistant_targets_user(event: Mapping[str, Any], *, user_id: str | None, nonce: str) -> bool:
    """Return true only for an assistant with an explicit turn relation."""

    reply_to = _event_reply_to(event)
    if reply_to:
        return bool((user_id and reply_to == user_id) or (not user_id and nonce and nonce in reply_to))
    # Some Muse payloads carry the nonce in a turn object instead of exposing
    # a reply_to field.  That remains defensible because the nonce is unique.
    return bool(nonce and _event_contains(event, nonce))


def _event_is_user(event: Mapping[str, Any]) -> bool:
    return _event_name(event) == "message.user"


def _event_is_assistant(event: Mapping[str, Any]) -> bool:
    return _event_name(event) == "message.assistant"


def _event_is_proactive(event: Mapping[str, Any]) -> bool:
    if not _event_is_assistant(event):
        return False
    reply_to = _event_reply_to(event)
    message_id = _event_id(event)
    return bool(reply_to and message_id and reply_to == message_id)


def _event_role(event: Mapping[str, Any]) -> str:
    value = event.get("role") or event.get("sender_role") or event.get("author_role")
    for nested_key in ("message", "author", "sender", "actor"):
        nested = event.get(nested_key)
        if isinstance(nested, Mapping):
            value = value or nested.get("role") or nested.get("type")
    return str(value or "").casefold().strip()


def _event_type(event: Mapping[str, Any]) -> str:
    value = event.get("event_name") or event.get("type") or event.get("event") or event.get("event_type") or event.get("kind") or ""
    return str(value).casefold().strip()


def _event_name(event: Mapping[str, Any]) -> str:
    return str(event.get("event_name") or "").casefold().strip()


def _event_payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    value = event.get("payload")
    return value if isinstance(value, Mapping) else {}


def _event_text(event: Mapping[str, Any]) -> str:
    payload = _event_payload(event)
    for key in ("display_text", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _event_id(event: Mapping[str, Any]) -> str | None:
    for source in (event, _event_payload(event)):
        for key in ("message_id", "messageId", "id"):
            value = source.get(key)
            if isinstance(value, Mapping):
                value = value.get("message_id") or value.get("messageId") or value.get("id")
            if value not in (None, ""):
                return str(value)
    return None


def _event_reply_to(event: Mapping[str, Any]) -> str | None:
    for source in (event, _event_payload(event)):
        for key in ("reply_to_message_id", "replyToMessageId", "reply_to"):
            value = source.get(key)
            if isinstance(value, Mapping):
                value = value.get("message_id") or value.get("messageId") or value.get("id")
            if value not in (None, ""):
                return str(value)
    return None


def _is_terminal_event(event: Mapping[str, Any]) -> bool:
    status = _event_payload(event).get("status")
    return isinstance(status, str) and status.casefold().replace("-", "_") in _TERMINAL_STATES


def _assistant_status(event: Mapping[str, Any]) -> str | None:
    payload = _event_payload(event)
    if "status" not in payload:
        return None
    value = payload.get("status")
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    normalized = value.casefold().replace("-", "_").strip()
    if normalized in _TERMINAL_STATES:
        return "completed"
    if normalized in _NON_TERMINAL_STATES:
        return "nonterminal"
    return "unknown"


def _assistant_is_terminal(event: Mapping[str, Any]) -> bool:
    status = _assistant_status(event)
    return status == "completed"


def _terminal_matches_turn(
    event: Mapping[str, Any],
    *,
    nonce: str,
    user_id: str | None,
    assistant_id: str | None,
) -> bool:
    """Reject a terminal marker that explicitly belongs to another turn."""

    if nonce and _event_contains(event, nonce):
        return True
    reply_to = _event_reply_to(event)
    if reply_to:
        return reply_to in {item for item in (user_id, assistant_id) if item}
    explicit_refs = _event_target_references(event)
    if explicit_refs:
        return bool({item for item in (user_id, assistant_id) if item}.intersection(explicit_refs))
    # Muse history commonly emits an unqualified ``turn.completed`` event
    # after a defensibly linked assistant message.  The caller only reaches
    # this fallback after rejecting every unlinked assistant event.
    return True


def _event_target_references(event: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in (
        "user_id",
        "userId",
        "assistant_id",
        "assistantId",
        "target_id",
        "targetId",
        "turn_id",
        "turnId",
    ):
        value = event.get(key)
        if isinstance(value, Mapping):
            value = value.get("id") or value.get("message_id") or value.get("messageId")
        if value not in (None, ""):
            values.add(str(value))
    return values


def _event_contains(value: Any, needle: str) -> bool:
    if isinstance(value, str):
        return needle in value
    if isinstance(value, Mapping):
        return any(_event_contains(item, needle) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return any(_event_contains(item, needle) for item in value)
    return False


def _compose_turn_text(prompt: str, *, nonce: str, role: ManagedRoleContext | None, first_turn: bool) -> str:
    if not prompt or not nonce:
        raise MuseInputError()
    prefix = f"[aoitalk-turn:{nonce}]"
    if role is not None and first_turn:
        prefix = (
            "[AoiTalk managed role context]\n"
            f"role_revision_id: {role.revision_id}\n"
            f"role_context_sha256: {role.role_context_sha256}\n"
            f"{role.text}\n"
            "[/AoiTalk managed role context]\n\n"
            + prefix
        )
    value = f"{prefix}\n{prompt}"
    if len(value) > MAX_MUSE_TEXT_CHARS:
        raise MuseInputError(MuseErrorCode.INPUT_TOO_LONG)
    return value


def _role_receipt(binding: MuseBinding) -> dict[str, Any]:
    if binding.role_mode == "managed_role":
        return {
            "mode": "managed_role",
            "mechanism": "first_turn_user_prefix",
            "role_revision_id": binding.role_revision_id,
            "role_context_sha256": binding.role_context_sha256,
            "system_prompt_applied": False,
            "native_persona_replaced": False,
            "adherence_guaranteed": False,
            "limitation": "Muse CLI has no system prompt flag; role text was sent in the first user turn.",
        }
    return _raw_role_receipt()


def _raw_role_receipt() -> dict[str, Any]:
    return {
        "mode": "raw",
        "mechanism": "none",
        "system_prompt_applied": False,
        "native_persona_replaced": False,
        "adherence_guaranteed": False,
    }


def _binding_mismatch(binding: MuseBinding, context: MuseCredentialResolution, role: ManagedRoleContext | None) -> str | None:
    if binding.owner_id != context.owner_id or binding.credential_ref != context.credential_ref or binding.vm_id != context.vm_id or binding.node_id != context.node_id:
        return MuseErrorCode.THREAD_BINDING_MISMATCH
    if binding.auth_fingerprint != context.auth_fingerprint:
        return MuseErrorCode.CURRENT_AUTH_MISMATCH
    if context.agent_id and binding.agent_id != context.agent_id:
        return MuseErrorCode.CURRENT_AUTH_MISMATCH
    if context.revision_id and binding.revision_id != context.revision_id:
        return MuseErrorCode.ROLE_REVISION_MISMATCH
    if context.remote_target_key and binding.remote_target_key and binding.remote_target_key != context.remote_target_key:
        return MuseErrorCode.THREAD_BINDING_MISMATCH
    expected_mode = "managed_role" if role else "raw"
    if binding.role_mode != expected_mode:
        return MuseErrorCode.ROLE_REVISION_MISMATCH
    if role and (binding.role_revision_id != role.revision_id or binding.role_context_sha256 != role.role_context_sha256):
        return MuseErrorCode.ROLE_REVISION_MISMATCH
    return None


def _binding_send_attempted(binding: MuseBinding | None) -> bool | None:
    """Evidence-backed answer to "was a send attempted?" for a binding that has no turn nonce.

    ``True``  - the durable ``send_attempted`` boundary was written (right before the CLI send).
    ``False`` - the thread was never created (create intent / pending id).  The turn nonce is only
                persisted after the thread exists, so no send can have happened.
    ``None``  - the record cannot prove either way (no binding, bound or unsafe with a real thread).
                Callers must treat it as unknown and never as "attempted".
    """

    if binding is None:
        return None
    if binding.state == _TURN_ATTEMPTED:
        return True
    if binding.state == _THREAD_CREATE_PENDING or binding.thread_id.startswith(_PENDING_THREAD_PREFIX):
        return False
    return None


def _attempted_for_recovery(binding: MuseBinding) -> bool | None:
    """send_attempted for a recovery outcome: unchanged inference while a turn nonce is pinned,
    durable evidence only (never a guess of ``True``) when there is no nonce."""

    if binding.current_turn_nonce:
        return binding.state in {_TURN_ATTEMPTED, "unsafe"}
    return _binding_send_attempted(binding)


def _binding_needs_recovery(binding: MuseBinding) -> bool:
    return bool(
        binding.current_turn_nonce
        or binding.state in {_THREAD_CREATE_PENDING, _TURN_PINNED, _TURN_ATTEMPTED, "unsafe"}
        or binding.thread_id.startswith(_PENDING_THREAD_PREFIX)
    )


def _binding_blocks_recovery(binding: MuseBinding, claim: Any) -> bool:
    """True only for the facts that make history-only recovery unsafe.

    Recovery never resends, so a same-run ambiguous send (timeout, acceptance
    unconfirmed) must stay recoverable by reading history.  A binding is
    blocked only when its run pin is missing (legacy row) or belongs to a
    different run, and when the local send boundary itself was cancelled.
    """

    if not _binding_run_matches(binding, claim):
        return True
    if not _bounded_identifier(binding.current_run_id):
        return True
    return str(binding.unsafe_reason or "") == MuseErrorCode.SEND_CANCELLED_LOCAL


async def _call_port(port: Any, *args: Any, **kwargs: Any) -> Any:
    callback = getattr(port, "resolve", None) if not callable(port) else port
    if callback is None:
        raise MuseCredentialError()
    try:
        signature = inspect.signature(callback)
        if not any(item.kind is inspect.Parameter.VAR_KEYWORD for item in signature.parameters.values()):
            kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
    except (TypeError, ValueError):
        pass
    value = callback(*args, **kwargs)
    return await value if inspect.isawaitable(value) else value


async def _store_call(store: Any, names: Sequence[str], *args: Any, **kwargs: Any) -> Any:
    callback = next((getattr(store, name, None) for name in names if callable(getattr(store, name, None))), None)
    if callback is None:
        raise MuseError(MuseErrorCode.BINDING_STORE_UNAVAILABLE)
    try:
        signature = inspect.signature(callback)
        if not any(item.kind is inspect.Parameter.VAR_KEYWORD for item in signature.parameters.values()):
            kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
    except (TypeError, ValueError):
        pass
    value = callback(*args, **kwargs)
    return await value if inspect.isawaitable(value) else value


def _coerce_resolution(value: Any, *, owner_id: str) -> MuseCredentialResolution:
    if isinstance(value, MuseCredentialResolution):
        return value
    if not isinstance(value, Mapping):
        raise MuseCredentialError()
    try:
        return MuseCredentialResolution(
            owner_id=str(value.get("owner_id") or owner_id),
            # The resolver owns this opaque reference.  A missing value is
            # rejected by ``normalized`` rather than filled from claim data.
            credential_ref=str(value.get("credential_ref") or ""),
            state_root=value.get("state_root"),
            vm_id=str(value.get("vm_id") or ""),
            node_id=str(value.get("node_id")) if value.get("node_id") not in (None, "") else None,
            auth_fingerprint=str(value.get("auth_fingerprint") or value.get("credential_identity_hash") or ""),
            authenticated=bool(value.get("authenticated")),
            remote_target_enabled=bool(value.get("remote_target_enabled", value.get("target_enabled", value.get("enabled", False)))),
            agent_id=str(value.get("agent_id") or value.get("assigned_agent_id")) if value.get("agent_id") or value.get("assigned_agent_id") else None,
            revision_id=str(value.get("revision_id") or value.get("agent_revision_id")) if value.get("revision_id") or value.get("agent_revision_id") else None,
            remote_target_key=str(value.get("remote_target_key") or value.get("target_key")) if value.get("remote_target_key") or value.get("target_key") else None,
        )
    except Exception:
        raise MuseCredentialError() from None


def _merge_resolution(base: MuseCredentialResolution, value: Any) -> MuseCredentialResolution:
    if isinstance(value, MuseCredentialResolution):
        extra = value
        return replace(
            base,
            state_root=extra.state_root or base.state_root,
            vm_id=extra.vm_id or base.vm_id,
            node_id=extra.node_id or base.node_id,
            auth_fingerprint=extra.auth_fingerprint or base.auth_fingerprint,
            authenticated=extra.authenticated,
            remote_target_enabled=extra.remote_target_enabled,
            agent_id=extra.agent_id or base.agent_id,
            revision_id=extra.revision_id or base.revision_id,
            remote_target_key=extra.remote_target_key or base.remote_target_key,
        )
    if not isinstance(value, Mapping):
        raise MuseCredentialError()
    return replace(
        base,
        state_root=value.get("state_root") or base.state_root,
        vm_id=str(value.get("vm_id") or base.vm_id),
        node_id=str(value.get("node_id")) if value.get("node_id") not in (None, "") else base.node_id,
        auth_fingerprint=str(value.get("auth_fingerprint") or value.get("credential_identity_hash") or base.auth_fingerprint),
        authenticated=bool(value.get("authenticated", base.authenticated)),
        remote_target_enabled=bool(value.get("remote_target_enabled", value.get("target_enabled", value.get("enabled", base.remote_target_enabled)))),
        agent_id=str(value.get("agent_id") or value.get("assigned_agent_id") or base.agent_id) if value.get("agent_id") or value.get("assigned_agent_id") or base.agent_id else None,
        revision_id=str(value.get("revision_id") or value.get("agent_revision_id") or base.revision_id) if value.get("revision_id") or value.get("agent_revision_id") or base.revision_id else None,
        remote_target_key=str(value.get("remote_target_key") or value.get("target_key") or base.remote_target_key) if value.get("remote_target_key") or value.get("target_key") or base.remote_target_key else None,
    )


def _claim_metadata(claim: Any) -> Mapping[str, Any]:
    value = _claim_value(claim, "metadata", {})
    return value if isinstance(value, Mapping) else {}


def _claim_value(claim: Any, name: str, default: Any = None) -> Any:
    if isinstance(claim, Mapping):
        return claim.get(name, default)
    return getattr(claim, name, default)


def _claim_run_id(claim: Any) -> str | None:
    metadata = _claim_metadata(claim)
    raw = (
        metadata.get("current_run_id")
        or _claim_value(claim, "current_run_id", None)
        or _claim_value(claim, "run_id", None)
        or _claim_value(claim, "source_id", None)
    )
    return _bounded_identifier(raw) if raw else None


def _binding_run_matches(binding: MuseBinding, claim: Any) -> bool:
    claim_run_id = _claim_run_id(claim)
    binding_run_id = _bounded_identifier(binding.current_run_id) if binding.current_run_id else None
    return bool(claim_run_id and binding_run_id and claim_run_id == binding_run_id)


def _authority_value(authority: Any, *names: str) -> Any:
    """Read immutable authority fields without promoting claim aliases."""

    if authority is None:
        return None
    for name in names:
        value = _claim_value(authority, name, None)
        if value not in (None, ""):
            return value
    nested = _claim_value(authority, "current_authority", None)
    if nested is not None and nested is not authority:
        return _authority_value(nested, *names)
    return None


def _authority_allowlist(authority: Any, *names: str) -> set[str] | None:
    if authority is None:
        return None
    for name in names:
        marker = object()
        value = _claim_value(authority, name, marker)
        if value is marker:
            continue
        if isinstance(value, str):
            values = {value}
        elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
            values = {str(item) for item in value}
        elif isinstance(value, Mapping):
            values = {str(key) for key, enabled in value.items() if enabled}
        else:
            values = set()
        return {item.strip() for item in values if item and item.strip()}
    nested = _claim_value(authority, "current_authority", None)
    if nested is not None and nested is not authority:
        return _authority_allowlist(nested, *names)
    return None


def _input_text(metadata: Mapping[str, Any]) -> str:
    for key in ("prompt", "input_text", "text", "user_text", "message"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _find_string(value: Any, keys: Sequence[str]) -> str | None:
    if isinstance(value, Mapping):
        for key in keys:
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
        for item in value.values():
            found = _find_string(item, keys)
            if found:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            found = _find_string(item, keys)
            if found:
                return found
    return None


def _extract_version(value: str) -> str | None:
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", str(value or ""))
    return match.group(1) if match else None


def _bounded_identifier(value: Any, *, max_length: int = 256) -> str:
    text = str(value or "").strip()
    if len(text) > max_length or not text or not _IDENTIFIER_RE.fullmatch(text):
        return ""
    return text


def _safe_hash(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if _SHA256_RE.fullmatch(text) else ""


def _safe_identity(value: Any) -> str:
    text = str(value or "").strip()
    if _SHA256_RE.fullmatch(text.lower()):
        return text.lower()
    return _bounded_identifier(text)


def _safe_code(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("-", "_")
    return text[:96] if re.fullmatch(r"[a-z0-9_.:]+", text[:96]) else "muse_error"


def _path_key(value: Path) -> str:
    return os.path.normcase(os.path.abspath(str(value)))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "EgressGate",
    "ExecutionOutcome",
    "InMemoryMuseBindingStore",
    "ManagedRoleContext",
    "MuseAuthorizationError",
    "MuseBinding",
    "MuseBindingError",
    "MuseBindingStore",
    "MuseCredentialError",
    "MuseCredentialResolution",
    "MuseError",
    "MuseErrorCode",
    "MuseGatewayError",
    "MuseHistorySnapshot",
    "MuseInputError",
    "MuseProcessError",
    "MuseProcessResult",
    "MuseProtocolError",
    "MuseProviderDescriptor",
    "MuseRemoteAdapter",
    "MuseRemoteClient",
    "MuseRemoteExecutionAdapter",
    "MuseRemoteTarget",
    "MuseSendReceipt",
    "MuseSessionStart",
    "MuseTimeoutError",
    "MuseTurnStatus",
    "MuseExecutionAdapter",
    "MUSE_ADAPTER_KEY",
    "MUSE_CLI_VERSION",
    "MUSE_EXECUTION_TARGET",
    "MUSE_EGRESS_DESCRIPTOR",
    "MUSE_PROVIDER",
    "MUSE_PROVIDER_DESCRIPTOR",
    "MUSE_REMOTE_PROVIDER_DESCRIPTOR",
    "REMOTE_AGENT_DELEGATE",
]
