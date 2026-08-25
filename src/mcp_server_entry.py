"""Entry point for launching bundled MCP servers without scripts wrappers."""

from __future__ import annotations

import argparse
import importlib
import sys
import types
from pathlib import Path

from .features import Features


SERVER_MODULES = {
    "utility": "src.tools.external.utility_mcp.server",
    "web_search": "src.tools.external.web_search_mcp.server",
    "x_search": "src.tools.external.x_search_mcp.server",
    "workspace": "src.tools.external.workspace_mcp.server",
    "memory_knowledge": "src.tools.external.memory_rag_mcp.server",
    "os_operations": "src.tools.external.os_operations_mcp.server",
}
if not Features.is_enterprise():
    SERVER_MODULES["media"] = "src.tools.external.media_mcp.server"


def _install_tools_namespace_stubs() -> None:
    """Avoid importing src.tools.__init__ when launching standalone MCP servers."""
    project_root = Path(__file__).resolve().parents[1]
    namespaces = {
        "src.tools": project_root / "src" / "tools",
        "src.tools.external": project_root / "src" / "tools" / "external",
    }
    for name, path in namespaces.items():
        if name in sys.modules:
            continue
        module = types.ModuleType(name)
        module.__path__ = [str(path)]
        module.__package__ = name
        sys.modules[name] = module


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch an AoiTalk MCP server")
    parser.add_argument("server", nargs="?", choices=sorted(SERVER_MODULES))
    parser.add_argument("--list", action="store_true", help="List available server keys")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.list:
        for key in sorted(SERVER_MODULES):
            print(key)
        return
    if not args.server:
        raise SystemExit("server is required; use --list to show choices")

    _install_tools_namespace_stubs()
    module = importlib.import_module(SERVER_MODULES[args.server])
    module.main()


if __name__ == "__main__":
    main()
