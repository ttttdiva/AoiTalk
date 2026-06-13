"""Provider-independent agent runtime helpers.

Provider adapters should only handle transport details. Required specialist
delegation, tool-result context shaping, and OpenAI-style tool loops live here
so OpenAI, Gemini, Ollama, SGLang, local compatible servers, and CLI adapters
can share the same agent contract.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from ..tools.registry import ToolRegistry
from .generation_policy import GenerationPolicy
from .tool_policy import (
    looks_like_filesystem_request,
    looks_like_project_management_request,
    looks_like_utility_request,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RequiredDelegationRule:
    tool_name: str
    detector: Callable[[str], bool]
    request_lines: tuple[str, ...]
    context_title: str
    context_lines: tuple[str, ...]


REQUIRED_DELEGATION_RULES: tuple[RequiredDelegationRule, ...] = (
    RequiredDelegationRule(
        tool_name="filesystem_assistant",
        detector=looks_like_filesystem_request,
        request_lines=(
            "Handle this filesystem/workspace request before the main assistant answers.",
            "Find, inspect, read, edit, or delete as the user requested and as permissions allow.",
            "For read/check requests, report found paths, inspected tree scope, files actually read, and items found but not read.",
            "Use exact relative paths and filenames with extensions for every file that was actually read.",
        ),
        context_title="Required Filesystem Delegation Result",
        context_lines=(
            "The user request required filesystem/workspace inspection or mutation before answering.",
            "Answer using this result as factual context. Distinguish completed reads/changes from unfetched or unverified items.",
            "Preserve exact relative paths and filenames from the delegation result; do not collapse read files into category labels.",
        ),
    ),
    RequiredDelegationRule(
        tool_name="project_management_assistant",
        detector=looks_like_project_management_request,
        request_lines=(
            "Handle this project/task/schedule request before the main assistant answers.",
            "Use the built-in project management tools for any requested task, schedule, timer, WBS, record-table, or project-information mutation.",
            "For task creation/update/delete/scheduling requests, perform the mutation and verify the resulting task state before answering.",
            "For task creation requests, never ask for project, classification, or priority when the user already provided the task content. Use the selected runtime project. If no runtime project exists, create the task in Inbox with priority medium.",
            "For project information DB completion requests, inspect existing project information, organize project filer documents when available, and include WBS.dbtable sync and issue-table sync when those files are configured or present; do not create normal task-list items from WBS unless the user explicitly asks for WBS task mirroring.",
            "Do not report a task, schedule, timer, record table, or project-information change as completed unless a tool result confirms it.",
            "Include created or updated task IDs and the final status/time fields when available.",
        ),
        context_title="Required Project Management Delegation Result",
        context_lines=(
            "The user request required project/task/schedule handling before answering.",
            "Answer using this result as factual context. Do not claim mutations succeeded unless the delegation result confirms them.",
            "Do not repeat the same project management mutation after a successful delegation result.",
            "If the result reports an error or lacks confirmation for a requested mutation, tell the user it was not completed.",
        ),
    ),
    RequiredDelegationRule(
        tool_name="utility_assistant",
        detector=looks_like_utility_request,
        request_lines=(
            "Handle this time, weather, or calculation request before the main assistant answers.",
            "Use the utility specialist tools for current time, weather lookup, or calculation.",
            "Do not estimate or invent time, weather, or calculation results. Report only confirmed tool results.",
        ),
        context_title="Required Utility Delegation Result",
        context_lines=(
            "The user request required time, weather, or calculation handling before answering.",
            "Answer using this result as factual context. Do not invent or alter the delegated utility result.",
            "If the result reports an error, tell the user the utility lookup was not completed.",
        ),
    ),
)


def build_required_delegation_context_sync(
    *,
    user_input: str,
    registry: ToolRegistry,
    policy: GenerationPolicy,
    log_prefix: str = "AgentRuntime",
) -> str:
    """Run required specialist delegation and return parent-facing context."""
    if not policy.required_delegation_enabled:
        return ""

    blocks: list[str] = []
    for rule in REQUIRED_DELEGATION_RULES:
        if not _should_run_rule(rule, user_input, registry):
            continue
        request = _build_delegation_request(rule, user_input)
        try:
            result = registry.execute(rule.tool_name, request=request)
        except Exception as exc:
            logger.exception("[%s] Required %s delegation failed", log_prefix, rule.tool_name)
            result = f"{_display_name(rule.tool_name)} delegation error: {exc}"
        blocks.append(_build_context_block(rule, result))

    return "\n\n".join(block for block in blocks if block)


async def build_required_delegation_context_async(
    *,
    user_input: str,
    registry: ToolRegistry,
    policy: GenerationPolicy,
    log_prefix: str = "AgentRuntime",
) -> str:
    """Async variant for the OpenAI Agents SDK client."""
    if not policy.required_delegation_enabled:
        return ""

    blocks: list[str] = []
    for rule in REQUIRED_DELEGATION_RULES:
        if not _should_run_rule(rule, user_input, registry):
            continue
        request = _build_delegation_request(rule, user_input)
        try:
            result = await registry.execute_async(rule.tool_name, request=request)
        except Exception as exc:
            logger.exception("[%s] Required %s delegation failed", log_prefix, rule.tool_name)
            result = f"{_display_name(rule.tool_name)} delegation error: {exc}"
        blocks.append(_build_context_block(rule, result))

    return "\n\n".join(block for block in blocks if block)


def compose_required_delegation_user_message(
    user_input: str,
    required_delegation_context: str,
) -> str:
    """Build the user message seen by the parent LLM after delegation."""
    if not required_delegation_context:
        return user_input
    return f"{required_delegation_context}\n\nCurrent user request:\n{user_input}"


def run_openai_tool_call_loop(
    *,
    initial_messages: list[dict[str, Any]],
    assistant_message: Any,
    api_kwargs: dict[str, Any],
    registry: ToolRegistry,
    create_completion: Callable[[dict[str, Any]], Any],
    log_prefix: str = "AgentRuntime",
    max_rounds: int = 5,
) -> str:
    """Execute OpenAI-compatible tool calls and re-prompt until text output."""
    current_messages = list(initial_messages)
    current_tool_calls = getattr(assistant_message, "tool_calls", None) or []
    current_content = getattr(assistant_message, "content", None)

    for _ in range(max_rounds):
        current_messages.append(
            {
                "role": "assistant",
                "content": current_content or "",
                "tool_calls": [_serialize_tool_call(tc) for tc in current_tool_calls],
            }
        )

        for tool_call in current_tool_calls:
            function_name, function_args = _tool_call_function(tool_call)
            try:
                result = registry.execute(function_name, **function_args)
                result_text = str(result)
            except Exception as exc:
                result_text = f"Error: {exc}"
            current_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": getattr(tool_call, "id", ""),
                    "content": result_text,
                }
            )
            logger.info("[%s] Tool %s -> %s", log_prefix, function_name, result_text[:160])

        follow_up_kwargs = dict(api_kwargs)
        follow_up_kwargs["messages"] = current_messages
        response = create_completion(follow_up_kwargs)
        choice = response.choices[0]
        message = choice.message
        next_tool_calls = getattr(message, "tool_calls", None)
        if next_tool_calls:
            current_tool_calls = next_tool_calls
            current_content = getattr(message, "content", None)
            continue
        return getattr(message, "content", None) or ""

    logger.warning("[%s] Tool call loop exceeded max rounds", log_prefix)
    return current_content or ""


def _should_run_rule(
    rule: RequiredDelegationRule,
    user_input: str,
    registry: ToolRegistry,
) -> bool:
    return rule.tool_name in registry and rule.detector(user_input)


def _build_delegation_request(rule: RequiredDelegationRule, user_input: str) -> str:
    return "\n".join(
        [
            *rule.request_lines,
            "",
            "User request:",
            user_input,
        ]
    )


def _build_context_block(rule: RequiredDelegationRule, result: Any) -> str:
    return "\n".join(
        [
            f"## {rule.context_title}",
            *rule.context_lines,
            "",
            str(result or "").strip(),
        ]
    ).strip()


def _display_name(tool_name: str) -> str:
    return tool_name.removesuffix("_assistant").replace("_", " ").title()


def _serialize_tool_call(tool_call: Any) -> dict[str, Any]:
    function_name, function_args = _tool_call_function(tool_call)
    function = getattr(tool_call, "function", None)
    raw_arguments = getattr(function, "arguments", None)
    if raw_arguments is None:
        raw_arguments = json.dumps(function_args, ensure_ascii=False)
    return {
        "id": getattr(tool_call, "id", ""),
        "type": "function",
        "function": {
            "name": function_name,
            "arguments": raw_arguments,
        },
    }


def _tool_call_function(tool_call: Any) -> tuple[str, dict[str, Any]]:
    function = getattr(tool_call, "function", None)
    function_name = str(getattr(function, "name", "") or "")
    raw_arguments = getattr(function, "arguments", None)
    if isinstance(raw_arguments, dict):
        return function_name, dict(raw_arguments)
    try:
        parsed = json.loads(raw_arguments or "{}")
    except Exception:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return function_name, parsed
