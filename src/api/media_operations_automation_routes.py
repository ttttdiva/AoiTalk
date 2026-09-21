"""HTTP API for AoiTalk-owned MediaOps Automation."""
from __future__ import annotations
from typing import Any, Callable, Literal
from typing import Annotated
from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..services.media_operations_automation_service import MediaOperationsAutomationService
from .media_operations_routes import _actor, _principal_projection, _raise_http_error, _with_session


class AutomationRevisionFields(BaseModel):
    model_config=ConfigDict(extra="forbid")
    execution_mode: Literal["research_only","draft","review_before_generate","auto_generate"]="draft"
    trigger: dict[str,Any]=Field(default_factory=lambda:{"type":"manual"})
    discovery: dict[str,Any]=Field(default_factory=lambda:{"type":"manual"})
    research_binding: dict[str,Any]=Field(default_factory=dict)
    planning_policy: dict[str,Any]=Field(default_factory=lambda:{"novelty_policy":"balanced","candidate_count":3})
    generation_action: dict[str,Any]=Field(default_factory=dict)
    fallback: dict[str,Any]=Field(default_factory=dict)

class AutomationProgramCreate(AutomationRevisionFields):
    project_id: str|None=None
    name: str=Field(min_length=1,max_length=255)
    enabled: bool=True

class AutomationRevisionCreate(AutomationRevisionFields):
    expected_version: int=Field(ge=1)

class ToggleRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    enabled: bool

class DuplicateRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    name: str|None=Field(default=None,max_length=255)

class TriggerRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    trigger_key: str=Field(min_length=1,max_length=255)
    trigger_kind: str=Field(default="manual",min_length=1,max_length=32)
    execute: bool=True

class CandidateEditRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    payload: dict[str,Any]


def create_media_operations_automation_router(*,get_db_manager:Callable[[],Any],get_user_from_request:Callable[...,Any],require_auth_dependency:Callable[...,Any])->APIRouter:
    router=APIRouter(prefix="/api/operations/media/automations",tags=["media-operations-automation"])
    async def actor(request:Request)->dict[str,Any]: return _principal_projection(await _actor(get_user_from_request,request))
    async def call(request:Request,callback):
        principal=await actor(request)
        try: return await _with_session(get_db_manager,lambda session: callback(MediaOperationsAutomationService(session=session),session,principal))
        except Exception as exc: _raise_http_error(exc); raise

    @router.get("")
    async def list_programs(request:Request,project_id:str|None=Query(None),limit:int=Query(100,ge=1,le=200),_auth:Any=Depends(require_auth_dependency)):
        return await call(request,lambda svc,s,a:svc.list_programs(s,a,project_id=project_id,limit=limit))

    @router.post("")
    async def create_program(payload:AutomationProgramCreate,request:Request,idempotency_key:Annotated[str,Header(alias="Idempotency-Key",min_length=1,max_length=128)],_auth:Any=Depends(require_auth_dependency)):
        data=payload.model_dump()
        return await call(request,lambda svc,s,a:svc.create_program(s,a,**data,idempotency_key=idempotency_key))

    @router.get("/{program_id}")
    async def get_program(program_id:str,request:Request,_auth:Any=Depends(require_auth_dependency)):
        return await call(request,lambda svc,s,a:svc.get_program(s,a,program_id))

    @router.post("/{program_id}/revisions")
    async def append_revision(program_id:str,payload:AutomationRevisionCreate,request:Request,idempotency_key:Annotated[str,Header(alias="Idempotency-Key",min_length=1,max_length=128)],_auth:Any=Depends(require_auth_dependency)):
        data=payload.model_dump(); expected=data.pop("expected_version")
        return await call(request,lambda svc,s,a:svc.append_revision(s,a,program_id,expected_version=expected,idempotency_key=idempotency_key,**data))

    @router.patch("/{program_id}/enabled")
    async def set_enabled(program_id:str,payload:ToggleRequest,request:Request,_auth:Any=Depends(require_auth_dependency)):
        return await call(request,lambda svc,s,a:svc.set_enabled(s,a,program_id,enabled=payload.enabled))

    @router.post("/{program_id}/duplicate")
    async def duplicate(program_id:str,payload:DuplicateRequest,request:Request,idempotency_key:Annotated[str,Header(alias="Idempotency-Key",min_length=1,max_length=128)],_auth:Any=Depends(require_auth_dependency)):
        return await call(request,lambda svc,s,a:svc.duplicate_program(s,a,program_id,name=payload.name,idempotency_key=idempotency_key))

    @router.get("/generation-studio/{workspace_id}/presets")
    async def preset_catalog(workspace_id:str,request:Request,_auth:Any=Depends(require_auth_dependency)):
        return await call(request,lambda svc,s,a:svc.list_preset_catalog(s,a,workspace_id=workspace_id))

    @router.post("/{program_id}/runs")
    async def trigger(program_id:str,payload:TriggerRequest,request:Request,_auth:Any=Depends(require_auth_dependency)):
        return await call(request,lambda svc,s,a:svc.trigger_program(s,a,program_id,trigger_key=payload.trigger_key,trigger_kind=payload.trigger_kind,execute=payload.execute))

    @router.get("/runs/list")
    async def list_runs(request:Request,program_id:str|None=Query(None),project_id:str|None=Query(None),limit:int=Query(100,ge=1,le=200),_auth:Any=Depends(require_auth_dependency)):
        return await call(request,lambda svc,s,a:svc.list_runs(s,a,program_id=program_id,project_id=project_id,limit=limit))

    @router.get("/runs/{run_id}")
    async def get_run(run_id:str,request:Request,_auth:Any=Depends(require_auth_dependency)):
        return await call(request,lambda svc,s,a:svc.get_run(s,a,run_id))

    @router.post("/runs/{run_id}/resume")
    async def resume(run_id:str,request:Request,_auth:Any=Depends(require_auth_dependency)):
        return await call(request,lambda svc,s,a:svc.execute_run(s,a,run_id))

    @router.patch("/runs/{run_id}/candidates/{candidate_id}")
    async def edit_candidate(run_id:str,candidate_id:str,payload:CandidateEditRequest,request:Request,_auth:Any=Depends(require_auth_dependency)):
        return await call(request,lambda svc,s,a:svc.edit_candidate(s,a,run_id,candidate_id=candidate_id,payload=payload.payload))

    @router.post("/runs/{run_id}/regenerate")
    async def regenerate(run_id:str,request:Request,idempotency_key:Annotated[str,Header(alias="Idempotency-Key",min_length=1,max_length=128)],_auth:Any=Depends(require_auth_dependency)):
        return await call(request,lambda svc,s,a:svc.regenerate_candidates(s,a,run_id,idempotency_key=idempotency_key))

    @router.post("/runs/{run_id}/candidates/{candidate_id}/approve")
    async def approve(run_id:str,candidate_id:str,request:Request,_auth:Any=Depends(require_auth_dependency)):
        return await call(request,lambda svc,s,a:svc.approve_candidate(s,a,run_id,candidate_id=candidate_id))

    @router.post("/runs/{run_id}/retry-uncertain")
    async def retry_uncertain(run_id:str,request:Request,_auth:Any=Depends(require_auth_dependency)):
        return await call(request,lambda svc,s,a:svc.retry_uncertain_generation(s,a,run_id))

    @router.post("/runs/{run_id}/reconcile")
    async def reconcile(run_id:str,request:Request,_auth:Any=Depends(require_auth_dependency)):
        return await call(request,lambda svc,s,a:svc.reconcile_run(s,a,run_id))
    return router
