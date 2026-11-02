"""Bounded, read-only conversation delegation projection."""
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import and_, or_, select

from ..memory.models import (Agent, AgentRevision, AgentDialogue, AgentDialogueDelivery,
                             AgentDelegation, AgentRun, AgentRunToolCall, AgentWorkItem, Task, AgentProjectGrant, ConversationSession,
                             ConversationParticipant)


class DelegationMapNode(BaseModel):
    id: str
    kind: Literal['conversation', 'dialogue', 'work']
    agent_id: str | None
    agent_revision_id: str | None
    agent_name: str | None
    title: str
    status: str
    work_id: str | None
    dialogue_id: str | None
    task_id: str | None


class DelegationMapEdge(BaseModel):
    id: str
    source: str
    target: str
    kind: Literal['delegation', 'work']


class DelegationMapGraph(BaseModel):
    nodes: list[DelegationMapNode]
    edges: list[DelegationMapEdge]


class DelegationMapResponse(BaseModel):
    schema_version: Literal['conversation-delegation-map-v1']
    session_id: str
    observed_at: str
    graph: DelegationMapGraph
    has_more: bool


def _id(value):
    return str(value) if value is not None else None


def _task_id(value):
    return value.hex if value is not None else None


def _title(value, fallback):
    value = ' '.join(str(value or '').split())
    return value[:100] if value else fallback


def _order_visible_nodes(nodes, created_at):
    """Order already-authorized nodes without changing the selected graph."""
    def rank(item):
        # Match agent_task_listing.STATE_STATUS for active WorkItem aliases.
        status = item.status
        if item.kind == 'work':
            status = {'pending': 'queued', 'claimed': 'running'}.get(status, status)
        if status in {'queued', 'running', 'waiting', 'retry_wait'}:
            return 0
        if status in {'succeeded', 'cancelled'}:
            return 2
        return 1

    ordered = sorted(nodes, key=lambda item: item.id)
    ordered.sort(key=lambda item: created_at[item.id], reverse=True)
    ordered.sort(key=lambda item: rank(item))
    return ordered


