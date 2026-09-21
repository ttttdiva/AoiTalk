"""Task management: 繰り返しルールとオカレンス生成。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete, func, inspect as sqlalchemy_inspect, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import NO_VALUE
from sqlalchemy.orm import selectinload

from ...memory.models import (
    NotificationDelivery,
    Project,
    ProjectNotificationSetting,
    ProjectMember,
    Space,
    Tag,
    Task,
    TaskActivity,
    TaskAssignee,
    TaskAttachment,
    TaskReference,
    TaskComment,
    TaskDependency,
    TaskOccurrence,
    TaskRecurrenceRule,
    TaskRecurrenceScheduleSegment,
    TaskTag,
    TimeEntry,
    User,
    KnowledgeNode,
    KnowledgeNodeSupertag,
    KnowledgeSupertag,
)
from ...memory.project_repository import ProjectRepository
from ...task_time import DEFAULT_TASK_TIMEZONE, normalize_task_timezone
from ..project_color_service import extract_project_color
from ..task_reference_service import attach_agent_run_source_reference
from ._shared import (
    DEFAULT_MEMBER_PERMISSIONS,
    DEFAULT_USER_NOTIFICATION_MINUTES,
    DISALLOWED_PLACEHOLDER_TITLES,
    LEGACY_STATUS_MAP,
    VALID_PRIORITIES,
    VALID_TASK_STATUSES,
    ScheduledOccurrence,
    TaskManagementError,
    build_occurrence_schedule,
    build_time_report,
    normalize_priority,
    normalize_skip_mode,
    normalize_task_status,
    _ensure_reminder_offsets,
    _get_user_notification_minutes,
    _get_user_task_notifications_default_enabled,
    is_recurrence_exception_source_kind,
    is_recurrence_override_source_kind,
    parse_recurrence_override_original_start_at,
    apply_recurrence_schedule_segment,
    get_recurrence_segment_envelope_seconds,
    _is_date_only_occurrence,
    _is_midnight,
    _normalize_member_permissions,
    _normalize_task_title,
    _strip_google_calendar_metadata,
)

logger = logging.getLogger(__name__)


class OccurrenceMixin:
    """繰り返しルールとオカレンス生成。"""

    async def _sync_repeat_tag(
        self,
        session: AsyncSession,
        *,
        task: Task,
        has_recurrence: bool,
    ) -> None:
        """繰り返し設定の有無に応じて 'repeat' タグを自動付与または除去する。"""
        repeat_tag_id = await self._get_or_create_repeat_tag(
            session, project_id=task.project_id
        )

        result = await session.execute(
            select(TaskTag).where(
                TaskTag.task_id == task.id, TaskTag.tag_id == repeat_tag_id
            )
        )
        existing_link = result.scalar_one_or_none()

        if has_recurrence and existing_link is None:
            session.add(TaskTag(task_id=task.id, tag_id=repeat_tag_id))
        elif not has_recurrence and existing_link is not None:
            await session.delete(existing_link)

    async def _upsert_recurrence(
        self,
        session: AsyncSession,
        *,
        task: Task,
        recurrence_rrule: Optional[str],
        timezone: str = DEFAULT_TASK_TIMEZONE,
        horizon_days: int = 90,
        trigger_status: Optional[str] = None,
        create_new: Optional[bool] = None,
        recur_forever: Optional[bool] = None,
        reset_status_to: Optional[str] = None,
        end_count: Optional[int] = None,
        end_date: Optional[datetime] = None,
        skip_weekend: Optional[bool] = None,
        skip_holiday: Optional[bool] = None,
        skip_mode: Optional[str] = None,
    ) -> Optional[TaskRecurrenceRule]:
        result = await session.execute(
            select(TaskRecurrenceRule).where(TaskRecurrenceRule.task_id == task.id)
        )
        existing = result.scalar_one_or_none()
        if recurrence_rrule:
            if existing is None:
                existing = TaskRecurrenceRule(
                    task_id=task.id,
                    rrule=recurrence_rrule,
                    timezone=normalize_task_timezone(timezone),
                    horizon_days=horizon_days,
                    trigger_status=normalize_task_status(trigger_status or "closed"),
                    create_new=bool(create_new) if create_new is not None else False,
                    recur_forever=(
                        bool(recur_forever) if recur_forever is not None else True
                    ),
                    reset_status_to=normalize_task_status(reset_status_to or "open"),
                    end_count=end_count,
                    end_date=end_date,
                    skip_weekend=(
                        bool(skip_weekend) if skip_weekend is not None else False
                    ),
                    skip_holiday=(
                        bool(skip_holiday) if skip_holiday is not None else False
                    ),
                    skip_mode=normalize_skip_mode(skip_mode),
                )
                session.add(existing)
            else:
                existing.rrule = recurrence_rrule
                existing.timezone = normalize_task_timezone(
                    timezone or existing.timezone
                )
                existing.horizon_days = horizon_days
                if trigger_status is not None:
                    existing.trigger_status = normalize_task_status(trigger_status)
                if create_new is not None:
                    existing.create_new = bool(create_new)
                if recur_forever is not None:
                    existing.recur_forever = bool(recur_forever)
                if reset_status_to is not None:
                    existing.reset_status_to = normalize_task_status(reset_status_to)
                if end_count is not None:
                    existing.end_count = end_count
                if end_date is not None:
                    existing.end_date = end_date
                if skip_weekend is not None:
                    existing.skip_weekend = bool(skip_weekend)
                if skip_holiday is not None:
                    existing.skip_holiday = bool(skip_holiday)
                if skip_mode is not None:
                    existing.skip_mode = normalize_skip_mode(skip_mode)
        elif existing is not None:
            await session.delete(existing)
            # Segments are meaningful only while a recurrence rule exists. If
            # the rule is removed and later re-created, stale future edits
            # must not silently reappear on the new series.
            await session.execute(
                delete(TaskRecurrenceScheduleSegment).where(
                    TaskRecurrenceScheduleSegment.task_id == task.id
                )
            )
            # Keep an already-loaded relationship in sync with the bulk
            # delete above; otherwise recreating a rule in the same unit of
            # work could re-apply stale in-memory segments.
            try:
                relationship_state = sqlalchemy_inspect(
                    task
                ).attrs.recurrence_schedule_segments
                loaded_segments = relationship_state.loaded_value
                if loaded_segments is not NO_VALUE:
                    loaded_segments.clear()
            except Exception:
                pass
            existing = None
        return existing

    async def _materialize_occurrences(
        self,
        session: AsyncSession,
        task: Task,
        *,
        recurrence_rrule: Optional[str] = None,
        horizon_days: int = 90,
        skip_weekend: bool = False,
        skip_holiday: bool = False,
        skip_mode: Optional[str] = None,
        schedule_segments: Optional[Iterable[Any]] = None,
    ) -> None:
        segments = await self._load_recurrence_schedule_segments(
            session, task, schedule_segments=schedule_segments
        )
        segment_padding = get_recurrence_segment_envelope_seconds(segments)
        schedule = build_occurrence_schedule(
            start_at=task.start_at,
            end_at=task.end_at,
            recurrence_rrule=recurrence_rrule,
            all_day=bool(task.all_day),
            horizon_days=horizon_days,
            skip_weekend=skip_weekend,
            skip_holiday=skip_holiday,
            skip_mode=normalize_skip_mode(skip_mode),
            window_padding=segment_padding,
        )
        # 繰り返しルールが無いタスクでは schedule が空になる（_shared.py 参照）。
        # その場合 expected_starts も空になり、以下の差分適用で既存のミラー行が
        # すべて delete される。過去に作られたミラー行はタスク更新時に自然消滅する。
        # ``expected_starts`` is keyed by canonical RRULE identity.  The
        # displayed start can differ after applying a future schedule segment
        # (or a weekend/holiday shift), so using actual timestamps here would
        # re-create rows at the wrong identity and resurrect exceptions.
        expected_starts: dict[datetime, ScheduledOccurrence] = {}
        for canonical_occurrence in schedule:
            canonical_start = (
                canonical_occurrence.original_start_at
                or canonical_occurrence.start_at
            )
            applied = apply_recurrence_schedule_segment(
                canonical_start=canonical_start,
                canonical_end=canonical_start
                + (task.end_at - task.start_at),
                base_start_at=canonical_occurrence.start_at,
                base_end_at=canonical_occurrence.start_at
                + (task.end_at - task.start_at),
                base_all_day=bool(task.all_day),
                segments=segments,
            )
            expected_starts[canonical_start] = ScheduledOccurrence(
                start_at=applied.start_at,
                end_at=applied.end_at,
                is_generated=canonical_occurrence.is_generated,
                source_kind=canonical_occurrence.source_kind,
                original_start_at=canonical_start,
                all_day=applied.all_day,
            )

        result = await session.execute(
            select(TaskOccurrence).where(TaskOccurrence.task_id == task.id)
        )
        existing_occurrences = result.scalars().all()

        stale_occurrences: list[TaskOccurrence] = []
        for occurrence in existing_occurrences:
            # ユーザーが個別の回に加えた例外（「この回だけ削除」＝recurrence_skip、
            # 「この回だけ別日へ移動」＝ro:）は materialize の管理対象外。
            # 以前は source_kind を planned のもので上書きしていたため、
            # 削除したはずの回が recurrence に戻って復活し、移動した回は
            # 予定に無い時刻として stale 扱いで消えていた。
            if is_recurrence_exception_source_kind(occurrence.source_kind):
                # 例外が指している「元の回」を予定から取り除き、同じ回が
                # 通常のオカレンスとして作り直されないようにする。
                original_start_at = self._occurrence_canonical_start(occurrence)
                expected_starts.pop(original_start_at, None)
                continue

            canonical_start = self._occurrence_canonical_start(occurrence)
            planned = expected_starts.pop(canonical_start, None)
            if planned is None:
                stale_occurrences.append(occurrence)
                continue
            # ここへ到達するのは schedule が空でない = 繰り返しタスクの場合だけなので、
            # 旧「recurrence_rrule is None ならタスク状態をミラーする」分岐は不要。
            occurrence.start_at = planned.start_at
            occurrence.end_at = planned.end_at
            occurrence.source_kind = planned.source_kind
            occurrence.is_generated = planned.is_generated
            occurrence.original_start_at = planned.original_start_at
            occurrence.all_day = bool(
                task.all_day if planned.all_day is None else planned.all_day
            )
            occurrence.reminder_offsets = task.reminder_offsets

        if stale_occurrences:
            # notification_deliveries.occurrence_id と time_entries.occurrence_id は
            # ON DELETE 指定の無い外部キーなので、参照を外さずに削除すると
            # IntegrityError になりタスクの作成・更新ごと失敗する。
            # 配信済み通知や実績時間の記録自体は task_id 側で残るため、参照だけ NULL にする。
            stale_ids = [occurrence.id for occurrence in stale_occurrences]
            for referencing_model in (NotificationDelivery, TimeEntry):
                await session.execute(
                    update(referencing_model)
                    .where(referencing_model.occurrence_id.in_(stale_ids))
                    .values(occurrence_id=None)
                )
            for occurrence in stale_occurrences:
                await session.delete(occurrence)

        for planned in expected_starts.values():
            session.add(
                TaskOccurrence(
                    task_id=task.id,
                    start_at=planned.start_at,
                    end_at=planned.end_at,
                    status=task.status,
                    all_day=bool(
                        task.all_day if planned.all_day is None else planned.all_day
                    ),
                    reminder_offsets=task.reminder_offsets,
                    source_kind=planned.source_kind,
                    is_generated=planned.is_generated,
                    original_start_at=planned.original_start_at,
                )
            )

    @staticmethod
    def _occurrence_canonical_start(occurrence: TaskOccurrence) -> datetime:
        """Resolve an occurrence's canonical identity with legacy fallbacks."""

        original_start_at = getattr(occurrence, "original_start_at", None)
        if isinstance(original_start_at, datetime):
            return original_start_at
        parsed = parse_recurrence_override_original_start_at(
            getattr(occurrence, "source_kind", None)
        )
        if parsed is not None:
            return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
        # ``recurrence_skip`` rows live at their canonical slot.  For legacy
        # generated/task-schedule rows without the new column, the only safe
        # fallback is their current start (a later materialization will stamp
        # the canonical value explicitly).
        return occurrence.start_at

    async def _load_recurrence_schedule_segments(
        self,
        session: AsyncSession,
        task: Task,
        *,
        schedule_segments: Optional[Iterable[Any]] = None,
    ) -> list[Any]:
        """Load a task's segments without triggering async lazy-loads.

        CRUD callers selectin-load the relationship.  The occurrence update
        path may receive a task loaded with only its recurrence rule, so query
        there when SQLAlchemy reports an unloaded collection.  Lightweight
        service doubles (``SimpleNamespace`` in the legacy tests) simply use
        an empty collection instead of changing their expected query shape.
        """

        if schedule_segments is not None:
            return list(schedule_segments)

        try:
            state = sqlalchemy_inspect(task)
            relationship_state = state.attrs.recurrence_schedule_segments
            loaded = relationship_state.loaded_value
            if loaded is not NO_VALUE:
                return list(loaded or [])
        except Exception:
            # Non-ORM test doubles do not expose SQLAlchemy inspection state.
            if hasattr(task, "recurrence_schedule_segments"):
                return list(getattr(task, "recurrence_schedule_segments") or [])
            return []

        if not isinstance(task, Task):
            return []
        result = await session.execute(
            select(TaskRecurrenceScheduleSegment)
            .where(TaskRecurrenceScheduleSegment.task_id == task.id)
            .order_by(TaskRecurrenceScheduleSegment.effective_from.asc())
        )
        return list(result.scalars().all())

    async def list_occurrences(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        project_id: Optional[UUID] = None,
        space_id: Optional[UUID] = None,
        browse_project_id: Optional[UUID | str] = None,
        browse_space_id: Optional[UUID | str] = None,
        start_from: Optional[datetime] = None,
        end_to: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        participating_project_ids = await self.resolve_read_project_ids(
            session,
            user_id=user_id,
            project_id=project_id,
            space_id=space_id,
            browse_project_id=browse_project_id,
            browse_space_id=browse_space_id,
        )
        if not participating_project_ids:
            return []

        stmt = (
            select(TaskOccurrence)
            .join(Task)
            .options(
                selectinload(TaskOccurrence.task).selectinload(Task.project),
                selectinload(TaskOccurrence.task)
                .selectinload(Task.task_tags)
                .selectinload(TaskTag.tag),
            )
            .where(
                Task.project_id.in_(participating_project_ids),
                Task.deleted_at.is_(None),
                TaskOccurrence.deleted_at.is_(None),
            )
            .order_by(TaskOccurrence.start_at.asc())
        )
        if start_from:
            stmt = stmt.where(TaskOccurrence.end_at >= start_from)
        if end_to:
            stmt = stmt.where(TaskOccurrence.start_at <= end_to)

        result = await session.execute(stmt)
        return [occurrence.to_dict() for occurrence in result.scalars().all()]

    async def update_occurrence(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        occurrence_id: UUID,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        result = await session.execute(
            select(TaskOccurrence)
            .join(Task, Task.id == TaskOccurrence.task_id)
            .options(
                selectinload(TaskOccurrence.task).selectinload(Task.recurrence_rule),
                selectinload(TaskOccurrence.task).selectinload(
                    Task.recurrence_schedule_segments
                ),
            )
            .where(
                TaskOccurrence.id == occurrence_id,
                TaskOccurrence.deleted_at.is_(None),
                Task.deleted_at.is_(None),
            )
        )
        occurrence = result.scalar_one_or_none()
        if occurrence is None or occurrence.task is None:
            raise TaskManagementError("Occurrence not found", status_code=404)

        task = occurrence.task
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )

        previous_occurrence_status = normalize_task_status(occurrence.status)
        previous_task_status = normalize_task_status(task.status)
        occurrence_status_changed = False
        task_became_closed = False
        if "status" in updates and updates["status"] is not None:
            next_occurrence_status = normalize_task_status(updates["status"])
            occurrence_status_changed = (
                previous_occurrence_status != next_occurrence_status
            )
            occurrence.status = next_occurrence_status
            if task.recurrence_rule is None:
                task.status = next_occurrence_status
                task_became_closed = (
                    previous_task_status not in {"closed", "cancelled"}
                    and next_occurrence_status == "closed"
                )
                if next_occurrence_status == "closed":
                    if task_became_closed or task.completed_at is None:
                        task.completed_at = datetime.utcnow()
                else:
                    task.completed_at = None

        # A status-only update remains a single-occurrence mutation even if a
        # caller sends a stray mode field.  Date changes explicitly select the
        # recurrence scope; omitted/invalid values are safe single semantics.
        next_start_at = updates.get("next_start_at")
        if next_start_at is None:
            next_start_at = updates.get("start_at")
        has_datetime_change = next_start_at is not None
        shifted = False
        if has_datetime_change:
            if not isinstance(next_start_at, datetime):
                next_start_at = self._coerce_occurrence_datetime(next_start_at)
            if next_start_at is None:
                raise TaskManagementError("Invalid occurrence start_at", status_code=400)

            mode = updates.get("mode") or "single"
            if mode not in {"single", "future"}:
                mode = "single"
            if task.recurrence_rule is None:
                mode = "single"

            if mode == "future":
                occurrence = await self._apply_future_occurrence_move(
                    session,
                    task=task,
                    occurrence=occurrence,
                    updates=updates,
                    new_start_at=next_start_at,
                )
            elif task.recurrence_rule is None:
                duration = occurrence.end_at - occurrence.start_at
                occurrence.start_at = next_start_at
                occurrence.end_at = updates.get("end_at") or (
                    next_start_at + duration
                )
                task.start_at = occurrence.start_at
                task.end_at = occurrence.end_at
            else:
                occurrence = await self._apply_single_occurrence_move(
                    session,
                    task=task,
                    occurrence=occurrence,
                    updates=updates,
                    new_start_at=next_start_at,
                )
            shifted = True

        if "end_at" in updates and updates["end_at"] is not None and not shifted:
            occurrence.end_at = updates["end_at"]
            if task.recurrence_rule is None:
                task.end_at = occurrence.end_at

        if "reminder_offsets" in updates and updates["reminder_offsets"] is not None:
            occurrence.reminder_offsets = _ensure_reminder_offsets(
                updates["reminder_offsets"],
                default=[],
            )

        activity_payload = {
            key: str(value) for key, value in updates.items() if value is not None
        }
        if "status" in updates and updates["status"] is not None:
            if occurrence_status_changed:
                activity_payload["status"] = normalize_task_status(
                    updates["status"]
                )
            else:
                # A closed->closed occurrence metadata edit is not a new
                # completion episode.  Keep the audit row, but do not leave a
                # completion-shaped status marker for recovery to consume.
                activity_payload.pop("status", None)
        activity = await self._record_activity(
            session,
            task_id=task.id,
            activity_type="occurrence_updated",
            user_id=user_id,
            payload=activity_payload,
        )
        if task.recurrence_rule is None and task_became_closed:
            from ..knowledge_capture_candidate_service import (
                enqueue_for_completed_task,
            )

            activity_id = getattr(activity, "id", None)
            if not isinstance(activity_id, UUID):
                activity_id = None
            enqueue_kwargs = {"trigger_user_id": user_id}
            if activity_id is not None:
                enqueue_kwargs["task_activity_id"] = activity_id
            await enqueue_for_completed_task(session, task, **enqueue_kwargs)
        await session.commit()
        # A future/single mutation may replace the originally selected row
        # with a generated/override row.  Refresh only when it remains
        # persistent; otherwise fetch the boundary row before serializing.
        try:
            await session.refresh(occurrence)
        except Exception:
            if task.recurrence_rule is None:
                raise
            canonical_start = self._occurrence_canonical_start(occurrence)
            refreshed = await session.execute(
                select(TaskOccurrence)
                .where(
                    TaskOccurrence.task_id == task.id,
                    TaskOccurrence.original_start_at == canonical_start,
                    TaskOccurrence.deleted_at.is_(None),
                )
                .order_by(TaskOccurrence.start_at.asc())
            )
            occurrence = refreshed.scalars().first()
            if occurrence is None:
                raise TaskManagementError("Occurrence not found", status_code=404)
        payload = occurrence.to_dict()
        await self._broadcast("task_occurrence_updated", payload)
        return payload

    @staticmethod
    def _coerce_occurrence_datetime(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value
        if value is None:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(
                tzinfo=None
            )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _occurrence_duration(
        *, task: Task, occurrence: TaskOccurrence
    ) -> timedelta:
        if task.start_at is not None and task.end_at is not None:
            return max(timedelta(0), task.end_at - task.start_at)
        return max(timedelta(0), occurrence.end_at - occurrence.start_at)

    @staticmethod
    def _pre_segment_occurrence_base(
        *, task: Task, rule: TaskRecurrenceRule, canonical_start: datetime
    ) -> tuple[datetime, datetime]:
        """Resolve the skip-adjusted base time before schedule offsets.

        Segment offsets are measured from the canonical RRULE timeline, but
        are applied to the existing weekend/holiday-adjusted occurrence.  A
        future edit must therefore not count the shift-forward days twice.
        """

        duration = (
            max(timedelta(0), task.end_at - task.start_at)
            if task.start_at is not None and task.end_at is not None
            else timedelta(0)
        )
        schedule = build_occurrence_schedule(
            start_at=task.start_at,
            end_at=task.end_at,
            recurrence_rrule=rule.rrule,
            all_day=bool(task.all_day),
            horizon_days=rule.horizon_days,
            skip_weekend=bool(rule.skip_weekend),
            skip_holiday=bool(rule.skip_holiday),
            skip_mode=rule.skip_mode,
            base_now=canonical_start,
        )
        for candidate in schedule:
            if (candidate.original_start_at or candidate.start_at) == canonical_start:
                return candidate.start_at, candidate.end_at
        return canonical_start, canonical_start + duration

    async def _apply_single_occurrence_move(
        self,
        session: AsyncSession,
        *,
        task: Task,
        occurrence: TaskOccurrence,
        updates: dict[str, Any],
        new_start_at: datetime,
    ) -> TaskOccurrence:
        """Persist a single recurring occurrence as skip + override rows."""

        canonical_start = self._coerce_occurrence_datetime(
            updates.get("original_start_at")
        ) or self._occurrence_canonical_start(occurrence)
        duration = self._occurrence_duration(task=task, occurrence=occurrence)
        new_end_at = self._coerce_occurrence_datetime(
            updates.get("next_end_at") or updates.get("end_at")
        ) or (new_start_at + duration)
        all_day = (
            bool(updates["all_day"])
            if isinstance(updates.get("all_day"), bool)
            else bool(occurrence.all_day)
        )

        # Locate an existing skip/override for this canonical identity.  A
        # generated row at the canonical slot is converted into the skip row;
        # an existing override is updated in place and keeps its own row id.
        result = await session.execute(
            select(TaskOccurrence).where(
                TaskOccurrence.task_id == task.id,
                TaskOccurrence.deleted_at.is_(None),
            )
        )
        rows = list(result.scalars().all())
        skip_row = next(
            (
                row
                for row in rows
                if row.source_kind == "recurrence_skip"
                and self._occurrence_canonical_start(row) == canonical_start
            ),
            None,
        )
        override = None
        if (
            occurrence.source_kind
            and is_recurrence_override_source_kind(occurrence.source_kind)
            and self._occurrence_canonical_start(occurrence) == canonical_start
        ):
            override = occurrence
        if override is None:
            override = next(
                (
                    row
                    for row in rows
                    if row.source_kind
                    and is_recurrence_override_source_kind(row.source_kind)
                    and self._occurrence_canonical_start(row) == canonical_start
                ),
                None,
            )

        if skip_row is None:
            if occurrence.source_kind == "recurrence_skip":
                skip_row = occurrence
            elif occurrence.source_kind and is_recurrence_override_source_kind(
                occurrence.source_kind
            ):
                # Legacy override rows may not have a materialized skip twin.
                skip_row = TaskOccurrence(
                    task_id=task.id,
                    start_at=canonical_start,
                    end_at=canonical_start + duration,
                    status=occurrence.status,
                    all_day=bool(task.all_day),
                    reminder_offsets=occurrence.reminder_offsets,
                    source_kind="recurrence_skip",
                    is_generated=False,
                    original_start_at=canonical_start,
                )
                session.add(skip_row)
            else:
                # The selected row is the generated canonical occurrence.
                # Reuse it as the skip row to avoid a transient unique-key
                # conflict, then create the displayed override below.
                occurrence.start_at = canonical_start
                occurrence.end_at = canonical_start + duration
                occurrence.source_kind = "recurrence_skip"
                occurrence.is_generated = False
                occurrence.original_start_at = canonical_start
                occurrence.all_day = bool(task.all_day)
                skip_row = occurrence

        if override is None or override is skip_row:
            if (
                occurrence is not skip_row
                and occurrence is not override
                and occurrence.source_kind == "recurrence"
            ):
                await self._detach_occurrence_references(session, occurrence.id)
                await session.delete(occurrence)
            override = TaskOccurrence(
                task_id=task.id,
                start_at=new_start_at,
                end_at=new_end_at,
                status=occurrence.status,
                all_day=all_day,
                reminder_offsets=occurrence.reminder_offsets,
                source_kind=self._format_override_source_kind(canonical_start),
                is_generated=False,
                original_start_at=canonical_start,
            )
            session.add(override)
        else:
            override.start_at = new_start_at
            override.end_at = new_end_at
            override.all_day = all_day
            override.original_start_at = canonical_start
            override.source_kind = self._format_override_source_kind(canonical_start)

        return override

    async def _apply_future_occurrence_move(
        self,
        session: AsyncSession,
        *,
        task: Task,
        occurrence: TaskOccurrence,
        updates: dict[str, Any],
        new_start_at: datetime,
    ) -> TaskOccurrence:
        """Persist a future schedule segment and rematerialize its task."""

        canonical_start = self._coerce_occurrence_datetime(
            updates.get("original_start_at")
        ) or self._occurrence_canonical_start(occurrence)
        duration = self._occurrence_duration(task=task, occurrence=occurrence)
        canonical_end = canonical_start + duration
        rule = task.recurrence_rule
        base_start_at, base_end_at = self._pre_segment_occurrence_base(
            task=task,
            rule=rule,
            canonical_start=canonical_start,
        )
        new_end_at = self._coerce_occurrence_datetime(
            updates.get("next_end_at") or updates.get("end_at")
        ) or (new_start_at + (base_end_at - base_start_at))
        start_offset_seconds = int(
            round((new_start_at - base_start_at).total_seconds())
        )
        end_offset_seconds = int(
            round((new_end_at - base_end_at).total_seconds())
        )
        all_day = (
            bool(updates["all_day"])
            if isinstance(updates.get("all_day"), bool)
            else bool(occurrence.all_day)
        )

        # A newer future edit supersedes every segment at or after its
        # boundary; the zero-offset case is intentionally retained as a reset.
        retained_result = await session.execute(
            select(TaskRecurrenceScheduleSegment)
            .where(
                TaskRecurrenceScheduleSegment.task_id == task.id,
                TaskRecurrenceScheduleSegment.effective_from < canonical_start,
            )
            .order_by(TaskRecurrenceScheduleSegment.effective_from.asc())
        )
        retained_segments = list(retained_result.scalars().all())
        await session.execute(
            delete(TaskRecurrenceScheduleSegment).where(
                TaskRecurrenceScheduleSegment.task_id == task.id,
                TaskRecurrenceScheduleSegment.effective_from >= canonical_start,
            )
        )
        segment = TaskRecurrenceScheduleSegment(
            task_id=task.id,
            effective_from=canonical_start,
            start_offset_seconds=start_offset_seconds,
            end_offset_seconds=end_offset_seconds,
            all_day=all_day,
        )
        session.add(segment)

        # Remove only the explicit exception at this boundary.  Exceptions
        # before it and after it remain untouched and continue to outrank the
        # new schedule segment during materialization.
        result = await session.execute(
            select(TaskOccurrence).where(
                TaskOccurrence.task_id == task.id,
                TaskOccurrence.deleted_at.is_(None),
            )
        )
        boundary_exceptions = [
            row
            for row in result.scalars().all()
            if is_recurrence_exception_source_kind(row.source_kind)
            and self._occurrence_canonical_start(row) == canonical_start
        ]
        boundary_state = next(
            (
                row
                for row in boundary_exceptions
                if is_recurrence_override_source_kind(row.source_kind)
            ),
            boundary_exceptions[0] if boundary_exceptions else None,
        )
        for row in boundary_exceptions:
            await self._detach_occurrence_references(session, row.id)
            await session.delete(row)

        await self._materialize_occurrences(
            session,
            task,
            recurrence_rrule=rule.rrule if rule else None,
            horizon_days=rule.horizon_days if rule else 90,
            skip_weekend=bool(rule.skip_weekend) if rule else False,
            skip_holiday=bool(rule.skip_holiday) if rule else False,
            skip_mode=rule.skip_mode if rule else None,
            schedule_segments=[*retained_segments, segment],
        )

        # Resolve the generated boundary row (materialization may have
        # replaced the selected exception row).
        result = await session.execute(
            select(TaskOccurrence)
            .where(
                TaskOccurrence.task_id == task.id,
                TaskOccurrence.original_start_at == canonical_start,
                TaskOccurrence.deleted_at.is_(None),
            )
            .order_by(TaskOccurrence.start_at.asc())
        )
        materialized = result.scalars().first()
        if materialized is not None and boundary_state is not None:
            # Promoting a boundary exception to a schedule segment changes
            # only its datetime/all-day semantics; preserve status and
            # reminder metadata so a completed/specially reminded occurrence
            # is not silently reset to the task defaults.
            materialized.status = boundary_state.status
            materialized.reminder_offsets = boundary_state.reminder_offsets
        return materialized or occurrence

    @staticmethod
    def _format_override_source_kind(canonical_start: datetime) -> str:
        return (
            "ro:"
            f"{canonical_start:%Y%m%dT%H%M%S}"
            f"{canonical_start.microsecond // 1000:03d}"
        )

    @staticmethod
    async def _detach_occurrence_references(
        session: AsyncSession, occurrence_id: UUID
    ) -> None:
        for referencing_model in (NotificationDelivery, TimeEntry):
            await session.execute(
                update(referencing_model)
                .where(referencing_model.occurrence_id == occurrence_id)
                .values(occurrence_id=None)
            )
