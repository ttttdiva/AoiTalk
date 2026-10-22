"""
バックエンド非依存の統一ツールレジストリ

ToolDefinition を一元管理し、各バックエンド向けの変換メソッドを提供する。
"""
import logging
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional

from .core import ToolDefinition

logger = logging.getLogger(__name__)


class ToolRegistry:
    """バックエンド非依存のツールレジストリ"""

    def __init__(self, *, capability_check=None, refresh=None):
        self._tools: Dict[str, ToolDefinition] = {}
        self._capability_check = capability_check
        self._refresh_callback = refresh
        self._refreshing = False

    def configure_runtime(self, *, capability_check, refresh=None):
        """Install server-owned hooks; standalone registries keep their behavior."""
        self._capability_check = capability_check
        self._refresh_callback = refresh
        for definition in list(self._tools.values()):
            self.register(definition)

    def _refresh(self):
        if self._refresh_callback is not None and not self._refreshing:
            self._refreshing = True
            try:
                self._refresh_callback(self)
            finally:
                self._refreshing = False

    def _available(self, definition):
        return self._capability_check is None or self._capability_check(definition) is None

    def register(self, tool_def: ToolDefinition):
        """ToolDefinition を登録する"""
        if self._capability_check is not None:
            original_check = tool_def.execution_check

            def check():
                reason = self._capability_check(tool_def)
                return reason or (original_check() if original_check else None)

            tool_def = replace(tool_def, execution_check=check)
        self._tools[tool_def.name] = tool_def
        logger.debug(f"[ToolRegistry] Registered: {tool_def.name}")

    def get(self, name: str) -> Optional[ToolDefinition]:
        """名前指定でツール取得"""
        self._refresh()
        definition = self._tools.get(name)
        return definition if definition is not None and self._available(definition) else None

    def get_all(self) -> List[ToolDefinition]:
        """全ツールを取得"""
        self._refresh()
        return [definition for definition in self._tools.values() if self._available(definition)]

    def get_names(self) -> List[str]:
        """全ツール名を取得"""
        return [definition.name for definition in self.get_all()]

    def execute(self, tool_name: str, **kwargs) -> Any:
        """名前指定でツール実行"""
        tool_def = self.get(tool_name)
        if not tool_def:
            raise ValueError(f"Tool not found: {tool_name}")
        return tool_def.execute(**kwargs)

    async def execute_async(self, tool_name: str, **kwargs) -> Any:
        """名前指定でツール非同期実行"""
        tool_def = self.get(tool_name)
        if not tool_def:
            raise ValueError(f"Tool not found: {tool_name}")
        return await tool_def.execute_async(**kwargs)

    def __len__(self) -> int:
        return len(self.get_all())

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None


# グローバルレジストリインスタンス
_global_registry = ToolRegistry()


def register_tool(tool_def: ToolDefinition):
    """グローバルレジストリにツールを登録"""
    _global_registry.register(tool_def)


def get_registry() -> ToolRegistry:
    """グローバルレジストリを取得"""
    return _global_registry


def init_global_tools_registry(config: Any | None = None) -> ToolRegistry:
    """旧ツール初期化APIとの互換用エントリポイント。

    LLM 実行時のツール一覧は `build_runtime_tool_registry()` が構築する。
    ここでは古い呼び出し元が NameError/ImportError にならないよう、
    `src.tools` import 時に初期登録済みのグローバルレジストリを返す。
    """
    return _global_registry
