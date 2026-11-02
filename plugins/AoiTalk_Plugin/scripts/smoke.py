"""Exercise MCP initialize and tool discovery without an AoiTalk API connection."""
from __future__ import annotations

import anyio
import shutil
import sys
import tempfile
from pathlib import Path

from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main(package_root: Path, repository_root: Path, scratch_root: Path) -> None:
    python = sys.executable
    params = StdioServerParameters(
        command=python,
        args=["-m", "aoitalk_plugin.stdio"],
        cwd=str(package_root),
        env={
            "HOME_MCP_TOOL_LOG": "0",
            "PYTHONUNBUFFERED": "1",
            "AOITALK_PLUGIN_ROOT": str(package_root),
            "AOITALK_REPOSITORY_ROOT": str(repository_root),
            "AOITALK_PLUGIN_LOG_ROOT": str(scratch_root / "logs"),
            "TMP": str(scratch_root),
            "TEMP": str(scratch_root),
            "TMPDIR": str(scratch_root),
        },
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
    package_root = Path(__file__).resolve().parents[1]
    repository_root = package_root.parents[1]
    local_root = repository_root / ".local"
    local_root.mkdir(exist_ok=True)
    scratch_root = Path(tempfile.mkdtemp(prefix="aoitalk-plugin-smoke-", dir=local_root)).resolve()
    try:
        anyio.run(main, package_root, repository_root, scratch_root)
    finally:
        try:
            scratch_root.relative_to(local_root.resolve())
        except ValueError:
            raise RuntimeError("Plugin smoke scratch escaped repository .local") from None
        shutil.rmtree(scratch_root)
