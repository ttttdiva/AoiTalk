"""
バックエンドアダプター

ToolDefinition を各 LLM バックエンド固有のフォーマットに変換する。
"""
import json
import re
import logging
from typing import Any, Dict, List, Optional

from .core import ToolDefinition
from .registry import ToolRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gemini 用スキーマクリーニング（既存 utils.py ロジックを移植）
# ---------------------------------------------------------------------------

def _clean_schema_for_gemini(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Gemini API 用にスキーマをクリーンアップする再帰関数"""
    if not isinstance(schema, dict):
        return schema

    cleaned: Dict[str, Any] = {}
    for key, value in schema.items():
        # Gemini でサポートされていないフィールドをスキップ
        if key in ("title", "$schema", "additionalProperties", "default"):
            continue
        # anyOf フィールドの処理
        if key == "anyOf" and isinstance(value, list):
            for option in value:
                if isinstance(option, dict) and option.get("type") != "null":
                    cleaned.update(option)
                    break
            continue
        # 再帰的にクリーンアップ
        if isinstance(value, dict):
            cleaned[key] = _clean_schema_for_gemini(value)
        elif isinstance(value, list):
            cleaned[key] = [
                _clean_schema_for_gemini(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            cleaned[key] = value

    return cleaned


# ---------------------------------------------------------------------------
# OpenAI Agents SDK Adapter
# ---------------------------------------------------------------------------

class OpenAIAgentAdapter:
    """ToolDefinition → OpenAI Agents SDK FunctionTool"""

    @staticmethod
    def convert(tool_def: ToolDefinition):
        """ToolDefinition を FunctionTool に変換する"""
        from agents import function_tool
        # ラッパー関数を作成して @function_tool を適用
        fn = tool_def.function
        # function_tool デコレータを適用
        return function_tool(fn)

    @staticmethod
    def convert_all(tools: List[ToolDefinition]) -> list:
        """複数の ToolDefinition を FunctionTool リストに変換する"""
        return [OpenAIAgentAdapter.convert(t) for t in tools]


# ---------------------------------------------------------------------------
# Gemini API Adapter
# ---------------------------------------------------------------------------

class GeminiAdapter:
    """ToolDefinition → Gemini FunctionDeclaration 辞書"""

    @staticmethod
    def convert(tool_def: ToolDefinition) -> Dict[str, Any]:
        """ToolDefinition を Gemini FunctionDeclaration 用辞書に変換する"""
        schema = tool_def.to_json_schema()
        schema = _clean_schema_for_gemini(schema)

        # Gemini に必要な形式保証
        if "properties" not in schema:
            schema["properties"] = {}
        if "required" not in schema:
            schema["required"] = []

        # プロパティも再帰的にクリーンアップ
        if "properties" in schema:
            cleaned_properties = {}
            for prop_name, prop_schema in schema["properties"].items():
                cleaned_properties[prop_name] = _clean_schema_for_gemini(prop_schema)
            schema["properties"] = cleaned_properties

        return {
            "name": tool_def.name,
            "description": tool_def.description,
            "parameters": schema,
        }

    @staticmethod
    def convert_all(tools: List[ToolDefinition]) -> List[Dict[str, Any]]:
        """複数の ToolDefinition を FunctionDeclaration リストに変換する"""
        return [GeminiAdapter.convert(t) for t in tools]


# ---------------------------------------------------------------------------
# OpenAI Chat Completions API Adapter（SGLang 等の OpenAI 互換 API 用）
# ---------------------------------------------------------------------------

class OpenAIAPIAdapter:
    """ToolDefinition → OpenAI Chat Completions API tools format"""

    @staticmethod
    def convert(tool_def: ToolDefinition) -> Dict[str, Any]:
        """ToolDefinition を OpenAI API tools 形式に変換する"""
        return {
            "type": "function",
            "function": {
                "name": tool_def.name,
                "description": tool_def.description,
                "parameters": tool_def.to_json_schema(),
            },
        }

    @staticmethod
    def convert_all(tools: List[ToolDefinition]) -> List[Dict[str, Any]]:
        """複数の ToolDefinition を OpenAI API tools リストに変換する"""
        return [OpenAIAPIAdapter.convert(t) for t in tools]


# ---------------------------------------------------------------------------
# CLI Adapter（テキストベースのツール呼び出し用）
# ---------------------------------------------------------------------------

class CLIAdapter:
    """ToolDefinition → CLI 向けテキスト説明 + パース・実行"""

    @staticmethod
    def to_prompt_text(tools: List[ToolDefinition]) -> str:
        """ツール一覧をプロンプト埋め込み用テキストに変換する"""
        lines = ["利用可能なツール:"]
        for t in tools:
            params_desc = ", ".join(
                f"{p.name}: {p.type}"
                + (f" (optional, default: {p.default})" if not p.required else "")
                for p in t.parameters
            )
            lines.append(f"  - {t.name}({params_desc}): {t.description}")
        lines.append("")
        lines.append(
            "ツールを使う場合は [TOOL_CALL: tool_name(key=value, key2=value2)] "
            "の形式で出力してください。"
        )
        return "\n".join(lines)

    @staticmethod
    def parse_tool_calls(cli_output: str) -> List[Dict[str, Any]]:
        """CLI の出力からツール呼び出しをパースする"""
        tool_calls: List[Dict[str, Any]] = []
        pattern = r"\[TOOL_CALL:\s*(\w+)\((.*?)\)\]"
        for match in re.finditer(pattern, cli_output, re.IGNORECASE):
            tool_name = match.group(1)
            args_str = match.group(2)

            args: Dict[str, Any] = {}
            if args_str.strip():
                for pair in args_str.split(","):
                    if "=" in pair:
                        key, value = pair.split("=", 1)
                        key = key.strip()
                        value = value.strip().strip("\"'")
                        args[key] = value

            tool_calls.append({"name": tool_name, "args": args})
            logger.debug(f"[CLIAdapter] Parsed tool call: {tool_name} with args {args}")

        return tool_calls

    @staticmethod
    def execute_tool_calls(
        tool_calls: List[Dict[str, Any]], registry: ToolRegistry
    ) -> List[Dict[str, Any]]:
        """パースしたツール呼び出しを実行する"""
        results: List[Dict[str, Any]] = []
        for tc in tool_calls:
            name = tc["name"]
            args = tc.get("args", {})
            try:
                result = registry.execute(name, **args)
                results.append({"name": name, "success": True, "result": str(result)})
            except Exception as e:
                results.append({"name": name, "success": False, "error": str(e)})
                logger.error(f"[CLIAdapter] Tool execution failed: {name} - {e}")
        return results

    @staticmethod
    def format_tool_results(results: List[Dict[str, Any]]) -> str:
        """ツール実行結果をフォローアッププロンプト用テキストに整形する"""
        lines = ["ツール実行結果:"]
        for r in results:
            if r["success"]:
                lines.append(f"  [{r['name']}] {r['result']}")
            else:
                lines.append(f"  [{r['name']}] エラー: {r['error']}")
        return "\n".join(lines)
