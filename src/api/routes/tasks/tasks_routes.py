"""タスク本体・参照・繰り返し・コメントのエンドポイント。"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select

from ...uuid_http import parse_uuid_or_400
from ....memory.models import (
    ConversationMessage,
    ConversationParticipant,
    ConversationSession,
    KnowledgeNode,
    KnowledgeWorkspace,
    TaskComment,
    TaskRecurrenceRule,
    TaskReference,
)
from ....services.task_management_service import (
    TaskManagementError,
    normalize_task_status,
)
from ....task_time import normalize_task_timezone
from ._shared import (
    CreateTaskPayload,
    TaskCommentPayload,
    TaskRecurrencePayload,
    TaskReferencePayload,
    TaskRouterContext,
    UpdateTaskPayload,
    _build_update_task_updates,
    _parse_wall_clock_datetime,
)

logger = logging.getLogger(__name__)


def register_task_routes(router: APIRouter, ctx: TaskRouterContext) -> None:
    require_auth_dependency = ctx.require_auth_dependency
    get_db_manager = ctx.get_db_manager
    service = ctx.service
    _get_current_user = ctx.get_current_user
    _translate_service_error = ctx.translate_service_error
    _sync_google_calendar_warning_only = ctx.sync_google_calendar_warning_only
    _delete_google_calendar_warning_only = ctx.delete_google_calendar_warning_only
    _with_pending_agent_triage = ctx.with_pending_agent_triage
    _load_task_for_attachment = ctx.load_task_for_attachment
    _serialize_task_reference = ctx.serialize_task_reference
    _validate_project_relative_path = ctx.validate_project_relative_path
    _project_storage_root = ctx.project_storage_root
    _resolve_attachment_file = ctx.resolve_attachment_file

    @router.post("/tasks")
    async def create_task(
        payload: CreateTaskPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            task = await service.create_task(
                session,
                user_id=user_id,
                project_id=parse_uuid_or_400(payload.project_id, "project_id"),
                knowledge_node_id=parse_uuid_or_400(payload.knowledge_node_id, "knowledge_node_id"),
                title=payload.title,
                description=payload.description,
                status=payload.status,
                priority=payload.priority,
                start_at=_parse_wall_clock_datetime(payload.start_at, "start_at"),
                end_at=_parse_wall_clock_datetime(payload.end_at, "end_at"),
                all_day=payload.all_day,
                reminder_offsets=payload.reminder_offsets,
                notifications_enabled=(
                    payload.notifications_enabled
                    if payload.notifications_enabled is not None
                    else None
                ),
                estimated_hours=payload.estimated_hours,
                parent_task_id=parse_uuid_or_400(
                    payload.parent_task_id, "parent_task_id"
                ),
                source=payload.source,
                assignee_ids=[
                    parse_uuid_or_400(value, "assignee_id") for value in payload.assignee_ids
                ],
                tag_ids=[parse_uuid_or_400(value, "tag_id") for value in payload.tag_ids],
                recurrence_rrule=payload.recurrence_rrule,
                recurrence_timezone=payload.recurrence_timezone,
                task_metadata=_with_pending_agent_triage(payload.task_metadata),
            )
            sync_result = await _sync_google_calendar_warning_only(
                session, user_id=user_id, task_id=str(task["id"])
            )
            if "metadata" in sync_result:
                task["metadata"] = sync_result["metadata"]
            task["google_calendar_sync"] = sync_result
            return task
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        except Exception as exc:
            await session.rollback()
            logger.exception("Unexpected task creation failure for user %s", user_id)
            raise HTTPException(status_code=500, detail="Task creation failed") from exc
        finally:
            await session.close()

    @router.get("/tasks")
    async def list_tasks(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.list_tasks(
                session,
                user_id=user_id,
                project_id=parse_uuid_or_400(
                    request.query_params.get("project_id"), "project_id"
                ),
                space_id=parse_uuid_or_400(
                    request.query_params.get("space_id"), "space_id"
                ),
                status=request.query_params.get("status"),
                assignee_id=parse_uuid_or_400(
                    request.query_params.get("assignee_id"), "assignee_id"
                ),
                search=request.query_params.get("search"),
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.get("/tasks/{task_id}")
    async def get_task(
        task_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.get_task(
                session,
                user_id=user_id,
                task_id=parse_uuid_or_400(task_id, "task_id"),
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.patch("/tasks/{task_id}")
    async def update_task(
        task_id: str,
        payload: UpdateTaskPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            updates = _build_update_task_updates(payload)
            task = await service.update_task(
                session,
                user_id=user_id,
                task_id=parse_uuid_or_400(task_id, "task_id"),
                updates=updates,
            )
            sync_result = await _sync_google_calendar_warning_only(
                session, user_id=user_id, task_id=str(task["id"])
            )
            if "metadata" in sync_result:
                task["metadata"] = sync_result["metadata"]
            task["google_calendar_sync"] = sync_result
            return task
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.delete("/tasks/{task_id}", status_code=204)
    async def delete_task(
        task_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            await _delete_google_calendar_warning_only(
                session, user_id=user_id, task_id=task_id
            )
            await service.delete_task(
                session,
                user_id=user_id,
                task_id=parse_uuid_or_400(task_id, "task_id"),
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        except Exception as exc:
            await session.rollback()
            logger.exception("Task deletion failed")
            raise HTTPException(
                status_code=500, detail="タスクの削除に失敗しました"
            ) from exc
        finally:
            await session.close()

    @router.get("/tasks/{task_id}/references")
    async def list_task_references(
        task_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            task = await _load_task_for_attachment(
                session, user_id=user_id, task_id=task_id, permission="read"
            )
            write_access = True
            try:
                await service.require_project_permission(
                    session,
                    project_id=task.project_id,
                    user_id=user_id,
                    permission="write",
                )
            except TaskManagementError:
                write_access = False
            result = await session.execute(
                select(TaskReference)
                .where(TaskReference.task_id == task.id)
                .order_by(TaskReference.created_at.desc())
            )
            return [
                await _serialize_task_reference(
                    session,
                    reference,
                    user_id=user_id,
                    can_remove=write_access,
                )
                for reference in result.scalars().all()
            ]
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.post("/tasks/{task_id}/references", status_code=201)
    async def create_task_reference(
        task_id: str,
        payload: TaskReferencePayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            task = await _load_task_for_attachment(
                session, user_id=user_id, task_id=task_id, permission="write"
            )
            target_id = payload.target_id.strip() if payload.target_id else None
            target_path = payload.target_path.strip() if payload.target_path else None
            target_url = payload.target_url.strip() if payload.target_url else None
            if payload.reference_type in {"conversation_session", "conversation_message", "docs_node"} and not target_id:
                raise HTTPException(status_code=400, detail="target_id is required")
            if payload.reference_type == "workspace_file":
                if not target_path:
                    raise HTTPException(status_code=400, detail="target_path is required")
                target_path = _validate_project_relative_path(target_path)
            if payload.reference_type == "url" and (
                not target_url or not re.match(r"^https?://", target_url, re.IGNORECASE)
            ):
                raise HTTPException(status_code=400, detail="http(s) URL is required")

            if payload.reference_type in {"conversation_session", "conversation_message"}:
                result = await session.execute(
                    select(ConversationSession).where(
                        ConversationSession.id == target_id,
                        ConversationSession.deleted_at.is_(None),
                    )
                )
                conversation = result.scalar_one_or_none()
                participant = None
                if conversation and conversation.user_id != str(user_id):
                    participant = (
                        await session.execute(
                            select(ConversationParticipant.id).where(
                                ConversationParticipant.session_id == conversation.id,
                                ConversationParticipant.participant_type == "user",
                                ConversationParticipant.participant_id == str(user_id),
                                ConversationParticipant.status == "joined",
                            )
                        )
                    ).scalar_one_or_none()
                if conversation is None or (
                    conversation.user_id != str(user_id) and participant is None
                ):
                    raise HTTPException(status_code=404, detail="Reference target not found")
                if payload.reference_type == "conversation_message":
                    message_id = (payload.metadata or {}).get("message_id")
                    if message_id:
                        try:
                            message = await session.get(ConversationMessage, UUID(str(message_id)))
                        except ValueError as exc:
                            raise HTTPException(status_code=400, detail="Invalid message_id") from exc
                        if (
                            message is None
                            or message.session_id != conversation.id
                            or message.deleted_at is not None
                        ):
                            raise HTTPException(status_code=404, detail="Reference target not found")
            elif payload.reference_type == "docs_node":
                try:
                    node_id = UUID(str(target_id))
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail="Invalid target_id") from exc
                result = await session.execute(
                    select(KnowledgeNode, KnowledgeWorkspace.owner_user_id)
                    .join(KnowledgeWorkspace, KnowledgeNode.workspace_id == KnowledgeWorkspace.id)
                    .where(
                        KnowledgeNode.id == node_id,
                        KnowledgeNode.archived_at.is_(None),
                    )
                )
                row = result.first()
                if row is None:
                    raise HTTPException(status_code=404, detail="Reference target not found")
                node, owner_id = row
                if node.project_id:
                    await service.require_project_permission(
                        session,
                        project_id=node.project_id,
                        user_id=user_id,
                        permission="read",
                    )
                elif owner_id != user_id:
                    raise HTTPException(status_code=404, detail="Reference target not found")
            elif payload.reference_type == "workspace_file":
                _, root_path = await _project_storage_root(task.project_id)
                _resolve_attachment_file(root_path, target_path or "")

            dedupe_key = f"{target_id or ''}|{target_path or ''}|{target_url or ''}"
            result = await session.execute(
                select(TaskReference).where(
                    TaskReference.task_id == task.id,
                    TaskReference.reference_type == payload.reference_type,
                    TaskReference.relation_type == payload.relation_type,
                    TaskReference.dedupe_key == dedupe_key,
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                return await _serialize_task_reference(
                    session, existing, user_id=user_id, can_remove=True
                )
            reference = TaskReference(
                task_id=task.id,
                project_id=task.project_id,
                reference_type=payload.reference_type,
                relation_type=payload.relation_type,
                target_id=target_id,
                target_path=target_path,
                target_url=target_url,
                display_name=(
                    payload.display_name.strip()
                    if payload.display_name and payload.display_name.strip()
                    else target_url or target_path or target_id or "参照"
                ),
                dedupe_key=dedupe_key,
                reference_metadata=payload.metadata or {},
                created_by=user_id,
            )
            session.add(reference)
            await session.commit()
            await session.refresh(reference)
            return await _serialize_task_reference(
                session, reference, user_id=user_id, can_remove=True
            )
        except TaskManagementError as exc:
            await session.rollback()
            raise _translate_service_error(exc)
        except HTTPException:
            await session.rollback()
            raise
        except Exception as exc:
            await session.rollback()
            logger.exception("Task reference creation failed")
            raise HTTPException(status_code=500, detail="Task reference creation failed") from exc
        finally:
            await session.close()

    @router.delete("/tasks/{task_id}/references/{reference_id}", status_code=204)
    async def delete_task_reference(
        task_id: str,
        reference_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            task = await _load_task_for_attachment(
                session, user_id=user_id, task_id=task_id, permission="write"
            )
            if reference_id.startswith("knowledge-node:"):
                if str(task.knowledge_node_id) == reference_id.removeprefix("knowledge-node:"):
                    task.knowledge_node_id = None
                    await session.commit()
                return Response(status_code=204)
            reference_uuid = parse_uuid_or_400(reference_id, "reference_id")
            result = await session.execute(
                select(TaskReference).where(
                    TaskReference.id == reference_uuid,
                    TaskReference.task_id == task.id,
                )
            )
            reference = result.scalar_one_or_none()
            if reference is None:
                raise HTTPException(status_code=404, detail="Reference not found")
            if reference.relation_type == "source" and request.query_params.get("confirm_source") != "true":
                raise HTTPException(status_code=409, detail="Source reference removal requires confirmation")
            await session.delete(reference)
            await session.commit()
            return Response(status_code=204)
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.get("/tasks/{task_id}/recurrence")
    async def get_task_recurrence(
        task_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            task = await service._load_task(session, parse_uuid_or_400(task_id, "task_id"))
            await service.require_project_permission(
                session,
                project_id=task.project_id,
                user_id=user_id,
                permission="read",
            )
            return task.recurrence_rule.to_dict() if task.recurrence_rule else None
        except TaskManagementError as exc:
            raise _translate_service_error(exc) from exc
        finally:
            await session.close()

    @router.put("/tasks/{task_id}/recurrence")
    async def upsert_task_recurrence(
        task_id: str,
        payload: TaskRecurrencePayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        rrule = (payload.rrule or "").strip()
        if not rrule:
            raise HTTPException(status_code=400, detail="rrule は必須です")

        fields_set = payload.model_fields_set
        session = await get_db_manager().get_session()
        try:
            task = await service._load_task(session, parse_uuid_or_400(task_id, "task_id"))
            await service.require_project_permission(
                session,
                project_id=task.project_id,
                user_id=user_id,
                permission="write",
            )
            result = await session.execute(
                select(TaskRecurrenceRule).where(
                    TaskRecurrenceRule.task_id == task.id
                )
            )
            rule = result.scalar_one_or_none()
            if rule is None:
                rule = TaskRecurrenceRule(
                    task_id=task.id,
                    rrule=rrule,
                    timezone=normalize_task_timezone(payload.timezone),
                    horizon_days=(
                        payload.horizon_days
                        if payload.horizon_days is not None
                        else 90
                    ),
                    trigger_status=normalize_task_status(
                        payload.trigger_status or "closed"
                    ),
                    create_new=bool(payload.create_new),
                    recur_forever=(
                        True
                        if payload.recur_forever is None
                        else bool(payload.recur_forever)
                    ),
                    reset_status_to=normalize_task_status(
                        payload.reset_status_to or "open"
                    ),
                    end_count=payload.end_count,
                    end_date=_parse_wall_clock_datetime(
                        payload.end_date, "end_date"
                    ),
                    skip_weekend=bool(payload.skip_weekend),
                    skip_holiday=bool(payload.skip_holiday),
                )
                session.add(rule)
            else:
                rule.rrule = rrule
                if "timezone" in fields_set:
                    rule.timezone = normalize_task_timezone(payload.timezone)
                if "horizon_days" in fields_set:
                    rule.horizon_days = (
                        payload.horizon_days
                        if payload.horizon_days is not None
                        else 90
                    )
                if "trigger_status" in fields_set:
                    rule.trigger_status = normalize_task_status(
                        payload.trigger_status or "closed"
                    )
                if "create_new" in fields_set:
                    rule.create_new = bool(payload.create_new)
                if "recur_forever" in fields_set:
                    rule.recur_forever = (
                        True
                        if payload.recur_forever is None
                        else bool(payload.recur_forever)
                    )
                if "reset_status_to" in fields_set:
                    rule.reset_status_to = normalize_task_status(
                        payload.reset_status_to or "open"
                    )
                if "end_count" in fields_set:
                    rule.end_count = payload.end_count
                if "end_date" in fields_set:
                    rule.end_date = _parse_wall_clock_datetime(
                        payload.end_date, "end_date"
                    )
                if "skip_weekend" in fields_set:
                    rule.skip_weekend = bool(payload.skip_weekend)
                if "skip_holiday" in fields_set:
                    rule.skip_holiday = bool(payload.skip_holiday)
                rule.updated_at = datetime.utcnow()

            await service._sync_repeat_tag(session, task=task, has_recurrence=True)
            await service._materialize_occurrences(
                session,
                task,
                recurrence_rrule=rule.rrule,
                horizon_days=rule.horizon_days,
            )
            await session.commit()
            await session.refresh(rule)
            return rule.to_dict()
        except TaskManagementError as exc:
            await session.rollback()
            raise _translate_service_error(exc) from exc
        except HTTPException:
            await session.rollback()
            raise
        except Exception as exc:
            await session.rollback()
            logger.exception("Task recurrence upsert failed")
            raise HTTPException(
                status_code=500, detail="繰り返し設定の保存に失敗しました"
            ) from exc
        finally:
            await session.close()

    @router.delete("/tasks/{task_id}/recurrence")
    async def delete_task_recurrence(
        task_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            task = await service._load_task(session, parse_uuid_or_400(task_id, "task_id"))
            await service.require_project_permission(
                session,
                project_id=task.project_id,
                user_id=user_id,
                permission="write",
            )
            if task.recurrence_rule is None:
                raise TaskManagementError(
                    "繰り返し設定が見つかりません", status_code=404
                )
            await session.delete(task.recurrence_rule)
            await service._sync_repeat_tag(session, task=task, has_recurrence=False)
            await service._materialize_occurrences(
                session,
                task,
                recurrence_rrule=None,
                horizon_days=90,
            )
            await session.commit()
            return {"success": True}
        except TaskManagementError as exc:
            await session.rollback()
            raise _translate_service_error(exc) from exc
        except HTTPException:
            await session.rollback()
            raise
        except Exception as exc:
            await session.rollback()
            logger.exception("Task recurrence deletion failed")
            raise HTTPException(
                status_code=500, detail="繰り返し設定の削除に失敗しました"
            ) from exc
        finally:
            await session.close()

    @router.post("/tasks/{task_id}/comments")
    async def add_comment(
        task_id: str,
        payload: TaskCommentPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.add_comment(
                session,
                user_id=user_id,
                task_id=parse_uuid_or_400(task_id, "task_id"),
                content=payload.content,
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()
