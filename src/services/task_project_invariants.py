"""Concurrency and integrity primitives shared by every task write path.

Task project moves, sync updates, and the agent task tool all eventually use
``TaskManagementService``.  The helpers in this module keep that boundary
explicit: project advisory locks are acquired in a deterministic order before
task/dependency rows are locked and revalidated.  Frontend BFF code uses the
same ``TASK_PROJECT_LOCK_NAMESPACE`` string so requests handled by either
process coordinate on PostgreSQL's transaction advisory lock namespace.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import Task, TaskDependency, TaskSchedulePlacement


# Keep this value in sync with frontend/src/lib/server/project-move-
# dependency-invariant.ts.  A project id is appended as text and hashed by
# PostgreSQL; callers must always acquire all ids in sorted order.
TASK_PROJECT_LOCK_NAMESPACE = "aoi-task-project-invariant:"


def _task_error(message: str, status_code: int, *, detail: dict[str, Any] | None = None):
    """Construct the domain error lazily to avoid task_management package cycles."""

    from .task_management._shared import TaskManagementError

    return TaskManagementError(message, status_code=status_code, detail=detail)


def _project_key(project_id: Any) -> str:
    return f"{TASK_PROJECT_LOCK_NAMESPACE}{project_id}"


def _sorted_unique(values: Iterable[Any]) -> list[str]:
    return sorted({str(value) for value in values if value is not None})


async def lock_task_project_ids(
    session: AsyncSession,
    project_ids: Iterable[UUID | str | None],
) -> list[str]:
    """Acquire transaction advisory locks for project ids in sorted order."""

    ordered = _sorted_unique(project_ids)
    for project_id in ordered:
        await session.execute(
            text("select pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": _project_key(project_id)},
        )
    return ordered


async def lock_task_rows(
    session: AsyncSession,
    task_ids: Iterable[UUID | str],
) -> dict[UUID, Task]:
    """Lock the requested task rows in deterministic id order."""

    normalized = sorted(
        {value if isinstance(value, UUID) else UUID(str(value)) for value in task_ids},
        key=str,
    )
    if not normalized:
        return {}
    result = await session.execute(
        select(Task)
        .where(Task.id.in_(normalized))
        .order_by(Task.id.asc())
        .with_for_update()
    )
    return {task.id: task for task in result.scalars().all()}


async def lock_task_dependencies(
    session: AsyncSession,
    task_ids: Iterable[UUID | str],
) -> list[TaskDependency]:
    """Lock dependency rows touching any of ``task_ids`` before checking."""

    normalized = [
        value if isinstance(value, UUID) else UUID(str(value)) for value in task_ids
    ]
    if not normalized:
        return []
    result = await session.execute(
        select(TaskDependency)
        .where(
            or_(
                TaskDependency.task_id.in_(normalized),
                TaskDependency.depends_on_task_id.in_(normalized),
            )
        )
        .order_by(TaskDependency.id.asc())
        .with_for_update()
    )
    return list(result.scalars().all())


async def prepare_task_project_move(
    session: AsyncSession,
    *,
    task_id: UUID,
    expected_project_id: UUID,
    target_project_id: UUID,
    target_parent_task_id: UUID | None = None,
) -> tuple[Task, Task | None]:
    """Lock and validate all state needed for a task project move.

    The caller is expected to have checked ACLs, but this function deliberately
    re-reads all mutable identity inside the transaction.  A parent with
    children is rejected rather than silently producing a cross-project tree;
    a requested destination parent is locked and must already belong to the
    target project.  The schedule placement is removed by this same
    transaction before the caller changes ``task.project_id``.
    """

    await lock_task_project_ids(session, (expected_project_id, target_project_id))
    rows = await lock_task_rows(
        session,
        (task_id,) + ((target_parent_task_id,) if target_parent_task_id else ()),
    )
    task = rows.get(task_id)
    if task is None or getattr(task, "deleted_at", None) is not None:
        raise _task_error("Task not found", 404)
    if task.project_id != expected_project_id:
        raise _task_error(
            "Task project changed; retry the move", status_code=409
        )

    # Lock the dependency rows after task identity is revalidated.  The
    # matching advisory lock serializes dependency CRUD and this move.
    dependencies = await lock_task_dependencies(session, (task_id,))
    if dependencies:
        raise _task_error(
            "依存関係があるタスクは別のプロジェクトへ移動できません。先に依存関係を明示的に削除してください",
            409,
            detail={"code": "task_project_move_has_dependencies"},
        )

    children_result = await session.execute(
        select(Task.id)
        .where(Task.parent_task_id == task_id, Task.deleted_at.is_(None))
        .order_by(Task.id.asc())
        .with_for_update()
    )
    if children_result.first() is not None:
        raise _task_error(
            "子タスクがある親タスクは別のプロジェクトへ移動できません",
            409,
            detail={"code": "task_project_move_has_children"},
        )

    parent = None
    if target_parent_task_id is not None:
        parent = rows.get(target_parent_task_id)
        if parent is None or getattr(parent, "deleted_at", None) is not None:
            raise _task_error("移動先の親タスクが見つかりません", 400)
        if parent.project_id != target_project_id:
            raise _task_error(
                "移動先の親タスクは移動先プロジェクトに属している必要があります",
                400,
            )

    # Keep the old parent relationship from crossing project boundaries.  The
    # caller may assign the requested target parent after this function.
    if target_parent_task_id is None:
        task.parent_task_id = None
    await session.execute(
        # A single task has at most one placement (task_id is the PK).
        TaskSchedulePlacement.__table__.delete().where(
            TaskSchedulePlacement.task_id == task_id
        )
    )
    return task, parent


async def prepare_task_parent_update(
    session: AsyncSession,
    *,
    task_id: UUID,
    expected_project_id: UUID,
    target_parent_task_id: UUID | None,
) -> tuple[Task, Task | None]:
    """Lock and revalidate a same-project parent change.

    Parent changes used to validate the parent with an unlocked read.  A
    concurrent project move could therefore change the parent project after
    validation but before the child row was committed.  Use the same
    deterministic project-advisory-lock -> task/parent-row-lock order as
    project moves, then re-read both project identities while those locks are
    held.
    """

    normalized_task_id = task_id if isinstance(task_id, UUID) else UUID(str(task_id))
    normalized_parent_id = (
        target_parent_task_id
        if isinstance(target_parent_task_id, UUID)
        else (
            UUID(str(target_parent_task_id))
            if target_parent_task_id is not None
            else None
        )
    )
    await lock_task_project_ids(session, (expected_project_id,))
    rows = await lock_task_rows(
        session,
        (normalized_task_id,)
        + ((normalized_parent_id,) if normalized_parent_id is not None else ()),
    )
    task = rows.get(normalized_task_id)
    if task is None or getattr(task, "deleted_at", None) is not None:
        raise _task_error("Task not found", 404)
    if task.project_id != expected_project_id:
        raise _task_error(
            "Task project changed; retry the parent update", status_code=409
        )

    parent: Task | None = None
    if target_parent_task_id is not None:
        if normalized_parent_id == normalized_task_id:
            raise _task_error("Task cannot be its own parent", 400)
        parent = rows.get(normalized_parent_id)
        if parent is None or getattr(parent, "deleted_at", None) is not None:
            raise _task_error("Parent task not found", 404)
        if parent.project_id != expected_project_id:
            raise _task_error(
                "Subtask parent must belong to the same project", status_code=400
            )
    return task, parent
