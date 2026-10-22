"""Authenticated AoiDots and AgentDialogue API boundary."""

from __future__ import annotations

import inspect
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..services.agent_dialogue_service import (
    AgentDialogueError,
    AgentDialogueService,
)


class _DialogueModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class AoiDotsProfile(_DialogueModel):
    root_id: str
    session_id: str
    agent_id: str
    agent_name: str = Field(default="AoiDots", min_length=1, max_length=160)
    state: str = Field(default="ready", min_length=1, max_length=32)
    pending_input_count: int = Field(default=0, ge=0, le=64)


class AoiDotsWorkItem(_DialogueModel):
    dialogue_id: str
    job_status: str
    human_status: str | None = None
    title: str | None = None
    label: str | None = None
    root_agent_name: str | None = Field(default=None, max_length=160)
    worker_agent_name: str | None = Field(default=None, max_length=160)
    status_source: Literal["dialogue", "worker"] | None = None
    current_run_status: str | None = Field(default=None, max_length=32)
    current_work_status: str | None = Field(default=None, max_length=32)
    current_delivery_state: str | None = Field(default=None, max_length=32)
    phase: Literal[
        "queued",
        "running",
        "receiving_result",
        "waiting_for_input",
        "blocked",
        "completed",
        "succeeded",
        "failed",
        "cancelled",
        "expired",
    ] | None = None
    budget: AgentDialogueBudgetDTO | None = None
    allowed_actions: list[Literal["stop", "resume", "rework", "answer"]] = Field(default_factory=list, max_length=4)
    refs: "AgentDialogueRefs"
    control_revision: int = 0
    pending_inputs: list["AgentDialogueInputDTO"] = Field(default_factory=list)
    artifacts: list["ArtifactSafeRef"] = Field(default_factory=list)
    safe_error: str | None = None
    deadline: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class AgentDialogueInputDTO(_DialogueModel):
    id: str
    dialogue_id: str | None = None
    question_message_id: str | None = None
    source_run_id: str | None = None
    state: Literal["pending", "answered", "invalidated", "blocked", "cancelled", "expired"] = "pending"
    revision: int = Field(default=0, ge=0)
    expected_input_revision: int = Field(default=1, ge=1)
    expected_control_revision: int = Field(default=0, ge=0)
    answer_message_id: str | None = None
    deadline: str | None = None
    question: str | None = None
    choices: list[str] = Field(default_factory=list, max_length=32)
    input_type: str | None = None
    allow_multiple: bool = False
    allow_free_text: bool = True


class ArtifactSafeRef(_DialogueModel):
    id: str | None = None
    artifact_ref: str | None = None
    phase: str | None = None
    delivery_id: str | None = None
    message_id: str | None = None
    created_at: str | None = None


class AgentDialogueRefs(_DialogueModel):
    root_session_id: str | None = None
    worker_session_id: str | None = None
    source_message_id: str | None = None
    source_human_message_id: str | None = None
    source_run_id: str | None = None
    source_human_run_id: str | None = None
    work_item_id: str | None = None
    source_human_work_item_id: str | None = None
    task_id: str | None = None
    project_id: str | None = None
    root_agent_id: str | None = None
    root_agent_revision_id: str | None = None
    worker_agent_id: str | None = None
    worker_agent_revision_id: str | None = None
    current_run_id: str | None = None
    current_work_item_id: str | None = None
    current_delivery_id: str | None = None


class AgentDialogueBudgetDTO(_DialogueModel):
    max_turns: int = Field(ge=1)
    turn_count: int = Field(ge=0)
    max_depth: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    attempt_count: int = Field(ge=0)
    max_tokens: int | None = Field(default=None, ge=1)
    total_tokens: int = Field(ge=0)
    reserved_tokens: int = Field(ge=0)
    remaining_tokens: int | None = Field(default=None, ge=0)
    max_cost_micros: int | None = Field(default=None, ge=1)
    cost_micros: int | None = Field(default=None, ge=0)
    reserved_cost_micros: int = Field(ge=0)
    cost_currency: str | None = Field(default=None, min_length=3, max_length=16)
    usage_state: Literal["known", "unknown", "cost_unknown", "token_unknown"]


