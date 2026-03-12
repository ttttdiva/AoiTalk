"""
バックエンド非依存の統一ツールレジストリ

ToolDefinition を一元管理し、各バックエンド向けの変換メソッドを提供する。
"""
import logging
from typing import Any, Callable, Dict, List, Optional

from .core import ToolDefinition

logger = logging.getLogger(__name__)


class ToolRegistry:
    """バックエンド非依存のツールレジストリ"""

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}

    def register(self, tool_def: ToolDefinition):
        """ToolDefinition を登録する"""
        self._tools[tool_def.name] = tool_def
        logger.debug(f"[ToolRegistry] Registered: {tool_def.name}")

    def get(self, name: str) -> Optional[ToolDefinition]:
        """名前指定でツール取得"""
        return self._tools.get(name)

    def get_all(self) -> List[ToolDefinition]:
        """全ツールを取得"""
        return list(self._tools.values())

    def get_names(self) -> List[str]:
        """全ツール名を取得"""
        return list(self._tools.keys())

    def execute(self, name: str, **kwargs) -> Any:
        """名前指定でツール実行"""
        tool_def = self._tools.get(name)
        if not tool_def:
            raise ValueError(f"Tool not found: {name}")
        return tool_def.execute(**kwargs)

    async def execute_async(self, name: str, **kwargs) -> Any:
        """名前指定でツール非同期実行"""
        tool_def = self._tools.get(name)
        if not tool_def:
            raise ValueError(f"Tool not found: {name}")
        return await tool_def.execute_async(**kwargs)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


# グローバルレジストリインスタンス
_global_registry = ToolRegistry()


def register_tool(tool_def: ToolDefinition):
    """グローバルレジストリにツールを登録"""
    _global_registry.register(tool_def)


def get_registry() -> ToolRegistry:
    """グローバルレジストリを取得"""
    return _global_registry
