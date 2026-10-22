"""Local task receipts backed by the existing AgentWork coordinator.

This module owns admission/projection only. The shared coordinator owns claim,
lease, cancellation and settlement; LocalAgentTaskRunner supplies its existing
bounded tool loop inline. No provider is constructed during admission/readout.
"""
from __future__ import annotations

import asyncio
import dataclasses
import inspect
import importlib
import os
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .local_agent_tasks import (
    IDEMPOTENCY_KEY_RE, LocalAgentTaskError, LocalAgentTaskRunner,
    LocalAgentTaskSpec, SecretRedactor, TASK_STATES, TERMINAL_STATES,
    _cfg, validate_task_request, resolve_task_route,
)

ADAPTER_KEY = "local_agent_task"
LOCAL_PROVIDERS = frozenset({"openai_compatible_local", "ollama"})
WORK_STATUS = {
    "pending": "queued", "claimed": "running", "running": "running",
    "awaiting_approval": "waiting_approval", "blocked": "waiting",
    "retry_wait": "waiting", "uncertain": "interrupted",
    "succeeded": "completed", "failed": "failed", "dead_letter": "failed",
    "cancelled": "cancelled",
}


def selected_agent_id(config: Any, requested: Any) -> str:
    value = requested if requested is not None else _cfg(config, "local_agent_tasks.default_agent_id")
    if not value:
        raise LocalAgentTaskError("agent_not_configured", "Select an Agent or configure the delegation Agent", 409)
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise LocalAgentTaskError("agent_not_configured", "The delegation Agent configuration is invalid", 409) from None


