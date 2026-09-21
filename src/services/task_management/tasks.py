"""Task management: タスク CRUD / タグ / コメント。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional
from uuid import UUID, uuid4

import httpx
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only, selectinload

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
    TaskRelation,
    TaskComment,
    TaskDependency,
    TaskOccurrence,
    TaskRecurrenceRule,
    TaskRecurrenceScheduleSegment,
    TaskSchedulePlacement,
    TaskAppLink,
    TaskTag,
    TimeEntry,
    User,
    KnowledgeNode,
    KnowledgeNodeShare,
    DocsLibrary,
    KnowledgeNodeSupertag,
    KnowledgeSupertag,
)
from ...memory.project_repository import ProjectRepository
from ...task_time import DEFAULT_TASK_TIMEZONE, normalize_task_timezone
from ..docs_acl import can_write_node
from ..managed_docs_policy import policy_for_node
from ..project_color_service import extract_project_color
from ..task_reference_service import attach_agent_run_source_reference
from ..task_project_invariants import (
    lock_task_project_ids,
    prepare_task_parent_update,
    prepare_task_project_move,
)
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
    normalize_task_status,
    _ensure_reminder_offsets,
    _get_user_notification_minutes,
    _get_user_task_notifications_default_enabled,
    _is_date_only_occurrence,
    _is_midnight,
    _normalize_member_permissions,
    _normalize_task_title,
    _strip_google_calendar_metadata,
)
from .helpers import _task_deletion_retention_days

logger = logging.getLogger(__name__)


def _assert_generation_mutation_allowed() -> None:
    """Reject a late generation write immediately before its commit.

    The generation cancellation gate is intentionally resolved lazily here so
    the task service remains importable in lightweight/legacy environments
    that do not load the LLM runtime.  Production generation workers always
    expose the guard module and therefore fail closed when their copied gate
    has been blocked by an interrupt or cancellation.
    """

    try:
        from ...llm.generation_cancellation import (
            raise_if_generation_mutation_blocked,
        )
    except ImportError:
        return
    raise_if_generation_mutation_blocked()


class TaskCrudMixin:
    """タスク CRUD / タグ / コメント。"""

    async def _assert_task_docs_guide_binding_allowed(
        self,
        session: AsyncSession,
        node: KnowledgeNode,
    ) -> None:
        """Reject only the repository-owned AoiTalk Guide subtree.

        Other managed Docs domains (notably ``project_inbox_item:*``) are
        legitimate task destinations for their owning workflows.  Walk the
        complete parent chain here instead of relying on the target's own
        metadata: legacy Guide descendants may have no stable key or display
        props after an older migration.  A missing or cross-library ancestor
        fails closed because we cannot prove that the target is ordinary.
        """

        current = node
        visited: set[Any] = set()
        docs_library_id = self._coerce_uuid(
            getattr(node, "docs_library_id", None)
            or getattr(node, "workspace_id", None)
        )
        depth = 0
        while current is not None:
            current_id = getattr(current, "id", None)
            if current_id in visited:
                raise TaskManagementError(
                    "Docs nodeの親階層が循環しています",
                    status_code=403,
                )
            visited.add(current_id)
            policy = policy_for_node(current)
            if policy is not None and policy.managed_domain == "aoitalk_guide":
                raise TaskManagementError(
                    "AoiTalk ガイドはタスク連携先にできません",
                    status_code=409,
                )
            parent_id = getattr(current, "parent_id", None)
            if parent_id is None:
                return
            if depth >= 512:
                raise TaskManagementError(
                    "Docs nodeの親階層を検証できません",
                    status_code=403,
                )
            depth += 1
            if isinstance(session, AsyncSession):
                current = await session.get(
                    KnowledgeNode,
                    parent_id,
                    populate_existing=True,
                )
            else:
                current = await session.get(KnowledgeNode, parent_id)
            if current is None:
                raise TaskManagementError(
                    "Docs nodeの親を検証できません",
                    status_code=403,
                )
            ancestor_library_id = self._coerce_uuid(
                getattr(current, "docs_library_id", None)
                or getattr(current, "workspace_id", None)
            )
            if (
                docs_library_id is None
                or ancestor_library_id is None
                or ancestor_library_id != docs_library_id
            ):
                raise TaskManagementError(
                    "Docs nodeの親が別Libraryにあります",
                    status_code=403,
                )

    @staticmethod
    def _coerce_uuid(value: Any) -> Optional[UUID]:
        if value is None:
            return None
        if isinstance(value, UUID):
            return value
        try:
            return UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            return None

    async def _lock_project_acl_for_task_write(
        self,
        session: AsyncSession,
        *,
        project_ids: Iterable[UUID | str | None],
        user_id: UUID,
        require_read: bool = False,
    ) -> None:
        """Recheck project ACLs while holding the rows a mutation consults.

        The public task APIs perform an early permission check for a useful
        response, but that check is not a transaction boundary.  Acquire the
        same project advisory locks used by task moves, then lock Project,
        User, and ProjectMember rows in deterministic order before asking the
        shared ProjectRepository authority for the current permissions.  A
        concurrent membership revoke therefore either wins before this point
        or waits until the task mutation commits.
        """

        if not isinstance(session, AsyncSession):
            return
        ordered_ids = sorted(
            {
                self._coerce_uuid(project_id)
                for project_id in project_ids
                if self._coerce_uuid(project_id) is not None
            },
            key=str,
        )
        if not ordered_ids:
            raise TaskManagementError("Project permission denied", status_code=403)

        await lock_task_project_ids(session, ordered_ids)

        locked_projects: list[Project] = []
        for project_id in ordered_ids:
            project_result = await session.execute(
                select(Project)
                .where(Project.id == project_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            project = project_result.scalar_one_or_none()
            if project is None or project.deleted_at is not None:
                raise TaskManagementError("Project not found", status_code=404)
            locked_projects.append(project)

        user_result = await session.execute(
            select(User)
            .where(User.id == user_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if user_result.scalar_one_or_none() is None:
            raise TaskManagementError("Project permission denied", status_code=403)

        for project in locked_projects:
            await session.execute(
                select(ProjectMember)
                .where(
                    and_(
                        ProjectMember.project_id == project.id,
                        ProjectMember.user_id == user_id,
                    )
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if not await ProjectRepository.has_permission(
                session,
                project_id=project.id,
                user_id=user_id,
                permission="write",
            ):
                raise TaskManagementError("Project permission denied", status_code=403)
            if require_read and not await ProjectRepository.has_permission(
                session,
                project_id=project.id,
                user_id=user_id,
                permission="read",
            ):
                raise TaskManagementError("Project permission denied", status_code=403)

    async def _lock_docs_acl_for_binding(
        self,
        session: AsyncSession,
        node: KnowledgeNode,
        library: DocsLibrary,
        user_id: UUID,
    ) -> DocsLibrary:
        """Lock every ACL row consulted by a task↔Docs binding.

        ``can_write_node`` remains the shared decision authority, but its
        point reads must run after the mutable library/project/member/share
        rows are locked.  This gives a concurrent revoke a transaction
        linearization point instead of allowing a stale preflight decision to
        survive until the task flush.
        """
        # Generic Docs writes gather the full closure and lock node rows in
        # ``KnowledgeNode.id`` order before locking the library.  Binding a
        # task must use the same order: a target→parent walk can deadlock with
        # a concurrent generic move that already owns the parent and waits for
        # the target.  The unlocked walk is only a structural snapshot; every
        # row is re-read under lock and a changed parent fails closed.
        expected_parent_by_id: dict[UUID, UUID | None] = {
            node.id: self._coerce_uuid(getattr(node, "parent_id", None)),
        }
        ancestor_ids: list[UUID] = [node.id]
        seen: set[UUID] = {node.id}
        current = node
        for depth in range(512):
            parent_id = self._coerce_uuid(getattr(current, "parent_id", None))
            if parent_id is None:
                break
            if parent_id in seen:
                raise TaskManagementError(
                    "Docs nodeの親階層が循環しています",
                    status_code=403,
                )
            parent_result = await session.execute(
                select(KnowledgeNode)
                .where(KnowledgeNode.id == parent_id)
                .execution_options(populate_existing=True)
            )
            parent = parent_result.scalar_one_or_none()
            if (
                parent is None
                or self._coerce_uuid(getattr(parent, "docs_library_id", None))
                != self._coerce_uuid(getattr(library, "id", None))
            ):
                raise TaskManagementError(
                    "Docs nodeの親を検証できません",
                    status_code=403,
                )
            seen.add(parent_id)
            ancestor_ids.append(parent_id)
            expected_parent_by_id[parent_id] = self._coerce_uuid(
                getattr(parent, "parent_id", None)
            )
            current = parent
            if depth == 511 and getattr(current, "parent_id", None) is not None:
                raise TaskManagementError(
                    "Docs nodeの親階層を検証できません",
                    status_code=403,
                )

        ordered_ancestor_ids = sorted(set(ancestor_ids), key=str)
        locked_by_id: dict[UUID, KnowledgeNode] = {}
        for ancestor_id in ordered_ancestor_ids:
            locked_result = await session.execute(
                select(KnowledgeNode)
                .where(KnowledgeNode.id == ancestor_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            locked = locked_result.scalar_one_or_none()
            if (
                locked is None
                or self._coerce_uuid(getattr(locked, "docs_library_id", None))
                != self._coerce_uuid(getattr(library, "id", None))
                or self._coerce_uuid(getattr(locked, "parent_id", None))
                != expected_parent_by_id.get(ancestor_id)
            ):
                raise TaskManagementError(
                    "Docs nodeの親階層が同時に変更されたためタスク連携を中止しました",
                    status_code=409,
                )
            locked_by_id[ancestor_id] = locked

        locked_node = locked_by_id.get(node.id)
        if locked_node is None:
            raise TaskManagementError("Docs node not found", status_code=404)
        # Return the fresh target through the session identity map so callers
        # do not continue evaluating a stale preflight object after the
        # closure lock.
        node = locked_node

        # Match generic Docs writes: node closure first, then the mutable
        # library and its ACL rows.  This avoids the library↔node inversion
        # during a concurrent move.
        library_result = await session.execute(
            select(DocsLibrary)
            .where(DocsLibrary.id == library.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        locked_library = library_result.scalar_one_or_none()
        if locked_library is None:
            raise TaskManagementError("Docs node not found", status_code=404)
        library = locked_library
        if node.project_id is not None:
            await session.execute(
                select(Project)
                .where(Project.id == node.project_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            await session.execute(
                select(ProjectMember)
                .where(
                    and_(
                        ProjectMember.project_id == node.project_id,
                        ProjectMember.user_id == user_id,
                    )
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            await session.execute(
                select(User)
                .where(User.id == user_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            return library

        # Shares are also locked in lexical node-id order.  ACL evaluation
        # below still follows nearest→root so a nearer read share cannot be
        # bypassed by a broader write share.
        for ancestor_id in ordered_ancestor_ids:
            await session.execute(
                select(KnowledgeNodeShare)
                .where(
                    and_(
                        KnowledgeNodeShare.user_id == user_id,
                        KnowledgeNodeShare.node_id == ancestor_id,
                    )
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        return library

    async def _validate_knowledge_node_binding(
        self,
        session: AsyncSession,
        *,
        knowledge_node_id: UUID,
        task_project_id: UUID,
        user_id: UUID,
    ) -> KnowledgeNode:
        """Validate a Docs node before attaching it to a task.

        A task writer must also be allowed to write the Docs node.  Project
        tasks may bind nodes in their canonical project library, or a
        personal node which the actor explicitly owns/has a write share for;
        a node from another project/library is never accepted.
        """

        node = await session.get(KnowledgeNode, knowledge_node_id)
        if node is None:
            raise TaskManagementError("Docs node not found", status_code=404)
        if getattr(node, "archived_at", None) is not None:
            raise TaskManagementError("Docs node not found", status_code=404)
        # Project pointer/repair writers lock Project rows before their target
        # node.  Lock the task Project and any reverse-pointer Projects first;
        # the Docs helper below then acquires the complete node closure in
        # lexical order.  Keeping pointer locks ahead of node locks matches
        # canonical repair and generic Docs moves without a cross-project
        # inversion.
        if isinstance(session, AsyncSession):
            project_lock_result = await session.execute(
                select(Project)
                .where(Project.id == task_project_id)
                .order_by(Project.id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            locked_projects = list(project_lock_result.scalars().all())
            node_result = await session.execute(
                select(KnowledgeNode)
                .where(KnowledgeNode.id == knowledge_node_id)
                .execution_options(populate_existing=True)
            )
            node = node_result.scalar_one_or_none()
            if node is None:
                raise TaskManagementError("Docs node not found", status_code=404)
            if getattr(node, "archived_at", None) is not None:
                raise TaskManagementError("Docs node not found", status_code=404)
            # Reject a foreign Project identity before consulting any Docs ACL
            # rows.  Task create/update already holds the task Project lock;
            # acquiring another Project after node locks would invert the
            # Project→node order used by canonical repair.
            node_project_id = self._coerce_uuid(getattr(node, "project_id", None))
            if node_project_id is not None and node_project_id != task_project_id:
                raise TaskManagementError("Docs node permission denied", status_code=403)
            # A Project may have assigned this node as its canonical pointer
            # while the initial preflight was running.  Lock reverse pointers
            # before the node closure; ``nowait`` converts a conflicting
            # canonical repair into a fail-closed 409 instead of waiting in a
            # Project↔node lock cycle.
            try:
                pointer_result = await session.execute(
                    select(Project)
                    .where(Project.knowledge_node_id == knowledge_node_id)
                    .order_by(Project.id)
                    .execution_options(populate_existing=True)
                    .with_for_update(nowait=True)
                )
                locked_projects.extend(pointer_result.scalars().all())
            except Exception as exc:
                raise TaskManagementError(
                    "Docs node identity is being repaired; retry the binding",
                    status_code=409,
                ) from exc
        # ``workspace_id`` is the legacy alias still used by dependency-free
        # service doubles and rolling-deploy callers.  Persisted ORM rows use
        # ``docs_library_id``; accepting the alias here preserves the existing
        # Task/Docs binding API without relaxing the ACL checks below.
        docs_library_id = self._coerce_uuid(
            getattr(node, "docs_library_id", None)
            or getattr(node, "workspace_id", None)
        )
        if docs_library_id is None:
            raise TaskManagementError("Docs node permission denied", status_code=403)
        library = await session.get(DocsLibrary, docs_library_id)
        if (
            library is None
            or self._coerce_uuid(getattr(library, "id", None)) != docs_library_id
        ):
            raise TaskManagementError("Docs node not found", status_code=404)

        if isinstance(session, AsyncSession):
            library = await self._lock_docs_acl_for_binding(
                session,
                node,
                library,
                user_id,
            )
            refreshed_node_result = await session.execute(
                select(KnowledgeNode)
                .where(KnowledgeNode.id == knowledge_node_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            node = refreshed_node_result.scalar_one_or_none()
            if node is None or getattr(node, "archived_at", None) is not None:
                raise TaskManagementError("Docs node not found", status_code=404)

        try:
            writable = await can_write_node(
                session,
                node,
                user_id,
                library=library,
            )
        except Exception:
            # ACL failures must fail closed.  In particular, a missing share
            # table during a rolling migration must not become a task binding
            # bypass or an unexpected 500.
            writable = False
        if not writable:
            raise TaskManagementError("Docs node permission denied", status_code=403)

        node_project_id = self._coerce_uuid(getattr(node, "project_id", None))
        # Project identity is carried by the canonical node, never by a Docs
        # Library discriminator.  A project-bound node must match the task's
        # project; an ordinary owner-controlled Personal node may remain
        # unbound and still be attached to a task (the task's project is the
        # authoritative task scope in that case).
        if node_project_id is not None and node_project_id != task_project_id:
            raise TaskManagementError("Docs node permission denied", status_code=403)

        # A task link is also a future Docs mutation target: title updates are
        # synchronized back to the bound node.  Perform the ACL and project
        # identity checks first so a caller who cannot read/write a private
        # Guide cannot learn its managed status.  For a real AsyncSession this
        # runs after the target row is locked/refreshed, closing the reparent
        # race between validation and task mutation; dependency-free service
        # doubles still get the same preflight check here.
        if isinstance(session, AsyncSession):
            system_key = str(getattr(node, "system_key", "") or "").strip()
            if (
                system_key == "project_information_root"
                or system_key.startswith("project_information:")
                or any(project.knowledge_node_id == knowledge_node_id for project in locked_projects)
            ):
                raise TaskManagementError(
                    "Project canonical Docs nodeはタスク連携先にできません",
                    status_code=409,
                )
        await self._assert_task_docs_guide_binding_allowed(session, node)
        return node

    async def _load_task_for_update(
        self,
        session: AsyncSession,
        task_id: UUID,
    ) -> Task:
        result = await session.execute(
            select(Task)
            .options(
                selectinload(Task.project),
                selectinload(Task.assignees).selectinload(TaskAssignee.user),
                selectinload(Task.comments).selectinload(TaskComment.user),
                selectinload(Task.activities).selectinload(TaskActivity.user),
                selectinload(Task.recurrence_rule),
                selectinload(Task.recurrence_schedule_segments),
                selectinload(Task.occurrences),
                selectinload(Task.time_entries).selectinload(TimeEntry.user),
                selectinload(Task.task_tags).selectinload(TaskTag.tag),
            )
            .where(Task.id == task_id, Task.deleted_at.is_(None))
            .with_for_update()
        )
        task = result.scalar_one_or_none()
        if task is None:
            raise TaskManagementError("Task not found", status_code=404)
        return task

    async def create_task(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        title: str,
        description: Optional[str] = None,
        project_id: Optional[UUID] = None,
        knowledge_node_id: Optional[UUID] = None,
        status: str = "todo",
        priority: Optional[str] = None,
        start_at: Optional[datetime] = None,
        end_at: Optional[datetime] = None,
        all_day: bool = False,
        auto_close_on_due: bool = False,
        reminder_offsets: Optional[Iterable[Any]] = None,
        notifications_enabled: Optional[bool] = None,
        estimated_hours: Optional[float] = None,
        parent_task_id: Optional[UUID] = None,
        assignee_ids: Optional[list[UUID]] = None,
        tag_ids: Optional[list[UUID]] = None,
        recurrence_rrule: Optional[str] = None,
        recurrence_timezone: str = DEFAULT_TASK_TIMEZONE,
        task_metadata: Optional[dict[str, Any]] = None,
        source: str = "local",
        legacy_local_task_id: Optional[UUID] = None,
        task_id: Optional[UUID] = None,
        agent_run_id: str | UUID | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        normalized_title = _normalize_task_title(title)
        normalized_status = normalize_task_status(status)
        normalized_priority = normalize_priority(priority)
        normalized_reminders = _ensure_reminder_offsets(reminder_offsets, default=[])
        target_project_id = await self._resolve_project_id(
            session,
            user_id=user_id,
            project_id=project_id,
            require_write=True,
        )
        # A task must be visible to its creator after a successful write.  Do
        # not infer or grant read access here: read/write remain independent
        # membership ACLs, and a write-only member must be rejected before any
        # task rows or related records are touched.
        await self.require_project_permission(
            session,
            project_id=target_project_id,
            user_id=user_id,
            permission="read",
        )
        await self._lock_project_acl_for_task_write(
            session,
            project_ids=(target_project_id,),
            user_id=user_id,
            require_read=True,
        )
        if knowledge_node_id is not None:
            await self._validate_knowledge_node_binding(
                session,
                knowledge_node_id=knowledge_node_id,
                task_project_id=target_project_id,
                user_id=user_id,
            )
        if notifications_enabled is None:
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            notifications_enabled = _get_user_task_notifications_default_enabled(user)
        # Web / mobile と同じく、トップレベルは全 Project、サブタスクは
        # 同じ親の中で先頭になるように採番する。
        accessible_project_ids = await self._get_accessible_project_ids(session, user_id)
        sort_project_ids = accessible_project_ids or [target_project_id]
        sort_conditions = [
            Task.project_id.in_(sort_project_ids),
            Task.parent_task_id.is_(None),
            Task.deleted_at.is_(None),
        ]
        if parent_task_id is not None:
            # Parent creation and project moves share the same advisory lock
            # namespace. Lock the project first, then re-read the parent row so
            # a move cannot commit between validation and child insertion.
            await lock_task_project_ids(session, (target_project_id,))
            parent_result = await session.execute(
                select(Task)
                .where(Task.id == parent_task_id, Task.deleted_at.is_(None))
                .with_for_update()
            )
            parent = parent_result.scalar_one_or_none()
            if parent is None:
                raise TaskManagementError("Parent task not found", status_code=404)
            if parent.project_id != target_project_id:
                raise TaskManagementError(
                    "Subtask parent must belong to the same project", status_code=400
                )
            sort_conditions = [
                Task.project_id == target_project_id,
                Task.parent_task_id == parent_task_id,
                Task.deleted_at.is_(None),
            ]
        min_sort_result = await session.execute(
            select(func.min(Task.sort_order)).where(*sort_conditions)
        )
        next_sort_order = float(min_sort_result.scalar_one_or_none() or 0) - 1

        task = Task(
            id=task_id or uuid4(),
            project_id=target_project_id,
            legacy_local_task_id=legacy_local_task_id,
            knowledge_node_id=knowledge_node_id,
            title=normalized_title,
            description=description,
            status=normalized_status,
            priority=normalized_priority,
            start_at=start_at,
            end_at=end_at,
            all_day=all_day,
            auto_close_on_due=bool(auto_close_on_due),
            reminder_offsets=normalized_reminders,
            notifications_enabled=bool(notifications_enabled),
            estimated_hours=estimated_hours,
            parent_task_id=parent_task_id,
            source=source,
            created_by=user_id,
            completed_at=datetime.utcnow() if normalized_status == "closed" else None,
            task_metadata=_strip_google_calendar_metadata(task_metadata),
            sort_order=next_sort_order,
        )
        session.add(task)
        await session.flush()

        # Verification provenance is request-local and server-authenticated;
        # never infer it from task titles or caller metadata.  The ledger row
        # and JSON marker are written in this same transaction.
        from ..verification_provenance import register_current_entity

        tagged_metadata = await register_current_entity(
            session,
            entity_type="task",
            entity_id=task.id,
            metadata=task.task_metadata,
        )
        if tagged_metadata is not None:
            task.task_metadata = tagged_metadata

        # Agent RunのContextVarに依存せず、呼び出し元で捕捉したRun IDを使う。
        # 参照登録はタスク作成と同じトランザクションに含める。
        await attach_agent_run_source_reference(
            session,
            task_id=task.id,
            project_id=target_project_id,
            user_id=user_id,
            agent_run_id=agent_run_id,
        )

        recurrence = await self._upsert_recurrence(
            session,
            task=task,
            recurrence_rrule=recurrence_rrule,
            timezone=recurrence_timezone,
        )
        await self._replace_assignees(
            session,
            task=task,
            assignee_ids=list(assignee_ids or []),
            assigned_by=user_id,
            assign_requester_when_empty=True,
        )
        if tag_ids:
            await self._replace_tags(session, task=task, tag_ids=tag_ids)
        await self._sync_repeat_tag(
            session, task=task, has_recurrence=recurrence is not None
        )
        await self._materialize_occurrences(
            session,
            task,
            recurrence_rrule=recurrence.rrule if recurrence else None,
            horizon_days=recurrence.horizon_days if recurrence else 90,
            skip_weekend=bool(recurrence.skip_weekend) if recurrence else False,
            skip_holiday=bool(recurrence.skip_holiday) if recurrence else False,
            skip_mode=recurrence.skip_mode if recurrence else None,
        )
        task_created_activity = await self._record_activity(
            session,
            task_id=task.id,
            activity_type="task_created",
            user_id=user_id,
            payload={
                "project_id": str(target_project_id),
                "status": normalized_status,
            },
        )

        if normalized_status == "closed":
            from ..knowledge_capture_candidate_service import (
                enqueue_for_completed_task,
            )

            activity_id = getattr(task_created_activity, "id", None)
            if not isinstance(activity_id, UUID):
                activity_id = None
            enqueue_kwargs = {"trigger_user_id": user_id}
            if activity_id is not None:
                enqueue_kwargs["task_activity_id"] = activity_id
            await enqueue_for_completed_task(session, task, **enqueue_kwargs)

        if commit:
            _assert_generation_mutation_allowed()
            await session.commit()
        task = await self._load_task(session, task.id)
        payload = task.to_dict()
        if commit:
            await self._broadcast("task_created", payload)
        return payload

    async def list_tasks(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        project_id: Optional[UUID] = None,
        space_id: Optional[UUID] = None,
        browse_project_id: Optional[UUID | str] = None,
        browse_space_id: Optional[UUID | str] = None,
        status: Optional[str] = None,
        assignee_id: Optional[UUID] = None,
        search: Optional[str] = None,
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
            select(Task)
            .options(
                selectinload(Task.project),
                selectinload(Task.assignees).selectinload(TaskAssignee.user),
                selectinload(Task.recurrence_rule),
                selectinload(Task.task_tags).selectinload(TaskTag.tag),
            )
            .where(
                Task.project_id.in_(participating_project_ids),
                Task.archived_at.is_(None),
                Task.deleted_at.is_(None),
            )
            .order_by(
                Task.sort_order.asc().nulls_last(),
                Task.project_id.asc(),
                Task.parent_task_id.asc().nulls_first(),
                Task.created_at.asc(),
                Task.id.asc(),
            )
        )

        if status:
            stmt = stmt.where(Task.status == normalize_task_status(status))
        if search:
            like_term = f"%{search.strip()}%"
            stmt = stmt.where(
                or_(Task.title.ilike(like_term), Task.description.ilike(like_term))
            )
        if assignee_id:
            stmt = stmt.join(TaskAssignee).where(TaskAssignee.user_id == assignee_id)

        result = await session.execute(stmt)
        return [task.to_dict() for task in result.scalars().unique().all()]

    @staticmethod
    def _task_candidate_snippet(description: str | None, *, max_len: int = 120) -> str:
        text = " ".join((description or "").split())
        if len(text) <= max_len:
            return text
        return text[: max_len - 1].rstrip() + "…"

    async def search_task_candidates(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        project_id: Optional[UUID] = None,
        space_id: Optional[UUID] = None,
        status: Optional[str] = None,
        assignee_id: Optional[UUID] = None,
        search: Optional[str] = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Return lightweight task candidates for agent duplicate checks."""
        participating_project_ids = await self._get_participating_project_ids(
            session, user_id
        )
        if project_id is not None:
            await self.require_project_permission(
                session, project_id=project_id, user_id=user_id, permission="read"
            )
            participating_project_ids = (
                [project_id]
                if project_id in participating_project_ids
                else []
            )
        elif space_id is not None:
            participating_project_ids = await self._filter_project_ids_by_space(
                session,
                project_ids=participating_project_ids,
                space_id=space_id,
            )

        if not participating_project_ids:
            return []

        bounded_limit = max(1, min(int(limit or 25), 50))
        stmt = (
            select(Task)
            .options(
                load_only(
                    Task.id,
                    Task.title,
                    Task.status,
                    Task.project_id,
                    Task.parent_task_id,
                    Task.updated_at,
                    Task.description,
                )
            )
            .where(
                Task.project_id.in_(participating_project_ids),
                Task.archived_at.is_(None),
                Task.deleted_at.is_(None),
            )
            .order_by(
                Task.updated_at.desc(),
                Task.sort_order.asc().nulls_last(),
                Task.id.asc(),
            )
            .limit(bounded_limit)
        )

        if status:
            stmt = stmt.where(Task.status == normalize_task_status(status))
        if search:
            like_term = f"%{search.strip()}%"
            stmt = stmt.where(
                or_(Task.title.ilike(like_term), Task.description.ilike(like_term))
            )
        if assignee_id:
            stmt = stmt.join(TaskAssignee).where(TaskAssignee.user_id == assignee_id)

        result = await session.execute(stmt)
        candidates: list[dict[str, Any]] = []
        for task in result.scalars().unique().all():
            candidates.append(
                {
                    "id": str(task.id),
                    "title": task.title,
                    "status": task.status,
                    "project_id": str(task.project_id),
                    "parent_task_id": (
                        str(task.parent_task_id) if task.parent_task_id else None
                    ),
                    "updated_at": (
                        task.updated_at.isoformat() if task.updated_at else None
                    ),
                    "snippet": self._task_candidate_snippet(task.description),
                }
            )
        return candidates

    async def delete_task(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: UUID,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Soft-delete a task tree and return its canonical tombstone.

        The operation is deliberately idempotent: a repeated request returns
        the existing root tombstone instead of creating a second batch.  Only
        rows which are still live participate in a new batch; pre-existing
        tombstones on children/occurrences remain untouched and therefore
        cannot be accidentally restored later.
        """

        # Serialize concurrent deletes of the same root.  Without a row lock,
        # two transactions can each mint a different batch while only one
        # update wins, leaving one caller with a non-existent restore batch.
        result = await session.execute(
            select(Task).where(Task.id == task_id).with_for_update()
        )
        task = result.scalar_one_or_none()
        if task is None:
            raise TaskManagementError("Task not found", status_code=404)
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )

        if getattr(task, "deleted_at", None) is not None:
            payload = {
                "id": str(task.id),
                "task_id": str(task.id),
                "task_ids": [str(task.id)],
                "deleted_at": task.deleted_at.isoformat(),
                "deletion_batch_id": (
                    str(getattr(task, "deletion_batch_id", None))
                    if getattr(task, "deletion_batch_id", None)
                    else None
                ),
                "idempotent": True,
            }
            if commit:
                await self._broadcast("task_deleted", payload)
            return payload

        task_ids = await self._collect_task_tree_ids(
            session,
            task.id,
            lock_rows=True,
        )
        deleted_at = datetime.utcnow()
        deletion_batch_id = uuid4()

        # Assign the same timestamp and batch to the live task tree.  Updating
        # ORM instances (rather than deleting rows) keeps all comments,
        # activities, references, dependencies and notification history
        # available during the restore window.
        await session.execute(
            update(Task)
            .where(Task.id.in_(task_ids), Task.deleted_at.is_(None))
            .values(
                deleted_at=deleted_at,
                deletion_batch_id=deletion_batch_id,
                updated_at=deleted_at,
            )
        )
        await session.execute(
            update(TaskOccurrence)
            .where(
                TaskOccurrence.task_id.in_(task_ids),
                TaskOccurrence.deleted_at.is_(None),
            )
            .values(
                deleted_at=deleted_at,
                deletion_batch_id=deletion_batch_id,
                updated_at=deleted_at,
            )
        )
        await session.execute(
            update(TimeEntry)
            .where(
                TimeEntry.task_id.in_(task_ids),
                TimeEntry.deleted_at.is_(None),
            )
            .values(
                deleted_at=deleted_at,
                deletion_batch_id=deletion_batch_id,
                updated_at=deleted_at,
            )
        )

        # Record audit metadata without making it a prerequisite for the
        # tombstone transaction.  The helper is best-effort by design and is
        # safe while the content-deletion migration rolls out.
        await self._append_task_deletion_audit(
            session,
            task_ids=task_ids,
            deletion_batch_id=deletion_batch_id,
            deleted_at=deleted_at,
            action="delete",
            actor_user_id=user_id,
            root_task_id=task.id,
            project_id=task.project_id,
        )
        await self._record_activity(
            session,
            task_id=task.id,
            activity_type="task_deleted",
            user_id=user_id,
            payload={
                "deletion_batch_id": str(deletion_batch_id),
                "deleted_at": deleted_at.isoformat(),
                "task_ids": [str(value) for value in task_ids],
            },
        )
        if commit:
            _assert_generation_mutation_allowed()
            await session.commit()
        payload = {
            "id": str(task.id),
            "task_id": str(task.id),
            "task_ids": [str(value) for value in task_ids],
            "deleted_at": deleted_at.isoformat(),
            "deletion_batch_id": str(deletion_batch_id),
            "idempotent": False,
        }
        if commit:
            await self._broadcast("task_deleted", payload)
        return payload

    async def restore_task(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: UUID,
        deletion_batch_id: Optional[UUID] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Restore one exact, unexpired task-deletion batch.

        The root's current batch is authoritative.  An optional caller
        supplied id is accepted as an optimistic-concurrency guard; it must
        match exactly.  Rows that were already tombstoned by another batch
        are intentionally excluded from the restore update.
        """

        result = await session.execute(
            select(Task).where(Task.id == task_id).with_for_update()
        )
        root = result.scalar_one_or_none()
        if root is None:
            raise TaskManagementError("Task not found", status_code=404)
        await self.require_project_permission(
            session, project_id=root.project_id, user_id=user_id, permission="write"
        )

        if root.deleted_at is None:
            if deletion_batch_id is not None:
                raise TaskManagementError(
                    "Deletion batch does not match task", status_code=409
                )
            # Do not call ``to_dict`` here: an idempotent restore may be
            # reached with a minimally-loaded ORM row and async lazy
            # relationships would otherwise raise MissingGreenlet.
            return {
                "id": str(root.id),
                "task_id": str(root.id),
                "restored": False,
                "idempotent": True,
                "deleted_at": None,
                "deletion_batch_id": None,
            }

        batch_id = getattr(root, "deletion_batch_id", None)
        if batch_id is None:
            raise TaskManagementError(
                "Task deletion has no restorable batch", status_code=409
            )
        if deletion_batch_id is not None and deletion_batch_id != batch_id:
            raise TaskManagementError(
                "Deletion batch does not match task", status_code=409
            )

        current_time = now or datetime.utcnow()
        expiry = root.deleted_at + timedelta(days=_task_deletion_retention_days())
        if current_time >= expiry:
            raise TaskManagementError(
                "Task deletion restore window has expired",
                status_code=410,
                detail={
                    "code": "task_restore_expired",
                    "deleted_at": root.deleted_at.isoformat(),
                    "expires_at": expiry.isoformat(),
                },
            )

        deleted_at = root.deleted_at
        task_result = await session.execute(
            select(Task.id).where(
                Task.deletion_batch_id == batch_id,
                Task.deleted_at == deleted_at,
            )
        )
        task_ids = list(task_result.scalars().all())
        if root.id not in task_ids:
            raise TaskManagementError(
                "Deletion batch does not match task", status_code=409
            )

        restored_at = current_time
        await session.execute(
            update(Task)
            .where(
                Task.id.in_(task_ids),
                Task.deletion_batch_id == batch_id,
                Task.deleted_at == deleted_at,
            )
            .values(
                deleted_at=None,
                deletion_batch_id=None,
                updated_at=restored_at,
            )
        )
        await session.execute(
            update(TaskOccurrence)
            .where(
                TaskOccurrence.task_id.in_(task_ids),
                TaskOccurrence.deletion_batch_id == batch_id,
                TaskOccurrence.deleted_at == deleted_at,
            )
            .values(
                deleted_at=None,
                deletion_batch_id=None,
                updated_at=restored_at,
            )
        )
        await session.execute(
            update(TimeEntry)
            .where(
                TimeEntry.task_id.in_(task_ids),
                TimeEntry.deletion_batch_id == batch_id,
                TimeEntry.deleted_at == deleted_at,
            )
            .values(
                deleted_at=None,
                deletion_batch_id=None,
                updated_at=restored_at,
            )
        )
        await self._record_activity(
            session,
            task_id=root.id,
            activity_type="task_restored",
            user_id=user_id,
            payload={
                "deletion_batch_id": str(batch_id),
                "restored_at": restored_at.isoformat(),
                "task_ids": [str(value) for value in task_ids],
            },
        )
        await self._append_task_deletion_audit(
            session,
            task_ids=task_ids,
            deletion_batch_id=batch_id,
            deleted_at=deleted_at,
            action="restore",
            actor_user_id=user_id,
            root_task_id=root.id,
            project_id=root.project_id,
            event_at=restored_at,
        )
        _assert_generation_mutation_allowed()
        await session.commit()
        payload = {
            "id": str(root.id),
            "task_id": str(root.id),
            "task_ids": [str(value) for value in task_ids],
            "deletion_batch_id": str(batch_id),
            "restored_at": restored_at.isoformat(),
            "restored": True,
            "idempotent": False,
        }
        await self._broadcast("task_restored", payload)
        return payload

    async def purge_deleted_task_ids(
        self,
        session: AsyncSession,
        *,
        task_ids: Iterable[UUID | str],
        expected_deletion_batch_id: UUID | str | None = None,
        expected_batch_ids: Mapping[UUID | str, UUID | str] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Permanently remove an explicitly approved set of task tombstones.

        Unlike :meth:`purge_expired_task_deletions`, this method never scans
        for expired rows.  The caller must provide the exact task UUIDs that
        were selected by an evidence-backed cleanup manifest.  Every selected
        row is locked and checked again in the same transaction; a caller may
        supply one expected batch id for the complete set or a mapping keyed
        by task UUID.  A stale/live/mismatched row fails closed before any
        dependent rows are changed.  Missing rows are treated as an idempotent
        retry because a previous invocation may already have purged them.

        The deletion order intentionally mirrors the canonical retention
        helper.  All task-owned rows are removed explicitly before the task
        rows themselves, while the content-deletion ledger receives a
        ``purged`` event so sync clients can converge after the physical row
        disappears.
        """

        # ``task_ids`` is an iterable so callers can pass a manifest generator,
        # but strings must never be interpreted as an iterable of characters.
        if task_ids is None:  # type: ignore[comparison-overlap]
            raw_task_ids: list[Any] = []
        elif isinstance(task_ids, (str, bytes)):
            raw_task_ids = [task_ids]
        else:
            raw_task_ids = list(task_ids)

        def _normalize_uuid(value: Any, field_name: str) -> UUID:
            try:
                return value if isinstance(value, UUID) else UUID(str(value))
            except (TypeError, ValueError, AttributeError) as exc:
                raise TaskManagementError(
                    f"{field_name} must be a valid UUID", status_code=400
                ) from exc

        normalized_task_ids: list[UUID] = []
        seen_task_ids: set[UUID] = set()
        for raw_task_id in raw_task_ids:
            task_id = _normalize_uuid(raw_task_id, "task_ids")
            if task_id in seen_task_ids:
                continue
            seen_task_ids.add(task_id)
            normalized_task_ids.append(task_id)

        expected_batch: UUID | None = None
        if expected_deletion_batch_id is not None:
            expected_batch = _normalize_uuid(
                expected_deletion_batch_id, "expected_deletion_batch_id"
            )

        expected_by_task: dict[UUID, UUID] | None = None
        if expected_batch_ids is not None:
            if not isinstance(expected_batch_ids, Mapping):
                raise TaskManagementError(
                    "expected_batch_ids must be a mapping", status_code=400
                )
            if expected_batch is not None:
                raise TaskManagementError(
                    "Provide either expected_deletion_batch_id or expected_batch_ids",
                    status_code=400,
                )
            expected_by_task = {}
            for raw_task_id, raw_batch_id in expected_batch_ids.items():
                expected_by_task[_normalize_uuid(raw_task_id, "expected_batch_ids")] = (
                    _normalize_uuid(raw_batch_id, "expected_batch_ids")
                )

        empty_result = {
            "purged_batches": 0,
            "purged_tasks": 0,
            "task_ids": [],
            "missing_task_ids": [str(task_id) for task_id in normalized_task_ids],
            "skipped_task_ids": [],
            "idempotent": True,
        }
        if not normalized_task_ids:
            return empty_result

        # Lock exactly the rows named by the manifest.  Do not replace this
        # with a batch/retention query: a batch can contain legitimate rows
        # outside the approved one-time cleanup scope.
        result = await session.execute(
            select(Task)
            .where(Task.id.in_(normalized_task_ids))
            .with_for_update()
        )
        selected_rows = list(result.scalars().all())
        selected_by_id: dict[UUID, Task] = {}
        for row in selected_rows:
            row_id = _normalize_uuid(getattr(row, "id", None), "task.id")
            # A primary-key result cannot contain duplicates, but retaining the
            # first row makes lightweight test doubles and unusual adapters
            # deterministic without widening the deletion scope.
            selected_by_id.setdefault(row_id, row)

        missing_task_ids = [
            task_id for task_id in normalized_task_ids if task_id not in selected_by_id
        ]
        eligible_rows: list[Task] = []
        skipped_task_ids: list[UUID] = []
        for task_id in normalized_task_ids:
            row = selected_by_id.get(task_id)
            if row is None:
                continue

            deleted_at = getattr(row, "deleted_at", None)
            deletion_batch_id = getattr(row, "deletion_batch_id", None)
            if deleted_at is None or deletion_batch_id is None:
                raise TaskManagementError(
                    "Task is not a restorable deletion tombstone",
                    status_code=409,
                )
            actual_batch = _normalize_uuid(
                deletion_batch_id, "task.deletion_batch_id"
            )

            if expected_batch is not None and actual_batch != expected_batch:
                raise TaskManagementError(
                    "Deletion batch does not match task", status_code=409
                )
            if expected_by_task is not None:
                expected_for_task = expected_by_task.get(task_id)
                if expected_for_task is None or actual_batch != expected_for_task:
                    raise TaskManagementError(
                        "Deletion batch does not match task", status_code=409
                    )

            eligible_rows.append(row)

        # Parent/child deletion is the one self-referential edge that can
        # cascade outside the explicit manifest.  Refuse a partial tree rather
        # than allowing a database-level ON DELETE CASCADE to remove an
        # unapproved child.  ``with_for_update`` serializes an existing child
        # against concurrent task writers in the maintenance transaction.
        eligible_ids = [row.id for row in eligible_rows]
        if eligible_ids:
            child_result = await session.execute(
                select(Task.id, Task.parent_task_id)
                .where(Task.parent_task_id.in_(eligible_ids))
                .with_for_update()
            )
            child_rows = list(child_result.all())
            eligible_id_set = set(eligible_ids)
            for child_id, _parent_id in child_rows:
                child_uuid = _normalize_uuid(child_id, "task.id")
                if child_uuid not in eligible_id_set:
                    raise TaskManagementError(
                        "Task purge requires the complete deleted task tree",
                        status_code=409,
                    )

        if not eligible_rows:
            return {
                "purged_batches": 0,
                "purged_tasks": 0,
                "task_ids": [],
                "missing_task_ids": [str(task_id) for task_id in missing_task_ids],
                "skipped_task_ids": [str(task_id) for task_id in skipped_task_ids],
                "idempotent": True,
            }

        # Keep the audit event's timestamp stable across all selected batches.
        event_at = datetime.utcnow()
        rows_by_batch: dict[UUID, list[Task]] = {}
        for row in eligible_rows:
            batch_id = _normalize_uuid(row.deletion_batch_id, "task.deletion_batch_id")
            rows_by_batch.setdefault(batch_id, []).append(row)

        # The shared task-deletion ledger is the durable sync source once the
        # physical Task row is gone.  Preserve one event per task and retain
        # its project scope for mobile/general sync queries.
        for batch_id, batch_rows in rows_by_batch.items():
            batch_task_ids = [row.id for row in batch_rows]
            batch_deleted_at = min(
                (row.deleted_at for row in batch_rows if row.deleted_at is not None),
                default=event_at,
            )
            batch_project_ids = {
                getattr(row, "project_id", None)
                for row in batch_rows
                if getattr(row, "project_id", None) is not None
            }
            project_id = (
                next(iter(batch_project_ids), None)
                if len(batch_project_ids) == 1
                else None
            )
            batch_id_set = set(batch_task_ids)
            root_task_id = next(
                (
                    row.id
                    for row in batch_rows
                    if getattr(row, "parent_task_id", None) not in batch_id_set
                ),
                batch_task_ids[0],
            )
            await self._append_task_deletion_audit(
                session,
                task_ids=batch_task_ids,
                deletion_batch_id=batch_id,
                deleted_at=batch_deleted_at,
                action="purge",
                root_task_id=root_task_id,
                project_id=project_id,
                event_at=event_at,
            )

        await self._remove_task_supertags_for_deleted_tasks(session, eligible_ids)

        # Keep this order aligned with purge_expired_task_deletions.  In
        # particular NotificationDelivery and TimeEntry rows may reference a
        # TaskOccurrence, so both are removed before occurrences themselves.
        # Include occurrence-only deliveries as older rows may not have a
        # task_id populated.
        occurrence_ids = select(TaskOccurrence.id).where(
            TaskOccurrence.task_id.in_(eligible_ids)
        )
        await session.execute(
            delete(NotificationDelivery).where(
                or_(
                    NotificationDelivery.task_id.in_(eligible_ids),
                    NotificationDelivery.occurrence_id.in_(occurrence_ids),
                )
            )
        )
        await session.execute(
            delete(TimeEntry).where(
                or_(
                    TimeEntry.task_id.in_(eligible_ids),
                    TimeEntry.occurrence_id.in_(occurrence_ids),
                )
            )
        )
        await session.execute(
            delete(TaskOccurrence).where(TaskOccurrence.task_id.in_(eligible_ids))
        )
        await session.execute(
            delete(TaskDependency).where(
                or_(
                    TaskDependency.task_id.in_(eligible_ids),
                    TaskDependency.depends_on_task_id.in_(eligible_ids),
                )
            )
        )
        await session.execute(
            delete(TaskActivity).where(TaskActivity.task_id.in_(eligible_ids))
        )
        await session.execute(
            delete(TaskRecurrenceRule).where(TaskRecurrenceRule.task_id.in_(eligible_ids))
        )
        await session.execute(
            delete(TaskRecurrenceScheduleSegment).where(
                TaskRecurrenceScheduleSegment.task_id.in_(eligible_ids)
            )
        )
        await session.execute(
            delete(TaskSchedulePlacement).where(
                TaskSchedulePlacement.task_id.in_(eligible_ids)
            )
        )
        await session.execute(
            delete(TaskComment).where(TaskComment.task_id.in_(eligible_ids))
        )
        await session.execute(
            delete(TaskAttachment).where(TaskAttachment.task_id.in_(eligible_ids))
        )
        await session.execute(
            delete(TaskReference).where(TaskReference.task_id.in_(eligible_ids))
        )
        await session.execute(
            delete(TaskRelation).where(
                or_(
                    TaskRelation.task_a_id.in_(eligible_ids),
                    TaskRelation.task_b_id.in_(eligible_ids),
                )
            )
        )
        await session.execute(
            delete(TaskAppLink).where(TaskAppLink.task_id.in_(eligible_ids))
        )
        await session.execute(delete(TaskTag).where(TaskTag.task_id.in_(eligible_ids)))
        await session.execute(
            delete(TaskAssignee).where(TaskAssignee.task_id.in_(eligible_ids))
        )

        # Detach the self-reference before deleting rows.  The complete-tree
        # guard above ensures this cannot silently cascade into an unapproved
        # child task.
        await session.execute(
            update(Task)
            .where(Task.id.in_(eligible_ids))
            .values(parent_task_id=None)
        )
        await session.execute(delete(Task).where(Task.id.in_(eligible_ids)))

        if commit:
            _assert_generation_mutation_allowed()
            await session.commit()

        purged_task_ids = [str(task_id) for task_id in eligible_ids]
        return {
            "purged_batches": len(rows_by_batch),
            "purged_tasks": len(eligible_ids),
            "task_ids": purged_task_ids,
            "missing_task_ids": [str(task_id) for task_id in missing_task_ids],
            "skipped_task_ids": [str(task_id) for task_id in skipped_task_ids],
            "idempotent": False,
        }

    async def _remove_task_supertags_for_deleted_tasks(
        self,
        session: AsyncSession,
        task_ids: list[UUID],
    ) -> None:
        if not task_ids:
            return
        linked_node_ids = select(Task.knowledge_node_id).where(
            Task.id.in_(task_ids),
            Task.knowledge_node_id.is_not(None),
        )
        task_tag_ids = select(KnowledgeSupertag.id).where(
            KnowledgeSupertag.system_key == "task"
        )
        await session.execute(
            delete(KnowledgeNodeSupertag).where(
                KnowledgeNodeSupertag.node_id.in_(linked_node_ids),
                KnowledgeNodeSupertag.supertag_id.in_(task_tag_ids),
            )
        )

    async def reorder_tasks(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        project_id: UUID,
        task_ids: list[UUID],
    ) -> None:
        await self.require_project_permission(
            session, project_id=project_id, user_id=user_id, permission="write"
        )
        result = await session.execute(
            select(Task.id).where(
                Task.project_id == project_id,
                Task.parent_task_id.is_(None),
                Task.deleted_at.is_(None),
            )
        )
        top_level_ids = set(result.scalars().all())
        requested_ids = set(task_ids)
        if top_level_ids != requested_ids:
            raise TaskManagementError(
                "task_ids must include every top-level task in the project",
                status_code=409,
            )

        for index, task_id in enumerate(task_ids):
            await session.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.project_id == project_id,
                    Task.parent_task_id.is_(None),
                )
                .values(sort_order=float(index))
            )
        _assert_generation_mutation_allowed()
        await session.commit()

    async def reorder_tasks_global(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_ids: list[UUID],
    ) -> None:
        """ALL 表示のトップレベルタスク並び替え（プロジェクト横断）。

        Web BFF の POST /api/tasks/reorder と同じ契約:
        - 重複は除去し、トップレベルかつ未削除のタスクのみ許可
        - 対象タスクが属する全プロジェクトに write 権限が必要
        """
        unique_ids: list[UUID] = []
        seen: set[UUID] = set()
        for task_id in task_ids:
            if task_id in seen:
                continue
            seen.add(task_id)
            unique_ids.append(task_id)
        if not unique_ids:
            return

        result = await session.execute(
            select(Task).where(
                Task.id.in_(unique_ids),
                Task.parent_task_id.is_(None),
                Task.deleted_at.is_(None),
            )
        )
        tasks_by_id = {task.id: task for task in result.scalars().all()}
        if len(tasks_by_id) != len(unique_ids):
            raise TaskManagementError(
                "task_ids contains a non top-level or missing task",
                status_code=400,
            )

        for project_id in {task.project_id for task in tasks_by_id.values()}:
            await self.require_project_permission(
                session, project_id=project_id, user_id=user_id, permission="write"
            )

        for index, task_id in enumerate(unique_ids):
            tasks_by_id[task_id].sort_order = float(index)
        _assert_generation_mutation_allowed()
        await session.commit()

    async def get_task(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: UUID,
        browse_project_id: Optional[UUID | str] = None,
        browse_space_id: Optional[UUID | str] = None,
    ) -> dict[str, Any]:
        task = await self._load_task(session, task_id)
        scoped_project_ids: list[UUID] | None = None
        if browse_project_id is not None or browse_space_id is not None:
            # Resolve the explicit target before checking the task's project ACL
            # so an out-of-scope/inaccessible task is consistently indistinguishable
            # from a missing task (404), rather than leaking a 403.
            scoped_project_ids = await self.resolve_browse_project_ids(
                session,
                user_id=user_id,
                browse_project_id=browse_project_id,
                browse_space_id=browse_space_id,
            )
            if task.project_id not in scoped_project_ids:
                raise TaskManagementError("Task not found", status_code=404)
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="read"
        )

        active_entry = await self.get_active_time_entry(
            session, user_id=user_id, task_id=task.id
        )
        result = task.to_dict()
        result["comments"] = [comment.to_dict() for comment in task.comments]
        result["activities"] = [activity.to_dict() for activity in task.activities]
        result["occurrences"] = [
            occurrence.to_dict()
            for occurrence in sorted(task.occurrences, key=lambda item: item.start_at)
            if occurrence.deleted_at is None
        ]
        # Keep task detail entries on the same timer serializer as
        # list/active/start/stop responses (including explicit API offsets and
        # original_* metadata normalization).
        result["time_entries"] = [
            self._build_time_entry_payload(entry)
            for entry in task.time_entries
            if entry.deleted_at is None
        ]
        result["active_time_entry"] = active_entry
        return result

    async def update_task(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: UUID,
        updates: dict[str, Any],
        close_incomplete_subtasks: bool = False,
        commit: bool = True,
    ) -> dict[str, Any]:
        requested_status = updates.get("status")
        is_close_request = (
            requested_status is not None
            and normalize_task_status(str(requested_status)) == "closed"
        )
        # Never take the task row lock before the project advisory lock.  Move
        # callers (REST, sync, and the agent tool) all use
        # ``prepare_task_project_move`` which acquires the sorted project lock
        # namespace first, avoiding a move/dependency/schedule deadlock.
        task = await self._load_task(session, task_id)
        # A few legacy callers provide a lightweight task double only through
        # ``_load_task_for_update`` for close requests. Keep that compatibility
        # path while real ORM tasks always carry ``project_id`` and therefore
        # still follow the advisory-lock-first move path below.
        if is_close_request and not hasattr(task, "project_id"):
            task = await self._load_task_for_update(session, task_id)
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )

        target_project_id = task.project_id
        if "project_id" in updates and updates["project_id"] is not None:
            target_project_id = await self._resolve_project_id(
                session,
                user_id=user_id,
                project_id=updates["project_id"],
                require_write=True,
            )
        await self._lock_project_acl_for_task_write(
            session,
            project_ids=(task.project_id, target_project_id),
            user_id=user_id,
        )
        if isinstance(session, AsyncSession):
            # Every ORM write must lock the Task only after the project
            # advisory/ACL rows above.  Title-only updates used to rely on the
            # initial identity read and therefore flushed a Task row while a
            # concurrent TS/Python writer held the opposite Project/Task lock
            # order.  Taking the row lock unconditionally also serializes the
            # Docs-node binding path without orphaning a concurrently-created
            # node.
            locked_task = await self._load_task_for_update(session, task_id)
            if locked_task.project_id != task.project_id:
                raise TaskManagementError(
                    "TaskのProjectが同時変更されたため更新できません",
                    status_code=409,
                )
            requested_binding = self._coerce_uuid(updates.get("knowledge_node_id"))
            locked_binding = self._coerce_uuid(locked_task.knowledge_node_id)
            prior_binding = self._coerce_uuid(task.knowledge_node_id)
            if (
                locked_binding != prior_binding
                and locked_binding != requested_binding
            ):
                raise TaskManagementError(
                    "TaskのDocs node bindingが同時変更されたため更新できません",
                    status_code=409,
                )
            task = locked_task
            if "project_id" not in updates or updates.get("project_id") is None:
                target_project_id = task.project_id
        project_will_change = target_project_id != task.project_id
        requested_parent_id = (
            updates.get("parent_task_id")
            if "parent_task_id" in updates
            else (None if project_will_change else task.parent_task_id)
        )
        locked_parent: Task | None = None
        if not project_will_change and "parent_task_id" in updates:
            # Same-project reparenting must use the same advisory-lock-first
            # protocol as project moves.  The helper re-reads both rows after
            # locking and rejects a parent whose project changed meanwhile.
            task, locked_parent = await prepare_task_parent_update(
                session,
                task_id=task.id,
                expected_project_id=task.project_id,
                target_parent_task_id=requested_parent_id,
            )
        if is_close_request and not project_will_change:
            # Same-project closes retain the historical confirmation semantics,
            # but acquire the parent row lock before touching its children.
            task = await self._load_task_for_update(session, task_id)

        incomplete_children: list[Task] = []
        if is_close_request and not project_will_change:
            children_result = await session.execute(
                select(Task)
                .where(
                    Task.parent_task_id == task.id,
                    Task.deleted_at.is_(None),
                )
                .with_for_update()
            )
            direct_children = list(children_result.scalars().all())
            incomplete_children = [
                child
                for child in direct_children
                if normalize_task_status(child.status) != "closed"
            ]
            for project_id in {
                child.project_id for child in incomplete_children
            }:
                await self.require_project_permission(
                    session,
                    project_id=project_id,
                    user_id=user_id,
                    permission="write",
                )
            if incomplete_children and not close_incomplete_subtasks:
                subtasks = [
                    {
                        "id": str(child.id),
                        "title": child.title,
                        "status": normalize_task_status(child.status),
                    }
                    for child in incomplete_children
                ]
                raise TaskManagementError(
                    "未完了のサブタスクがあります",
                    status_code=409,
                    detail={
                        "code": "incomplete_subtasks_confirmation_required",
                        "detail": "未完了のサブタスクがあります",
                        "incomplete_subtasks": subtasks,
                    },
                )

        previous_task_status = normalize_task_status(task.status)
        parent_was_closed = previous_task_status == "closed"
        completion_time = datetime.utcnow()

        next_knowledge_node_id = (
            updates.get("knowledge_node_id")
            if "knowledge_node_id" in updates
            else task.knowledge_node_id
        )
        if (
            next_knowledge_node_id is not None
            and ("knowledge_node_id" in updates or "project_id" in updates)
        ):
            await self._validate_knowledge_node_binding(
                session,
                knowledge_node_id=next_knowledge_node_id,
                task_project_id=target_project_id,
                user_id=user_id,
            )
        if project_will_change:
            task, target_parent = await prepare_task_project_move(
                session,
                task_id=task.id,
                expected_project_id=task.project_id,
                target_project_id=target_project_id,
                target_parent_task_id=requested_parent_id,
            )
            if task.knowledge_node_id is not None:
                # A Project move must never leave a Docs-bound task pointing
                # at the old Project's node.  The helper above re-reads the
                # locked Task after the advisory Project locks, so this also
                # rejects a binding that raced in after the initial
                # validation (including an explicit unbind payload).
                raise TaskManagementError(
                    "Docs連携済みタスクは先にDocs連携を解除してからProjectを変更してください",
                    status_code=409,
                )
            locked_parent = target_parent
            if requested_parent_id is not None:
                task.parent_task_id = target_parent.id if target_parent else None
            # Moving a task with no explicit parent clears its existing parent
            # in the same transaction (the invariant helper has already done
            # so while the task row is locked).
        if "project_id" in updates and updates["project_id"] is not None:
            task.project_id = target_project_id

        if "title" in updates and updates["title"] is not None:
            task.title = _normalize_task_title(str(updates["title"]))
        if "description" in updates:
            task.description = updates["description"]
        if "knowledge_node_id" in updates:
            task.knowledge_node_id = updates["knowledge_node_id"]
        if "status" in updates and updates["status"] is not None:
            task.status = normalize_task_status(updates["status"])
            if task.status == "closed":
                if not parent_was_closed or task.completed_at is None:
                    task.completed_at = completion_time
            else:
                task.completed_at = None
        if "priority" in updates and updates["priority"] is not None:
            task.priority = normalize_priority(updates["priority"])
        if "start_at" in updates:
            task.start_at = updates["start_at"]
        if "end_at" in updates:
            task.end_at = updates["end_at"]
        if "all_day" in updates and updates["all_day"] is not None:
            task.all_day = bool(updates["all_day"])
        if "auto_close_on_due" in updates and updates["auto_close_on_due"] is not None:
            task.auto_close_on_due = bool(updates["auto_close_on_due"])
        if (
            "notifications_enabled" in updates
            and updates["notifications_enabled"] is not None
        ):
            task.notifications_enabled = bool(updates["notifications_enabled"])
        if "reminder_offsets" in updates and updates["reminder_offsets"] is not None:
            task.reminder_offsets = _ensure_reminder_offsets(
                updates["reminder_offsets"],
                default=[],
            )
        if "estimated_hours" in updates:
            task.estimated_hours = updates["estimated_hours"]
        if "parent_task_id" in updates:
            next_parent_id = updates["parent_task_id"]
            if next_parent_id == task.id:
                raise TaskManagementError("Task cannot be its own parent", status_code=400)
            if next_parent_id is not None:
                parent = locked_parent
                if parent is None or parent.id != next_parent_id:
                    # Compatibility fallback for lightweight service doubles;
                    # real writes above always lock the requested parent.
                    parent = await self._load_task(session, next_parent_id)
                if parent.project_id != task.project_id:
                    raise TaskManagementError(
                        "Subtask parent must belong to the same project", status_code=400
                    )
            task.parent_task_id = next_parent_id
        if "task_metadata" in updates and updates["task_metadata"] is not None:
            merged_metadata = dict(task.task_metadata or {})
            merged_metadata.update(updates["task_metadata"])
            task.task_metadata = merged_metadata
        recurrence = await self._upsert_recurrence(
            session,
            task=task,
            recurrence_rrule=updates.get(
                "recurrence_rrule",
                task.recurrence_rule.rrule if task.recurrence_rule else None,
            ),
            timezone=updates.get(
                "recurrence_timezone",
                task.recurrence_rule.timezone
                if task.recurrence_rule
                else DEFAULT_TASK_TIMEZONE,
            ),
        )

        if "assignee_ids" in updates and updates["assignee_ids"] is not None:
            await self._replace_assignees(
                session,
                task=task,
                assignee_ids=list(updates["assignee_ids"]),
                assigned_by=user_id,
            )

        if "tag_ids" in updates and updates["tag_ids"] is not None:
            await self._replace_tags(
                session, task=task, tag_ids=list(updates["tag_ids"])
            )
        await self._sync_repeat_tag(
            session, task=task, has_recurrence=recurrence is not None
        )

        await self._materialize_occurrences(
            session,
            task,
            recurrence_rrule=recurrence.rrule if recurrence else None,
            horizon_days=recurrence.horizon_days if recurrence else 90,
            skip_weekend=bool(recurrence.skip_weekend) if recurrence else False,
            skip_holiday=bool(recurrence.skip_holiday) if recurrence else False,
            skip_mode=recurrence.skip_mode if recurrence else None,
        )
        completed_children: list[tuple[Task, Any]] = []
        for child in incomplete_children:
            previous_status = normalize_task_status(child.status)
            child.status = "closed"
            child.completed_at = completion_time
            child.updated_at = completion_time
            child_activity = await self._record_activity(
                session,
                task_id=child.id,
                activity_type="closed_by_parent",
                user_id=user_id,
                payload={
                    "parent_task_id": str(task.id),
                    "previous_status": previous_status,
                },
            )
            if previous_status not in {"closed", "cancelled"}:
                completed_children.append((child, child_activity))

        task_became_closed = (
            previous_task_status not in {"closed", "cancelled"}
            and normalize_task_status(task.status) == "closed"
        )
        is_idempotent_close_replay = (
            parent_was_closed
            and not incomplete_children
            and set(updates) == {"status"}
        )
        task_activity = None
        if not is_idempotent_close_replay:
            activity_payload = {
                key: str(value)
                for key, value in updates.items()
                if value is not None and key != "status"
            }
            if "status" in updates and updates.get("status") is not None:
                # A status marker is a completion/lifecycle event only when
                # the row actually changed status.  In particular, a
                # closed->closed metadata request must not look like a new
                # completion episode to recovery.
                if task_became_closed:
                    activity_payload["status"] = "closed"
                elif previous_task_status != normalize_task_status(task.status):
                    activity_payload["status"] = normalize_task_status(task.status)
            if incomplete_children:
                activity_payload["closed_incomplete_subtask_count"] = len(
                    incomplete_children
                )
            task_activity = await self._record_activity(
                session,
                task_id=task.id,
                activity_type="task_updated",
                user_id=user_id,
                payload=activity_payload,
            )

        # Knowledge Capture is a durable enqueue only.  Keep this local
        # import out of the task-management module graph and do not run any
        # LLM/research work in the Task transaction.
        if task_became_closed:
            from ..knowledge_capture_candidate_service import (
                enqueue_for_completed_task,
            )

            activity_id = getattr(task_activity, "id", None)
            if not isinstance(activity_id, UUID):
                activity_id = None
            enqueue_kwargs = {"trigger_user_id": user_id}
            if activity_id is not None:
                enqueue_kwargs["task_activity_id"] = activity_id
            await enqueue_for_completed_task(session, task, **enqueue_kwargs)
        if completed_children:
            from ..knowledge_capture_candidate_service import (
                enqueue_for_completed_task,
            )

            for child, child_activity in completed_children:
                activity_id = getattr(child_activity, "id", None)
                if not isinstance(activity_id, UUID):
                    activity_id = None
                enqueue_kwargs = {"trigger_user_id": user_id}
                if activity_id is not None:
                    enqueue_kwargs["task_activity_id"] = activity_id
                await enqueue_for_completed_task(session, child, **enqueue_kwargs)
        if "title" in updates and task.knowledge_node_id is not None:
            await self._sync_bound_docs_node_title(
                session,
                task=task,
                user_id=user_id,
            )
        if commit:
            _assert_generation_mutation_allowed()
            await session.commit()
        task = await self._load_task(session, task.id)
        payload = task.to_dict()
        if commit:
            await self._broadcast("task_updated", payload)
        return payload

    async def _sync_bound_docs_node_title(
        self,
        session: AsyncSession,
        *,
        task: Task,
        user_id: UUID,
    ) -> None:
        task_project_id = self._coerce_uuid(getattr(task, "project_id", None))
        if task_project_id is None:
            return
        try:
            node = await self._validate_knowledge_node_binding(
                session,
                knowledge_node_id=task.knowledge_node_id,
                task_project_id=task_project_id,
                user_id=user_id,
            )
        except TaskManagementError as exc:
            # A revoked personal share or deleted/archived target is an
            # expected stale binding; leave the Docs row untouched and keep
            # the task update usable.  Canonical identity conflicts are a
            # deterministic 409 and must remain visible to the caller.
            if exc.status_code in {403, 404}:
                return
            raise
        if node.title == task.title:
            return
        node.title = task.title
        node.updated_by = user_id
        node.updated_at = datetime.utcnow()

        # ``tasks.py`` is a submodule of ``services.task_management`` while
        # DocsGraphService lives in ``services``.  The previous single-dot
        # import resolved to the non-existent
        # ``src.services.task_management.docs_graph_service`` package path and
        # made otherwise valid task updates fail when title sync was needed.
        from ..docs_graph_service import DocsGraphService

        await DocsGraphService(session).record_node_change(
            node,
            user_id,
            "タスクタイトルをDocs nodeへ同期",
        )

    async def list_tags(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        user_id: UUID,
        browse_project_id: Optional[UUID | str] = None,
        browse_space_id: Optional[UUID | str] = None,
    ) -> list[dict[str, Any]]:
        if browse_project_id is not None or browse_space_id is not None:
            scoped_project_ids = await self.resolve_browse_project_ids(
                session,
                user_id=user_id,
                browse_project_id=browse_project_id,
                browse_space_id=browse_space_id,
            )
            if project_id not in scoped_project_ids:
                raise TaskManagementError("Browse target not found", status_code=404)
        else:
            await self.require_project_permission(
                session, project_id=project_id, user_id=user_id, permission="read"
            )
        space_id = await self._get_project_space_id(session, project_id=project_id)
        if space_id is None:
            return []
        tag_query = select(Tag).where(Tag.space_id == space_id)
        if browse_project_id is not None or browse_space_id is not None:
            # Tags are Space-owned, but a browse detail must not turn the
            # project tag catalog into a sibling-project metadata oracle. Only
            # tags actually attached to live tasks in the requested Project are
            # returned for an explicit browse.
            tag_query = (
                tag_query.join(TaskTag, TaskTag.tag_id == Tag.id)
                .join(Task, Task.id == TaskTag.task_id)
                .where(Task.project_id == project_id, Task.deleted_at.is_(None))
                .distinct()
            )
        result = await session.execute(tag_query.order_by(Tag.name))
        tags = []
        for tag in result.scalars().all():
            payload = tag.to_dict()
            payload["project_id"] = str(project_id)
            tags.append(payload)
        return tags

    async def create_tag(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        name: str,
        color: Optional[str],
        user_id: UUID,
    ) -> dict[str, Any]:
        await self.require_project_permission(
            session, project_id=project_id, user_id=user_id, permission="write"
        )
        name = name.strip()
        if not name:
            raise TaskManagementError("タグ名は必須です", status_code=400)
        space_id = await self._ensure_project_space_id(session, project_id=project_id)
        existing = await session.execute(
            select(Tag).where(Tag.space_id == space_id, Tag.name == name)
        )
        if existing.scalar_one_or_none() is not None:
            raise TaskManagementError("同名のタグが既に存在します", status_code=409)
        tag = Tag(space_id=space_id, name=name, color=color, created_by=user_id)
        session.add(tag)
        _assert_generation_mutation_allowed()
        await session.commit()
        await session.refresh(tag)
        payload = tag.to_dict()
        payload["project_id"] = str(project_id)
        return payload

    async def delete_tag(
        self,
        session: AsyncSession,
        *,
        tag_id: UUID,
        user_id: UUID,
    ) -> None:
        result = await session.execute(select(Tag).where(Tag.id == tag_id))
        tag = result.scalar_one_or_none()
        if tag is None:
            raise TaskManagementError("タグが見つかりません", status_code=404)
        await self._require_space_tag_permission(
            session, space_id=tag.space_id, user_id=user_id, permission="write"
        )
        await session.delete(tag)
        _assert_generation_mutation_allowed()
        await session.commit()

    async def add_comment(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_id: UUID,
        content: str,
    ) -> dict[str, Any]:
        task = await self._load_task(session, task_id)
        await self.require_project_permission(
            session, project_id=task.project_id, user_id=user_id, permission="write"
        )

        comment = TaskComment(task_id=task.id, user_id=user_id, content=content.strip())
        session.add(comment)
        await self._record_activity(
            session,
            task_id=task.id,
            activity_type="comment_added",
            user_id=user_id,
            payload={"content": content.strip()},
        )
        _assert_generation_mutation_allowed()
        await session.commit()
        await session.refresh(comment)
        await self._broadcast("task_comment_added", comment.to_dict())
        return comment.to_dict()
