"""Read-only Operations Command Center projection.

The projection composes existing ledgers (Tasks, AgentWork, AgentRuns,
ExternalActions and activity rows).  It deliberately owns no mutable
workflow state and omits provider payloads, prompts, credentials and lease
tokens from every response.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import desc, or_, select

from ..memory.models import (
    Agent,
    AgentOrganizationProfile,
    AgentProjectGrant,
    AgentRevision,
    AgentSpaceAssignment,
    AgentRun,
    AgentRunEvent,
    AgentRunToolCall,
    AgentWorkEvent,
    AgentWorkItem,
    ExternalAction,
    OperationEvent,
    Project,
    ProjectMember,
    Space,
    Task,
    TaskActivity,
    HeartbeatRunHistory,
    Persona,
    Organization,
    PersonaOperatorAssignment,
    User,
)
from ..features import Features
from .project_permissions import normalize_project_member_permissions
from .operations_employee_observability import (
    causal_activity, employee_summary, fields, optional_rows, scoped, work_metadata,
)


def _uuid(value: Any) -> UUID | None:
    if value in (None, ""):
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _actor_value(actor: Any, name: str, default: Any = None) -> Any:
    if isinstance(actor, Mapping):
        return actor.get(name, default)
    return getattr(actor, name, default)


def _is_admin(actor: Any) -> bool:
    return str(_actor_value(actor, "role", "")).strip().casefold() == "admin"


def _safe_row(row: Any) -> dict[str, Any]:
    if isinstance(row, Task):
        # Task.to_dict traverses several lazy relationships and can trigger a
        # MissingGreenlet in an async read projection. Keep the command-center
        # task card deliberately flat and bounded.
        return _safe_value(
            {
                "id": str(row.id) if getattr(row, "id", None) else None,
                "project_id": (
                    str(row.project_id) if getattr(row, "project_id", None) else None
                ),
                "title": getattr(row, "title", None),
                "description": getattr(row, "description", None),
                "status": getattr(row, "status", None),
                "priority": getattr(row, "priority", None),
                "start_at": getattr(row, "start_at", None),
                "end_at": getattr(row, "end_at", None),
                "created_at": getattr(row, "created_at", None),
                "updated_at": getattr(row, "updated_at", None),
            }
        )
    method = getattr(row, "to_safe_dict", None) or getattr(row, "to_dict", None)
    if callable(method):
        try:
            value = method()
            return _safe_value(dict(value)) if isinstance(value, Mapping) else {}
        except Exception:
            return {}
    return {}


_SECRET_KEYS = frozenset(
    {
        "prompt",
        "transcript",
        "secret",
        "token",
        "password",
        "credential",
        "authorization",
        "cookie",
        "api_key",
        "apikey",
        "provider_response",
        "raw_response",
        "environment",
        "filesystem",
        "file_path",
        "storage_path",
        "objective",
        "instructions",
        "mission",
        "operational_instructions",
        "allowed_subagent_ids",
        "run_metadata",
        "validation",
        "result",
        "payload",
    }
)


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    """Bound a ledger DTO before it enters the command-center response."""

    if depth > 4:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        lowered = text.casefold()
        if any(marker in lowered for marker in ("bearer ", "api_key", "password=", "token=", "secret=", "credential=")):
            return "[redacted]"
        if "@" in text and "://" in text:
            return "[redacted]"
        return text[:2048]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:128]:
            key = str(raw_key)
            normalized = key.casefold().replace("-", "_")
            if any(marker in normalized for marker in _SECRET_KEYS):
                continue
            projected = _safe_value(raw_value, depth=depth + 1)
            if projected is not None:
                result[key[:96]] = projected
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_value(item, depth=depth + 1) for item in list(value)[:128]]
    return None


def _active_at(row: Any, now: datetime) -> bool:
    state = str(getattr(row, "state", "") or "").casefold()
    if state and state != "active":
        return False
    active_from = getattr(row, "active_from", None)
    active_until = getattr(row, "active_until", None)
    try:
        if active_from is not None and active_from.replace(tzinfo=None) > now:
            return False
        if active_until is not None and active_until.replace(tzinfo=None) <= now:
            return False
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def _run_summary(row: Any) -> dict[str, Any]:
    """Expose lifecycle metadata without prompts/results/provider payloads."""

    safe = _safe_row(row)
    return {
        key: safe.get(key)
        for key in (
            "id",
            "run_id",
            "work_item_id",
            "agent_id",
            "agent_revision_id",
            "project_id",
            "task_id",
            "run_type",
            "status",
            "created_at",
            "updated_at",
            "started_at",
            "ended_at",
            "last_event_at",
            "error",
            "execution_manifest_hash",
        )
        if safe.get(key) is not None
    }


class OperationsCommandCenterService:
    """Compose ACL-scoped, read-only operational data."""

    def __init__(self, *, action_registry: Any = None):
        self.action_registry = action_registry

    async def project_scope(self, session: Any, actor: Any) -> set[UUID] | None:
        if _is_admin(actor):
            return None
        actor_id = _uuid(_actor_value(actor, "id") or _actor_value(actor, "user_id"))
        if actor_id is None:
            return set()
        owned = (await session.execute(select(Project.id).where(Project.owner_id == actor_id, Project.deleted_at.is_(None)))).scalars().all()
        member_rows = (await session.execute(select(ProjectMember).where(ProjectMember.user_id == actor_id, ProjectMember.deleted_at.is_(None) if hasattr(ProjectMember, "deleted_at") else True))).scalars().all()
        member = [row.project_id for row in member_rows if normalize_project_member_permissions(getattr(row, "permissions", None)).get("read") is True]
        return {value for value in [*owned, *member] if value is not None}

    async def snapshot(
        self,
        session: Any,
        actor: Any,
        *,
        space_id: Any = None,
        project_id: Any = None,
        agent_id: Any = None,
        domain: str | None = None,
        state: str | None = None,
        from_at: Any = None,
        to_at: Any = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        if not (Features.virtual_company() and Features.autonomous_agent_runtime()):
            # Manual Operations/MediaOps remain available through their
            # existing tabs; company Agent/Work projections are unadvertised
            # when the autonomous company capability is disabled.
            return self._empty_snapshot()
        safe_limit = max(1, min(int(limit or 100), 500))
        now = datetime.utcnow()
        organization_row = (
            await session.execute(
                select(Organization)
                .where(Organization.singleton_key == "installation")
                .limit(1)
            )
        ).scalars().first()
        organization_payload = _safe_row(organization_row) if organization_row else {}
        organization_payload = {
            key: organization_payload.get(key)
            for key in (
                "id",
                "display_name",
                "autonomy_level",
                "policy_version",
            )
            if organization_payload.get(key) is not None
        }
        organization_payload["singleton"] = True
        organization_payload.setdefault("display_name", "AoiTalk")
        requested_project = _uuid(project_id)
        requested_space = _uuid(space_id)
        requested_agent = _uuid(agent_id)
        allowed_projects = await self.project_scope(session, actor)
        if allowed_projects is not None and requested_project is not None and requested_project not in allowed_projects:
            return self._empty_snapshot()

        project_stmt = select(Project).where(Project.deleted_at.is_(None))
        if requested_project is not None:
            project_stmt = project_stmt.where(Project.id == requested_project)
        if requested_space is not None:
            project_stmt = project_stmt.where(Project.space_id == requested_space)
        if allowed_projects is not None:
            project_stmt = project_stmt.where(Project.id.in_(allowed_projects or {UUID(int=0)}))
        projects = list((await session.execute(project_stmt.order_by(Project.name).limit(safe_limit))).scalars().all())
        project_ids = {row.id for row in projects}
        space_ids = {row.space_id for row in projects if row.space_id} or {UUID(int=0)}
        spaces = list(
            (
                await session.execute(
                    select(Space)
                    .where(Space.id.in_(space_ids))
                    .order_by(Space.name)
                    .limit(safe_limit)
                )
            )
            .scalars()
            .all()
        )
        space_names = {str(row.id): row.name for row in spaces}
        project_names = {str(row.id): row.name for row in projects}

        task_stmt = select(Task).where(Task.deleted_at.is_(None), Task.archived_at.is_(None))
        if project_ids:
            task_stmt = task_stmt.where(Task.project_id.in_(project_ids))
        elif allowed_projects is not None:
            task_stmt = task_stmt.where(
                Task.project_id.in_(allowed_projects or {UUID(int=0)})
            )
        elif requested_project is not None or requested_space is not None:
            # A requested scope with no matching projects must not widen to
            # every task in the installation.
            task_stmt = task_stmt.where(Task.project_id == UUID(int=0))
        tasks = list((await session.execute(task_stmt.order_by(desc(Task.updated_at)).limit(safe_limit))).scalars().all())
        task_names = {
            str(row.id): getattr(row, "title", None)
            for row in tasks
            if getattr(row, "id", None)
        }

        agent_stmt = select(Agent)
        if requested_agent is not None:
            agent_stmt = agent_stmt.where(Agent.id == requested_agent)
        agents = list((await session.execute(agent_stmt.order_by(Agent.display_name).limit(safe_limit))).scalars().all())
        visible_agent_projects = (
            project_ids
            if requested_project is not None or requested_space is not None
            else allowed_projects
        )
        if visible_agent_projects is not None:
            grant_ids = set(
                (
                    await session.execute(
                        select(AgentProjectGrant.agent_id).where(
                            AgentProjectGrant.project_id.in_(
                                visible_agent_projects or {UUID(int=0)}
                            ),
                            AgentProjectGrant.state == "active",
                            or_(
                                AgentProjectGrant.active_from.is_(None),
                                AgentProjectGrant.active_from <= now,
                            ),
                            or_(
                                AgentProjectGrant.active_until.is_(None),
                                AgentProjectGrant.active_until > now,
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            agents = [row for row in agents if row.id in grant_ids]
        agent_names = {str(row.id): row.display_name for row in agents}
        agent_ids = {row.id for row in agents}

        agent_projections: list[dict[str, Any]] = []
        for agent in agents:
            safe_agent = _safe_row(agent)
            projection = {
                key: safe_agent.get(key)
                for key in ("id", "display_name", "slug", "state", "character_id")
                if safe_agent.get(key) is not None
            }
            profile = await session.get(AgentOrganizationProfile, agent.id)
            if profile is not None:
                safe_profile = _safe_row(profile)
                primary_space_id = safe_profile.get("primary_space_id")
                if (
                    visible_agent_projects is not None
                    and primary_space_id is not None
                    and _uuid(primary_space_id) not in space_ids
                ):
                    primary_space_id = None
                projection.update(
                    {
                        "job_title": safe_profile.get("job_title"),
                        "responsibility_summary": safe_profile.get("responsibility_summary"),
                        "primary_space_id": primary_space_id,
                        "autonomy_level": safe_profile.get("autonomy_level"),
                        "employment_state": safe_profile.get("employment_state"),
                        "budget": {
                            "configured": bool(
                                safe_profile.get("company_permission_ceiling")
                            )
                        },
                        "concurrency": {},
                    }
                )
            latest_revision = (await session.execute(select(AgentRevision).where(AgentRevision.agent_id == agent.id).order_by(AgentRevision.version.desc()).limit(1))).scalars().first()
            if latest_revision is not None:
                revision_payload = _safe_row(latest_revision)
                revision_payload = {
                    key: revision_payload.get(key)
                    for key in (
                        "id",
                        "agent_id",
                        "version",
                        "display_name",
                        "agent_team_id",
                        "execution_profile_id",
                        "capability_ceiling",
                        "content_hash",
                        "created_at",
                    )
                    if revision_payload.get(key) is not None
                }
                projection["latest_revision"] = revision_payload
                projection["agent_team_id"] = revision_payload.get("agent_team_id")
                projection["execution_profile_id"] = revision_payload.get("execution_profile_id")
                projection["capabilities"] = revision_payload.get("capability_ceiling") or []
            grant_stmt = select(AgentProjectGrant).where(
                AgentProjectGrant.agent_id == agent.id,
                AgentProjectGrant.state == "active",
                or_(
                    AgentProjectGrant.active_from.is_(None),
                    AgentProjectGrant.active_from <= now,
                ),
                or_(
                    AgentProjectGrant.active_until.is_(None),
                    AgentProjectGrant.active_until > now,
                ),
            )
            grants = (
                await session.execute(grant_stmt.limit(safe_limit))
            ).scalars().all()
            if visible_agent_projects is not None:
                grants = [
                    grant
                    for grant in grants
                    if visible_agent_projects is not None
                    and grant.project_id in visible_agent_projects
                ]
            projection["project_grants"] = [_safe_row(grant) for grant in grants]
            space_stmt = select(AgentSpaceAssignment).where(
                AgentSpaceAssignment.agent_id == agent.id,
                AgentSpaceAssignment.state == "active",
                or_(
                    AgentSpaceAssignment.active_from.is_(None),
                    AgentSpaceAssignment.active_from <= now,
                ),
                or_(
                    AgentSpaceAssignment.active_until.is_(None),
                    AgentSpaceAssignment.active_until > now,
                ),
            )
            if visible_agent_projects is not None:
                space_stmt = space_stmt.where(
                    AgentSpaceAssignment.space_id.in_(space_ids or {UUID(int=0)})
                )
            spaces_for_agent = (
                await session.execute(space_stmt.limit(safe_limit))
            ).scalars().all()
            projection["space_assignments"] = [_safe_row(item) for item in spaces_for_agent]
            persona_stmt = (
                select(PersonaOperatorAssignment)
                .join(Persona, Persona.id == PersonaOperatorAssignment.persona_id)
                .where(
                    PersonaOperatorAssignment.agent_id == agent.id,
                    PersonaOperatorAssignment.state == "active",
                    or_(
                        PersonaOperatorAssignment.active_from.is_(None),
                        PersonaOperatorAssignment.active_from <= now,
                    ),
                    or_(
                        PersonaOperatorAssignment.active_until.is_(None),
                        PersonaOperatorAssignment.active_until > now,
                    ),
                )
            )
            if visible_agent_projects is not None:
                viewer_id = _uuid(
                    _actor_value(actor, "id")
                    or _actor_value(actor, "user_id")
                )
                persona_stmt = persona_stmt.where(
                    or_(
                        Persona.project_id.in_(
                            visible_agent_projects or {UUID(int=0)}
                        ),
                        Persona.project_id.is_(None)
                        & (Persona.owner_user_id == viewer_id)
                        if viewer_id is not None
                        else False,
                    )
                )
            persona_assignments = (
                await session.execute(persona_stmt.limit(safe_limit))
            ).scalars().all()
            projection["persona_assignments"] = [_safe_row(item) for item in persona_assignments]
            run_stmt = select(AgentRun).where(AgentRun.agent_id == agent.id)
            if visible_agent_projects is not None:
                run_stmt = run_stmt.where(
                    AgentRun.project_id.in_(
                        visible_agent_projects or {UUID(int=0)}
                    )
                )
            recent_runs = (
                await session.execute(
                    run_stmt.order_by(desc(AgentRun.created_at)).limit(10)
                )
            ).scalars().all()
            projection["run_history"] = [_run_summary(run) for run in recent_runs]
            if recent_runs:
                projection["latest_run"] = _run_summary(recent_runs[0])
            agent_projections.append(projection)

        work_stmt = select(AgentWorkItem)
        if project_ids:
            work_stmt = work_stmt.where(AgentWorkItem.project_id.in_(project_ids))
        elif allowed_projects is not None:
            work_stmt = work_stmt.where(AgentWorkItem.project_id.in_(allowed_projects or {UUID(int=0)}))
        if requested_project is not None:
            work_stmt = work_stmt.where(AgentWorkItem.project_id == requested_project)
        if requested_space is not None:
            work_stmt = work_stmt.where(AgentWorkItem.space_id == requested_space)
        if requested_agent is not None:
            work_stmt = work_stmt.where(AgentWorkItem.assigned_agent_id == requested_agent)
        if domain:
            work_stmt = work_stmt.where(AgentWorkItem.domain == str(domain)[:64])
        if state:
            work_stmt = work_stmt.where(AgentWorkItem.state == str(state)[:32])
        if from_at:
            parsed = _datetime(from_at)
            if parsed:
                work_stmt = work_stmt.where(AgentWorkItem.updated_at >= parsed)
        if to_at:
            parsed = _datetime(to_at)
            if parsed:
                work_stmt = work_stmt.where(AgentWorkItem.updated_at <= parsed)
        work_rows = list((await session.execute(work_stmt.order_by(desc(AgentWorkItem.updated_at)).limit(safe_limit))).scalars().all())
        current_by_agent: dict[str, dict[str, Any]] = {}
        for work_row in work_rows:
            if (
                work_row.assigned_agent_id
                and work_row.state in {"claimed", "running"}
            ):
                current_by_agent.setdefault(
                    str(work_row.assigned_agent_id),
                    _safe_row(work_row),
                )
        for projection in agent_projections:
            current = current_by_agent.get(str(projection.get("id")))
            if current is not None:
                projection["current_work"] = {
                    key: current.get(key)
                    for key in (
                        "id",
                        "source_type",
                        "source_id",
                        "task_id",
                        "project_id",
                        "space_id",
                        "state",
                        "attempt_count",
                    )
                    if current.get(key) is not None
                }
        work_ids = [row.id for row in work_rows]
        run_rows = list((await session.execute(scoped(select(AgentRun).where(AgentRun.work_item_id.in_(work_ids or {UUID(int=0)})), AgentRun.project_id, visible_agent_projects).order_by(desc(AgentRun.created_at)).limit(safe_limit * 2))).scalars().all())
        latest_run: dict[str, AgentRun] = {}
        for row in run_rows:
            latest_run.setdefault(str(row.work_item_id), row)

        action_stmt = scoped(select(ExternalAction).where(ExternalAction.status.in_(("proposed", "approved", "attempting", "uncertain", "failed"))), ExternalAction.project_id, visible_agent_projects)
        if requested_agent is not None:
            action_stmt = action_stmt.where(ExternalAction.origin_agent_id == requested_agent)
        actions = await optional_rows(session, action_stmt.order_by(desc(ExternalAction.updated_at)).limit(safe_limit)) or []

        registry = self.action_registry
        if registry is None:
            from .integration_action_registry import IntegrationActionRegistry
            registry = IntegrationActionRegistry()
        for projection in agent_projections:
            projection["employee_observability"] = await employee_summary(
                session, _uuid(projection["id"]), admin=_is_admin(actor),
                projects=visible_agent_projects, registry=registry,
            )

        current_work = []
        now = datetime.utcnow()
        for row in work_rows:
            projection = work_metadata(row) if row.source_type in {"automation_event", "external_action"} else _safe_row(row)
            started_at = getattr(row, "started_at", None) or getattr(row, "claimed_at", None)
            elapsed_seconds = None
            if started_at is not None:
                try:
                    elapsed_seconds = max(0.0, (now - started_at.replace(tzinfo=None)).total_seconds())
                except (AttributeError, TypeError, ValueError):
                    elapsed_seconds = None
            lease_expires_at = getattr(row, "lease_expires_at", None)
            if lease_expires_at is None:
                lease_health = "none"
            else:
                try:
                    lease_health = "active" if lease_expires_at.replace(tzinfo=None) > now else "expired"
                except (AttributeError, TypeError, ValueError):
                    lease_health = "unknown"
            work_state = str(getattr(row, "state", "") or "")
            projection.update(
                {
                    "agent": agent_names.get(str(row.assigned_agent_id)) if row.assigned_agent_id else None,
                    "space": space_names.get(str(row.space_id)) if row.space_id else None,
                    "project": project_names.get(str(row.project_id)) if row.project_id else None,
                    "task_title": task_names.get(str(row.task_id))
                    if row.task_id
                    else None,
                    "latest_run": _run_summary(latest_run[str(row.id)])
                    if str(row.id) in latest_run
                    else None,
                    # Derived operator fields keep the UI projection useful
                    # without exposing the fenced lease token itself.
                    "elapsed_seconds": elapsed_seconds,
                    "elapsed": elapsed_seconds,
                    "lease_health": lease_health,
                    "approval_state": "awaiting_approval" if work_state == "awaiting_approval" else None,
                    "attempt": int(getattr(row, "attempt_count", 0) or 0),
                    "attempt_count": int(getattr(row, "attempt_count", 0) or 0),
                }
            )
            current_work.append(projection)

        counts = {name: sum(1 for row in agents if str(row.state or "") == name) for name in ("active", "paused", "retired", "draft")}
        work_counts: dict[str, int] = {}
        for row in work_rows:
            work_counts[str(row.state)] = work_counts.get(str(row.state), 0) + 1
        summary = {
            "active_agents": counts.get("active", 0),
            "working": work_counts.get("running", 0) + work_counts.get("claimed", 0),
            "idle": max(0, counts.get("active", 0) - work_counts.get("running", 0) - work_counts.get("claimed", 0)),
            "blocked": work_counts.get("blocked", 0),
            "awaiting_approval": work_counts.get("awaiting_approval", 0),
            "uncertain": work_counts.get("uncertain", 0),
            "failed_stale": work_counts.get("failed", 0) + work_counts.get("dead_letter", 0),
            "budget_usage": self._budget_usage(work_rows),
        }
        attention = [
            {"kind": "approval", "id": str(row.id), "status": row.status, "project_id": str(row.project_id) if row.project_id else None}
            for row in actions
        ] + [
            {"kind": "work", "id": str(row.id), "state": row.state, "error_code": row.safe_error_code}
            for row in work_rows
            if row.state in {"blocked", "uncertain", "dead_letter"} or row.safe_error_code
        ]
        activity = await self._activity(
            session,
            work_ids,
            safe_limit,
            project_ids=project_ids,
            allowed_projects=visible_agent_projects,
            admin=_is_admin(actor),
            agent_id=requested_agent,
        )
        for projection in agent_projections:
            activity.extend(dict(call, kind="telephony_call") for call in
                            projection["employee_observability"]["phone"]["recent_calls"])
        activity = sorted(activity, key=lambda row: str(row.get("created_at") or row.get("occurred_at") or row.get("started_at") or ""), reverse=True)[:safe_limit]
        return {
            "schema_version": "operations-command-center-v1",
            "organization": organization_payload,
            "summary": summary,
            "current_work": current_work,
            "attention": attention[:safe_limit],
            "agents": agent_projections,
            "projects": [_safe_row(row) for row in projects],
            "spaces": [_safe_row(row) for row in spaces],
            "tasks": [_safe_row(row) for row in tasks],
            "schedules": [],
            "artifacts": [],
            "evidence": [],
            "media_status": {},
            "runtime_usage": {"work_items": work_counts},
            "activity": activity,
            "filters": {"space_id": str(requested_space) if requested_space else None, "project_id": str(requested_project) if requested_project else None, "agent_id": str(requested_agent) if requested_agent else None, "domain": domain, "state": state},
        }

    @staticmethod
    def _budget_usage(rows: list[Any]) -> dict[str, Any]:
        reserved = 0.0
        used = 0.0
        limit = 0.0
        for row in rows:
            value = row.budget_reservation_json if isinstance(row.budget_reservation_json, Mapping) else {}
            reserved += _number(value.get("reserved", 0))
            used += _number(value.get("consumed", value.get("used", 0)))
            limit += _number(value.get("limit", value.get("budget", 0)))
        return {
            "reserved": reserved,
            "used": used,
            "consumed": used,
            "limit": limit,
            "percent": (used / limit * 100.0) if limit > 0 else None,
        }

    async def _activity(
        self,
        session: Any,
        work_ids: list[UUID],
        limit: int,
        *,
        project_ids: set[UUID] | None = None,
        allowed_projects: set[UUID] | None = None,
        admin: bool = False,
        agent_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        project_ids = project_ids or set()

        async def fetch(statement: Any) -> list[Any]:
            """Read an optional ledger without making it a hard dependency."""

            return await optional_rows(session, statement) or []

        run_ids = scoped(select(AgentRun.id).where(
            AgentRun.work_item_id.in_(work_ids or [UUID(int=0)])
        ), AgentRun.project_id, allowed_projects)
        if work_ids:
            events = await fetch(
                select(AgentWorkEvent)
                .where(AgentWorkEvent.work_item_id.in_(work_ids))
                .order_by(desc(AgentWorkEvent.created_at))
                .limit(limit)
            )
            rows.extend(dict(fields(event, "id", "work_item_id", "sequence", "event_type", "created_at"), kind="work_event") for event in events)
            run_events = await fetch(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id.in_(run_ids))
                .order_by(desc(AgentRunEvent.created_at))
                .limit(limit)
            )
            rows.extend(dict(fields(event, "id", "run_id", "sequence", "event_type", "created_at"), kind="run_event") for event in run_events)
            tool_calls = await fetch(
                select(AgentRunToolCall)
                .where(AgentRunToolCall.run_id.in_(run_ids))
                .order_by(desc(AgentRunToolCall.created_at))
                .limit(limit)
            )
            rows.extend(dict(fields(event, "id", "run_id", "tool_name", "success", "mutation_confirmed", "created_at"), kind="tool_call") for event in tool_calls)

        work_rows = (
            await fetch(select(AgentWorkItem).where(AgentWorkItem.id.in_(work_ids)))
            if work_ids
            else []
        )
        task_ids = [row.task_id for row in work_rows if getattr(row, "task_id", None)]
        visible_project_ids = set(project_ids)
        work_project_ids = {
            row.project_id
            for row in work_rows
            if getattr(row, "project_id", None)
        }
        if task_ids:
            task_events = await fetch(
                select(TaskActivity)
                .where(TaskActivity.task_id.in_(task_ids))
                .order_by(desc(TaskActivity.created_at))
                .limit(limit)
            )
            rows.extend(_safe_row(event) for event in task_events)

        # ExternalAction attempts/receipts and operation events are linked by
        # the explicit origin_work_item_id added in WS01/WS04.  Keep the
        # fallback project filter for historical rows that predate that link.
        action_scope = list(visible_project_ids | work_project_ids) or [UUID(int=0)]
        action_predicates = [
            ExternalAction.project_id.in_(action_scope),
        ]
        if work_ids:
            action_predicates.insert(0, ExternalAction.origin_work_item_id.in_(work_ids))
        action_stmt = scoped(select(ExternalAction).where(or_(*action_predicates)), ExternalAction.project_id, allowed_projects)
        if agent_id is not None:
            action_stmt = action_stmt.where(ExternalAction.origin_agent_id == agent_id)
        actions = await fetch(action_stmt.order_by(ExternalAction.updated_at.desc()).limit(limit))
        rows.extend(await causal_activity(session, work_rows, actions, projects=allowed_projects,
                                          admin=admin, limit=limit))
        action_ids = [row.id for row in actions if getattr(row, "id", None)]
        if action_ids:
            operation_events = await fetch(
                scoped(select(OperationEvent)
                .where(OperationEvent.entity_type == "action", OperationEvent.entity_id.in_(action_ids)), OperationEvent.project_id, allowed_projects)
                .order_by(desc(OperationEvent.created_at))
                .limit(limit)
            )
            rows.extend(dict(fields(item, "id", "project_id", "entity_id", "event_type", "created_at"), kind="operation_event") for item in operation_events)

        # Heartbeat history is project-scoped rather than WorkItem-scoped.  It
        # is still a canonical activity ledger for the same company/project.
        heartbeat_project_ids = visible_project_ids | work_project_ids
        if heartbeat_project_ids:
            heartbeat_rows = await fetch(
                select(HeartbeatRunHistory)
                .where(HeartbeatRunHistory.project_id.in_(heartbeat_project_ids))
                .order_by(desc(HeartbeatRunHistory.started_at))
                .limit(limit)
            )
            rows.extend(_safe_row(item) for item in heartbeat_rows)

        return sorted(
            (row for row in rows if row),
            key=lambda row: str(row.get("created_at") or row.get("occurred_at") or row.get("started_at") or ""),
            reverse=True,
        )[:limit]

    @staticmethod
    def _empty_snapshot() -> dict[str, Any]:
        return {
            "schema_version": "operations-command-center-v1",
            "organization": {"singleton": True, "display_name": "AoiTalk"},
            "summary": {
                "active_agents": 0,
                "working": 0,
                "idle": 0,
                "blocked": 0,
                "awaiting_approval": 0,
                "uncertain": 0,
                "failed_stale": 0,
                "budget_usage": {
                    "reserved": 0,
                    "used": 0,
                    "consumed": 0,
                    "limit": 0,
                    "percent": None,
                },
            },
            "current_work": [],
            "attention": [],
            "agents": [],
            "projects": [],
            "spaces": [],
            "tasks": [],
            "schedules": [],
            "artifacts": [],
            "evidence": [],
            "media_status": {},
            "runtime_usage": {},
            "activity": [],
        }


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["OperationsCommandCenterService"]
