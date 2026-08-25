"""Task management shared constants, pure helpers, and domain types.

TaskManagementService から切り出したモジュールレベルの定数・純粋関数・
データクラス・例外。繰り返し予定のスキップ処理もここで一元化する。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional
from uuid import UUID, uuid4

from dateutil.rrule import rrulestr

from ...memory.models import (
    TaskOccurrence,
    Task,
    User,
)
from ...task_time import DEFAULT_TASK_TIMEZONE, normalize_task_timezone
# 既存の内部 import パスとの互換性のため、旧定数名もここから参照できるようにする。
from ...task_recurrence import (
    DEFAULT_SKIP_MODE,
    SKIP_MODE_OMIT,
    SKIP_MODE_SHIFT_BACKWARD,
    SKIP_MODE_SHIFT_FORWARD,
    VALID_SKIP_MODES,
    normalize_skip_mode,
)
from ...utils.japanese_holidays import is_japanese_holiday, is_weekend
from ..project_permissions import normalize_project_member_permissions

VALID_TASK_STATUSES = {
    "todo",
    "open",
    "in_progress",
    "blocked",
    "on_hold",
    "review",
    "closed",
    "cancelled",
}
VALID_PRIORITIES = {"low", "medium", "high", "urgent"}
DEFAULT_USER_NOTIFICATION_MINUTES = 5
DISALLOWED_PLACEHOLDER_TITLES = {"無題のタスク", "Untitled task"}
# 繰り返し設定はユーザー入力かつ展開時に CPU / メモリを消費するため、API と
# サービスの両方で同じ上限を適用する。サービス側の検証は、既存 DB の値や
# API を経由しない内部呼び出しに対する最後の防御でもある。
MAX_RECURRENCE_RRULE_LENGTH = 2048
MAX_RECURRENCE_HORIZON_DAYS = 3650
MAX_RECURRENCE_END_COUNT = 10_000
MAX_EXPANDED_OCCURRENCES = 10_000
DEFAULT_MEMBER_PERMISSIONS = {
    "owner": {
        "read": True,
        "write": True,
        "delete": True,
        "manage_members": True,
        "manage_settings": True,
    },
    "admin": {
        "read": True,
        "write": True,
        "delete": True,
        "manage_members": True,
        "manage_settings": False,
    },
    "member": {
        "read": True,
        "write": True,
        "delete": False,
        "manage_members": False,
        "manage_settings": False,
    },
    "viewer": {
        "read": True,
        "write": False,
        "delete": False,
        "manage_members": False,
        "manage_settings": False,
    },
}
LEGACY_STATUS_MAP = {
    "todo": "todo",
    "open": "open",
    "in_progress": "in_progress",
    # Docs #Task の状態フィールド（todo/doing/done）からの連携値
    "doing": "in_progress",
    "paused": "on_hold",
    "blocked": "blocked",
    "on_hold": "on_hold",
    "review": "review",
    "done": "closed",
    "closed": "closed",
}

logger = logging.getLogger(__name__)


class TaskManagementError(Exception):
    """Task management domain error with HTTP-like status information."""

    def __init__(
        self,
        message: str,
        status_code: int = 400,
        *,
        detail: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail


@dataclass
class ScheduledOccurrence:
    """Pure representation of an occurrence window."""

    start_at: datetime
    end_at: datetime
    is_generated: bool
    source_kind: str


def normalize_task_status(status: str) -> str:
    """Normalize legacy and public task statuses into the current enum."""
    normalized = (status or "").strip().lower()
    mapped = LEGACY_STATUS_MAP.get(normalized, normalized)
    if mapped not in VALID_TASK_STATUSES:
        raise TaskManagementError(f"Invalid status: {status}", status_code=400)
    return mapped


def normalize_priority(priority: Optional[str]) -> str:
    """Normalize priority values."""
    normalized = (priority or "medium").strip().lower()
    if normalized == "normal":
        normalized = "medium"
    if normalized not in VALID_PRIORITIES:
        raise TaskManagementError(f"Invalid priority: {priority}", status_code=400)
    return normalized


def _ensure_reminder_offsets(
    reminder_offsets: Optional[Iterable[Any]],
    *,
    default: Optional[Iterable[int]] = None,
) -> list[int]:
    if reminder_offsets is None:
        return list(default or [])

    normalized: list[int] = []
    for offset in reminder_offsets:
        try:
            value = int(offset)
        except (TypeError, ValueError) as exc:
            raise TaskManagementError(f"Invalid reminder offset: {offset}") from exc
        if value < 0:
            raise TaskManagementError("Reminder offsets must be >= 0")
        normalized.append(value)

    unique_sorted = sorted(set(normalized))
    return unique_sorted or list(default or [])


def _normalize_task_title(title: Optional[str]) -> str:
    normalized = (title or "").strip()
    if not normalized:
        raise TaskManagementError("title is required", status_code=400)
    if normalized in DISALLOWED_PLACEHOLDER_TITLES:
        raise TaskManagementError(
            "placeholder task titles are not allowed", status_code=400
        )
    return normalized


def _strip_google_calendar_metadata(
    metadata: Optional[dict[str, Any]],
) -> dict[str, Any]:
    cleaned = dict(metadata or {})
    cleaned.pop("google_calendar", None)
    return cleaned


def _get_user_notification_minutes(user: Optional[User]) -> int:
    raw = None
    if user is not None:
        raw = (user.user_settings or {}).get("task_notification_minutes_before")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_USER_NOTIFICATION_MINUTES
    return value if value >= 0 else DEFAULT_USER_NOTIFICATION_MINUTES


def _get_user_task_notifications_default_enabled(user: Optional[User]) -> bool:
    if user is None:
        return True
    raw = (user.user_settings or {}).get("task_notifications_default_enabled")
    return raw if isinstance(raw, bool) else True


def _is_midnight(value: Optional[datetime]) -> bool:
    return (
        value is not None
        and value.hour == 0
        and value.minute == 0
        and value.second == 0
        and value.microsecond == 0
    )


def _is_date_only_occurrence(occurrence: TaskOccurrence, task: Task) -> bool:
    if occurrence.all_day or task.all_day:
        return True
    if occurrence.start_at and occurrence.end_at:
        return _is_midnight(occurrence.start_at) and _is_midnight(occurrence.end_at)
    return _is_midnight(occurrence.start_at or occurrence.end_at)


def _normalize_member_permissions(role: str, permissions: Any) -> dict[str, bool]:
    # ``role`` is retained in the signature for compatibility with existing
    # task-service callers.  Permission grants must be explicit, even when a
    # legacy row has a role but its JSON permissions are NULL or malformed.
    _ = role
    return normalize_project_member_permissions(permissions)


_SKIP_SHIFT_GUARD_DAYS = 14


def _should_skip_occurrence(
    value: datetime, *, skip_weekend: bool, skip_holiday: bool
) -> bool:
    if skip_weekend and is_weekend(value.date()):
        return True
    if skip_holiday and is_japanese_holiday(value.date()):
        return True
    return False


def apply_occurrence_skip(
    value: datetime,
    *,
    skip_weekend: bool,
    skip_holiday: bool,
    skip_mode: str = DEFAULT_SKIP_MODE,
) -> Optional[datetime]:
    """土日・祝日に当たる発生日を skip_mode に従って処理する。

    frontend/src/lib/recurrence-preview.ts の applySkip と同じ挙動。
    - shift_forward: 条件を満たす最初の翌日へずらす（既定）。
      毎月3日のような設定でもその月の分が消えずに翌営業日へ回る。
    - 既存値 shift_backward: shift_forward として扱い、条件を満たす最初の翌日へずらす。
    - omit: その回自体を発生させない（None を返す）。
    """
    if not skip_weekend and not skip_holiday:
        return value
    if not _should_skip_occurrence(
        value, skip_weekend=skip_weekend, skip_holiday=skip_holiday
    ):
        return value

    mode = normalize_skip_mode(skip_mode)
    if mode == SKIP_MODE_OMIT:
        return None

    step = timedelta(days=1)
    shifted = value
    for _ in range(_SKIP_SHIFT_GUARD_DAYS):
        shifted += step
        if not _should_skip_occurrence(
            shifted, skip_weekend=skip_weekend, skip_holiday=skip_holiday
        ):
            return shifted
    return shifted


RECURRENCE_SKIP_SOURCE_KIND = "recurrence_skip"
RECURRENCE_OVERRIDE_PREFIX = "ro:"
LEGACY_RECURRENCE_OVERRIDE_PREFIX = "recurrence_override:"

# frontend/src/lib/recurrence-exceptions.ts の compactOriginalStartAt と同じ形式。
_COMPACT_OVERRIDE_RE = re.compile(
    r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(\d{3})Z?$"
)


def is_recurrence_skip_source_kind(source_kind: Optional[str]) -> bool:
    """「この回だけ削除」で作られた行か。"""
    return source_kind == RECURRENCE_SKIP_SOURCE_KIND


def is_recurrence_override_source_kind(source_kind: Optional[str]) -> bool:
    """「この回だけ別日へ移動」で作られた行か。"""
    if not source_kind:
        return False
    return source_kind.startswith(RECURRENCE_OVERRIDE_PREFIX) or source_kind.startswith(
        LEGACY_RECURRENCE_OVERRIDE_PREFIX
    )


def is_recurrence_exception_source_kind(source_kind: Optional[str]) -> bool:
    """ユーザーが個別の回に加えた例外（削除・移動）の行か。"""
    return is_recurrence_skip_source_kind(
        source_kind
    ) or is_recurrence_override_source_kind(source_kind)


def parse_recurrence_override_original_start_at(
    source_kind: Optional[str],
) -> Optional[datetime]:
    """移動した回の source_kind に埋め込まれた「元の回」の開始時刻を返す。

    frontend/src/lib/recurrence-exceptions.ts の parseRecurrenceOriginalStartAt と
    同じ形式を解釈する。解釈できない場合は None。
    """
    if not is_recurrence_override_source_kind(source_kind):
        return None
    assert source_kind is not None
    if source_kind.startswith(LEGACY_RECURRENCE_OVERRIDE_PREFIX):
        raw = source_kind[len(LEGACY_RECURRENCE_OVERRIDE_PREFIX) :].replace("Z", "")
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    matched = _COMPACT_OVERRIDE_RE.match(
        source_kind[len(RECURRENCE_OVERRIDE_PREFIX) :]
    )
    if not matched:
        return None
    year, month, day, hour, minute, second, millisecond = (
        int(part) for part in matched.groups()
    )
    return datetime(year, month, day, hour, minute, second, millisecond * 1000)


def build_occurrence_schedule(
    *,
    start_at: Optional[datetime],
    end_at: Optional[datetime],
    recurrence_rrule: Optional[str],
    all_day: bool = False,
    horizon_days: int = 90,
    base_now: Optional[datetime] = None,
    skip_weekend: bool = False,
    skip_holiday: bool = False,
    skip_mode: str = DEFAULT_SKIP_MODE,
) -> list[ScheduledOccurrence]:
    """Pure helper that expands a task schedule into materialized occurrences."""
    if not start_at or not end_at:
        return []
    duration = end_at - start_at
    if duration < timedelta(0) or (duration == timedelta(0) and not all_day):
        raise TaskManagementError("end_at must be after start_at", status_code=400)

    # Date-only tasks created by older clients can represent a one-day event with
    # the same midnight value for both endpoints.  Keep that legacy shape valid
    # for recurrence materialization while preserving the rejection for timed
    # zero-length tasks.  All-day end dates are inclusive, so generated
    # occurrences retain the zero-length representation; only the rolling
    # materialization window needs a non-zero one-day span.
    window_duration = (
        timedelta(days=1)
        if duration == timedelta(0) and all_day
        else duration
    )

    if not recurrence_rrule:
        # 非繰り返しタスクの予定は tasks 本体（start_at / end_at）が唯一の正本とする。
        # ここで 1 件返すと task_occurrences に同じ予定のミラー行が作られ、
        # 予定が tasks と task_occurrences の 2 か所に重複保存されてしまう。
        # Web BFF（frontend/src/app/api/tasks）はタスク更新時に task_occurrences を
        # 書き換えないため、日付を変更すると古い日付のミラー行が残り、
        # カレンダーに同じタスクが別日として二重表示される原因になっていた。
        # 繰り返しルールが無い場合はオカレンスを一切生成しない。
        return []

    if len(recurrence_rrule) > MAX_RECURRENCE_RRULE_LENGTH:
        raise TaskManagementError(
            f"recurrence_rrule must be at most {MAX_RECURRENCE_RRULE_LENGTH} characters",
            status_code=400,
        )
    if (
        not isinstance(horizon_days, int)
        or isinstance(horizon_days, bool)
        or horizon_days < 1
        or horizon_days > MAX_RECURRENCE_HORIZON_DAYS
    ):
        raise TaskManagementError(
            f"horizon_days must be between 1 and {MAX_RECURRENCE_HORIZON_DAYS}",
            status_code=400,
        )

    now = base_now or datetime.utcnow()
    try:
        window_start = min(start_at, now - window_duration)
        window_end = now + timedelta(days=horizon_days)
        rule = rrulestr(recurrence_rrule, dtstart=start_at)

        # ``between`` は該当回をすべて list 化するため、秒次かつ無期限のルールで
        # 容易にプロセスを枯渇させる。xafter の count で走査自体を打ち切り、上限を
        # 超えるルールは部分結果を返さず 400 とする。
        starts: list[datetime] = []
        for value in rule.xafter(
            window_start, count=MAX_EXPANDED_OCCURRENCES + 1, inc=True
        ):
            if value > window_end:
                break
            starts.append(value)
            if len(starts) > MAX_EXPANDED_OCCURRENCES:
                raise TaskManagementError(
                    "recurrence rule expands to more than "
                    f"{MAX_EXPANDED_OCCURRENCES} occurrences",
                    status_code=400,
                )
    except TaskManagementError:
        raise
    except Exception as exc:
        raise TaskManagementError(f"Invalid recurrence rule: {exc}") from exc

    # dateutil は dtstart のマイクロ秒を切り捨てる。元の start_at を追加する前に、
    # 同じ秒の初回を除かないと 09:00:00 と 09:00:00.123456 が二重生成される。
    if start_at.microsecond:
        truncated_start = start_at.replace(microsecond=0)
        starts = [value for value in starts if value != truncated_start]
    if start_at not in starts and start_at <= window_end:
        starts.insert(0, start_at)

    if skip_weekend or skip_holiday:
        # 開始日はユーザーが明示した日付なのでずらさない。2回目以降だけを対象にし、
        # ずらした結果が同じ日に着地した分は set で 1 件にまとめる
        # （毎日+土日スキップだと土・日・月がいずれも月曜へ寄るため）。
        # omit モードでは None が返るので、その回自体を落とす。
        adjusted: list[datetime] = []
        for value in starts:
            if value == start_at:
                adjusted.append(value)
                continue
            shifted = apply_occurrence_skip(
                value,
                skip_weekend=skip_weekend,
                skip_holiday=skip_holiday,
                skip_mode=skip_mode,
            )
            if shifted is not None:
                adjusted.append(shifted)
        starts = adjusted

    unique_starts = sorted(set(starts))
    if len(unique_starts) > MAX_EXPANDED_OCCURRENCES:
        raise TaskManagementError(
            "recurrence rule expands to more than "
            f"{MAX_EXPANDED_OCCURRENCES} occurrences",
            status_code=400,
        )

    try:
        return [
            ScheduledOccurrence(
                start_at=occurrence_start,
                end_at=occurrence_start + duration,
                is_generated=True,
                source_kind="recurrence",
            )
            for occurrence_start in unique_starts
        ]
    except OverflowError as exc:
        raise TaskManagementError(
            "recurrence occurrence is outside the supported datetime range",
            status_code=400,
        ) from exc


_TIMER_UTC_SKEW_MIN = timedelta(hours=8)
_TIMER_UTC_SKEW_MAX = timedelta(hours=10)


def correct_likely_timer_started_at(
    started_at: Optional[datetime],
    created_at: Optional[datetime],
    source: Optional[str],
) -> Optional[datetime]:
    """timer 起動時に UTC が混入した started_at を created_at で補正する。

    Web BFF（frontend/src/lib/server/db-time.ts の correctLikelyTimerStartedAt）と
    同じ発見的補正。DB はローカル壁時計時刻で保存する規約のため、
    started_at と created_at の差が 8〜10 時間ある timer エントリは
    UTC で書かれた可能性が高く、created_at を開始時刻として扱う。
    """
    if (
        source == "timer"
        and started_at is not None
        and created_at is not None
        and _TIMER_UTC_SKEW_MIN <= (created_at - started_at) <= _TIMER_UTC_SKEW_MAX
    ):
        return created_at
    return started_at


def build_time_report(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Pure aggregation helper used by reports and tests."""
    summary = {
        "total_seconds": 0,
        "entry_count": 0,
        "active_entries": 0,
    }
    by_project: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, Any]] = {}
    by_user: dict[str, dict[str, Any]] = {}
    by_task: dict[str, dict[str, Any]] = {}

    def _bucket(
        target: dict[str, dict[str, Any]],
        key: str,
        label: str,
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if key not in target:
            target[key] = {
                "key": key,
                "label": label,
                "seconds": 0,
                "entries": 0,
                **(extra or {}),
            }
        return target[key]

    # DB はローカル壁時計時刻で保存しているため、実行中エントリの集計も
    # ローカル現在時刻で行う（utcnow だと JST 環境で 9 時間ずれる）。
    now = datetime.now()
    for entry in entries:
        started_at = entry.get("started_at")
        ended_at = entry.get("ended_at")
        if not isinstance(started_at, datetime):
            continue

        effective_end = ended_at if isinstance(ended_at, datetime) else now
        duration_seconds = max(0, int((effective_end - started_at).total_seconds()))
        summary["total_seconds"] += duration_seconds
        summary["entry_count"] += 1
        if ended_at is None:
            summary["active_entries"] += 1

        project_key = entry.get("project_id") or "unknown"
        project_label = entry.get("project_name") or "Unknown project"
        project_bucket = _bucket(
            by_project,
            project_key,
            project_label,
            extra={"project_id": project_key, "project_name": project_label},
        )
        project_bucket["seconds"] += duration_seconds
        project_bucket["entries"] += 1

        task_key = entry.get("task_id") or "unknown"
        task_label = entry.get("task_title") or "Unknown task"
        task_bucket = _bucket(
            by_task,
            task_key,
            task_label,
            extra={
                "project_id": entry.get("project_id"),
                "project_name": entry.get("project_name"),
            },
        )
        task_bucket["seconds"] += duration_seconds
        task_bucket["entries"] += 1

        user_key = entry.get("user_id") or "unknown"
        user_label = (
            entry.get("display_name") or entry.get("username") or "Unknown user"
        )
        user_bucket = _bucket(by_user, user_key, user_label)
        user_bucket["seconds"] += duration_seconds
        user_bucket["entries"] += 1

        day_key = started_at.date().isoformat()
        day_bucket = _bucket(by_day, day_key, day_key)
        day_bucket["seconds"] += duration_seconds
        day_bucket["entries"] += 1

    return {
        "summary": summary,
        "by_project": sorted(
            by_project.values(), key=lambda item: item["seconds"], reverse=True
        ),
        "by_day": sorted(by_day.values(), key=lambda item: item["key"]),
        "by_user": sorted(
            by_user.values(), key=lambda item: item["seconds"], reverse=True
        ),
        "by_task": sorted(
            by_task.values(), key=lambda item: item["seconds"], reverse=True
        ),
    }
