"""Compose an authorized local section around retrieved atomic/record hits."""
from ..memory.models import KnowledgeNode
from .docs_acl import can_read_node
from .docs_read_projection import build_docs_read_projection


async def expand_docs_hits(service, nodes, *, actor_id, scope_ids=None, turn_project_id=None):
    anchors, evidence = set(), []
    for hit in nodes[:5]:
        anchor = hit
        if hit.parent_id:
            parent = await service.session.get(KnowledgeNode, hit.parent_id)
            if (parent is not None and parent.archived_at is None
                    and parent.docs_library_id == hit.docs_library_id and parent.project_id == hit.project_id
                    and (scope_ids is None or parent.id in scope_ids)
                    and await can_read_node(service.session, parent, actor_id)):
                anchor = parent
        if anchor.id in anchors:
            continue
        anchors.add(anchor.id)
        page = await build_docs_read_projection(service, anchor, actor_id,
            allowed_node_ids=scope_ids, turn_project_id=turn_project_id, depth=2, page_chars=4096)
        hit_page = await build_docs_read_projection(service, hit, actor_id,
            allowed_node_ids=scope_ids, turn_project_id=turn_project_id, record_only=True, page_chars=4096)
        evidence.append({"hit_id": str(hit.id), "anchor_id": str(anchor.id), "hit": hit_page, "page": page})
        if len(evidence) == 2:
            break
    return {
        "schema": "docs_retrieval.v1", "hit_ids": [str(node.id) for node in nodes], "evidence": evidence,
        "coverage_complete": False,
        "usage": "Local evidence only; continue docs_read on anchors/cursors or docs_overview for corpus coverage",
    }
