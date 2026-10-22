"""Common AgentWork adapter for trusted Windows CLI session turns.

This adapter deliberately has no queue and no process implementation.  It is
registered on the shared :class:`AgentWorkCoordinator`; the only provider
process entry point is ``CLIBackendBase.execute_prompt`` (with its per-call
``CLIInvocationOptions``).  A failed native resume is reported as disconnected
and is never converted into a fresh start or a stateless retry.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .agent_work_runtime import ExecutionOutcome, WorkCandidate, WorkClaim
from .cli_development_sessions import (
    CLI_DEVELOPMENT_CAPABILITIES,
    CLIDevelopmentSessionInvalid,
    normalize_cli_capabilities,
    pinned_cli_capabilities,
    CLI_SESSION_ADAPTER_KEY,
    CLI_SESSION_SOURCE_TYPE,
    CLIDevelopmentSessionService,
    _sha256,
)

from ..llm.cli_backends.base import CLIInvocationOptions


_URL_CREDENTIAL_PATTERN = re.compile(r"(?i)(://)[^\s]*@")
_CONTROL_CHAR_RE = re.compile(r"[\r\n\t\x00-\x1f\x7f-\x9f]+")
_THOUGHT_BLOCK_RE = re.compile(r"(?is)<(?P<tag>thought|analysis)\b[^>]*>.*?</(?P=tag)>")
_UNCLOSED_THOUGHT_RE = re.compile(r"(?is)<(?:thought|analysis)\b[^>]*>")
_PRIVATE_KEY_RE = re.compile(r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----")


def _sanitize_cli_error_diagnostic(raw_error: Any, *, limit: int = 512) -> str:
    """Return a bounded, secret-redacted provider failure diagnostic."""
    if raw_error is None:
        return "CLI execution failed"
    text = str(raw_error).strip()
    if not text:
        return "CLI execution failed"

    # Remove internal thought/analysis blocks and private keys first.
    # If an unclosed thought/analysis start tag remains, fail closed to generic error.
    text = _THOUGHT_BLOCK_RE.sub(" ", text)
    if _UNCLOSED_THOUGHT_RE.search(text):
        return "CLI execution failed"
    text = _PRIVATE_KEY_RE.sub("[REDACTED_KEY]", text)

    # Redact URL credentials (://user:pass@)
    text = _URL_CREDENTIAL_PATTERN.sub(r"\1<credentials>@", text)

    # Apply existing secret redaction rule (fail closed on unexpected redactor failure)
    try:
        from .outbound_privacy_service import redact_secret_for_local_display

        text = str(redact_secret_for_local_display(text))
    except Exception:
        return "CLI execution failed"

    # Ensure bearer/token/api_key/secret patterns are scrubbed if not caught
    text = re.sub(
        r"""(?ix)
            \b(?P<key>password|passwd|token|secret|api[_-]?key|client[_-]?secret|authorization)\b
            \s*[:=]\s*
            (?:
                (?P<quote>[\"']) (?P<quoted>.*?) (?P=quote)
                |
                (?P<unquoted>[^\s,;]+)
            )
        """,
        lambda match: f"{match.group('key')}=<redacted>",
        text,
    )
    text = re.sub(r"(?i)\b(bearer\s+)[a-z0-9_\-\.]{8,}\b", r"\1<redacted>", text)

    # Collapse whitespace and control characters to a single bounded line
    text = _CONTROL_CHAR_RE.sub(" ", text)
    text = " ".join(text.split()).strip()
    if not text:
        return "CLI execution failed"

    if len(text) > limit:
        return text[: max(0, limit - 3)].rstrip() + "..."
    return text


class CLISessionWorkError(RuntimeError):
    """Adapter boundary error with a safe classification/code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = str(code or "cli_session_work_error")[:96]
        super().__init__(message or self.code)


class CLISessionTurnWorkSource:
    """Source contract for already-admitted CLI session turns."""

    source_type = CLI_SESSION_SOURCE_TYPE

    def __init__(self, service: CLIDevelopmentSessionService):
        self.service = service

    async def discover(self, session: Any, *, now: Any = None) -> Sequence[WorkCandidate]:
        # API admission creates a Work row directly.  Discovery must never
        # replay a send or synthesize a second user message.
        return ()

    async def refresh(self, session: Any, claim: WorkClaim) -> bool:
        db = session
        owns = db is None
        if db is None:
            db = await self.service._session()
        try:
            from ..memory.models import AgentRun, AgentWorkItem, ConversationSession

            work = await db.get(AgentWorkItem, uuid.UUID(str(claim.work_item_id)))
            if work is None or str(work.source_type or "") != CLI_SESSION_SOURCE_TYPE or str(work.execution_adapter or "") != CLI_SESSION_ADAPTER_KEY:
                return False
            run = await db.get(AgentRun, work.active_agent_run_id) if work.active_agent_run_id else None
            conversation = await db.get(ConversationSession, work.conversation_session_id)
            if conversation is None or run is None:
                # A pending API Work may not be linked until the coordinator
                # creates the attempt Run.  Identity is still checked from
                # the Work row and owner marker.
                context = self.service._context(conversation) if conversation is not None else {}
                return conversation is not None and str(work.assigned_agent_id) == str(claim.assigned_agent_id) and str(context.get("owner_id") or work.metadata_json.get("owner_id")) == str(work.metadata_json.get("owner_id"))
            return (
                str(work.assigned_agent_id) == str(claim.assigned_agent_id)
                and str(work.agent_revision_id) == str(claim.agent_revision_id)
                and str(run.session_id) == str(work.conversation_session_id)
                and str(run.agent_id) == str(work.assigned_agent_id)
                and str(run.agent_revision_id) == str(work.agent_revision_id)
                and str(run.run_type or "") in {CLI_SESSION_SOURCE_TYPE, "autonomous_work"}
                and str((work.metadata_json or {}).get("owner_id") or "") == str(claim.metadata.get("owner_id") or "")
            )
        except (TypeError, ValueError, AttributeError):
            return False
        finally:
            if owns:
                await self.service._close(db)


class CLISessionWorkAdapter:
    """Execute one admitted native CLI turn through the shared Work lane."""

    adapter_key = CLI_SESSION_ADAPTER_KEY

    def __init__(
        self,
        service: CLIDevelopmentSessionService,
        *,
        backend_factory: Any | None = None,
        session_store: Any | None = None,
    ) -> None:
        self.service = service
        self.backend_factory = backend_factory
        self.session_store = session_store or getattr(service, "session_store", None)
        if self.session_store is None:
            from ..llm.cli_sessions import CLISessionStore

            self.session_store = CLISessionStore()
        self._active: dict[str, tuple[Any, Any]] = {}
        self._active_lock = threading.Lock()

    def required_capabilities(self, claim: WorkClaim) -> tuple[str, ...]:
        declared = tuple(str(item).strip() for item in (claim.required_capabilities or ()) if str(item).strip())
        return declared or CLI_DEVELOPMENT_CAPABILITIES

    @staticmethod
    def _claim_value(claim: Any, name: str, default: Any = None) -> Any:
        if isinstance(claim, Mapping):
            return claim.get(name, default)
        return getattr(claim, name, default)

    async def _backend(self, *, provider: str, model: str, effort: str, options: CLIInvocationOptions) -> Any:
        factory = self.backend_factory
        if factory is None:
            provider_key = provider.casefold().replace("_", "-")
            if provider_key in {"claude", "claude-cli", "claudecode"}:
                from ..llm.cli_backends.claude import ClaudeCLIBackend

                return ClaudeCLIBackend(model=model, reasoning_effort=effort or None)
            if provider_key in {"codex", "codex-cli"}:
                from ..llm.cli_backends.codex import CodexCLIBackend

                return CodexCLIBackend(model=model, reasoning_effort=effort or None)
            if provider_key in {"grok", "grok-cli"}:
                from ..llm.cli_backends.grok import GrokCLIBackend

                return GrokCLIBackend(model=model)
            if provider_key in {"antigravity", "agy", "antigravity-cli"}:
                from ..llm.cli_backends.antigravity import AntigravityCLIBackend

                return AntigravityCLIBackend(model=model)
            raise CLISessionWorkError("cli_provider_unavailable", f"unsupported CLI provider: {provider}")
        maker = getattr(factory, "create", None)
        if callable(maker):
            value = maker(provider=provider, model=model, effort=effort, execution_options=options)
        elif callable(factory):
            value = factory(provider=provider, model=model, effort=effort, execution_options=options)
        else:
            value = factory
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    def _native_scope(service: CLIDevelopmentSessionService, *, session_id: str, context: Mapping[str, Any], run_id: str) -> Any:
        from ..llm.cli_sessions import CLINativeSessionScope, fingerprint_settings

        cwd = str(context.get("working_directory") or "")
        provider = str(context.get("provider") or "")
        model = str(context.get("model") or "")
        effort = str(context.get("effort") or "")
        proof = {
            "provider": provider,
            "model": model,
            "effort": effort,
            "working_directory": cwd,
            "agent_id": context.get("agent_id"),
            "agent_revision_id": context.get("agent_revision_id"),
            "binding_id": context.get("binding_id"),
            "binding_snapshot_hash": context.get("binding_snapshot_hash"),
        }
        return CLINativeSessionScope(
            chat_session_id=session_id,
            branch_key="cli_development",
            provider=provider,
            model=model,
            project_key=str(context.get("project_id") or ""),
            working_directory=cwd,
            fingerprint=fingerprint_settings(proof),
            agent_run_id=run_id,
            agent_revision_id=str(context.get("agent_revision_id") or "") or None,
            binding_snapshot_hash=str(context.get("binding_snapshot_hash") or "") or None,
            capability_manifest_digest=_sha256(json.dumps(list(pinned_cli_capabilities(context)), separators=(",", ":"))),
            authority_digest=_sha256(json.dumps(context.get("authority") or {}, sort_keys=True, default=str)),
            policy_fingerprint=_sha256(json.dumps(context.get("policy") or {}, sort_keys=True, default=str)),
            provider_cache_root=str(context.get("provider_cache_root") or "") or None,
        )

    async def _invoke_backend(
        self,
        backend: Any,
        prompt: str,
        *,
        run_id: str,
        options: CLIInvocationOptions,
        event_callback: Callable[[str, Mapping[str, Any]], Any],
        cancel_handle: Any,
    ) -> tuple[bool, str]:
        execute = getattr(backend, "execute_prompt", None)
        if not callable(execute):
            raise CLISessionWorkError("cli_backend_invalid", "CLI backend has no execute_prompt")
        invocation = options
        if options.allowed_capabilities is not None:
            # A narrowed ceiling must reach the provider; never let signature
            # filtering silently run the default tools instead.
            try:
                params = inspect.signature(execute).parameters
                accepts = "invocation_options" in params or any(item.kind is inspect.Parameter.VAR_KEYWORD for item in params.values())
            except (TypeError, ValueError):
                accepts = False
            if not accepts or not getattr(backend, "supports_capability_ceiling", False):
                raise CLISessionWorkError("cli_capability_ceiling_unenforceable", "CLI backend cannot enforce the pinned capability ceiling")
        validate = getattr(backend, "validate_invocation_options", None)
        if callable(validate):
            validate(invocation)
        kwargs: dict[str, Any] = {
            "cwd": Path(options.cwd) if options.cwd is not None else None,
            "timeout": None,
            "extra_args": [],
            "system_context": None,
            "event_callback": event_callback,
            "native_session_id": options.native_session_id,
            "native_session_action": options.native_session_action,
            "ephemeral": False,
            "invocation_options": invocation,
        }
        try:
            signature = inspect.signature(execute)
            params = signature.parameters
            if not any(item.kind is inspect.Parameter.VAR_KEYWORD for item in params.values()):
                kwargs = {key: value for key, value in kwargs.items() if key in params}
        except (TypeError, ValueError):
            pass

        def operation() -> tuple[bool, str]:
            # The only process boundary is the backend implementation.  The
            # raw prompt is passed as-is and no system context is synthesized.
            return execute(prompt, **kwargs)

        from ..llm.generation_cancellation import (
            GenerationCancelled,
            register_generation_cancellation,
            release_generation_cancellation,
            reset_current_generation_cancellation,
            run_owned_cli_worker,
            set_current_generation_cancellation,
        )

        owned = register_generation_cancellation(str(run_id))
        token = set_current_generation_cancellation(owned)
        callback_remove = None
        try:
            stop = getattr(backend, "cancel_active_execution", None) or getattr(backend, "cancel", None) or getattr(backend, "stop", None)
            if owned is not None and callable(stop):
                callback_remove = owned.add_cancel_callback(stop)
            return await run_owned_cli_worker(operation)
        except GenerationCancelled:
            raise
        finally:
            if callback_remove is not None:
                callback_remove()
            reset_current_generation_cancellation(token)
            release_generation_cancellation(owned)

    async def execute(self, claim: WorkClaim, *, coordinator: Any, run_id: str | None = None, run: Mapping[str, Any] | None = None, **_: Any) -> ExecutionOutcome:
        work_id = str(self._claim_value(claim, "work_item_id") or "")
        lease_owner = str(self._claim_value(claim, "lease_owner") or "") or None
        lease_token = str(self._claim_value(claim, "lease_token") or "") or None
        claim_attempt = int(self._claim_value(claim, "attempt", 0) or 0) or None
        if str(self._claim_value(claim, "source_type") or "") != CLI_SESSION_SOURCE_TYPE or str(self._claim_value(claim, "execution_adapter") or "") != CLI_SESSION_ADAPTER_KEY:
            return ExecutionOutcome(classification="blocked", error_code="cli_session_binding_invalid")
        if int(self._claim_value(claim, "attempt", 1) or 1) != 1:
            return ExecutionOutcome(classification="blocked", error_code="cli_session_retry_forbidden")
        run_id_text = str(run_id or (run or {}).get("id") or "")
        if not run_id_text:
            return ExecutionOutcome(classification="uncertain", error_code="cli_session_run_missing")
        db = await self.service._session()
        lease = None
        backend = None
        try:
            from ..memory.models import AgentRun, AgentWorkItem, ConversationSession

            async def record_result(**kwargs: Any) -> dict[str, Any]:
                return await self.service.record_execution_result(
                    session_id=str(conversation.id),
                    run_id=run_id_text,
                    work_item_id=work_id,
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                    attempt=claim_attempt,
                    **kwargs,
                )

            work = await db.get(AgentWorkItem, uuid.UUID(work_id))
            run_row = await db.get(AgentRun, uuid.UUID(run_id_text))
            conversation = await db.get(ConversationSession, work.conversation_session_id) if work is not None else None
            if work is None or run_row is None or conversation is None:
                return ExecutionOutcome(classification="blocked", error_code="cli_session_binding_missing")
            trigger = await db.get(__import__("src.memory.models", fromlist=["ConversationMessage"]).ConversationMessage, run_row.trigger_message_id) if run_row.trigger_message_id else None
            context = self.service._context(conversation)
            if (
                str(work.execution_adapter or "") != CLI_SESSION_ADAPTER_KEY
                or str(work.source_type or "") != CLI_SESSION_SOURCE_TYPE
                or str(work.active_agent_run_id) != str(run_row.id)
                or str(run_row.work_item_id) != str(work.id)
                or str(run_row.run_type or "") != CLI_SESSION_SOURCE_TYPE
                or str(run_row.session_id) != str(conversation.id)
                or trigger is None
                or str(trigger.session_id) != str(conversation.id)
                or str(trigger.role or "") != "user"
            ):
                return ExecutionOutcome(classification="blocked", error_code="cli_session_binding_invalid")
            if str(run_row.status or "") == "cancelled" or str(work.state or "") == "cancelled":
                return ExecutionOutcome(classification="cancelled", error_code="cancelled_before_provider")
            if str(run_row.status or "") in {"succeeded", "failed"} or str(work.state or "") in {"succeeded", "failed", "dead_letter", "uncertain"}:
                return ExecutionOutcome(classification="uncertain", error_code="late_provider_start_denied")
            if lease_owner is not None or lease_token is not None or claim_attempt is not None:
                if (
                    str(work.state or "") != "running"
                    or str(work.lease_owner or "") != str(lease_owner or "")
                    or str(work.lease_token or "") != str(lease_token or "")
                    or work.lease_expires_at is None
                    or work.lease_expires_at <= datetime.utcnow()
                    or int(work.attempt_count or 0) != int(claim_attempt or 0)
                    or int(run_row.work_item_attempt or 0) != int(claim_attempt or 0)
                ):
                    return ExecutionOutcome(classification="uncertain", error_code="lease_fence_lost", error_message="CLI Work lease changed before provider start")
            try:
                from ..security.agent_run_scope import get_current_run_scope

                if get_current_run_scope() is not None:
                    # The WSL/bubblewrap scope is a different authority lane.
                    # Falling through to a host Windows child would silently
                    # widen the boundary, so reject the mixed entry point.
                    return ExecutionOutcome(classification="blocked", error_code="trusted_windows_scope_conflict", error_message="trusted_windows CLI lane cannot run inside AgentRunScope")
            except ImportError:
                pass
            policy, cwd = self.service._policy_for_request(owner_id=str(context.get("owner_id") or work.metadata_json.get("owner_id") or run_row.user_id), agent_id=str(context.get("agent_id") or work.assigned_agent_id), working_directory=str(context.get("working_directory") or ""))
            permission_mode = self.service._validate_permission_mode(policy, str(context.get("permission_mode") or "native_default"))
            self.service._validate_provider_permission_mode(str(context.get("provider") or ""), permission_mode)
            expected_cache = self.service._provider_cache_root(policy, str(context.get("provider") or ""))
            if str(Path(expected_cache).resolve()).casefold() != str(Path(str(context.get("provider_cache_root") or "")).resolve()).casefold():
                return ExecutionOutcome(classification="blocked", error_code="provider_state_root_changed", error_message="provider state root changed before CLI start")
            binding = await self.service._binding_for_row(db, conversation)
            # ``_binding_for_row`` is authoritative; compare against the
            # persisted fixed tuple before touching CLISessionStore.
            from .cli_development_sessions import _assert_route

            route_projection = _assert_route(binding, provider=str(context.get("provider") or ""), model=str(context.get("model") or ""), effort=str(context.get("effort") or ""))
            pinned = pinned_cli_capabilities(context)
            manifest = getattr(run_row, "resolved_execution_manifest", None)
            records = (
                self._claim_value(claim, "required_capabilities"),
                getattr(work, "required_capabilities_json", None),
                manifest.get("capabilities") if isinstance(manifest, Mapping) else None,
            )
            try:
                # Immutable-session contract: every execution record must
                # equal the pinned ceiling exactly; missing/null fails closed.
                matched = all(record is not None and normalize_cli_capabilities(record) == pinned for record in records)
            except CLIDevelopmentSessionInvalid:
                matched = False
            if not matched:
                return ExecutionOutcome(classification="blocked", error_code="cli_session_capability_mismatch", error_message="execution capabilities differ from the pinned session ceiling")
            await self.service._resolve_authority(agent_id=str(context.get("agent_id") or work.assigned_agent_id), revision_id=str(context.get("agent_revision_id") or work.agent_revision_id), project_id=context.get("project_id"), capabilities=pinned)
            scope = self._native_scope(self.service, session_id=str(conversation.id), context=context, run_id=run_id_text)
            lease = await self.session_store.acquire(scope)
            await self.service.record_native_scope(str(conversation.id), scope_key=scope.scope_key, state="resuming" if lease.action == "resume" else "starting")
            if lease.action == "resume" and not lease.native_session_id:
                return ExecutionOutcome(classification="uncertain", error_code="native_session_disconnected", error_message="native CLI session handle is unavailable")
            provider = str(context.get("provider") or "")
            model = str(context.get("model") or "")
            effort = str(context.get("effort") or "")
            invocation_values = {
                "model": model,
                "effort": effort or None,
                "cwd": Path(cwd),
                "provider_state_root": str(context.get("provider_cache_root") or "") or None,
                "permission_mode": permission_mode,
                "native_session_id": lease.native_session_id,
                "native_session_action": lease.action,
                # The trusted_windows lane is an AoiTalk authority decision;
                # it is not a provider ``--sandbox`` value. Leave provider
                # sandbox unset unless the deployment pins a valid provider
                # value in its own backend adapter.
                "sandbox": None,
                "ephemeral": False,
                "retry_count": 1,
                "raw_prompt": True,
                "allowed_capabilities": None if pinned_cli_capabilities(context) == CLI_DEVELOPMENT_CAPABILITIES else pinned_cli_capabilities(context),
            }
            option_parameters = inspect.signature(CLIInvocationOptions).parameters
            options = CLIInvocationOptions(**{
                key: value for key, value in invocation_values.items()
                if key in option_parameters
            })
            backend = await self._backend(provider=provider, model=model, effort=effort, options=options)
            with self._active_lock:
                self._active[work_id] = (backend, run_id_text)
            loop = asyncio.get_running_loop()
            waiting_kind: str | None = None
            audit_failed = False

            def event_callback(kind: str, payload: Mapping[str, Any]) -> None:
                # Provider output is never copied to Work/Run event payloads.
                nonlocal waiting_kind, audit_failed
                safe = {"provider": provider, "event": str(kind)[:80]}
                if isinstance(payload, Mapping):
                    for key in ("status", "state", "phase", "approval", "input_required"):
                        if key in payload and isinstance(payload[key], (str, bool, int)):
                            safe[key] = payload[key]
                normalized_kind = str(kind or "").casefold().replace("-", "_")
                normalized_state = str((payload or {}).get("status") or (payload or {}).get("state") or "").casefold().replace("-", "_") if isinstance(payload, Mapping) else ""
                if normalized_kind in {"permission_required", "approval_required", "waiting_permission", "permission_pending"} or normalized_state in {"permission_required", "approval_required", "waiting_approval", "permission_pending"}:
                    waiting_kind = "waiting_approval"
                elif normalized_kind in {"input_required", "user_input_required", "waiting_for_input", "input_pending", "ask_user"} or normalized_state in {"input_required", "user_input_required", "waiting_for_input", "input_pending"}:
                    waiting_kind = "waiting_input"
                future = asyncio.run_coroutine_threadsafe(
                    self.service.record_runtime_event(
                        run_id=run_id_text,
                        work_item_id=work_id,
                        event_type=f"cli.{str(kind)[:80]}",
                        payload=safe,
                    ),
                    loop,
                )
                try:
                    future.result(timeout=30)
                except BaseException:
                    audit_failed = True
                    future.cancel()
                    raise

            # Prompt comes from the encrypted ConversationMessage.  It must be
            # read verbatim, including leading/trailing whitespace.
            prompt = str(getattr(trigger, "content", ""))
            success, output = await self._invoke_backend(backend, prompt, run_id=run_id_text, options=options, event_callback=event_callback, cancel_handle=run_row)
            native_id = None
            consume = getattr(backend, "consume_last_native_session_id", None)
            if callable(consume):
                native_id = consume()
            if not native_id:
                native_id = getattr(backend, "_last_native_session_id", None)
            resume_failed = bool(getattr(backend, "was_native_session_resume_failure", lambda: False)()) if callable(getattr(backend, "was_native_session_resume_failure", None)) else bool(getattr(backend, "_last_native_resume_failure", False))
            diagnostics = getattr(backend, "get_last_cli_diagnostics", lambda: {})()
            diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}
            invocation_config = getattr(backend, "get_last_invocation_config", lambda: {})()
            invocation_config = invocation_config if isinstance(invocation_config, Mapping) else {}
            observed_config = {
                str(key): str(value)[:256]
                for key, value in invocation_config.items()
                if str(key).startswith("observed_") and value not in (None, "")
            }
            applied_keys = {
                "executable", "cwd", "model", "effort", "state_root", "provider_state_root",
                "state_root_source", "provider_state_root_source", "approval_policy", "permission_mode",
                "applied_approval_policy", "applied_permission_mode", "sandbox", "applied_sandbox",
                "source", "settings_source", "sandbox_source",
            }
            applied_config = {
                str(key): str(value)[:512]
                for key, value in invocation_config.items()
                if str(key) in applied_keys and value not in (None, "")
            }
            await self.service.record_runtime_event(
                run_id=run_id_text,
                work_item_id=work_id,
                event_type="cli.invocation",
                status=str(diagnostics.get("status") or diagnostics.get("state") or "unknown")[:32],
                payload={
                    "requested": {
                        "provider": provider,
                        "model": model,
                        "effort": effort,
                        "permission_mode": str(context.get("permission_mode") or "native_default"),
                        "provider_state_root": str(context.get("provider_cache_root") or ""),
                        "execution_lane": "trusted_windows",
                        "native_session_action": lease.action,
                        "raw_prompt": True,
                        "retry_count": 1,
                        "source": "invocation_options",
                    },
                    "observed": observed_config,
                    "applied": applied_config,
                    "diagnostics": {str(key): value for key, value in diagnostics.items() if key in {"status", "state", "error_code", "wait_state", "wait_kind", "permission_event_count", "native_permission_denied_event_count", "permission_denial_count", "permission_denial_summary", "input_event_count", "effective_model", "cwd", "permission_mode", "stderr_present"}},
                },
            )
            if not native_id:
                for key in ("native_session_id", "session_id", "sessionId", "native_id"):
                    if diagnostics.get(key) not in (None, ""):
                        native_id = str(diagnostics[key])
                        break
            diagnostic_values = {
                str(diagnostics.get(key) or "").casefold().replace("-", "_")
                for key in ("status", "state", "provider_state", "cli_state", "wait_state", "wait_kind", "error_code")
                if diagnostics.get(key) not in (None, "")
            }
            if diagnostics.get("permission_denials"):
                waiting_kind = waiting_kind or "waiting_approval"
            if diagnostic_values & {"waiting", "waiting_for_input", "input_required", "input_pending", "user_input_required"}:
                waiting_kind = waiting_kind or "waiting_input"
            elif diagnostic_values & {"waiting_approval", "approval_required", "permission_required", "permission_denied", "permission_pending"}:
                waiting_kind = waiting_kind or "waiting_approval"
            if waiting_kind is not None and not native_id:
                native_id = getattr(backend, "_active_native_session_id", None)
            if success and not str(output or ""):
                success = False
            if success and diagnostic_values & {"error", "not_found", "timeout", "exception", "session_id_missing", "session_id_mismatch"}:
                success = False
            if audit_failed:
                await record_result(classification="failed", error_code="audit_write_failed", error_message="CLI event audit could not be persisted", native_state="active" if native_id else "unsupported", native_scope_key=scope.scope_key)
                return ExecutionOutcome(classification="permanent", error_code="audit_write_failed", error_message="CLI event audit could not be persisted")
            if waiting_kind is not None and not native_id:
                await record_result(classification="uncertain", error_code="native_session_unconfirmed", error_message="provider wait state had no confirmed native session handle", native_state="unsupported", native_scope_key=scope.scope_key)
                return ExecutionOutcome(classification="uncertain", error_code="native_session_unconfirmed", error_message="provider wait state had no confirmed native session handle")
            if waiting_kind is not None:
                recorded = await self.session_store.record_native_session(lease, native_id)
                if not recorded:
                    await record_result(classification="uncertain", error_code="native_session_persist_failed", error_message="provider session handle could not be persisted", native_state="disconnected", native_scope_key=scope.scope_key)
                    return ExecutionOutcome(classification="uncertain", error_code="native_session_persist_failed", error_message="provider session handle could not be persisted")
                await self.service.record_native_scope(str(conversation.id), scope_key=scope.scope_key, state="active")
                await record_result(classification=waiting_kind, error_code=waiting_kind, error_message="provider requested additional input or approval", native_state="active", native_scope_key=scope.scope_key)
                return ExecutionOutcome(classification="awaiting_approval" if waiting_kind == "waiting_approval" else "blocked", error_code=waiting_kind, error_message="provider requested additional input or approval")
            if success and not native_id:
                await self.service.record_native_scope(str(conversation.id), scope_key=scope.scope_key, state="unsupported")
                await record_result(classification="uncertain", error_code="native_session_unconfirmed", error_message="provider did not confirm a native session handle", native_state="unsupported", native_scope_key=scope.scope_key)
                return ExecutionOutcome(classification="uncertain", error_code="native_session_unconfirmed", error_message="provider did not confirm a native session handle")
            if success:
                recorded = await self.session_store.record_native_session(lease, native_id)
                if not recorded:
                    await record_result(classification="uncertain", error_code="native_session_persist_failed", error_message="provider session handle could not be persisted", native_state="disconnected", native_scope_key=scope.scope_key)
                    return ExecutionOutcome(classification="uncertain", error_code="native_session_persist_failed", error_message="provider session handle could not be persisted")
                await self.service.record_native_scope(str(conversation.id), scope_key=scope.scope_key, state="active")
                result = await record_result(classification="succeeded", output=str(output or ""), native_state="active", native_scope_key=scope.scope_key)
                if result.get("discarded"):
                    return ExecutionOutcome(classification="uncertain", error_code="late_result_discarded", error_message="late CLI result was discarded after the Run/Work fence changed")
                return ExecutionOutcome(classification="succeeded", result_summary="CLI session turn completed", result={"assistant_message_id": result.get("assistant_message_id")}, metadata={"model_routes": {"operator": route_projection}})
            if resume_failed:
                await self.session_store.invalidate(lease, reason="native_session_resume_failed")
                await self.service.record_native_scope(str(conversation.id), scope_key=scope.scope_key, state="invalid")
                await record_result(classification="uncertain", error_code="native_session_disconnected", error_message="native CLI session resume failed", native_state="invalid", native_scope_key=scope.scope_key)
                return ExecutionOutcome(classification="uncertain", error_code="native_session_disconnected", error_message="native CLI session resume failed")
            diagnostic = _sanitize_cli_error_diagnostic(output)
            await record_result(classification="failed", error_code="cli_execution_failed", error_message=diagnostic, native_state="active" if native_id else "unsupported", native_scope_key=scope.scope_key)
            return ExecutionOutcome(classification="permanent", error_code="cli_execution_failed", error_message=diagnostic, result_summary=diagnostic)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None) or "cli_session_adapter_error"
            if lease is not None and backend is not None:
                try:
                    await self.service.record_execution_result(session_id=str(getattr(lease.scope, "chat_session_id", "")), run_id=run_id_text, work_item_id=work_id, classification="uncertain", error_code=str(code), error_message="CLI execution state is uncertain", lease_owner=lease_owner, lease_token=lease_token, attempt=claim_attempt)
                except Exception:
                    pass
            return ExecutionOutcome(classification="blocked" if str(code).startswith(("trusted_windows", "agent_", "cli_session_binding", "cli_session_capability", "cli_capability")) else "uncertain", error_code=str(code), error_message="CLI session execution could not be completed")
        finally:
            native_handle_for_continuation = None
            release_ok = False
            if lease is not None:
                native_handle_for_continuation = lease.native_session_id
                if not native_handle_for_continuation and backend is not None:
                    native_handle_for_continuation = getattr(backend, "_last_native_session_id", None)
                if native_handle_for_continuation and not lease.native_session_id:
                    try:
                        if not await self.session_store.record_native_session(lease, native_handle_for_continuation):
                            native_handle_for_continuation = None
                    except Exception:
                        native_handle_for_continuation = None
                try:
                    release_ok = bool(await self.session_store.release(lease))
                except Exception:
                    release_ok = False
            try:
                session_for_finalize = str(getattr(lease.scope, "chat_session_id", "")) if lease is not None else str(getattr(locals().get("conversation", None), "id", ""))
                if session_for_finalize:
                    await self.service.finalize_cancelled_run(session_id=session_for_finalize, run_id=run_id_text, work_item_id=work_id, native_state="active" if release_ok and native_handle_for_continuation else "cancelled")
            except Exception:
                pass
            with self._active_lock:
                self._active.pop(work_id, None)
            await self.service._close(db)

    async def abort(self, claim: WorkClaim) -> None:
        work_id = str(self._claim_value(claim, "work_item_id") or "")
        with self._active_lock:
            active = self._active.get(work_id)
        if active is None:
            return
        backend = active[0]
        try:
            from ..llm.generation_cancellation import request_generation_cancellation

            request_generation_cancellation(str(active[1]))
        except Exception:
            pass
        for name in ("cancel_active_execution", "cancel", "stop"):
            callback = getattr(backend, name, None)
            if not callable(callback):
                continue
            value = callback()
            if inspect.isawaitable(value):
                await value
            break

    async def recover_stale(self, session: Any, row: Any, *, coordinator: Any, now: Any) -> bool:
        """Never replay a stale native CLI attempt after restart."""

        if str(getattr(row, "state", "") or "") not in {"claimed", "running"}:
            return False
        await coordinator._transition_locked(
            session,
            row,
            "uncertain",
            token=None,
            error_code="interrupted_by_restart",
            error_message="CLI native execution cannot be replayed after restart",
        )
        return True

    cancel = abort

    async def cleanup(self, claim: WorkClaim) -> None:
        # Process cleanup is owned by the backend/generation cancellation path;
        # this hook only drops the adapter's bookkeeping after it has drained.
        work_id = str(self._claim_value(claim, "work_item_id") or "")
        with self._active_lock:
            self._active.pop(work_id, None)


def register_cli_session_work(coordinator: Any, service: CLIDevelopmentSessionService, *, backend_factory: Any | None = None, session_store: Any | None = None) -> CLISessionWorkAdapter:
    """Register the source and adapter on an existing shared coordinator."""

    if coordinator is None:
        raise ValueError("shared AgentWorkCoordinator is required")
    adapter = CLISessionWorkAdapter(service, backend_factory=backend_factory, session_store=session_store)
    source = CLISessionTurnWorkSource(service)
    coordinator.register_source(source, source_type=CLI_SESSION_SOURCE_TYPE)
    coordinator.register_adapter(adapter, adapter_key=CLI_SESSION_ADAPTER_KEY)
    return adapter


__all__ = [
    "CLIInvocationOptions",
    "CLISessionTurnWorkSource",
    "CLISessionWorkAdapter",
    "register_cli_session_work",
    "CLISessionWorkError",
]
