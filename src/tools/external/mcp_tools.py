"""MCP (Model Context Protocol) integration tools."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
from typing import Any

import nest_asyncio

from src.services.failure_recorder import record_failure_event
from ..core import tool as _tool_decorator

# Agent tool callbacks may need to re-enter the current loop.
nest_asyncio.apply()

# Global MCP plugin instance (set by the active LLM client).
_mcp_plugin = None
logger = logging.getLogger(__name__)


def set_mcp_plugin(plugin):
    """Set the global MCP plugin instance."""
    global _mcp_plugin
    _mcp_plugin = plugin


def _format_mcp_result(result: Any, *, server_name: str, tool_name: str) -> str:
    if result is None:
        return f"Failed to execute tool {tool_name} on server {server_name}"

    from .mcp_plugin import _format_mcp_result as format_result
    return format_result(result)


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
    plugin = _mcp_plugin
    if plugin is None:
        return "MCP plugin is not initialized"
    if not plugin.is_initialized():
        return "MCP plugin is not active"
    owner = getattr(plugin, "_owner_loop", None)
    if owner is not None and owner is not asyncio.get_running_loop():
        if owner.is_closed() or not owner.is_running():
            return "MCP connection owner loop is unavailable"
        pending = asyncio.run_coroutine_threadsafe(
            _call_plugin_tool(plugin, server_name, tool_name, arguments_json), owner,
        )
        return await asyncio.wait_for(asyncio.wrap_future(pending), timeout=35)
    return await _call_plugin_tool(plugin, server_name, tool_name, arguments_json)


async def _call_plugin_tool(plugin, server_name: str, tool_name: str, arguments_json: Any) -> str:
    try:
        arguments = _parse_arguments(arguments_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        return "Invalid MCP arguments JSON"

    try:
        server_exists = server_name in getattr(plugin.client, "sessions", {})
        if server_exists:
            result = await plugin.client.call_tool(server_name, tool_name, arguments)
            formatted = _format_mcp_result(
                result,
                server_name=server_name,
                tool_name=tool_name,
            )
            return formatted

        tool_call = {
            "name": f"mcp_{server_name}_{tool_name}",
            "arguments": arguments,
        }
        result = await plugin.execute_tool(tool_call)
        return result
    except Exception as e:
        error_msg = f"MCP tool execution failed ({type(e).__name__})"
        logger.warning("MCP tool execution failed (%s)", type(e).__name__)
        await record_failure_event(
            source="tool",
            operation="mcp_tool",
            tool_name=f"{server_name}.{tool_name}",
            error=RuntimeError(type(e).__name__),
            input_summary={"server": server_name, "tool": tool_name},
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

    plugin = _mcp_plugin
    owner = getattr(plugin, "_owner_loop", None)
    if owner is not None and owner is not loop:
        if owner.is_closed() or not owner.is_running():
            return "MCP connection owner loop is unavailable"
        future = asyncio.run_coroutine_threadsafe(
            _call_plugin_tool(plugin, server_name, tool_name, arguments_json), owner,
        )
        try:
            return future.result(timeout=35)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return "MCP tool execution timed out"
    if loop is not None:
        return loop.run_until_complete(call_mcp_tool_async(server_name, tool_name, arguments_json))
    # No live session may be moved to a temporary loop. This case is only
    # initialization/error reporting for legacy plugins without an owner.
    if getattr(getattr(_mcp_plugin, "client", None), "sessions", {}):
        return "MCP connection owner loop is unavailable"
    return asyncio.run(call_mcp_tool_async(server_name, tool_name, arguments_json))


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
