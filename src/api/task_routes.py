"""Task, calendar, timer, report, and notification API routes."""

from __future__ import annotations

import logging
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Optional
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..memory.models import (
    Project,
    ProjectMember,
    Space,
    Tag,
    Task,
    TaskAttachment,
    TaskComment,
    TaskRecurrenceRule,
)
from ..memory.project_repository import ProjectRepository
from ..memory.user_repository import UserRepository
from ..services.google_calendar_service import (
    GoogleCalendarService,
    GoogleCalendarServiceError,
)
from ..services.task_management_service import (
    TaskManagementError,
    TaskManagementService,
    normalize_task_status,
)
from ..task_time import DEFAULT_TASK_TIMEZONE, normalize_task_timezone

logger = logging.getLogger(__name__)


class CreateTaskPayload(BaseModel):
    project_id: Optional[str] = None
    knowledge_node_id: Optional[str] = None
    title: str
    description: Optional[str] = None
    status: str = "todo"
    priority: Optional[str] = None
    start_at: Optional[str] = None
    end_at: Optional[str] = None
    all_day: bool = False
    reminder_offsets: Optional[list[int]] = None
    notifications_enabled: Optional[bool] = None
    source: str = "local"
    assignee_ids: list[str] = Field(default_factory=list)
    tag_ids: list[str] = Field(default_factory=list)
    recurrence_rrule: Optional[str] = None
    recurrence_timezone: str = DEFAULT_TASK_TIMEZONE
    task_metadata: Optional[dict[str, Any]] = None


class UpdateTaskPayload(BaseModel):
    project_id: Optional[str] = None
    knowledge_node_id: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    start_at: Optional[str] = None
    end_at: Optional[str] = None
    all_day: Optional[bool] = None
    reminder_offsets: Optional[list[int]] = None
    notifications_enabled: Optional[bool] = None
    assignee_ids: Optional[list[str]] = None
    tag_ids: Optional[list[str]] = None
    recurrence_rrule: Optional[str] = None
    recurrence_timezone: Optional[str] = None
    task_metadata: Optional[dict[str, Any]] = None


class CreateTagPayload(BaseModel):
    name: str
    color: Optional[str] = None


class ReorderTasksPayload(BaseModel):
    task_ids: list[str]


class TaskCommentPayload(BaseModel):
    content: str


class OccurrenceUpdatePayload(BaseModel):
    status: Optional[str] = None
    start_at: Optional[str] = None
    end_at: Optional[str] = None
    reminder_offsets: Optional[list[int]] = None


class TaskRecurrencePayload(BaseModel):
    rrule: Optional[str] = None
    timezone: Optional[str] = None
    horizon_days: Optional[int] = None
    trigger_status: Optional[str] = None
    create_new: Optional[bool] = None
    recur_forever: Optional[bool] = None
    reset_status_to: Optional[str] = None
    end_count: Optional[int] = None
    end_date: Optional[str] = None
    skip_weekend: Optional[bool] = None
    skip_holiday: Optional[bool] = None


class TimerStartPayload(BaseModel):
    task_id: str
    occurrence_id: Optional[str] = None
    note: Optional[str] = None
    # Web BFF の /api/time-entries/start はタイマー起動なので "timer" を既定にする
    # （モバイルは明示的に source="mobile" を送る）。
    source: str = "timer"


class TimerStopPayload(BaseModel):
    time_entry_id: Optional[str] = None


class UpdateTimeEntryPayload(BaseModel):
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    note: Optional[str] = None


class TimeLogPayload(BaseModel):
    task_id: str
    occurrence_id: Optional[str] = None
    started_at: str
    ended_at: str
    note: Optional[str] = None
    source: str = "manual"


class NotificationSettingsPayload(BaseModel):
    discord_webhook_url: Optional[str] = None
    default_reminder_offsets: Optional[list[int]] = None
    notify_overdue: Optional[bool] = None


class UserNotificationPreferencesPayload(BaseModel):
    task_notification_minutes_before: Optional[int] = None
    task_notifications_default_enabled: Optional[bool] = None


class CreateSpacePayload(BaseModel):
    name: str
    description: Optional[str] = None
    color: Optional[str] = None
    sort_order: Optional[float] = 0


class GoogleCalendarConnectPayload(BaseModel):
    platform: Literal["web", "mobile"] = "web"
    mobile_redirect_uri: Optional[str] = None


