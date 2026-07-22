"""Task ルーター分割で共有する payload・ヘルパー・実行時コンテキスト定義。

`create_task_router`（src/api/task_routes.py）が依存注入とクロージャ生成を行い、
生成した service / helper 群を `TaskRouterContext` に詰めて各 register 関数へ渡す。
各 register モジュールはコンテキストからローカル名へ束ね直すことで、
エンドポイント本文を元実装と同一に保つ。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field

from ....task_time import DEFAULT_TASK_TIMEZONE
from ...uuid_http import parse_uuid_or_400


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
    estimated_hours: Optional[float] = None
    parent_task_id: Optional[str] = None
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
    estimated_hours: Optional[float] = None
    parent_task_id: Optional[str] = None
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


class TaskReferencePayload(BaseModel):
    reference_type: Literal[
        "conversation_session",
        "conversation_message",
        "docs_node",
        "workspace_file",
        "url",
    ]
    relation_type: Literal["source", "related"] = "related"
    target_id: Optional[str] = None
    target_path: Optional[str] = None
    target_url: Optional[str] = None
    display_name: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


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
            parse_uuid_or_400(payload.project_id, "project_id")
            if payload.project_id is not None
            else None
        )
    if "knowledge_node_id" in fields_set:
        updates["knowledge_node_id"] = (
            parse_uuid_or_400(payload.knowledge_node_id, "knowledge_node_id")
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
    if "estimated_hours" in fields_set:
        updates["estimated_hours"] = payload.estimated_hours
    if "parent_task_id" in fields_set:
        updates["parent_task_id"] = (
            parse_uuid_or_400(payload.parent_task_id, "parent_task_id")
            if payload.parent_task_id is not None
            else None
        )
    if "assignee_ids" in fields_set:
        updates["assignee_ids"] = (
            [parse_uuid_or_400(value, "assignee_id") for value in payload.assignee_ids]
            if payload.assignee_ids is not None
            else None
        )
    if "tag_ids" in fields_set:
        updates["tag_ids"] = (
            [parse_uuid_or_400(value, "tag_id") for value in payload.tag_ids]
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


@dataclass
class TaskRouterContext:
    """`create_task_router` が生成した実行時依存とクロージャ helper 群の集約。

    各 register モジュールはこのコンテキストからローカル名へ束ね直して使う。
    """

    # 注入された依存
    get_db_manager: Callable
    get_user_from_request: Callable
    require_auth_dependency: Any
    service: Any
    google_calendar: Any
    blocked_attachment_extensions: set

    # クロージャ helper 群
    get_current_user: Callable
    space_slug: Callable
    sanitize_file_name: Callable
    validate_project_relative_path: Callable
    unique_target_path: Callable
    attachment_kind: Callable
    serialize_attachment: Callable
    serialize_task_reference: Callable
    load_task_for_attachment: Callable
    project_storage_root: Callable
    resolve_attachment_file: Callable
    with_pending_agent_triage: Callable
    triage_task: Callable
    translate_google_calendar_error: Callable
    is_inbox_space: Callable
    is_admin_user: Callable
    member_space_ids: Callable
    load_space: Callable
    get_readable_space: Callable
    can_write_space: Callable
    load_task_for_google_calendar: Callable
    sync_google_calendar_warning_only: Callable
    delete_google_calendar_warning_only: Callable
    load_tag: Callable
    translate_service_error: Callable
