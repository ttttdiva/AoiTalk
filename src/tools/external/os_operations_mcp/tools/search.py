"""ディレクトリ一覧・ファイル検索ツール"""

from __future__ import annotations

import json
import logging
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

from ..config_loader import resolve_path_for_user

logger = logging.getLogger(__name__)


def register(mcp: FastMCP):
    """ディレクトリ・検索ツールを MCP サーバーに登録する。"""

    from src.tools.os_operations.file_system import get_file_system, FileSystemError

    @mcp.tool()
    async def list_directory(path: str, max_depth: int = 2, pattern: Optional[str] = None) -> str:
        """ディレクトリの内容を一覧表示する

        Args:
            path: 一覧表示するディレクトリのパス
            max_depth: 再帰的に表示する深さ（デフォルト: 2）
            pattern: フィルタするパターン（例: "*.py"）
        """
        path = resolve_path_for_user(path)
        try:
            fs = get_file_system()
            result = fs.list_directory(path, max_depth=max_depth, pattern=pattern)
            return json.dumps({"success": True, "content": result}, ensure_ascii=False)
        except FileSystemError as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": f"予期しないエラー: {str(e)}"}, ensure_ascii=False)

    @mcp.tool()
    async def search_files(
        query: str,
        path: str,
        extensions: Optional[str] = None,
        search_content: bool = False
    ) -> str:
        """ファイルを検索する

        Args:
            query: 検索クエリ（ファイル名検索時はglob、内容検索時は正規表現）
            path: 検索対象のディレクトリパス
            extensions: フィルタする拡張子（カンマ区切り、例: ".py,.js"）
            search_content: Trueの場合、ファイル内容を検索
        """
        path = resolve_path_for_user(path)

        # extensionsをリストに変換
        ext_list = None
        if extensions:
            ext_list = [e.strip() for e in extensions.split(',') if e.strip()]

        try:
            fs = get_file_system()
            result = fs.search_files(
                query, path,
                extensions=ext_list,
                search_content=search_content
            )
            return json.dumps({"success": True, "content": result}, ensure_ascii=False)
        except FileSystemError as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": f"予期しないエラー: {str(e)}"}, ensure_ascii=False)