AoiDotsWorkItem.model_rebuild()


class AoiDotsWorkList(_DialogueModel):
    items: list[AoiDotsWorkItem] = Field(default_factory=list, max_length=100)


class AgentDialogueResponse(AoiDotsWorkItem):
    pass


class InputAnswerRequest(_DialogueModel):
    answer: str = Field(min_length=1, max_length=30_000)
    client_message_id: str = Field(min_length=1, max_length=512)
    expected_control_revision: int = Field(ge=0)
    expected_input_revision: int = Field(ge=1)


class DialogueControlRequest(_DialogueModel):
    action: Literal["stop", "resume", "rework"]
    expected_control_revision: int = Field(ge=0)
    instruction: str | None = Field(default=None, max_length=30_000)


class AoiDotsBootstrapRequest(_DialogueModel):
    # This field is context only.  The service never attaches the private root
    # to it implicitly and never persists it as the root's Project binding.
    context_project_hint_id: str | None = Field(default=None, max_length=200)


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _user_value(user: Any, *names: str) -> Any:
    if isinstance(user, dict):
        for name in names:
            if user.get(name) is not None:
                return user[name]
        return None
    for name in names:
        value = getattr(user, name, None)
        if value is not None:
            return value
    return None


def _error(exc: AgentDialogueError) -> HTTPException:
    return HTTPException(
        status_code=int(getattr(exc, "status_code", 409)),
        detail={"code": str(getattr(exc, "code", "agent_dialogue_error")), "message": str(exc)},
    )


