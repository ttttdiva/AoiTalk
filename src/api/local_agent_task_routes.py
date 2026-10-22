"""Admin-only API for durable local-LLM agent tasks (Home MCP Gateway bridge).

Every route requires the ordinary cookie session plus administrator role:
tasks run host commands and edit files inside an operator-chosen workspace.
Responses are bounded summaries; credentials and raw environment values are
never included.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Callable, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..llm.codex_task_completion import CodexTaskProtocolError
from ..services.codex_app_profiles import CodexProfileError
from .operations_routes import _authenticated_human_actor

from ..services.local_agent_tasks import (
    LocalAgentTaskError,
    LocalAgentTaskManager,
    TASK_ID_RE,
)

logger = logging.getLogger(__name__)


class LocalAgentTaskCreateRequest(BaseModel):
    """High-level task: a goal plus bounds, not a list of low-level commands."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(..., min_length=1, max_length=8000)
    workspace: str = Field(..., min_length=1, max_length=1024,
                           description="Absolute directory inside local_agent_tasks.allowed_roots")
    constraints: list[str] = Field(default_factory=list, max_length=20)
    completion_criteria: list[str] = Field(default_factory=list, max_length=20)
    mode: str = Field("read_only", pattern="^(read_only|mutate)$")
    allow_commands: Optional[bool] = None
    allow_adb: bool = False
    allow_git_commit: bool = False
    max_tool_rounds: Optional[int] = Field(None, ge=1, le=1000)
    timeout_sec: Optional[int] = Field(None, ge=1, le=86400)
    command_timeout_sec: Optional[int] = Field(None, ge=1, le=86400)
    expected_branch: Optional[str] = Field(None, max_length=200)
    idempotency_key: Optional[str] = Field(None, max_length=128)
    agent_id: Optional[str] = Field(None, min_length=36, max_length=36, description="Canonical Agent UUID; omitted uses the explicitly configured delegation Agent")
    provider: Optional[str] = Field(None, pattern="^(openai_compatible_local|ollama|codex-cli)$")
    model: Optional[str] = Field(None, min_length=1, max_length=200)
    effort: Optional[str] = Field(None, min_length=1, max_length=40)
    codex_connection: Literal["app_profile"] | None = None


class CodexProfileIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CodexProfileStatus(BaseModel):
    enabled: bool
    state: str
    profile_id: str | None
    generation: int
    connected: bool
    model_ready: bool
    login_started: bool
    connector_available: bool


class CodexProfileDisclosure(BaseModel):
    version: int
    cli_version: str
    storage_destination: str
    storage_provisioned: Literal[False]
    credential_saved: Literal[False]
    credential_store: Literal["keyring_only"]
    windows_storage: Literal["os_keyring_key_and_encrypted_codex_home_secrets"]
    credential_lifetime: Literal["no_fixed_expiry_refresh_possible"]
    connection_lifetime: Literal["memory_only_until_disconnect_or_restart"]
    oauth_scopes: list[str]
    intended_use: Literal["operations_read_only"]
    usage: Literal["owner_attributed_observed_usage"]
    disconnect_effects: list[str]
    disconnect_removes_credentials: Literal[False]
    disconnect_revokes_provider_grant: Literal[False]
    official_logout_required_separately: Literal[True]
    official_connector_available: bool
    connector_spec: dict[str, Any] | None
    approval_receipt_seconds: int
    login_wait_seconds: int


class CodexProfileHandoff(CodexProfileStatus):
    nonce: str
    expires_in_seconds: int
    intended_use: Literal["operations_read_only"]
    credential_store: Literal["keyring_only"]
    oauth_scopes: list[str]
    login_method: Literal["official_browser_or_device"]
    required_approvals: list[str]
    token_import_supported: bool
    spec_digest: str = Field(pattern="^[a-f0-9]{64}$")
    disclosure: CodexProfileDisclosure


class CodexProfileApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nonce: str = Field(pattern="^[a-f0-9]{32}$")
    spec_digest: str = Field(pattern="^[a-f0-9]{64}$")
    accept_storage: StrictBool
    accept_scopes: StrictBool
    accept_use_and_disconnect: StrictBool
    accept_official_login: StrictBool


