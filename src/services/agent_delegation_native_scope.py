"""Read-only native workspace lease for a persisted delegation phase."""

import os
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from ..llm.generation_budget import current_generation_budget
from ..memory.models import AgentDelegation, AgentWorkItem
from ..security.harness_execution_scope import ResourceLimits
from .agent_delegation_native_budget import admit_native_phase_budget
from .agent_delegation_service import DelegationError, delegation_uuid
from .ephemeral_llm import agent_work_generation_timeout


_PROOF_SENTINEL = object()


def _canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class NativeDelegationScopeProof:
    """Process-local evidence of a validated native delegation lease."""

    run_id: str
    session_id: str
    work_item_id: str
    attempt: int
    owner_id: str
    project_id: str
    persona_id: str | None
    agent_id: str
    agent_revision_id: str
    binding_id: str
    binding_snapshot_hash: str
    execution_manifest_hash: str
    authority_hash: str
    lease_owner: str
    lease_expires_at: datetime
    issued_scope: Any = field(repr=False, compare=False)
    workspace: Any = field(repr=False, compare=False)
    _binding: Any = field(repr=False, compare=False)
    _lease_token: str = field(repr=False, compare=False)
    _lifetime: Any = field(repr=False, compare=False)
    _generation: int = field(repr=False, compare=False)
    _sentinel: Any = field(repr=False, compare=False)
    scope_kind = "agent_delegation"

    def __post_init__(self):
        if self._sentinel is not _PROOF_SENTINEL:
            raise PermissionError("native delegation proof must be server issued")

    def assert_lease(self, *, now: datetime | None = None) -> None:
        if (not self._lifetime[0] or self._lifetime[1] != self._generation
                or not self.lease_owner or not self._lease_token):
            raise PermissionError("native delegation proof is no longer current")
        current = now or datetime.now(timezone.utc)
        expiry = self.lease_expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if expiry <= current:
            raise PermissionError("native delegation lease expired")
        self.workspace.assert_provider_entry()

    def assert_run(self, run_id, *, session_id=None) -> None:
        self.assert_lease()
        if str(run_id) != self.run_id or (session_id is not None and str(session_id) != self.session_id):
            raise PermissionError("native delegation run mismatch")

    def assert_context(self, client) -> None:
        from .turn_context import get_turn_context
        from .agent_run_service import get_current_agent_run_id
        from .agent_dialogue_workspace import get_current_agent_dialogue_workspace
        from ..security.agent_run_scope import get_current_run_scope

        self.assert_run(get_current_agent_run_id())
        turn = get_turn_context()
        if (getattr(turn, "agent_execution_binding", None) is not self._binding
                or getattr(client, "_agent_execution_binding", None) is not self._binding
                or str(getattr(turn, "agent_id", "")) != self.agent_id
                or str(getattr(turn, "agent_revision_id", "")) != self.agent_revision_id
                or str(getattr(client, "agent_id", "")) != self.agent_id
                or str(getattr(client, "agent_revision_id", "")) != self.agent_revision_id
                or getattr(client, "_agent_dialogue_workspace", None) is not self.workspace
                or get_current_agent_dialogue_workspace() is not self.workspace
                or get_current_run_scope() is not self.workspace.native_scope
                or str(getattr(client, "model_name", "")) != str(self.workspace.model_id)
                or str(client.cli_backend.get_provider_name()).lower().replace(" ", "-") != str(self.workspace.provider_id)):
            raise PermissionError("native delegation context mismatch")

    def to_metadata(self) -> dict:
        return {key: getattr(self, key) for key in ("run_id", "session_id", "work_item_id", "attempt",
                "owner_id", "project_id", "persona_id", "agent_id", "agent_revision_id", "binding_id",
                "binding_snapshot_hash", "execution_manifest_hash", "authority_hash", "lease_owner")} | {
                    "scope_kind": self.scope_kind, "lease_expires_at": self.lease_expires_at.isoformat()}


