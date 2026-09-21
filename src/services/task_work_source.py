"""Canonical WorkSource for existing AoiTalk Tasks.

The source is deliberately read-only with respect to Tasks.  It discovers a
Task only when an explicit active :class:`AgentTaskAssignment` and matching
:class:`AgentProjectGrant` exist; Space membership or text in the task cannot
grant authority.  Task mutations remain owned by ``TaskManagementService``
and the normal project ACL boundary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import selectinload

from ..memory.models import (
    Agent,
    AgentProjectGrant,
    AgentRevision,
    AgentSpaceAssignment,
    AgentTaskAssignment,
    Project,
    Task,
    TaskDependency,
)
from .agent_team_v3 import AGENT_TEAM_CAPABILITY_CATALOG
from .agent_work_runtime import WorkCandidate, WorkClaim
from .project_permissions import normalize_project_member_permissions


TASK_TERMINAL_STATES = frozenset({"completed", "complete", "closed", "cancelled", "canceled", "archived", "done"})
TASK_EXECUTABLE_STATES = frozenset({"todo", "open", "in_progress", "ready", "review", "pending"})


def _uuid(value: Any) -> str | None:
    return str(value) if value is not None else None


def _normalized_state(value: Any) -> str:
    return str(value or "").strip().casefold()


def _priority(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return {"urgent": 100, "high": 75, "medium": 50, "normal": 50, "low": 25}.get(
        str(value or "").strip().casefold(), 0
    )


def _revision(task: Task, assignment: AgentTaskAssignment, grant: AgentProjectGrant, revision: AgentRevision | None = None) -> str:
    payload = {
        "task_id": _uuid(task.id),
        "project_id": _uuid(task.project_id),
        "status": _normalized_state(task.status),
        "priority": _priority(task.priority),
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "assignment_id": _uuid(assignment.id),
        "assignment_state": assignment.state,
        "grant_id": _uuid(grant.id),
        "grant_state": grant.state,
        "agent_revision_id": _uuid(revision.id) if revision is not None else None,
        "agent_revision_version": int(revision.version or 0) if revision is not None else None,
        "agent_revision_hash": str(revision.content_hash or "") if revision is not None else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _metadata(task: Task) -> dict[str, Any]:
    raw = getattr(task, "task_metadata", None)
    return dict(raw) if isinstance(raw, Mapping) else {}


class TaskWorkSource:
    """Discover durable autonomous work from canonical Tasks."""

    source_type = "task"

    def __init__(self, *, include_harness_enabled: bool = True, domain: str = "internal") -> None:
        self.include_harness_enabled = bool(include_harness_enabled)
        self.domain = str(domain or "internal")[:80]

    async def discover(self, session: Any, *, now: datetime | None = None) -> list[WorkCandidate]:
        now = now or datetime.utcnow()
        result = await session.execute(
            select(Task)
            .options(selectinload(Task.project))
            .where(Task.deleted_at.is_(None), Task.archived_at.is_(None))
            .order_by(Task.created_at, Task.id)
        )
        candidates: list[WorkCandidate] = []
        for task in result.scalars().all():
            state = _normalized_state(task.status)
            if state not in TASK_EXECUTABLE_STATES or state in TASK_TERMINAL_STATES:
                continue
            if getattr(task, "completed_at", None) is not None:
                continue
            project = getattr(task, "project", None)
            if project is None or getattr(project, "deleted_at", None) is not None or bool(getattr(project, "is_completed", False)):
                continue
            metadata = _metadata(task)
            if not self.include_harness_enabled and not bool(metadata.get("autonomous_enabled")):
                continue
            if not await self._dependencies_ready(session, task):
                continue
            assignments = (
                await session.execute(
                    select(AgentTaskAssignment)
                    .where(
                        AgentTaskAssignment.task_id == task.id,
                        AgentTaskAssignment.assignment_role.in_(("owner", "executor")),
                        AgentTaskAssignment.state == "active",
                        or_(AgentTaskAssignment.active_from.is_(None), AgentTaskAssignment.active_from <= now),
                        or_(AgentTaskAssignment.active_until.is_(None), AgentTaskAssignment.active_until > now),
                    )
                    .order_by(AgentTaskAssignment.created_at, AgentTaskAssignment.id)
                )
            ).scalars().all()
            for assignment in assignments:
                agent = await session.get(Agent, assignment.agent_id)
                if agent is None or str(agent.state or "") != "active":
                    continue
                grant = (
                    await session.execute(
                        select(AgentProjectGrant)
                        .where(
                            AgentProjectGrant.project_id == task.project_id,
                            AgentProjectGrant.agent_id == assignment.agent_id,
                            AgentProjectGrant.state == "active",
                            or_(AgentProjectGrant.active_from.is_(None), AgentProjectGrant.active_from <= now),
                            or_(AgentProjectGrant.active_until.is_(None), AgentProjectGrant.active_until > now),
                        )
                        .order_by(AgentProjectGrant.created_at.desc())
                    )
                ).scalars().first()
                if grant is None:
                    continue
                permissions = normalize_project_member_permissions(getattr(grant, "permissions", None))
                if permissions.get("write") is not True and permissions.get("delete") is not True:
                    continue
                if project.space_id is not None:
                    space_assignment = (
                        await session.execute(
                            select(AgentSpaceAssignment).where(
                                AgentSpaceAssignment.agent_id == agent.id,
                                AgentSpaceAssignment.space_id == project.space_id,
                                AgentSpaceAssignment.state == "active",
                                or_(AgentSpaceAssignment.active_from.is_(None), AgentSpaceAssignment.active_from <= now),
                                or_(AgentSpaceAssignment.active_until.is_(None), AgentSpaceAssignment.active_until > now),
                            )
                        )
                    ).scalars().first()
                    if space_assignment is None:
                        continue
                revision = (
                    await session.execute(
                        select(AgentRevision)
                        .where(AgentRevision.agent_id == agent.id)
                        .order_by(AgentRevision.version.desc())
                        .limit(1)
                    )
                ).scalars().first()
                if revision is None:
                    continue
                raw_caps = metadata.get("required_capabilities", metadata.get("capabilities", []))
                if not raw_caps and metadata.get("agent_harness_enabled"):
                    raw_caps = ["workspace_read", "workspace_write", "command_execute"]
                if not raw_caps and not metadata.get("agent_harness_enabled"):
                    # Task execution is a Project mutation; a read-only
                    # grant must never become an executable candidate merely
                    # because the metadata omitted a capability list.
                    raw_caps = ["project_write"]
                if not isinstance(raw_caps, Sequence) or isinstance(raw_caps, (str, bytes)):
                    continue
                if any(str(item).strip() not in AGENT_TEAM_CAPABILITY_CATALOG for item in raw_caps):
                    continue
                caps = tuple(
                    dict.fromkeys(
                        str(item).strip()
                        for item in (raw_caps if isinstance(raw_caps, Sequence) and not isinstance(raw_caps, (str, bytes)) else [])
                        if str(item).strip() in AGENT_TEAM_CAPABILITY_CATALOG
                    )
                )[:32]
                adapter = str(metadata.get("execution_adapter") or ("code_agent" if metadata.get("agent_harness_enabled") else "task"))[:120]
                intent = str(metadata.get("autonomous_intent_key") or metadata.get("intent_key") or "task.execute")[:255]
                concurrency = str(metadata.get("concurrency_key") or f"task:{task.id}")[:255]
                candidates.append(
                    WorkCandidate(
                        source_type=self.source_type,
                        source_id=str(task.id),
                        source_revision=_revision(task, assignment, grant, revision),
                        intent_key=intent,
                        domain=self.domain,
                        space_id=_uuid(getattr(getattr(task, "project", None), "space_id", None)),
                        project_id=_uuid(task.project_id),
                        task_id=_uuid(task.id),
                        assigned_agent_id=_uuid(agent.id),
                        agent_revision_id=_uuid(revision.id) if revision else None,
                        required_capabilities=caps,
                        execution_adapter=adapter,
                        priority=_priority(task.priority),
                        not_before=getattr(task, "start_at", None),
                        deadline=getattr(task, "end_at", None),
                        max_attempts=_safe_attempts(metadata.get("max_attempts")),
                        concurrency_key=concurrency,
                        causation_id=str(metadata.get("causation_id") or "")[:255] or None,
                        causal_depth=int(metadata.get("causal_depth") or 0),
                        mutation_fingerprint=str(metadata.get("mutation_fingerprint") or "")[:64] or None,
                        metadata={
                        "task_title": str(task.title or "")[:240],
                            "description": str(task.description or "")[:2_000],
                            "assignment_role": str(assignment.assignment_role or "")[:32],
                            "project_role": str(grant.role or "")[:32],
                            "identifier": str(metadata.get("identifier") or "")[:120],
                        },
                    )
                )
        return candidates

    async def refresh(self, session: Any, claim: WorkClaim) -> bool:
        """Revalidate task, assignment, grant and dependencies before settle."""

        task_key = claim.task_id or claim.source_id
        try:
            task_key = UUID(str(task_key))
        except (TypeError, ValueError):
            return False
        task = await session.get(Task, task_key)
        if task is None or task.deleted_at is not None or task.archived_at is not None:
            return False
        project = await session.get(Project, task.project_id)
        if project is None or getattr(project, "deleted_at", None) is not None or bool(getattr(project, "is_completed", False)):
            return False
        if _normalized_state(task.status) not in TASK_EXECUTABLE_STATES or getattr(task, "completed_at", None) is not None:
            return False
        now = datetime.utcnow()
        if getattr(task, "start_at", None) is not None and task.start_at > now:
            return False
        if getattr(task, "end_at", None) is not None and task.end_at <= now:
            return False
        if not await self._dependencies_ready(session, task):
            return False
        if not claim.assigned_agent_id:
            return False
        try:
            agent_key = UUID(str(claim.assigned_agent_id))
        except (TypeError, ValueError):
            return False
        agent = await session.get(Agent, agent_key)
        if agent is None or str(agent.state or "") != "active":
            return False
        assignment = (
            await session.execute(
                select(AgentTaskAssignment).where(
                    AgentTaskAssignment.task_id == task.id,
                    AgentTaskAssignment.agent_id == agent.id,
                    AgentTaskAssignment.state == "active",
                    AgentTaskAssignment.assignment_role.in_(("owner", "executor")),
                    or_(AgentTaskAssignment.active_from.is_(None), AgentTaskAssignment.active_from <= now),
                    or_(AgentTaskAssignment.active_until.is_(None), AgentTaskAssignment.active_until > now),
                )
            )
        ).scalars().first()
        if assignment is None:
            return False
        grant = (
            await session.execute(
                select(AgentProjectGrant).where(
                    AgentProjectGrant.project_id == task.project_id,
                    AgentProjectGrant.agent_id == agent.id,
                    AgentProjectGrant.state == "active",
                    or_(AgentProjectGrant.active_from.is_(None), AgentProjectGrant.active_from <= now),
                    or_(AgentProjectGrant.active_until.is_(None), AgentProjectGrant.active_until > now),
                )
            )
        ).scalars().first()
        if grant is None:
            return False
        permissions = normalize_project_member_permissions(getattr(grant, "permissions", None))
        if permissions.get("write") is not True and permissions.get("delete") is not True:
            return False
        if project.space_id is not None:
            space_assignment = (
                await session.execute(
                    select(AgentSpaceAssignment).where(
                        AgentSpaceAssignment.agent_id == agent.id,
                        AgentSpaceAssignment.space_id == project.space_id,
                        AgentSpaceAssignment.state == "active",
                        or_(AgentSpaceAssignment.active_from.is_(None), AgentSpaceAssignment.active_from <= now),
                        or_(AgentSpaceAssignment.active_until.is_(None), AgentSpaceAssignment.active_until > now),
                    )
                )
            ).scalars().first()
            if space_assignment is None:
                return False
        if claim.source_revision:
            revision = (
                await session.execute(
                    select(AgentRevision)
                    .where(AgentRevision.agent_id == agent.id)
                    .order_by(AgentRevision.version.desc())
                    .limit(1)
                )
            ).scalars().first()
            current_revision = _revision(task, assignment, grant, revision)
            if str(current_revision) != str(claim.source_revision):
                return False
        return True

    async def _dependencies_ready(self, session: Any, task: Task) -> bool:
        dependencies = (
            await session.execute(
                select(Task).join(TaskDependency, TaskDependency.depends_on_task_id == Task.id).where(TaskDependency.task_id == task.id)
            )
        ).scalars().all()
        for dependency in dependencies:
            if dependency.deleted_at is not None or dependency.archived_at is not None:
                return False
            if _normalized_state(dependency.status) not in TASK_TERMINAL_STATES:
                return False
        return True


def _safe_attempts(value: Any) -> int:
    try:
        return max(1, min(int(value or 3), 100))
    except (TypeError, ValueError):
        return 3


# Historical naming used by early work-source prototypes.
BuiltInTaskWorkSource = TaskWorkSource

__all__ = ["TaskWorkSource", "BuiltInTaskWorkSource", "TASK_TERMINAL_STATES", "TASK_EXECUTABLE_STATES"]