class GoogleCalendarSettingsPayload(BaseModel):
    default_action: Optional[Literal["open_template", "create_event"]] = None
    default_event_reminder_minutes: Optional[int] = None


class LegacyEventPayload(BaseModel):
    event_type: str
    trigger_source: str = "manual"
    payload: Optional[dict[str, Any]] = None


def _parse_uuid(value: Optional[str], field_name: str) -> Optional[UUID]:
    if value in (None, ""):
        return None
    try:
        return UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}") from exc


def _parse_datetime(value: Optional[str], field_name: str) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}") from exc


def _parse_wall_clock_datetime(
    value: Optional[str], field_name: str
) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1]
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return parsed
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}") from exc


def _build_update_task_updates(payload: UpdateTaskPayload) -> dict[str, Any]:
    fields_set = payload.model_fields_set
    updates: dict[str, Any] = {}
    if "project_id" in fields_set:
        updates["project_id"] = (
            _parse_uuid(payload.project_id, "project_id")
            if payload.project_id is not None
            else None
        )
    if "knowledge_node_id" in fields_set:
        updates["knowledge_node_id"] = (
            _parse_uuid(payload.knowledge_node_id, "knowledge_node_id")
            if payload.knowledge_node_id is not None
            else None
        )
    if "title" in fields_set:
        updates["title"] = payload.title
    if "description" in fields_set:
        updates["description"] = payload.description
    if "status" in fields_set:
        updates["status"] = payload.status
    if "priority" in fields_set:
        updates["priority"] = payload.priority
    if "start_at" in fields_set:
        updates["start_at"] = (
            _parse_wall_clock_datetime(payload.start_at, "start_at")
            if payload.start_at is not None
            else None
        )
    if "end_at" in fields_set:
        updates["end_at"] = (
            _parse_wall_clock_datetime(payload.end_at, "end_at")
            if payload.end_at is not None
            else None
        )
    if "all_day" in fields_set:
        updates["all_day"] = payload.all_day
    if "reminder_offsets" in fields_set:
        updates["reminder_offsets"] = payload.reminder_offsets
    if "notifications_enabled" in fields_set:
        updates["notifications_enabled"] = payload.notifications_enabled
    if "assignee_ids" in fields_set:
        updates["assignee_ids"] = (
            [_parse_uuid(value, "assignee_id") for value in payload.assignee_ids]
            if payload.assignee_ids is not None
            else None
        )
    if "tag_ids" in fields_set:
        updates["tag_ids"] = (
            [_parse_uuid(value, "tag_id") for value in payload.tag_ids]
            if payload.tag_ids is not None
            else None
        )
    if "recurrence_rrule" in fields_set:
        updates["recurrence_rrule"] = payload.recurrence_rrule
    if "recurrence_timezone" in fields_set:
        updates["recurrence_timezone"] = payload.recurrence_timezone
    if "task_metadata" in fields_set:
        updates["task_metadata"] = payload.task_metadata
    return updates


