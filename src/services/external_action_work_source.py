"""Generic external actions use the existing durable AgentWork queue."""
from sqlalchemy import select

from ..memory.models import AgentWorkItem
from ..memory.models.operations import ExternalAction
from .agent_action_policy_service import uid
from .agent_work_runtime import WorkCandidate
from .external_action_execution_service import action_source_revision
from .integration_action_registry import ActionPolicyError


class ExternalActionWorkSource:
    source_type = "external_action"

    def __init__(self, *, policy_service):
        self.policies = policy_service
        self._after_action_id = None

    async def discover(self, session, *, now=None):
        # Realtime controls use synchronous execution and never enter a retry
        # queue. Legacy specialized actions retain their existing sources.
        durable_types = tuple(d.action_type for d in self.policies.registry.definitions()
                              if d.execution_lane == "durable")
        if not durable_types:
            return []
        query = select(ExternalAction).where(
            ExternalAction.action_type.in_(durable_types), ExternalAction.origin_work_item_id.is_not(None),
            ExternalAction.status.in_(["proposed", "approved", "failed", "attempting"])
        ).order_by(ExternalAction.id).limit(200)
        page = query.where(ExternalAction.id > self._after_action_id) if self._after_action_id else query
        rows = (await session.scalars(page)).all()
        if not rows and self._after_action_id:
            rows = (await session.scalars(query)).all()
        # A cyclic keyset is only a discovery optimization: action/WorkItem
        # persistence still owns identity and recovery, including restarts.
        self._after_action_id = rows[-1].id if len(rows) == 200 else None
        result = []
        for action in rows:
            try:
                _, definition, _, _, run, origin = await self.policies.authorize_action(session, action,
                    allow_unapproved=action.authorization_mode == "human_approval" and "quote" not in action.payload_json)
            except (ActionPolicyError, ValueError):
                continue
            result.append(WorkCandidate(source_type=self.source_type, source_id=str(action.id),
                source_revision=action_source_revision(action), intent_key="external-action.execute", domain="internal",
                assigned_agent_id=str(action.origin_agent_id), agent_revision_id=str(run.agent_revision_id),
                project_id=str(action.project_id) if action.project_id else None,
                space_id=str(origin.space_id) if origin.space_id else None,
                required_capabilities=definition.required_capabilities, execution_adapter="external_action",
                concurrency_key=f"external-action-connection:{action.connection_id}", max_attempts=3,
                parent_work_item_id=str(origin.id), root_work_item_id=str(origin.root_work_item_id or origin.id),
                causal_depth=int(origin.causal_depth or 0) + 1,
                metadata={"action_policy_id": str(action.action_policy_id)}))
        return result

    async def refresh(self, session, claim):
        action = await session.get(ExternalAction, uid(claim.source_id), populate_existing=True)
        if not action:
            return False
        if action_source_revision(action) != claim.source_revision:
            work = await session.get(AgentWorkItem, uid(claim.work_item_id), populate_existing=True)
            metadata = (work.metadata_json or {}) if work else {}
            return bool(work and work.state == "running" and work.lease_token == claim.lease_token
                and metadata.get("approval_transition_from") == claim.source_revision
                and metadata.get("approval_action_id") == str(action.id)
                and action.authorization_mode == "human_approval" and action.status == "proposed")
        if action.status in {"succeeded", "uncertain"}:
            # Settlement must preserve provider evidence even after authority
            # is revoked while a committed request is in flight.
            return True
        try:
            await self.policies.authorize_action(session, action,
                allow_unapproved=action.authorization_mode == "human_approval" and "quote" not in action.payload_json)
            return True
        except (ActionPolicyError, ValueError):
            return False
