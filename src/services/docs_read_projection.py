"""Bounded, loss-aware document reads over canonical Docs rows.

The fingerprint binds read pages, not writes: it is deliberately not a generic
Docs CAS token. Every page rebuilds and authorizes the projection. A changed
projection requires a fresh read instead of combining incompatible pages.
"""
from __future__ import annotations

import base64
import hashlib
import json
from uuid import UUID

from sqlalchemy import String, cast, literal, select

from ..memory.models import (
    DocsLibrary, KnowledgeField, KnowledgeFieldValue, KnowledgeNode,
    KnowledgeNodeSupertag, KnowledgeSupertag,
)
from .docs_acl import can_read_node, docs_readable_node_predicate
from .docs_graph_service import TASK_FIELD_TO_TASK_UPDATE
from .managed_docs_policy import policy_for_node
from .docs_consistency import docs_id_predicate


SCHEMA = "docs_read_projection.v1"
MAX_NODES = 500
MAX_DEPTH = 32
SEGMENT_CHARS = 1024
MAX_CONTENT_CHARS = 1_000_000
OUTLINE_METADATA_FORMATS = {
    "project_information_doc_block", "project_information_collection",
    "work_intake_collection", "work_intake_item", "work_intake_generated_document",
    "work_intake_generated_block", "work_intake_inline_source", "work_intake_attachment_link",
    "mail_management", "email", "email_message",
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _cursor(fingerprint, offset):
    return base64.urlsafe_b64encode(_json([SCHEMA, fingerprint, offset]).encode()).decode().rstrip("=")


def _offset(cursor, fingerprint, size):
    if cursor == "":
        return 0
    try:
        if not isinstance(cursor, str) or len(cursor) > 512:
            raise ValueError()
        version, previous, offset = json.loads(base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True,
        ))
        if (version != SCHEMA or previous != fingerprint or type(offset) is not int
                or not 0 <= offset < size):
            raise ValueError()
        return offset
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("Docs read cursor is invalid or the document/scope changed; restart the read") from exc


