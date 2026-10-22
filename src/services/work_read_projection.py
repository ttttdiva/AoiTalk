"""One row per canonical job for Operations, Tasks and Projects.

No workflow state, runtime dispatch or usage writes belong here. Source type
and ID are authority-bearing references, never fuzzy title/project joins.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    String,
    and_,
    case,
    cast,
    desc,
    func,
    literal,
    or_,
    select,
    union_all,
)
from sqlalchemy.orm import load_only

from ..memory.models import (
    Agent,
    AgentProjectGrant,
    AgentRun,
    AgentWorkItem,
    ArtifactVersion,
    ContentItem,
    EngagementOpportunity,
    ExternalAction,
    GenerationPlan,
    Project,
    ProjectMember,
    Task,
)
from ..memory.models.media_operations_content import (
    ContentVariant,
    ContentVariantRevision,
)
from ..memory.models.operations import safe_opportunity_title
from ..models.ecc_models import TokenUsage
from .media_generation_budget import _holds
from .operations_command_center import (
    _actor_value,
    _is_admin,
    _page_scope,
    _safe_value,
    _uuid,
)
from .operations_employee_observability import optional_rows
from .operations_execution_projection import code, dt, iso, latest_phase_events, phase
from .operations_service import (
    OperationsConflictError,
    OperationsNotFoundError,
    OperationsService,
    OperationsValidationError,
)
from .project_permissions import (
    has_effective_project_permission,
    normalize_project_member_permissions,
)


_MODELS = {
    "task": Task,
    "opportunity": EngagementOpportunity,
    "content_item": ContentItem,
}
_STATES = {
    "task": {
        "todo",
        "open",
        "in_progress",
        "blocked",
        "on_hold",
        "review",
        "closed",
        "cancelled",
    },
    "opportunity": {
        "open",
        "archived",
        "applied",
        "rejected",
        "accepted",
        "closed",
        "escrow_pending",
        "contract_accepted",
        "ready_for_review",
    },
    "content_item": {"draft", "ready", "in_progress", "published", "archived"},
}
_WORK_STATES = {
    "pending",
    "claimed",
    "running",
    "succeeded",
    "cancelled",
    "blocked",
    "uncertain",
    "retry_wait",
    "dead_letter",
    "awaiting_approval",
    "failed",
}
_RUN_STATES = {
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "awaiting_approval",
}
MAX_USAGE_ROWS = 500


def _money(value):
    # Only the canonical Decimal columns are money evidence. Legacy floats
    # and placeholder zeroes from unpriced engines cannot establish a cost.
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        return None
    if value == 0:
        return "0"
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _status(value, values):
    return value if value in values else "unknown"


def _same_scope(row, source):
    # A Project grant is shared across owners. Only personal rows keep an
    # owner boundary, matching OperationsService._assert_entity_access.
    return row.project_id == source.project_id and (
        source.project_id is not None or row.owner_user_id == source.owner_user_id
    )


def _scope_condition(model, source):
    if source.project_id is not None:
        return model.project_id == source.project_id
    return and_(model.project_id.is_(None), model.owner_user_id == source.owner_user_id)


def _work_scope(source, space_id):
    return and_(
        AgentWorkItem.project_id == source.project_id
        if source.project_id
        else AgentWorkItem.project_id.is_(None),
        or_(AgentWorkItem.space_id.is_(None), AgentWorkItem.space_id == space_id),
    )


async def _work_reference(session, work, source):
    """Resolve recorded business ownership, separately from causal ancestry.

    None means an internal stage with no independent business identity. A
    known source whose reference is broken is unknown, never an inherited job.
    """
    identity = _uuid(work.source_id)
    if work.source_type == "task":
        if identity is None or work.task_id not in (None, identity):
            return ("unknown", None)
        return ("task", identity)
    if work.source_type in {
        "marketplace_crowdworks",
        "marketplace_crowdworks_fulfillment",
    }:
        return ("opportunity", identity) if identity else ("unknown", None)
    models = {
        "media.generation.plan": GenerationPlan,
        "media.publication": ContentVariant,
        "external_action": ExternalAction,
    }
    model = models.get(work.source_type)
    if model is not None:
        row = (
            (
                await optional_rows(
                    session,
                    select(model)
                    .where(model.id == identity, _scope_condition(model, source))
                    .limit(1),
                )
            )
            if identity
            else []
        )
        if not row:
            return ("unknown", None)
        row = row[0]
        if model is ExternalAction:
            if row.opportunity_id and row.content_item_id:
                return ("unknown", None)
            if row.opportunity_id:
                return ("opportunity", row.opportunity_id)
            if row.content_item_id:
                return ("content_item", row.content_item_id)
        elif row.content_item_id:
            return ("content_item", row.content_item_id)
        else:
            return ("unknown", None)
    return ("task", work.task_id) if work.task_id else None


async def _owned_work_ids(session, kind, source, roots, space_id, gaps):
    """Traverse internal stages; another canonical source cuts the branch.

    A root pin cannot bypass a parent assigned to another job. Independently
    source-bound roots are selected before this traversal, so they still appear
    under their own canonical job even if their causal parent belongs elsewhere.
    """
    identities = set()
    reference = (kind, source.id)
    for row in roots:
        own_reference = await _work_reference(session, row, source)
        if own_reference == reference:
            identities.add(row.id)
        else:
            gaps.append("work_source_link_unresolved")
    frontier, seen = set(identities), set(identities)
    for _depth in range(16):
        if not frontier:
            return identities
        children = await optional_rows(
            session,
            select(AgentWorkItem)
            .where(
                _work_scope(source, space_id),
                AgentWorkItem.id.not_in(seen),
                or_(
                    AgentWorkItem.parent_work_item_id.in_(frontier),
                    and_(
                        AgentWorkItem.parent_work_item_id.is_(None),
                        AgentWorkItem.root_work_item_id.in_(identities),
                    ),
                ),
            )
            .order_by(AgentWorkItem.id)
            .limit(201 - len(identities)),
        )
        if children is None:
            gaps.append("work_ledger_unavailable")
            return identities
        if not children:
            return identities
        remaining = 200 - len(identities)
        next_frontier = set()
        for child in children[:remaining]:
            seen.add(child.id)
            own_reference = await _work_reference(session, child, source)
            if own_reference is None or own_reference == reference:
                identities.add(child.id)
                next_frontier.add(child.id)
            else:
                gaps.append(
                    "work_source_link_unresolved"
                    if own_reference[0] == "unknown"
                    else "related_work_has_other_source"
                )
        if len(children) > remaining:
            gaps.append("attempts_truncated")
            return identities
        frontier = next_frontier
    gaps.append("attempts_truncated")
    return identities


async def _owned_run_ids(session, kind, source, work_rows, gaps):
    """Typed Work/Task ownership wins over a Run's parent/root context."""
    work_ids = {row.id for row in work_rows}
    legacy_task = (
        and_(AgentRun.work_item_id.is_(None), AgentRun.task_id == source.id)
        if kind == "task"
        else False
    )
    project_match = (
        AgentRun.project_id == source.project_id
        if source.project_id
        else AgentRun.project_id.is_(None)
    )
    pins = {row.active_agent_run_id for row in work_rows if row.active_agent_run_id}
    direct = (
        (
            await session.execute(
                select(AgentRun.id)
                .outerjoin(AgentWorkItem, AgentWorkItem.id == AgentRun.work_item_id)
                .where(
                    project_match, or_(AgentRun.work_item_id.in_(work_ids), legacy_task)
                )
                .order_by(
                    # Completed Work clears the active pin; retain its exact
                    # current attempt before trimming historical Run rows.
                    case(
                        (AgentRun.id.in_(pins), 0),
                        (
                            and_(
                                AgentWorkItem.attempt_count.is_not(None),
                                AgentRun.work_item_attempt
                                == AgentWorkItem.attempt_count,
                            ),
                            1,
                        ),
                        else_=2,
                    ),
                    AgentRun.created_at,
                    AgentRun.id,
                )
                .limit(201)
            )
        )
        .scalars()
        .all()
    )
    identities = set(direct[:200])
    if len(direct) > 200:
        gaps.append("runs_truncated")
    frontier = set(identities)
    for _depth in range(16):
        if not frontier:
            return identities
        children = (
            (
                await session.execute(
                    select(AgentRun.id)
                    .where(
                        project_match,
                        AgentRun.id.not_in(identities),
                        AgentRun.work_item_id.is_(None),
                        or_(
                            AgentRun.task_id.is_(None),
                            AgentRun.task_id == source.id if kind == "task" else False,
                        ),
                        or_(
                            AgentRun.parent_run_id.in_(frontier),
                            and_(
                                AgentRun.parent_run_id.is_(None),
                                AgentRun.root_run_id.in_(identities),
                            ),
                        ),
                    )
                    .order_by(AgentRun.created_at, AgentRun.id)
                    .limit(201 - len(identities))
                )
            )
            .scalars()
            .all()
        )
        if not children:
            return identities
        remaining = 200 - len(identities)
        identities.update(children[:remaining])
        if len(children) > remaining:
            gaps.append("runs_truncated")
            return identities
        frontier = set(children)
    gaps.append("runs_truncated")
    return identities


