"""Canonical Web/Mobile contract for private Daily capture and organization."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..memory.models import DocsDailyCapture, KnowledgeNode
from ..services.daily_capture_service import DailyCaptureConflict, DailyCaptureService
from ..services.docs_acl import can_read_node
from ..services.docs_consistency import DocsConflict


class DailyCaptureBody(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    day: date
    idempotency_key: UUID
    docs_library_id: UUID | None = None


class DailyCaptureChange(BaseModel):
    kind: Literal["tag", "link"]
    target_id: str
    label: str
    removed: bool


class DailyCaptureResponse(BaseModel):
    id: str
    node_id: str
    day_node_id: str
    docs_library_id: str
    day: str
    content: str
    status: Literal["queued", "running", "succeeded", "failed", "undone"]
    attempt_count: int
    error_message: str | None = None
    changes: list[DailyCaptureChange] = Field(default_factory=list)
    undo_skipped_count: int = 0
    created_at: datetime
    updated_at: datetime


class DailyCaptureListResponse(BaseModel):
    items: list[DailyCaptureResponse] = Field(default_factory=list)
    docs_library_id: str | None = None
    library_name: str = "Personal Library"
    day: str
    day_node_id: str | None = None


def create_daily_capture_router(*, get_db_manager, get_current_user, require_auth_dependency):
    router = APIRouter(prefix="/daily-captures", tags=["docs"])

    def mapped(error):
        if isinstance(error, HTTPException):
            return error
        if isinstance(error, PermissionError):
            return HTTPException(403, str(error))
        if isinstance(error, (DailyCaptureConflict, DocsConflict)):
            return HTTPException(409, str(error))
        if isinstance(error, ValueError):
            return HTTPException(400, str(error))
        return HTTPException(503, "Dailyメモを処理できません。時間をおいて再試行してください")

    @router.post("", response_model=DailyCaptureResponse, operation_id="create_daily_capture")
    async def capture(request: Request, body: DailyCaptureBody, _auth=Depends(require_auth_dependency)):
        actor = await get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            service = DailyCaptureService(session)
            job = await service.capture(actor_id=actor, content=body.content, day=body.day, key=body.idempotency_key, library_id=body.docs_library_id)
            result = await service.snapshot(job, actor)
            await session.commit()
            return result
        except Exception as error:
            await session.rollback()
            raise mapped(error) from error
        finally:
            await session.close()

    @router.get("", response_model=DailyCaptureListResponse, operation_id="list_daily_captures")
    async def listing(request: Request, day: date, docs_library_id: UUID | None = None, _auth=Depends(require_auth_dependency)):
        actor = await get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            service = DailyCaptureService(session)
            library = await service.library(actor, docs_library_id)
            result = {"items": [], "day": day.isoformat(), "docs_library_id": str(library.id) if library else None, "library_name": library.name if library else "Personal Library", "day_node_id": None}
            if library is None:
                return result
            day_node = (await session.execute(select(KnowledgeNode).where(
                KnowledgeNode.docs_library_id == library.id, KnowledgeNode.day_date == day, KnowledgeNode.archived_at.is_(None),
            ).order_by(KnowledgeNode.created_at).limit(1))).scalar_one_or_none()
            if day_node is not None and await can_read_node(session, day_node, actor):
                result["day_node_id"] = str(day_node.id)
            jobs = (await session.execute(select(DocsDailyCapture).where(
                DocsDailyCapture.actor_user_id == actor, DocsDailyCapture.docs_library_id == library.id, DocsDailyCapture.day == day,
            ).order_by(DocsDailyCapture.created_at.desc()).limit(100))).scalars().all()
            for job in jobs:
                try:
                    result["items"].append(await service.snapshot(job, actor))
                except PermissionError:
                    continue
            return result
        except Exception as error:
            raise mapped(error) from error
        finally:
            await session.close()

    async def job_action(request, job_id, action=None):
        actor = await get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            statement = select(DocsDailyCapture).where(DocsDailyCapture.id == job_id, DocsDailyCapture.actor_user_id == actor)
            if action:
                statement = statement.with_for_update()
            job = (await session.execute(statement)).scalar_one_or_none()
            if job is None:
                raise HTTPException(404, "Dailyメモが見つかりません")
            service = DailyCaptureService(session)
            if action == "retry":
                await service.retry(job, actor)
            elif action == "undo":
                await service.undo(job, actor)
            result = await service.snapshot(job, actor)
            if action:
                await session.commit()
            return result
        except Exception as error:
            await session.rollback()
            raise mapped(error) from error
        finally:
            await session.close()

    @router.get("/{job_id}", response_model=DailyCaptureResponse, operation_id="get_daily_capture")
    async def detail(request: Request, job_id: UUID, _auth=Depends(require_auth_dependency)):
        return await job_action(request, job_id)

    @router.post("/{job_id}/retry", response_model=DailyCaptureResponse, operation_id="retry_daily_capture")
    async def retry(request: Request, job_id: UUID, _auth=Depends(require_auth_dependency)):
        return await job_action(request, job_id, "retry")

    @router.post("/{job_id}/undo", response_model=DailyCaptureResponse, operation_id="undo_daily_capture")
    async def undo(request: Request, job_id: UUID, _auth=Depends(require_auth_dependency)):
        return await job_action(request, job_id, "undo")

    return router
