"""タスク添付ファイルとエージェントトリアージのエンドポイント。"""

from __future__ import annotations

import logging
import mimetypes
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select

from ...uuid_http import parse_uuid_or_400
from ....memory.models import TaskAttachment, TaskComment
from ....services.task_management_service import TaskManagementError
from ._shared import TaskRouterContext

logger = logging.getLogger(__name__)


def register_attachment_routes(router: APIRouter, ctx: TaskRouterContext) -> None:
    require_auth_dependency = ctx.require_auth_dependency
    get_db_manager = ctx.get_db_manager
    _get_current_user = ctx.get_current_user
    _translate_service_error = ctx.translate_service_error
    _load_task_for_attachment = ctx.load_task_for_attachment
    _triage_task = ctx.triage_task
    _serialize_attachment = ctx.serialize_attachment
    _sanitize_file_name = ctx.sanitize_file_name
    _project_storage_root = ctx.project_storage_root
    _resolve_attachment_file = ctx.resolve_attachment_file
    _unique_target_path = ctx.unique_target_path
    _attachment_kind = ctx.attachment_kind
    blocked_attachment_extensions = ctx.blocked_attachment_extensions

    @router.post("/tasks/{task_id}/agent-triage")
    async def run_task_agent_triage(
        task_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        marker = "[aoitalk-agent-triage]"
        try:
            task = await _load_task_for_attachment(
                session, user_id=user_id, task_id=task_id, permission="write"
            )
            triage = _triage_task(task)
            metadata = dict(task.task_metadata or {})
            run_id = str(uuid4())
            metadata.update(
                {
                    "agent_triage_status": triage["status"],
                    "agent_triage_summary": triage["summary"],
                    "agent_triage_questions": triage["questions"],
                    "agent_triage_checked_at": datetime.utcnow().isoformat(),
                    "agent_triage_run_id": run_id,
                    "agent_triage_error": None,
                }
            )
            task.task_metadata = metadata
            task.updated_at = datetime.utcnow()

            comment_content = f"{marker}\n{triage['summary']}"
            if triage["questions"]:
                question_lines = "\n".join(f"- {question}" for question in triage["questions"])
                comment_content = f"{comment_content}\n\nQuestions:\n{question_lines}"

            result = await session.execute(
                select(TaskComment).where(TaskComment.task_id == task.id)
            )
            comment = next(
                (
                    candidate
                    for candidate in result.scalars().all()
                    if (candidate.content or "").startswith(marker)
                ),
                None,
            )
            if comment is None:
                session.add(
                    TaskComment(
                        task_id=task.id,
                        user_id=user_id,
                        content=comment_content,
                    )
                )
            else:
                comment.content = comment_content
                comment.updated_at = datetime.utcnow()

            await session.commit()
            await session.refresh(task)
            return {
                "task_id": str(task.id),
                "status": triage["status"],
                "summary": triage["summary"],
                "questions": triage["questions"],
                "metadata": metadata,
                "task": task.to_dict(),
            }
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        except Exception as exc:
            await session.rollback()
            logger.exception("Task agent triage failed")
            try:
                from ....services.failure_recorder import record_failure_event

                await record_failure_event(
                    source="backend",
                    operation="task_agent_triage",
                    task_id=task_id,
                    error=exc,
                    input_summary={"task_id": task_id},
                )
            except Exception:
                logger.debug("Failed to record task triage failure", exc_info=True)
            raise HTTPException(status_code=500, detail="Task agent triage failed") from exc
        finally:
            await session.close()

    @router.get("/tasks/{task_id}/attachments")
    async def list_task_attachments(
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
            result = await session.execute(
                select(TaskAttachment)
                .where(TaskAttachment.task_id == task.id)
                .order_by(TaskAttachment.created_at.desc())
            )
            return [_serialize_attachment(row) for row in result.scalars().all()]
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.post("/tasks/{task_id}/attachments")
    async def upload_task_attachment(
        task_id: str,
        request: Request,
        file: UploadFile = File(...),
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            task = await _load_task_for_attachment(
                session, user_id=user_id, task_id=task_id, permission="write"
            )
            file_name = _sanitize_file_name(file.filename or "uploaded-file")
            ext = Path(file_name).suffix.lower()
            if ext in blocked_attachment_extensions:
                raise HTTPException(status_code=400, detail="This file extension cannot be uploaded")

            _, root_path = await _project_storage_root(task.project_id)
            target_dir = root_path / "attachments" / "tasks" / str(task.id)
            target_dir.mkdir(parents=True, exist_ok=True)
            content = await file.read()
            target_path = _unique_target_path(target_dir, file_name)
            target_path.write_bytes(content)

            relative_path = target_path.relative_to(root_path).as_posix()
            mime_type = file.content_type or mimetypes.guess_type(file_name)[0]
            attachment = TaskAttachment(
                task_id=task.id,
                project_id=task.project_id,
                file_path=relative_path,
                display_name=target_path.name,
                mime_type=mime_type,
                size_bytes=len(content),
                kind=_attachment_kind(mime_type, file_name),
                created_by=user_id,
                attachment_metadata={},
            )
            session.add(attachment)
            await session.commit()
            await session.refresh(attachment)
            return _serialize_attachment(attachment)
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        except HTTPException:
            raise
        except Exception as exc:
            await session.rollback()
            logger.exception("Task attachment upload failed")
            try:
                from ....services.failure_recorder import record_failure_event

                await record_failure_event(
                    source="backend",
                    operation="task_attachment_upload",
                    task_id=task_id,
                    error=exc,
                    input_summary={"task_id": task_id, "filename": file.filename},
                )
            except Exception:
                logger.debug("Failed to record task attachment upload failure", exc_info=True)
            raise HTTPException(status_code=500, detail="Task attachment upload failed") from exc
        finally:
            await session.close()

    @router.get("/tasks/{task_id}/attachments/{attachment_id}")
    async def download_task_attachment(
        task_id: str,
        attachment_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            task = await _load_task_for_attachment(
                session, user_id=user_id, task_id=task_id, permission="read"
            )
            result = await session.execute(
                select(TaskAttachment).where(
                    TaskAttachment.id == parse_uuid_or_400(attachment_id, "attachment_id"),
                    TaskAttachment.task_id == task.id,
                )
            )
            attachment = result.scalar_one_or_none()
            if attachment is None:
                raise HTTPException(status_code=404, detail="Attachment not found")
            _, root_path = await _project_storage_root(task.project_id)
            target = _resolve_attachment_file(root_path, attachment.file_path)
            if not target.exists() or not target.is_file():
                raise HTTPException(status_code=404, detail="File not found")
            # Web BFF と同じく ASCII フォールバック + RFC5987 の filename* を併記する
            ascii_name = attachment.display_name.replace('"', "").encode(
                "ascii", "replace"
            ).decode("ascii")
            encoded_name = quote(attachment.display_name)
            return Response(
                content=target.read_bytes(),
                media_type=attachment.mime_type or "application/octet-stream",
                headers={
                    "Content-Disposition": (
                        f'inline; filename="{ascii_name}"; '
                        f"filename*=UTF-8''{encoded_name}"
                    ),
                },
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.delete("/tasks/{task_id}/attachments/{attachment_id}", status_code=204)
    async def delete_task_attachment(
        task_id: str,
        attachment_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            task = await _load_task_for_attachment(
                session, user_id=user_id, task_id=task_id, permission="write"
            )
            result = await session.execute(
                select(TaskAttachment).where(
                    TaskAttachment.id == parse_uuid_or_400(attachment_id, "attachment_id"),
                    TaskAttachment.task_id == task.id,
                )
            )
            attachment = result.scalar_one_or_none()
            if attachment is None:
                raise HTTPException(status_code=404, detail="Attachment not found")
            await session.delete(attachment)
            await session.commit()
            return Response(status_code=204)
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()
