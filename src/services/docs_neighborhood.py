"""Bounded, independently authorized graph context for multi-hop Docs reads."""
from sqlalchemy import case, or_, select
from sqlalchemy.orm import aliased

from ..memory.models import DocsLibrary, KnowledgeEdge, KnowledgeFieldValue, KnowledgeNode, KnowledgeNodePlacement
from .docs_acl import can_read_node, docs_readable_node_predicate
from .docs_read_projection import build_docs_read_projection
from .docs_consistency import docs_id_predicate


async def read_docs_neighborhood(service, root, actor_id, *, scope_ids=None, turn_project_id=None):
    session = service.session
    library = await session.get(DocsLibrary, root.docs_library_id)
    def authorized(statement, target_id):
        target = aliased(KnowledgeNode)
        statement = statement.join(target, target.id == target_id).where(
            target.docs_library_id == root.docs_library_id, target.archived_at.is_(None),
            docs_readable_node_predicate(target, docs_library_id=root.docs_library_id,
                user_id=actor_id, library_owner_id=getattr(library, "owner_user_id", None)),
        )
        if scope_ids is not None:
            statement = statement.where(docs_id_predicate(target.id, scope_ids, session))
        elif root.project_id is not None:
            statement = statement.where(target.project_id == root.project_id)
        return statement
    links, candidates = [], []
    fields = (await session.execute(authorized(select(KnowledgeFieldValue).where(
        KnowledgeFieldValue.node_id == root.id, KnowledgeFieldValue.target_node_id.is_not(None),
    ), KnowledgeFieldValue.target_node_id).order_by(KnowledgeFieldValue.field_id).limit(100))).scalars().all()
    candidates.extend((value.target_node_id, "field_reference", str(value.field_id)) for value in fields)
    edges = (await session.execute(authorized(select(KnowledgeEdge).where(
        or_(KnowledgeEdge.source_node_id == root.id, KnowledgeEdge.target_node_id == root.id),
    ), case((KnowledgeEdge.source_node_id == root.id, KnowledgeEdge.target_node_id),
            else_=KnowledgeEdge.source_node_id)).order_by(KnowledgeEdge.id).limit(100))).scalars().all()
    for edge in edges:
        outgoing = edge.source_node_id == root.id
        candidates.append((edge.target_node_id if outgoing else edge.source_node_id,
                           "outgoing:" + edge.relation_type if outgoing else "incoming:" + edge.relation_type,
                           str(edge.id)))
    placements = (await session.execute(authorized(select(KnowledgeNodePlacement).where(
        or_(KnowledgeNodePlacement.node_id == root.id, KnowledgeNodePlacement.parent_node_id == root.id),
    ), case((KnowledgeNodePlacement.node_id == root.id, KnowledgeNodePlacement.parent_node_id),
            else_=KnowledgeNodePlacement.node_id)).order_by(KnowledgeNodePlacement.id).limit(100))).scalars().all()
    for placement in placements:
        candidates.append((placement.parent_node_id if placement.node_id == root.id else placement.node_id,
                           "placement", str(placement.id)))
    if root.parent_id:
        candidates.append((root.parent_id, "parent", None))
    children = (await session.execute(authorized(select(KnowledgeNode.id).where(
        KnowledgeNode.parent_id == root.id, KnowledgeNode.docs_library_id == root.docs_library_id,
    ), KnowledgeNode.id).order_by(KnowledgeNode.sort_order, KnowledgeNode.id).limit(100))).scalars().all()
    candidates.extend((node_id, "child", None) for node_id in children)
    records = [await build_docs_read_projection(service, root, actor_id, record_only=True,
        allowed_node_ids=scope_ids, turn_project_id=turn_project_id, page_chars=4096)]
    seen = {root.id}
    for node_id, relation, relation_id in candidates:
        if node_id == root.id or (scope_ids is not None and node_id not in scope_ids):
            continue
        node = await session.get(KnowledgeNode, node_id)
        if (node is None or node.archived_at is not None or node.docs_library_id != root.docs_library_id
                or (scope_ids is None and root.project_id is not None and node.project_id != root.project_id)
                or not await can_read_node(session, node, actor_id)
                or not await service._query_reference_visible(node_id, user_id=actor_id, turn_project_id=turn_project_id)):
            continue
        links.append({"node_id": str(node_id), "relation": relation, "relation_id": relation_id})
        if node_id not in seen and len(records) < 3:
            seen.add(node_id)
            records.append(await build_docs_read_projection(service, node, actor_id, record_only=True,
                allowed_node_ids=scope_ids, turn_project_id=turn_project_id, page_chars=4096))
        if len(links) >= 30:
            break
    return {"schema": "docs_neighborhood.v1", "root_id": str(root.id), "links": links, "records": records,
            "coverage_complete": False, "hop_limit": 1,
            "usage": "Bounded authorized neighborhood; use docs_read on linked IDs for further hops/content"}
