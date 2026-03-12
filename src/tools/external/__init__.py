"""
External integration tools for voice assistant
"""
from .mcp_tools import use_mcp_tool, create_mcp_tool_wrapper, set_mcp_plugin
from .mcp_plugin import MCPPlugin

__all__ = [
    'use_mcp_tool',
    'create_mcp_tool_wrapper',
    'set_mcp_plugin',
    'MCPPlugin',
]