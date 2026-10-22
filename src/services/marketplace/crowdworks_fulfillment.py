"""AgentWork fulfillment lanes for accepted CrowdWorks opportunities.

Writing / research / data entry / general work is produced by the CrowdWorks
employee as a review artifact.  Web and app development is routed to the
Web制作担当 / アプリ開発担当 specialists, which develop in an isolated client
workspace (see ``client_development``).  Every lane stops at READY_FOR_REVIEW;
final delivery is always a human action.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from ...memory.models import AgentRun, AgentRevision
from ...memory.models.operations import EngagementOpportunity, ExternalAction
from ..agent_model_routing import AgentModelRouteError, load_pinned_agent_pins
from ..agent_work_runtime import ExecutionOutcome, WorkCandidate
from .client_development import CrowdWorksDevelopmentAdapter, development_candidate
from .crowdworks_specialists import (
    DEVELOPMENT_WORKER_KINDS,
    GENERAL_WORKER_KINDS,
    is_development_kind,
    normalize_worker_kind,
)
from .crowdworks_workflow import CrowdWorksWorkflowService


# ``code`` remains registered only so work items materialized before the
# specialist split can still settle through the development lane.
WORKER_KINDS = GENERAL_WORKER_KINDS


class CrowdWorksFulfillmentWorkSource:
    source_type = "marketplace_crowdworks_fulfillment"

    async def discover(self, session, *, now=None):
        rows = (
            await session.scalars(
                select(EngagementOpportunity)
                .where(EngagementOpportunity.status == "contract_accepted")
                .order_by(EngagementOpportunity.updated_at.asc(), EngagementOpportunity.id.asc())
                .limit(200)
            )
        ).all()
        result: list[WorkCandidate] = []
        for opportunity in rows:
            action = await session.scalar(
                select(ExternalAction)
                .where(
                    ExternalAction.opportunity_id == opportunity.id,
                    ExternalAction.action_type == "engagement.submit_application",
                    ExternalAction.status == "succeeded",
                    ExternalAction.origin_agent_id.is_not(None),
                )
                .order_by(ExternalAction.created_at.desc())
                .limit(1)
            )
            if action is None:
                continue
            metadata = dict(opportunity.metadata_json or {})
            kind = normalize_worker_kind(metadata.get("worker_kind"))
            if is_development_kind(kind):
                candidate = await development_candidate(
                    session, opportunity, kind, source_type=self.source_type
                )
                if candidate is not None:
                    result.append(candidate)
                continue
            origin_run = await session.get(AgentRun, action.origin_agent_run_id)
            revision = (
                await session.get(AgentRevision, origin_run.agent_revision_id)
                if origin_run is not None and origin_run.agent_revision_id is not None
                else None
            )
            if revision is None or revision.agent_id != action.origin_agent_id:
                continue
            source_revision = ":".join(
                [
                    str(opportunity.source_snapshot_hash or "no-snapshot"),
                    str(metadata.get("latest_evaluation_id") or "no-evaluation"),
                    kind,
                ]
            )
            result.append(
                WorkCandidate(
                    source_type=self.source_type,
                    source_id=str(opportunity.id),
                    source_revision=source_revision,
                    intent_key=f"marketplace.crowdworks.fulfill:{kind}",
                    domain="marketplace",
                    project_id=str(opportunity.project_id) if opportunity.project_id else None,
                    assigned_agent_id=str(action.origin_agent_id),
                    agent_revision_id=str(revision.id),
                    required_capabilities=(),
                    execution_adapter=f"crowdworks_fulfillment_{kind}",
                    priority=20,
                    max_attempts=3,
                    concurrency_key=f"crowdworks-fulfillment:{opportunity.id}",
                    metadata={"opportunity_id": str(opportunity.id), "worker_kind": kind},
                )
            )
        return result

    async def refresh(self, session, claim):
        try:
            opportunity_id = UUID(str(claim.source_id))
        except (TypeError, ValueError):
            return False
        opportunity = await session.get(EngagementOpportunity, opportunity_id, populate_existing=True)
        if opportunity is None:
            return False
        metadata = dict(opportunity.metadata_json or {})
        kind = normalize_worker_kind(metadata.get("worker_kind"))
        work_item_id = str(getattr(claim, "work_item_id", "") or "")
        if opportunity.status == "ready_for_review":
            # Settlement re-checks the source after the adapter moved the
            # opportunity to READY_FOR_REVIEW.  Only the producing item is
            # still valid; any other claim is stale.
            return bool(work_item_id) and metadata.get("review_work_item_id") == work_item_id
        if opportunity.status != "contract_accepted":
            return False
        if is_development_kind(kind):
            return claim.intent_key in {
                f"marketplace.crowdworks.develop:{kind}",
                "marketplace.crowdworks.fulfill:code",
            }
        return claim.intent_key == f"marketplace.crowdworks.fulfill:{kind}"


class CrowdWorksFulfillmentAdapter:
    def __init__(self, worker_kind: str, *, config: Any = None):
        # ``code`` is accepted for direct compatibility with pre-split
        # callers.  Materialized legacy code work uses
        # ``LegacyCodeFulfillmentAdapter`` below, so the registered adapter
        # set still routes it through real development.
        if worker_kind not in (*WORKER_KINDS, "code"):
            raise ValueError("unsupported CrowdWorks worker kind")
        self.worker_kind = worker_kind
        self.config = config
        self.adapter_key = f"crowdworks_fulfillment_{worker_kind}"

    def required_capabilities(self, claim):
        return ()

    async def execute(self, claim, *, session=None, run=None, run_id=None, **_kwargs):
        if session is None:
            return ExecutionOutcome(classification="transient", error_code="crowdworks_runtime_context_missing")
        opportunity = await session.get(EngagementOpportunity, UUID(str(claim.source_id)))
        if opportunity is None:
            return ExecutionOutcome(classification="blocked", error_code="crowdworks_opportunity_missing")
        try:
            # The deliverable is produced with the route of the exact
            # AgentRevision pinned by this WorkItem/AgentRun, not Base Model.
            pins = await load_pinned_agent_pins(
                session,
                agent_revision_id=claim.agent_revision_id,
                agent_id=claim.assigned_agent_id,
                run=run if isinstance(run, dict) else None,
            )
        except AgentModelRouteError as exc:
            return ExecutionOutcome(classification="blocked", error_code=exc.code, domain_ref=str(opportunity.id))
        owner_user_id = str(opportunity.owner_user_id)
        service = CrowdWorksWorkflowService(config=self.config)
        metadata = dict(opportunity.metadata_json or {})
        selected_kind = normalize_worker_kind(metadata.get("worker_kind") or self.worker_kind)
        try:
            fulfilled = await service.fulfill_opportunity(
                session,
                owner_user_id=owner_user_id,
                opportunity_id=opportunity.id,
                worker_kind=selected_kind,
                pins=pins,
            )
            metadata = dict(opportunity.metadata_json or {})
            metadata["review_work_item_id"] = str(getattr(claim, "work_item_id", "") or "") or None
            opportunity.metadata_json = metadata
            await session.commit()
        except AgentModelRouteError as exc:
            # Invalid Team/Profile/Subagent configuration: retrying cannot fix
            # it and falling back to Base Model is forbidden.
            return ExecutionOutcome(classification="blocked", error_code=exc.code, domain_ref=str(opportunity.id))
        except Exception as exc:
            return ExecutionOutcome(
                classification="transient",
                error_code="crowdworks_fulfillment_failed",
                error_message=(str(exc)[:240] or type(exc).__name__),
                domain_ref=str(opportunity.id),
                metadata={
                    "model_routes": {"fulfillment": service.last_model_generation},
                    "primary_model_route": "fulfillment",
                },
            )
        return ExecutionOutcome(
            classification="succeeded",
            result_summary=f"CrowdWorks {self.worker_kind} work is ready for review",
            result={
                "artifact_version_id": fulfilled.artifact["id"],
                "worker_kind": self.worker_kind,
                "model_routes": fulfilled.model_routes,
            },
            domain_ref=str(opportunity.id),
            domain_status="ready_for_review",
            metadata={
                "model_routes": fulfilled.model_routes,
                "primary_model_route": "fulfillment",
            },
        )


class LegacyCodeFulfillmentAdapter(CrowdWorksDevelopmentAdapter):
    """Settle pre-split ``crowdworks_fulfillment_code`` items via real development."""

    def __init__(self, *, config: Any = None):
        super().__init__("app_development", config=config)
        self.adapter_key = "crowdworks_fulfillment_code"


def fulfillment_adapters(config: Any = None) -> list[Any]:
    adapters: list[Any] = [CrowdWorksFulfillmentAdapter(kind, config=config) for kind in WORKER_KINDS]
    adapters.extend(CrowdWorksDevelopmentAdapter(kind, config=config) for kind in DEVELOPMENT_WORKER_KINDS)
    adapters.append(LegacyCodeFulfillmentAdapter(config=config))
    return adapters


__all__ = [
    "WORKER_KINDS",
    "CrowdWorksFulfillmentWorkSource",
    "CrowdWorksFulfillmentAdapter",
    "LegacyCodeFulfillmentAdapter",
    "fulfillment_adapters",
]