async def _cost_ledger_rows(session, source, work_rows, space_id, gaps):
    """A shared root may hold money allocated to another source's Work ID.

    Ancestors are ledger context only; they never become this job's attempts.
    The caller filters every hold by the separately resolved owned Work IDs.
    """
    rows = {row.id: row for row in work_rows}
    frontier = list(work_rows)
    for _depth in range(16):
        links = {
            identity
            for row in frontier
            for identity in (row.parent_work_item_id, row.root_work_item_id)
            if identity and identity not in rows
        }
        if not links:
            return list(rows.values())
        ancestors = await optional_rows(
            session,
            select(AgentWorkItem)
            .where(_work_scope(source, space_id), AgentWorkItem.id.in_(links))
            .order_by(AgentWorkItem.id)
            .limit(201 - len(rows)),
        )
        if ancestors is None:
            gaps.append("cost_ancestry_partial")
            return list(rows.values())
        if {row.id for row in ancestors} != links:
            gaps.append("cost_ancestry_partial")
        remaining = 200 - len(rows)
        rows.update({row.id: row for row in ancestors[:remaining]})
        if len(ancestors) > remaining:
            gaps.append("cost_ancestry_partial")
            return list(rows.values())
        frontier = ancestors
    gaps.append("cost_ancestry_partial")
    return list(rows.values())


