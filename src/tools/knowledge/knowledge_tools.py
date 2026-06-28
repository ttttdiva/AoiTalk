"""Knowledge Workspace tools for LLM function calling."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import uuid
from typing import Optional

from ..core import tool

logger = logging.getLogger(__name__)

_current_project_id: Optional[str] = None


def set_current_project_context(project_id: Optional[str] = None) -> None:
    """Set the current project context for Knowledge Workspace operations."""
    global _current_project_id
    _current_project_id = project_id
    if project_id:
        logger.debug("Knowledge project context set: %s", project_id)
    else:
        logger.debug("Knowledge project context cleared")


def get_current_project_context() -> Optional[str]:
    """Return the current project context."""
    return _current_project_id


def _run_async_in_thread(coro):
    def run_in_loop():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(run_in_loop).result()


def _user_context() -> tuple[Optional[uuid.UUID], bool]:
    try:
        from ..os_operations.tools import get_current_user_context

        context = get_current_user_context()
        user_id = context.get("user_id")
        return (uuid.UUID(str(user_id)) if user_id else None), bool(
            context.get("is_admin", False)
        )
    except Exception:
        return None, False


async def _search_async(query: str, top_n: int) -> str:
    from ...knowledge.service import KnowledgeSearchFilters, KnowledgeService
    from ...memory.database import get_database_manager

    db = get_database_manager()
    session = await db.get_session()
    actor_user_id, is_admin = _user_context()
    project_id = uuid.UUID(_current_project_id) if _current_project_id else None
    try:
        results = await KnowledgeService.search(
            session,
            query=query,
            actor_user_id=actor_user_id,
            is_admin=is_admin,
            filters=KnowledgeSearchFilters(project_id=project_id),
            limit=top_n,
        )
        if not results:
            return "関連するナレッジ文書が見つかりませんでした。"
        lines = []
        for index, item in enumerate(results, start=1):
            document = item["document"]
            source = item["source"]
            chunk = item["chunk"]
            heading = " > ".join(chunk.get("heading_path") or [])
            citation = f"{source['name']} / {document['path']}"
            if heading:
                citation = f"{citation} / {heading}"
            lines.append(
                f"{index}. {citation}\n"
                f"score={item['score']:.2f}\n"
                f"{chunk['text']}"
            )
        return "**Knowledge検索結果:**\n\n" + "\n\n---\n\n".join(lines)
    finally:
        await session.close()


async def _status_async() -> str:
    from ...knowledge.service import KnowledgeService
    from ...memory.database import get_database_manager

    db = get_database_manager()
    session = await db.get_session()
    actor_user_id, is_admin = _user_context()
    try:
        sources = await KnowledgeService.list_sources(
            session, actor_user_id=actor_user_id, is_admin=is_admin
        )
        if not sources:
            return "Knowledge Source はまだ登録されていません。"
        lines = [
            f"- {source.name}: {source.status} / documents={source.document_count or 0} / chunks={source.chunk_count or 0}"
            for source in sources
        ]
        return "**Knowledge Sources:**\n" + "\n".join(lines)
    finally:
        await session.close()


async def _read_async(document_id: str) -> str:
    from ...knowledge.service import KnowledgeService
    from ...memory.database import get_database_manager

    db = get_database_manager()
    session = await db.get_session()
    actor_user_id, is_admin = _user_context()
    try:
        payload = await KnowledgeService.read_document(
            session,
            actor_user_id=actor_user_id,
            is_admin=is_admin,
            document_id=uuid.UUID(document_id),
        )
        document = payload["document"]
        return f"**{document['path']}**\n\n{payload['content']}"
    finally:
        await session.close()


@tool
def knowledge_search(query: str, top_n: int = 5) -> str:
    """Knowledge Workspaceから関連文書を検索する。

    外部Markdownや案件フォルダなど、登録済みKnowledge Sourceの文書を
    ユーザー権限と現在のプロジェクト文脈に従って検索します。
    """
    try:
        return _run_async_in_thread(_search_async(query, int(top_n)))
    except Exception as exc:
        logger.exception("Knowledge search failed")
        return f"Knowledge検索でエラーが発生しました: {exc}"


@tool
def knowledge_read(document_id: str) -> str:
    """Knowledge Document IDを指定して正本ファイルの現在内容を読む。"""
    try:
        return _run_async_in_thread(_read_async(document_id))
    except Exception as exc:
        logger.exception("Knowledge read failed")
        return f"Knowledge文書の読み取りでエラーが発生しました: {exc}"


@tool
def knowledge_status() -> str:
    """登録済みKnowledge Sourceと同期状態を確認する。"""
    try:
        return _run_async_in_thread(_status_async())
    except Exception as exc:
        logger.exception("Knowledge status failed")
        return f"Knowledge状態確認でエラーが発生しました: {exc}"
