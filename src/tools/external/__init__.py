"""
External integration tools for voice assistant
"""
from .mcp_tools import (
    call_mcp_tool,
    call_mcp_tool_async,
    create_mcp_tool_wrapper,
    set_mcp_plugin,
    use_mcp_tool,
)
from .mcp_plugin import MCPPlugin

__all__ = [
    'call_mcp_tool',
    'call_mcp_tool_async',
    'use_mcp_tool',
    'create_mcp_tool_wrapper',
    'set_mcp_plugin',
    'MCPPlugin',
]