def _translate_service_error(exc: TaskManagementError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


def _deep_merge_settings(
    current: dict[str, Any], patch: dict[str, Any]
) -> dict[str, Any]:
    """Web BFF の mergeUserSettings と同じ再帰マージ（dict 同士のみ深くマージ）。"""
    merged = dict(current)
    for key, value in patch.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge_settings(existing, value)
        else:
            merged[key] = value
    return merged


def create_task_router(
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
    broadcaster=None,
) -> APIRouter:
    """Create task management router with injected dependencies."""

    router = APIRouter(prefix="/api", tags=["tasks"])
    service = TaskManagementService(broadcaster=broadcaster)
    google_calendar = GoogleCalendarService()
    from ..tools.file_explorer import get_root_dir as get_workspace_root

    blocked_attachment_extensions = {
        ".exe",
        ".bat",
        ".cmd",
        ".sh",
        ".ps1",
        ".vbs",
        ".scr",
        ".com",
    }

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
        while candidate.exists():
            candidate = target_dir / f"{stem}-{index}{suffix}"
            index += 1
        return candidate

    def _attachment_kind(mime_type: Optional[str], file_name: str) -> str:
        if mime_type and mime_type.startswith("image/"):
            return "image"
        return "image" if Path(file_name).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"} else "file"

    def _serialize_attachment(attachment: TaskAttachment) -> dict[str, Any]:
        data = attachment.to_dict()
        data["url"] = f"/api/tasks/{attachment.task_id}/attachments/{attachment.id}"
        return data

    async def _load_task_for_attachment(session, *, user_id: UUID, task_id: str, permission: str) -> Task:
        task_uuid = _parse_uuid(task_id, "task_id")
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
        root_path = get_workspace_root() / storage_root
        root_path.mkdir(parents=True, exist_ok=True)
        return storage_root, root_path

    def _resolve_attachment_file(root_path: Path, file_path: str) -> Path:
        clean = _validate_project_relative_path(file_path)
        target = (root_path / clean).resolve()
        root_resolved = root_path.resolve()
        if root_resolved not in target.parents and target != root_resolved:
            raise HTTPException(status_code=400, detail="Invalid attachment path")
        return target

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
            execution.append("Implement storage path, blocked-extension handling, UI refresh, and failure handling.")
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

    def _is_inbox_space(space: Space) -> bool:
        """ユーザー専用 Inbox スペースかどうか（Web BFF の isInboxSpace と同期）。"""
        return space.slug == f"inbox-{space.owner_id}"

    def _is_admin_user(user_info: dict[str, Any]) -> bool:
        return str(user_info.get("role") or "") == "admin"

    async def _member_space_ids(session, user_id: UUID) -> set[UUID]:
        result = await session.execute(
            select(Project.space_id)
            .join(ProjectMember, ProjectMember.project_id == Project.id)
            .where(
                ProjectMember.user_id == user_id,
                Project.space_id.isnot(None),
            )
        )
        return set(result.scalars().all())

    async def _load_space(session, space_id: str) -> Optional[Space]:
        parsed = _parse_uuid(space_id, "space_id")
        result = await session.execute(select(Space).where(Space.id == parsed))
        return result.scalar_one_or_none()

    async def _get_readable_space(
        session, *, space_id: str, user_id: UUID, user_info: dict[str, Any]
    ) -> Optional[Space]:
        space = await _load_space(session, space_id)
        if space is None:
            return None
        if _is_inbox_space(space):
            return space if space.owner_id == user_id else None
        if space.owner_id == user_id or _is_admin_user(user_info):
            return space
        result = await session.execute(
            select(ProjectMember.project_id)
            .join(Project, Project.id == ProjectMember.project_id)
            .where(
                ProjectMember.user_id == user_id,
                Project.space_id == space.id,
            )
            .limit(1)
        )
        return space if result.scalar_one_or_none() is not None else None

    async def _can_write_space(
        session, *, space_id: str, user_id: UUID, user_info: dict[str, Any]
    ) -> tuple[bool, Optional[Space]]:
        """スペースへの書き込み可否（Web BFF の canWriteSpace と同期）。"""
        space = await _load_space(session, space_id)
        if space is None:
            return False, None
        if _is_inbox_space(space):
            return space.owner_id == user_id, space
        return space.owner_id == user_id or _is_admin_user(user_info), space

    @router.get("/spaces")
    async def list_spaces(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, user_info = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            await ProjectRepository.ensure_user_inbox_setup(session, user_id)
            await session.commit()
            member_space_ids = await _member_space_ids(session, user_id)
            is_admin = _is_admin_user(user_info)
            result = await session.execute(
                select(Space).order_by(Space.sort_order.asc(), Space.created_at.asc())
            )
            spaces = []
            for space in result.scalars().all():
                if _is_inbox_space(space):
                    if space.owner_id == user_id:
                        spaces.append(space.to_dict())
                elif (
                    space.owner_id == user_id
                    or is_admin
                    or space.id in member_space_ids
                ):
                    spaces.append(space.to_dict())
            return {"spaces": spaces, "total": len(spaces)}
        finally:
            await session.close()

    @router.post("/spaces")
    async def create_space(
        payload: CreateSpacePayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        session = await get_db_manager().get_session()
        try:
            base_slug = _space_slug(name)
            slug = base_slug
            counter = 1
            while True:
                existing = await session.execute(
                    select(Space.id).where(Space.owner_id == user_id, Space.slug == slug)
                )
                if not existing.scalar_one_or_none():
                    break
                counter += 1
                slug = f"{base_slug}-{counter}"
            space = Space(
                name=name,
                slug=slug,
                description=payload.description,
                color=payload.color,
                owner_id=user_id,
                sort_order=payload.sort_order or 0,
            )
            session.add(space)
            await session.commit()
            await session.refresh(space)
            return {"success": True, "space": space.to_dict()}
        finally:
            await session.close()

    @router.get("/spaces/{space_id}")
    async def get_space(
        space_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, user_info = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            space = await _get_readable_space(
                session, space_id=space_id, user_id=user_id, user_info=user_info
            )
            if space is None:
                raise HTTPException(
                    status_code=404, detail="スペースが見つかりません"
                )
            return space.to_dict()
        finally:
            await session.close()

    @router.patch("/spaces/{space_id}")
    async def update_space(
        space_id: str,
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
            allowed, space = await _can_write_space(
                session, space_id=space_id, user_id=user_id, user_info=user_info
            )
            if space is None:
                raise HTTPException(
                    status_code=404, detail="スペースが見つかりません"
                )
            if not allowed:
                raise HTTPException(status_code=403, detail="権限がありません")

            if body.get("name") is not None:
                space.name = str(body["name"])
            if "description" in body:
                space.description = body["description"]
            if "color" in body:
                space.color = body["color"]
            if body.get("sort_order") is not None:
                space.sort_order = float(body["sort_order"])
            space.updated_at = datetime.utcnow()

            await session.commit()
            await session.refresh(space)
            return {"success": True, "space": space.to_dict()}
        finally:
            await session.close()

    @router.delete("/spaces/{space_id}")
    async def delete_space(
        space_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, user_info = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            allowed, space = await _can_write_space(
                session, space_id=space_id, user_id=user_id, user_info=user_info
            )
            if space is None:
                raise HTTPException(
                    status_code=404, detail="スペースが見つかりません"
                )
            if not allowed:
                raise HTTPException(status_code=403, detail="権限がありません")
            if _is_inbox_space(space):
                raise HTTPException(
                    status_code=400, detail="Inboxスペースは削除できません"
                )

            deleted_project_count = await ProjectRepository.delete_projects_in_space(
                session,
                space.id,
                delete_workspaces=True,
            )
            await session.delete(space)
            await session.commit()
            return {"success": True, "deleted_project_count": deleted_project_count}
        finally:
            await session.close()

    @router.get("/spaces/{space_id}/tags")
    async def list_space_tags(
        space_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, user_info = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            space = await _get_readable_space(
                session, space_id=space_id, user_id=user_id, user_info=user_info
            )
            if space is None:
                raise HTTPException(
                    status_code=404, detail="スペースが見つかりません"
                )
            result = await session.execute(
                select(Tag).where(Tag.space_id == space.id).order_by(Tag.name)
            )
            return [tag.to_dict() for tag in result.scalars().all()]
        finally:
            await session.close()

    @router.post("/spaces/{space_id}/tags")
    async def create_space_tag(
        space_id: str,
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
        name = str(body.get("name") or "").strip()
        color_raw = body.get("color")
        color = (
            color_raw.strip()
            if isinstance(color_raw, str) and color_raw.strip()
            else None
        )

        session = await get_db_manager().get_session()
        try:
            allowed, space = await _can_write_space(
                session, space_id=space_id, user_id=user_id, user_info=user_info
            )
            if space is None:
                raise HTTPException(
                    status_code=404, detail="スペースが見つかりません"
                )
            if not allowed:
                raise HTTPException(status_code=403, detail="権限がありません")
            if not name:
                raise HTTPException(status_code=400, detail="nameは必須です")

            existing = await session.execute(
                select(Tag).where(Tag.space_id == space.id, Tag.name == name)
            )
            tag = existing.scalar_one_or_none()
            if tag is not None:
                return tag.to_dict()

            tag = Tag(space_id=space.id, name=name, color=color, created_by=user_id)
            session.add(tag)
            await session.commit()
            await session.refresh(tag)
            return tag.to_dict()
        finally:
            await session.close()

    async def _load_task_for_google_calendar(
        session, *, user_id: UUID, task_id: str
    ) -> dict[str, Any]:
        task_uuid = _parse_uuid(task_id, "task_id")
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
                project_id=_parse_uuid(payload.project_id, "project_id"),
                knowledge_node_id=_parse_uuid(payload.knowledge_node_id, "knowledge_node_id"),
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
                source=payload.source,
                assignee_ids=[
                    _parse_uuid(value, "assignee_id") for value in payload.assignee_ids
                ],
                tag_ids=[_parse_uuid(value, "tag_id") for value in payload.tag_ids],
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
                project_id=_parse_uuid(
                    request.query_params.get("project_id"), "project_id"
                ),
                space_id=_parse_uuid(
                    request.query_params.get("space_id"), "space_id"
                ),
                status=request.query_params.get("status"),
                assignee_id=_parse_uuid(
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
                task_id=_parse_uuid(task_id, "task_id"),
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
                task_id=_parse_uuid(task_id, "task_id"),
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
                task_id=_parse_uuid(task_id, "task_id"),
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

    @router.get("/tasks/{task_id}/recurrence")
    async def get_task_recurrence(
        task_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            task = await service._load_task(session, _parse_uuid(task_id, "task_id"))
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
            task = await service._load_task(session, _parse_uuid(task_id, "task_id"))
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
            task = await service._load_task(session, _parse_uuid(task_id, "task_id"))
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
                task_id=_parse_uuid(task_id, "task_id"),
                content=payload.content,
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

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
                from ..services.failure_recorder import record_failure_event

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
                from ..services.failure_recorder import record_failure_event

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
                    TaskAttachment.id == _parse_uuid(attachment_id, "attachment_id"),
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
                    TaskAttachment.id == _parse_uuid(attachment_id, "attachment_id"),
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

    @router.get("/task-occurrences")
    async def list_occurrences(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.list_occurrences(
                session,
                user_id=user_id,
                project_id=_parse_uuid(
                    request.query_params.get("project_id"), "project_id"
                ),
                space_id=_parse_uuid(
                    request.query_params.get("space_id"), "space_id"
                ),
                start_from=_parse_datetime(
                    request.query_params.get("start_from"), "start_from"
                ),
                end_to=_parse_datetime(request.query_params.get("end_to"), "end_to"),
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.patch("/task-occurrences/{occurrence_id}")
    async def update_occurrence(
        occurrence_id: str,
        payload: OccurrenceUpdatePayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.update_occurrence(
                session,
                user_id=user_id,
                occurrence_id=_parse_uuid(occurrence_id, "occurrence_id"),
                updates={
                    "status": payload.status,
                    "start_at": (
                        _parse_wall_clock_datetime(payload.start_at, "start_at")
                        if payload.start_at is not None
                        else None
                    ),
                    "end_at": (
                        _parse_wall_clock_datetime(payload.end_at, "end_at")
                        if payload.end_at is not None
                        else None
                    ),
                    "reminder_offsets": payload.reminder_offsets,
                },
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.get("/time-entries")
    async def list_time_entries(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.list_time_entries(
                session,
                user_id=user_id,
                project_id=_parse_uuid(
                    request.query_params.get("project_id"), "project_id"
                ),
                space_id=_parse_uuid(
                    request.query_params.get("space_id"), "space_id"
                ),
                task_id=_parse_uuid(request.query_params.get("task_id"), "task_id"),
                active_only=request.query_params.get("active_only", "false").lower()
                in {"1", "true", "yes"},
                # DB はローカル壁時計時刻保存のため、Web BFF と同じ壁時計解釈で受ける
                date_from=_parse_wall_clock_datetime(
                    request.query_params.get("date_from"), "date_from"
                ),
                date_to=_parse_wall_clock_datetime(
                    request.query_params.get("date_to"), "date_to"
                ),
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.get("/time-entries/active")
    async def get_active_time_entry(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.get_active_time_entry(session, user_id=user_id)
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.post("/time-entries/start")
    async def start_timer(
        payload: TimerStartPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.start_timer(
                session,
                user_id=user_id,
                task_id=_parse_uuid(payload.task_id, "task_id"),
                occurrence_id=_parse_uuid(payload.occurrence_id, "occurrence_id"),
                source=payload.source,
                note=payload.note,
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.post("/time-entries/stop")
    async def stop_timer(
        payload: TimerStopPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.stop_timer(
                session,
                user_id=user_id,
                time_entry_id=_parse_uuid(payload.time_entry_id, "time_entry_id"),
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.patch("/time-entries/{entry_id}")
    async def update_time_entry(
        entry_id: str,
        payload: UpdateTimeEntryPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.update_time_entry(
                session,
                user_id=user_id,
                entry_id=_parse_uuid(entry_id, "entry_id"),
                started_at=(
                    _parse_wall_clock_datetime(payload.started_at, "started_at")
                    if payload.started_at is not None
                    else None
                ),
                ended_at=(
                    _parse_wall_clock_datetime(payload.ended_at, "ended_at")
                    if payload.ended_at is not None
                    else None
                ),
                note=payload.note,
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.delete("/time-entries/{entry_id}", status_code=204)
    async def delete_time_entry(
        entry_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            await service.delete_time_entry(
                session,
                user_id=user_id,
                entry_id=_parse_uuid(entry_id, "entry_id"),
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.post("/time-entries/log")
    async def log_time(
        payload: TimeLogPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.log_time(
                session,
                user_id=user_id,
                task_id=_parse_uuid(payload.task_id, "task_id"),
                occurrence_id=_parse_uuid(payload.occurrence_id, "occurrence_id"),
                started_at=_parse_wall_clock_datetime(payload.started_at, "started_at"),
                ended_at=_parse_wall_clock_datetime(payload.ended_at, "ended_at"),
                note=payload.note,
                source=payload.source,
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.get("/reports/time")
    async def get_time_report(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.get_time_report(
                session,
                user_id=user_id,
                project_id=_parse_uuid(
                    request.query_params.get("project_id"), "project_id"
                ),
                space_id=_parse_uuid(
                    request.query_params.get("space_id"), "space_id"
                ),
                date_from=_parse_wall_clock_datetime(
                    request.query_params.get("date_from"), "date_from"
                ),
                date_to=_parse_wall_clock_datetime(
                    request.query_params.get("date_to"), "date_to"
                ),
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.get("/notifications")
    async def list_notifications(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.list_notifications(
                session,
                user_id=user_id,
                unread_only=request.query_params.get("unread_only", "false").lower()
                in {"1", "true", "yes"},
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.get("/users/me/notification-preferences")
    async def get_user_notification_preferences(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.get_user_notification_preferences(
                session, user_id=user_id
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.patch("/users/me/notification-preferences")
    async def update_user_notification_preferences(
        payload: UserNotificationPreferencesPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.update_user_notification_preferences(
                session,
                user_id=user_id,
                task_notification_minutes_before=payload.task_notification_minutes_before,
                task_notifications_default_enabled=payload.task_notifications_default_enabled,
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.get("/users/me/settings")
    async def get_user_settings(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            user = await UserRepository.get_by_id(session, user_id)
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            return {"settings": user.user_settings or {}}
        finally:
            await session.close()

    @router.patch("/users/me/settings")
    async def update_user_settings(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        updates = body if isinstance(body, dict) else {}

        session = await get_db_manager().get_session()
        try:
            user = await UserRepository.get_by_id(session, user_id)
            if not user:
                raise HTTPException(status_code=404, detail="User not found")

            merged = _deep_merge_settings(dict(user.user_settings or {}), updates)
            await UserRepository.update_user(
                session=session,
                user_id=user_id,
                user_settings=merged,
            )
            return {"settings": merged}
        finally:
            await session.close()

    @router.get("/google-calendar/settings")
    async def get_google_calendar_settings(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await google_calendar.get_settings(session, user_id)
        finally:
            await session.close()

    @router.patch("/google-calendar/settings")
    async def update_google_calendar_settings(
        payload: GoogleCalendarSettingsPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await google_calendar.update_settings(
                session,
                user_id,
                default_action=payload.default_action,
                default_event_reminder_minutes=payload.default_event_reminder_minutes,
            )
        except GoogleCalendarServiceError as exc:
            raise _translate_google_calendar_error(exc)
        finally:
            await session.close()

    @router.post("/google-calendar/connect")
    async def start_google_calendar_connect(
        payload: GoogleCalendarConnectPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, user_info = await _get_current_user(request)
        try:
            authorization_url = await google_calendar.build_authorization_url(
                user_id=user_id,
                username=str(user_info.get("username") or ""),
                platform=payload.platform,
                mobile_redirect_uri=payload.mobile_redirect_uri,
            )
            return {"authorization_url": authorization_url}
        except GoogleCalendarServiceError as exc:
            raise _translate_google_calendar_error(exc)

    @router.get("/google-calendar/oauth/callback")
    async def google_calendar_oauth_callback(
        code: Optional[str] = None,
        state: Optional[str] = None,
        error: Optional[str] = None,
    ):
        session = await get_db_manager().get_session()
        try:
            result = await google_calendar.handle_callback(
                session,
                code=code,
                state=state,
                error=error,
            )
        except GoogleCalendarServiceError as exc:
            html = google_calendar.render_web_callback_html(
                success=False, error=exc.message
            )
            return HTMLResponse(html, status_code=exc.status_code)
        finally:
            await session.close()

        if result.platform == "mobile":
            target = google_calendar.build_mobile_redirect_uri(
                result.mobile_redirect_uri,
                "connected" if result.success else "error",
                result.error,
            )
            return RedirectResponse(target, status_code=302)

        html = google_calendar.render_web_callback_html(
            success=result.success,
            email=result.email,
            error=result.error,
        )
        return HTMLResponse(html)

    @router.post("/google-calendar/disconnect")
    async def disconnect_google_calendar(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            await google_calendar.disconnect(session, user_id)
            return await google_calendar.get_settings(session, user_id)
        finally:
            await session.close()

    @router.post("/tasks/{task_id}/google-calendar-event")
    async def create_google_calendar_event(
        task_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            task = await _load_task_for_google_calendar(
                session, user_id=user_id, task_id=task_id
            )
            return await google_calendar.create_event_for_task(
                session, user_id=user_id, task=task
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        except GoogleCalendarServiceError as exc:
            raise _translate_google_calendar_error(exc)
        finally:
            await session.close()

    @router.post("/tasks/{task_id}/google-calendar-auto-sync")
    async def auto_sync_google_calendar_event(
        task_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await _sync_google_calendar_warning_only(
                session, user_id=user_id, task_id=task_id
            )
        finally:
            await session.close()

    @router.post("/tasks/{task_id}/google-calendar-auto-delete")
    async def delete_auto_google_calendar_event(
        task_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await _delete_google_calendar_warning_only(
                session, user_id=user_id, task_id=task_id
            )
        finally:
            await session.close()

    @router.post("/notifications/{notification_id}/read")
    async def mark_notification_read(
        notification_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.mark_notification_read(
                session,
                user_id=user_id,
                notification_id=_parse_uuid(notification_id, "notification_id"),
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.post("/notifications/read-all")
    async def mark_all_notifications_read(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            count = await service.mark_all_notifications_read(
                session, user_id=user_id
            )
            return {"success": True, "count": count}
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.get("/projects/{project_id}/notification-settings")
    async def get_notification_settings(
        project_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            parsed_project_id = _parse_uuid(project_id, "project_id")
            await service.require_project_permission(
                session,
                project_id=parsed_project_id,
                user_id=user_id,
                permission="read",
            )
            setting = await service.get_or_create_notification_setting(
                session, project_id=parsed_project_id
            )
            return setting.to_dict()
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.patch("/projects/{project_id}/notification-settings")
    async def update_notification_settings(
        project_id: str,
        payload: NotificationSettingsPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.update_notification_setting(
                session,
                user_id=user_id,
                project_id=_parse_uuid(project_id, "project_id"),
                discord_webhook_url=payload.discord_webhook_url,
                default_reminder_offsets=payload.default_reminder_offsets,
                notify_overdue=payload.notify_overdue,
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

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
                project_id=_parse_uuid(project_id, "project_id"),
                task_ids=[_parse_uuid(tid, "task_id") for tid in payload.task_ids],
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
                task_ids=[_parse_uuid(tid, "task_id") for tid in payload.task_ids],
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
                project_id=_parse_uuid(project_id, "project_id"),
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
                project_id=_parse_uuid(project_id, "project_id"),
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

    async def _load_tag(session, tag_id: str) -> Tag:
        parsed = _parse_uuid(tag_id, "tag_id")
        result = await session.execute(select(Tag).where(Tag.id == parsed))
        tag = result.scalar_one_or_none()
        if tag is None:
            raise HTTPException(status_code=404, detail="タグが見つかりません")
        return tag

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
        task_uuid = _parse_uuid(task_id, "task_id")
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

    return router