async def build_docs_read_projection(
    service, root, user_id: UUID, *, allowed_node_ids: set[UUID] | None = None,
    turn_project_id: UUID | None = None, depth: int = 8, cursor: str = "",
    page_chars: int = 12000,
    include_manifest: bool = False,
    record_only: bool = False,
) -> dict:
    """Read canonical content as ID-anchored segments with explicit omissions.

    The view covers containment, own descriptions, typed content, tags and
    Fields. It does not traverse placements, backlinks, edges or attachments.
    Limits describe the projection separately from pagination of its segments.
    """
    if type(depth) is not int or not 0 <= depth <= MAX_DEPTH:
        raise ValueError(f"depth must be between 0 and {MAX_DEPTH}")
    if type(page_chars) is not int or not 4096 <= page_chars <= 32000:
        raise ValueError("page_chars must be between 4096 and 32000")
    session = service.session
    root = (await session.execute(select(KnowledgeNode).where(
        KnowledgeNode.id == root.id,
    ).execution_options(populate_existing=True))).scalar_one_or_none()
    if root is None:
        raise PermissionError("Docs node is outside the active scope")
    if (not user_id or (allowed_node_ids is not None and root.id not in allowed_node_ids)
            or not await can_read_node(session, root, user_id)):
        raise PermissionError("Docs node is outside the active scope")
    library = await session.get(DocsLibrary, root.docs_library_id)
    # Containment traversal is independent of ACL. A readable grandchild can
    # exist below an unreadable intermediate, whose identity is never emitted.
    path = literal("|") + cast(KnowledgeNode.id, String) + literal("|")
    tree = select(KnowledgeNode.id.label("id"), literal(0).label("depth"), path.label("path")).where(
        KnowledgeNode.id == root.id,
        KnowledgeNode.docs_library_id == root.docs_library_id,
    ).cte("docs_read_tree", recursive=True)
    if not record_only:
        tree = tree.union_all(select(
            KnowledgeNode.id, tree.c.depth + 1,
            tree.c.path + cast(KnowledgeNode.id, String) + literal("|"),
        ).join(tree, KnowledgeNode.parent_id == tree.c.id).where(
            KnowledgeNode.docs_library_id == root.docs_library_id,
            tree.c.depth < MAX_DEPTH + 1,
            ~tree.c.path.like(literal("%|") + cast(KnowledgeNode.id, String) + literal("|%")),
        ))
    statement = select(KnowledgeNode, tree.c.depth).join(tree, KnowledgeNode.id == tree.c.id).where(
        KnowledgeNode.archived_at.is_(None),
        docs_readable_node_predicate(
            KnowledgeNode, docs_library_id=root.docs_library_id, user_id=user_id,
            library_owner_id=getattr(library, "owner_user_id", None),
        ),
    )
    if allowed_node_ids is not None:
        statement = statement.where(docs_id_predicate(KnowledgeNode.id, allowed_node_ids, session))
    if root.project_id is not None:
        # Match the canonical Project containment contract and legacy outline:
        # malformed cross-Project parent edges are not a document expansion.
        statement = statement.where(KnowledgeNode.project_id == root.project_id)
    statement = service._query_email_turn_visibility(
        statement, docs_library_id=root.docs_library_id, turn_project_id=turn_project_id,
    )
    rows = (await session.execute(statement.order_by(
        tree.c.depth, KnowledgeNode.sort_order, KnowledgeNode.created_at, KnowledgeNode.id,
    ).limit(MAX_NODES + 1).execution_options(populate_existing=True))).all()
    reasons = set()
    if len(rows) > MAX_NODES:
        reasons.add("node_limit")
    if any(level > depth for _, level in rows) and not record_only:
        reasons.add("depth_limit")
    # At the fixed discovery horizon we cannot attest to deeper independent
    # grants; do not claim an unbounded graph census from this bounded read.
    if await session.scalar(select(tree.c.id).where(tree.c.depth == MAX_DEPTH + 1).limit(1)):
        reasons.add("traversal_limit")
    cycle = select(KnowledgeNode.id).join(tree, KnowledgeNode.parent_id == tree.c.id).where(
        KnowledgeNode.docs_library_id == root.docs_library_id,
        tree.c.path.like(literal("%|") + cast(KnowledgeNode.id, String) + literal("|%")),
    ).limit(1)
    if not record_only and await session.scalar(cycle):
        reasons.add("cycle_detected")
    selected = [(node, level) for node, level in rows[:MAX_NODES] if level <= depth and (not record_only or node.id == root.id)]
    selected_ids = {node.id for node, _ in selected}
    if root.id not in selected_ids:
        raise PermissionError("Docs node is outside the active scope")
    # Discovery uses breadth-first order to give bounded reads a useful outline;
    # the model-facing stream is depth-first so each section stays together.
    children = {}
    roots = []
    for entry in selected:
        node, _ = entry
        parent_id = node.parent_id if node.id != root.id and node.parent_id in selected_ids else None
        if parent_id is None:
            roots.append(entry)
        else:
            children.setdefault(parent_id, []).append(entry)
    ordered, visited = [], set()
    pending = list(reversed(roots))
    while pending:
        entry = pending.pop()
        node, _ = entry
        if node.id in visited:
            continue
        visited.add(node.id)
        ordered.append(entry)
        pending.extend(reversed(children.get(node.id, [])))
    selected = ordered + [entry for entry in selected if entry[0].id not in visited]
    segments = []
    content_chars = 0

    def append(node_id, kind, text, **metadata):
        nonlocal content_chars
        text = str(text or "")
        original_chars = len(text)
        available = max(0, MAX_CONTENT_CHARS - content_chars)
        if len(text) > available:
            reasons.add("content_limit")
            text = text[:available]
        content_chars += len(text)
        offset = 0
        while offset < len(text) or (offset == 0 and not text):
            segment = {
                "node_id": str(node_id), "kind": kind, "offset": offset,
                "total_chars": original_chars, "text": "", **metadata,
            }
            length = min(SEGMENT_CHARS, len(text) - offset)
            while length > 1:
                segment["text"] = text[offset:offset + length]
                if len(_json(segment)) <= 2048:
                    break
                length //= 2
            segment["text"] = text[offset:offset + length]
            segments.append(segment)
            if length == 0:
                break
            offset += length

    for node, level in selected:
        if not await can_read_node(session, node, user_id):
            raise PermissionError("Docs access changed during read; restart the read")
        body = node.body_json or {}
        block = {"type": "paragraph"}
        if not isinstance(body, dict):
            body = {}
            reasons.add("unsupported_body_format")
            block = {"type": "unsupported"}
        elif body.get("format") == "doc_block" or (
            not body.get("format") and any(key in body for key in ("block_type", "kind", "type"))
        ):
            kind = body.get("block_type") or body.get("kind") or body.get("type") or "paragraph"
            block = {"type": kind}
            if kind == "checkbox":
                block["checked"] = body.get("checked") is True
            if kind == "paragraph" and node.is_explicit_blank:
                block["blank"] = True
            if kind not in {"paragraph", "heading_1", "heading_2", "heading_3", "checkbox",
                            "quote", "markdown", "code", "content_container"}:
                reasons.add("unsupported_body_format")
            if kind in {"markdown", "code"} and body.get("format") != "doc_block":
                reasons.add("unsupported_body_format")
        elif (body.get("format") and body.get("format") not in OUTLINE_METADATA_FORMATS
              or any(key in body for key in ("verbatim_blocks", "verbatim_content", "bookmark"))
              or ("content" in body and body.get("format") != "doc_block")
              or (body and not body.get("format"))):
            block = {"type": "unsupported"}
            reasons.add("unsupported_body_format")
        if node.node_type == "search":
            reasons.add("dynamic_query_not_materialized")
        policy = policy_for_node(node)
        append(node.id, "title", node.title, depth=level, block=block,
               parent_id=str(node.parent_id) if node.id != root.id and node.parent_id in selected_ids else None,
               node_type=node.node_type, updated_at=node.updated_at.isoformat() if node.updated_at else None,
               created_at=node.created_at.isoformat() if node.created_at else None,
               day_date=node.day_date.isoformat() if node.day_date else None,
               **({"managed_domain": policy.managed_domain, "mutation_tools": sorted(policy.allowed_tools)} if policy else {}))
        if node.description:
            append(node.id, "description", node.description)
        if body.get("format") == "doc_block" and body.get("block_type") in {"markdown", "code"}:
            if isinstance(body.get("content"), str):
                append(node.id, "content", body["content"], block_type=body["block_type"])
            else:
                reasons.add("invalid_typed_content")
        if body.get("format") == "project_information_doc_block" and body.get("blocks"):
            # Match the human Project Information view: accepted live entries
            # only. Source chat/message provenance is not a grant to that chat.
            from ..memory.models import ProjectQaEntry
            from ..memory.project_repository import ProjectRepository
            if node.project_id is None or not await ProjectRepository.has_permission(
                session, project_id=node.project_id, user_id=user_id, permission="read",
            ):
                reasons.add("project_qa_unavailable")
            else:
                entries = (await session.execute(select(ProjectQaEntry).where(
                    ProjectQaEntry.project_id == node.project_id, ProjectQaEntry.deleted_at.is_(None),
                    ProjectQaEntry.review_state == "accepted", ProjectQaEntry.status != "archived",
                ).order_by(ProjectQaEntry.updated_at.desc(), ProjectQaEntry.id).limit(101))).scalars().all()
                if len(entries) > 100:
                    reasons.add("project_qa_limit")
                for entry in entries[:100]:
                    append(node.id, "project_qa", _json({"question": entry.question, "answer": entry.answer,
                        "status": entry.status, "version": entry.version}), field_id=str(entry.id), editable=False)
        # Do not dump body_json: managed metadata is not document prose.
        tags = (await session.execute(select(KnowledgeSupertag.id, KnowledgeSupertag.name).join(
            KnowledgeNodeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id,
        ).where(KnowledgeNodeSupertag.node_id == node.id,
                KnowledgeSupertag.docs_library_id == node.docs_library_id,
        ).order_by(KnowledgeSupertag.id).limit(101))).all()
        if len(tags) > 100:
            reasons.add("tag_limit")
            tags = tags[:100]
        for tag_id, name in tags:
            append(node.id, "tag", name, tag_id=str(tag_id))
        fields = (await session.execute(select(KnowledgeField, KnowledgeFieldValue).join(
            KnowledgeFieldValue, KnowledgeFieldValue.field_id == KnowledgeField.id,
        ).where(KnowledgeFieldValue.node_id == node.id,
                KnowledgeField.docs_library_id == node.docs_library_id,
        ).order_by(KnowledgeField.sort_order, KnowledgeField.id).limit(201)
          .execution_options(populate_existing=True))).all()
        if len(fields) > 200:
            reasons.add("field_limit")
            fields = fields[:200]
        definitions = await service.resolve_node_fields(node)
        distinct_definitions = {field.id: field for field in definitions.values()}
        if len(distinct_definitions) > 200:
            reasons.add("field_limit")
        stored_ids = {field.id for field, _ in fields}
        for field in list(distinct_definitions.values())[:200]:
            if field.id not in stored_ids and field.system_key not in TASK_FIELD_TO_TASK_UPDATE:
                append(node.id, "field", "", field_id=str(field.id), name=field.name,
                       field_type=field.field_type, is_unset=True, required=bool(field.required))
        task = await service._get_bound_task(node)
        readable_task = task is not None and await service._can_read_bound_task_metadata(
            node=node, task=task, user_id=user_id,
        )
        for field, value in fields:
            if task is not None and field.system_key in TASK_FIELD_TO_TASK_UPDATE:
                continue  # Authoritative Task values are resolved below.
            if field.field_type == "reference" and value.target_node_id is not None:
                if (allowed_node_ids is not None and value.target_node_id not in allowed_node_ids):
                    continue
                if not await service._query_reference_visible(
                    value.target_node_id, user_id=user_id, turn_project_id=turn_project_id,
                ):
                    continue
            append(node.id, "field", service._format_field_value(field, value),
                   field_id=str(field.id), name=field.name, field_type=field.field_type)
        if readable_task:
            for key, attr in TASK_FIELD_TO_TASK_UPDATE.items():
                field = definitions.get(key)
                value = getattr(task, attr, None)
                if field is not None and value is not None:
                    append(node.id, "field", value.isoformat() if hasattr(value, "isoformat") else str(value),
                           field_id=str(field.id), name=field.name, field_type=field.field_type, source="task")
    binding = {
        "schema": SCHEMA, "actor": str(user_id), "root": str(root.id), "depth": depth,
        "turn_project": str(turn_project_id),
        "record_only": record_only,
        "scope": sorted(map(str, allowed_node_ids)) if allowed_node_ids is not None else None,
        "segments": segments, "reasons": sorted(reasons),
    }
    fingerprint = _hash(binding)
    start = _offset(cursor, fingerprint, len(segments))
    end, used = start, 0
    while end < len(segments) and end - start < 64:
        cost = len(_json(segments[end]))
        if end > start and used + cost > page_chars - 1600:
            break
        used += cost
        end += 1
    return {
        "schema": SCHEMA, "view": "document", "root_id": str(root.id),
        "read_fingerprint": fingerprint, "fingerprint_is_write_revision": False,
        "consistency": "live_read",
        "coverage_complete": not reasons, "coverage_reasons": sorted(reasons),
        "coverage": "authorized containment, descriptions, typed content, tags and fields",
        "excluded_relations": ["placements", "backlinks", "edges", "attachments"],
        "projected_nodes": len(selected), "depth": depth, "max_nodes": MAX_NODES,
        "segments": segments[start:end], "page_start": start, "page_end": end,
        "has_more": end < len(segments),
        "next_cursor": _cursor(fingerprint, end) if end < len(segments) else None,
        **({"node_ids": list(map(str, selected_ids))} if include_manifest else {}),
    }
