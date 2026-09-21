"""Execution adapters for the durable AgentWork runtime.

The original Agent Harness predates the common AgentWork coordinator and
therefore bundled workspace creation, prompt rendering, runner invocation,
and retry scheduling in one orchestrator.  This module extracts the
provider/workspace portion as a small, provider-neutral execution adapter.

``CodeAgentExecutionAdapter`` deliberately owns *one attempt only*.  It does
not claim work, retry, reserve concurrency, or settle durable state.  Those
responsibilities belong to the common coordinator.  The legacy orchestrator
can still use the same runner/workspace assets when no coordinator is wired.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from ..features import Features
from .config import AGENT_HARNESS_SAFE_EXECUTION_BACKENDS, AgentHarnessSettings
from .models import HarnessEventCallback, RunResult, WorkItem, _safe_prompt_metadata
from .runner import AgentRunner
from .workflow import HarnessWorkflow, render_prompt
from .workspace import WorkspaceManager


logger = logging.getLogger(__name__)


class ExecutionAdapter(Protocol):
    """Minimal protocol consumed by a common AgentWork coordinator."""

    name: str
    adapter_key: str
    execution_adapter: str
    required_capabilities: Callable[[Any], Any]

    async def execute(
        self,
        work_item: Any,
        *,
        attempt: int | None = None,
        on_event: HarnessEventCallback | None = None,
        **kwargs: Any,
    ) -> RunResult:
        ...


@dataclass(frozen=True)
class PreparedCodeAgentRun:
    """Prepared, but not yet executed, code-agent attempt."""

    work_item: WorkItem
    workspace: Path
    prompt: str
    attempt: int
    created: bool
    prepared_at: datetime


class CodeAgentExecutionAdapter:
    """Run one code-oriented AgentWork attempt through existing harness assets.

    The adapter is intentionally independent of the concrete WS02 coordinator
    implementation.  Coordinators may pass additional lease, authority, or
    manifest context through ``**kwargs``; only the server-issued execution
    scope and workspace are consumed here.  Unknown context is never copied
    into prompts or child process environments.
    """

    name = "code_agent"
    adapter_key = "code_agent"
    execution_adapter = "code_agent"
    adapter_aliases = frozenset({"code_agent", "agent_harness"})
    # Reuse the existing Team capability vocabulary rather than inventing a
    # ``code_execution`` authority that the resolver cannot recognize.
    # Workspace writes and command execution remain constrained by the
    # server-issued HarnessExecutionScope; they are not inferred from task
    # text or provider prompts.
    _DEFAULT_CAPABILITIES = frozenset(
        {"workspace_read", "workspace_write", "command_execute"}
    )
    # Parent WS02's protocol calls ``required_capabilities(claim)``.  The
    # instance stores a callable frozenset so compatibility callers can still
    # compare it to a normal set (``adapter.required_capabilities == {...}``).
    required_capabilities = _DEFAULT_CAPABILITIES
    capabilities = _DEFAULT_CAPABILITIES
    _SUPPORTED_ADAPTER_NAMES = frozenset(
        {"code_agent", "agent_harness", "codex", "codex_exec", "claude", "claude_code"}
    )

    def __init__(
        self,
        settings: AgentHarnessSettings,
        runner: AgentRunner,
        workspace_manager: WorkspaceManager,
        workflow: HarnessWorkflow,
        required_capabilities: set[str] | frozenset[str] | None = None,
        event_sink: HarnessEventCallback | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.workspace_manager = workspace_manager
        self.workflow = workflow
        self.event_sink = event_sink
        configured = required_capabilities or self.required_capabilities
        self.capabilities = frozenset(
            _bounded_identifier(value, max_length=96)
            for value in configured
            if _bounded_identifier(value, max_length=96)
        ) or self._DEFAULT_CAPABILITIES
        self.required_capabilities = _CapabilitySet(self.capabilities)

    def required_capabilities_for(self, claim: Any | None = None) -> tuple[str, ...]:
        """Return capability declarations in the common-runtime shape."""

        del claim
        return tuple(sorted(self.capabilities))

    @classmethod
    def from_harness(
        cls,
        settings: AgentHarnessSettings,
        *,
        runner: AgentRunner,
        workspace_manager: WorkspaceManager,
        workflow: HarnessWorkflow,
        required_capabilities: set[str] | frozenset[str] | None = None,
        event_sink: HarnessEventCallback | None = None,
    ) -> "CodeAgentExecutionAdapter":
        """Build an adapter from an existing Harness construction graph."""

        return cls(
            settings=settings,
            runner=runner,
            workspace_manager=workspace_manager,
            workflow=workflow,
            required_capabilities=required_capabilities,
            event_sink=event_sink,
        )

    @classmethod
    def from_settings(
        cls,
        settings: AgentHarnessSettings,
        *,
        repo_root: Path | None = None,
        required_capabilities: set[str] | frozenset[str] | None = None,
        event_sink: HarnessEventCallback | None = None,
    ) -> "CodeAgentExecutionAdapter":
        """Construct the adapter from deployment-owned Harness settings."""

        from .runner import build_runner
        from .workflow import load_harness_workflow

        resolved_root = repo_root or Path(__file__).resolve().parents[2]
        workspace_manager = WorkspaceManager(
            settings.workspace_root,
            settings.hooks,
            repo_root=resolved_root,
            base_ref=settings.workspace_base_ref,
            branch_prefix=settings.workspace_branch_prefix,
        )
        return cls(
            settings=settings,
            runner=build_runner(settings),
            workspace_manager=workspace_manager,
            workflow=load_harness_workflow(settings.workflow_file),
            required_capabilities=required_capabilities,
            event_sink=event_sink,
        )

    def supports(self, work_item: Any) -> bool:
        """Return whether this adapter is selected by a projected work item."""

        requested = _work_item_value(work_item, "execution_adapter", None)
        if requested in (None, ""):
            # Legacy Task projections are code-agent candidates when they carry
            # the explicit harness marker.  A common runtime normally sets the
            # adapter field, so this fallback only preserves old trackers.
            metadata = _work_item_value(work_item, "metadata", {})
            if isinstance(metadata, Mapping):
                harness = metadata.get("agent_harness")
                requested = metadata.get("execution_adapter")
                if not requested and isinstance(harness, dict):
                    requested = harness.get("runner")
        if requested in (None, ""):
            return True
        return str(requested).strip().lower().replace("-", "_") in self._SUPPORTED_ADAPTER_NAMES

    def prepare(
        self,
        work_item: Any,
        *,
        attempt: int | None = None,
    ) -> PreparedCodeAgentRun:
        """Create/validate a workspace and render the repository workflow."""

        normalized = normalize_work_item(work_item)
        attempt_number = max(1, int(attempt or 1))
        workspace, created = self.workspace_manager.create_for(normalized.identifier)
        try:
            self.workspace_manager.run_before_run(workspace)
            prompt = render_prompt(
                self.workflow,
                issue=normalized,
                attempt=attempt_number,
            )
        except Exception:
            # A failed preparation must not strand a newly-created worktree.
            # Existing workspaces are intentionally retained for compatibility
            # and diagnostics; only this attempt's fresh workspace is removed.
            if created:
                try:
                    self.workspace_manager.remove_for(normalized.identifier)
                except Exception:
                    logger.exception(
                        "Code-agent workspace cleanup failed after preparation error for %s",
                        normalized.identifier,
                    )
            raise
        return PreparedCodeAgentRun(
            work_item=normalized,
            workspace=workspace,
            prompt=prompt,
            attempt=attempt_number,
            created=created,
            prepared_at=datetime.utcnow(),
        )

    async def execute(
        self,
        work_item: Any,
        *,
        attempt: int | None = None,
        on_event: HarnessEventCallback | None = None,
        prepared: PreparedCodeAgentRun | None = None,
        **kwargs: Any,
    ) -> RunResult:
        """Execute exactly one attempt and return the runner's normalized result.

        ``lease_token``, ``run_id``, ``authority``, and similar coordinator
        values are accepted for interface compatibility but never trusted as
        execution authority.  A server-issued ``AgentRunScope``/harness scope
        in the current context remains the only subprocess boundary.
        """

        # A WS02 coordinator supplies ``coordinator=self`` and expects a
        # normalized outcome mapping.  Legacy callers (including AgentRunner
        # tests) receive the historical ``RunResult`` value.
        coordinator = kwargs.pop("coordinator", None)
        run_id = kwargs.pop("run_id", None) or kwargs.pop("agent_run_id", None)
        del kwargs  # Remaining context is coordinator-owned; never leak it.
        if attempt is None:
            attempt = _work_item_value(work_item, "attempt", None)
        normalized_item = normalize_work_item(work_item)
        if prepared is not None:
            prepared_id = prepared.work_item.work_item_id or prepared.work_item.id
            requested_id = normalized_item.work_item_id or normalized_item.id
            if str(prepared_id) != str(requested_id):
                raise ValueError("prepared execution does not match work item")
            prepared_run = prepared
        else:
            prepared_run = self.prepare(normalized_item, attempt=attempt)
        event = _bounded_event_callback(on_event, prepared_run)
        if event is None and self.event_sink is not None:
            event = _bounded_event_callback(self.event_sink, prepared_run)
        if event is None and coordinator is not None:
            event = _coordinator_event_callback(coordinator, prepared_run)
        observed_provider_session_id: list[str | None] = [None]
        observed_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        if event is not None:
            delegate_event = event

            async def capture_event(payload: dict[str, Any]) -> None:
                provider_id = payload.get("provider_session_id") or payload.get("session_id")
                if isinstance(provider_id, str) and provider_id.strip():
                    observed_provider_session_id[0] = provider_id.strip()
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    for key in observed_usage:
                        try:
                            observed_usage[key] += max(0, int(usage.get(key) or 0))
                        except (TypeError, ValueError):
                            continue
                await delegate_event(payload)

            event = capture_event
        try:
            result = await self._invoke_runner(
                prepared_run,
                on_event=event,
                run_id=run_id,
            )
            normalized = normalize_run_result(result)
            if normalized.provider_session_id is None and observed_provider_session_id[0]:
                normalized.provider_session_id = observed_provider_session_id[0]
            normalized.input_tokens += observed_usage["input_tokens"]
            normalized.output_tokens += observed_usage["output_tokens"]
            normalized.total_tokens += observed_usage["total_tokens"]
            if coordinator is not None:
                return self.normalize_outcome(normalized)
            return normalized
        except asyncio.CancelledError:
            # Cancellation is a lifecycle signal to the coordinator, not a
            # retry decision made by this adapter.
            raise
        except Exception as exc:
            logger.exception("Code-agent execution failed for %s", prepared_run.work_item.identifier)
            failed = RunResult(success=False, message=_bounded_text(str(exc)))
            if coordinator is not None:
                return self.normalize_outcome(failed)
            return failed
        finally:
            # The after-run hook belongs to the workspace asset and must run
            # for success, failure, and cancellation.  Workspace removal is a
            # separate coordinator settlement decision (``cleanup``).
            try:
                maybe_cleanup = self.workspace_manager.run_after_run(prepared_run.workspace)
                if inspect.isawaitable(maybe_cleanup):
                    await maybe_cleanup
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Code-agent after-run hook failed for %s",
                    prepared_run.work_item.identifier,
                )

    async def run(self, work_item: Any, **kwargs: Any) -> RunResult:
        """Compatibility alias matching ``AgentRunner.run``/adapter callers."""

        return await self.execute(work_item, **kwargs)

    async def _invoke_runner(
        self,
        prepared: PreparedCodeAgentRun,
        *,
        on_event: HarnessEventCallback | None,
        run_id: str | None = None,
    ) -> RunResult:
        if Features.is_enterprise():
            return await self._invoke_enterprise_runner(
                prepared,
                on_event=on_event,
                run_id=run_id,
            )
        result = await self.runner.run(
            work_item=prepared.work_item,
            workspace=prepared.workspace,
            prompt=prepared.prompt,
            attempt=prepared.attempt,
            on_event=on_event,
        )
        return normalize_run_result(result)

    async def _invoke_enterprise_runner(
        self,
        prepared: PreparedCodeAgentRun,
        *,
        on_event: HarnessEventCallback | None,
        run_id: str | None = None,
    ) -> RunResult:
        """Use the existing server-issued WSL2/bubblewrap execution fence."""

        from ..security.agent_run_scope import get_current_run_scope

        if (
            not self.settings.execution_enabled
            or self.settings.execution_backend not in AGENT_HARNESS_SAFE_EXECUTION_BACKENDS
            or self.settings.execution_network not in {"none", "broad"}
        ):
            return RunResult(
                success=False,
                message="trusted Enterprise harness execution is disabled or unsupported",
            )
        # A server-issued AgentRunScope may already be bound by the common
        # coordinator.  Preserve that exact actor/authority context rather
        # than replacing it with the legacy service-owned harness scope.
        active_run_scope = get_current_run_scope()
        if active_run_scope is None:
            # Enterprise autonomous execution must be issued a claim-specific
            # server scope by the composition root.  Constructing a broad
            # deployment-owned scope here would ignore per-Agent authority and
            # revocation fences, so fail closed until that context is present.
            return RunResult(
                success=False,
                message="server-issued Enterprise AgentRun scope is unavailable",
            )
        result = await self.runner.run(
            work_item=prepared.work_item,
            workspace=prepared.workspace,
            prompt=prepared.prompt,
            attempt=prepared.attempt,
            on_event=on_event,
        )
        return normalize_run_result(result)

    async def cleanup(self, work_item: Any) -> None:
        """Remove a worktree after durable settlement has completed."""

        normalized = normalize_work_item(work_item)
        maybe = self.workspace_manager.remove_for(normalized.identifier)
        if inspect.isawaitable(maybe):
            await maybe

    def execution_manifest(
        self,
        work_item: Any,
        *,
        attempt: int | None = None,
        prepared: PreparedCodeAgentRun | None = None,
        **context: Any,
    ) -> dict[str, Any]:
        """Return a bounded, non-secret execution manifest for AgentRun pinning."""

        normalized = normalize_work_item(work_item)
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "capabilities": sorted(self.capabilities),
            "workspace_access": "write",
            "network_access": "none",
            "source": normalized.source_type,
        }
        for key in ("agent_id", "agent_revision_id", "task_id", "work_item_id"):
            value = _work_item_value(normalized, key, None)
            if value:
                safe_value = _safe_manifest_identifier(value)
                if safe_value:
                    manifest[key] = safe_value
        # The authority resolver/coordinator may provide hash-only execution
        # evidence and exact Team/Profile/Subagent pins.  Accept only the
        # AgentRunService allowlist and never persist prompts, paths, models,
        # or arbitrary context.
        for key in (
            "agent_revision_version",
            "team_id",
            "execution_profile_id",
            "subagent_id",
        ):
            value = context.get(key, _work_item_value(normalized, key, None))
            safe_value = _safe_manifest_identifier(value)
            if safe_value:
                manifest[key] = safe_value
        for key in ("authority_hash", "run_scope_hash"):
            value = context.get(key, _work_item_value(normalized, key, None))
            if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value.strip()):
                manifest[key] = value.strip().lower()
        return {key: value for key, value in manifest.items() if value not in (None, "")}

    # Naming aliases used by coordinator integrations during the WS02→WS03
    # migration.  They intentionally point at the same bounded projection.
    build_execution_manifest = execution_manifest
    build_manifest = execution_manifest

    def _runner_kind(self) -> str:
        return _bounded_identifier(type(self.runner).__name__, max_length=64) or "runner"

    @classmethod
    def normalize_outcome(cls, result: Any) -> dict[str, Any]:
        """Return a coordinator-friendly bounded classification/evidence DTO."""

        normalized = normalize_run_result(result)
        message = _redact_sensitive_text(normalized.message, max_length=2000)
        lowered = message.lower()
        declared = str(normalized.classification or "").strip().lower().replace("-", "_")
        if declared in {
            "succeeded",
            "transient",
            "permanent",
            "blocked",
            "awaiting_approval",
            "uncertain",
            "cancelled",
        }:
            classification = declared
        elif normalized.success:
            classification = "succeeded"
        elif any(token in lowered for token in ("awaiting approval", "approval required", "needs approval")):
            classification = "awaiting_approval"
        elif "uncertain" in lowered or "unknown outcome" in lowered:
            classification = "uncertain"
        elif any(token in lowered for token in ("blocked", "permission denied", "not authorized", "forbidden")):
            classification = "blocked"
        elif any(token in lowered for token in ("cancelled", "canceled", "cancelled by")):
            classification = "cancelled"
        elif any(token in lowered for token in ("timeout", "timed out", "temporar", "stalled", "retry")) or re.search(
            r"\b(?:429|5\d\d)\b", lowered
        ):
            classification = "transient"
        else:
            classification = "permanent"
        return {
            "classification": classification,
            "success": normalized.success,
            "message": message,
            "result_summary": message if normalized.success else None,
            "error_code": f"code_agent_{classification}" if not normalized.success else None,
            "error_message": message if not normalized.success else None,
            "provider_session_id": normalized.provider_session_id,
            "usage": {
                "input_tokens": max(0, int(normalized.input_tokens)),
                "output_tokens": max(0, int(normalized.output_tokens)),
                "total_tokens": max(0, int(normalized.total_tokens)),
            },
            "evidence": [{"adapter": cls.execution_adapter}],
        }

    normalize_result = normalize_outcome


def normalize_work_item(value: Any) -> WorkItem:
    """Project a durable row/dict into the legacy prompt work-item shape."""

    if isinstance(value, WorkItem):
        safe_metadata = _safe_prompt_metadata(value.metadata)
        if safe_metadata != value.metadata:
            return replace(value, metadata=safe_metadata)
        return value
    if hasattr(value, "to_work_item"):
        candidate = value.to_work_item()
        if isinstance(candidate, WorkItem):
            return candidate
        value = candidate
    if isinstance(value, Mapping):
        source = value
        get = source.get
    else:
        source = None

        def get(name: str, default: Any = None) -> Any:
            return getattr(value, name, default)

    item_id = _bounded_identifier(get("id", None) or get("work_item_id", None) or get("source_id", None), max_length=128)
    identifier = _bounded_identifier(get("identifier", None) or get("source_id", None) or item_id, max_length=128)
    if not item_id:
        # A malformed source row must fail closed rather than creating an
        # unbounded workspace name.  The common coordinator should classify
        # this as permanent/blocked before invoking the adapter.
        raise ValueError("work item id is required")
    if not identifier:
        identifier = item_id
    metadata = get("metadata", None)
    if metadata is None:
        metadata = get("work_metadata", None)
    if not isinstance(metadata, Mapping):
        metadata = {}
    else:
        metadata = dict(metadata)
    blocked_by = get("blocked_by", None)
    if not isinstance(blocked_by, list):
        blocked_by = list(metadata.get("blocked_by") or []) if isinstance(metadata, Mapping) else []
    priority = get("priority", None)
    labels = get("labels", None)
    if not isinstance(labels, list):
        labels = []
    title = _bounded_text(get("title", None), max_length=512)
    if not title and isinstance(metadata, Mapping):
        title = _bounded_text(
            metadata.get("title") or metadata.get("task_title"),
            max_length=512,
        )
    if not title:
        title = identifier
    description = _bounded_text(get("description", None), max_length=16_384)
    if not description and isinstance(metadata, Mapping):
        description = _bounded_text(metadata.get("description"), max_length=16_384)
    raw_capabilities = get("required_capabilities", None)
    if isinstance(raw_capabilities, (list, tuple, set, frozenset)):
        capability_values = list(raw_capabilities)[:64]
    else:
        capability_values = []
    return WorkItem(
        id=item_id,
        identifier=identifier,
        title=title,
        description=description,
        state=_bounded_identifier(get("state", None) or get("status", None) or "todo", max_length=64) or "todo",
        priority=priority,
        project_id=_bounded_identifier(get("project_id", None), max_length=128) or None,
        project_name=_bounded_text(get("project_name", None), max_length=256) or None,
        space_id=_bounded_identifier(get("space_id", None), max_length=128) or None,
        url=None,  # URLs are not needed by the code-agent prompt projection.
        labels=[_bounded_identifier(label, max_length=96) for label in labels[:64] if _bounded_identifier(label, max_length=96)],
        blocked_by=blocked_by[:64],
        created_at=get("created_at", None),
        updated_at=get("updated_at", None),
        metadata=_safe_prompt_metadata(metadata),
        source_type=_bounded_identifier(get("source_type", None) or "task", max_length=64) or "task",
        source_id=_bounded_identifier(get("source_id", None) or item_id, max_length=128) or item_id,
        source_revision=_bounded_identifier(get("source_revision", None), max_length=128) or None,
        intent_key=_bounded_identifier(get("intent_key", None) or "agent_harness", max_length=128) or "agent_harness",
        domain=_bounded_identifier(get("domain", None) or "task", max_length=64) or "task",
        work_item_id=_bounded_identifier(get("work_item_id", None) or item_id, max_length=128) or item_id,
        agent_id=_bounded_identifier(
            get("agent_id", None) or get("assigned_agent_id", None),
            max_length=128,
        )
        or None,
        agent_revision_id=_bounded_identifier(get("agent_revision_id", None), max_length=128) or None,
        task_id=_bounded_identifier(get("task_id", None), max_length=128) or None,
        persona_id=_bounded_identifier(get("persona_id", None), max_length=128) or None,
        app_id=_bounded_identifier(get("app_id", None), max_length=128) or None,
        execution_adapter=_bounded_identifier(get("execution_adapter", None) or "code_agent", max_length=64) or "code_agent",
        required_capabilities=[
            _bounded_identifier(capability, max_length=96)
            for capability in capability_values
            if _bounded_identifier(capability, max_length=96)
        ],
        concurrency_key=_bounded_identifier(get("concurrency_key", None), max_length=128) or None,
        root_work_item_id=_bounded_identifier(get("root_work_item_id", None), max_length=128) or None,
        parent_work_item_id=_bounded_identifier(get("parent_work_item_id", None), max_length=128) or None,
        causation_id=_bounded_identifier(get("causation_id", None), max_length=128) or None,
        causal_depth=max(0, min(int(get("causal_depth", 0) or 0), 64)),
        team_id=_bounded_identifier(get("team_id", None), max_length=128) or None,
        execution_profile_id=_bounded_identifier(get("execution_profile_id", None), max_length=128) or None,
        subagent_id=_bounded_identifier(get("subagent_id", None), max_length=128) or None,
        authority_hash=(
            str(get("authority_hash", "")).strip().lower()
            if re.fullmatch(r"[0-9a-fA-F]{64}", str(get("authority_hash", "")).strip())
            else None
        ),
        run_scope_hash=(
            str(get("run_scope_hash", "")).strip().lower()
            if re.fullmatch(r"[0-9a-fA-F]{64}", str(get("run_scope_hash", "")).strip())
            else None
        ),
    )


def normalize_run_result(result: Any) -> RunResult:
    """Normalize provider/fake runner return values without dropping usage."""

    if isinstance(result, RunResult):
        return result
    if result is True or result is False:
        return RunResult(success=bool(result))
    if isinstance(result, dict):
        provider_session_id = result.get("provider_session_id") or result.get("session_id")
        success = result.get("success")
        if success is None:
            success = str(result.get("classification") or result.get("status") or "").casefold() in {
                "success",
                "succeeded",
                "ok",
            }
        elif isinstance(success, str):
            success = success.strip().casefold() in {"1", "true", "yes", "on", "success", "succeeded", "ok"}
        return RunResult(
            success=bool(success),
            message=_bounded_text(
                result.get("message")
                or result.get("result_summary")
                or result.get("error_message")
                or result.get("error", "")
            ),
            provider_session_id=(
                _safe_provider_identifier(provider_session_id) or None
            ),
            input_tokens=_safe_nonnegative_int(result.get("input_tokens")),
            output_tokens=_safe_nonnegative_int(result.get("output_tokens")),
            total_tokens=_safe_nonnegative_int(result.get("total_tokens")),
            classification=(
                _bounded_identifier(
                    result.get("classification") or result.get("status"),
                    max_length=32,
                )
                or None
            ),
        )
    raw_success = getattr(result, "success", None)
    classification = str(getattr(result, "classification", "") or "").casefold()
    if raw_success is None:
        raw_success = classification in {"success", "succeeded", "ok"}
    raw_message = getattr(result, "message", None)
    if raw_message in (None, ""):
        raw_message = getattr(result, "result_summary", None) or getattr(
            result, "error_message", ""
        )
    return RunResult(
        success=bool(raw_success),
        message=_bounded_text(raw_message),
        provider_session_id=(
                _safe_provider_identifier(
                getattr(result, "provider_session_id", None)
                or getattr(result, "session_id", None),
                )
            or None
        ),
        input_tokens=_safe_nonnegative_int(getattr(result, "input_tokens", 0)),
        output_tokens=_safe_nonnegative_int(getattr(result, "output_tokens", 0)),
        total_tokens=_safe_nonnegative_int(getattr(result, "total_tokens", 0)),
        classification=(
            _bounded_identifier(
                getattr(result, "classification", None)
                or getattr(result, "status", None),
                max_length=32,
            )
            or None
        ),
    )


def _bounded_event_callback(
    callback: HarnessEventCallback | None,
    prepared: PreparedCodeAgentRun,
) -> HarnessEventCallback | None:
    if callback is None:
        return None

    async def emit(event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        # Preserve the established event shape while projecting provider
        # payloads to bounded text/usage.  Raw JSONL envelopes can contain
        # transcripts, URLs, or credentials and must not cross the durable
        # AgentWorkEvent boundary.
        bounded: dict[str, Any] = {
            "event": _bounded_identifier(event.get("event") or "runner.event", max_length=80)
            or "runner.event",
            "message": _safe_event_message(event.get("message")),
            "work_item_id": prepared.work_item.work_item_id or prepared.work_item.id,
            "attempt": prepared.attempt,
        }
        usage = event.get("usage")
        if isinstance(usage, dict):
            bounded["usage"] = {
                key: _safe_nonnegative_int(usage.get(key))
                for key in ("input_tokens", "output_tokens", "total_tokens")
            }
        provider_session_id = event.get("provider_session_id") or event.get("session_id")
        if provider_session_id:
            safe_provider_id = _safe_provider_identifier(provider_session_id)
            if safe_provider_id:
                bounded["provider_session_id"] = safe_provider_id
        maybe = callback(bounded)
        if inspect.isawaitable(maybe):
            await maybe

    return emit


def _coordinator_event_callback(
    coordinator: Any,
    prepared: PreparedCodeAgentRun,
) -> HarnessEventCallback | None:
    """Best-effort bridge for durable AgentWork/AgentRun evidence hooks."""

    method = None
    for name in ("record_event", "record_work_event", "append_event"):
        candidate = getattr(coordinator, name, None)
        if callable(candidate):
            method = candidate
            break
    if method is None:
        return None

    async def emit(event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        payload = {
            "event": _bounded_identifier(event.get("event") or "runner.event", max_length=80)
            or "runner.event",
            "message": _safe_event_message(event.get("message")),
            "work_item_id": prepared.work_item.work_item_id or prepared.work_item.id,
            "attempt": prepared.attempt,
        }
        provider_session_id = event.get("provider_session_id") or event.get("session_id")
        if provider_session_id:
            payload["provider_session_id"] = _safe_provider_identifier(provider_session_id)
        try:
            signature = inspect.signature(method)
            parameters = signature.parameters
            accepts_kwargs = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
        except (TypeError, ValueError):
            parameters = {}
            accepts_kwargs = False
        try:
            if "event" in parameters:
                value = method(event=payload)
            elif "payload" in parameters:
                if "claim" in parameters:
                    value = method(claim=prepared.work_item, payload=payload)
                elif "work_item" in parameters:
                    value = method(work_item=prepared.work_item, payload=payload)
                else:
                    value = method(payload=payload)
            elif accepts_kwargs:
                value = method(event=payload)
            else:
                positional = [
                    parameter
                    for parameter in parameters.values()
                    if parameter.kind
                    in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                ]
                if len(positional) >= 2:
                    value = method(prepared.work_item, payload)
                else:
                    value = method(payload)
            if inspect.isawaitable(value):
                await value
        except Exception:
            # Evidence persistence must not make a provider run appear to
            # have failed; the coordinator can audit this warning separately.
            logger.debug("AgentWork event bridge failed", exc_info=True)

    return emit


def _work_item_value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _safe_event_message(value: Any) -> str:
    """Project provider event payloads to bounded, non-secret text."""

    if isinstance(value, dict):
        for key in ("text", "message", "summary", "error", "status"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return _redact_sensitive_text(candidate, max_length=4096)
        return "[runner event]"
    return _redact_sensitive_text(value, max_length=4096)


def _bounded_text(value: Any, *, max_length: int = 32_768) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) <= max_length:
        return text
    return text[: max(0, max_length - 32)] + "...[truncated]"


_SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"(?:https?|ftp)://[^\s]+", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s]+"),
    re.compile(r"(?<![A-Za-z0-9])/(?:Users?|home|tmp|var|etc|appdata)/[^\s]+", re.IGNORECASE),
    re.compile(r"\b(?:authorization|bearer|api[_-]?key|token|password)\s*[:=]\s*[^\s]+", re.IGNORECASE),
)


def _redact_sensitive_text(value: Any, *, max_length: int = 32_768) -> str:
    text = _bounded_text(value, max_length=max_length)
    for pattern in _SENSITIVE_TEXT_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:/-]+$")


def _bounded_identifier(value: Any, *, max_length: int = 128) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or len(text) > max_length or not _IDENTIFIER_RE.fullmatch(text):
        return ""
    return text


def _safe_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_workspace_key(value: Any) -> str:
    """Return an opaque workspace key without path separators/traversal."""

    text = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or ""))
    text = text.strip("._")
    return text[:128] or "work-item"


def _safe_manifest_identifier(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 128 or not re.fullmatch(r"[A-Za-z0-9_.-]+", text):
        return ""
    return text


def _safe_provider_identifier(value: Any) -> str:
    """Keep opaque provider continuation IDs free of URL/path payloads."""

    text = str(value or "").strip()
    if (
        not text
        or len(text) > 512
        or any(character.isspace() or ord(character) < 32 for character in text)
        or not re.fullmatch(r"[A-Za-z0-9_.:@+-]+", text)
    ):
        return ""
    return text


__all__ = [
    "AgentHarnessExecutionAdapter",
    "CodeExecutionAdapter",
    "CodeAgentExecutionAdapter",
    "ExecutionAdapter",
    "PreparedCodeAgentRun",
    "normalize_run_result",
    "normalize_work_item",
]


class _CapabilitySet(frozenset[str]):
    """A set that is also callable for the WS02 adapter protocol."""

    def __call__(self, claim: Any | None = None) -> tuple[str, ...]:
        del claim
        return tuple(sorted(self))


# Compatibility names used by early convergence prototypes.
AgentHarnessExecutionAdapter = CodeAgentExecutionAdapter
CodeExecutionAdapter = CodeAgentExecutionAdapter
