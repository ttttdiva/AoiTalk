"""Tracker adapters for AoiTalk built-in tasks and tests."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..memory.models import Project, Task
from .config import AgentHarnessSettings
from .models import WorkItem


class WorkItemTracker(Protocol):
    async def fetch_candidates(self) -> list[WorkItem]:
        ...

    async def fetch_by_ids(self, ids: list[str]) -> list[WorkItem]:
        ...


class BuiltInTaskTrackerAdapter:
    """Treat AoiTalk tasks as harness work items."""

    def __init__(self, get_db_manager: Any, settings: AgentHarnessSettings):
        self._get_db_manager = get_db_manager
        self._settings = settings

    async def fetch_candidates(self) -> list[WorkItem]:
        db_manager = self._get_db_manager()
        if db_manager is None:
            return []
        session = await db_manager.get_session()
        try:
            active_states = {_normalize_state(state) for state in self._settings.tracker.active_states}
            stmt = (
                select(Task)
                .options(selectinload(Task.project))
                .where(Task.deleted_at.is_(None), Task.archived_at.is_(None))
            )
            if self._settings.tracker.project_id:
                try:
                    project_id = UUID(self._settings.tracker.project_id)
                except (TypeError, ValueError):
                    return []
                stmt = stmt.where(Task.project_id == project_id)
            result = await session.execute(stmt)
            items = []
            for task in result.scalars().unique().all():
                if _normalize_state(task.status) not in active_states:
                    continue
                if not self._settings.tracker.include_all_active_tasks and not _task_harness_enabled(task):
                    continue
                items.append(_task_to_work_item(task))
            return items
        finally:
            await session.close()

    async def fetch_by_ids(self, ids: list[str]) -> list[WorkItem]:
        db_manager = self._get_db_manager()
        if db_manager is None:
            return []
        task_ids = []
        for raw in ids:
            try:
                task_ids.append(UUID(raw))
            except (TypeError, ValueError):
                continue
        if not task_ids:
            return []
        session = await db_manager.get_session()
        try:
            result = await session.execute(
                select(Task)
                .options(selectinload(Task.project))
                .where(Task.id.in_(task_ids), Task.deleted_at.is_(None))
            )
            return [_task_to_work_item(task) for task in result.scalars().unique().all()]
        finally:
            await session.close()


class InMemoryWorkItemTracker:
    """Small deterministic tracker used by unit tests and dry harness wiring."""

    def __init__(self, items: list[WorkItem] | None = None):
        self.items = {item.id: item for item in items or []}

    async def fetch_candidates(self) -> list[WorkItem]:
        return list(self.items.values())

    async def fetch_by_ids(self, ids: list[str]) -> list[WorkItem]:
        return [self.items[item_id] for item_id in ids if item_id in self.items]

    def set_item(self, item: WorkItem) -> None:
        self.items[item.id] = item


def _task_harness_enabled(task: Task) -> bool:
    metadata = task.task_metadata or {}
    harness = metadata.get("agent_harness")
    if isinstance(harness, dict):
        return bool(harness.get("enabled"))
    return bool(metadata.get("agent_harness_enabled"))


def _task_to_work_item(task: Task) -> WorkItem:
    project: Project | None = task.project
    metadata = task.task_metadata or {}
    identifier = str(metadata.get("identifier") or f"TASK-{task.id}")
    return WorkItem(
        id=str(task.id),
        identifier=identifier,
        title=task.title,
        description=task.description or "",
        state=task.status,
        priority=_priority_rank(task.priority),
        project_id=str(task.project_id) if task.project_id else None,
        project_name=project.name if project is not None else None,
        labels=[],
        blocked_by=list(metadata.get("blocked_by") or []),
        created_at=task.created_at,
        updated_at=task.updated_at,
        metadata=metadata,
    )


def _priority_rank(priority: Any) -> int:
    if isinstance(priority, int):
        return priority
    return {
        "urgent": 1,
        "high": 2,
        "medium": 3,
        "normal": 3,
        "low": 4,
    }.get(str(priority or "").strip().lower(), 5)


def _normalize_state(state: str) -> str:
    return str(state or "").strip().lower()
