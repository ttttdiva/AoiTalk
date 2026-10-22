"""Authenticated invocation API for canonical API-backed Agent tasks."""

from __future__ import annotations

import asyncio
import inspect
import re
from typing import Any, Callable, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .operations_routes import _authenticated_human_actor
from ..services.agent_task_invocation import (
    AgentTaskInvocationError,
    AgentTaskInvocationService,
)

_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class AgentTaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(..., min_length=1, max_length=8000)
    workspace: str = Field(..., min_length=1, max_length=1024)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    completion_criteria: list[str] = Field(default_factory=list, max_length=20)
    requires_command_verification: StrictBool | None = Field(
        None, description="Require successful command evidence after the last mutation point: a file-tool edit, or any command whose bounded before/after workspace content observation did not prove the workspace unchanged (changed, unknown, partial/over-limit, background launch, or no evidence; success does not matter). The last unproven command cannot verify itself. Observation is whole-workspace and bounded (excludes .git; unobservable links/reparse points or limits fail closed), not a per-path change list. An explicit boolean overrides language inference; omitted/null infers from the goal and completion criteria."
    )
    required_outputs: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Workspace-relative files the task must create or change. The task completes only when each is a safe, non-empty regular file whose streamed content digest differs from the one captured before the first model turn AND the task has write provenance: a successful file-tool write receipt for that path (file_tool), or a permitted command that completed cleanly (command_correlated: correlation only, command receipts carry only a whole-workspace freshness observation, not per-path mutation proof). An unchanged pre-existing file, a touch or an identical rewrite never satisfies. Requires mutate mode. Windows-safe relative names only; links and reparse points are rejected.",
    )
    edit_targets: list[str] | None = Field(
        None,
        max_length=20,
        description="Optional explicit editable files (distinct workspace-relative files, Windows-safe names, links rejected). Each entry names exactly that one file; a same-named file in another folder is never matched. Omitted/null keeps the legacy scope derived from the goal/constraints prose; an explicit list replaces only the prose-inferred editable list, so protected/read-only files and carve-outs in the prose still apply (a conflict is rejected with 422), and an empty list grants no file edits (accepted only with allow_commands; otherwise 422). Every required_outputs entry must be inside this scope (required_outputs never grants write access). The scope is frozen at admission and persisted; framework guidance, corrective turns and restarts never change it. It limits file-tool targets only: it is not an OS sandbox and does not inspect what an allowed command writes. Requires mutate mode and remains bounded by the Agent's existing grants.",
    )
    mode: str | None = Field(None, pattern="^(read_only|mutate)$")
    allow_commands: StrictBool | None = None
    allow_git_commit: StrictBool | None = Field(
        None, description="Omitted/null denies. Allow explicit-path git add and a normal task-scoped commit; requires mutate and commands"
    )
    allow_git_push: StrictBool | None = Field(
        None, description="Omitted/null denies. Allow a normal non-force branch push; requires mutate and commands"
    )
    tool_generation_max_tokens: StrictInt | None = Field(
        None, ge=1, le=4096,
        description="Omitted/null keeps the model's legacy output reserve. Explicit per-task opt-in: uncapped tool generation may use a bounded allowance up to this many tokens (never above 4096 or the remaining context window); explicit caller/runtime caps still win.",
    )
    agent_id: UUID
    provider: str | None = Field(None, min_length=1, max_length=200)
    model: str | None = Field(None, min_length=1, max_length=200)
    effort: str | None = Field(None, min_length=1, max_length=40)
    execution_profile_id: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        description="Optional runtime ExecutionProfile for this task. Omitted/null keeps the pinned AgentRevision default. An explicit id must be a formal system Profile or an enabled Profile of the Agent's own Team; it never changes the Agent role and never falls back. provider/model/effort remain assertions against the selected route.",
    )
    required_capabilities: list[str] = Field(default_factory=list, max_length=20)


AgentTaskStatus = Literal[
    "queued",
    "running",
    "waiting",
    "waiting_approval",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
]


class AgentTaskFinalResult(BaseModel):
    response: str = Field(..., description="Secret-redacted final assistant answer; no internal execution trace")


class AgentTaskFailedTool(BaseModel):
    tool: str
    denied: bool = Field(..., description="True when the tool call was refused by Agent authority")
    reason: str | None = Field(None, description="Framework-authored fixed reason code; never tool output")
    receipt_id: str | None = Field(None, description="AgentRunToolCall receipt id of the failed call")


class AgentTaskToolFailures(BaseModel):
    recovered_count: int = Field(..., description="Intermediate tool failures that did not block completion")
    recovered: list[AgentTaskFailedTool] = Field(
        ..., description="Executed read-only failures or failures later retried successfully"
    )
    unresolved_count: int = Field(..., description="Write, command or refused tool failures left unresolved")
    unresolved: list[AgentTaskFailedTool]
    diagnostic_count: int = Field(
        0, description="Launched foreground commands that exited nonzero; real output is kept, never reported as passed"
    )
    diagnostic: list[AgentTaskFailedTool] = Field(
        default_factory=list, description="Nonzero command exits left as diagnostic observations"
    )


