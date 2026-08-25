"""Task, calendar, timer, report, and notification API routes.

エンドポイント定義は `routes/tasks/` 配下の register モジュールへ分割済み。
本モジュールは依存注入とクロージャ helper の生成、`TaskRouterContext` の構築、
各 register 関数の呼び出しに責務を絞る。payload / モジュールレベル helper は
`routes/tasks/_shared.py` から re-export し、既存 import 面を維持する。
"""

from __future__ import annotations

import logging
import os
import re
import stat
import tempfile
import asyncio
from os import PathLike
from pathlib import Path, PurePosixPath
from typing import Any, Optional
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from ..memory.models import (
    ConversationMessage,
    ConversationParticipant,
    ConversationSession,
    KnowledgeNode,
    DocsLibrary,
    Tag,
    Task,
    TaskAttachment,
    TaskReference,
)
from ..memory.project_repository import ProjectRepository
from ..services.docs_acl import can_read_node
from ..services.google_calendar_service import (
    GoogleCalendarService,
    GoogleCalendarServiceError,
)
from ..services.task_management_service import (
    TaskManagementError,
    TaskManagementService,
)
from ..services.space_access import (
    can_write_space as _space_can_write_space,
    get_readable_space as _space_get_readable_space,
    is_admin_user as _space_is_admin_user,
    is_inbox_space as _space_is_inbox_space,
    load_space as _space_load_space,
    member_space_ids as _space_member_space_ids,
)
from ..tools.file_explorer.storage_context import calculate_storage_usage
from .routes.tasks import (
    register_attachment_routes,
    register_google_calendar_routes,
    register_notification_routes,
    register_occurrence_time_routes,
    register_space_routes,
    register_tag_reorder_routes,
    register_task_routes,
)
from .routes.tasks._shared import (  # noqa: F401  (既存 import 面を維持)
    CreateSpacePayload,
    CreateTagPayload,
    CreateTaskPayload,
    GoogleCalendarConnectPayload,
    GoogleCalendarSettingsPayload,
    LegacyEventPayload,
    NotificationSettingsPayload,
    OccurrenceUpdatePayload,
    ReorderTasksPayload,
    TaskCommentPayload,
    TaskRecurrencePayload,
    TaskReferencePayload,
    TaskRouterContext,
    TimeLogPayload,
    TimerStartPayload,
    TimerStopPayload,
    UpdateTaskPayload,
    UpdateTimeEntryPayload,
    UserNotificationPreferencesPayload,
    WebPushSubscriptionPayload,
    WebPushSubscriptionDeletePayload,
    _build_update_task_updates,
    _deep_merge_settings,
    _parse_datetime,
    _parse_wall_clock_datetime,
)
from .uuid_http import parse_uuid_or_400

logger = logging.getLogger(__name__)


