"""Read-only Operations Command Center projection.

The projection composes existing ledgers (Tasks, AgentWork, AgentRuns,
ExternalActions and activity rows).  It deliberately owns no mutable
workflow state and omits provider payloads, prompts, credentials and lease
tokens from every response.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import String, and_, cast, desc, func, or_, select

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
    EngagementOpportunity,
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
from .operations_execution_projection import attention_predicate, review_predicate, card_summaries, code, _SECRET
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


def _page_scope(actor: Any, filters: dict[str, Any]) -> str:
    values = dict(filters, actor=str(_actor_value(actor, "id") or _actor_value(actor, "user_id") or ""), admin=_is_admin(actor))
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def _read_cursor(value: str | None, *, kind: str, scope: str) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        if len(value) > 2048:
            raise ValueError()
        row = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if row["kind"] != kind or row["scope"] != scope or _uuid(row["id"]) is None:
            raise ValueError()
        if _datetime(row["at"]) is None or _datetime(row["read_at"]) is None:
            raise ValueError()
        return row
    except (binascii.Error, ValueError, TypeError, KeyError, UnicodeError, OverflowError) as exc:
        raise ValueError(f"invalid {kind} cursor or scope") from exc


def _page_metadata(rows: list[Any], limit: int, available: int, *, cursor: dict[str, Any]) -> dict[str, Any]:
    has_more = len(rows) > limit
    next_cursor = None
    if has_more:
        next_cursor = base64.urlsafe_b64encode(json.dumps(cursor, separators=(",", ":")).encode()).decode().rstrip("=")
    return {"limit": limit, "fetched": min(len(rows), limit), "available": available,
            "has_more": has_more, "truncated": has_more, "next_cursor": next_cursor}


_ATTENTION_REASON_PRIORITY = {
    "blocked": 0,
    "uncertain": 0,
    "verification_failed": 0,
    "human_action_required": 0,
    "retry_exhausted": 1,
    "failed": 1,
    "stale": 2,
    "retry_wait": 2,
    "awaiting_approval": 2,
    "ready_for_review": 3,
}


def _card_text(value):
    if not isinstance(value, str) or _SECRET.search(value) or "://" in value:
        return None
    return value.strip()[:160] or None


def action_attention_summary(row, visible_work, *, task_names=None, agent_names=None, targets=None):
    """Safe approval card metadata from canonical columns, never raw payload.

    Historical origin pins are exposed only after same-project resolution.
    Target IDs belong to the authorized action, rather than a fetched resource.
    """
    operation = code(row.action_type)
    effects = {
        "engagement.submit_application": "応募内容を外部へ送信",
        "media.publish_content": "コンテンツを外部へ公開",
        "media.update_content": "公開済みコンテンツを変更",
        "media.delete_content": "公開済みコンテンツを削除",
        "media.release_product": "商品を外部へ公開",
    }
    origin = visible_work.get(str(row.origin_work_item_id))
    valid_origin = origin is not None and origin.project_id == row.project_id
    proposing_agent = getattr(row, "origin_agent_id", None)
    target = row.opportunity_id if operation == "engagement.submit_application" else row.content_item_id if operation in effects else None
    labels = {"engagement.submit_application": "応募を送信", "media.publish_content": "コンテンツを公開", "media.update_content": "公開内容を変更", "media.delete_content": "公開内容を削除", "media.release_product": "商品を公開"}
    return {
        "operation_label": labels.get(operation, "操作種別の詳細未取得"),
        "target_kind": "応募先案件" if operation == "engagement.submit_application" else "コンテンツ" if operation in effects else "対象種別未記録",
        "target_name": _card_text((targets or {}).get((str(row.project_id), str(target)))),
        "work_name": _card_text((task_names or {}).get(str(origin.task_id))) if valid_origin else None,
        "actor_name": _card_text((agent_names or {}).get(str(proposing_agent))) if proposing_agent else None,
        "current_assignee": str(origin.assigned_agent_id) if valid_origin and origin.assigned_agent_id else None,
        "current_assignee_name": _card_text((agent_names or {}).get(str(origin.assigned_agent_id))) if valid_origin else None,
        "operation": operation,
        "target": str(target) if target else None,
        "consequence": effects.get(operation, "影響は未記録。正本で確認してください"),
        "work_item_id": str(origin.id) if valid_origin else None,
        "actor": str(proposing_agent) if proposing_agent else None,
        "action_version": row.action_version,
        "authorization_mode": code(row.authorization_mode),
        "decision": "対象・内容・外部への影響を確認し、この版を承認するか判断",
    }


def _attention_priority(reasons: Any, *, action_status: str | None = None) -> int:
    if action_status in {"uncertain", "failed"}:
        return 0
    if action_status == "approved":
        return 1
    if action_status in {"attempting", "proposed"}:
        return 2
    values = [
        _ATTENTION_REASON_PRIORITY.get(str(reason), 4)
        for reason in reasons
        if isinstance(reason, str)
    ]
    return min(values, default=4)


def _catalog_visibility(model: Any, actor: Any, prefix: str) -> Any:
    """Keep reserved personal Inboxes out of another user's browse catalog.

    This matches Project/Space navigation, including for administrators. It
    does not change the operator's underlying management/ledger permissions.
    Rebuild canonical UUID text so PostgreSQL and SQLite use the same exact
    owner-bound slug test; a shared project merely named Inbox remains visible.
    """

    owner = func.replace(cast(model.owner_id, String), "-", "")
    canonical_owner = (
        func.substr(owner, 1, 8) + "-" + func.substr(owner, 9, 4)
        + "-" + func.substr(owner, 13, 4) + "-" + func.substr(owner, 17, 4)
        + "-" + func.substr(owner, 21, 12)
    )
    viewer = _uuid(_actor_value(actor, "id") or _actor_value(actor, "user_id"))
    return or_(model.owner_id == viewer, model.slug != prefix + canonical_owner)


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
    """Expose lifecycle metadata without invoking the full AgentRun DTO.

    ``AgentRun.to_dict`` also sanitizes the resolved execution manifest and
    route snapshots.  Those payloads are intentionally outside this closed
    projection, so reading the handful of scalar columns directly avoids the
    expensive serializer while keeping the exact allow-list above.
    """

    result: dict[str, Any] = {}
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
    ):
        value = getattr(row, key, None)
        if value is None and key == "run_id":
            # AgentRun's canonical identity column is named ``id``; retain
            # compatibility with rows that expose a separate run_id field.
            continue
        if key == "id" or key.endswith("_id"):
            value = str(value) if value is not None else None
        elif isinstance(value, datetime):
            value = value.isoformat()
        value = _safe_value(value)
        if value is not None:
            result[key] = value
    return result


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
        member_rows = (await session.execute(
            select(ProjectMember)
            .join(Project, Project.id == ProjectMember.project_id)
            .where(
                ProjectMember.user_id == actor_id,
                Project.deleted_at.is_(None),
                ProjectMember.deleted_at.is_(None) if hasattr(ProjectMember, "deleted_at") else True,
            )
        )).scalars().all()
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
        work_cursor: str | None = None,
        agents_cursor: str | None = None,
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
        cursor_scope = _page_scope(actor, {"project": requested_project, "space": requested_space,
                                          "agent": requested_agent, "domain": domain, "state": state,
                                          "from": from_at, "to": to_at})
        work_position = _read_cursor(work_cursor, kind="work", scope=cursor_scope)
        agent_position = _read_cursor(agents_cursor, kind="agents", scope=cursor_scope)
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
        # Authorization and explicit scope use the full Project query. The
        # bounded browse catalog below must never narrow an operational ledger.
        project_scope_rows = (await session.execute(project_stmt.with_only_columns(Project.id, Project.space_id))).all()
        project_ids = {row.id for row in project_scope_rows}
        space_ids = {row.space_id for row in project_scope_rows if row.space_id} or {UUID(int=0)}
        visible_agent_projects = (
            project_ids if requested_project is not None or requested_space is not None else allowed_projects
        )
        # The operational ledger scope above retains the existing admin/read
        # ACL. Browse catalogs separately apply the personal Inbox visibility
        # rule before LIMIT, so other users' seeds cannot crowd out real work.
        catalog_projects = list(
            (await session.execute(
                project_stmt.where(_catalog_visibility(Project, actor, "inbox-project-"))
                .order_by(Project.name, Project.id).limit(safe_limit)
            )).scalars().all()
        )
        catalog_space_ids = {row.space_id for row in catalog_projects if row.space_id}
        spaces = list(
            (
                await session.execute(
                    select(Space)
                    .where(
                        Space.id.in_(catalog_space_ids or {UUID(int=0)}),
                        _catalog_visibility(Space, actor, "inbox-"),
                    )
                    .order_by(Space.name, Space.id)
                    .limit(safe_limit)
                )
            )
            .scalars()
            .all()
        )
        space_names = {str(row.id): row.name for row in spaces}
        project_names = {str(row.id): row.name for row in catalog_projects}

        task_stmt = select(Task).where(Task.deleted_at.is_(None), Task.archived_at.is_(None))
        task_stmt = scoped(task_stmt, Task.project_id, visible_agent_projects)
        tasks = list((await session.execute(task_stmt.order_by(desc(Task.updated_at)).limit(safe_limit))).scalars().all())
        task_names = {
            str(row.id): getattr(row, "title", None)
            for row in tasks
            if getattr(row, "id", None)
        }

        agent_scope_stmt = select(Agent)
        if visible_agent_projects is not None:
            visible_grants = select(AgentProjectGrant.agent_id).where(
                AgentProjectGrant.project_id.in_(visible_agent_projects or {UUID(int=0)}),
                AgentProjectGrant.state == "active",
                or_(AgentProjectGrant.active_from.is_(None), AgentProjectGrant.active_from <= now),
                or_(AgentProjectGrant.active_until.is_(None), AgentProjectGrant.active_until > now),
            )
            agent_scope_stmt = agent_scope_stmt.where(Agent.id.in_(visible_grants))
        agent_stmt = agent_scope_stmt
        if requested_agent is not None:
            agent_stmt = agent_stmt.where(Agent.id == requested_agent)
        filtered_agent_stmt = agent_stmt
        # Agent names and state can change while traversing pages. Use the
        # immutable creation order and a fixed creation boundary, while grant
        # authorization and manager resolution continue to use the live ACL.
        agent_read_at = _datetime(agent_position["read_at"]) if agent_position else datetime.utcnow()
        agent_stmt = agent_stmt.where(Agent.created_at <= agent_read_at)
        agent_available = (await session.execute(select(func.count()).select_from(agent_stmt.subquery()))).scalar_one()
        agent_state_rows = (await session.execute(agent_stmt.with_only_columns(Agent.state, func.count()).group_by(Agent.state))).all()
        if agent_position is not None:
            agent_stmt = agent_stmt.where(or_(
                Agent.created_at < _datetime(agent_position["at"]),
                and_(Agent.created_at == _datetime(agent_position["at"]), Agent.id > _uuid(agent_position["id"])),
            ))
        agent_candidates = list((await session.execute(agent_stmt.order_by(desc(Agent.created_at), Agent.id).limit(safe_limit + 1))).scalars().all())
        agents = agent_candidates[:safe_limit]
        agent_page = _page_metadata(agent_candidates, safe_limit, agent_available, cursor={
            "kind": "agents", "scope": cursor_scope, "read_at": agent_read_at.isoformat(),
            "at": agents[-1].created_at.isoformat() if agents else None, "id": str(agents[-1].id) if agents else "",
        })
        agent_names = {str(row.id): row.display_name for row in agents}
        profiles = (await session.execute(select(AgentOrganizationProfile).where(
            AgentOrganizationProfile.agent_id.in_([row.id for row in agents] or {UUID(int=0)})
        ))).scalars().all() if agents else []
        profiles_by_agent = {row.agent_id: row for row in profiles}
        manager_ids = {row.manager_agent_id for row in profiles if row.manager_agent_id}
        authorized_managers = (await session.execute(filtered_agent_stmt.where(Agent.id.in_(manager_ids)))).scalars().all() if manager_ids else []
        agent_ids = {row.id for row in [*agents, *authorized_managers]}
        agent_names.update({str(row.id): row.display_name for row in authorized_managers})

        agent_projections: list[dict[str, Any]] = []
        for agent in agents:
            safe_agent = _safe_row(agent)
            projection = {
                key: safe_agent.get(key)
                for key in ("id", "display_name", "slug", "state", "character_id")
                if safe_agent.get(key) is not None
            }
            projection["manager_relation"] = "unrecorded"
            profile = profiles_by_agent.get(agent.id)
            if profile is not None:
                safe_profile = _safe_row(profile)
                # A supervisor is a recorded organization relation, not a team
                # configuration. Resolve against the full authorized scope,
                # including other pages, while omitting human manager identity.
                manager_id = _uuid(safe_profile.get("manager_agent_id"))
                if manager_id is not None:
                    if manager_id in agent_ids:
                        projection["manager_agent_id"] = str(manager_id)
                        projection["manager_agent_name"] = agent_names.get(str(manager_id))
                        projection["manager_relation"] = "agent"
                    else:
                        projection["manager_relation"] = "outside_scope"
                elif safe_profile.get("manager_user_id") is not None:
                    projection["manager_relation"] = "human"
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
            if visible_agent_projects is not None:
                grant_stmt = grant_stmt.where(AgentProjectGrant.project_id.in_(visible_agent_projects or {UUID(int=0)}))
            grants = (await session.execute(grant_stmt.order_by(AgentProjectGrant.id).limit(safe_limit))).scalars().all()
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

        # The canonical Work detail permits NULL Projects for administrators
        # only. Apply its ACL first, then the actual stored scope fields.
        # Earlier queries have completed SQLAlchemy's normal autoflush;
        # capture after that so newly materialized local rows are included.
        # Authorization/expiry checks above always use current time, never
        # this continuation's historical creation boundary.
        work_read_at = _datetime(work_position["read_at"]) if work_position else datetime.utcnow()
        work_stmt = scoped(select(AgentWorkItem), AgentWorkItem.project_id, allowed_projects)
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
        # Work traversal uses immutable creation time, separately from the
        # prioritized attention preview. State/update changes cannot move an
        # existing row across a continuation boundary; newer rows await refresh.
        work_stmt = work_stmt.where(AgentWorkItem.created_at <= work_read_at)
        work_available = (await session.execute(select(func.count()).select_from(work_stmt.subquery()))).scalar_one()
        work_state_rows = (await session.execute(work_stmt.with_only_columns(AgentWorkItem.state, func.count()).group_by(AgentWorkItem.state))).all()
        work_budget_rows = (await session.execute(work_stmt.with_only_columns(AgentWorkItem.budget_reservation_json))).all()
        # Review is a correlated predicate over the entire ledger; a bounded
        # review-ID preview would omit review candidates beyond that preview.
        opportunity_rows = await optional_rows(session, select(EngagementOpportunity.id).limit(1))
        review_due = (select(EngagementOpportunity.id).where(review_predicate())
                      .correlate(AgentWorkItem).exists()) if opportunity_rows is not None else False
        paged_work_stmt = work_stmt
        if work_position is not None:
            at, identity = _datetime(work_position["at"]), _uuid(work_position["id"])
            paged_work_stmt = paged_work_stmt.where(or_(
                AgentWorkItem.created_at < at,
                and_(AgentWorkItem.created_at == at, AgentWorkItem.id > identity),
            ))
        work_candidates = list((await session.execute(paged_work_stmt.order_by(
            desc(AgentWorkItem.created_at), AgentWorkItem.id,
        ).limit(safe_limit + 1))).scalars().all())
        work_rows = work_candidates[:safe_limit]
        work_page = _page_metadata(work_candidates, safe_limit, work_available, cursor={
            "kind": "work", "scope": cursor_scope, "read_at": work_read_at.isoformat(),
            "at": work_rows[-1].created_at.isoformat() if work_rows else None,
            "id": str(work_rows[-1].id) if work_rows else "",
        })
        attention_work_rows = list((await session.execute(work_stmt.where(
            or_(attention_predicate(now), review_due),
        ).order_by(
            desc(AgentWorkItem.updated_at), AgentWorkItem.id,
        ).limit(safe_limit + 1))).scalars().all())
        summary_rows = list({
            str(row.id): row for row in [*work_rows, *attention_work_rows]
        }.values())
        execution_summaries = await card_summaries(session, summary_rows, allowed_projects, now=now)
        current_by_agent: dict[str, dict[str, Any]] = {}
        running_rows = (await session.execute(work_stmt.where(
            AgentWorkItem.state.in_(("claimed", "running")),
            AgentWorkItem.assigned_agent_id.in_([row.id for row in agents] or {UUID(int=0)}),
        ).order_by(desc(AgentWorkItem.updated_at), AgentWorkItem.id))).scalars().all() if agents else []
        for work_row in running_rows:
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
        # Resolve card labels by referenced IDs through the full ACL, independent
        # of the bounded Project/Task/Agent browse previews.
        referenced_projects = {row.project_id for row in summary_rows if row.project_id}
        labels = (await session.execute(scoped(select(Project).where(Project.id.in_(referenced_projects)), Project.id, allowed_projects))).scalars().all() if referenced_projects else []
        project_names.update({str(row.id): row.name for row in labels})
        referenced_tasks = {row.task_id for row in summary_rows if row.task_id}
        labels = (await session.execute(scoped(select(Task).where(Task.id.in_(referenced_tasks)), Task.project_id, allowed_projects))).scalars().all() if referenced_tasks else []
        task_names.update({str(row.id): row.title for row in labels})
        referenced_agents = {row.assigned_agent_id for row in summary_rows if row.assigned_agent_id}
        labels = (await session.execute(agent_scope_stmt.where(Agent.id.in_(referenced_agents)))).scalars().all() if referenced_agents else []
        agent_names.update({str(row.id): row.display_name for row in labels})
        referenced_spaces = {row.space_id for row in summary_rows if row.space_id}
        space_label_stmt = select(Space).where(Space.id.in_(referenced_spaces), _catalog_visibility(Space, actor, "inbox-"))
        if allowed_projects is not None:
            space_label_stmt = space_label_stmt.where(Space.id.in_(space_ids))
        labels = (await session.execute(space_label_stmt)).scalars().all() if referenced_spaces else []
        space_names.update({str(row.id): row.name for row in labels})
        run_rows = list((await session.execute(scoped(select(AgentRun).join(AgentWorkItem, AgentWorkItem.id == AgentRun.work_item_id).where(
            AgentRun.work_item_id.in_(work_ids or {UUID(int=0)}),
            or_(AgentRun.project_id == AgentWorkItem.project_id,
                and_(AgentRun.project_id.is_(None), AgentWorkItem.project_id.is_(None))),
        ), AgentRun.project_id, allowed_projects).order_by(desc(AgentRun.created_at)).limit(safe_limit * 2))).scalars().all())
        latest_run: dict[str, AgentRun] = {}
        for row in run_rows:
            latest_run.setdefault(str(row.work_item_id), row)

        action_stmt = scoped(select(ExternalAction).where(ExternalAction.status.in_(("proposed", "approved", "attempting", "uncertain", "failed"))), ExternalAction.project_id, visible_agent_projects)
        if requested_agent is not None:
            action_stmt = action_stmt.where(ExternalAction.origin_agent_id == requested_agent)
        actions = await optional_rows(session, action_stmt.order_by(desc(ExternalAction.updated_at)).limit(safe_limit + 1)) or []

        # Approval provenance is resolved independently of the Work page. A
        # continuation must not erase an authorized action's origin context.
        origin_ids = {row.origin_work_item_id for row in actions if row.origin_work_item_id}
        action_origins = (await session.execute(scoped(select(AgentWorkItem).where(
            AgentWorkItem.id.in_(origin_ids),
        ), AgentWorkItem.project_id, allowed_projects))).scalars().all() if origin_ids else []
        action_work = {str(row.id): row for row in [*work_rows, *attention_work_rows, *action_origins]}
        origin_tasks = {row.task_id for row in action_origins if row.task_id}
        origin_task_rows = (await session.execute(scoped(select(Task).where(Task.id.in_(origin_tasks)), Task.project_id, allowed_projects))).scalars().all() if origin_tasks else []
        task_names.update({str(row.id): row.title for row in origin_task_rows})
        action_agents = {row.origin_agent_id for row in actions if row.origin_agent_id} | {row.assigned_agent_id for row in action_origins if row.assigned_agent_id}
        action_agent_rows = (await session.execute(agent_scope_stmt.where(Agent.id.in_(action_agents)))).scalars().all() if action_agents else []
        agent_names.update({str(row.id): row.display_name for row in action_agent_rows})

        # Resolve names only across the same authorized project boundary as the action.
        target_names = {}
        opportunity_ids = {row.opportunity_id for row in actions if row.opportunity_id}
        if opportunity_ids:
            targets = await optional_rows(session, scoped(select(EngagementOpportunity).where(EngagementOpportunity.id.in_(opportunity_ids)), EngagementOpportunity.project_id, visible_agent_projects))
            target_names.update({(str(row.project_id), str(row.id)): row.title for row in targets or []})
        content_ids = {row.content_item_id for row in actions if row.content_item_id}
        if content_ids:
            from ..memory.models import ContentItem
            targets = await optional_rows(session, scoped(select(ContentItem).where(ContentItem.id.in_(content_ids)), ContentItem.project_id, visible_agent_projects))
            target_names.update({(str(row.project_id), str(row.id)): row.title for row in targets or []})

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
            execution = execution_summaries[str(row.id)]
            projection.update(execution_summary=execution,
                              elapsed_seconds=execution["elapsed_seconds"],
                              elapsed=execution["elapsed_seconds"])
            current_work.append(projection)

        counts = {str(name): count for name, count in agent_state_rows}
        work_counts = {str(name): count for name, count in work_state_rows}
        active_agent_stmt = agent_scope_stmt.where(Agent.state == "active", Agent.created_at <= agent_read_at)
        if requested_agent is not None:
            active_agent_stmt = active_agent_stmt.where(Agent.id == requested_agent)
        idle_stmt = active_agent_stmt.where(~Agent.id.in_(work_stmt.with_only_columns(AgentWorkItem.assigned_agent_id).where(
            AgentWorkItem.state.in_(("running", "claimed")), AgentWorkItem.assigned_agent_id.is_not(None),
        )))
        summary = {
            "active_agents": counts.get("active", 0),
            "working": work_counts.get("running", 0) + work_counts.get("claimed", 0),
            "idle": (await session.execute(select(func.count()).select_from(idle_stmt.subquery()))).scalar_one(),
            "blocked": work_counts.get("blocked", 0),
            "awaiting_approval": work_counts.get("awaiting_approval", 0),
            "uncertain": work_counts.get("uncertain", 0),
            "failed_stale": work_counts.get("failed", 0) + work_counts.get("dead_letter", 0),
            "budget_usage": self._budget_usage(work_budget_rows),
        }
        attention = []
        for row in actions:
            attention.append({
                "kind": "approval", "id": str(row.id), "status": row.status,
                "project_id": str(row.project_id) if row.project_id else None,
                "approval_summary": action_attention_summary(row, action_work, task_names=task_names, agent_names=agent_names, targets=target_names),
                "_attention_priority": _attention_priority([], action_status=str(row.status)),
                "_attention_at": getattr(row, "updated_at", None) or getattr(row, "created_at", None),
            })
        for row in attention_work_rows:
            execution = execution_summaries[str(row.id)]
            if not execution["attention_reasons"]:
                continue
            attention.append({
                "kind": "work", "id": str(row.id), "state": row.state,
                "error_code": execution["safe_error_code"],
                "execution_summary": execution,
                "attention_reasons": execution["attention_reasons"],
                "_attention_priority": _attention_priority(execution["attention_reasons"]),
                "_attention_at": getattr(row, "updated_at", None) or getattr(row, "created_at", None),
            })
        attention.sort(key=lambda item: str(item.get("_attention_at") or ""), reverse=True)
        attention.sort(key=lambda item: item.get("_attention_priority", 4))
        attention_has_more = len(attention) > safe_limit
        for item in attention:
            item.pop("_attention_priority", None)
            item.pop("_attention_at", None)
        activity = await self._activity(
            session,
            work_ids,
            safe_limit,
            project_ids=project_ids,
            allowed_projects=allowed_projects,
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
            "work_page": work_page,
            "attention": attention[:safe_limit],
            "attention_page": {"limit": safe_limit, "has_more": attention_has_more, "next_cursor": None},
            "attention_has_more": attention_has_more,
            "agents": agent_projections,
            "agents_page": agent_page,
            "projects": [_safe_row(row) for row in catalog_projects],
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
            "work_page": {"limit": 0, "fetched": 0, "available": 0, "has_more": False, "truncated": False, "next_cursor": None},
            "attention": [],
            "attention_page": {"limit": 0, "has_more": False, "next_cursor": None},
            "attention_has_more": False,
            "agents": [],
            "agents_page": {"limit": 0, "fetched": 0, "available": 0, "has_more": False, "truncated": False, "next_cursor": None},
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
