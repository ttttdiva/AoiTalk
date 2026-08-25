"""Task management service package.

TaskManagementService を関心ごとに Mixin 分割したパッケージ。
公開 API は従来どおり src.services.task_management_service から import できる。
"""

from ._shared import (
    DEFAULT_MEMBER_PERMISSIONS,
    DEFAULT_USER_NOTIFICATION_MINUTES,
    DISALLOWED_PLACEHOLDER_TITLES,
    LEGACY_STATUS_MAP,
    VALID_PRIORITIES,
    VALID_SKIP_MODES,
    VALID_TASK_STATUSES,
    ScheduledOccurrence,
    TaskManagementError,
    build_occurrence_schedule,
    build_time_report,
    correct_likely_timer_started_at,
    normalize_priority,
    normalize_skip_mode,
    normalize_task_status,
)
from .service import TaskManagementService
from .due_completion import _auto_close_now, _task_due_at

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