class AgentTaskFailure(BaseModel):
    reason: str = Field(..., description="Stable safe failure code")
    unresolved_tools: list[AgentTaskFailedTool]
    successful_tool_count: int
    successful_tools: list[str] = Field(..., description="Tools that completed before the failure; progress is kept")
    final_response: str | None = Field(
        None,
        description="Secret-redacted last Agent answer; kept for the user but never a completed result",
    )
    missing_required_outputs: list[str] | None = Field(
        None, description="Workspace-relative required_outputs not proven created or changed by the task"
    )


class AgentTaskInterruptedOperation(BaseModel):
    tool: str
    operation_id: str | None = Field(None, description="Durable tool operation id; confirm it to continue")
    path: str | None = Field(None, description="Workspace-relative target, when the tool named one")


class AgentTaskProjection(BaseModel):
    task_id: str = Field(..., pattern=r"^[0-9a-f]{32}$")
    status: AgentTaskStatus
    work_item_id: UUID
    agent_run_id: UUID | None = None
    agent_id: UUID
    agent_revision_id: UUID
    cancellation_requested: bool
    reconciliation_required: list[AgentTaskInterruptedOperation] | None = Field(
        None,
        description="Operations whose outcome a restart left unknown; inspect the workspace, then resume with their operation ids",
    )
    result: AgentTaskFinalResult | None = None
    failure: AgentTaskFailure | None = None
    tool_failures: AgentTaskToolFailures | None = Field(
        None, description="Recovered and unresolved tool failures for a completed or failed task"
    )


class AgentTaskCreateResponse(BaseModel):
    task: AgentTaskProjection
    created: bool


class AgentTaskResponse(BaseModel):
    task: AgentTaskProjection


class AgentTaskEventProjection(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    work_item_id: UUID
    sequence: int
    event_type: str
    from_state: str | None = None
    to_state: str | None = None
    status: str | None = None
    state: str | None = None
    actor_kind: str
    actor_id: str | None = None
    actor_user_id: UUID | None = None
    actor_agent_id: UUID | None = None
    actor_service_key: str | None = None
    agent_run_id: UUID | None = None
    causation_id: str | None = None
    causal_depth: int
    mutation_fingerprint: str | None = None
    safe_error_code: str | None = None
    error_code: str | None = None
    message: str | None = None
    error_message: str | None = None
    result_summary: str | None = None
    payload: Any = None
    payload_json: Any = None
    created_at: str | None = None
    occurred_at: str | None = None


class AgentTaskTailResponse(BaseModel):
    task_id: str = Field(..., pattern=r"^[0-9a-f]{32}$")
    status: AgentTaskStatus
    events: list[AgentTaskEventProjection]
    truncated: bool
    event_count: int


class AgentTaskResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed_operation_ids: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Operation ids the owner inspected and accepts continuing with an unknown outcome",
    )


class AgentTaskCancelResponse(BaseModel):
    task: AgentTaskProjection
    cancellation_requested: bool


class AgentTaskErrorResponse(BaseModel):
    detail: str | list[dict[str, Any]]
    error: str | None = None


def _error_responses(*statuses: int) -> dict[int, dict[str, Any]]:
    return {
        status: {
            "model": AgentTaskErrorResponse,
            "description": "Agent task request failed",
        }
        for status in statuses
    }


def _maybe(value: Any) -> Any:
    return value


def bind_agent_task_service(manager: Any, config: Any, coordinator: Any) -> AgentTaskInvocationService:
    """Return the single canonical invocation service for this coordinator.

    Startup calls this before ``recover_stale`` so the evidence-based restart
    hook is registered; the routes reuse the same instance afterwards.
    """

    service = getattr(coordinator, "_agent_task_service", None)
    if service is None:
        service = AgentTaskInvocationService(manager, config, coordinator)
        coordinator._agent_task_service = service
    service.attach()
    return service


