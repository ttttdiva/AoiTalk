"""Project information Docs canonical storage helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import (
    KnowledgeNode,
    KnowledgeNodeSupertag,
    KnowledgeRevision,
    KnowledgeSearchIndex,
    KnowledgeSupertag,
    DocsLibrary,
    Project,
)
from .docs_workspace import ensure_project_docs_library
from .docs_graph_service import DocsGraphService


PROJECT_INFORMATION_SUPERTAG = "案件情報"
PROJECT_INFORMATION_SYSTEM_KEY = "project_info"
PROJECT_INFORMATION_ROOT_SYSTEM_KEY = "project_information_root"
PROJECT_INFORMATION_SECTIONS = (
    "概要",
    "進捗",
    "課題管理",
    "決定事項",
    "確認事項",
    "要確認",
    "構成",
    "詳細設計",
    "検証",
    "参照",
)


def _clean_markdown(value: Any, *, max_chars: int = 200000) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    return text[:max_chars]


def _project_information_body_json() -> dict[str, Any]:
    return {
        "format": "project_information_doc_block",
        "source": "docs_canonical",
        "blocks": [{"type": "project_qa_block", "source": "project_qa_entries"}],
    }


def is_default_inbox_project(project: Project) -> bool:
    metadata = project.project_metadata if isinstance(project.project_metadata, dict) else {}
    return bool(
        metadata.get("isInboxDefault") is True
        or project.slug == f"inbox-project-{project.owner_id}"
    )


async def ensure_project_information_root(
    session: AsyncSession,
    *,
    docs_library_id: UUID,
    user_id: UUID | None,
) -> KnowledgeNode:
    library = await session.get(DocsLibrary, docs_library_id)
    if library is None:
        raise ValueError("Docs Libraryが見つかりません")
    owner_id = getattr(library, "owner_user_id", None)
    if (
        str(getattr(library, "library_type", "personal") or "personal").lower()
        != "personal"
        or owner_id is None
    ):
        raise ValueError("案件情報hubはowner付きPersonal Docs Libraryに限られます")

    await session.execute(
        text("select pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"{docs_library_id}:project-information-root"},
    )
    result = await session.execute(
        select(KnowledgeNode)
        .where(
            KnowledgeNode.docs_library_id == docs_library_id,
            func.btrim(KnowledgeNode.system_key) == PROJECT_INFORMATION_ROOT_SYSTEM_KEY,
        )
        .limit(1)
    )
    root = result.scalar_one_or_none()
    is_owner = str(user_id or "") == str(owner_id or "")
    if root is None:
        # The hub is owner-private Personal metadata.  A Project writer may
        # create/edit project-bound children, but may not bootstrap or repair
        # this hub on behalf of the owner.
        if not is_owner:
            raise PermissionError("案件情報hubの作成・修復はPersonal Library所有者のみ許可されています")
        root_creator = owner_id or user_id
        root = await DocsGraphService(session).create_node(
            docs_library_id=docs_library_id,
            user_id=root_creator,
            title="案件情報",
            system_key=PROJECT_INFORMATION_ROOT_SYSTEM_KEY,
            body_json={"format": "project_information_collection"},
            sort_order=1,
        )
    else:
        root_needs_repair = bool(
            root.docs_library_id != docs_library_id
            or getattr(root, "system_key", None) != PROJECT_INFORMATION_ROOT_SYSTEM_KEY
            or getattr(root, "parent_id", None) is not None
            or getattr(root, "project_id", None) is not None
            or getattr(root, "archived_at", None) is not None
            or getattr(root, "root_page_id", None) not in (None, root.id)
            or getattr(root, "is_explicit_blank", False) is True
        )
        if root_needs_repair and not is_owner:
            raise PermissionError("案件情報hubの修復はPersonal Library所有者のみ許可されています")
        # A valid hub is owner-private metadata.  Project members may resolve
        # it as a read-only parent for their project-bound child, but must not
        # rewrite its title/body, updated_by, search index, or timestamps.
        if not root_needs_repair and not is_owner:
            return root
    root.title = "案件情報"
    root.body_text = root.title
    root.parent_id = None
    root.root_page_id = root.id
    root.project_id = None
    root.archived_at = None
    root.is_explicit_blank = False
    root.updated_by = user_id
    await _upsert_search_index(session, root)
    await session.flush()
    return root


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
    row.docs_library_id = node.docs_library_id
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

    # Every caller eventually takes the Project-information advisory and may
    # touch the Project pointer/node.  Lock the canonical Project first so
    # callers that also hold chat/task rows (for example Project Q&A curation)
    # share one parent-first order with Project deletion/rebinding paths.
    if isinstance(session, AsyncSession):
        locked_project_result = await session.execute(
            select(Project)
            .where(Project.id == project.id)
            .with_for_update()
            .execution_options(populate_existing=True)
            .limit(1)
        )
        locked_project = locked_project_result.scalars().first()
        if locked_project is None:
            raise ValueError("Projectが見つかりません")
        project = locked_project

    if is_default_inbox_project(project):
        raise ValueError("Inboxは案件情報Docsの保存先にできません。実案件を指定してください。")
    if project.deleted_at is not None or project.is_completed:
        # Retained canonical rows are cleanup/inspection artifacts.  Reusing
        # this active bootstrap writer for them would unarchive a stale root
        # and silently resurrect a deleted/completed Project identity.
        raise ValueError(
            "完了/削除済みProjectのcanonical Docsは専用クリーンアップ経路で管理されます"
        )

    # Project information lives in the owner's Personal Docs Library.  The
    # resolver checks Project write membership; node mutations re-check that
    # ACL from ``project_id`` even though the parent hub itself is personal.
    library = await ensure_project_docs_library(
        session,
        project_id=project.id,
        actor_user_id=user_id,
    )
    if (
        str(getattr(library, "library_type", "personal") or "personal").lower()
        != "personal"
        or getattr(library, "owner_user_id", None) != project.owner_id
    ):
        raise ValueError("案件情報Docsは案件所有者のPersonal Docs Libraryに限られます")

    # Project members may write existing project-bound children, but they may
    # not bootstrap or repair owner-private library metadata, the Personal
    # 案件情報 hub, or the canonical Project Information root/tag.  The
    # member resolver above is SELECT-only and validates the complete pointer
    # contract; return that exact node without fallback-tag adoption,
    # supertag creation/rename, or timestamp/search-index writes.
    if str(user_id or "") != str(project.owner_id or ""):
        pointer_id = project.knowledge_node_id
        node = await session.get(KnowledgeNode, pointer_id) if pointer_id else None
        if (
            node is None
            or node.docs_library_id != library.id
            or node.project_id != project.id
            or node.archived_at is not None
            or str(node.system_key or "").strip() != f"project_information:{project.id}"
            or node.parent_id is None
            or node.root_page_id != node.parent_id
            or getattr(node, "is_explicit_blank", False) is True
        ):
            raise PermissionError(
                "Project Docsのcanonical正本は所有者以外には作成・修復できません"
            )
        exact_tag = await session.scalar(
            select(KnowledgeNodeSupertag.node_id)
            .join(KnowledgeSupertag, KnowledgeSupertag.id == KnowledgeNodeSupertag.supertag_id)
            .where(
                KnowledgeNodeSupertag.node_id == node.id,
                KnowledgeSupertag.docs_library_id == library.id,
                func.btrim(KnowledgeSupertag.system_key) == PROJECT_INFORMATION_SYSTEM_KEY,
            )
            .limit(1)
        )
        if exact_tag is None:
            raise PermissionError(
                "Project Docsのcanonical project_infoタグは所有者以外には作成・修復できません"
            )
        return node

    root = await ensure_project_information_root(
        session,
        docs_library_id=library.id,
        user_id=user_id,
    )
    await session.execute(
        text("select pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"project-information:{project.id}"},
    )

    node = None
    if project.knowledge_node_id:
        # Lock the pointed node before any repair/unarchive write.  Generic
        # Docs archive/delete uses the same node->Project lock order, so a
        # concurrent pointer repair cannot commit an active pointer to a row
        # that was just archived after its preflight read.
        node_result = await session.execute(
            select(KnowledgeNode)
            .where(KnowledgeNode.id == project.knowledge_node_id)
            .with_for_update()
            .limit(1)
        )
        node = node_result.scalars().first()
        if node and (
            node.docs_library_id != library.id
            or node.project_id != project.id
            or node.archived_at is not None
            or node.parent_id != root.id
            or node.root_page_id != root.id
            or str(node.system_key or "").strip() != f"project_information:{project.id}"
            or getattr(node, "is_explicit_blank", False) is True
        ):
            node = None

    # Prefer the canonical system key.  A historical row may have retained
    # only the display name; it can be upgraded by the Project owner before
    # searching for a canonical node, but an arbitrary tag is never treated as
    # the Project Information identity.
    tag_result = await session.execute(
        select(KnowledgeSupertag)
        .where(
            KnowledgeSupertag.docs_library_id == library.id,
            func.btrim(KnowledgeSupertag.system_key) == PROJECT_INFORMATION_SYSTEM_KEY,
        )
        .limit(1)
    )
    supertag = tag_result.scalars().first()
    if supertag is None:
        tag_result = await session.execute(
            select(KnowledgeSupertag)
            .where(
                KnowledgeSupertag.docs_library_id == library.id,
                KnowledgeSupertag.name == PROJECT_INFORMATION_SUPERTAG,
            )
            .limit(1)
        )
        supertag = tag_result.scalars().first()
    if supertag is None:
        supertag = KnowledgeSupertag(
            docs_library_id=library.id,
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
        # This is a canonical owner-side metadata migration, not adoption of
        # an arbitrary tag.  The selected row is constrained to this library
        # and exact display name above.
        supertag.system_key = PROJECT_INFORMATION_SYSTEM_KEY
        await session.flush()

    if node is not None:
        pointer_tag = await session.scalar(
            select(KnowledgeNodeSupertag.node_id).where(
                KnowledgeNodeSupertag.node_id == node.id,
                KnowledgeNodeSupertag.supertag_id == supertag.id,
            )
        )
        if pointer_tag is None:
            # A pointer to an ordinary/stale node is not repaired in place;
            # leave that row untouched and create a fresh canonical root.
            node = None

    if node is None:
        # ``docs_library_id + system_key`` is unique even when an old
        # canonical row was archived or left under a malformed parent.  Lock
        # and reuse that exact row during repair rather than attempting an
        # INSERT that can never satisfy the unique constraint.
        exact_result = await session.execute(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.docs_library_id == library.id,
                func.btrim(KnowledgeNode.system_key) == f"project_information:{project.id}",
            )
            .with_for_update()
            .limit(1)
        )
        node = exact_result.scalars().first()

    # If a pointer was stale/malformed, search only for an already canonical
    # candidate.  A stale ordinary node is never adopted, while an existing
    # canonical candidate is reused so repeated repair cannot create duplicate
    # rows (or race into a unique-key IntegrityError).
    if node is None:
        node_result = await session.execute(
            select(KnowledgeNode)
            .join(
                KnowledgeNodeSupertag,
                KnowledgeNodeSupertag.node_id == KnowledgeNode.id,
            )
            .join(
                KnowledgeSupertag,
                KnowledgeSupertag.id == KnowledgeNodeSupertag.supertag_id,
            )
            .where(
                KnowledgeNode.docs_library_id == library.id,
                KnowledgeNode.project_id == project.id,
                KnowledgeNode.archived_at.is_(None),
                func.btrim(KnowledgeNode.system_key) == f"project_information:{project.id}",
                KnowledgeNode.parent_id == root.id,
                KnowledgeNode.root_page_id == root.id,
                KnowledgeNode.is_explicit_blank.is_(False),
                KnowledgeSupertag.docs_library_id == library.id,
                func.btrim(KnowledgeSupertag.system_key) == PROJECT_INFORMATION_SYSTEM_KEY,
            )
            .order_by(KnowledgeNode.updated_at.desc(), KnowledgeNode.id)
            .limit(1)
        )
        node = node_result.scalars().first()

    created = False
    if node is None:
        node_title = project.name
        node = KnowledgeNode(
            docs_library_id=library.id,
            parent_id=root.id,
            root_page_id=root.id,
            project_id=project.id,
            system_key=f"project_information:{project.id}",
            is_explicit_blank=False,
            title=node_title,
            body_json=_project_information_body_json(),
            body_text=node_title,
            node_type="node",
            sort_order=0,
            created_by=user_id,
            updated_by=user_id,
        )
        session.add(node)
        await session.flush()
        created = True

    canonical_title = (str(project.name or "").strip() or "案件情報")
    node.title = canonical_title
    node.parent_id = root.id
    node.root_page_id = root.id
    node.project_id = project.id
    node.system_key = f"project_information:{project.id}"
    node.archived_at = None
    # A canonical Project-information root is identity-bearing, never an
    # explicit user blank, even if a stale row carried the old marker.
    node.is_explicit_blank = False
    conflicting_identity = await session.execute(
        text(
            """
            with recursive descendants as (
                select id, system_key
                from knowledge_nodes
                where id = :node_id and docs_library_id = :library_id
                union all
                select child.id, child.system_key
                from knowledge_nodes child
                join descendants parent on child.parent_id = parent.id
                where child.docs_library_id = :library_id
            )
            select 1
            from descendants d
            left join projects p on p.knowledge_node_id = d.id
            where d.id <> :node_id
              and (
                (p.id is not null and p.id <> :project_id)
                or btrim(d.system_key) like 'project_information:%'
              )
            limit 1
            """
        ),
        {
            "node_id": node.id,
            "library_id": library.id,
            "project_id": project.id,
        },
    )
    if conflicting_identity.first() is not None:
        raise ValueError("Project canonical hierarchy contains another Project identity")
    # Rebase the complete structural closure, not only rows that happened to
    # retain the stale root_page_id/project_id denormalizers.  This keeps
    # grandchildren visible after repairing an archived/malformed root.
    await session.execute(
        text(
            """
            with recursive descendants as (
                select id
                from knowledge_nodes
                where id = :node_id and docs_library_id = :library_id
                union all
                select child.id
                from knowledge_nodes child
                join descendants parent on child.parent_id = parent.id
                where child.docs_library_id = :library_id
            )
            update knowledge_nodes
            set root_page_id = :root_id,
                project_id = :project_id,
                updated_by = :user_id,
                updated_at = now()
            where id in (select id from descendants)
              and id <> :node_id
            """
        ),
        {
            "node_id": node.id,
            "library_id": library.id,
            "root_id": root.id,
            "project_id": project.id,
            "user_id": user_id,
        },
    )

    legacy_root_body = ""
    if (node.body_text or "").strip() not in {"", node.title.strip()}:
        legacy_root_body = node.body_text
    node.body_text = canonical_title

    if node.docs_library_id != supertag.docs_library_id:
        tag_result = await session.execute(
            select(KnowledgeSupertag)
            .where(
                KnowledgeSupertag.docs_library_id == node.docs_library_id,
                or_(
                    func.btrim(KnowledgeSupertag.system_key) == PROJECT_INFORMATION_SYSTEM_KEY,
                    KnowledgeSupertag.name == PROJECT_INFORMATION_SUPERTAG,
                ),
            )
            .limit(1)
        )
        supertag = tag_result.scalar_one_or_none()
        if supertag is None:
            supertag = KnowledgeSupertag(
                docs_library_id=node.docs_library_id,
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
    if legacy_root_body:
        await service.append_to_section(
            parent=node,
            section_title="概要",
            text=legacy_root_body,
            operation="append",
            user_id=user_id,
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
    # Project.name is the sole canonical label authority.  Historical callers
    # may still pass a proposed title, but accepting it would let an agent or
    # stale mobile retry silently diverge the Project pointer identity.
    canonical_title = str(project.name or "").strip() or "案件情報"
    node.title = canonical_title
    node.body_text = canonical_title

    if section_heading and (body_text is not None or append_text is not None):
        await service.append_to_section(
            parent=node,
            section_title=section_heading,
            text=body_text if body_text is not None else append_text or "",
            operation=operation,
            user_id=user_id,
        )
    elif body_text is not None:
        await service.append_to_section(
            parent=node,
            section_title="概要",
            text=_clean_markdown(body_text),
            operation="replace",
            user_id=user_id,
        )
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
        "docs_library_id": str(node.docs_library_id),
        "parent_id": str(node.parent_id) if node.parent_id else None,
        "root_page_id": str(node.root_page_id) if node.root_page_id else None,
        "project_id": str(node.project_id) if node.project_id else None,
        "system_key": node.system_key,
        "is_explicit_blank": bool(getattr(node, "is_explicit_blank", False)),
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
