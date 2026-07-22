"""タグ・並べ替え・レガシーイベントのエンドポイント。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from ....memory.models import Tag
from ....services.task_management_service import TaskManagementError
from ._shared import (
    CreateTagPayload,
    LegacyEventPayload,
    ReorderTasksPayload,
    TaskRouterContext,
)
from ...uuid_http import parse_uuid_or_400

logger = logging.getLogger(__name__)


def register_tag_reorder_routes(router: APIRouter, ctx: TaskRouterContext) -> None:
    require_auth_dependency = ctx.require_auth_dependency
    get_db_manager = ctx.get_db_manager
    service = ctx.service
    _get_current_user = ctx.get_current_user
    _translate_service_error = ctx.translate_service_error
    _get_readable_space = ctx.get_readable_space
    _can_write_space = ctx.can_write_space
    _load_tag = ctx.load_tag

    @router.post("/projects/{project_id}/tasks/reorder", status_code=204)
    async def reorder_tasks(
        project_id: str,
        payload: ReorderTasksPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            await service.reorder_tasks(
                session,
                user_id=user_id,
                project_id=parse_uuid_or_400(project_id, "project_id"),
                task_ids=[parse_uuid_or_400(tid, "task_id") for tid in payload.task_ids],
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.post("/tasks/reorder")
    async def reorder_tasks_global(
        payload: ReorderTasksPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            await service.reorder_tasks_global(
                session,
                user_id=user_id,
                task_ids=[parse_uuid_or_400(tid, "task_id") for tid in payload.task_ids],
            )
            return {"success": True}
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.get("/projects/{project_id}/tags")
    async def list_tags(
        project_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.list_tags(
                session,
                project_id=parse_uuid_or_400(project_id, "project_id"),
                user_id=user_id,
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.post("/projects/{project_id}/tags")
    async def create_tag(
        project_id: str,
        payload: CreateTagPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.create_tag(
                session,
                project_id=parse_uuid_or_400(project_id, "project_id"),
                name=payload.name,
                color=payload.color,
                user_id=user_id,
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        except Exception as exc:
            await session.rollback()
            logger.exception("Tag creation failed")
            raise HTTPException(
                status_code=500, detail="タグの作成に失敗しました"
            ) from exc
        finally:
            await session.close()

    @router.patch("/tags/{tag_id}")
    async def update_tag(
        tag_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, user_info = await _get_current_user(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        session = await get_db_manager().get_session()
        try:
            tag = await _load_tag(session, tag_id)
            allowed, _space = await _can_write_space(
                session,
                space_id=str(tag.space_id),
                user_id=user_id,
                user_info=user_info,
            )
            if not allowed:
                raise HTTPException(status_code=403, detail="権限がありません")

            has_update = False
            if "name" in body:
                tag.name = body["name"]
                has_update = True
            if "color" in body:
                tag.color = body["color"]
                has_update = True
            if not has_update:
                raise HTTPException(
                    status_code=400, detail="更新するフィールドがありません"
                )

            await session.commit()
            await session.refresh(tag)
            return tag.to_dict()
        finally:
            await session.close()

    @router.post("/tags/{tag_id}/copy")
    async def copy_tag_to_space(
        tag_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, user_info = await _get_current_user(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        target_space_id = (
            body.get("space_id").strip()
            if isinstance(body.get("space_id"), str)
            else ""
        )
        if not target_space_id:
            raise HTTPException(status_code=400, detail="space_id is required")

        session = await get_db_manager().get_session()
        try:
            tag = await _load_tag(session, tag_id)
            source_space = await _get_readable_space(
                session,
                space_id=str(tag.space_id),
                user_id=user_id,
                user_info=user_info,
            )
            if source_space is None:
                raise HTTPException(status_code=404, detail="タグが見つかりません")

            allowed, target_space = await _can_write_space(
                session,
                space_id=target_space_id,
                user_id=user_id,
                user_info=user_info,
            )
            if target_space is None:
                raise HTTPException(
                    status_code=404, detail="スペースが見つかりません"
                )
            if not allowed:
                raise HTTPException(status_code=403, detail="権限がありません")

            if tag.space_id == target_space.id:
                return tag.to_dict()

            existing = await session.execute(
                select(Tag).where(
                    Tag.space_id == target_space.id, Tag.name == tag.name
                )
            )
            existing_tag = existing.scalar_one_or_none()
            if existing_tag is not None:
                return existing_tag.to_dict()

            created = Tag(
                space_id=target_space.id,
                name=tag.name,
                color=tag.color,
                created_by=user_id,
            )
            session.add(created)
            await session.commit()
            await session.refresh(created)
            return created.to_dict()
        finally:
            await session.close()

    @router.delete("/tags/{tag_id}", status_code=204)
    async def delete_tag(
        tag_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, user_info = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            tag = await _load_tag(session, tag_id)
            # Web BFF と同じくスペース所有者または admin のみ削除可
            allowed, _space = await _can_write_space(
                session,
                space_id=str(tag.space_id),
                user_id=user_id,
                user_info=user_info,
            )
            if not allowed:
                raise HTTPException(status_code=403, detail="権限がありません")
            await session.delete(tag)
            await session.commit()
        finally:
            await session.close()

    @router.post("/tasks/{task_id}/events")
    async def create_legacy_event(
        task_id: str,
        payload: LegacyEventPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        task_uuid = parse_uuid_or_400(task_id, "task_id")
        try:
            if payload.event_type in {"started", "resumed"}:
                time_entry = await service.start_timer(
                    session,
                    user_id=user_id,
                    task_id=task_uuid,
                    source=payload.trigger_source,
                )
                task = await service.get_task(
                    session, user_id=user_id, task_id=task_uuid
                )
                return {"task": task, "time_entry": time_entry}
            if payload.event_type == "paused":
                time_entry = await service.stop_timer(session, user_id=user_id)
                task = await service.get_task(
                    session, user_id=user_id, task_id=task_uuid
                )
                return {"task": task, "time_entry": time_entry}
            if payload.event_type == "completed":
                try:
                    time_entry = await service.stop_timer(session, user_id=user_id)
                except TaskManagementError:
                    time_entry = None
                task = await service.update_task(
                    session,
                    user_id=user_id,
                    task_id=task_uuid,
                    updates={"status": "closed"},
                )
                return {"task": task, "time_entry": time_entry}
            if payload.event_type == "blocked":
                task = await service.update_task(
                    session,
                    user_id=user_id,
                    task_id=task_uuid,
                    updates={"status": "blocked"},
                )
                return {"task": task}
            raise HTTPException(
                status_code=400, detail=f"Invalid event_type: {payload.event_type}"
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()