@asynccontextmanager
async def native_phase_scope(*, session, service, config, claim, run_id, route,
                             workspace_manager, dialogue_budget=None):
    """Yield (execution, run, workspace, issued scope, launch revalidation)."""
    from .agent_authority import AgentAuthorityResolver
    from .agent_delegation_runtime import AgentDelegationWorkSource, _resolve_phase_execution
    from .harness_execution_scope_service import build_agent_work_harness_scope, release_created_scratch_roots

    parent = current_generation_budget.get()
    admit_native_phase_budget(dialogue_budget=dialogue_budget, parent=parent)
    source = AgentDelegationWorkSource(service)
    issued_pins = None
    issued_scope = None
    workspace = None
    lifetime = [True, 0]

    def _issue_proof(work, order, execution, run, decision):
        workspace.assert_lease_active()
        return NativeDelegationScopeProof(
            run_id=str(run.id), session_id=str(run.session_id), work_item_id=str(work.id),
            attempt=work.attempt_count, owner_id=str(order.created_by), project_id=str(order.project_id),
            persona_id=str(order.persona_id) if order.persona_id else None, agent_id=str(run.agent_id),
            agent_revision_id=str(run.agent_revision_id), binding_id=str(execution.binding.binding_id),
            binding_snapshot_hash=str(execution.binding.snapshot_hash),
            execution_manifest_hash=_canonical_hash(execution.manifest()), authority_hash=_canonical_hash(decision.to_dict()),
            lease_owner=work.lease_owner, lease_expires_at=work.lease_expires_at, issued_scope=issued_scope,
            workspace=workspace, _binding=execution.binding, _lease_token=work.lease_token,
            _lifetime=lifetime, _generation=lifetime[1], _sentinel=_PROOF_SENTINEL)

    async def revalidate_launch():
        lifetime[1] += 1
        revalidate_launch.native_phase_proof = None
        async def checked_work():
            work = await session.get(AgentWorkItem, delegation_uuid(claim.work_item_id), populate_existing=True)
            now = datetime.utcnow()
            expiry = work.lease_expires_at if work else None
            if expiry is not None and expiry.tzinfo is not None:
                now = datetime.now(timezone.utc)
            if (work is None or work.state != "running" or work.lease_owner != claim.lease_owner
                    or work.lease_token != claim.lease_token or work.attempt_count != claim.attempt
                    or expiry is None or expiry <= now or str(work.active_agent_run_id) != str(run_id)
                    or claim.source_type != "employee_delegation" or claim.execution_adapter != "employee_delegation"
                    or work.execution_adapter != claim.execution_adapter
                    or work.source_type != claim.source_type or work.source_id != claim.source_id
                    or work.source_revision != claim.source_revision or work.intent_key != claim.intent_key
                    or str(work.assigned_agent_id) != str(claim.assigned_agent_id)
                    or str(work.agent_revision_id) != str(claim.agent_revision_id)
                    or str(work.project_id) != str(claim.project_id)
                    or (str(work.persona_id) if work.persona_id else None) != claim.persona_id):
                raise DelegationError("delegation_claim_invalid", 409)
            return work

        await checked_work()
        if not await source.refresh(session, claim):
            raise DelegationError("delegation_source_invalid", 409)
        work = await checked_work()
        order = await session.get(AgentDelegation, delegation_uuid(claim.source_id), populate_existing=True)
        await service.validate_authority(session, order)
        execution, run = await _resolve_phase_execution(session, service, config, order, claim, run_id, route)
        # The persisted execution is authoritative; a caller-supplied budget
        # cannot stand in for its limits, including on later launch checks.
        admit_native_phase_budget(dialogue_budget=execution.dialogue_budget, parent=parent)
        if (run.status != "running" or run.work_item_attempt != claim.attempt
                or str(run.agent_id) != str(claim.assigned_agent_id)
                or str(run.agent_revision_id) != str(claim.agent_revision_id)):
            raise DelegationError("delegation_run_invalid", 409)
        decision = await AgentAuthorityResolver(service.identity._db_manager, config=config).resolve(
            session=session, agent_id=claim.assigned_agent_id, revision_id=claim.agent_revision_id,
            project_id=order.project_id, persona_id=order.persona_id, required_capability="workspace_read")
        if (not decision.allowed or not decision.organization_id
                or str(decision.agent_id) != str(claim.assigned_agent_id)
                or str(decision.agent_revision_id) != str(claim.agent_revision_id)):
            raise DelegationError("delegation_workspace_denied", 403)
        pins = (str(order.created_by), str(order.project_id), str(order.persona_id),
                str(decision.organization_id), str(run.session_id),
                *(str(getattr(execution.binding, key, "")) for key in (
                    "binding_id", "snapshot_hash", "agent_id", "agent_revision_id",
                    "revision_version", "revision_content_hash")))
        if issued_pins is not None and pins != issued_pins:
            raise DelegationError("delegation_scope_changed", 409)
        if parent is not None:
            parent.checkpoint()
        if work.deadline is not None:
            now = datetime.now(timezone.utc) if work.deadline.tzinfo else datetime.utcnow()
            if work.deadline <= now:
                raise TimeoutError("delegation_phase_deadline_expired")
        if issued_scope is not None and workspace is not None:
            revalidate_launch.native_phase_proof = _issue_proof(work, order, execution, run, decision)
        return work, order, execution, run, decision

    revalidate_launch.native_phase_proof = None

    work, order, execution, run, decision = await revalidate_launch()
    issued_pins = (str(order.created_by), str(order.project_id), str(order.persona_id),
                   str(decision.organization_id), str(run.session_id),
                *(str(getattr(execution.binding, key, "")) for key in (
                    "binding_id", "snapshot_hash", "agent_id", "agent_revision_id",
                    "revision_version", "revision_content_hash")))
    seconds = agent_work_generation_timeout(config)
    if parent is not None:
        remaining = parent.checkpoint()
        if remaining is not None:
            seconds = min(seconds, remaining)
    if work.deadline is not None:
        now = datetime.now(timezone.utc) if work.deadline.tzinfo else datetime.utcnow()
        seconds = min(seconds, (work.deadline - now).total_seconds())
    if seconds <= 0:
        raise TimeoutError("delegation_phase_deadline_expired")
    ids = dict(session_id=str(run.session_id), agent_id=str(claim.assigned_agent_id),
               provider_id=str(route.provider), model_id=str(route.model), run_id=str(run.id),
               owner_id=str(order.created_by), project_id=str(order.project_id), readonly=True)
    allocated = workspace_manager.allocate(**ids)
    scope = build_agent_work_harness_scope(
        workspace_path=allocated.workspace_path, harness_workspace_root=workspace_manager.workspace_root,
        resource_limits=ResourceLimits(timeout_seconds=seconds, max_processes=32,
            max_output_bytes=256 * 1024, max_disk_bytes=256 * 1024 * 1024, max_background_seconds=seconds),
        agent_id=ids["agent_id"], organization_id=str(decision.organization_id), project_id=ids["project_id"],
        app_id=None, run_id=ids["run_id"], audit_id=f"delegation:{claim.work_item_id}:{claim.attempt}",
        mode="read_only", backend_kind="native_windows" if os.name == "nt" else "wsl_bwrap")
    issued_scope = scope
    try:
        async with workspace_manager.session(**ids, native_allowed=True, authorized_scope=scope.to_agent_run_scope()) as workspace:
            yield execution, run, workspace, scope, revalidate_launch
    finally:
        lifetime[0] = False
        revalidate_launch.native_phase_proof = None
        release_created_scratch_roots(scope)
