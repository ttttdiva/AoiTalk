"""Call AoiTalk project-management agent tools from a repository worktree.

This bridge is intended for non-chat agents such as the agent harness Codex
runner. It reuses the existing ProjectManagementAgent tool definitions instead
of duplicating direct database write logic.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def find_repo_root(start: Path) -> Path:
    current = start.resolve()
    for path in (current, *current.parents):
        if (path / "CLAUDE.md").exists() or (path / ".git").exists():
            return path
    return current


REPO_ROOT = find_repo_root(Path(__file__).resolve())
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")


def build_tool_index() -> dict[str, Any]:
    with contextlib.redirect_stdout(sys.stderr):
        from src.agents.project_management_agent import ProjectManagementAgent

        agent = ProjectManagementAgent().agent
    return {
        str(getattr(tool, "name", "")): tool
        for tool in getattr(agent, "tools", [])
        if getattr(tool, "name", None)
    }


def tool_schema(tool: Any) -> dict[str, Any]:
    return {
        "name": getattr(tool, "name", ""),
        "description": getattr(tool, "description", "") or "",
        "parameters": tool.to_json_schema(),
    }


def parse_json_value(value: str) -> Any:
    text = value.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def parse_key_value(raw: str) -> tuple[str, Any]:
    if "=" not in raw:
        raise ValueError(f"--arg must be key=value: {raw}")
    key, value = raw.split("=", 1)
    key = key.strip()
    if not key:
        raise ValueError(f"--arg key is empty: {raw}")
    return key, parse_json_value(value)


def parse_args_json(args_json: str, arg_items: list[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if args_json.strip():
        decoded = json.loads(args_json)
        if not isinstance(decoded, dict):
            raise ValueError("--args-json must be a JSON object")
        payload.update(decoded)
    for raw in arg_items:
        key, value = parse_key_value(raw)
        payload[key] = value
    return payload


def normalize_result(result: Any) -> Any:
    if isinstance(result, str):
        stripped = result.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return result
    return result


async def invoke_tool(tool: Any, args: dict[str, Any]) -> Any:
    result = await tool.execute_async(**args)
    return normalize_result(result)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Call AoiTalk project-management tools from an agent worktree."
    )
    parser.add_argument("--list-tools", action="store_true", help="List callable tools")
    parser.add_argument("--tool", help="Tool name to call")
    parser.add_argument(
        "--args-json",
        default="{}",
        help="JSON object passed to the tool, for example: '{\"project\":\"AoiTalk\"}'",
    )
    parser.add_argument(
        "--arg",
        action="append",
        default=[],
        help="Additional key=value argument. Values are parsed as JSON when possible.",
    )
    parser.add_argument("--raw", action="store_true", help="Print the raw tool result")
    return parser


async def async_main() -> int:
    parser = make_parser()
    args = parser.parse_args()
    tools = build_tool_index()

    if args.list_tools:
        print(
            json.dumps(
                {"success": True, "tools": [tool_schema(tool) for tool in tools.values()]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not args.tool:
        parser.error("--tool is required unless --list-tools is used")

    tool = tools.get(args.tool)
    if tool is None:
        print(
            json.dumps(
                {
                    "success": False,
                    "error": f"Unknown tool: {args.tool}",
                    "available_tools": sorted(tools),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    try:
        tool_args = parse_args_json(args.args_json, args.arg)
        result = await invoke_tool(tool, tool_args)
    except Exception as exc:
        print(
            json.dumps(
                {"success": False, "tool": args.tool, "error": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    if args.raw:
        print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))
    else:
        print(
            json.dumps(
                {"success": True, "tool": args.tool, "result": result},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    return 0


def main() -> int:
    os.environ.setdefault("PYTHONUTF8", "1")
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