def create_agent_task_router(
    *,
    get_db_manager: Callable[[], Any],
    get_coordinator: Callable[[], Any],
    get_user_from_request: Callable[[Request], Any],
    require_auth_dependency: Callable[..., Any],
    config: Any,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/agent-tasks",
        tags=["agent-tasks"],
        dependencies=[Depends(require_auth_dependency)],
    )
    async def _service() -> AgentTaskInvocationService:
        manager = get_db_manager()
        if inspect.isawaitable(manager):
            manager = await manager
        coordinator = get_coordinator()
        if inspect.isawaitable(coordinator):
            coordinator = await coordinator
        if manager is None or coordinator is None:
            raise HTTPException(status_code=503, detail="Agent task runtime is unavailable")
        existing = getattr(coordinator, "_agent_task_service", None)
        if existing is not None:
            return existing
        service = AgentTaskInvocationService(manager, config, coordinator)
        coordinator._agent_task_service = service
        return service

    async def _owner(request: Request) -> tuple[str, str]:
        principal = get_user_from_request(request)
        if inspect.isawaitable(principal):
            principal = await principal
        actor = _authenticated_human_actor(principal)
        authority = (
            principal.get("_authority_source")
            if isinstance(principal, dict)
            else getattr(principal, "_authority_source", None)
        )
        identity = actor.get("id") or actor.get("user_id")
        try:
            owner_id = str(UUID(str(identity)))
        except (TypeError, ValueError, AttributeError):
            raise HTTPException(status_code=403, detail="A stable authenticated human identity is required") from None
        if (
            authority != "web_session"
            or actor.get("actor_type") != "human"
            or actor.get("is_agent")
        ):
            raise HTTPException(status_code=403, detail="An ordinary human session is required")
        role = str(actor.get("role") or "user")
        return owner_id, role

    def _task_id(value: str) -> str:
        if not _TASK_ID_RE.fullmatch(value):
            raise HTTPException(status_code=404, detail="task not found")
        return value

    def _error(exc: AgentTaskInvocationError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"detail": exc.message, "error": exc.code},
        )

    @router.post(
        "",
        status_code=202,
        operation_id="agent_task_create",
        response_model=AgentTaskCreateResponse,
        responses={
            200: {"model": AgentTaskCreateResponse, "description": "Idempotent replay"},
            **_error_responses(403, 409, 422, 503),
        },
    )
    async def create_task(
        body: AgentTaskCreateRequest,
        request: Request,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ) -> AgentTaskCreateResponse | JSONResponse:
        owner_id, role = await _owner(request)
        try:
            task, created = await (await _service()).submit(
                body.model_dump(exclude_none=True),
                owner=owner_id,
                actor_role=role,
                idempotency_key=idempotency_key,
            )
            response.status_code = 202 if created else 200
            return {"task": task, "created": created}
        except AgentTaskInvocationError as exc:
            return _error(exc)

    @router.get(
        "/{task_id}",
        operation_id="agent_task_get",
        response_model=AgentTaskResponse,
        responses=_error_responses(403, 404, 503),
    )
    async def get_task(task_id: str, request: Request) -> AgentTaskResponse | JSONResponse:
        owner_id, _ = await _owner(request)
        try:
            task = await (await _service()).get(_task_id(task_id), owner=owner_id)
            return {"task": task}
        except AgentTaskInvocationError as exc:
            return _error(exc)

    @router.get(
        "/{task_id}/tail",
        operation_id="agent_task_tail",
        response_model=AgentTaskTailResponse,
        responses=_error_responses(403, 404, 422, 503),
    )
    async def get_task_tail(
        task_id: str,
        request: Request,
        limit: int = Query(50, ge=1, le=200),
        max_bytes: int = Query(16000, ge=1024, le=64000),
    ) -> AgentTaskTailResponse | JSONResponse:
        owner_id, _ = await _owner(request)
        try:
            return await (await _service()).tail(
                _task_id(task_id),
                owner=owner_id,
                limit=limit,
                max_bytes=max_bytes,
            )
        except AgentTaskInvocationError as exc:
            return _error(exc)

    @router.post(
        "/{task_id}/cancel",
        operation_id="agent_task_cancel",
        response_model=AgentTaskCancelResponse,
        responses=_error_responses(403, 404, 503),
    )
    async def cancel_task(task_id: str, request: Request) -> AgentTaskCancelResponse | JSONResponse:
        owner_id, _ = await _owner(request)
        try:
            task = await (await _service()).cancel(_task_id(task_id), owner=owner_id)
            return {
                "task": task,
                "cancellation_requested": bool(task.get("cancellation_requested")),
            }
        except AgentTaskInvocationError as exc:
            return _error(exc)

    @router.post(
        "/{task_id}/resume",
        operation_id="agent_task_resume",
        response_model=AgentTaskResponse,
        responses=_error_responses(403, 404, 409, 422, 503),
    )
    async def resume_task(
        task_id: str, body: AgentTaskResumeRequest, request: Request
    ) -> AgentTaskResponse | JSONResponse:
        owner_id, _ = await _owner(request)
        try:
            task = await (await _service()).resume(
                _task_id(task_id),
                owner=owner_id,
                confirmed_operation_ids=body.confirmed_operation_ids,
            )
            return {"task": task}
        except AgentTaskInvocationError as exc:
            return _error(exc)

    return router


__all__ = [
    "AgentTaskCancelResponse",
    "AgentTaskCreateRequest",
    "AgentTaskCreateResponse",
    "AgentTaskEventProjection",
    "AgentTaskErrorResponse",
    "AgentTaskFailure",
    "AgentTaskFailedTool",
    "AgentTaskFinalResult",
    "AgentTaskInterruptedOperation",
    "AgentTaskProjection",
    "AgentTaskResponse",
    "AgentTaskResumeRequest",
    "AgentTaskTailResponse",
    "bind_agent_task_service",
    "create_agent_task_router",
]
