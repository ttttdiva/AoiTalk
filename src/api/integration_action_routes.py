"""Safe code-owned action catalog; connections retain Operations ownership."""
from fastapi import APIRouter, Depends, Request

from .agent_action_policy_routes import policy_actor, policy_session
from ..services.agent_action_policy_service import AgentActionPolicyService
from ..services.integration_action_registry import IntegrationActionRegistry


def create_integration_action_router(get_db_manager, get_user_from_request, require_auth_dependency, *, registry=None):
    registry = registry or IntegrationActionRegistry()
    router = APIRouter(prefix="/api/integrations", tags=["integration-actions"], dependencies=[Depends(require_auth_dependency)])

    @router.get("/actions")
    async def actions(request: Request):
        actor = await policy_actor(request, get_user_from_request)
        async with policy_session(get_db_manager) as session:
            await AgentActionPolicyService().admin(session, actor)
            return {"success": True, "actions": registry.safe_catalog()}

    return router
