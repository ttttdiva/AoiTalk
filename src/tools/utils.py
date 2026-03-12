"""
ツール関連のユーティリティ関数
GeminiとOpenAI間でツールを共通化するための機能を提供
"""
from typing import Any, Callable, Dict, List, Optional, Tuple
from agents import FunctionTool
import inspect
import json


def extract_original_function(function_tool: FunctionTool) -> Callable:
    """FunctionToolオブジェクトから元の関数を抽出
    
    Args:
        function_tool: FunctionToolオブジェクト
        
    Returns:
        元の関数
        
    Raises:
        TypeError: FunctionToolオブジェクトでない場合
        ValueError: 元の関数を抽出できない場合
    """
    if not isinstance(function_tool, FunctionTool):
        raise TypeError(f"Expected FunctionTool, got {type(function_tool)}")
    
    # on_invoke_toolにアクセス
    on_invoke = function_tool.on_invoke_tool
    
    # _on_invoke_tool_implをクロージャから取得
    if hasattr(on_invoke, '__closure__') and on_invoke.__closure__ and len(on_invoke.__closure__) >= 1:
        _on_invoke_tool_impl = on_invoke.__closure__[0].cell_contents
        
        # 元の関数を_on_invoke_tool_implのクロージャから取得
        if hasattr(_on_invoke_tool_impl, '__closure__') and _on_invoke_tool_impl.__closure__ and len(_on_invoke_tool_impl.__closure__) >= 2:
            original_func = _on_invoke_tool_impl.__closure__[1].cell_contents
            return original_func
    
    raise ValueError("Could not extract original function from FunctionTool")


def get_tool_info(function_tool: FunctionTool) -> Dict[str, Any]:
    """FunctionToolから情報を抽出
    
    Args:
        function_tool: FunctionToolオブジェクト
        
    Returns:
        ツール情報の辞書
    """
    return {
        "name": function_tool.name,
        "description": function_tool.description,
        "parameters": function_tool.params_json_schema,
        "function": extract_original_function(function_tool)
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
        
        # anyOfフィールドの処理
        if key == 'anyOf' and isinstance(value, list):
            # anyOfから最初の非null型を選択
            for option in value:
                if isinstance(option, dict) and option.get('type') != 'null':
                    cleaned.update(option)
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


def create_gemini_function_declaration(function_tool: FunctionTool) -> Dict[str, Any]:
    """FunctionToolからGemini用のFunctionDeclaration定義を作成
    
    Args:
        function_tool: FunctionToolオブジェクト
        
    Returns:
        Gemini FunctionDeclaration用の辞書
    """
    # パラメータスキーマを調整
    params_schema = function_tool.params_json_schema.copy()
    
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
    if function_tool.name == "search_conversation_memory":
        # queryのみを必須にし、time_rangeとmax_resultsはオプショナルに
        params_schema["required"] = ["query"]
    
    return {
        "name": function_tool.name,
        "description": function_tool.description,
        "parameters": params_schema
    }


class ToolRegistry:
    """ツールを一元管理するレジストリ"""
    
    def __init__(self):
        self.tools: Dict[str, FunctionTool] = {}
        self.raw_functions: Dict[str, Callable] = {}
    
    def register(self, function_tool: FunctionTool):
        """ツールを登録
        
        Args:
            function_tool: 登録するFunctionTool
        """
        name = function_tool.name
        self.tools[name] = function_tool
        self.raw_functions[name] = extract_original_function(function_tool)
    
    def get_tool(self, name: str) -> Optional[FunctionTool]:
        """ツールを取得
        
        Args:
            name: ツール名
            
        Returns:
            FunctionToolオブジェクト、存在しない場合はNone
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
    
    def get_all_tools(self) -> List[FunctionTool]:
        """すべてのツールを取得
        
        Returns:
            FunctionToolのリスト
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


def register_tool(function_tool: FunctionTool):
    """グローバルレジストリにツールを登録"""
    _global_registry.register(function_tool)


def get_tool_registry() -> ToolRegistry:
    """グローバルレジストリを取得"""
    return _global_registry