async def validate_default_agent_id(config: Any, value: Any, *, actor_user_id: Any,
                                    actor_role: Any) -> str | None:
    """Validate a default selection without creating work or granting authority."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise LocalAgentTaskError("invalid_agent_id", "Agent ID must be a UUID or null", 400)
    try:
        agent_id = str(uuid.UUID(value.strip()))
    except (ValueError, TypeError, AttributeError):
        raise LocalAgentTaskError("invalid_agent_id", "Agent ID must be a UUID or null", 400) from None

    from .conversation_agent_service import ConversationAgentService
    from .agent_model_routing import AgentRoutingPins, resolve_agent_role_route

    _, revision = await ConversationAgentService(config=config).validate_agent_selection(
        agent_id=agent_id, actor_user_id=actor_user_id, actor_role=actor_role)
    route = resolve_agent_role_route(config, AgentRoutingPins.from_revision(revision), "operator")
    spec = LocalAgentTaskSpec(goal="", workspace="", allow_commands=False)
    validate_canonical_local_route(config, spec, route)
    return agent_id


def require_workspace_runtime() -> None:
    """Deny admission until the common workspace authority is integrated.

    This check is intentionally lazy: receipt readout, cancellation, ordinary
    tools and application startup do not depend on the optional foundation.
    Presence never grants authority; the existing lease and per-tool guards
    still validate the actual owner, Agent, Run, paths and capabilities.
    """
    contracts = {
        "src.services.agent_dialogue_workspace": ("AgentDialogueWorkspaceManager", "get_current_agent_dialogue_workspace"),
        "src.services.tool_mutation_resource_guard": ("ToolMutationResourceDenied", "_resource_key_for_workspace_root",
            "execute_with_mutation_resource_lease_sync", "existing_active_lease_proof"),
        "src.llm.tool_policy": ("set_current_agent_workspace_scope", "reset_current_agent_workspace_scope"),
    }
    try:
        for module_name, names in contracts.items():
            module = importlib.import_module(module_name)
            if any(not callable(getattr(module, name, None)) for name in names):
                raise RuntimeError("common workspace contract is incomplete")
    except Exception:
        raise LocalAgentTaskError("canonical_runtime_unavailable",
            "The common Agent workspace authority is not integrated; canonical task admission is unavailable", 409) from None


def workspace_concurrency_key(workspace: str) -> str:
    # Match the common tool resource fence exactly, including native Windows
    # case/path normalization; POSIX case-sensitive paths stay distinct.
    from .tool_mutation_resource_guard import _resource_key_for_workspace_root
    return _resource_key_for_workspace_root(Path(workspace))


def task_capabilities(spec: LocalAgentTaskSpec) -> tuple[str, ...]:
    if spec.allow_adb or spec.allow_git_commit:
        raise LocalAgentTaskError("permission_denied", "Canonical ADB and Git commit capabilities are not supported", 403)
    if spec.allow_commands and spec.mode != "mutate":
        raise LocalAgentTaskError("permission_denied", "Scoped commands require explicit mutate mode and command authority", 403)
    if spec.expected_branch:
        raise LocalAgentTaskError("permission_denied", "Branch verification requires a scoped command execution boundary", 403)
    caps = ("workspace_read", "workspace_write") if spec.mode == "mutate" else ("workspace_read",)
    return (*caps, "command_execute") if spec.allow_commands else caps


def command_workspace_root(config: Any, workspace: str) -> Path:
    from .harness_execution_scope_service import _path_has_any_link_or_reparse
    # Use only an existing trusted harness deployment root. Never invent the
    # request's parent as a managed root, and never create/install a root here.
    raw = next((value for key in ("harness_execution.workspace_root", "harness.execution.workspace_root",
        "agent_harness.execution.workspace_root", "workspaces_root", "workspace_root")
        if (value := _cfg(config, key))), None) or os.environ.get("AOITALK_WORKSPACES_DIR")
    if not isinstance(raw, (str, os.PathLike)) or not str(raw).strip():
        raise LocalAgentTaskError("permission_denied", "Configure an existing trusted harness workspace root for commands", 403)
    lexical_root = Path(raw).expanduser().absolute()
    lexical_workspace = Path(workspace).expanduser().absolute()
    if _path_has_any_link_or_reparse(lexical_root) or _path_has_any_link_or_reparse(lexical_workspace):
        raise LocalAgentTaskError("permission_denied", "Command workspace roots cannot contain links or reparse points", 403)
    root, target = lexical_root.resolve(), lexical_workspace.resolve()
    if not root.is_dir() or not target.is_dir() or target == root or not target.is_relative_to(root):
        raise LocalAgentTaskError("permission_denied", "Command workspace must be a strict descendant of the configured harness root", 403)
    return root


def route_expectations(spec: LocalAgentTaskSpec, route: Any) -> dict[str, str]:
    resolved = {key: str(getattr(route, key, None) or "") for key in ("provider", "model", "effort")}
    if resolved["provider"] not in LOCAL_PROVIDERS or not resolved["model"]:
        raise LocalAgentTaskError("agent_route_mismatch", "The Agent must explicitly use an existing local provider/model route", 409)
    for key in resolved:
        expected = getattr(spec, key)
        if expected is not None and expected != resolved[key]:
            raise LocalAgentTaskError("agent_route_mismatch", "Requested route does not match the canonical Agent revision", 409)
    return resolved


def validate_canonical_local_route(
    config: Any, spec: LocalAgentTaskSpec, route: Any,
) -> dict[str, str]:
    """Validate one pinned Agent route against the existing local catalog.

    A canonical route with an empty effort deliberately means "use the
    provider/model default". Keep that policy when checking the catalog and
    leave the durable route projection empty; injecting a concrete default
    here would change the pinned route and make the runner reject it later.
    """

    resolved = route_expectations(spec, route)
    catalog_spec = dataclasses.replace(
        spec,
        provider=resolved["provider"],
        model=resolved["model"],
        effort=resolved["effort"] or None,
    )
    try:
        catalog_route = resolve_task_route(config, catalog_spec)
    except LocalAgentTaskError:
        raise LocalAgentTaskError(
            "agent_route_mismatch",
            "The canonical route does not match the local catalog",
            409,
        ) from None
    if (
        catalog_route.get("provider") != resolved["provider"]
        or catalog_route.get("model") != resolved["model"]
        or (resolved["effort"] and catalog_route.get("effort") != resolved["effort"])
    ):
        raise LocalAgentTaskError(
            "agent_route_mismatch",
            "The canonical route does not match the local catalog",
            409,
        )
    return resolved


def _owner(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise LocalAgentTaskError("permission_denied", "An authenticated human owner is required", 403) from None


def _error(exc: Exception) -> LocalAgentTaskError:
    if isinstance(exc, LocalAgentTaskError):
        return exc
    code = str(getattr(exc, "code", ""))
    if code not in {"agent_not_found", "agent_inactive", "agent_revision_missing", "agent_route_mismatch"}:
        code = "permission_denied" if "denied" in code or "grant" in code else "canonical_runtime_unavailable"
    return LocalAgentTaskError(code, "Canonical Agent admission could not be completed", 403 if code == "permission_denied" else 409)


class CanonicalLocalAgentTasks:
    def __init__(self, manager: Any, coordinator: Any):
        self.manager = manager
        self.coordinator = coordinator
        self.config = manager.config
        self.db = coordinator._db_manager
        self._submit_lock = asyncio.Lock()
        self._cleanup_tasks: set[asyncio.Task[Any]] = set()
        self.adapter = LocalTaskExecutionAdapter(self)

    async def _session(self):
        value = self.db.get_session()
        return await value if inspect.isawaitable(value) else value

    def _record(self, task_id: str, owner: str) -> dict[str, Any]:
        record = self.manager.store.load(task_id)
        if record.get("owner") != owner:
            raise LocalAgentTaskError("not_found", "task not found", 404)
        return record

    def _update(self, task_id: str, **fields: Any) -> dict[str, Any]:
        return self.manager._mutate(task_id, fields)

    def _event(self, task_id: str, kind: str, payload: Mapping[str, Any]) -> None:
        with self.manager._lock:
            record = self.manager.store.load(task_id)
            count = int(record.get("event_count") or 0) + 1
            if count <= self.manager.limits.max_events:
                self.manager.store.append_event(task_id, {"ts": time.time(), "type": kind,
                    **SecretRedactor().value(dict(payload), 2000)})
            self._update(task_id, event_count=count, last_progress_at=time.time())

    async def _authority(self, agent_id: str, revision_id: str, capabilities: tuple[str, ...]) -> None:
        from .agent_authority import AgentAuthorityResolver
        resolver = AgentAuthorityResolver(self.db, config=self.config)
        for capability in capabilities:
            decision = await resolver.resolve(agent_id=agent_id, revision_id=revision_id,
                required_capability=capability, tool_capabilities=capabilities, harness_capabilities=capabilities)
            if not decision.allowed:
                raise LocalAgentTaskError("permission_denied", "Current Agent authority does not permit this task scope", 403)

    async def _preflight_commands(self, spec: LocalAgentTaskSpec, run_id: str) -> None:
        from ..agent_harness.runner import _scoped_backend
        from ..security.agent_run_scope import AgentRunScope
        command_workspace_root(self.config, spec.workspace)
        scope = AgentRunScope.for_repository(spec.workspace, run_id=run_id, workspace_access_level="write")
        probe = asyncio.create_task(asyncio.to_thread(_scoped_backend, scope, Path(spec.workspace)))
        try:
            prepared = await asyncio.shield(probe)
        except asyncio.CancelledError:
            # The existing readiness smoke has its own finite process timeout;
            # do not abandon its owning thread on an HTTP cancellation.
            await asyncio.shield(probe)
            raise
        if isinstance(prepared, str):
            raise LocalAgentTaskError("command_runtime_unavailable", "The existing file-scoped WSL/bubblewrap runtime is not ready", 409)

    async def _admit(self, record: Mapping[str, Any], spec: LocalAgentTaskSpec, *, actor_role: str) -> None:
        from sqlalchemy import select
        from ..memory.conversation_repository import ConversationRepository
        from ..memory.models import AgentWorkItem
        from .agent_model_routing import AgentRoutingPins, resolve_agent_role_route
        from .agent_run_service import AgentRunService, _binding_audit_projection
        from .agent_work_runtime import WorkCandidate
        from .conversation_agent_service import ConversationAgentService

        task_id, owner = record["task_id"], record["owner"]
        aid = selected_agent_id(self.config, spec.agent_id)
        caps = task_capabilities(spec)
        if spec.allow_commands:
            command_workspace_root(self.config, spec.workspace)
        agents = ConversationAgentService(self.db, config=self.config)
        agent, revision = await agents.validate_agent_selection(agent_id=aid, actor_user_id=owner, actor_role=actor_role)
        route = resolve_agent_role_route(self.config, AgentRoutingPins.from_revision(revision), "operator")
        validate_canonical_local_route(self.config, spec, route)
        await self._authority(aid, str(revision.id), caps)
        # The ordinary session repository and binding service remain the only
        # identity boundary. Never attach delegation to an unrelated Chat.
        async def remember_session(_dbs, row):
            # Save the allocated identity before the repository's commit
            # await, so a disconnected admission never loses its own session.
            self._update(task_id, session_id=str(row.id))

        repository_session = await self._session()
        try:
            conversation = await ConversationRepository(repository_session).create_session(user_id=owner, character_name="",
                title=spec.goal[:120], owner_participant={"participant_id": owner, "role": "owner"},
                initialize_agent=remember_session)
        finally:
            await repository_session.close()
        sid = str(conversation.id)
        self._update(task_id, session_id=sid)
        bound = await agents.bind_session_agent(session_id=sid, agent_id=aid, actor_user_id=owner,
            actor_role=actor_role, expected_agent_id=None)
        binding = bound.binding
        if binding is None:
            raise LocalAgentTaskError("agent_revision_missing", "The selected Agent has no immutable binding", 409)
        # Recheck the actual persisted snapshot after the selection transaction.
        route = resolve_agent_role_route(self.config, binding.routing_pins(), "operator")
        resolved = validate_canonical_local_route(self.config, spec, route)
        await self._authority(aid, binding.agent_revision_id, caps)
        self._update(task_id, agent_id=aid, agent_revision_id=binding.agent_revision_id,
            binding_id=binding.binding_id, snapshot_hash=binding.snapshot_hash, resolved_route=resolved, llm=resolved)
        deadline = datetime.utcfromtimestamp(float(record["deadline_at"]))
        candidate = WorkCandidate(source_type=ADAPTER_KEY, source_id=task_id,
            source_revision=record["request_fingerprint"], intent_key="delegate", domain="local_workspace",
            conversation_session_id=sid, assigned_agent_id=aid, agent_revision_id=binding.agent_revision_id,
            required_capabilities=caps, execution_adapter=ADAPTER_KEY, max_attempts=1,
            # Until Run linkage is durable this item can only expire, never run.
            not_before=deadline, deadline=deadline,
            concurrency_key=workspace_concurrency_key(spec.workspace),
            metadata={"local_task_id": task_id, "owner_user_id": owner, "session_id": sid,
                "request_fingerprint": record["request_fingerprint"], "binding_id": binding.binding_id})
        work_session = await self._session()
        try:
            work = await self.coordinator.materialize(candidate, session=work_session)
            if not work or not work.get("id"):
                raise LocalAgentTaskError("canonical_runtime_unavailable", "Common AgentWork admission is unavailable", 409)
            wid = str(work["id"])
            self._update(task_id, work_id=wid)
            await work_session.commit()
        except BaseException:
            await work_session.rollback()
            raise
        finally:
            await work_session.close()
        manifest = {"schema_version": 2, "source": ADAPTER_KEY, "agent_id": aid,
            "agent_revision_id": binding.agent_revision_id, "work_item_id": wid,
            "team_id": binding.agent_team_id, "execution_profile_id": binding.execution_profile_id,
            "capabilities": list(caps), "route_snapshot": binding.routing_pins().route_snapshot.get("roles", binding.routing_pins().route_snapshot)}
        run = await AgentRunService(self.db, config=self.config).create_run(session_id=sid, user_id=owner,
            agent_id=aid, agent_revision_id=binding.agent_revision_id, work_item_id=wid, work_item_attempt=1,
            run_type="autonomous_work", title=spec.goal[:120], objective=spec.goal,
            provider=resolved["provider"], model=resolved["model"], acting_subagent_id=route.acting_subagent_id,
            execution_manifest=manifest, metadata={"local_task_id": task_id,
                "agent_binding": _binding_audit_projection(binding), "requested_route": record["requested_route"],
                "resolved_route": resolved, "workspace": spec.workspace})
        rid = str(run["id"])
        self._update(task_id, agent_run_id=rid)
        if spec.allow_commands:
            await self._preflight_commands(spec, rid)
            if float(record["deadline_at"]) <= time.time():
                raise LocalAgentTaskError("task_timeout", "The task deadline elapsed during command preflight", 409)
        dbs = await self._session()
        try:
            row = (await dbs.execute(select(AgentWorkItem).where(AgentWorkItem.id == uuid.UUID(wid)).with_for_update())).scalars().first()
            if (row is None or row.state != "pending" or row.active_agent_run_id is not None
                    or str(row.assigned_agent_id) != aid or str(row.agent_revision_id) != binding.agent_revision_id):
                raise LocalAgentTaskError("canonical_runtime_unavailable", "Work admission fence changed", 409)
            row.active_agent_run_id = uuid.UUID(rid)
            row.not_before = datetime.utcnow()
            await dbs.commit()
        except BaseException:
            await dbs.rollback()
            raise
        finally:
            await dbs.close()
        self._update(task_id, execution_state="pending", latest_activity="queued on common AgentWork")
        self._event(task_id, "admitted", {"agent_id": aid, "agent_revision_id": binding.agent_revision_id,
            "agent_run_id": rid, "work_id": wid, "session_id": sid, "resolved_route": resolved})

    async def _cancel_admission(self, task_id: str, owner: str, reason: str) -> None:
        """Reconcile an unknown admission result using durable source identity."""
        from sqlalchemy import select
        from ..memory.models import AgentRun, AgentWorkItem
        from .agent_run_service import AgentRunService

        self._update(task_id, cancel_requested=True)
        await self.adapter.abort_task(task_id)
        dbs = await self._session()
        try:
            work = (await dbs.execute(select(AgentWorkItem).where(
                AgentWorkItem.source_type == ADAPTER_KEY, AgentWorkItem.source_id == task_id))).scalars().first()
            if work is None:
                return
            metadata = work.metadata_json or {}
            if metadata.get("owner_user_id") != owner:
                raise LocalAgentTaskError("permission_denied", "Admission owner binding changed", 403)
            work_id = str(work.id)
            runs = (await dbs.execute(select(AgentRun).where(AgentRun.work_item_id == work.id))).scalars().all()
            run_ids = [str(row.id) for row in runs if str(row.user_id) == owner]
            self._update(task_id, work_id=work_id, **({"agent_run_id": run_ids[-1]} if run_ids else {}))
        finally:
            await dbs.close()
        cancelled = await self.coordinator.cancel(work_id, actor={"id": owner, "actor_type": "human"}, reason=reason)
        if cancelled and task_id not in self.adapter._active:
            for run_id in run_ids:
                await AgentRunService(self.db, config=self.config).cancel_run(run_id, message=reason)

    async def _shield_admission_cleanup(self, task_id: str, owner: str, reason: str) -> None:
        cleanup = asyncio.create_task(self._cancel_admission(task_id, owner, reason))
        self._cleanup_tasks.add(cleanup)
        def done(task):
            self._cleanup_tasks.discard(task)
            if not task.cancelled():
                task.exception()
        cleanup.add_done_callback(done)
        try:
            await asyncio.shield(cleanup)
        except Exception:
            # Keep uncertainty visible. Never retry the original admission.
            self._update(task_id, status="interrupted", execution_state="uncertain", failure_reason="admission_cleanup_uncertain")

    async def submit(self, payload: Mapping[str, Any], *, owner: str, idempotency_key: str | None = None,
                     actor_role: str = "") -> tuple[dict[str, Any], bool]:
        owner = _owner(owner)
        payload = {"allow_commands": False, **dict(payload)}
        spec = validate_task_request(payload, allowed_roots=self.manager.allowed_roots,
            limits=self.manager.limits, route_expectations=True)
        if spec.codex_connection is not None:
            raise LocalAgentTaskError("agent_route_mismatch", "A canonical local task cannot adopt a Codex connection", 409)
        key = idempotency_key or payload.get("idempotency_key")
        if key is not None and (not isinstance(key, str) or not IDEMPOTENCY_KEY_RE.fullmatch(key)):
            raise LocalAgentTaskError("invalid_input", "idempotency_key is invalid")
        fingerprint = spec.fingerprint()
        async with self._submit_lock:
            existing = self.manager.store.find_key(owner, key) if key else None
            if existing:
                record = self._record(existing["task_id"], owner)
                if existing.get("fingerprint") != fingerprint:
                    raise LocalAgentTaskError("idempotency_conflict", "idempotency_key was already used for a different request", 409)
                return await self.get(record["task_id"], owner=owner), False
            caps = task_capabilities(spec)
            # Resolve defaults before creating the receipt; no arbitrary Agent.
            aid = selected_agent_id(self.config, spec.agent_id)
            if not self.coordinator._runtime_enabled() or self.coordinator.adapters.get(ADAPTER_KEY) is not self.adapter:
                raise LocalAgentTaskError("canonical_runtime_unavailable", "Common AgentWork is not connected", 409)
            require_workspace_runtime()
            task_id = uuid.uuid5(uuid.NAMESPACE_URL, f"aoitalk-local-task:{owner}:{key}").hex if key else uuid.uuid4().hex
            now = time.time()
            record = {"task_id": task_id, "owner": owner, "executor": "aoitalk_agent", "status": "queued",
                "execution_state": "accepted", "request": spec.to_dict(), "request_fingerprint": fingerprint,
                "requested_route": {k: getattr(spec, k) for k in ("provider", "model", "effort") if getattr(spec, k) is not None},
                "agent_id": aid, "required_capabilities": list(caps), "idempotency_key": key,
                "created_at": now, "updated_at": now, "last_progress_at": now, "deadline_at": now + spec.timeout_sec, "boot_id": self.manager.boot_id,
                "event_count": 0, "counters": {}, "cancel_requested": False}
            try:
                self.manager.store.create(record)
            except FileExistsError:
                # The deterministic receipt is the cross-process first-writer
                # fence, including a crash before the auxiliary key index.
                record = self._record(task_id, owner)
                if record.get("request_fingerprint") != fingerprint:
                    raise LocalAgentTaskError("idempotency_conflict", "idempotency_key was already used for a different request", 409) from None
                return await self.get(task_id, owner=owner), False
            try:
                if key:
                    self.manager.store.put_key(owner, key, task_id, fingerprint)
                await self._admit(record, spec, actor_role=actor_role)
                return await self.get(task_id, owner=owner), True
            except asyncio.CancelledError:
                self._update(task_id, status="interrupted", execution_state="uncertain", failure_reason="admission_interrupted")
                await self._shield_admission_cleanup(task_id, owner, "admission_interrupted")
                raise
            except Exception as exc:
                safe = _error(exc)
                self._update(task_id, status="failed", execution_state="admission_failed", failure_reason=safe.code,
                    finished_at=time.time())
                await self._shield_admission_cleanup(task_id, owner, "admission_failed")
                # No automatic admission replay. A gated Work remains unable
                # to run, and the same key always returns this receipt.
                raise safe from None

    async def _work_projection(self, record: Mapping[str, Any]) -> dict[str, Any]:
        from ..memory.models import AgentWorkItem
        if not record.get("work_id"):
            return {}
        dbs = await self._session()
        try:
            row = await dbs.get(AgentWorkItem, uuid.UUID(record["work_id"]))
            if row is None and record.get("execution_state") in {"admission_failed", "uncertain"}:
                return {}
            if row is None or row.source_type != ADAPTER_KEY or row.source_id != record["task_id"]:
                raise LocalAgentTaskError("canonical_runtime_unavailable", "Task Work binding is unavailable", 409)
            return {**row.to_safe_dict(), "admission_released": bool(row.active_agent_run_id
                and row.not_before is not None and row.deadline is not None and row.not_before < row.deadline),
                "worker_alive": bool(row.state in {"claimed", "running"} and row.lease_expires_at is not None
                    and row.lease_expires_at > datetime.utcnow())}
        finally:
            await dbs.close()

    async def get(self, task_id: str, *, owner: str, allow_profile: bool = True, **_: Any) -> dict[str, Any]:
        record = self._record(task_id, owner)
        if record.get("executor") != "aoitalk_agent":
            return self.manager.get(task_id, owner=owner, allow_profile=allow_profile)
        work = await self._work_projection(record)
        state = work.get("state")
        if state:
            status = WORK_STATUS.get(state, "waiting")
            if state == "cancelled" and task_id in self.adapter._active:
                status, state = "running", "cancelling"
            # Pending gated admission after process loss is never replayed.
            if (state == "pending" and not work.get("admission_released")
                    and record.get("execution_state") == "accepted" and record.get("boot_id") != self.manager.boot_id):
                status, state = "interrupted", "uncertain"
            # A running provider cold-start remains observable separately.
            execution_state = record.get("execution_state") if state == "running" and record.get("execution_state") == "loading_model" else state
            record = {**record, "status": status, "execution_state": execution_state,
                "failure_reason": (record.get("failure_reason") if record.get("execution_state") == "admission_failed"
                    else work.get("safe_error_code") or record.get("failure_reason"))}
        elif record.get("execution_state") == "accepted" and record.get("boot_id") != self.manager.boot_id:
            record = {**record, "status": "interrupted", "execution_state": "uncertain", "failure_reason": "admission_interrupted"}
        view = self.manager.view(record)
        if view["status"] == "running":
            # The common lease heartbeat, rather than a provider response or
            # this process's legacy worker thread, proves liveness.
            view["worker_alive"] = bool(work.get("worker_alive"))
            view["stalled"] = not view["worker_alive"] or (
                view.get("execution_state") not in {"loading_model", "cancelling"}
                and (view.get("seconds_since_progress") or 0) > view.get("stall_threshold_sec", 300))
        return view

    async def list(self, *, owner: str, limit: int = 20, status: str | None = None, allow_profile: bool = True, **_: Any) -> list[dict[str, Any]]:
        if status is not None and status not in TASK_STATES:
            raise LocalAgentTaskError("invalid_input", "unknown status filter")
        result = []
        for record in self.manager.store.list():
            if record.get("owner") != owner or (not allow_profile and record.get("codex_profile_binding") is not None):
                continue
            view = await self.get(record["task_id"], owner=owner, allow_profile=allow_profile)
            if status is None or view["status"] == status:
                result.append(view)
            if len(result) >= max(1, min(limit, 100)):
                break
        return result

    async def tail(self, task_id: str, *, owner: str, limit: int = 50, max_bytes: int = 16000, allow_profile: bool = True, **_: Any) -> dict[str, Any]:
        view = await self.get(task_id, owner=owner, allow_profile=allow_profile)
        events, truncated = self.manager.store.tail(task_id, limit=max(1, min(limit, 200)), max_bytes=max(1024, min(max_bytes, 64000)))
        return {"task_id": task_id, "status": view["status"], "execution_state": view.get("execution_state"),
            "events": events, "truncated": truncated, "event_count": view["event_count"]}

    async def cancel(self, task_id: str, *, owner: str, allow_profile: bool = True, **_: Any) -> dict[str, Any]:
        record = self._record(task_id, owner)
        if record.get("executor") != "aoitalk_agent":
            return await asyncio.to_thread(self.manager.cancel, task_id, owner=owner, allow_profile=allow_profile)
        current = await self.get(task_id, owner=owner)
        if current["status"] in TERMINAL_STATES:
            return current
        await self._cancel_admission(task_id, owner, "owner_cancelled")
        if not self.manager.store.load(task_id).get("work_id"):
            self._update(task_id, status="cancelled", execution_state="cancelled", finished_at=time.time())
        return await self.get(task_id, owner=owner)


class LocalTaskWorkSource:
    source_type = ADAPTER_KEY

    def __init__(self, service: CanonicalLocalAgentTasks):
        self.service = service

    async def discover(self, session: Any, *, now: Any = None):
        # Public admission materializes exactly once. Discovery never replays
        # a lost receipt or converts a human request into a second Work item.
        return ()

    async def refresh(self, session: Any, claim: Any) -> bool:
        try:
            record = self.service.manager.store.load(claim.source_id)
            return (record.get("executor") == "aoitalk_agent" and not record.get("cancel_requested")
                and record.get("work_id") == claim.work_item_id
                and record.get("request_fingerprint") == claim.source_revision
                and record.get("owner") == claim.metadata.get("owner_user_id")
                and record.get("agent_id") == claim.assigned_agent_id
                and record.get("agent_revision_id") == claim.agent_revision_id
                and record.get("session_id") == claim.conversation_session_id)
        except LocalAgentTaskError:
            return False


class LocalTaskExecutionAdapter:
    adapter_key = ADAPTER_KEY

    def __init__(self, service: CanonicalLocalAgentTasks):
        self.service = service
        self._active: dict[str, tuple[threading.Event, asyncio.Task[Any]]] = {}

    def required_capabilities(self, claim: Any) -> tuple[str, ...]:
        return tuple(claim.required_capabilities)

    async def abort_task(self, task_id: str) -> None:
        active = self._active.get(task_id)
        if active is not None:
            active[0].set()

    async def abort(self, claim: Any) -> None:
        await self.abort_task(claim.source_id)

    async def recover_stale(self, session: Any, row: Any, *, coordinator: Any, now: Any) -> bool:
        if row.state not in {"claimed", "running"}:
            return False
        await coordinator._transition_locked(session, row, "uncertain", token=None,
            error_code="interrupted_by_restart", error_message="Local task execution cannot be replayed safely")
        return True

    async def _checkpoint(self, claim: Any, run_id: str, record: Mapping[str, Any]) -> None:
        from ..memory.models import AgentRun, AgentWorkItem
        service = self.service
        dbs = await service._session()
        try:
            row = await dbs.get(AgentWorkItem, uuid.UUID(claim.work_item_id))
            current_run = await dbs.get(AgentRun, uuid.UUID(run_id))
            if (current_run is None or current_run.status != "running"
                    or str(current_run.user_id) != record["owner"]
                    or str(current_run.session_id) != record["session_id"]
                    or str(current_run.agent_id) != record["agent_id"]
                    or str(current_run.agent_revision_id) != record["agent_revision_id"]
                    or str(current_run.work_item_id) != claim.work_item_id
                    or current_run.work_item_attempt != claim.attempt):
                raise LocalAgentTaskError("permission_denied", "The canonical Run binding is no longer active", 403)
            if row is not None and row.deadline is not None and row.deadline <= datetime.utcnow():
                raise LocalAgentTaskError("task_timeout", "The task deadline has elapsed", 409)
            if (row is None or row.state != "running" or row.lease_token != claim.lease_token
                    or row.lease_owner != claim.lease_owner or row.attempt_count != claim.attempt
                    or row.source_type != ADAPTER_KEY or row.source_id != claim.source_id
                    or row.source_revision != record["request_fingerprint"]
                    or str(row.assigned_agent_id) != record["agent_id"]
                    or str(row.agent_revision_id) != record["agent_revision_id"]
                    or str(row.conversation_session_id) != record["session_id"]
                    or (row.metadata_json or {}).get("owner_user_id") != record["owner"]
                    or tuple(row.required_capabilities_json or ()) != tuple(record["required_capabilities"])
                    or str(row.active_agent_run_id) != run_id or row.lease_expires_at is None
                    or row.lease_expires_at <= datetime.utcnow()):
                raise LocalAgentTaskError("permission_denied", "The AgentWork lease is no longer active", 403)
        finally:
            await dbs.close()
        if service.manager.store.load(claim.source_id).get("cancel_requested"):
            raise LocalAgentTaskError("permission_denied", "The task has been cancelled", 403)
        await service._authority(record["agent_id"], record["agent_revision_id"], tuple(record["required_capabilities"]))

    async def _command_scope(self, claim: Any, run_id: str, record: Mapping[str, Any], spec: LocalAgentTaskSpec):
        from .agent_authority import AgentAuthorityResolver
        from .harness_execution_scope_service import build_agent_work_harness_scope
        from ..security.harness_execution_scope import ResourceLimits
        await self._checkpoint(claim, run_id, record)
        decision = await AgentAuthorityResolver(self.service.db, config=self.service.config).resolve(
            agent_id=record["agent_id"], revision_id=record["agent_revision_id"],
            required_capability="command_execute", tool_capabilities=claim.required_capabilities,
            harness_capabilities=claim.required_capabilities)
        if (not decision.allowed or not decision.organization_id or decision.agent_id != record["agent_id"]
                or decision.agent_revision_id != record["agent_revision_id"]):
            raise LocalAgentTaskError("permission_denied", "Current Agent command authority is unavailable", 403)
        root = command_workspace_root(self.service.config, spec.workspace)
        await self.service._preflight_commands(spec, run_id)
        await self._checkpoint(claim, run_id, record)
        return build_agent_work_harness_scope(workspace_path=spec.workspace, harness_workspace_root=root,
            resource_limits=ResourceLimits(timeout_seconds=min(spec.timeout_sec, spec.command_timeout_sec, 3600),
                max_processes=32, max_output_bytes=min(self.service.manager.limits.tool_result_chars * 4, 1024 * 1024),
                max_disk_bytes=256 * 1024 * 1024, max_background_seconds=min(spec.timeout_sec, 3600)),
            agent_id=record["agent_id"], organization_id=decision.organization_id, project_id=None, app_id=None,
            run_id=run_id, audit_id=f"local-task:{claim.work_item_id}:{claim.attempt}",
            mode="workspace_write", delete_workspace=False)

    @asynccontextmanager
    async def _workspace_lease(self, claim: Any, run_id: str, record: Mapping[str, Any], spec: LocalAgentTaskSpec, harness_scope: Any = None):
        from ..security.agent_run_scope import AgentRunScope
        from ..llm.tool_policy import set_current_agent_workspace_scope, reset_current_agent_workspace_scope
        from .agent_dialogue_workspace import AgentDialogueWorkspaceManager

        # This is the existing structured-file scope, reconstructed only after
        # exact persisted request/owner/claim validation. Only a separately
        # verified HarnessExecutionScope enables native commands; the plain
        # AgentRunScope remains structured-file-only with no external roots.
        scope = harness_scope.to_agent_run_scope() if harness_scope is not None else AgentRunScope.for_repository(
            spec.workspace, run_id=run_id, workspace_access_level="write" if spec.mode == "mutate" else "read")
        manager = AgentDialogueWorkspaceManager(runtime_cache_root=self.service.manager.store.root / "_runtime")
        workspace = manager.allocate(session_id=record["session_id"], agent_id=record["agent_id"],
            provider_id=spec.provider, model_id=spec.model, run_id=run_id, owner_id=record["owner"],
            readonly=spec.mode == "read_only", workspace_path=spec.workspace,
            authorized_scope=scope, native_allowed=harness_scope is not None)
        async with manager.lease(workspace, owner_id=record["owner"]):
            await self._checkpoint(claim, run_id, record)
            token = set_current_agent_workspace_scope(session_id=workspace.session_id,
                agent_id=workspace.agent_id, run_id=run_id, resource_key=workspace.resource_key,
                readonly=workspace.readonly, native_allowed=workspace.native_allowed)
            try:
                yield workspace
            finally:
                reset_current_agent_workspace_scope(token)

    async def execute(self, claim: Any, *, coordinator: Any, run_id: str, run: Mapping[str, Any], **_: Any):
        from .agent_run_service import AgentRunService
        from .agent_work_runtime import ExecutionOutcome
        from .chat_execution_adapter import bind_chat_agent_execution, resolve_chat_agent_execution
        from .turn_context import reset_turn_context, set_turn_context
        from .scoped_local_mcp import scoped_local_mcp_tools

        service = self.service
        try:
            require_workspace_runtime()
        except LocalAgentTaskError as exc:
            return ExecutionOutcome(classification="blocked", error_code=exc.code, error_message=exc.message)
        record = service.manager.store.load(claim.source_id)
        if (not await LocalTaskWorkSource(service).refresh(None, claim)
                or record.get("agent_run_id") != run_id or str(run.get("user_id")) != record.get("owner")
                or str(run.get("session_id")) != record.get("session_id")):
            return ExecutionOutcome(classification="blocked", error_code="permission_denied")
        spec = validate_task_request(record["request"], allowed_roots=service.manager.allowed_roots,
            limits=service.manager.limits, route_expectations=True)
        if (spec.fingerprint() != record["request_fingerprint"] or task_capabilities(spec) != tuple(claim.required_capabilities)
                or tuple(record["required_capabilities"]) != tuple(claim.required_capabilities)):
            return ExecutionOutcome(classification="blocked", error_code="permission_denied")
        execution = await resolve_chat_agent_execution(session_id=record["session_id"], run=run,
            config=service.config, db_manager=service.db)
        if execution is None or execution.binding.binding_id != record.get("binding_id"):
            return ExecutionOutcome(classification="blocked", error_code="agent_revision_missing")
        try:
            resolved = validate_canonical_local_route(service.config, spec, execution.route)
        except LocalAgentTaskError as exc:
            return ExecutionOutcome(classification="blocked", error_code=exc.code, error_message=exc.message)
        if resolved != record.get("resolved_route"):
            return ExecutionOutcome(classification="blocked", error_code="agent_route_mismatch")
        if record.get("deadline_at") is None or float(record["deadline_at"]) <= time.time():
            return ExecutionOutcome(classification="permanent", error_code="timeout")
        remaining = max(1, min(spec.timeout_sec, int(float(record["deadline_at"]) - time.time())))
        # An empty canonical effort means the provider/model default. Keep it
        # as None for the runner so resolve_task_route can apply that policy.
        spec = dataclasses.replace(
            spec,
            provider=resolved["provider"],
            model=resolved["model"],
            effort=resolved["effort"] or None,
            timeout_sec=remaining,
        )
        await self._checkpoint(claim, run_id, record)
        try:
            harness_scope = await self._command_scope(claim, run_id, record, spec) if spec.allow_commands else None
        except LocalAgentTaskError as exc:
            return ExecutionOutcome(classification="blocked" if exc.code == "permission_denied" else "permanent",
                error_code=exc.code, error_message="The scoped command execution boundary is unavailable")
        stop = threading.Event()
        audit_failed = threading.Event()
        loop = asyncio.get_running_loop()
        context_token = set_turn_context(user_id=record["owner"], session_id=record["session_id"],
            suppress_automatic_context=True)
        try:
            async with self._workspace_lease(claim, run_id, record, spec, harness_scope) as workspace, \
                    bind_chat_agent_execution(execution, user_id=record["owner"], session_id=record["session_id"],
                        project_id=None, config=service.config):
                async with scoped_local_mcp_tools(config=service.config, workspace=spec.workspace,
                        owner_id=record["owner"], agent_run_id=run_id) as tools:
                    def checkpoint():
                        if audit_failed.is_set():
                            raise LocalAgentTaskError("audit_write_failed", "The canonical audit ledger could not be written", 409)
                        future = asyncio.run_coroutine_threadsafe(self._checkpoint(claim, run_id, record), loop)
                        try:
                            future.result(timeout=30)
                        except BaseException:
                            future.cancel()
                            stop.set()
                            raise

                    def update(fields):
                        allowed = {k: v for k, v in fields.items() if k in {"execution_state", "current_step", "latest_activity", "counters", "baseline"}}
                        service._update(claim.source_id, **allowed, last_progress_at=time.time(), heartbeat_at=time.time())

                    def emit(kind, fields):
                        service._event(claim.source_id, kind, fields)
                        # The existing run ledger receives the same bounded,
                        # redacted events; it is not a separate audit identity.
                        future = asyncio.run_coroutine_threadsafe(AgentRunService(service.db, config=service.config).record_event(
                            run_id, event_type="local_task." + kind, payload=SecretRedactor().value(dict(fields), 2000)), loop)
                        try:
                            if future.result(timeout=30) is None:
                                raise RuntimeError("canonical audit event was not persisted")
                        except BaseException:
                            audit_failed.set()
                            raise

                    runner = LocalAgentTaskRunner(service.config, collect_git_metadata=False, harness_execution_scope=harness_scope)
                    service._update(claim.source_id, status="running", execution_state="running", started_at=time.time())
                    worker = asyncio.create_task(asyncio.to_thread(runner.run_inline, spec,
                        task_id=run_id, user_id=record["owner"], cancel_event=stop,
                        limits=service.manager.limits, extra_tools=tools, agent_system_prompt=execution.system_prompt,
                        execution_checkpoint=checkpoint, update=update, emit=emit))
                    self._active[claim.source_id] = (stop, worker)
                    try:
                        outcome = await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        stop.set()
                        # Do not close the MCP owner loop or release ownership
                        # while its synchronous worker can still invoke tools.
                        await asyncio.shield(worker)
                        raise
                    finally:
                        if worker.done():
                            self._active.pop(claim.source_id, None)
        finally:
            reset_turn_context(context_token)
            if harness_scope is not None:
                from .harness_execution_scope_service import release_created_scratch_roots

                try:
                    release_created_scratch_roots(harness_scope)
                except Exception:  # noqa: BLE001 - cleanup must not mask the outcome
                    pass
        if service.manager.store.load(claim.source_id).get("cancel_requested"):
            await AgentRunService(service.db, config=service.config).cancel_run(run_id)
        service._update(claim.source_id, result=outcome.get("result"), summary_text=outcome.get("summary_text"),
            completion_summary=outcome.get("completion_summary"), changed_files=outcome.get("changed_files") or [],
            failure_reason=outcome.get("failure_reason"), failure_detail=outcome.get("failure_detail"), finished_at=time.time())
        if audit_failed.is_set():
            return ExecutionOutcome(classification="permanent", error_code="audit_write_failed")
        classification = {"completed": "succeeded", "cancelled": "cancelled", "waiting_approval": "awaiting_approval"}.get(outcome.get("status"), "permanent")
        if outcome.get("failure_reason") in {"authority_revoked", "agent_blocked"}:
            classification = "blocked"
        if outcome.get("failure_reason") in {"approval_required", "awaiting_approval"}:
            classification = "awaiting_approval"
        return ExecutionOutcome(classification=classification, result_summary=outcome.get("summary_text"),
            error_code=outcome.get("failure_reason"), result=outcome.get("result"),
            metadata={"local_task_id": claim.source_id, "resolved_route": resolved})


def bind_local_agent_task_coordinator(manager: Any, coordinator: Any) -> CanonicalLocalAgentTasks:
    existing = getattr(manager, "canonical", None)
    if existing is not None:
        if existing.coordinator is not coordinator:
            raise RuntimeError("Local tasks are already bound to another coordinator")
        return existing
    service = CanonicalLocalAgentTasks(manager, coordinator)
    coordinator.register_source(LocalTaskWorkSource(service))
    coordinator.register_adapter(service.adapter)
    manager.canonical = service
    return service
