"""Knowledge Workspace MCP tools."""

from __future__ import annotations

import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


def _user_context() -> tuple[Optional[uuid.UUID], bool]:
    try:
        from src.tools.os_operations.tools import get_current_user_context

        context = get_current_user_context()
        user_id = context.get("user_id")
        return (uuid.UUID(str(user_id)) if user_id else None), bool(
            context.get("is_admin", False)
        )
    except Exception:
        return None, False


def register(mcp):
    """Register Knowledge Workspace tools with the MCP server."""

    @mcp.tool()
    async def knowledge_search(
        query: str,
        top_n: int = 5,
        project_id: Optional[str] = None,
    ) -> str:
        """Search registered Knowledge Sources."""
        try:
            from src.knowledge.service import KnowledgeSearchFilters, KnowledgeService
            from src.memory.database import get_database_manager

            db = get_database_manager()
            session = await db.get_session()
            actor_user_id, is_admin = _user_context()
            try:
                results = await KnowledgeService.search(
                    session,
                    query=query,
                    actor_user_id=actor_user_id,
                    is_admin=is_admin,
                    filters=KnowledgeSearchFilters(
                        project_id=uuid.UUID(project_id) if project_id else None
                    ),
                    limit=int(top_n),
                )
            finally:
                await session.close()
            if not results:
                return "関連するナレッジ文書が見つかりませんでした。"
            lines = []
            for index, item in enumerate(results, start=1):
                document = item["document"]
                source = item["source"]
                chunk = item["chunk"]
                lines.append(
                    f"{index}. {source['name']} / {document['path']}\n{chunk['text']}"
                )
            return "**Knowledge検索結果:**\n\n" + "\n\n---\n\n".join(lines)
        except Exception as exc:
            logger.exception("Knowledge MCP search failed")
            return f"Knowledge検索でエラーが発生しました: {exc}"

    @mcp.tool()
    async def knowledge_status() -> str:
        """Show registered Knowledge Sources and index status."""
        try:
            from src.knowledge.service import KnowledgeService
            from src.memory.database import get_database_manager

            db = get_database_manager()
            session = await db.get_session()
            actor_user_id, is_admin = _user_context()
            try:
                sources = await KnowledgeService.list_sources(
                    session, actor_user_id=actor_user_id, is_admin=is_admin
                )
            finally:
                await session.close()
            if not sources:
                return "Knowledge Source はまだ登録されていません。"
            return "\n".join(
                f"- {source.name}: {source.status} / documents={source.document_count or 0}"
                for source in sources
            )
        except Exception as exc:
            logger.exception("Knowledge MCP status failed")
            return f"Knowledge状態確認でエラーが発生しました: {exc}"
