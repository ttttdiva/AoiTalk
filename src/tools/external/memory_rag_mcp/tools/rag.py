"""RAGドキュメント検索・管理ツール"""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


def register(mcp: FastMCP):
    """RAGツールを MCP サーバーに登録する。"""

    def _get_rag_manager(project_id: Optional[str] = None):
        from src.rag import get_rag_manager
        return get_rag_manager(project_id=project_id)

    def _get_project_collection_names(project_id: Optional[str]) -> list:
        if not project_id:
            return []
        try:
            from src.memory.database import get_database_manager
            from src.memory.rag_collection_repository import RagCollectionRepository
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

    @mcp.tool()
    async def search_rag(query: str, top_n: int = 5, project_id: Optional[str] = None) -> str:
        """RAGベースの文書検索を実行する。

        インデックス済みのドキュメント（PDF、テキスト、Markdownなど）から
        クエリに関連する情報を検索します。

        Args:
            query: 検索クエリ（具体的なキーワード推奨）
            top_n: 返す結果の最大数（デフォルト: 5）
            project_id: プロジェクトID（省略時はデフォルトコレクション）
        """
        top_n = int(top_n)

        try:
            collection_names = _get_project_collection_names(project_id)

            if len(collection_names) > 1:
                manager = _get_rag_manager()
                context = await manager.search_across_collections(collection_names, query, top_n=top_n)
                if not context:
                    return "関連するドキュメントが見つかりませんでした。"
                lines = []
                for r in context:
                    source = r.metadata.get("source_file", "unknown")
                    lines.append(f"[{source}] (score: {r.score:.3f})\n{r.text}\n")
                return f"**検索結果 (top {top_n}):**\n\n" + "\n---\n".join(lines)

            elif len(collection_names) == 1:
                from src.rag.manager import get_rag_manager_for_collection
                manager = get_rag_manager_for_collection(collection_names[0])
                context = await manager.get_context(query, top_n=top_n, include_sources=True)
                if not context:
                    return "関連するドキュメントが見つかりませんでした。"
                return f"**検索結果 (top {top_n}):**\n\n{context}"

            else:
                manager = _get_rag_manager(project_id)
                context = await manager.get_context(query, top_n=top_n, include_sources=True)
                if not context:
                    return "関連するドキュメントが見つかりませんでした。"
                return f"**検索結果 (top {top_n}):**\n\n{context}"

        except Exception as e:
            logger.error(f"RAG search failed: {e}")
            return f"RAG検索でエラーが発生しました: {str(e)}"

    @mcp.tool()
    async def add_document_to_rag(file_path: str, project_id: Optional[str] = None) -> str:
        """ドキュメントをRAGインデックスに追加する。

        対応ファイル形式: Markdown (.md), テキスト (.txt), PDF (.pdf) 等

        Args:
            file_path: インデックスに追加するファイルのパス
            project_id: プロジェクトID（省略時はデフォルトコレクション）
        """
        try:
            manager = _get_rag_manager(project_id)
            chunks_count = await manager.index_file(file_path)

            if chunks_count > 0:
                return f"ファイル '{file_path}' を {chunks_count} チャンクでインデックスしました。"
            else:
                return f"ファイル '{file_path}' のインデックスに失敗しました。"

        except Exception as e:
            logger.error(f"Document indexing failed: {e}")
            return f"ドキュメントの追加でエラーが発生しました: {str(e)}"

    @mcp.tool()
    async def get_rag_status(project_id: Optional[str] = None) -> str:
        """RAGシステムのステータスを取得する。

        Args:
            project_id: プロジェクトID（省略時はデフォルトコレクション）
        """
        try:
            manager = _get_rag_manager(project_id)
            info = await manager.get_collection_info()

            if info:
                return (
                    f"**RAGステータス:**\n"
                    f"- コレクション: {info.get('name', 'N/A')}\n"
                    f"- ポイント数: {info.get('points_count', 0)}\n"
                    f"- ベクトル数: {info.get('vectors_count', 0)}\n"
                    f"- ステータス: {info.get('status', 'N/A')}"
                )
            else:
                return "RAGシステムが初期化されていないか、Qdrantに接続できません。"

        except Exception as e:
            logger.error(f"RAG status check failed: {e}")
            return f"RAGステータスの取得でエラーが発生しました: {str(e)}"