def create_agent_dialogue_router(
    *,
    get_db_manager: Callable[[], Any],
    require_auth_dependency: Callable[..., Any],
    get_user_from_request: Callable[..., Any],
    get_service: Callable[[], Any] | None = None,
    get_dispatch_bridge: Callable[[], Any] | None = None,
    config: Any | None = None,
) -> APIRouter:
    """Build the small authenticated AoiDots/AgentDialogue router."""

    router = APIRouter(tags=["agent-dialogue"])

    async def _current_user(request: Request) -> Any:
        if get_user_from_request is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        user = await _maybe(get_user_from_request(request))
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        user_id = _user_value(user, "id", "user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return user

    async def _service() -> AgentDialogueService:
        if get_service is not None:
            value = await _maybe(get_service())
            if value is not None:
                return value
        manager = get_db_manager() if callable(get_db_manager) else get_db_manager
        manager = await _maybe(manager)
        bridge = await _maybe(get_dispatch_bridge()) if get_dispatch_bridge is not None else None
        if bridge is None:
            from ..services.agent_dialogue_service import get_agent_dialogue_dispatch_bridge

            bridge = get_agent_dialogue_dispatch_bridge(manager, config=config)
        return AgentDialogueService(manager, config=config, dispatch_bridge=bridge)

    @router.post(
        "/api/aoidots/bootstrap",
        response_model=AoiDotsProfile,
        response_model_exclude_none=False,
    )
    async def bootstrap(
        payload: AoiDotsBootstrapRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> AoiDotsProfile:
        user = await _current_user(request)
        service = await _service()
        try:
            result = await service.bootstrap(
                actor_user_id=_user_value(user, "id", "user_id"),
                actor_role=_user_value(user, "role", "user_role"),
                context_project_hint_id=payload.context_project_hint_id,
            )
            return AoiDotsProfile.model_validate(result)
        except AgentDialogueError as exc:
            raise _error(exc) from exc

    @router.get(
        "/api/aoidots/work",
        response_model=AoiDotsWorkList,
        response_model_exclude_none=False,
    )
    async def list_work(
        request: Request,
        root_session_id: str | None = Query(default=None, max_length=200),
        state: str | None = Query(default=None, max_length=48),
        limit: int = Query(default=50, ge=1, le=100),
        _: Any = Depends(require_auth_dependency),
    ) -> AoiDotsWorkList:
        user = await _current_user(request)
        service = await _service()
        try:
            items = await service.list_work(
                actor_user_id=_user_value(user, "id", "user_id"),
                actor_role=_user_value(user, "role", "user_role"),
                root_session_id=root_session_id,
                state=state,
                limit=limit,
            )
            return AoiDotsWorkList.model_validate({"items": items})
        except AgentDialogueError as exc:
            raise _error(exc) from exc

    @router.get(
        "/api/agent-dialogues/{dialogue_id}",
        response_model=AgentDialogueResponse,
        response_model_exclude_none=False,
    )
    async def get_dialogue(
        dialogue_id: str,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> AgentDialogueResponse:
        user = await _current_user(request)
        service = await _service()
        try:
            result = await service.get_dialogue(
                dialogue_id,
                actor_user_id=_user_value(user, "id", "user_id"),
                actor_role=_user_value(user, "role", "user_role"),
            )
            return AgentDialogueResponse.model_validate(result)
        except AgentDialogueError as exc:
            raise _error(exc) from exc

    @router.post(
        "/api/agent-dialogues/{dialogue_id}/inputs/{input_id}/answer",
        response_model=dict[str, Any],
    )
    async def answer_input(
        dialogue_id: str,
        input_id: str,
        payload: InputAnswerRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        user = await _current_user(request)
        service = await _service()
        try:
            return await service.resolve_input(
                dialogue_id,
                input_id,
                answer=payload.answer,
                client_message_id=payload.client_message_id,
                expected_control_revision=payload.expected_control_revision,
                expected_input_revision=payload.expected_input_revision,
                actor_user_id=_user_value(user, "id", "user_id"),
                actor_role=_user_value(user, "role", "user_role"),
                source_kind="human",
            )
        except AgentDialogueError as exc:
            raise _error(exc) from exc

    async def _control(
        dialogue_id: str,
        payload: DialogueControlRequest,
        request: Request,
    ) -> dict[str, Any]:
        user = await _current_user(request)
        service = await _service()
        try:
            return await service.record_human_intervention(
                dialogue_id,
                action=payload.action,
                instruction=payload.instruction if payload.action == "rework" else None,
                actor_user_id=_user_value(user, "id", "user_id"),
                actor_role=_user_value(user, "role", "user_role"),
                expected_control_revision=payload.expected_control_revision,
            )
        except AgentDialogueError as exc:
            raise _error(exc) from exc

    @router.post("/api/agent-dialogues/{dialogue_id}/control", response_model=dict[str, Any])
    async def control(
        dialogue_id: str,
        payload: DialogueControlRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        return await _control(dialogue_id, payload, request)

    @router.post("/api/agent-dialogues/{dialogue_id}/stop", response_model=dict[str, Any])
    async def stop(
        dialogue_id: str,
        request: Request,
        expected_control_revision: int = Query(ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        return await _control(
            dialogue_id,
            DialogueControlRequest(action="stop", expected_control_revision=expected_control_revision),
            request,
        )

    @router.post("/api/agent-dialogues/{dialogue_id}/resume", response_model=dict[str, Any])
    async def resume(
        dialogue_id: str,
        request: Request,
        expected_control_revision: int = Query(ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        return await _control(
            dialogue_id,
            DialogueControlRequest(action="resume", expected_control_revision=expected_control_revision),
            request,
        )

    @router.post("/api/agent-dialogues/{dialogue_id}/rework", response_model=dict[str, Any])
    async def rework(
        dialogue_id: str,
        request: Request,
        expected_control_revision: int = Query(ge=0),
        _: Any = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        return await _control(
            dialogue_id,
            DialogueControlRequest(action="rework", expected_control_revision=expected_control_revision),
            request,
        )

    return router


__all__ = [
    "AgentDialogueBudgetDTO",
    "AgentDialogueInputDTO",
    "AgentDialogueRefs",
    "AgentDialogueResponse",
    "AoiDotsBootstrapRequest",
    "AoiDotsProfile",
    "AoiDotsWorkItem",
    "AoiDotsWorkList",
    "ArtifactSafeRef",
    "DialogueControlRequest",
    "InputAnswerRequest",
    "create_agent_dialogue_router",
]
