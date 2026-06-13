from __future__ import annotations

import pytest

from src.tools.external import mcp_tools


class _FakeClient:
    def __init__(self):
        self.sessions = {}

class _FakePlugin:
    def __init__(self):
        self.calls = []
        self.client = _FakeClient()

    def is_initialized(self) -> bool:
        return True

    def is_initialized_in_current_loop(self) -> bool:
        return True

    async def execute_tool(self, tool_call):
        self.calls.append(tool_call)
        return "ok"


@pytest.mark.asyncio
async def test_call_mcp_tool_reuses_current_loop():
    plugin = _FakePlugin()
    mcp_tools.set_mcp_plugin(plugin)

    result = mcp_tools.call_mcp_tool("utility", "get_current_time", "{}")

    assert result == "ok"
    assert plugin.calls == [{"name": "mcp_utility_get_current_time", "arguments": {}}]
