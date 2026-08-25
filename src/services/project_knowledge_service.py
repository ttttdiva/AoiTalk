"""Project-to-KnowledgeNode reference management.

Project Information ownership remains represented by ``Project.knowledge_node_id``
and ``KnowledgeNode.project_id``.  This service manages the separate, explicit
references to reusable KnowledgeNodes without changing either canonical pointer.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..memory.database import get_db_session
from ..memory.models import KnowledgeNode, Project, ProjectKnowledgeRef
from ..memory.project_repository import ProjectRepository
from .docs_acl import can_read_node
from .project_context_pack_service import invalidate_project_context_pack


PROJECT_KNOWLEDGE_RELATION_TYPES = frozenset({"canonical", "related", "reference"})
MAX_PROJECT_KNOWLEDGE_PRIORITY = 1_000_000
logger = logging.getLogger(__name__)


class ProjectKnowledgeError(RuntimeError):
    """Base error for Project Knowledge reference operations."""


class ProjectKnowledgeNotFound(ProjectKnowledgeError):
    """A Project or reference target does not exist or is not visible."""


class ProjectKnowledgeConflict(ProjectKnowledgeError):
    """The requested reference conflicts with an existing relation."""


def _uuid(value: UUID | str, *, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a valid UUID") from exc


def _relation_type(value: str) -> str:
    normalized = str(value or "related").strip().casefold()
    if normalized not in PROJECT_KNOWLEDGE_RELATION_TYPES:
        allowed = ", ".join(sorted(PROJECT_KNOWLEDGE_RELATION_TYPES))
        raise ValueError(f"relation_type must be one of: {allowed}")
    return normalized


def _priority(value: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("priority must be an integer") from exc
    if normalized < 0 or normalized > MAX_PROJECT_KNOWLEDGE_PRIORITY:
        raise ValueError(
            f"priority must be between 0 and {MAX_PROJECT_KNOWLEDGE_PRIORITY}"
        )
    return normalized


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _node_dict(node: KnowledgeNode, *, relation_type: str | None = None, priority: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(node.id),
        "node_id": str(node.id),
        "title": node.title,
        "docs_library_id": str(node.docs_library_id),
        "project_id": str(node.project_id) if node.project_id else None,
        "parent_id": str(node.parent_id) if node.parent_id else None,
        "root_page_id": str(node.root_page_id) if node.root_page_id else None,
        "updated_at": _iso(node.updated_at),
    }
    if relation_type is not None:
        payload["relation_type"] = relation_type
    if priority is not None:
        payload["priority"] = priority
    return payload


def _ref_dict(ref: ProjectKnowledgeRef, node: KnowledgeNode) -> dict[str, Any]:
    payload = {
        "id": str(ref.id),
        "project_id": str(ref.project_id),
        "knowledge_node_id": str(ref.knowledge_node_id),
        "relation_type": ref.relation_type,
        "priority": int(ref.priority),
        "created_by": str(ref.created_by),
        "created_at": _iso(ref.created_at),
        "updated_at": _iso(ref.updated_at),
    }
    payload["node"] = _node_dict(
        node,
        relation_type=ref.relation_type,
        priority=int(ref.priority),
    )
    return payload


async def _authorized_project(session: Any, project_id: UUID, actor_user_id: UUID, *, write: bool) -> Project:
    project = await session.get(Project, project_id)
    if project is None or project.deleted_at is not None:
        raise ProjectKnowledgeNotFound("project not found")
    permission = "manage_settings" if write else "read"
    allowed = await ProjectRepository.has_permission(
        session,
        project_id=project_id,
        user_id=actor_user_id,
        permission=permission,
    )
    if not allowed:
        raise PermissionError("project knowledge permission denied")
    return project


async def attach_project_knowledge_ref(
    *,
    project_id: UUID | str,
    knowledge_node_id: UUID | str,
    relation_type: str = "related",
    priority: int = 100,
    actor_user_id: UUID | str,
) -> dict[str, Any]:
    """Attach one ACL-visible KnowledgeNode to a Project."""

    project_uuid = _uuid(project_id, field="project_id")
    node_uuid = _uuid(knowledge_node_id, field="knowledge_node_id")
    actor_uuid = _uuid(actor_user_id, field="actor_user_id")
    relation = _relation_type(relation_type)
    normalized_priority = _priority(priority)

    async with await get_db_session() as session:
        await _authorized_project(session, project_uuid, actor_uuid, write=True)
        node = await session.get(KnowledgeNode, node_uuid)
        if node is None or not await can_read_node(session, node, actor_uuid):
            raise ProjectKnowledgeNotFound("knowledge node not found or inaccessible")

        existing = await session.scalar(
            select(ProjectKnowledgeRef).where(
                ProjectKnowledgeRef.project_id == project_uuid,
                ProjectKnowledgeRef.knowledge_node_id == node_uuid,
            )
        )
        if existing is not None:
            raise ProjectKnowledgeConflict("project knowledge reference already exists")

        ref = ProjectKnowledgeRef(
            id=uuid4(),
            project_id=project_uuid,
            knowledge_node_id=node_uuid,
            relation_type=relation,
            priority=normalized_priority,
            created_by=actor_uuid,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(ref)
        await invalidate_project_context_pack(
            session=session,
            project_id=project_uuid,
            reason="project_knowledge_ref_attached",
        )
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ProjectKnowledgeConflict(
                "project knowledge reference already exists"
            ) from exc
        await session.refresh(ref)
        result = _ref_dict(ref, node)
    # Scheduling is deliberately outside the source transaction.  If commit
    # above rolled back, this code is never reached and no orphan job exists.
    from .project_context_pack_job_service import enqueue_project_context_pack_rebuild

    try:
        await enqueue_project_context_pack_rebuild(
            project_uuid,
            actor_uuid,
            "project_knowledge_ref_attached",
        )
    except Exception:
        logger.exception("Failed to enqueue ProjectContextPack rebuild after attach")
    return result


async def remove_project_knowledge_ref(
    *,
    project_id: UUID | str,
    knowledge_node_id: UUID | str,
    actor_user_id: UUID | str,
) -> dict[str, Any]:
    """Remove one Project reference without changing the target node."""

    project_uuid = _uuid(project_id, field="project_id")
    node_uuid = _uuid(knowledge_node_id, field="knowledge_node_id")
    actor_uuid = _uuid(actor_user_id, field="actor_user_id")

    async with await get_db_session() as session:
        await _authorized_project(session, project_uuid, actor_uuid, write=True)
        ref = await session.scalar(
            select(ProjectKnowledgeRef).where(
                ProjectKnowledgeRef.project_id == project_uuid,
                ProjectKnowledgeRef.knowledge_node_id == node_uuid,
            )
        )
        if ref is None:
            raise ProjectKnowledgeNotFound("project knowledge reference not found")
        await session.delete(ref)
        await invalidate_project_context_pack(
            session=session,
            project_id=project_uuid,
            reason="project_knowledge_ref_removed",
        )
        await session.commit()
        result = {
            "success": True,
            "project_id": str(project_uuid),
            "knowledge_node_id": str(node_uuid),
            "removed": True,
        }
    from .project_context_pack_job_service import enqueue_project_context_pack_rebuild

    try:
        await enqueue_project_context_pack_rebuild(
            project_uuid,
            actor_uuid,
            "project_knowledge_ref_removed",
        )
    except Exception:
        logger.exception("Failed to enqueue ProjectContextPack rebuild after remove")
    return result


async def list_project_knowledge_refs(
    *,
    project_id: UUID | str,
    actor_user_id: UUID | str,
) -> list[dict[str, Any]]:
    """List only references whose target remains ACL-readable."""

    project_uuid = _uuid(project_id, field="project_id")
    actor_uuid = _uuid(actor_user_id, field="actor_user_id")
    async with await get_db_session() as session:
        await _authorized_project(session, project_uuid, actor_uuid, write=False)
        rows = (
            await session.execute(
                select(ProjectKnowledgeRef, KnowledgeNode)
                .join(KnowledgeNode, KnowledgeNode.id == ProjectKnowledgeRef.knowledge_node_id)
                .where(
                    ProjectKnowledgeRef.project_id == project_uuid,
                    KnowledgeNode.archived_at.is_(None),
                )
                .order_by(ProjectKnowledgeRef.priority, ProjectKnowledgeRef.updated_at)
            )
        ).all()
        visible: list[dict[str, Any]] = []
        for ref, node in rows:
            if await can_read_node(session, node, actor_uuid):
                visible.append(_ref_dict(ref, node))
        return visible


async def resolve_project_knowledge(
    *,
    project_id: UUID | str,
    actor_user_id: UUID | str,
) -> dict[str, list[dict[str, Any]]]:
    """Resolve canonical Project Docs plus visible related references."""

    project_uuid = _uuid(project_id, field="project_id")
    actor_uuid = _uuid(actor_user_id, field="actor_user_id")
    async with await get_db_session() as session:
        project = await _authorized_project(session, project_uuid, actor_uuid, write=False)
        canonical_nodes: list[dict[str, Any]] = []
        seen_canonical: set[UUID] = set()
        if project.knowledge_node_id:
            node = await session.get(KnowledgeNode, project.knowledge_node_id)
            if node is not None and await can_read_node(session, node, actor_uuid):
                canonical_nodes.append(_node_dict(node, relation_type="canonical", priority=0))
                seen_canonical.add(node.id)

        rows = (
            await session.execute(
                select(ProjectKnowledgeRef, KnowledgeNode)
                .join(KnowledgeNode, KnowledgeNode.id == ProjectKnowledgeRef.knowledge_node_id)
                .where(
                    ProjectKnowledgeRef.project_id == project_uuid,
                    KnowledgeNode.archived_at.is_(None),
                )
                .order_by(ProjectKnowledgeRef.priority, ProjectKnowledgeRef.updated_at)
            )
        ).all()
        related_nodes: list[dict[str, Any]] = []
        for ref, node in rows:
            if not await can_read_node(session, node, actor_uuid):
                continue
            payload = _node_dict(
                node,
                relation_type=ref.relation_type,
                priority=int(ref.priority),
            )
            if ref.relation_type == "canonical":
                if node.id not in seen_canonical:
                    canonical_nodes.append(payload)
                    seen_canonical.add(node.id)
            else:
                related_nodes.append(payload)
        return {
            "canonical_nodes": canonical_nodes,
            "related_nodes": related_nodes,
        }


__all__ = [
    "MAX_PROJECT_KNOWLEDGE_PRIORITY",
    "PROJECT_KNOWLEDGE_RELATION_TYPES",
    "ProjectKnowledgeConflict",
    "ProjectKnowledgeError",
    "ProjectKnowledgeNotFound",
    "attach_project_knowledge_ref",
    "list_project_knowledge_refs",
    "remove_project_knowledge_ref",
    "resolve_project_knowledge",
]