class CodexProfileApprovalReceipt(BaseModel):
    approval_receipt: str
    expires_in_seconds: int


class CodexProfileBeginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_receipt: str = Field(pattern="^[a-f0-9]{32}$")


class CodexProfileLoginHandoff(BaseModel):
    login_id: str
    auth_url: str
    expires_in_seconds: int


class InstructionReviewSource(BaseModel):
    path: str
    exists: bool
    sha256: str | None
    size_bytes: int
    selected: bool
    text: str | None


class InstructionReviewPreview(BaseModel):
    nonce: str
    task_id: str
    owner: str
    task_fingerprint: str
    review_digest: str
    expires_at: float
    execution_expires_at: float
    cli_version: str
    sources: list[InstructionReviewSource]


class InstructionReviewApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_fingerprint: str = Field(pattern="^[a-f0-9]{64}$")
    review_digest: str = Field(pattern="^[a-f0-9]{64}$")
    accept_openai_transmission: StrictBool
    accept_trusted_host_assumption: StrictBool


class InstructionReviewStatus(BaseModel):
    task_id: str
    task: dict[str, Any] | None
    expired: bool
    consumed: bool


def _error(exc: LocalAgentTaskError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status,
                        content={"detail": exc.message, "error": exc.code})


def create_local_agent_task_router(
    *,
    require_admin: Callable[..., Any],
    get_manager: Callable[[], Optional[LocalAgentTaskManager]],
    get_user_id: Callable[[Request], Any] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/local-agent", tags=["local-agent"],
                       dependencies=[Depends(require_admin)])

    def _manager() -> LocalAgentTaskManager:
        manager = get_manager()
        if manager is None:
            raise HTTPException(status_code=409, detail="local agent tasks are disabled")
        return manager

    async def _task_call(method: str, *args: Any, **kwargs: Any) -> Any:
        manager = _manager()
        canonical = getattr(manager, "canonical", None)
        if canonical is not None:
            return await getattr(canonical, method)(*args, **kwargs)
        return await asyncio.to_thread(getattr(manager, method), *args, **kwargs)

    async def _owner(request: Request) -> str:
        if get_user_id is None:
            return "admin"
        value = get_user_id(request)
        if asyncio.iscoroutine(value):
            value = await value
        if isinstance(value, dict):
            value = value.get("id") or value.get("user_id")
        return str(value or "admin")

    async def _review_owner(request: Request) -> str:
        if get_user_id is None:
            raise HTTPException(status_code=403, detail="通常の本人セッションが必要です")
        principal = get_user_id(request)
        if asyncio.iscoroutine(principal):
            principal = await principal
        actor = _authenticated_human_actor(principal)
        authority = principal.get("_authority_source") if isinstance(principal, dict) else getattr(principal, "_authority_source", None)
        identity = actor.get("id") or actor.get("user_id")
        try:
            owner = str(uuid.UUID(str(identity)))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=403, detail="安定した本人IDが必要です") from None
        if authority != "web_session" or actor["actor_type"] != "human" or actor["is_agent"] or actor["role"] != "admin":
            raise HTTPException(status_code=403, detail="通常の本人セッションが必要です")
        return owner

    def _review_nonce(nonce: str) -> str:
        if not TASK_ID_RE.fullmatch(nonce):
            raise HTTPException(status_code=404, detail="指示レビューが見つかりません")
        return nonce

    async def _task_owner(request: Request) -> tuple[str, bool]:
        owner = await _owner(request)
        try:
            return owner, await _review_owner(request) == owner
        except HTTPException:
            return owner, False

    def _review_error(exc: CodexTaskProtocolError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": str(exc), "detail": "ホスト指示またはCLIの確認が失効しました。全文を確認し直してください"})

    def _profile_error(exc: CodexProfileError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.code, "detail": "専用Codex接続には本人への個別許可と公式認証が必要です"})

    @router.get("/codex-app-profile/status", response_model=CodexProfileStatus,
                operation_id="local_agent_codex_profile_status")
    async def codex_profile_status(request: Request) -> Any:
        owner = await _review_owner(request)
        try:
            return await asyncio.to_thread(_manager().runner.codex_profiles.status, owner)
        except CodexProfileError as exc:
            return _profile_error(exc)

    @router.post("/codex-app-profile/handoff", response_model=CodexProfileHandoff,
                 operation_id="local_agent_codex_profile_handoff")
    async def codex_profile_handoff(body: CodexProfileIntentRequest, request: Request) -> Any:
        owner = await _review_owner(request)
        try:
            return await asyncio.to_thread(_manager().runner.codex_profiles.handoff, owner)
        except CodexProfileError as exc:
            return _profile_error(exc)

    @router.post("/codex-app-profile/approve", response_model=CodexProfileApprovalReceipt,
                 operation_id="local_agent_codex_profile_approve")
    async def codex_profile_approve(body: CodexProfileApprovalRequest, request: Request) -> Any:
        owner = await _review_owner(request)
        try:
            return await asyncio.to_thread(_manager().runner.codex_profiles.approve_handoff,
                                           owner, **body.model_dump())
        except CodexProfileError as exc:
            return _profile_error(exc)

    @router.post("/codex-app-profile/login", response_model=CodexProfileLoginHandoff,
                 operation_id="local_agent_codex_profile_login")
    async def codex_profile_login(body: CodexProfileBeginRequest, request: Request) -> Any:
        owner = await _review_owner(request)
        try:
            return await asyncio.to_thread(_manager().runner.codex_profiles.begin_login,
                                           owner, **body.model_dump())
        except CodexProfileError as exc:
            return _profile_error(exc)

    @router.post("/codex-app-profile/poll", response_model=CodexProfileStatus,
                 operation_id="local_agent_codex_profile_poll")
    async def codex_profile_poll(body: CodexProfileIntentRequest, request: Request) -> Any:
        owner = await _review_owner(request)
        try:
            return await asyncio.to_thread(_manager().runner.codex_profiles.poll_login, owner)
        except CodexProfileError as exc:
            return _profile_error(exc)

    @router.post("/codex-app-profile/disconnect", response_model=CodexProfileStatus,
                 operation_id="local_agent_codex_profile_disconnect")
    async def codex_profile_disconnect(body: CodexProfileIntentRequest, request: Request) -> Any:
        owner = await _review_owner(request)
        try:
            return await asyncio.to_thread(_manager().runner.codex_profiles.disconnect, owner)
        except CodexProfileError as exc:
            return _profile_error(exc)

    @router.post("/instruction-reviews/preview", response_model=InstructionReviewPreview,
                 operation_id="local_agent_instruction_review_preview")
    async def instruction_review_preview(body: LocalAgentTaskCreateRequest, request: Request) -> Any:
        owner = await _review_owner(request)
        try:
            return await asyncio.to_thread(_manager().preview_instruction_review,
                                          body.model_dump(exclude_none=True), owner=owner)
        except LocalAgentTaskError as exc:
            return _error(exc)
        except CodexTaskProtocolError as exc:
            return _review_error(exc)

    @router.get("/instruction-reviews/{nonce}", response_model=InstructionReviewStatus,
                operation_id="local_agent_instruction_review_status")
    async def instruction_review_status(nonce: str, request: Request) -> Any:
        owner = await _review_owner(request)
        try:
            return await asyncio.to_thread(_manager().instruction_review_status, _review_nonce(nonce), owner=owner)
        except LocalAgentTaskError as exc:
            return _error(exc)

    @router.post("/instruction-reviews/{nonce}/approve", operation_id="local_agent_instruction_review_approve")
    async def instruction_review_approve(nonce: str, body: InstructionReviewApproveRequest, request: Request) -> Any:
        owner = await _review_owner(request)
        if not body.accept_openai_transmission or not body.accept_trusted_host_assumption:
            raise HTTPException(status_code=409, detail="本文のOpenAI送信と信頼済みホストの前提の明示採用が必要です")
        try:
            task, created = await asyncio.to_thread(
                _manager().approve_instruction_review, _review_nonce(nonce), owner=owner,
                review_digest=body.review_digest, task_fingerprint=body.task_fingerprint)
            return JSONResponse(status_code=202 if created else 200, content={"task": task, "created": created})
        except LocalAgentTaskError as exc:
            return _error(exc)
        except CodexTaskProtocolError as exc:
            return _review_error(exc)

    @router.get("/status", operation_id="local_agent_status")
    async def local_agent_status() -> dict[str, Any]:
        return _manager().describe()

    @router.post("/tasks", status_code=202, operation_id="local_agent_task_create")
    async def local_agent_task_create(
        body: LocalAgentTaskCreateRequest,
        request: Request,
        idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    ) -> Any:
        manager = _manager()
        payload = body.model_dump(exclude_none=True)
        key = idempotency_key or payload.pop("idempotency_key", None)
        payload.pop("idempotency_key", None)
        canonical_request = body.agent_id is not None or body.provider != "codex-cli"
        owner = await _review_owner(request) if canonical_request or body.codex_connection == "app_profile" else await _owner(request)
        try:
            if canonical_request:
                canonical = getattr(manager, "canonical", None)
                if canonical is None:
                    raise LocalAgentTaskError("canonical_runtime_unavailable", "Common AgentWork is not connected", 409)
                # The admin role is only used by Agent selection ACLs. Actual
                # tool authority always resolves the pinned Agent revision.
                task, created = await canonical.submit(payload, owner=owner, idempotency_key=key, actor_role="admin")
            else:
                task, created = await asyncio.to_thread(manager.submit, payload, owner=owner, idempotency_key=key)
        except LocalAgentTaskError as exc:
            return _error(exc)
        return JSONResponse(status_code=202 if created else 200, content={"task": task, "created": created})

    @router.get("/tasks", operation_id="local_agent_task_list")
    async def local_agent_task_list(
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        status: Optional[str] = Query(None, pattern="^(queued|running|waiting|waiting_approval|interrupted|completed|failed|cancelled)$"),
    ) -> Any:
        try:
            owner, allow_profile = await _task_owner(request)
            tasks = await _task_call("list", limit=limit, status=status, owner=owner, allow_profile=allow_profile)
        except LocalAgentTaskError as exc:
            return _error(exc)
        return {"tasks": tasks}

    def _task_id(task_id: str) -> str:
        if not TASK_ID_RE.fullmatch(task_id):
            raise HTTPException(status_code=404, detail="task not found")
        return task_id

    @router.get("/tasks/{task_id}", operation_id="local_agent_task_get")
    async def local_agent_task_get(task_id: str, request: Request) -> Any:
        try:
            owner, allow_profile = await _task_owner(request)
            return {"task": await _task_call("get", _task_id(task_id), owner=owner, allow_profile=allow_profile)}
        except LocalAgentTaskError as exc:
            return _error(exc)

    @router.get("/tasks/{task_id}/tail", operation_id="local_agent_task_tail")
    async def local_agent_task_tail(
        task_id: str,
        request: Request,
        limit: int = Query(50, ge=1, le=200),
        max_bytes: int = Query(16000, ge=1024, le=64000),
    ) -> Any:
        try:
            owner, allow_profile = await _task_owner(request)
            return await _task_call("tail", _task_id(task_id), limit=limit, max_bytes=max_bytes, owner=owner, allow_profile=allow_profile)
        except LocalAgentTaskError as exc:
            return _error(exc)

    @router.post("/tasks/{task_id}/cancel", operation_id="local_agent_task_cancel")
    async def local_agent_task_cancel(task_id: str, request: Request) -> Any:
        try:
            owner, allow_profile = await _task_owner(request)
            return {"task": await _task_call("cancel", _task_id(task_id), owner=owner, allow_profile=allow_profile)}
        except LocalAgentTaskError as exc:
            return _error(exc)

    return router
