"""AgentWork source/adapter for the CrowdWorks marketplace workflow."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from uuid import UUID
from typing import Any

from sqlalchemy import select

from ...memory.models import Agent, AgentRevision
from ...memory.models.agent_automation import AgentActionPolicy, AgentActionPolicyRevision
from ...memory.models.operations import ExternalConnection
from ..agent_action_policy_service import window
from ..agent_model_routing import AgentModelRouteError, load_pinned_agent_pins
from ..agent_work_runtime import ExecutionOutcome, WorkCandidate
from .crowdworks_action import CrowdWorksPolicy
from .crowdworks_workflow import CrowdWorksWorkflowService


class CrowdWorksWorkSource:
    source_type = "marketplace_crowdworks"

    def __init__(self, *, policy_service):
        self.policies = policy_service

    async def discover(self, session, *, now=None):
        now = now or datetime.utcnow()
        policies = (
            await session.scalars(
                select(AgentActionPolicy)
                .where(AgentActionPolicy.state == "active")
                .order_by(AgentActionPolicy.id)
                .limit(200)
            )
        ).all()
        result: list[WorkCandidate] = []
        for policy in policies:
            revision = await session.scalar(
                select(AgentActionPolicyRevision)
                .where(
                    AgentActionPolicyRevision.policy_id == policy.id,
                    AgentActionPolicyRevision.action_type == "engagement.submit_application",
                )
                .order_by(AgentActionPolicyRevision.version.desc())
                .limit(1)
            )
            if revision is None or not window(revision, now):
                continue
            try:
                config = CrowdWorksPolicy.model_validate(revision.constraints_json)
                definition = self.policies.registry.require_executable(revision.action_type)
            except Exception:
                continue
            agent = await session.get(Agent, policy.agent_id)
            if agent is None or agent.state != "active":
                continue
            agent_revision = await session.scalar(
                select(AgentRevision)
                .where(AgentRevision.agent_id == policy.agent_id)
                .order_by(AgentRevision.version.desc())
                .limit(1)
            )
            if agent_revision is None:
                continue
            connection = await session.get(ExternalConnection, revision.connection_id)
            if connection is None or connection.provider_key != "crowdworks":
                continue
            bucket_seconds = int(config.scan_interval_minutes) * 60
            utc_now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
            bucket = int(utc_now.timestamp()) // max(300, bucket_seconds)
            # A new identity revision must produce a new immutable work pin,
            # even within the same scan bucket. Never select an older revision
            # just because it had more authority than the current one.
            binding = f"{revision.content_hash}:{agent_revision.id}:{bucket}"
            source_revision = "scan:" + hashlib.sha256(binding.encode("utf-8")).hexdigest()
            result.append(
                WorkCandidate(
                    source_type=self.source_type,
                    source_id=str(policy.id),
                    source_revision=source_revision,
                    intent_key="marketplace.crowdworks.scan",
                    domain="marketplace",
                    project_id=str(connection.project_id) if connection.project_id else None,
                    assigned_agent_id=str(policy.agent_id),
                    agent_revision_id=str(agent_revision.id),
                    required_capabilities=definition.required_capabilities,
                    execution_adapter="crowdworks_workflow",
                    priority=10,
                    max_attempts=3,
                    concurrency_key=f"crowdworks:{connection.id}",
                    metadata={
                        "policy_id": str(policy.id),
                        "policy_revision_id": str(revision.id),
                        "connection_id": str(connection.id),
                    },
                )
            )
        return result

    async def refresh(self, session, claim):
        if claim.intent_key != "marketplace.crowdworks.scan":
            return False
        metadata = claim.metadata if isinstance(claim.metadata, dict) else {}
        policy_id = metadata.get("policy_id")
        revision_id = metadata.get("policy_revision_id")
        if not policy_id or not revision_id:
            return False
        policy = await session.get(AgentActionPolicy, UUID(str(policy_id)))
        revision = await session.get(AgentActionPolicyRevision, UUID(str(revision_id)))
        if (
            policy is None
            or revision is None
            or policy.state != "active"
            or revision.policy_id != policy.id
            or revision.action_type != "engagement.submit_application"
            or str(policy.agent_id) != str(claim.assigned_agent_id)
        ):
            return False
        current = await session.scalar(
            select(AgentActionPolicyRevision)
            .where(AgentActionPolicyRevision.policy_id == policy.id)
            .order_by(AgentActionPolicyRevision.version.desc())
            .limit(1)
        )
        if current is None or current.id != revision.id:
            return False
        agent = await session.get(Agent, policy.agent_id)
        if agent is None or agent.state != "active":
            return False
        current_agent_revision = await session.scalar(
            select(AgentRevision).where(AgentRevision.agent_id == agent.id)
            .order_by(AgentRevision.version.desc()).limit(1)
        )
        # Pins are immutable execution identity, not permission to keep an old
        # revision alive after the user replaces it (including a revocation).
        # A new revision is discovered as a distinct work item in this bucket.
        return current_agent_revision is not None and str(current_agent_revision.id) == str(claim.agent_revision_id)


class CrowdWorksWorkflowAdapter:
    adapter_key = "crowdworks_workflow"

    def __init__(self, *, policy_service, config: Any = None):
        self.policies = policy_service
        self.config = config

    def required_capabilities(self, claim):
        return claim.required_capabilities

    async def execute(self, claim, *, coordinator, session=None, run_id=None, run=None, **_kwargs):
        if session is None or not run_id:
            return ExecutionOutcome(
                classification="transient",
                error_code="crowdworks_runtime_context_missing",
            )
        try:
            # Model routing follows the exact AgentRevision this WorkItem and
            # AgentRun pinned, never the Agent's latest revision.
            pins = await load_pinned_agent_pins(
                session,
                agent_revision_id=claim.agent_revision_id,
                agent_id=claim.assigned_agent_id,
                run=run if isinstance(run, dict) else None,
            )
        except AgentModelRouteError as exc:
            return ExecutionOutcome(classification="blocked", error_code=exc.code)
        metadata = claim.metadata if isinstance(claim.metadata, dict) else {}
        revision = await session.get(AgentActionPolicyRevision, UUID(str(metadata.get("policy_revision_id"))))
        connection = await session.get(ExternalConnection, UUID(str(metadata.get("connection_id"))))
        if revision is None or connection is None:
            return ExecutionOutcome(
                classification="blocked",
                error_code="crowdworks_policy_binding_missing",
            )
        service = CrowdWorksWorkflowService(config=self.config)
        try:
            result = await service.run_cycle(
                session,
                owner_user_id=str(connection.owner_user_id),
                connection=connection,
                policy_revision=revision,
                agent_id=str(claim.assigned_agent_id),
                run_id=str(run_id),
                work_item_id=str(claim.work_item_id),
                action_policy_service=self.policies,
                pins=pins,
            )
        except AgentModelRouteError as exc:
            return ExecutionOutcome(
                classification="blocked",
                error_code=exc.code,
                domain_ref=str(connection.id),
            )
        except Exception as exc:
            code = str(exc)[:240] or type(exc).__name__
            return ExecutionOutcome(
                classification="transient",
                error_code="crowdworks_cycle_failed",
                error_message=code,
            )
        payload = result.to_dict()
        return ExecutionOutcome(
            classification="succeeded",
            result_summary=(
                f"CrowdWorks cycle: +{payload['ingested']} opportunities, "
                f"{payload['evaluated']} evaluated, {payload['proposed']} proposed, "
                f"{payload['ready_for_review']} ready for review"
                + (f", {payload['proposal_deferred']} proposals deferred (action_rate_limited)"
                   if payload.get("proposal_deferred") else "")
            ),
            result={**payload, "model_routes": result.model_routes},
            domain_ref=str(connection.id),
            domain_status="cycle_completed",
            metadata={"model_routes": result.model_routes, "primary_model_route": "evaluation"},
        )


__all__ = ["CrowdWorksWorkSource", "CrowdWorksWorkflowAdapter"]
