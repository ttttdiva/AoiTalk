"""
LLM Function Tools for Repository Maps

Provides function tools for generating repository structure maps.
"""

import logging
from typing import Any, Dict, List, Optional

from ..core import tool as function_tool

from .repo_map import get_repo_map_instance

logger = logging.getLogger(__name__)


@function_tool
def get_repo_map(
    path: str,
    max_tokens: int = 4096,
    exclude_files: Optional[List[str]] = None
) -> Dict[str, Any]:
    """リポジトリの構造マップを生成する
    
    コードベースの構造を効率的に把握するためのマップを返します。
    tree-sitterでコードを解析し、重要な定義（関数、クラス）と
    その参照関係を抽出してランキングします。
    
    Args:
        path: リポジトリのルートパス
        max_tokens: 出力の最大トークン数（デフォルト: 4096）
        exclude_files: 除外するファイルのリスト（チャットに含まれているファイル等）
    
    Returns:
        Dict[str, Any]: リポジトリマップと結果情報
    
    Examples:
        >>> get_repo_map(".")
        >>> get_repo_map("src", max_tokens=2048)
        >>> get_repo_map(".", exclude_files=["main.py", "config.py"])
    """
    print(f"[Tool] get_repo_map が呼び出されました: {path}")
    
    try:
        rm = get_repo_map_instance(path)
        rm.max_tokens = max_tokens
        
        repo_map = rm.get_repo_map(
            chat_files=exclude_files,
            other_files=None,
            force_refresh=False
        )
        
        if repo_map:
            return {
                "success": True,
                "repo_map": repo_map,
                "root": str(rm.root),
                "token_estimate": len(repo_map) // 4  # rough estimate
            }
        else:
            return {
                "success": True,
                "repo_map": "(empty repository or no source files found)",
                "root": str(rm.root),
                "token_estimate": 0
            }
            
    except Exception as e:
        logger.error(f"Error generating repo map: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"リポジトリマップの生成に失敗しました: {str(e)}"
        }