def _translate_service_error(exc: TaskManagementError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


def create_task_router(
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
    broadcaster=None,
    workspace_root: "str | PathLike[str] | None" = None,
) -> APIRouter:
    """Create task management router with injected dependencies.

    Args:
        workspace_root: App/Project library root。``None`` なら
            ``AOITALK_WORKSPACES_DIR`` 由来の既定 root を使う。Space 削除は配下
            Project の library を消すため、ロック取得先と削除先が同じ root に
            なるよう、この値をそのまま ``TaskRouterContext`` へ透過する。
    """

    router = APIRouter(prefix="/api", tags=["tasks"])
    service = TaskManagementService(broadcaster=broadcaster)
    google_calendar = GoogleCalendarService()
    from ..tools.file_explorer import (
        get_root_dir as get_workspace_root,
        is_safe_workspace_path,
    )

    effective_workspace_root = (
        Path(workspace_root).expanduser().resolve()
        if workspace_root is not None
        else get_workspace_root().resolve()
    )
    effective_workspace_root.mkdir(parents=True, exist_ok=True)

    async def _get_current_user(request: Request) -> tuple[UUID, dict[str, Any]]:
        user_info = await get_user_from_request(request)
        if not user_info or "id" not in user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return UUID(user_info["id"]), user_info

    def _space_slug(name: str) -> str:
        slug = re.sub(r"[^\w\s-]", "", name.lower())
        slug = re.sub(r"[-\s]+", "-", slug).strip("-")
        return slug[:100] if slug else "space"

    def _sanitize_file_name(name: str) -> str:
        cleaned = re.sub(r"[/\\:*?\"<>|\x00-\x1f]", "", name).strip()
        cleaned = re.sub(r"^\.+$", "", cleaned)
        return cleaned[:180] or "uploaded-file"

    def _validate_project_relative_path(value: str) -> str:
        clean = (value or "").replace("\\", "/").strip("/")
        if not clean:
            return ""
        parts = PurePosixPath(clean).parts
        if any(part in {"..", ""} for part in parts):
            raise HTTPException(status_code=400, detail="Invalid project file path")
        return "/".join(parts)

    def _unique_target_path(target_dir: Path, file_name: str) -> Path:
        candidate = target_dir / file_name
        stem = candidate.stem
        suffix = candidate.suffix
        index = 1
        while True:
            try:
                candidate.lstat()
            except FileNotFoundError:
                return candidate
            except OSError:
                pass
            candidate = target_dir / f"{stem}-{index}{suffix}"
            index += 1

    def _write_unique_target_file(
        target_dir: Path, file_name: str, content: bytes
    ) -> Path:
        """Publish one attachment atomically under the project row lock."""
        target_dir.mkdir(parents=True, exist_ok=True)
        candidate = _unique_target_path(target_dir, file_name)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target_dir,
                prefix=".aoitalk-upload-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, candidate)
            temporary_path = None
            return candidate
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Failed to remove temporary attachment: %s", temporary_path)

    def _attachment_kind(mime_type: Optional[str], file_name: str) -> str:
        if mime_type and mime_type.startswith("image/"):
            return "image"
        return "image" if Path(file_name).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"} else "file"

    def _serialize_attachment(attachment: TaskAttachment) -> dict[str, Any]:
        data = attachment.to_dict()
        data["url"] = f"/api/tasks/{attachment.task_id}/attachments/{attachment.id}"
        return data

    async def _serialize_task_reference(
        session,
        reference: TaskReference,
        *,
        user_id: UUID,
        can_remove: bool,
    ) -> dict[str, Any]:
        data = reference.to_dict()
        data.update(
            {
                "subtitle": None,
                "can_remove": can_remove,
                "exists": True,
                "open": {
                    "id": reference.target_id,
                    "path": reference.target_path,
                    "url": reference.target_url,
                },
            }
        )
        if reference.reference_type in {"conversation_session", "conversation_message"}:
            conversation = None
            try:
                conversation_id = UUID(str(reference.target_id)) if reference.target_id else None
            except (TypeError, ValueError):
                conversation_id = None
            if conversation_id:
                result = await session.execute(
                    select(ConversationSession).where(
                        ConversationSession.id == conversation_id,
                        ConversationSession.deleted_at.is_(None),
                    )
                )
                candidate = result.scalar_one_or_none()
                if candidate and (
                    candidate.user_id == str(user_id)
                    or (
                        await session.execute(
                            select(ConversationParticipant.id).where(
                                ConversationParticipant.session_id == candidate.id,
                                ConversationParticipant.participant_type == "user",
                                ConversationParticipant.participant_id == str(user_id),
                                ConversationParticipant.status == "joined",
                            )
                        )
                    ).scalar_one_or_none()
                    is not None
                ):
                    conversation = candidate
            if conversation is None:
                data.update(
                    {
                        "display_name": reference.display_name or "参照先が見つかりません",
                        "subtitle": "参照先が見つかりません",
                        "exists": False,
                    }
                )
            else:
                metadata = reference.reference_metadata or {}
                message_id = metadata.get("message_id") or metadata.get("trigger_message_id")
                if reference.reference_type == "conversation_message" and message_id:
                    try:
                        message_uuid = UUID(str(message_id))
                    except (TypeError, ValueError):
                        message_uuid = None
                    message = (
                        await session.get(ConversationMessage, message_uuid)
                        if message_uuid
                        else None
                    )
                    if (
                        message is None
                        or message.session_id != conversation.id
                        or message.deleted_at is not None
                    ):
                        data.update(
                            {
                                "subtitle": "参照先が見つかりません",
                                "exists": False,
                            }
                        )
                    else:
                        data["subtitle"] = "発生元メッセージ"
                        data["open"] = {
                            "id": conversation.id,
                            "path": f"/chat?s={conversation.id}&message={quote(str(message.id))}",
                            "url": None,
                        }
                else:
                    data["subtitle"] = "チャットセッション"
                data["display_name"] = conversation.title or reference.display_name or "無題の会話"
        elif reference.reference_type == "docs_node":
            node = None
            try:
                node_id = UUID(str(reference.target_id)) if reference.target_id else None
            except (TypeError, ValueError):
                node_id = None
            if node_id:
                result = await session.execute(
                    select(KnowledgeNode, DocsLibrary.owner_user_id)
                    .join(DocsLibrary, KnowledgeNode.docs_library_id == DocsLibrary.id)
                    .where(
                        KnowledgeNode.id == node_id,
                        KnowledgeNode.archived_at.is_(None),
                    )
                )
                row = result.first()
                if row:
                    candidate, owner_id = row
                    if await can_read_node(session, candidate, user_id):
                        node = candidate
            if node is None:
                data.update({"subtitle": "参照先が見つかりません", "exists": False})
            else:
                data["display_name"] = node.title or reference.display_name or "Docsノード"
                data["subtitle"] = "Docs"
                data["open"] = {"id": node.id, "path": f"/docs/{node.id}", "url": None}
        elif reference.reference_type == "workspace_file":
            if not reference.target_path:
                data.update({"subtitle": "参照先が見つかりません", "exists": False})
            else:
                try:
                    _, root_path = await _project_storage_root(reference.project_id)
                    target = _resolve_attachment_file(root_path, reference.target_path)
                    exists = target.exists() and target.is_file()
                except (HTTPException, OSError):
                    exists = False
                data["exists"] = exists
                data["subtitle"] = "library" if exists else "参照先が見つかりません"
        elif reference.reference_type == "url":
            data["subtitle"] = "URL"
            data["exists"] = bool(reference.target_url)
        return data

    async def _load_task_for_attachment(session, *, user_id: UUID, task_id: str, permission: str) -> Task:
        task_uuid = parse_uuid_or_400(task_id, "task_id")
        result = await session.execute(select(Task).where(Task.id == task_uuid))
        task = result.scalar_one_or_none()
        if task is None or task.deleted_at is not None:
            raise TaskManagementError("Task not found", status_code=404)
        await service.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission=permission
        )
        return task

    async def _project_storage_root(project_id: UUID) -> tuple[str, Path]:
        storage_root = await ProjectRepository.get_storage_path(project_id)
        root_path = effective_workspace_root / storage_root
        if not is_safe_workspace_path(effective_workspace_root, root_path):
            raise HTTPException(status_code=400, detail="Invalid project storage path")
        root_path.mkdir(parents=True, exist_ok=True)
        if not is_safe_workspace_path(effective_workspace_root, root_path):
            raise HTTPException(status_code=400, detail="Invalid project storage path")
        return storage_root, root_path

    def _resolve_attachment_file(root_path: Path, file_path: str) -> Path:
        clean = _validate_project_relative_path(file_path)
        lexical_target = root_path / clean
        if not is_safe_workspace_path(root_path, lexical_target):
            raise HTTPException(status_code=400, detail="Invalid attachment path")
        return lexical_target.resolve()

    def _with_pending_agent_triage(metadata: Optional[dict[str, Any]]) -> dict[str, Any]:
        next_metadata = dict(metadata or {})
        next_metadata.setdefault("agent_triage_status", "pending")
        return next_metadata

    def _triage_task(task: Task) -> dict[str, Any]:
        text = f"{task.title}\n{task.description or ''}".strip()
        questions: list[str] = []
        investigation: list[str] = []
        execution: list[str] = []

        if len(text) < 24:
            questions.append("Please clarify the goal, target surface, and expected done state.")
        if re.search(r"irodori|tts|voice|watermark|gpu|checkpoint", text, re.I):
            investigation.append("Check external dependencies, GPU/VRAM, checkpoints, and audible watermark risk.")
        if re.search(r"mobile|expo|android|ios", text, re.I):
            execution.append("Review the mobile screen, API client, and offline/pending behavior.")
        if re.search(r"webui|frontend|next|browser|ui", text, re.I):
            execution.append("Update the web UI component and add focused regression checks.")
        if re.search(r"backend|api|db|database|migration", text, re.I):
            execution.append("Align backend API, DB model/migration, and permission checks.")
        if re.search(r"upload|file|attachment", text, re.I):
            execution.append("Implement storage path, UI refresh, and failure handling.")
        if re.search(r"bug|fix|overflow|position|failure|error", text, re.I):
            execution.append("Lock down reproduction and make the smallest compatible fix.")
        if not execution and not investigation:
            execution.append("Inspect the target code and turn the request into concrete implementation units.")

        status = "needs_user" if questions else "ready"
        summary_parts = [
            f"Goal: {task.title}",
            f"Investigation: {' / '.join(investigation)}" if investigation else None,
            f"Execution: {' / '.join(execution)}",
        ]
        summary = "\n".join(part for part in summary_parts if part)
        return {
            "status": status,
            "summary": summary,
            "questions": questions,
            "investigation": investigation,
            "execution": execution,
        }

    def _translate_google_calendar_error(
        exc: GoogleCalendarServiceError,
    ) -> HTTPException:
        return HTTPException(status_code=exc.status_code, detail=exc.message)

    # Space の ACL は Files API と同じ共有 policy を使う。各 callable を
    # Context へ詰めることで、既存の register モジュール／テストの注入面を
    # 変えずに既存 TaskRouterContext の挙動を維持する。
    _is_inbox_space = _space_is_inbox_space
    _is_admin_user = _space_is_admin_user
    _member_space_ids = _space_member_space_ids
    _load_space = _space_load_space
    _get_readable_space = _space_get_readable_space
    _can_write_space = _space_can_write_space

    async def _load_task_for_google_calendar(
        session, *, user_id: UUID, task_id: str
    ) -> dict[str, Any]:
        task_uuid = parse_uuid_or_400(task_id, "task_id")
        result = await session.execute(select(Task).where(Task.id == task_uuid))
        task = result.scalar_one_or_none()
        if task is None or task.deleted_at is not None:
            raise TaskManagementError("Task not found", status_code=404)
        await service.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="read"
        )
        return {
            "id": str(task.id),
            "project_id": str(task.project_id),
            "title": task.title,
            "description": task.description,
            "status": task.status,
            "priority": task.priority,
            "start_at": task.start_at.isoformat() if task.start_at else None,
            "end_at": task.end_at.isoformat() if task.end_at else None,
            "all_day": task.all_day,
            "reminder_offsets": task.reminder_offsets or [],
            "notifications_enabled": task.notifications_enabled,
            "metadata": task.task_metadata or {},
        }

    async def _sync_google_calendar_warning_only(
        session,
        *,
        user_id: UUID,
        task_id: str,
    ) -> dict[str, Any]:
        try:
            task = await _load_task_for_google_calendar(
                session, user_id=user_id, task_id=task_id
            )
            return await google_calendar.auto_sync_event_for_task(
                session, user_id=user_id, task=task
            )
        except GoogleCalendarServiceError as exc:
            logger.warning(
                "Google Calendar auto sync failed for task %s: %s",
                task_id,
                exc.message,
            )
            return {"status": "warning", "message": exc.message}
        except Exception as exc:
            logger.exception("Unexpected Google Calendar auto sync failure")
            return {"status": "warning", "message": str(exc)}

    async def _delete_google_calendar_warning_only(
        session,
        *,
        user_id: UUID,
        task_id: str,
    ) -> dict[str, Any]:
        try:
            task = await _load_task_for_google_calendar(
                session, user_id=user_id, task_id=task_id
            )
            return await google_calendar.delete_auto_event_for_task(
                session, user_id=user_id, task=task
            )
        except GoogleCalendarServiceError as exc:
            logger.warning(
                "Google Calendar auto event delete failed for task %s: %s",
                task_id,
                exc.message,
            )
            return {"status": "warning", "message": exc.message}
        except Exception as exc:
            logger.exception("Unexpected Google Calendar auto event delete failure")
            return {"status": "warning", "message": str(exc)}

    async def _load_tag(session, tag_id: str) -> Tag:
        parsed = parse_uuid_or_400(tag_id, "tag_id")
        result = await session.execute(select(Tag).where(Tag.id == parsed))
        tag = result.scalar_one_or_none()
        if tag is None:
            raise HTTPException(status_code=404, detail="タグが見つかりません")
        return tag

    ctx = TaskRouterContext(
        get_db_manager=get_db_manager,
        get_user_from_request=get_user_from_request,
        require_auth_dependency=require_auth_dependency,
        service=service,
        google_calendar=google_calendar,
        get_current_user=_get_current_user,
        space_slug=_space_slug,
        sanitize_file_name=_sanitize_file_name,
        validate_project_relative_path=_validate_project_relative_path,
        unique_target_path=_unique_target_path,
        write_unique_target_file=_write_unique_target_file,
        attachment_kind=_attachment_kind,
        serialize_attachment=_serialize_attachment,
        serialize_task_reference=_serialize_task_reference,
        load_task_for_attachment=_load_task_for_attachment,
        project_storage_root=_project_storage_root,
        resolve_attachment_file=_resolve_attachment_file,
        is_safe_workspace_path=is_safe_workspace_path,
        with_pending_agent_triage=_with_pending_agent_triage,
        triage_task=_triage_task,
        translate_google_calendar_error=_translate_google_calendar_error,
        is_inbox_space=_is_inbox_space,
        is_admin_user=_is_admin_user,
        member_space_ids=_member_space_ids,
        load_space=_load_space,
        get_readable_space=_get_readable_space,
        can_write_space=_can_write_space,
        load_task_for_google_calendar=_load_task_for_google_calendar,
        sync_google_calendar_warning_only=_sync_google_calendar_warning_only,
        delete_google_calendar_warning_only=_delete_google_calendar_warning_only,
        load_tag=_load_tag,
        translate_service_error=_translate_service_error,
        workspace_root=workspace_root,
    )

    register_space_routes(router, ctx)
    register_task_routes(router, ctx)
    register_attachment_routes(router, ctx)
    register_occurrence_time_routes(router, ctx)
    register_notification_routes(router, ctx)
    register_google_calendar_routes(router, ctx)
    register_tag_reorder_routes(router, ctx)

    return router
