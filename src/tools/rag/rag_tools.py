"""
RAG tools for voice assistant - LLM function tools for document retrieval.
"""

import asyncio
import concurrent.futures
import logging
from typing import Optional

from ..core import tool as function_tool

logger = logging.getLogger(__name__)

# Current project context (set by WebSocket handler or server)
_current_project_id: Optional[str] = None


def set_current_project_context(project_id: Optional[str] = None):
    """Set the current project context for RAG operations.
    
    Should be called by the WebSocket handler when a message is received
    with a project_id context.
    
    Args:
        project_id: Project UUID string, or None for default collection
    """
    global _current_project_id
    _current_project_id = project_id
    if project_id:
        logger.debug(f"RAG project context set: {project_id}")
    else:
        logger.debug("RAG project context cleared (using default)")


def get_current_project_context() -> Optional[str]:
    """Get the current project context.
    
    Returns:
        Current project ID or None
    """
    return _current_project_id


def _get_rag_manager(project_id: Optional[str] = None):
    """Get RAG manager for a specific project.

    Args:
        project_id: Project ID, or None to use current context/default

    Returns:
        RagManager instance
    """
    from ...rag import get_rag_manager

    # Use provided project_id, fall back to current context
    effective_project_id = project_id if project_id is not None else _current_project_id
    return get_rag_manager(project_id=effective_project_id)


def _get_project_collection_names(project_id: Optional[str]) -> list:
    """Get linked collection names for a project from DB.

    Returns empty list if no linkage exists or DB is unavailable.
    """
    if not project_id:
        return []
    try:
        from ...memory.database import get_database_manager
        from ...memory.rag_collection_repository import RagCollectionRepository

        db = get_database_manager()
        if db is None:
            return []
        with db.get_sync_session() as session:
            return RagCollectionRepository.get_active_collection_names_for_project_sync(
                session, project_id
            )
    except Exception as e:
        logger.debug(f"Could not fetch project collections: {e}")
        return []


def _run_async_in_thread(coro):
    """Run async coroutine in a separate thread with its own event loop."""
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


@function_tool
def search_rag(query: str, top_n: int = 5) -> str:
    """RAGベースの文書検索を実行する。
    
    インデックス済みのドキュメント（PDF、テキスト、Markdownなど）から
    クエリに関連する情報を検索します。
    
    検索対象は現在選択中のプロジェクトのドキュメントです。
    プロジェクト未選択時はデフォルトのドキュメントが検索されます。
    
    検索のコツ:
    - 具体的なキーワードを使用する
    - 「私の」「自分の」などの代名詞は避ける
    - 製品名や技術用語など固有名詞を含めると精度が上がる
    
    Args:
        query: 検索クエリ（具体的なキーワード推奨）
        top_n: 返す結果の最大数（デフォルト: 5）
        
    Returns:
        検索結果のテキストとソース情報
    """
    # LLM may pass float (e.g., 5.0) so ensure int for slice operations
    top_n = int(top_n)
    project_id = _current_project_id
    print(f"[Tool] search_rag が呼び出されました: query={query}, top_n={top_n}, project={project_id or 'default'}")

    try:
        # Check if this project has linked collections
        collection_names = _get_project_collection_names(project_id)

        if len(collection_names) > 1:
            # Multiple collections: cross-collection search
            manager = _get_rag_manager()
            context = _run_async_in_thread(
                manager.search_across_collections(collection_names, query, top_n=top_n)
            )
            if not context:
                return "関連するドキュメントが見つかりませんでした。インデックスにドキュメントが追加されていることを確認してください。"
            # Format results
            lines = []
            for r in context:
                source = r.metadata.get("source_file", "unknown")
                col = r.metadata.get("_source_collection", "")
                lines.append(f"[{source}] (score: {r.score:.3f})\n{r.text}\n")
            result = f"**検索結果 (top {top_n}):**\n\n" + "\n---\n".join(lines)
        elif len(collection_names) == 1:
            # Single linked collection
            from ...rag.manager import get_rag_manager_for_collection
            manager = get_rag_manager_for_collection(collection_names[0])
            context = _run_async_in_thread(
                manager.get_context(query, top_n=top_n, include_sources=True)
            )
            if not context:
                return "関連するドキュメントが見つかりませんでした。インデックスにドキュメントが追加されていることを確認してください。"
            result = f"**検索結果 (top {top_n}):**\n\n{context}"
        else:
            # No linked collections: fallback to default/project manager
            manager = _get_rag_manager()
            context = _run_async_in_thread(
                manager.get_context(query, top_n=top_n, include_sources=True)
            )
            if not context:
                return "関連するドキュメントが見つかりませんでした。インデックスにドキュメントが追加されていることを確認してください。"
            result = f"**検索結果 (top {top_n}):**\n\n{context}"

        print(f"[Tool] search_rag 結果: {result[:200]}...")
        return result

    except Exception as e:
        logger.error(f"RAG search failed: {e}")
        return f"RAG検索でエラーが発生しました: {str(e)}"