def _identity(column):
    # SQLite UUID text has no hyphens; normalize only exact IDs, never prose.
    return func.replace(cast(column, String), "-", "")


def _cursor(value, scope):
    if not value:
        return None
    try:
        if len(value) > 2048:
            raise ValueError()
        row = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if (
            row["scope"] != scope
            or row["kind"] not in _MODELS
            or _uuid(row["id"]) is None
        ):
            raise ValueError()
        if dt(row["at"]) is None or dt(row["read_at"]) is None:
            raise ValueError()
        return row
    except (KeyError, TypeError, ValueError, binascii.Error, UnicodeError) as exc:
        raise OperationsValidationError(
            "invalid work projection cursor or scope"
        ) from exc


class WorkReadProjectionService:
    """Authenticated business reads with the canonical Project/owner ACL."""

    def __init__(self):
        self.operations = OperationsService()

    async def snapshot(
        self,
        session,
        actor,
        *,
        project_id=None,
        space_id=None,
        kind=None,
        limit=50,
        cursor=None,
    ):
        viewer = _uuid(_actor_value(actor, "id") or _actor_value(actor, "user_id"))
        if viewer is None:
            raise OperationsValidationError("actor id is required")
        for value, name in ((project_id, "project_id"), (space_id, "space_id")):
            if value is not None and _uuid(value) is None:
                raise OperationsValidationError(f"{name} is invalid")
        if kind is not None and kind not in _MODELS:
            raise OperationsValidationError("work kind is invalid")
        project_id, space_id = _uuid(project_id), _uuid(space_id)
        limit = max(1, min(int(limit), 100))
        scope = _page_scope(
            actor, {"project": project_id, "space": space_id, "kind": kind}
        )
        position = _cursor(cursor, scope)
        # Re-evaluate live authorization on EVERY continuation. A cursor grants
        # no authority and its creation boundary only defers newer records.
        projects = set(await self.operations._authorized_project_ids(session, actor))
        project_rows = (
            (await session.execute(select(Project).where(Project.id.in_(projects))))
            .scalars()
            .all()
        )
        spaces = {row.id: row.space_id for row in project_rows}
        if space_id is not None:
            projects = {
                identity for identity in projects if spaces.get(identity) == space_id
            }
        if project_id is not None:
            projects &= {project_id}
        read_at = dt(position["read_at"]) if position else datetime.utcnow()
        queries = []
        for source_kind, model in _MODELS.items():
            if kind is not None and kind != source_kind:
                continue
            allowed = model.project_id.in_(projects)
            if model is not Task and project_id is None and space_id is None:
                personal = model.project_id.is_(None)
                if not _is_admin(actor):
                    personal = and_(personal, model.owner_user_id == viewer)
                allowed = or_(allowed, personal)
            query = select(
                literal(source_kind).label("kind"),
                _identity(model.id).label("id"),
                model.created_at.label("at"),
            ).where(allowed, model.created_at <= read_at)
            if model is Task:
                query = query.where(
                    Task.deleted_at.is_(None), Task.archived_at.is_(None)
                )
            queries.append(query)
        ledger = union_all(*queries).subquery()
        available = (
            await session.execute(select(func.count()).select_from(ledger))
        ).scalar_one()
        query = select(ledger)
        if position:
            at, identity = dt(position["at"]), _uuid(position["id"]).hex
            query = query.where(
                or_(
                    ledger.c.at < at,
                    and_(
                        ledger.c.at == at,
                        or_(
                            ledger.c.kind > position["kind"],
                            and_(
                                ledger.c.kind == position["kind"],
                                ledger.c.id > identity,
                            ),
                        ),
                    ),
                )
            )
        candidates = (
            await session.execute(
                query.order_by(desc(ledger.c.at), ledger.c.kind, ledger.c.id).limit(
                    limit + 1
                )
            )
        ).all()
        selected = candidates[:limit]
        items = []
        for row in selected:
            source = await session.get(_MODELS[row.kind], UUID(row.id))
            items.append(await self._item(session, actor, row.kind, source, spaces))
        await self._agent_names(session, actor, items)
        next_cursor = None
        if len(candidates) > limit:
            last = selected[-1]
            payload = dict(
                scope=scope,
                read_at=read_at.isoformat(),
                at=last.at.isoformat(),
                kind=last.kind,
                id=last.id,
            )
            next_cursor = (
                base64.urlsafe_b64encode(
                    json.dumps(payload, separators=(",", ":")).encode()
                )
                .decode()
                .rstrip("=")
            )
        return dict(
            schema_version="work-read-projection-v1",
            observed_at=iso(datetime.utcnow()),
            items=items,
            page=dict(
                limit=limit,
                fetched=len(items),
                available=available,
                has_more=next_cursor is not None,
                next_cursor=next_cursor,
            ),
        )

    async def _agent_names(self, session, actor, items):
        """Batch discovery labels within already-authorized business rows.

        Creators/global admins can browse their Agent identities. Other labels
        require an active, in-window AgentProjectGrant with canonical read ACL;
        a visible Project or a Work's Agent ID alone is never such a grant.
        """
        identities = {
            _uuid(row[key])
            for item in items
            for rows, key in (("attempts", "assigned_agent_id"), ("runs", "agent_id"))
            for row in item[rows]
            if row[key] is not None
        }
        if not identities:
            return
        viewer = _uuid(_actor_value(actor, "id") or _actor_value(actor, "user_id"))
        projects = {_uuid(item["project_id"]) for item in items if item["project_id"]}
        now = datetime.utcnow()
        grants = await optional_rows(
            session,
            select(AgentProjectGrant)
            .options(
                load_only(
                    AgentProjectGrant.agent_id,
                    AgentProjectGrant.project_id,
                    AgentProjectGrant.permissions,
                )
            )
            .where(
                AgentProjectGrant.agent_id.in_(identities),
                AgentProjectGrant.project_id.in_(projects),
                AgentProjectGrant.state == "active",
                or_(
                    AgentProjectGrant.active_from.is_(None),
                    AgentProjectGrant.active_from <= now,
                ),
                or_(
                    AgentProjectGrant.active_until.is_(None),
                    AgentProjectGrant.active_until > now,
                ),
            ),
        )
        permitted = {
            (row.agent_id, row.project_id)
            for row in grants or []
            if normalize_project_member_permissions(row.permissions).get("read") is True
        }
        agent_rows = await optional_rows(
            session,
            select(Agent)
            .options(load_only(Agent.created_by, Agent.display_name))
            .where(
                Agent.id.in_(identities),
                or_(
                    _is_admin(actor),
                    Agent.created_by == viewer,
                    Agent.id.in_({identity for identity, _project in permitted}),
                ),
            ),
        )
        if grants is None or agent_rows is None:
            for item in items:
                if any(row["assigned_agent_id"] for row in item["attempts"]) or any(
                    row["agent_id"] for row in item["runs"]
                ):
                    item["gaps"] = sorted(
                        set(item["gaps"] + ["agent_names_unavailable"])
                    )
        names = {row.id: row for row in agent_rows or []}
        for item in items:
            for rows, key, name_key in (
                ("attempts", "assigned_agent_id", "assigned_agent_display_name"),
                ("runs", "agent_id", "agent_display_name"),
            ):
                for row in item[rows]:
                    identity = _uuid(row[key])
                    agent = names.get(identity)
                    if agent is not None and (
                        _is_admin(actor)
                        or agent.created_by == viewer
                        or (identity, _uuid(item["project_id"])) in permitted
                    ):
                        row[name_key] = _safe_value(agent.display_name)[:160] or None

    async def _can_decide(self, session, actor, source):
        if (
            _actor_value(actor, "is_agent", False)
            or _actor_value(actor, "actor_type") == "agent"
        ):
            return False
        if _actor_value(actor, "actor_type") != "human" and not _is_admin(actor):
            return False
        viewer = _uuid(_actor_value(actor, "id") or _actor_value(actor, "user_id"))
        if source.project_id is None:
            return _is_admin(actor) or viewer == source.owner_user_id
        project = await session.get(Project, source.project_id)
        member = (
            (
                await session.execute(
                    select(ProjectMember).where(
                        ProjectMember.project_id == source.project_id,
                        ProjectMember.user_id == viewer,
                    )
                )
            )
            .scalars()
            .first()
        )
        return has_effective_project_permission(
            user_id=viewer,
            user_role=_actor_value(actor, "role"),
            project_owner_id=project.owner_id,
            member_permissions=member.permissions if member else None,
            permission="write",
        )

    async def _item(self, session, actor, kind, source, spaces):
        item = dict(
            ref=dict(kind=kind, id=str(source.id)),
            project_id=str(source.project_id) if source.project_id else None,
            space_id=str(spaces[source.project_id])
            if spaces.get(source.project_id)
            else None,
            title=safe_opportunity_title(
                source.title, source.source_url, source.source_text
            )
            if kind == "opportunity"
            else _safe_value(source.title)[:500],
            business_status=_status(source.status, _STATES[kind]),
            created_at=iso(source.created_at),
            task_due_at=iso(source.end_at) if kind == "task" else None,
            content_scheduled_at=iso(source.scheduled_at)
            if kind == "content_item"
            else None,
            last_recorded_phase=None,
            last_recorded_phase_at=None,
            last_recorded_phase_run_id=None,
            attempts=[],
            runs=[],
            artifacts=[],
            actions=[],
            decisions=[],
            costs=[],
            gaps=[],
            execution_availability="not_advertised",
        )
        if item["business_status"] == "unknown":
            item["gaps"].append("business_status_unknown")
        target = (
            ExternalAction.opportunity_id
            if kind == "opportunity"
            else ExternalAction.content_item_id
        )
        actions = (
            await optional_rows(
                session,
                select(ExternalAction)
                .where(target == source.id, _scope_condition(ExternalAction, source))
                .order_by(ExternalAction.created_at, ExternalAction.id)
                .limit(101),
            )
            if kind != "task"
            else []
        )
        if actions is None or len(actions) > 100:
            item["gaps"].append("actions_partial")
        if actions is not None:
            if any(row.opportunity_id and row.content_item_id for row in actions):
                item["gaps"].append("action_source_link_ambiguous")
            actions = [
                row
                for row in actions
                if not (row.opportunity_id and row.content_item_id)
            ]
        project_match = (
            AgentWorkItem.project_id == source.project_id
            if source.project_id
            else AgentWorkItem.project_id.is_(None)
        )
        work_query = select(AgentWorkItem).where(
            project_match,
            or_(
                AgentWorkItem.space_id.is_(None),
                AgentWorkItem.space_id == spaces.get(source.project_id),
            ),
        )
        if source.project_id is None and not _is_admin(actor):
            # Nullable-project Work has no owner column; its existing detail
            # API is admin-only. A Space/source ID is not a substitute grant.
            work_query = work_query.where(False)
            item["gaps"].append("personal_work_owner_unrecorded")
        if kind == "task":
            source_match = and_(
                AgentWorkItem.source_type == "task",
                _identity(AgentWorkItem.source_id) == source.id.hex,
                or_(
                    AgentWorkItem.task_id.is_(None), AgentWorkItem.task_id == source.id
                ),
            )
        elif kind == "opportunity":
            source_match = and_(
                AgentWorkItem.source_type.in_(
                    ("marketplace_crowdworks", "marketplace_crowdworks_fulfillment")
                ),
                _identity(AgentWorkItem.source_id) == source.id.hex,
            )
        else:
            plans = await optional_rows(
                session,
                select(GenerationPlan)
                .where(
                    GenerationPlan.content_item_id == source.id,
                    _scope_condition(GenerationPlan, source),
                )
                .order_by(GenerationPlan.id)
                .limit(101),
            )
            variants = await optional_rows(
                session,
                select(ContentVariant)
                .where(
                    ContentVariant.content_item_id == source.id,
                    _scope_condition(ContentVariant, source),
                )
                .order_by(ContentVariant.id)
                .limit(101),
            )
            if (
                plans is None
                or variants is None
                or len(plans) > 100
                or len(variants) > 100
            ):
                item["gaps"].append("content_attempt_links_partial")
            source_match = or_(
                and_(
                    AgentWorkItem.source_type == "media.generation.plan",
                    _identity(AgentWorkItem.source_id).in_(
                        [row.id.hex for row in (plans or [])[:100]]
                    ),
                ),
                and_(
                    AgentWorkItem.source_type == "media.publication",
                    _identity(AgentWorkItem.source_id).in_(
                        [row.id.hex for row in (variants or [])[:100]]
                    ),
                ),
            )
        work_query = work_query.where(
            or_(
                source_match,
                and_(
                    AgentWorkItem.source_type == "external_action",
                    _identity(AgentWorkItem.source_id).in_(
                        [row.id.hex for row in (actions or [])[:100]]
                    ),
                ),
            )
        )
        roots = await optional_rows(
            session,
            work_query.order_by(AgentWorkItem.created_at, AgentWorkItem.id).limit(101),
        )
        if roots is None:
            item["gaps"].append("work_ledger_unavailable")
        if roots and len(roots) > 100:
            item["gaps"].append("attempts_truncated")
        work_ids = await _owned_work_ids(
            session,
            kind,
            source,
            (roots or [])[:100],
            spaces.get(source.project_id),
            item["gaps"],
        )
        work_rows = (
            (
                await session.execute(
                    select(AgentWorkItem)
                    .where(AgentWorkItem.id.in_(work_ids))
                    .order_by(AgentWorkItem.created_at, AgentWorkItem.id)
                )
            )
            .scalars()
            .all()
            if work_ids
            else []
        )
        run_ids = await _owned_run_ids(session, kind, source, work_rows, item["gaps"])
        run_rows = (
            (
                await session.execute(
                    select(AgentRun)
                    .options(
                        load_only(
                            AgentRun.work_item_id,
                            AgentRun.parent_run_id,
                            AgentRun.status,
                            AgentRun.created_at,
                            AgentRun.agent_id,
                            AgentRun.agent_revision_id,
                            AgentRun.acting_subagent_id,
                            AgentRun.work_item_attempt,
                            AgentRun.started_at,
                            AgentRun.ended_at,
                            AgentRun.last_event_at,
                        )
                    )
                    .where(
                        AgentRun.id.in_(run_ids),
                        or_(
                            AgentRun.work_item_id.in_(work_ids),
                            AgentRun.work_item_id.is_(None),
                        ),
                    )
                    .order_by(AgentRun.created_at, AgentRun.id)
                )
            )
            .scalars()
            .all()
            if run_ids
            else []
        )
        # Reuse the metadata-only event projection, scoped to exact owned IDs.
        # Do not infer phases from status, arbitrary prose or a Project join.
        try:
            async with session.begin_nested():
                phase_events = await latest_phase_events(
                    session, [row.id for row in run_rows]
                )
        except Exception as exc:
            from sqlalchemy.exc import DBAPIError

            if not isinstance(exc, DBAPIError):
                raise
            phase_events = {}
            item["gaps"].append("phase_events_unavailable")
        recorded = [
            event for event in phase_events.values() if phase(event) is not None
        ]
        latest = max(
            recorded,
            key=lambda event: (
                dt(event.created_at) or datetime.min,
                event.sequence,
                str(event.run_id),
                str(event.id),
            ),
            default=None,
        )
        if latest is not None:
            item["last_recorded_phase"] = phase(latest)
            item["last_recorded_phase_at"] = iso(latest.created_at)
            item["last_recorded_phase_run_id"] = str(latest.run_id)
        item["runs"] = [
            dict(
                id=str(row.id),
                work_item_id=str(row.work_item_id)
                if row.work_item_id in work_ids
                else None,
                parent_run_id=str(row.parent_run_id)
                if row.parent_run_id in run_ids
                else None,
                status=_status(row.status, _RUN_STATES),
                created_at=iso(row.created_at),
                agent_id=str(row.agent_id) if row.agent_id else None,
                agent_revision_id=str(row.agent_revision_id)
                if row.agent_revision_id
                else None,
                agent_display_name=None,
                acting_subagent_id=code(row.acting_subagent_id),
                work_item_attempt=row.work_item_attempt,
                started_at=iso(row.started_at),
                ended_at=iso(row.ended_at),
                last_event_at=iso(row.last_event_at),
                last_recorded_phase=phase(phase_events[str(row.id)])
                if str(row.id) in phase_events
                else None,
                last_recorded_phase_at=iso(phase_events[str(row.id)].created_at)
                if str(row.id) in phase_events
                else None,
            )
            for row in run_rows
        ]
        by_run = {row.id: row for row in run_rows}
        for work in work_rows:
            current = by_run.get(work.active_agent_run_id)
            if current is not None and current.work_item_id != work.id:
                current = None
            if work.active_agent_run_id is None:
                candidates = [
                    row
                    for row in run_rows
                    if row.work_item_id == work.id
                    and row.work_item_attempt is not None
                    and row.work_item_attempt == work.attempt_count
                ]
                current = candidates[0] if len(candidates) == 1 else None
                if len(candidates) > 1:
                    item["gaps"].append("current_run_link_ambiguous")
            current_status = (
                _status(current.status, _RUN_STATES) if current else "unknown"
            )
            if current_status in {"queued", "running"}:
                expiry = dt(work.lease_expires_at)
                if (
                    work.state not in {"claimed", "running"}
                    or expiry is None
                    or expiry <= datetime.utcnow()
                ):
                    current_status = "unknown"
                    item["gaps"].append("runtime_state_unobserved")
            item["attempts"].append(
                dict(
                    id=str(work.id),
                    parent_work_item_id=str(work.parent_work_item_id)
                    if work.parent_work_item_id in work_ids
                    else None,
                    state=_status(work.state, _WORK_STATES),
                    current_run_id=str(current.id) if current else None,
                    current_run_status=current_status,
                    linkage="recorded" if current else "missing",
                    assigned_agent_id=str(work.assigned_agent_id)
                    if work.assigned_agent_id
                    else None,
                    agent_revision_id=str(work.agent_revision_id)
                    if work.agent_revision_id
                    else None,
                    assigned_agent_display_name=None,
                    deadline=iso(work.deadline),
                    not_before=iso(work.not_before),
                    started_at=iso(work.started_at),
                    completed_at=iso(work.completed_at),
                    cancelled_at=iso(work.cancelled_at),
                    heartbeat_at=iso(work.heartbeat_at),
                    lease_expires_at=iso(work.lease_expires_at),
                    current_phase=phase(phase_events[str(current.id)])
                    if current
                    and current_status != "unknown"
                    and str(current.id) in phase_events
                    else None,
                    current_phase_at=iso(phase_events[str(current.id)].created_at)
                    if current
                    and current_status != "unknown"
                    and str(current.id) in phase_events
                    else None,
                )
            )
            if current is None and (work.attempt_count or work.active_agent_run_id):
                item["gaps"].append("current_run_link_missing")
        if not run_rows:
            item["gaps"].append("run_link_unrecorded")
        await self._artifacts_and_decisions(session, actor, kind, source, item, actions)
        ledger_rows = await _cost_ledger_rows(
            session, source, work_rows, spaces.get(source.project_id), item["gaps"]
        )
        self._costs(work_rows, item, ledger_rows=ledger_rows)
        await self._usage_costs(session, source, run_rows, item)
        item["gaps"] = sorted(set(item["gaps"]))
        return item

    async def _artifacts_and_decisions(
        self, session, actor, kind, source, item, actions
    ):
        can_decide = await self._can_decide(session, actor, source)
        artifacts = set()
        if kind == "opportunity":
            metadata = (
                source.metadata_json if isinstance(source.metadata_json, dict) else {}
            )
            refs = metadata.get("review_artifact_version_ids", [])
            if isinstance(refs, list):
                artifacts.update(
                    identity for value in refs[:20] if (identity := _uuid(value))
                )
            development = metadata.get("development")
            development_status = (
                development.get("status") if isinstance(development, dict) else None
            )
            if development_status is not None:
                development_status = code(development_status, "unknown")
            if (
                can_decide
                and source.status == "ready_for_review"
                and metadata.get("workflow_stage") == "READY_FOR_REVIEW"
                and development_status in {None, "ready_for_review"}
            ):
                item["decisions"].append(
                    dict(kind="deliverable_review", id=str(source.id), version=None)
                )
        elif kind == "content_item":
            revisions = await optional_rows(
                session,
                select(ContentVariantRevision)
                .where(
                    ContentVariantRevision.content_item_id == source.id,
                    _scope_condition(ContentVariantRevision, source),
                )
                .order_by(ContentVariantRevision.created_at, ContentVariantRevision.id)
                .limit(101),
            )
            for row in (revisions or [])[:100]:
                if _same_scope(row, source):
                    # The canonical revision reader authorizes its parent
                    # variant. A dangling/mismatched parent is no usable result.
                    parent = await session.get(ContentVariant, row.content_variant_id)
                    available = (
                        parent is not None
                        and _same_scope(parent, source)
                        and parent.content_item_id == source.id
                    )
                    if not available:
                        item["gaps"].append(
                            "content_result_parent_missing_or_inaccessible"
                        )
                    item["artifacts"].append(
                        dict(
                            kind="content_variant_revision",
                            id=str(row.id),
                            availability="available" if available else "unavailable",
                            content_href=f"/api/operations/media/content-variant-revisions/{row.id}"
                            if available
                            else None,
                        )
                    )
            if revisions is None or len(revisions) > 100:
                item["gaps"].append("content_results_partial")
        for action in (actions or [])[:100]:
            if not _same_scope(action, source):
                continue
            item["actions"].append(
                dict(
                    id=str(action.id),
                    status=code(action.status, "unknown"),
                    version=action.action_version,
                )
            )
            if (
                can_decide
                and action.status == "proposed"
                and action.authorization_mode == "human_approval"
            ):
                item["decisions"].append(
                    dict(
                        kind="external_action",
                        id=str(action.id),
                        version=action.action_version,
                    )
                )
        for identity in sorted(artifacts, key=str):
            artifact = await session.get(ArtifactVersion, identity)
            if artifact is None or not _same_scope(artifact, source):
                # Do not expose an artifact outside the authorized job scope.
                item["gaps"].append("artifact_missing_or_inaccessible")
                continue
            availability = "available"
            try:
                await self.operations.get_artifact_content(session, actor, artifact.id)
            except (OperationsNotFoundError, OperationsConflictError):
                availability = "unavailable"
                item["gaps"].append("artifact_bytes_unavailable")
            item["artifacts"].append(
                dict(
                    kind="artifact_version",
                    id=str(artifact.id),
                    availability=availability,
                    content_href=f"/api/operations/artifacts/{artifact.id}/content"
                    if availability == "available"
                    else None,
                )
            )

    @staticmethod
    def _costs(work_rows, item, *, ledger_rows=None):
        # Generic reserved/consumed are quota UNITS, not money. The Media
        # producer's spent hold is still a quote, not provider actual cost.
        seen = set()
        owned_ids = {row.id for row in work_rows}
        for work in work_rows if ledger_rows is None else ledger_rows:
            try:
                if work.metadata_json is not None and not isinstance(
                    work.metadata_json, dict
                ):
                    raise ValueError("invalid metadata container")
                holds = _holds(work)
            except OperationsValidationError:
                holds = []
                item["gaps"].append("cost_ledger_invalid")
            except ValueError:
                holds = []
                item["gaps"].append("cost_ledger_invalid")
            for hold in holds:
                identity = _uuid(hold["work_id"])
                key = (identity, hold["slot"])
                if identity not in owned_ids or key in seen:
                    continue
                seen.add(key)
                item["costs"].append(
                    dict(
                        category="reservation"
                        if hold["state"] == "reserved"
                        else "estimate",
                        amount=str(Decimal(hold["amount_micros"]) / Decimal(1_000_000)),
                        currency=hold["currency"],
                        source="media_generation_hold",
                    )
                )

    @staticmethod
    async def _usage_costs(session, source, run_rows, item):
        def missing(reason, *, run_id=None):
            evidence = dict(
                category="unmeasured", amount=None, currency=None, source=reason
            )
            if run_id is not None:
                evidence["run_id"] = str(run_id)
            item["costs"].append(evidence)
            item["gaps"].append(reason)

        if not run_rows:
            missing("token_usage_run_link_unrecorded")
            return
        # These Run IDs have already passed typed business ownership, Project
        # and Space checks. Check both ledgers' recorded scopes again, rather
        # than deriving a link from sessions, agents, titles or timestamps.
        scope = and_(
            TokenUsage.project_id == source.project_id,
            AgentRun.project_id == source.project_id,
        )
        if source.project_id is None:
            scope = and_(
                TokenUsage.project_id.is_(None),
                AgentRun.project_id.is_(None),
                TokenUsage.user_id == str(source.owner_user_id),
                AgentRun.user_id == str(source.owner_user_id),
            )
        rows = await optional_rows(
            session,
            select(TokenUsage)
            .join(AgentRun, AgentRun.id == TokenUsage.agent_run_id)
            .options(
                load_only(
                    TokenUsage.id,
                    TokenUsage.agent_run_id,
                    TokenUsage.pricing_status,
                    TokenUsage.provider_reported_cost,
                    TokenUsage.list_total_cost,
                )
            )
            .where(TokenUsage.agent_run_id.in_({row.id for row in run_rows}), scope)
            .order_by(TokenUsage.created_at, TokenUsage.id)
            .limit(MAX_USAGE_ROWS + 1),
        )
        if rows is None:
            # Missing migration/table is a partial read, never a zero cost or
            # a reason to discard the authorized source row.
            missing("token_usage_ledger_unavailable")
            return
        if len(rows) > MAX_USAGE_ROWS:
            missing("token_usage_rows_truncated")
        seen, measured_runs = set(), set()
        for row in rows[:MAX_USAGE_ROWS]:
            if row.id in seen:
                continue
            seen.add(row.id)
            measured_runs.add(row.agent_run_id)
            actual = _money(row.provider_reported_cost)
            estimate = (
                _money(row.list_total_cost)
                if row.pricing_status in {"priced", "free_incentive"}
                else None
            )
            amount = actual if actual is not None else estimate
            item["costs"].append(
                dict(
                    category="actual"
                    if actual is not None
                    else "estimate"
                    if estimate is not None
                    else "unmeasured",
                    amount=amount,
                    currency="USD" if amount is not None else None,
                    source="token_usage",
                    usage_id=str(row.id),
                    run_id=str(row.agent_run_id),
                )
            )
            if amount is None:
                item["gaps"].append("token_usage_cost_unmeasured")
            if (row.provider_reported_cost is not None and actual is None) or (
                row.pricing_status in {"priced", "free_incentive"}
                and row.list_total_cost is not None
                and estimate is None
            ):
                item["gaps"].append("token_usage_cost_invalid")
        for row in run_rows:
            if row.id not in measured_runs:
                missing("token_usage_run_unmeasured", run_id=row.id)
        # Nullable historical rows cannot be assigned to this job. Even a
        # nonempty linked subtotal does not prove that every call was captured.
        missing("token_usage_coverage_unverified")
