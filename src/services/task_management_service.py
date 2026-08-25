"""Core task, calendar, timer, report, and notification services.

このモジュールは後方互換の re-export シムです。実装は関心ごとに
`src/services/task_management/` パッケージへ Mixin 分割されています。
既存の import パス（`from ..services.task_management_service import ...`）と、
テストによる `task_management_service.TaskManagementService` のモンキーパッチを
維持するため、公開シンボルをここから再公開します。
"""

from __future__ import annotations

from .task_management import (
    DEFAULT_MEMBER_PERMISSIONS,
    DEFAULT_USER_NOTIFICATION_MINUTES,
    DISALLOWED_PLACEHOLDER_TITLES,
    LEGACY_STATUS_MAP,
    VALID_PRIORITIES,
    VALID_SKIP_MODES,
    VALID_TASK_STATUSES,
    ScheduledOccurrence,
    TaskManagementError,
    TaskManagementService,
    build_occurrence_schedule,
    build_time_report,
    correct_likely_timer_started_at,
    normalize_priority,
    normalize_skip_mode,
    normalize_task_status,
    _auto_close_now,
    _task_due_at,
)

# テストが import する内部ヘルパーも従来どおり公開する。
from .task_management._shared import (
    _ensure_reminder_offsets,
    _get_user_notification_minutes,
    _get_user_task_notifications_default_enabled,
    _is_date_only_occurrence,
    _is_midnight,
    _normalize_member_permissions,
    _normalize_task_title,
    _strip_google_calendar_metadata,
)

__all__ = [
    "DEFAULT_MEMBER_PERMISSIONS",
    "DEFAULT_USER_NOTIFICATION_MINUTES",
    "DISALLOWED_PLACEHOLDER_TITLES",
    "LEGACY_STATUS_MAP",
    "VALID_PRIORITIES",
    "VALID_SKIP_MODES",
    "VALID_TASK_STATUSES",
    "ScheduledOccurrence",
    "TaskManagementError",
    "TaskManagementService",
    "build_occurrence_schedule",
    "build_time_report",
    "correct_likely_timer_started_at",
    "normalize_priority",
    "normalize_skip_mode",
    "normalize_task_status",
    "_auto_close_now",
    "_task_due_at",
]
