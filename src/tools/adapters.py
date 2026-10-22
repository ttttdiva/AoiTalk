"""Tool adapters for the supported LLM backends."""

from __future__ import annotations

import ast
import json
import logging
import re
from typing import Any, Dict, List

from .core import ToolDefinition
from .registry import ToolRegistry

logger = logging.getLogger(__name__)


_GEMINI_SCHEMA_FIELDS = frozenset(
    {
        # These are the fields represented by the deprecated
        # google.generativeai Schema proto.  Keep this an explicit allowlist:
        # forwarding arbitrary JSON Schema keywords makes FunctionDeclaration
        # fail while constructing the provider message.
        "type",
        "format",
        "description",
        "nullable",
        "enum",
        "items",
        "properties",
        "required",
    }
)


def _clean_schema_for_gemini(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Project JSON Schema into the provider's supported schema subset.

    ``ToolDefinition.to_json_schema`` remains the canonical server-side
    validation schema.  Gemini receives a separate projection because the
    ``google.generativeai`` proto only accepts a small subset of JSON Schema.
    In particular, unknown keywords must be removed rather than passed through
    to ``FunctionDeclaration`` where they raise a ``ValueError``.
    """
    if not isinstance(schema, dict):
        return schema

    # Optional/union annotations are emitted as ``anyOf``.  Gemini has no
    # union schema, so retain the first non-null branch while continuing to
    # process the surrounding schema fields.  ``oneOf`` gets the same safe,
    # deterministic projection for schemas supplied by external tools.
    for union_key in ("anyOf", "oneOf"):
        options = schema.get(union_key)
        if not isinstance(options, list):
            continue
        selected = next(
            (
                option
                for option in options
                if isinstance(option, dict) and option.get("type") != "null"
            ),
            None,
        )
        if selected is None:
            schema = {
                key: value
                for key, value in schema.items()
                if key not in {"anyOf", "oneOf"}
            }
        else:
            # Branch fields should win over a duplicate outer field (the
            # historical cleaner used the same precedence), while the outer
            # description/properties remain available when the branch omits
            # them.
            merged = {
                key: value
                for key, value in schema.items()
                if key not in {"anyOf", "oneOf"}
            }
            merged.update(selected)
            schema = merged
        break

    cleaned: Dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _GEMINI_SCHEMA_FIELDS:
            # This also removes JSON Schema metadata/constraints such as
            # title, default, additionalProperties, minimum/maximum,
            # minItems/maxItems, pattern and minLength/maxLength.  They stay
            # authoritative in the canonical server schema.
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {
                prop_name: _clean_schema_for_gemini(prop_schema)
                for prop_name, prop_schema in value.items()
                if isinstance(prop_name, str) and isinstance(prop_schema, dict)
            }
            continue
        if key == "items":
            if isinstance(value, dict):
                cleaned[key] = _clean_schema_for_gemini(value)
            continue
        if key == "enum":
            # The provider proto models enum as repeated strings.  Preserve
            # valid JSON Schema string enums and omit incompatible values so a
            # malformed external schema cannot break the whole tool roster.
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                cleaned[key] = list(value)
            continue
        if key == "required":
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                cleaned[key] = list(value)
            continue
        if key in {"type", "format", "description"}:
            if isinstance(value, str):
                cleaned[key] = value
            continue
        if key == "nullable":
            if isinstance(value, bool):
                cleaned[key] = value
            continue
        # All remaining fields are covered by the allowlist above, but do not
        # forward an unexpected value shape if the SDK adds one later.
        if isinstance(value, (str, bool)):
            cleaned[key] = value

    return cleaned


class GeminiAdapter:
    """Convert ToolDefinition objects into Gemini declarations."""

    @staticmethod
    def convert(tool_def: ToolDefinition) -> Dict[str, Any]:
        schema = _clean_schema_for_gemini(tool_def.to_json_schema())
        schema.setdefault("properties", {})
        schema.setdefault("required", [])

        if "properties" in schema:
            schema["properties"] = {
                prop_name: _clean_schema_for_gemini(prop_schema)
                for prop_name, prop_schema in schema["properties"].items()
            }

        return {
            "name": tool_def.name,
            "description": tool_def.description,
            "parameters": schema,
        }

    @staticmethod
    def convert_all(tools: List[ToolDefinition]) -> List[Dict[str, Any]]:
        return [GeminiAdapter.convert(t) for t in tools]


class OpenAIAPIAdapter:
    """Convert ToolDefinition objects into Chat Completions tool specs."""

    @staticmethod
    def convert(tool_def: ToolDefinition) -> Dict[str, Any]:
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
        return [OpenAIAPIAdapter.convert(t) for t in tools]


class CLIAdapter:
    """Prompt formatting and tool-call parsing for CLI backends."""

    @staticmethod
    def to_prompt_text(tools: List[ToolDefinition]) -> str:
        if not tools:
            return ""
        lines = ["利用可能なツール:"]
        for tool_def in tools:
            params_desc = ", ".join(
                f"{param.name}: {param.type}"
                + (
                    f" (任意, 既定値: {param.default})"
                    if not param.required
                    else ""
                )
                for param in tool_def.parameters
            )
            lines.append(f"  - {tool_def.name}({params_desc}): {tool_def.description}")
        lines.append("")
        lines.append(
            "ツールが必要な場合は "
            "[TOOL_CALL: tool_name(key=value, key2=value2)] 形式で出力してください。"
        )
        return "\n".join(lines)

    @staticmethod
    def parse_tool_calls(cli_output: str) -> List[Dict[str, Any]]:
        tool_calls: List[Dict[str, Any]] = []
        pattern = r"\[TOOL_CALL:\s*(\w+)\((.*?)\)\]"
        for match in re.finditer(pattern, cli_output, re.IGNORECASE | re.DOTALL):
            tool_name = match.group(1)
            args = CLIAdapter._parse_tool_args(match.group(2))
            tool_calls.append({"name": tool_name, "args": args})
            logger.debug("[CLIAdapter] Parsed tool call: %s %s", tool_name, args)
        return tool_calls

    @staticmethod
    def _parse_tool_args(args_str: str) -> Dict[str, Any]:
        args: Dict[str, Any] = {}
        if not args_str or not args_str.strip():
            return args

        try:
            parsed = ast.parse(f"_tool({args_str})", mode="eval")
            call = parsed.body
            if isinstance(call, ast.Call):
                for keyword in call.keywords:
                    if keyword.arg is None:
                        continue
                    args[keyword.arg] = CLIAdapter._ast_value_to_python(keyword.value)
                return args
        except Exception:
            logger.debug("[CLIAdapter] Falling back to legacy arg parser", exc_info=True)

        for pair in args_str.split(","):
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            args[key.strip()] = value.strip().strip("\"'")
        return args

    @staticmethod
    def _ast_value_to_python(node: ast.AST) -> Any:
        if isinstance(node, ast.Name):
            lowered = node.id.lower()
            if lowered == "true":
                return True
            if lowered == "false":
                return False
            if lowered in {"none", "null"}:
                return None
            return node.id
        return ast.literal_eval(node)

    @staticmethod
    def execute_tool_calls(
        tool_calls: List[Dict[str, Any]],
        registry: ToolRegistry,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for tool_call in tool_calls:
            name = tool_call["name"]
            args = tool_call.get("args", {})
            try:
                result = registry.execute(name, **args)
                results.append({"name": name, "success": True, "result": str(result)})
            except Exception as e:
                results.append({"name": name, "success": False, "error": str(e)})
                logger.error("[CLIAdapter] Tool execution failed: %s - %s", name, e)
        return results

    @staticmethod
    def format_tool_results(results: List[Dict[str, Any]]) -> str:
        lines = ["Tool results:"]
        for result in results:
            if result["success"]:
                lines.append(f"  [{result['name']}] {result['result']}")
            else:
                lines.append(f"  [{result['name']}] Error: {result['error']}")
        return "\n".join(lines)
