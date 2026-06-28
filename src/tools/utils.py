"""
ツール関連のユーティリティ関数
GeminiとOpenAI間でツールを共通化するための機能を提供
"""
from src.tools.core import ToolDefinition, ensure_tool_definition
from typing import Any, Callable, Dict, List, Optional, Union
import inspect
import json


def extract_original_function(tool: Union[Callable, ToolDefinition]) -> Callable:
    """ToolDefinition または callable から元の関数を抽出

    Args:
        tool: ToolDefinition または callable

    Returns:
        元の関数

    Raises:
        TypeError: 対応していないオブジェクト型の場合
        ValueError: 元の関数を抽出できない場合
    """
    if isinstance(tool, ToolDefinition):
        return tool.function

    if callable(tool):
        return tool

    raise TypeError(f"Expected ToolDefinition or callable, got {type(tool)}")


def get_tool_info(tool: Union[Callable, ToolDefinition]) -> Dict[str, Any]:
    """ToolDefinition または callable から情報を抽出

    Args:
        tool: ToolDefinition または callable

    Returns:
        ツール情報の辞書
    """
    tool_def = ensure_tool_definition(tool)
    return {
        "name": tool_def.name,
        "description": tool_def.description,
        "parameters": tool_def.to_json_schema(),
        "function": tool_def.function
    }


def _clean_schema_for_gemini(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Gemini用にスキーマをクリーンアップする再帰関数

    Args:
        schema: クリーンアップするスキーマ

    Returns:
        クリーンアップされたスキーマ
    """
    if not isinstance(schema, dict):
        return schema

    cleaned = {}

    for key, value in schema.items():
        # Geminiでサポートされていないフィールドをスキップ
        if key in ['title', '$schema', 'additionalProperties', 'default']:
            continue
        if key == 'properties' and isinstance(value, dict):
            cleaned[key] = {
                prop_name: _clean_schema_for_gemini(prop_schema)
                for prop_name, prop_schema in value.items()
            }
            continue

        # anyOfフィールドの処理
        if key == 'anyOf' and isinstance(value, list):
            # anyOfから最初の非null型を選択
            for option in value:
                if isinstance(option, dict) and option.get('type') != 'null':
                    cleaned.update(_clean_schema_for_gemini(option))
                    break
            continue

        # 再帰的にクリーンアップ
        if isinstance(value, dict):
            cleaned[key] = _clean_schema_for_gemini(value)
        elif isinstance(value, list):
            cleaned[key] = [_clean_schema_for_gemini(item) if isinstance(item, dict) else item for item in value]
        else:
            cleaned[key] = value

    return cleaned


def create_gemini_function_declaration(tool: Union[Callable, ToolDefinition]) -> Dict[str, Any]:
    """ToolDefinitionからGemini用のFunctionDeclaration定義を作成

    Args:
        tool: ToolDefinition または callable

    Returns:
        Gemini FunctionDeclaration用の辞書
    """
    tool_def = ensure_tool_definition(tool)
    params_schema = tool_def.to_json_schema().copy()

    # Gemini用にスキーマをクリーンアップ
    params_schema = _clean_schema_for_gemini(params_schema)

    # Geminiに必要な形式に変換
    if "properties" not in params_schema:
        params_schema["properties"] = {}

    # requiredフィールドがない場合は空リストを設定
    if "required" not in params_schema:
        params_schema["required"] = []

    # プロパティも再帰的にクリーンアップ
    if "properties" in params_schema:
        cleaned_properties = {}
        for prop_name, prop_schema in params_schema["properties"].items():
            cleaned_properties[prop_name] = _clean_schema_for_gemini(prop_schema)
        params_schema["properties"] = cleaned_properties

    # 特定のツールのrequiredフィールドを調整
    if tool_def.name == "search_conversation_memory":
        # queryのみを必須にし、time_rangeとmax_resultsはオプショナルに
        params_schema["required"] = ["query"]

    return {
        "name": tool_def.name,
        "description": tool_def.description,
        "parameters": params_schema
    }


class ToolRegistry:
    """ツールを一元管理するレジストリ"""

    def __init__(self):
        self.tools: Dict[str, ToolDefinition] = {}
        self.raw_functions: Dict[str, Callable] = {}

    def register(self, tool: Union[Callable, ToolDefinition]):
        """ツールを登録

        Args:
            tool: 登録するツール
        """
        tool_def = ensure_tool_definition(tool)
        name = tool_def.name
        self.tools[name] = tool_def
        self.raw_functions[name] = tool_def.function

    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        """ツールを取得

        Args:
            name: ツール名

        Returns:
            ToolDefinitionオブジェクト、存在しない場合はNone
        """
        return self.tools.get(name)

    def get_function(self, name: str) -> Optional[Callable]:
        """生の関数を取得

        Args:
            name: ツール名

        Returns:
            関数、存在しない場合はNone
        """
        return self.raw_functions.get(name)

    def get_all_tools(self) -> List[ToolDefinition]:
        """すべてのツールを取得

        Returns:
            ToolDefinitionのリスト
        """
        return list(self.tools.values())

    def get_all_functions(self) -> Dict[str, Callable]:
        """すべての生の関数を取得

        Returns:
            関数名と関数のマッピング
        """
        return self.raw_functions.copy()

    def get_gemini_declarations(self) -> List[Dict[str, Any]]:
        """Gemini用のFunctionDeclarationリストを取得

        Returns:
            FunctionDeclarationの辞書のリスト
        """
        return [create_gemini_function_declaration(tool) for tool in self.tools.values()]


# グローバルレジストリインスタンス
_global_registry = ToolRegistry()


def register_tool(tool: Union[Callable, ToolDefinition]):
    """グローバルレジストリにツールを登録"""
    _global_registry.register(tool)


def get_tool_registry() -> ToolRegistry:
    """グローバルレジストリを取得"""
    return _global_registry


def init_global_tools_registry() -> ToolRegistry:
    """旧ツール初期化APIとの互換用エントリポイント。"""
    return _global_registry
