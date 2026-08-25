"""タスク添付ファイルとエージェントトリアージのエンドポイント。"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import stat
from datetime import datetime
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import select

from ...uuid_http import parse_uuid_or_400
from ....memory.models import TaskAttachment, TaskComment
from ....memory.project_repository import ProjectRepository
from ....tools.file_explorer.storage_context import calculate_storage_usage
from ....services.task_management_service import TaskManagementError
from ._shared import TaskRouterContext

logger = logging.getLogger(__name__)


def register_attachment_routes(router: APIRouter, ctx: TaskRouterContext) -> None:
    require_auth_dependency = ctx.require_auth_dependency
    get_db_manager = ctx.get_db_manager
    service = ctx.service
    _get_current_user = ctx.get_current_user
    _translate_service_error = ctx.translate_service_error
    _load_task_for_attachment = ctx.load_task_for_attachment
    _triage_task = ctx.triage_task
    _serialize_attachment = ctx.serialize_attachment
    _sanitize_file_name = ctx.sanitize_file_name
    _project_storage_root = ctx.project_storage_root
    _resolve_attachment_file = ctx.resolve_attachment_file
    _is_safe_storage_path = ctx.is_safe_workspace_path
    _unique_target_path = ctx.unique_target_path
    _write_unique_target_file = ctx.write_unique_target_file
    _attachment_kind = ctx.attachment_kind

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
        created_path = None
        committed = False
        try:
            task = await _load_task_for_attachment(
                session, user_id=user_id, task_id=task_id, permission="write"
            )
            file_name = _sanitize_file_name(file.filename or "uploaded-file")

            content = await file.read(50 * 1024 * 1024 + 1)
            if len(content) > 50 * 1024 * 1024:
                raise HTTPException(
                    status_code=413,
                    detail="ファイルサイズは 50 MB までです",
                )

            # Every project storage writer uses this row lock.  Re-check the
            # permission after waiting for the lock so a membership revoke or
            # role downgrade cannot race an upload already admitted above.
            project = await ProjectRepository.get_by_id_for_update(
                session, task.project_id
            )
            if project is None:
                raise HTTPException(status_code=404, detail="Project not found")
            await service.require_project_permission(
                session,
                project_id=task.project_id,
                user_id=user_id,
                permission="write",
            )

            _, root_path = await _project_storage_root(task.project_id)
            target_dir = root_path / "attachments" / "tasks" / str(task.id)
            if not _is_safe_storage_path(root_path, target_dir):
                raise HTTPException(status_code=400, detail="Invalid attachment path")
            target_dir.mkdir(parents=True, exist_ok=True)
            usage = await asyncio.to_thread(
                calculate_storage_usage,
                root_path,
                strict=True,
            )
            quota_mb = 1000 if project.storage_quota_mb is None else project.storage_quota_mb
            quota_bytes = max(0, int(quota_mb)) * 1024 * 1024
            if usage["total_bytes"] + len(content) > quota_bytes:
                raise HTTPException(status_code=413, detail="Project storage quota exceeded")

            target_path = _write_unique_target_file(target_dir, file_name, content)
            created_path = target_path
            if not _is_safe_storage_path(root_path, target_path):
                raise HTTPException(status_code=400, detail="Invalid attachment path")

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
            final_usage = await asyncio.to_thread(
                calculate_storage_usage,
                root_path,
                strict=True,
            )
            project.storage_used_mb = final_usage["total_bytes"] / (1024 * 1024)
            await session.commit()
            committed = True
            await session.refresh(attachment)
            return _serialize_attachment(attachment)
        except TaskManagementError as exc:
            await session.rollback()
            if created_path is not None and not committed:
                try:
                    if created_path.is_file() and not created_path.is_symlink():
                        created_path.unlink()
                except OSError:
                    logger.warning("Failed to remove orphaned task attachment: %s", created_path)
            raise _translate_service_error(exc)
        except HTTPException:
            await session.rollback()
            if created_path is not None and not committed:
                try:
                    if created_path.is_file() and not created_path.is_symlink():
                        created_path.unlink()
                except OSError:
                    logger.warning("Failed to remove orphaned task attachment: %s", created_path)
            raise
        except Exception as exc:
            await session.rollback()
            if created_path is not None and not committed:
                try:
                    if created_path.is_file() and not created_path.is_symlink():
                        created_path.unlink()
                except OSError:
                    logger.warning("Failed to remove orphaned task attachment: %s", created_path)
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
            try:
                target_stat = target.lstat()
            except OSError:
                raise HTTPException(status_code=404, detail="File not found")
            if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode):
                raise HTTPException(status_code=404, detail="File not found")
            # Web BFF と同じく ASCII フォールバック + RFC5987 の filename* を併記する
            ascii_name = attachment.display_name.replace('"', "").encode(
                "ascii", "replace"
            ).decode("ascii")
            encoded_name = quote(attachment.display_name)
            stored_mime_type = (attachment.mime_type or "").split(";", 1)[0].strip().lower()
            inline_mime_types = {
                "image/png",
                "image/jpeg",
                "image/gif",
                "image/webp",
                "image/bmp",
            }
            can_render_inline = stored_mime_type in inline_mime_types
            return FileResponse(
                path=target,
                media_type=stored_mime_type if can_render_inline else "application/octet-stream",
                stat_result=target_stat,
                headers={
                    "Content-Disposition": (
                        f'{"inline" if can_render_inline else "attachment"}; filename="{ascii_name}"; '
                        f"filename*=UTF-8''{encoded_name}"
                    ),
                    "X-Content-Type-Options": "nosniff",
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

            project = await ProjectRepository.get_by_id_for_update(
                session, task.project_id
            )
            if project is None:
                raise HTTPException(status_code=404, detail="Project not found")
            await service.require_project_permission(
                session,
                project_id=task.project_id,
                user_id=user_id,
                permission="write",
            )
            _, root_path = await _project_storage_root(task.project_id)
            target = _resolve_attachment_file(root_path, attachment.file_path)
            try:
                target_stat = target.lstat()
            except FileNotFoundError:
                target_stat = None
            except OSError as exc:
                raise HTTPException(status_code=500, detail="Attachment cleanup failed") from exc
            if target_stat is not None:
                if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode):
                    raise HTTPException(status_code=400, detail="Invalid attachment path")
                target.unlink()
            await session.delete(attachment)
            usage = await asyncio.to_thread(
                calculate_storage_usage,
                root_path,
                strict=True,
            )
            project.storage_used_mb = usage["total_bytes"] / (1024 * 1024)
            await session.commit()
            return Response(status_code=204)
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()
