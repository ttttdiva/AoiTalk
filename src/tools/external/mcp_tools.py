"""MCP (Model Context Protocol) integration tools."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import traceback
from typing import Any

import nest_asyncio

from src.services.failure_recorder import record_failure_event
from ..core import tool as _tool_decorator

# Agent tool callbacks may need to re-enter the current loop.
nest_asyncio.apply()

# Global MCP plugin instance (set by the active LLM client).
_mcp_plugin = None


def set_mcp_plugin(plugin):
    """Set the global MCP plugin instance."""
    global _mcp_plugin
    _mcp_plugin = plugin


def _format_mcp_result(result: Any, *, server_name: str, tool_name: str) -> str:
    if result is None:
        return f"Failed to execute tool {tool_name} on server {server_name}"

    if result.get("isError", False):
        return f"Tool execution error: {result.get('content', 'Unknown error')}"

    content = result.get("content", [])
    if isinstance(content, list):
        texts = []
        for item in content:
            if hasattr(item, "text"):
                texts.append(str(item.text))
            elif isinstance(item, dict) and "text" in item:
                texts.append(str(item["text"]))
            else:
                texts.append(str(item))
        return "\n".join(texts)
    return str(content)


def _parse_arguments(arguments_json: Any) -> dict[str, Any]:
    if isinstance(arguments_json, dict):
        return arguments_json
    if arguments_json in ("", None):
        return {}
    return json.loads(arguments_json)


async def call_mcp_tool_async(
    server_name: str,
    tool_name: str,
    arguments_json: Any = "{}",
) -> str:
    """Execute an MCP tool asynchronously."""
    print(
        f"[Tool] call_mcp_tool_async invoked: "
        f"{server_name}.{tool_name}({arguments_json})"
    )

    global _mcp_plugin

    if _mcp_plugin is None:
        return "MCP plugin is not initialized"

    if not _mcp_plugin.is_initialized():
        return "MCP plugin is not active"

    try:
        arguments = _parse_arguments(arguments_json)
    except json.JSONDecodeError as e:
        return f"Invalid arguments JSON: {arguments_json} - {str(e)}"

    try:
        server_exists = server_name in getattr(_mcp_plugin.client, "sessions", {})
        if server_exists:
            print(f"[Tool] Executing MCP tool: {server_name}.{tool_name}")
            result = await _mcp_plugin.client.call_tool(server_name, tool_name, arguments)
            formatted = _format_mcp_result(
                result,
                server_name=server_name,
                tool_name=tool_name,
            )
            print(f"[Tool] MCP tool result: {formatted}")
            return formatted

        tool_call = {
            "name": f"mcp_{server_name}_{tool_name}",
            "arguments": arguments,
        }
        print(f"[Tool] Executing MCP tool via fallback: {tool_call['name']}")
        result = await _mcp_plugin.execute_tool(tool_call)
        print(f"[Tool] MCP tool result: {result}")
        return result
    except Exception as e:
        error_msg = f"MCP tool execution error: {str(e)}"
        print(f"[Tool] {error_msg}")
        traceback.print_exc()
        await record_failure_event(
            source="tool",
            operation="mcp_tool",
            tool_name=f"{server_name}.{tool_name}",
            error=e,
            input_summary={"arguments": arguments_json},
        )
        return error_msg


def call_mcp_tool(
    server_name: str,
    tool_name: str,
    arguments_json: Any = "{}",
) -> str:
    """Execute an MCP tool from synchronous code."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        return asyncio.run(call_mcp_tool_async(server_name, tool_name, arguments_json))

    # Re-enter the loop that owns the MCP client sessions.
    if (
        _mcp_plugin is not None
        and hasattr(_mcp_plugin, "is_initialized_in_current_loop")
        and _mcp_plugin.is_initialized_in_current_loop()
    ):
        return loop.run_until_complete(
            call_mcp_tool_async(server_name, tool_name, arguments_json)
        )

    # Fallback for callers that are executing in a different async context.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            asyncio.run,
            call_mcp_tool_async(server_name, tool_name, arguments_json),
        )
        return future.result()


@_tool_decorator
def use_mcp_tool(server_name: str, tool_name: str, arguments_json: str = "{}") -> str:
    """Execute an MCP tool.

    Args:
        server_name: MCP server name
        tool_name: MCP tool name
        arguments_json: Tool arguments as a JSON string
    """
    return call_mcp_tool(server_name, tool_name, arguments_json)


def create_mcp_tool_wrapper(mcp_plugin):
    """Create a wrapper for MCP tools."""
    set_mcp_plugin(mcp_plugin)
    return [use_mcp_tool]
