"""プロジェクト毎のエージェントメモリDocs正本ストレージ。

Claude Code の ``MEMORY.md`` に相当する、エージェント自動書込み用の記憶置き場を
Docsノードとして保持する。案件情報（``project_information_docs.py``）と同型で、
プロジェクト毎に ``system_key="agent_memory:<project_id>"`` の索引ノードを 1 つ持つ。

索引ノードは案件情報正本（``Project.knowledge_node_id``）の子ではなく、専用の
ルートハブ「エージェントメモリ」配下へ置く。これにより案件情報タブが引く
``tree_nodes``（案件情報正本ノード配下のDocsWorkspaceビュー）へ混入しない一方、
Docsワークスペース本体からは通常のページとして閲覧・編集できる。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import (
    KnowledgeNode,
    KnowledgeNodeSupertag,
    KnowledgeRevision,
    KnowledgeSupertag,
    Project,
)
from .docs_graph_service import DocsGraphService
from .docs_workspace import ensure_docs_workspace


# Supertag（型定義）と索引ノード system_key の接頭辞に用いる基底キー。
AGENT_MEMORY_SYSTEM_KEY = "agent_memory"
# Supertag 用 system_key（案件情報の "project_info" に相当する型キー）。
AGENT_MEMORY_SUPERTAG_SYSTEM_KEY = "agent_memory"
# 全プロジェクトの索引ノードをぶら下げるルートハブの system_key。
AGENT_MEMORY_ROOT_SYSTEM_KEY = "agent_memory_root"

AGENT_MEMORY_SUPERTAG_NAME = "エージェントメモリ"
AGENT_MEMORY_ROOT_TITLE = "エージェントメモリ"
AGENT_MEMORY_EMPTY_PLACEHOLDER = "(まだ記憶はありません)"

# Supertag.ai_instructions に埋め込む保存基準。
AGENT_MEMORY_AI_INSTRUCTIONS = (
    "エージェントメモリはプロジェクト毎の恒久記憶（Claude CodeのMEMORY.md相当）。"
    "同じ訂正を2回受けた事項・コードやDBから導出できない知見・ユーザーの作業上の"
    "嗜好のみを保存する。秘密情報（パスワード・トークン）は保存禁止。"
    "索引ノードは1エントリ1行で簡潔に保ち、詳細は子ノードへ書く。"
    "上限接近時は古い項目を統合・削除して圧縮する。"
)


def _agent_memory_node_system_key(project_id: UUID) -> str:
    return f"{AGENT_MEMORY_SYSTEM_KEY}:{project_id}"


def _agent_memory_body_json() -> dict[str, Any]:
    return {
        "format": "agent_memory_doc_block",
        "source": "docs_canonical",
    }


def _title_mirror(title: Any) -> str:
    """title 由来の検索ミラー本文（不変条件: 改行禁止・500字以内）を返す。"""
    return str(title or "").strip()[:500]


def _revision(
    node: KnowledgeNode,
    *,
    user_id: UUID | None,
    change_summary: str,
) -> KnowledgeRevision:
    return KnowledgeRevision(
        node_id=node.id,
        title=node.title,
        body_json=node.body_json or {},
        body_text=node.body_text or "",
        change_summary=change_summary,
        source_refs_json=[],
        created_by=user_id,
    )


async def _ensure_agent_memory_root(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    user_id: UUID | None,
) -> KnowledgeNode:
    """全プロジェクトの索引ノードを束ねるルートハブを ensure する。"""

    await session.execute(
        text("select pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"{workspace_id}:agent-memory-root"},
    )
    result = await session.execute(
        select(KnowledgeNode)
        .where(
            KnowledgeNode.workspace_id == workspace_id,
            KnowledgeNode.system_key == AGENT_MEMORY_ROOT_SYSTEM_KEY,
        )
        .limit(1)
    )
    root = result.scalar_one_or_none()
    if root is None:
        root = await DocsGraphService(session).create_node(
            workspace_id=workspace_id,
            user_id=user_id,
            title=AGENT_MEMORY_ROOT_TITLE,
            system_key=AGENT_MEMORY_ROOT_SYSTEM_KEY,
            body_json={"format": "agent_memory_collection"},
            sort_order=2,
        )
    root.title = AGENT_MEMORY_ROOT_TITLE
    root.body_text = _title_mirror(root.title)
    root.parent_id = None
    root.root_page_id = root.id
    root.project_id = None
    root.archived_at = None
    root.updated_by = user_id
    await session.flush()
    return root


async def _ensure_agent_memory_supertag(
    session: AsyncSession,
    *,
    workspace_id: UUID,
) -> KnowledgeSupertag:
    """エージェントメモリ用 Supertag を ensure する。"""

    result = await session.execute(
        select(KnowledgeSupertag)
        .where(
            KnowledgeSupertag.workspace_id == workspace_id,
            or_(
                KnowledgeSupertag.system_key == AGENT_MEMORY_SUPERTAG_SYSTEM_KEY,
                KnowledgeSupertag.name == AGENT_MEMORY_SUPERTAG_NAME,
            ),
        )
        .limit(1)
    )
    supertag = result.scalar_one_or_none()
    if supertag is None:
        supertag = KnowledgeSupertag(
            workspace_id=workspace_id,
            system_key=AGENT_MEMORY_SUPERTAG_SYSTEM_KEY,
            name=AGENT_MEMORY_SUPERTAG_NAME,
            # base_type は ck_knowledge_supertags_base_type の許容値内に限る。
            # "project_information" を使うと context_builder の
            # base_type == "project_information" 抽出に混入するため、中立の "note" を用い、
            # 識別は system_key="agent_memory" / name で行う。
            base_type="note",
            description="プロジェクト毎のエージェント恒久記憶（Agent Memory）",
            icon="brain",
            color="#7c3aed",
            template_json=_agent_memory_body_json(),
            pinned_field_ids=[],
            ai_instructions=AGENT_MEMORY_AI_INSTRUCTIONS,
        )
        session.add(supertag)
        await session.flush()
    else:
        if supertag.system_key != AGENT_MEMORY_SUPERTAG_SYSTEM_KEY:
            supertag.system_key = AGENT_MEMORY_SUPERTAG_SYSTEM_KEY
        if not (supertag.ai_instructions or "").strip():
            supertag.ai_instructions = AGENT_MEMORY_AI_INSTRUCTIONS
    return supertag


async def ensure_agent_memory_doc(
    session: AsyncSession,
    project: Project,
) -> KnowledgeNode:
    """対象プロジェクトのエージェントメモリ索引ノードを ensure して返す。

    案件情報とは独立したルートハブ「エージェントメモリ」配下へ、
    ``system_key="agent_memory:<project_id>"`` の索引ノードを 1 つ作る。
    ``Project.knowledge_node_id``（案件情報正本）は変更しない。
    """

    user_id = project.owner_id
    workspace = await ensure_docs_workspace(session, owner_user_id=user_id)
    root = await _ensure_agent_memory_root(
        session, workspace_id=workspace.id, user_id=user_id
    )
    await session.execute(
        text("select pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"{workspace.id}:agent-memory:{project.id}"},
    )

    supertag = await _ensure_agent_memory_supertag(session, workspace_id=workspace.id)
    node_system_key = _agent_memory_node_system_key(project.id)

    # 存在チェックは archived 込みで引く。uq_knowledge_nodes_workspace_system_key
    # により同一 system_key はワークスペース内に最大1件しか存在できないため、
    # ユーザーが Docs UI で索引ノードをアーカイブすると archived 除外の検索では
    # 見つからず、同一 system_key での INSERT がユニーク制約違反となり以後その
    # プロジェクトのメモリが恒久無効化される。archived 済みノードが見つかった場合は
    # 下の else 分岐で archived_at=None にして復活させる。
    result = await session.execute(
        select(KnowledgeNode)
        .where(
            KnowledgeNode.workspace_id == workspace.id,
            KnowledgeNode.project_id == project.id,
            KnowledgeNode.system_key == node_system_key,
        )
        .limit(1)
    )
    node = result.scalar_one_or_none()

    created = False
    if node is None:
        node = KnowledgeNode(
            workspace_id=workspace.id,
            parent_id=root.id,
            root_page_id=root.id,
            project_id=project.id,
            system_key=node_system_key,
            title=project.name,
            body_json=_agent_memory_body_json(),
            body_text=_title_mirror(project.name),
            node_type="node",
            sort_order=0,
            created_by=user_id,
            updated_by=user_id,
        )
        session.add(node)
        await session.flush()
        created = True
    else:
        # ルートハブが再生成された場合などに備えて配置を正規化する。
        node.parent_id = root.id
        node.root_page_id = root.id
        node.project_id = project.id
        node.system_key = node_system_key
        node.archived_at = None
        if not (node.title or "").strip():
            node.title = project.name
        node.body_text = _title_mirror(node.title)
        node.updated_by = user_id

    current_body_json = node.body_json if isinstance(node.body_json, dict) else {}
    if current_body_json.get("format") != "agent_memory_doc_block":
        node.body_json = _agent_memory_body_json()

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

    service = DocsGraphService(session)
    if created:
        session.add(
            _revision(
                node,
                user_id=user_id,
                change_summary="エージェントメモリDocsを作成",
            )
        )
        # 初期の可視プレースホルダは本文正本ではなく子ノードとして持つ
        # （不変条件: body_text は title ミラー固定）。
        await service.create_node(
            workspace_id=workspace.id,
            user_id=user_id,
            parent=node,
            project_id=project.id,
            title=AGENT_MEMORY_EMPTY_PLACEHOLDER,
        )

    await service.upsert_search_index(node)
    await session.flush()
    return node


async def get_agent_memory_doc(
    session: AsyncSession,
    project_id: UUID,
) -> KnowledgeNode | None:
    """存在すればエージェントメモリ索引ノードを返す（副作用なし・archived除外）。"""

    result = await session.execute(
        select(KnowledgeNode)
        .where(
            KnowledgeNode.project_id == project_id,
            KnowledgeNode.system_key == _agent_memory_node_system_key(project_id),
            KnowledgeNode.archived_at.is_(None),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


def serialize_agent_memory_node(node: KnowledgeNode) -> dict[str, Any]:
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
