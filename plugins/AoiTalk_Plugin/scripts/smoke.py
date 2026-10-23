"""Exercise MCP initialize and tool discovery without an AoiTalk API connection."""
from __future__ import annotations

import anyio
import sys
from pathlib import Path

from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main() -> None:
    package_root = Path(__file__).resolve().parents[1]
    python = sys.executable
    params = StdioServerParameters(
        command=python,
        args=["-m", "aoitalk_plugin.stdio"],
        cwd=str(package_root),
        env={"HOME_MCP_TOOL_LOG": "0", "PYTHONUNBUFFERED": "1"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
    names = {tool.name for tool in tools}
    if len(tools) != 35 or len(names) != 35:
        raise SystemExit(f"Expected 35 unique AoiTalk tools, found {len(tools)}")
    forbidden = {"run_command", "job_start", "call_mcp_tool"}
    if names & forbidden:
        raise SystemExit("Generic Gateway tools appeared in the AoiTalk catalog")
    print("MCP initialize/list_tools smoke passed (35 tools).")


if __name__ == "__main__":
    anyio.run(main)
