"""Project information Docs canonical storage helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import (
    KnowledgeNode,
    KnowledgeNodeSupertag,
    KnowledgeRevision,
    KnowledgeSearchIndex,
    KnowledgeSupertag,
    Project,
)
from .docs_workspace import ensure_docs_workspace
from .docs_graph_service import DocsGraphService


PROJECT_INFORMATION_SUPERTAG = "案件情報"
PROJECT_INFORMATION_SYSTEM_KEY = "project_info"
PROJECT_INFORMATION_SECTIONS = (
    "概要",
    "体制",
    "確認事項",
    "決定事項",
    "課題管理",
    "参照",
    "Q&A",
)


def _clean_markdown(value: Any, *, max_chars: int = 200000) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    return text[:max_chars]


def _initial_project_information_sections(project: Project) -> dict[str, str]:
    overview = project.description.strip() if project.description else "未記入"
    return {
        "概要": overview,
        "体制": "未記入",
        "確認事項": "未記入",
        "決定事項": "未記入",
        "課題管理": "未記入",
        "参照": "未記入",
        "Q&A": "[[project-qa]]",
    }


def _project_information_body_json() -> dict[str, Any]:
    return {
        "format": "project_information_doc_block",
        "source": "docs_canonical",
        "blocks": [{"type": "project_qa_block", "source": "project_qa_entries"}],
    }


def _revision(
    node: KnowledgeNode,
    *,
    user_id: UUID | None,
    change_summary: str,
    source_refs: list[Any] | None = None,
) -> KnowledgeRevision:
    return KnowledgeRevision(
        node_id=node.id,
        title=node.title,
        body_json=node.body_json or {},
        body_text=node.body_text or "",
        change_summary=change_summary,
        source_refs_json=source_refs or [],
        created_by=user_id,
    )


async def _upsert_search_index(session: AsyncSession, node: KnowledgeNode) -> None:
    row = await session.get(KnowledgeSearchIndex, node.id)
    if row is None:
        row = KnowledgeSearchIndex(node_id=node.id)
        session.add(row)
    row.workspace_id = node.workspace_id
    row.project_id = node.project_id
    row.title_text = node.title or ""
    row.body_text_plain = node.body_text or ""
    row.updated_at = datetime.utcnow()


async def ensure_project_information_doc(
    session: AsyncSession,
    *,
    project: Project,
    user_id: UUID | None,
) -> KnowledgeNode:
    """Ensure the single canonical Docs node for a project information page."""

    workspace = await ensure_docs_workspace(session, owner_user_id=user_id)

    node = None
    if project.knowledge_node_id:
        node = await session.get(KnowledgeNode, project.knowledge_node_id)
        if node and (node.project_id != project.id or node.archived_at is not None):
            node = None

    tag_result = await session.execute(
        select(KnowledgeSupertag)
        .where(
            KnowledgeSupertag.workspace_id == workspace.id,
            or_(
                KnowledgeSupertag.system_key == PROJECT_INFORMATION_SYSTEM_KEY,
                KnowledgeSupertag.name == PROJECT_INFORMATION_SUPERTAG,
            ),
        )
        .limit(1)
    )
    supertag = tag_result.scalar_one_or_none()
    if supertag is None:
        supertag = KnowledgeSupertag(
            workspace_id=workspace.id,
            system_key=PROJECT_INFORMATION_SYSTEM_KEY,
            name=PROJECT_INFORMATION_SUPERTAG,
            base_type="project_information",
            description="案件概要、進捗、課題管理、決定事項、参照、Q&Aをまとめる正本ページ",
            icon="book-open",
            color="#2563eb",
            template_json=_project_information_body_json(),
            pinned_field_ids=[],
            ai_instructions=(
                "案件情報ページはプロジェクトの正本として扱う。"
                "既存見出し構造を尊重し、根拠のある事実だけを本文へ追記する。"
            ),
        )
        session.add(supertag)
        await session.flush()

    if node is None and project.knowledge_node_id:
        node = await session.get(KnowledgeNode, project.knowledge_node_id)
        if node and (node.project_id != project.id or node.archived_at is not None):
            node = None

    if node is None:
        node_result = await session.execute(
            select(KnowledgeNode)
            .join(
                KnowledgeNodeSupertag,
                KnowledgeNodeSupertag.node_id == KnowledgeNode.id,
            )
            .where(
                KnowledgeNode.workspace_id == workspace.id,
                KnowledgeNode.project_id == project.id,
                KnowledgeNode.archived_at.is_(None),
                KnowledgeNodeSupertag.supertag_id == supertag.id,
            )
            .order_by(KnowledgeNode.updated_at.desc())
            .limit(1)
        )
        node = node_result.scalar_one_or_none()

    created = False
    if node is None:
        node = KnowledgeNode(
            workspace_id=workspace.id,
            project_id=project.id,
            title=f"{project.name} 案件情報",
            body_json=_project_information_body_json(),
            body_text="",
            node_type="node",
            sort_order=0,
            created_by=user_id,
            updated_by=user_id,
        )
        session.add(node)
        await session.flush()
        created = True

    if node.workspace_id != workspace.id:
        node.workspace_id = workspace.id
        node.updated_at = datetime.utcnow()

    if node.workspace_id != supertag.workspace_id:
        tag_result = await session.execute(
            select(KnowledgeSupertag)
            .where(
                KnowledgeSupertag.workspace_id == node.workspace_id,
                or_(
                    KnowledgeSupertag.system_key == PROJECT_INFORMATION_SYSTEM_KEY,
                    KnowledgeSupertag.name == PROJECT_INFORMATION_SUPERTAG,
                ),
            )
            .limit(1)
        )
        supertag = tag_result.scalar_one_or_none()
        if supertag is None:
            supertag = KnowledgeSupertag(
                workspace_id=node.workspace_id,
                system_key=PROJECT_INFORMATION_SYSTEM_KEY,
                name=PROJECT_INFORMATION_SUPERTAG,
                base_type="project_information",
                description="案件概要、進捗、課題管理、決定事項、参照、Q&Aをまとめる正本ページ",
                icon="book-open",
                color="#2563eb",
                template_json=_project_information_body_json(),
                pinned_field_ids=[],
                ai_instructions=(
                    "案件情報ページはプロジェクトの正本として扱う。"
                    "既存見出し構造を尊重し、根拠のある事実だけを本文へ追記する。"
                ),
            )
            session.add(supertag)
            await session.flush()
    elif supertag.system_key != PROJECT_INFORMATION_SYSTEM_KEY:
        supertag.system_key = PROJECT_INFORMATION_SYSTEM_KEY

    link = await session.get(
        KnowledgeNodeSupertag,
        {"node_id": node.id, "supertag_id": supertag.id},
    )
    if link is None:
        session.add(
            KnowledgeNodeSupertag(
                node_id=node.id,
                supertag_id=supertag.id,
                created_by=user_id,
            )
        )

    if project.knowledge_node_id != node.id:
        project.knowledge_node_id = node.id
        project.updated_at = datetime.utcnow()

    if created:
        session.add(
            _revision(
                node,
                user_id=user_id,
                change_summary="案件情報Docs正本を作成",
            )
        )

    current_body_json = node.body_json if isinstance(node.body_json, dict) else {}
    if current_body_json.get("format") != "project_information_doc_block":
        node.body_json = _project_information_body_json()

    service = DocsGraphService(session)
    await service.ensure_child_sections(
        parent=node,
        titles=PROJECT_INFORMATION_SECTIONS,
        user_id=user_id,
        body_by_title=_initial_project_information_sections(project) if created else None,
    )
    await service.upsert_search_index(node)
    await session.flush()
    return node


async def update_project_information_doc(
    session: AsyncSession,
    *,
    project: Project,
    user_id: UUID | None,
    body_text: str | None = None,
    append_text: str | None = None,
    section_heading: str | None = None,
    operation: str = "append",
    title: str | None = None,
    change_summary: str = "案件情報Docs正本を更新",
    source_refs: list[Any] | None = None,
) -> KnowledgeNode:
    node = await ensure_project_information_doc(session, project=project, user_id=user_id)
    service = DocsGraphService(session)
    if title is not None and str(title).strip():
        node.title = str(title).strip()[:500]

    if section_heading and (body_text is not None or append_text is not None):
        await service.append_to_section(
            parent=node,
            section_title=section_heading,
            text=body_text if body_text is not None else append_text or "",
            operation=operation,
            user_id=user_id,
        )
    elif body_text is not None:
        node.body_text = _clean_markdown(body_text)
    elif append_text is not None:
        await service.append_to_section(
            parent=node,
            section_title="概要",
            text=append_text,
            operation="append",
            user_id=user_id,
        )

    node.updated_by = user_id
    node.updated_at = datetime.utcnow()
    node.body_json = _project_information_body_json()
    await service.upsert_search_index(node)
    session.add(
        _revision(
            node,
            user_id=user_id,
            change_summary=change_summary[:500],
            source_refs=source_refs,
        )
    )
    await session.flush()
    return node


def serialize_project_information_node(node: KnowledgeNode) -> dict[str, Any]:
    return {
        "id": str(node.id),
        "workspace_id": str(node.workspace_id),
        "parent_id": str(node.parent_id) if node.parent_id else None,
        "root_page_id": str(node.root_page_id) if node.root_page_id else None,
        "project_id": str(node.project_id) if node.project_id else None,
        "title": node.title,
        "body_json": node.body_json or {},
        "body_text": node.body_text or "",
        "display_props": node.display_props or {},
        "query_json": node.query_json or None,
        "view_json": node.view_json or {},
        "day_date": node.day_date.isoformat() if node.day_date else None,
        "sort_order": node.sort_order,
        "created_by": str(node.created_by) if node.created_by else None,
        "updated_by": str(node.updated_by) if node.updated_by else None,
        "created_at": node.created_at.isoformat() if node.created_at else None,
        "updated_at": node.updated_at.isoformat() if node.updated_at else None,
        "archived_at": node.archived_at.isoformat() if node.archived_at else None,
    }
