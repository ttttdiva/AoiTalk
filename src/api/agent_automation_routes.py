"""Authenticated human automation management; normal tests never enqueue."""

from __future__ import annotations

import inspect

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import ValidationError

from ..services.agent_automation_service import (
    AgentAutomationService,
    AutomationError,
    RuleCreate,
    RulePatch,
    RuleRevisionCreate,
    RuleState,
    RuleTest,
)


class _AutomationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # FastAPI's default validation payload echoes rejected input.
                # This boundary must never echo a credential-shaped mistake.
                raise HTTPException(422, "automation_request_invalid") from exc

        return safe_handler


def create_agent_automation_router(
    require_auth_dependency,
    get_current_user_from_request,
    *,
    service=None,
    db_manager=None,
    config=None,
):
    automation = service or AgentAutomationService(db_manager, config=config)
    router = APIRouter(
        prefix="/api/agent-automation",
        tags=["agent-automation"],
        route_class=_AutomationRoute,
    )

    async def actor(request: Request, _=Depends(require_auth_dependency)):
        try:
            automation.require_features()
        except AutomationError as exc:
            raise HTTPException(exc.status_code, exc.code) from exc
        user = get_current_user_from_request(request)
        user = await user if inspect.isawaitable(user) else user
        if not user:
            raise HTTPException(401, "Not authenticated")

        def field(key, default=None):
            return (
                user.get(key, default)
                if isinstance(user, dict)
                else getattr(user, key, default)
            )

        kind = field("actor_type") or field("principal_kind") or "human"
        if kind != "human" or field("is_agent", False) or field("role") != "admin":
            raise HTTPException(403, "automation_human_admin_required")
        return field("id") or field("user_id")

    async def invoke(awaitable):
        try:
            return await awaitable
        except AutomationError as exc:
            raise HTTPException(exc.status_code, exc.code) from exc
        except ValidationError as exc:
            raise HTTPException(422, "automation_request_invalid") from exc
        except Exception as exc:
            # Registry/policy errors are codes; arbitrary provider text is not.
            code = getattr(exc, "code", None)
            if (
                isinstance(code, str)
                and code.startswith("semantic_")
                and len(code) < 100
            ):
                raise HTTPException(
                    503 if getattr(exc, "retryable", False) else 422, code
                ) from exc
            if (
                isinstance(code, str)
                and code.startswith(("action_", "policy_", "automation_"))
                and len(code) < 100
            ):
                raise HTTPException(getattr(exc, "status_code", 422), code) from exc
            raise HTTPException(503, "automation_service_unavailable") from exc

    @router.get("/rules")
    async def list_rules(
        agent_id: str | None = None,
        limit: int = Query(100, ge=1, le=500),
        actor_id=Depends(actor),
    ):
        rules = await invoke(
            automation.list_rules(
                actor_user_id=actor_id, agent_id=agent_id, limit=limit
            )
        )
        return {"success": True, "rules": rules, "total": len(rules)}

    @router.post("/rules")
    async def create_rule(payload: RuleCreate, actor_id=Depends(actor)):
        return {
            "success": True,
            "rule": await invoke(
                automation.create_rule(actor_user_id=actor_id, **payload.model_dump())
            ),
        }

    @router.get("/rules/{rule_id}")
    async def get_rule(rule_id: str, actor_id=Depends(actor)):
        return {
            "success": True,
            "rule": await invoke(automation.get_rule(rule_id, actor_user_id=actor_id)),
        }

    @router.patch("/rules/{rule_id}")
    async def update_rule(rule_id: str, payload: RulePatch, actor_id=Depends(actor)):
        return {
            "success": True,
            "rule": await invoke(
                automation.update_rule(
                    rule_id, actor_user_id=actor_id, **payload.model_dump()
                )
            ),
        }

    @router.post("/rules/{rule_id}/state")
    async def state(rule_id: str, payload: RuleState, actor_id=Depends(actor)):
        return {
            "success": True,
            "rule": await invoke(
                automation.transition_state(
                    rule_id, actor_user_id=actor_id, **payload.model_dump()
                )
            ),
        }

    @router.post("/rules/{rule_id}/revisions")
    async def create_revision(
        rule_id: str, payload: RuleRevisionCreate, actor_id=Depends(actor)
    ):
        return {
            "success": True,
            "rule": await invoke(
                automation.create_revision(
                    rule_id, actor_user_id=actor_id, **payload.model_dump()
                )
            ),
        }

    @router.post("/rules/{rule_id}/test")
    async def test(rule_id: str, payload: RuleTest, actor_id=Depends(actor)):
        return {
            "success": True,
            "evaluation": await invoke(
                automation.test_rule(
                    rule_id, actor_user_id=actor_id, **payload.model_dump()
                )
            ),
        }

    return router