async def project_delegation_map(db, conversation, viewer_id):
    """Follow only durable session, delivery, run and work references.

    All queries are bounded and contain a causal or exact-session predicate.
    No message bodies, instructions, arbitrary metadata or runtime writers are used.
    """
    limit = 100
    more = False
    nodes, edges = [], []
    names = {}
    created_at = {}
    root = f'conversation:{conversation.id}'

    async def identity(agent_id, revision_id, project_id):
        key = (agent_id, revision_id, project_id)
        if key not in names:
            name = None
            allowed = True
            if project_id and agent_id:
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                allowed = bool(await db.scalar(select(AgentProjectGrant.id).where(
                    AgentProjectGrant.agent_id == agent_id,
                    AgentProjectGrant.project_id == project_id,
                    AgentProjectGrant.state == 'active',
                    or_(AgentProjectGrant.active_from.is_(None), AgentProjectGrant.active_from <= now),
                    or_(AgentProjectGrant.active_until.is_(None), AgentProjectGrant.active_until > now))
                    .limit(1)))
            if allowed and revision_id and agent_id:
                name = await db.scalar(select(AgentRevision.display_name).where(
                    AgentRevision.id == revision_id, AgentRevision.agent_id == agent_id))
            if allowed and not name and agent_id:
                name = await db.scalar(select(Agent.display_name).where(Agent.id == agent_id))
            names[key] = _title(name, '') or None
        return names[key]

    async def node(node_id, kind, agent_id, revision_id, title, status,
                   work_id=None, dialogue_id=None, task_id=None, project_id=None, created=None):
        created_at[node_id] = created
        scope = project_id if project_id is not None else conversation.project_id
        nodes.append(DelegationMapNode(id=node_id, kind=kind, agent_id=_id(agent_id),
            agent_revision_id=_id(revision_id), agent_name=await identity(agent_id, revision_id, scope),
            title=_title(title, '委任'), status=status, work_id=_id(work_id),
            dialogue_id=_id(dialogue_id), task_id=_task_id(task_id)))

    def edge(source, target, kind):
        edges.append(DelegationMapEdge(id=f'{kind}:{source}:{target}', source=source, target=target, kind=kind))

    await node(root, 'conversation', conversation.agent_id, None,
               conversation.title, 'active' if conversation.is_active else 'inactive')
    try:
        owner = UUID(str(conversation.user_id))
    except ValueError:
        owner = None
    project = conversation.project_id
    allowed_projects = {project} if project is not None else set()
    def project_match(col):
        conditions = [col.in_(allowed_projects)] if allowed_projects else []
        if project is None:
            conditions.append(col.is_(None))
        return or_(*conditions)

    async def allow_child_project(project_id):
        if project_id in allowed_projects:
            return True
        if project_id is None:
            return project is None
        try:
            viewer = UUID(str(viewer_id))
        except (TypeError, ValueError):
            return False
        from ..memory.project_repository import ProjectRepository
        if not await ProjectRepository.has_permission(db, project_id, viewer, 'read'):
            return False
        allowed_projects.add(project_id)
        return True
    session_nodes = {conversation.id: root}
    dialogue_nodes = {}
    work_nodes = {}
    untitled_work = {}
    work_frontier = set()
    pins = set()
    frontier = {conversation.id}
    seen_sessions = set()
    for _ in range(16):
        if not frontier:
            break
        seen_sessions.update(frontier)
        rows = (await db.execute(select(AgentDialogue.id, AgentDialogue.root_session_id,
            AgentDialogue.worker_session_id, AgentDialogue.worker_agent_id,
            AgentDialogue.worker_agent_revision_id, AgentDialogue.state,
            AgentDialogue.task_id, AgentDialogue.parent_dialogue_id, AgentDialogue.project_id, AgentDialogue.created_at).where(
                AgentDialogue.root_session_id.in_(frontier),
                AgentDialogue.owner_user_id == owner).order_by(AgentDialogue.created_at, AgentDialogue.id)
                .limit(limit + 1))).all()
        if len(rows) > limit - len(dialogue_nodes):
            more = True
        rows = rows[:max(0, limit - len(dialogue_nodes))]
        next_frontier = set()
        for row in rows:
            if not await allow_child_project(row.project_id):
                continue
            if row.parent_dialogue_id and row.parent_dialogue_id not in dialogue_nodes:
                more = True
                continue
            if len(nodes) >= limit:
                more = True
                break
            ident = f'dialogue:{row.id}'
            dialogue_nodes[row.id] = ident
            session_nodes[row.worker_session_id] = ident
            parent = dialogue_nodes.get(row.parent_dialogue_id, session_nodes.get(row.root_session_id, root))
            task_id = None
            task_title = None
            if row.task_id:
                task = (await db.execute(select(Task.id, Task.title).where(Task.id == row.task_id,
                    project_match(Task.project_id), Task.deleted_at.is_(None)))).first()
                if task:
                    task_id, task_title = task
            session_title = None
            if not _title(task_title, ''):
                session_title = await db.scalar(select(ConversationSession.title).where(
                    ConversationSession.id == row.worker_session_id,
                    ConversationSession.user_id == str(owner),
                    ConversationSession.project_id == row.project_id,
                    ConversationSession.deleted_at.is_(None)))
            title = _title(task_title, '') or _title(session_title, '担当への依頼')
            await node(ident, 'dialogue', row.worker_agent_id, row.worker_agent_revision_id,
                       title, row.state, dialogue_id=row.id, task_id=task_id, project_id=row.project_id, created=row.created_at)
            edge(parent, ident, 'delegation')
            if row.worker_session_id not in seen_sessions:
                next_frontier.add(row.worker_session_id)
        frontier = next_frontier
        if len(dialogue_nodes) >= limit:
            break
    if frontier:
        more = True

    if dialogue_nodes:
        deliveries = (await db.execute(select(AgentDialogueDelivery.dialogue_id,
            AgentDialogueDelivery.receiver_session_id, AgentDialogueDelivery.receiver_run_id,
            AgentDialogueDelivery.receiver_work_item_id, AgentDialogueDelivery.receiver_agent_id,
            AgentDialogueDelivery.receiver_agent_revision_id).where(
                AgentDialogueDelivery.dialogue_id.in_(dialogue_nodes),
                AgentDialogueDelivery.kind == 'request').order_by(
                    AgentDialogueDelivery.sequence, AgentDialogueDelivery.id).limit(limit + 1))).all()
        if len(deliveries) > limit:
            more = True
        for delivery in deliveries[:limit]:
            if delivery.receiver_work_item_id:
                work_frontier.add(delivery.receiver_work_item_id)
                work_nodes[delivery.receiver_work_item_id] = dialogue_nodes[delivery.dialogue_id]
            if (delivery.receiver_run_id and delivery.receiver_work_item_id
                    and delivery.receiver_agent_id and delivery.receiver_agent_revision_id):
                pins.add((str(delivery.receiver_session_id), str(delivery.receiver_run_id),
                          str(delivery.receiver_work_item_id), str(delivery.receiver_agent_id),
                          str(delivery.receiver_agent_revision_id)))

    # Runs in the exact root session are also source-actor pins. Work belongs
    # to this session and project; this is not a scan of all owned work.
    runs = (await db.execute(select(AgentRun.id, AgentRun.work_item_id, AgentRun.agent_id, AgentRun.agent_revision_id).join(
        AgentWorkItem, AgentWorkItem.id == AgentRun.work_item_id).where(
            AgentRun.session_id == conversation.id,
            AgentWorkItem.conversation_session_id == conversation.id,
            project_match(AgentWorkItem.project_id)).order_by(AgentRun.created_at, AgentRun.id).limit(limit + 1))).all()
    if len(runs) > limit:
        more = True
    for run in runs[:limit]:
        if run.agent_id and run.agent_revision_id:
            pins.add((str(conversation.id), str(run.id), str(run.work_item_id),
                      str(run.agent_id), str(run.agent_revision_id)))

    # Work details impose conversation visibility as well as project access.
    visible_sessions = set((await db.scalars(select(ConversationSession.id).where(
        ConversationSession.id.in_(session_nodes), ConversationSession.deleted_at.is_(None),
        project_match(ConversationSession.project_id),
        or_(ConversationSession.user_id == str(viewer_id), ConversationSession.id.in_(
            select(ConversationParticipant.session_id).where(
                ConversationParticipant.participant_type == 'user',
                ConversationParticipant.participant_id == str(viewer_id),
                ConversationParticipant.status == 'joined')))))).all())
    work_access = or_(AgentWorkItem.conversation_session_id.in_(visible_sessions),
                      and_(AgentWorkItem.conversation_session_id.is_(None),
                           AgentWorkItem.project_id.is_not(None),
                           or_(AgentWorkItem.metadata_json['conversation_session_id'].as_string().is_(None),
                               AgentWorkItem.metadata_json['conversation_session_id'].as_string().in_(
                                   [str(value) for value in visible_sessions]))))

    seen_work = set()
    seen_orders = set()
    expanded_orders = set()
    seen_pins = set()
    for _pass in range(16):
        prior_order_count = len(seen_orders)
        for _ in range(16):
            if not work_frontier:
                break
            rows = (await db.execute(select(AgentWorkItem.id, AgentWorkItem.parent_work_item_id,
                AgentWorkItem.assigned_agent_id, AgentWorkItem.agent_revision_id,
                AgentWorkItem.state, AgentWorkItem.task_id, AgentWorkItem.project_id, AgentWorkItem.created_at,
                AgentWorkItem.source_type, AgentWorkItem.execution_adapter).where(
                    AgentWorkItem.id.in_(work_frontier), project_match(AgentWorkItem.project_id), work_access)
                    .order_by(AgentWorkItem.id).limit(limit + 1))).all()
            work_frontier = set()
            for work in rows:
                if work.id in seen_work:
                    continue
                if len(nodes) >= limit:
                    more = True
                    break
                seen_work.add(work.id)
                parent = work_nodes.get(work.id) or work_nodes.get(work.parent_work_item_id)
                if not parent:
                    continue
                ident = f'work:{work.id}'
                work_nodes[work.id] = ident
                title = None
                if work.task_id:
                    title = await db.scalar(select(Task.title).where(Task.id == work.task_id,
                        project_match(Task.project_id), Task.deleted_at.is_(None)))
                await node(ident, 'work', work.assigned_agent_id, work.agent_revision_id,
                           title or '処理', work.state, work_id=work.id, task_id=work.task_id if title else None,
                           project_id=work.project_id, created=work.created_at)
                if not _title(title, ''):
                    untitled_work[work.id] = (ident, work.source_type, work.execution_adapter)
                edge(parent, ident, 'work')
                # A concrete receiver failure must not appear as active dialogue.
                if work.state in {'failed', 'dead_letter', 'cancelled', 'blocked'}:
                    for item in nodes:
                        if item.id == parent and item.kind == 'dialogue' and item.status in {'queued', 'running', 'waiting'}:
                            item.status = work.state
            if not seen_work:
                break
            children = (await db.execute(select(AgentWorkItem.id, AgentWorkItem.parent_work_item_id).where(
                AgentWorkItem.parent_work_item_id.in_(seen_work), AgentWorkItem.id.not_in(seen_work),
                AgentWorkItem.source_type != 'employee_delegation', project_match(AgentWorkItem.project_id), work_access).order_by(AgentWorkItem.id).limit(limit + 1))).all()
            if len(children) > limit:
                more = True
            work_frontier = {row.id for row in children[:limit]}
            if len(seen_work) >= limit:
                more = more or bool(work_frontier)
                break
        if work_frontier:
            more = True

        # Only the run's server-owned work, session, agent and revision can
        # authorize an order. Do not trust JSON pins by themselves.
        if seen_work:
            linked_runs = (await db.execute(select(AgentRun.id, AgentRun.work_item_id,
                AgentRun.session_id, AgentRun.agent_id, AgentRun.agent_revision_id).join(
                    AgentWorkItem, AgentWorkItem.id == AgentRun.work_item_id).where(
                        AgentRun.work_item_id.in_(seen_work), AgentRun.session_id.in_(visible_sessions),
                        project_match(AgentWorkItem.project_id), work_access)
                        .order_by(AgentRun.id).limit(limit + 1))).all()
            if len(linked_runs) > limit:
                more = True
            for run in linked_runs[:limit]:
                if run.agent_id and run.agent_revision_id:
                    pins.add((str(run.session_id), str(run.id), str(run.work_item_id),
                              str(run.agent_id), str(run.agent_revision_id)))
        # Validate pins (including delivery pins) against persisted runs/work.
        if pins:
            run_ids = [UUID(pin[1]) for pin in pins]
            valid_runs = (await db.execute(select(AgentRun.id, AgentRun.work_item_id,
                AgentRun.session_id, AgentRun.agent_id, AgentRun.agent_revision_id).join(
                    AgentWorkItem, AgentWorkItem.id == AgentRun.work_item_id).where(
                        AgentRun.id.in_(run_ids), AgentRun.session_id.in_(visible_sessions),
                        project_match(AgentWorkItem.project_id), work_access).limit(limit + 1))).all()
            valid = {(str(r.session_id), str(r.id), str(r.work_item_id), str(r.agent_id), str(r.agent_revision_id))
                     for r in valid_runs}
            pins.intersection_update(valid)
            pins.difference_update(seen_pins)
        orders = []
        if pins:
            conditions = [and_(AgentDelegation.source_actor_json['source_session_id'].as_string() == sid,
                AgentDelegation.source_actor_json['source_run_id'].as_string() == rid,
                AgentDelegation.source_actor_json['source_work_item_id'].as_string() == wid,
                AgentDelegation.delegator_agent_id == UUID(aid),
                AgentDelegation.delegator_revision_id == UUID(revid))
                for sid, rid, wid, aid, revid in pins]
            orders = (await db.execute(select(AgentDelegation.id, AgentDelegation.worker_agent_id,
                AgentDelegation.worker_revision_id, AgentDelegation.title, AgentDelegation.source_actor_json,
                AgentDelegation.cancelled_at, AgentDelegation.parent_delegation_id, AgentDelegation.project_id, AgentDelegation.created_at).where(or_(*conditions), project_match(AgentDelegation.project_id),
                    AgentDelegation.created_by == owner).order_by(AgentDelegation.created_at, AgentDelegation.id)
                    .limit(limit + 1))).all()
            if len(orders) > limit:
                more = True
            seen_pins.update(pins)
            pins.clear()
        parent_frontier = seen_orders - expanded_orders
        if parent_frontier:
            children = (await db.execute(select(AgentDelegation.id, AgentDelegation.worker_agent_id,
                AgentDelegation.worker_revision_id, AgentDelegation.title, AgentDelegation.source_actor_json,
                AgentDelegation.cancelled_at, AgentDelegation.parent_delegation_id, AgentDelegation.project_id, AgentDelegation.created_at).where(
                    AgentDelegation.parent_delegation_id.in_(parent_frontier),
                    AgentDelegation.created_by == owner)
                .order_by(AgentDelegation.created_at, AgentDelegation.id).limit(limit + 1))).all()
            expanded_orders.update(parent_frontier)
            if len(children) > limit:
                more = True
            for child in children[:limit]:
                if await allow_child_project(child.project_id):
                    orders.append(child)
        for order in orders[:limit]:
            if order.id in seen_orders:
                continue
            if len(nodes) >= limit:
                more = True
                break
            source = order.source_actor_json
            if order.parent_delegation_id in seen_orders:
                parent = f'order:{order.parent_delegation_id}'
            elif isinstance(source, dict):
                source_work = UUID(source['source_work_item_id'])
                parent = work_nodes.get(source_work) or session_nodes.get(next((sid for sid in session_nodes if str(sid) == source['source_session_id']), None), root)
            else:
                continue
            seen_orders.add(order.id)
            ident = f'order:{order.id}'
            # An order is represented as a pending work node until the actual
            # phase Work exists; its ID is never presented as a work reference.
            phases = (await db.execute(select(AgentWorkItem.id, AgentWorkItem.state, AgentWorkItem.intent_key,
                AgentWorkItem.assigned_agent_id, AgentWorkItem.agent_revision_id, AgentWorkItem.created_at)
                .where(AgentWorkItem.source_type == 'employee_delegation',
                    AgentWorkItem.source_id == str(order.id), project_match(AgentWorkItem.project_id), work_access)
                .order_by(AgentWorkItem.created_at.desc(), AgentWorkItem.id.desc()).limit(limit + 1))).all()
            if len(phases) > limit:
                more = True
            worker_phase = next((item for item in phases if item.intent_key == 'work'
                                 and item.assigned_agent_id == order.worker_agent_id
                                 and item.agent_revision_id == order.worker_revision_id), None)
            await node(ident, 'work', order.worker_agent_id, order.worker_revision_id, order.title,
                       ('cancelled' if order.cancelled_at else
                        worker_phase.state if worker_phase else 'pending'),
                       work_id=None, task_id=None, project_id=order.project_id, created=order.created_at)
            edge(parent, ident, 'delegation')
            phase_ids = set()
            for phase_row in phases[:limit]:
                if len(nodes) >= limit:
                    more = True
                    break
                phase_node = f'work:{phase_row.id}'
                if not any(item.id == phase_node for item in nodes):
                    await node(phase_node, 'work', phase_row.assigned_agent_id,
                               phase_row.agent_revision_id, order.title, phase_row.state,
                               work_id=phase_row.id, project_id=order.project_id, created=phase_row.created_at)
                edge(ident, phase_node, 'work')
                work_nodes[phase_row.id] = phase_node
                seen_work.add(phase_row.id)
                phase_ids.add(phase_row.id)
            if phase_ids:
                children = (await db.execute(select(AgentWorkItem.id, AgentWorkItem.parent_work_item_id).where(
                    AgentWorkItem.parent_work_item_id.in_(phase_ids),
                    AgentWorkItem.source_type != 'employee_delegation',
                    project_match(AgentWorkItem.project_id), work_access).order_by(AgentWorkItem.id).limit(limit + 1))).all()
                if len(children) > limit:
                    more = True
                work_frontier.update(row.id for row in children[:limit])

        if not work_frontier and not (pins - seen_pins) and not (seen_orders - expanded_orders) and len(seen_orders) == prior_order_count:
            break
    else:
        more = True

    # Only pins validated against visible persisted runs may supply a title.
    run_work = {UUID(run_id): UUID(work_id) for _, run_id, work_id, _, _ in seen_pins
                if UUID(work_id) in untitled_work}
    titles = {}
    if run_work:
        calls = (await db.execute(select(AgentRunToolCall.run_id,
            AgentRunToolCall.arguments['title'].as_json()).join(
                AgentRun, AgentRun.id == AgentRunToolCall.run_id).join(
                AgentWorkItem, AgentWorkItem.id == AgentRun.work_item_id).where(
                AgentRunToolCall.run_id.in_(run_work),
                AgentRun.user_id == str(owner),
                or_(AgentRun.project_id == AgentWorkItem.project_id,
                    and_(AgentRun.project_id.is_(None), AgentWorkItem.project_id.is_(None))),
                AgentRunToolCall.tool_name.in_(('delegate_plan', 'delegate', 'delegate_media')))
            .order_by(AgentRunToolCall.created_at, AgentRunToolCall.id).limit(limit + 1))).all()
        for run_id, value in calls[:limit]:
            if isinstance(value, str):
                title = _title(value, '')
                if title:
                    titles.setdefault(run_work[run_id], title)
    by_id = {item.id: item for item in nodes}
    for work_id, (ident, source_type, adapter) in untitled_work.items():
        fallback = '担当との対話' if adapter == 'dialogue_transport' or source_type == 'agent_dialogue' else '処理'
        by_id[ident].title = titles.get(work_id, fallback)

    node_ids = {item.id for item in nodes}
    closed_edges = [item for item in edges if item.source in node_ids and item.target in node_ids]
    more = more or len(closed_edges) != len(edges)
    edges = closed_edges

    nodes = nodes[:1] + _order_visible_nodes(nodes[1:], created_at)

    return DelegationMapResponse(schema_version='conversation-delegation-map-v1',
        session_id=str(conversation.id), observed_at=datetime.now(timezone.utc).isoformat(),
        graph=DelegationMapGraph(nodes=nodes, edges=edges), has_more=more)