@function_tool
def add_document_to_rag(file_path: str) -> str:
    """ドキュメントをRAGインデックスに追加する。
    
    指定されたファイルを読み込み、チャンクに分割して
    ベクトルデータベースにインデックスします。
    
    現在選択中のプロジェクトのインデックスに追加されます。
    プロジェクト未選択時はデフォルトのインデックスに追加されます。
    
    対応ファイル形式:
    - Markdown (.md)
    - テキスト (.txt)
    - PDF (.pdf)
    - その他LlamaIndexがサポートする形式
    
    Args:
        file_path: インデックスに追加するファイルのパス
        
    Returns:
        処理結果のメッセージ
    """
    project_id = _current_project_id
    print(f"[Tool] add_document_to_rag が呼び出されました: file_path={file_path}, project={project_id or 'default'}")
    
    try:
        manager = _get_rag_manager()
        
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                chunks_count = _run_async_in_thread(manager.index_file(file_path))
            else:
                chunks_count = loop.run_until_complete(manager.index_file(file_path))
        except RuntimeError:
            chunks_count = _run_async_in_thread(manager.index_file(file_path))
        
        if chunks_count > 0:
            result = f"ファイル '{file_path}' を {chunks_count} チャンクでインデックスしました。"
        else:
            result = f"ファイル '{file_path}' のインデックスに失敗しました。ファイルが存在し、対応形式であることを確認してください。"
        
        print(f"[Tool] add_document_to_rag 結果: {result}")
        return result
        
    except Exception as e:
        logger.error(f"Document indexing failed: {e}")
        return f"ドキュメントの追加でエラーが発生しました: {str(e)}"


@function_tool
def get_rag_status() -> str:
    """RAGシステムのステータスを取得する。
    
    Qdrantコレクションの情報やインデックス済みドキュメント数などを
    確認できます。
    
    現在選択中のプロジェクトのステータスが表示されます。
    
    Returns:
        RAGシステムのステータス情報
    """
    project_id = _current_project_id
    print(f"[Tool] get_rag_status が呼び出されました: project={project_id or 'default'}")
    
    try:
        manager = _get_rag_manager()
        
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                info = _run_async_in_thread(manager.get_collection_info())
            else:
                info = loop.run_until_complete(manager.get_collection_info())
        except RuntimeError:
            info = _run_async_in_thread(manager.get_collection_info())
        
        if info:
            result = (
                f"**RAGステータス:**\n"
                f"- コレクション: {info.get('name', 'N/A')}\n"
                f"- ポイント数: {info.get('points_count', 0)}\n"
                f"- ベクトル数: {info.get('vectors_count', 0)}\n"
                f"- ステータス: {info.get('status', 'N/A')}"
            )
        else:
            result = "RAGシステムが初期化されていないか、Qdrantに接続できません。"
        
        print(f"[Tool] get_rag_status 結果: {result}")
        return result
        
    except Exception as e:
        logger.error(f"RAG status check failed: {e}")
        return f"RAGステータスの取得でエラーが発生しました: {str(e)}"
