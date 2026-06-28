"""JSON tool loop for models without reliable native tool calling."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..tools.core import ToolDefinition
from ..tools.registry import ToolRegistry
from .unified_turn_runtime import RegistryToolRouter, UnifiedToolCall


@dataclass(frozen=True)
class JsonToolCallRecord:
    tool: str
    arguments: dict[str, Any]
    result: str

    @property
    def successful(self) -> bool:
        lowered = self.result.strip().lower()
        return not (
            lowered.startswith("tool not found:")
            or lowered.startswith("tool execution error:")
        )


@dataclass(frozen=True)
class JsonToolLoopResult:
    final_output: str
    tool_calls: list[JsonToolCallRecord] = field(default_factory=list)

    @property
    def tool_call_count(self) -> int:
        return len(self.tool_calls)

    def has_successful_tool(self, tool_names: set[str]) -> bool:
        expected = {name.strip() for name in tool_names if name.strip()}
        return any(
            call.tool in expected and call.successful
            for call in self.tool_calls
        )


def build_json_tool_loop_system_prompt(base_instructions: str, registry: ToolRegistry) -> str:
    """Append a strict JSON tool protocol to a normal system prompt."""
    tool_specs = [_tool_spec(tool) for tool in registry.get_all()]
    return "\n\n".join(
        part
        for part in [
            base_instructions.strip(),
            "ツール実行プロトコル:",
            "利用可能なツールは、MarkdownなしのJSONオブジェクト1個だけを返して使ってください。",
            'ツールを呼ぶ場合: {"type":"tool_call","tool":"tool_name","arguments":{...}}',
            '最終回答する場合: {"type":"final","content":"..."}',
            "ツール結果が返された後は、その結果を根拠に続行するか最終回答してください。",
            "ツール結果を捏造しないでください。必要なツールがない場合は、その旨を最終回答してください。",
            "利用可能なツール:",
            json.dumps(tool_specs, ensure_ascii=False, indent=2),
        ]
        if part
    )


def run_json_tool_loop(
    *,
    create_completion: Callable[[list[dict[str, Any]]], str],
    initial_messages: list[dict[str, Any]],
    registry: ToolRegistry,
    max_rounds: int = 5,
    original_request: str | None = None,
    required_tool_names: set[str] | None = None,
    required_tool_reason: str | None = None,
    require_all_required_tools: bool = False,
    return_result: bool = False,
) -> str | JsonToolLoopResult:
    """Run a text JSON tool loop and return the final user-facing response."""
    messages = list(initial_messages)
    last_content = ""
    tool_calls: list[JsonToolCallRecord] = []
    required_tools = {
        name.strip()
        for name in (required_tool_names or set())
        if name and name.strip()
    }

    def _finish(output: str) -> str | JsonToolLoopResult:
        if return_result:
            return JsonToolLoopResult(final_output=output, tool_calls=list(tool_calls))
        return output

    def _has_required_tool() -> bool:
        if not required_tools:
            return True
        if require_all_required_tools:
            successful = {
                call.tool
                for call in tool_calls
                if call.tool in required_tools and call.successful
            }
            return required_tools.issubset(successful)
        return any(
            call.tool in required_tools and call.successful
            for call in tool_calls
        )

    for _ in range(max_rounds):
        content = str(create_completion(messages) or "")
        last_content = content
        action = parse_json_tool_action(content)

        if not action:
            if required_tools and not _has_required_tool() and len(registry) > 0:
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": _build_required_tool_prompt(
                            original_request,
                            required_tools,
                            required_tool_reason,
                            require_all_required_tools,
                        ),
                    }
                )
                continue
            if original_request and len(registry) > 0 and _should_repair_response(content):
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {"role": "user", "content": _build_repair_prompt(original_request)}
                )
                continue
            return _finish(content)

        action_type = str(action.get("type") or "").strip().lower()
        if action_type == "final":
            final_content = str(action.get("content") or "")
            if (
                len(tool_calls) == 0
                and original_request
                and len(registry) > 0
                and _should_repair_response(final_content)
            ):
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {"role": "user", "content": _build_repair_prompt(original_request)}
                )
                continue
            if required_tools and not _has_required_tool() and len(registry) > 0:
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": _build_required_tool_prompt(
                            original_request,
                            required_tools,
                            required_tool_reason,
                            require_all_required_tools,
                        ),
                    }
                )
                continue
            return _finish(final_content)

        if action_type != "tool_call":
            return _finish(content)

        tool_name = str(action.get("tool") or "").strip()
        arguments = action.get("arguments") or action.get("parameters") or action.get("args") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except Exception:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}

        tool_result = execute_json_tool_call(
            registry,
            tool_name,
            arguments,
            fallback_request=original_request,
        )
        tool_calls.append(
            JsonToolCallRecord(
                tool=tool_name,
                arguments=dict(arguments),
                result=str(tool_result),
            )
        )
        messages.append({"role": "assistant", "content": content})
        messages.append(
            {
                "role": "user",
                "content": "\n".join(
                    [
                        f"Tool result for `{tool_name}`:",
                        str(tool_result),
                        "",
                        "Return the next JSON object.",
                    ]
                ),
            }
        )

    return _finish(last_content)


def parse_json_tool_action(content: str) -> Optional[dict[str, Any]]:
    text = (content or "").strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    if not text.startswith("{"):
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        text = match.group(0)
    try:
        payload = json.loads(text)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def execute_json_tool_call(
    registry: ToolRegistry,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    fallback_request: str | None = None,
) -> str:
    tool = registry.get(tool_name)
    if tool is None:
        return f"Tool not found: {tool_name}"

    normalized_args = _normalize_arguments(
        tool,
        arguments,
        fallback_request=fallback_request,
    )
    result = RegistryToolRouter(
        registry,
        log_prefix="JsonToolLoop",
    ).execute(
        UnifiedToolCall(
            tool=tool_name,
            arguments=normalized_args,
        )
    )
    if result.success:
        return result.output
    return f"Tool execution error: {result.error or result.output}"


def _tool_spec(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.to_json_schema(),
    }


def _normalize_arguments(
    tool: ToolDefinition,
    arguments: dict[str, Any],
    *,
    fallback_request: str | None = None,
) -> dict[str, Any]:
    if arguments:
        normalized = dict(arguments)
        if "request" in normalized and _is_empty_or_garbled_request(normalized["request"]):
            required = [param for param in tool.parameters if param.required]
            if len(required) == 1 and required[0].name == "request" and fallback_request:
                normalized["request"] = fallback_request
        return normalized
    required = [param for param in tool.parameters if param.required]
    if len(required) == 1 and required[0].name == "request":
        return {"request": fallback_request or ""}
    return arguments


def _is_empty_or_garbled_request(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    stripped = text.replace("?", "").replace("？", "").replace("�", "").strip()
    return not stripped


def _should_repair_response(content: str) -> bool:
    text = str(content or "").strip().lower()
    if not text:
        return True
    repair_markers = [
        "request is empty",
        "provide a request",
        "couldn't understand your request",
        "could not understand your request",
        "please provide more information",
        "please rephrase",
    ]
    return any(marker in text for marker in repair_markers)


def _build_repair_prompt(original_request: str) -> str:
    return "\n".join(
        [
            "Your previous response did not process the user request.",
            "Process this exact user request:",
            original_request,
            "",
            "Return one JSON object only.",
            (
                "If a relevant tool is available and the request explicitly asks for web search "
                "or uses Japanese terms such as 調べて or 調査して, call that tool."
            ),
            (
                "If you cannot translate the request, call the relevant `*_assistant` tool with the exact "
                "original request as the `request` argument."
            ),
        ]
    )


def _build_required_tool_prompt(
    original_request: str | None,
    required_tool_names: set[str],
    reason: str | None,
    require_all: bool = False,
) -> str:
    tools = ", ".join(f"`{name}`" for name in sorted(required_tool_names))
    requirement = "all of these tools" if require_all else "one of these tools"
    lines = [
        "Your previous response did not complete the required tool action.",
        f"You must call {requirement} before returning a final answer: {tools}.",
    ]
    if reason:
        lines.append(f"Reason: {reason}")
    if original_request:
        lines.extend(["", "Original request:", original_request])
    lines.extend(
        [
            "",
            "Return exactly one JSON object and no markdown.",
            'Use this shape: {"type":"tool_call","tool":"tool_name","arguments":{...}}',
            "Do not claim completion until the tool result confirms it.",
        ]
    )
    return "\n".join(lines)
