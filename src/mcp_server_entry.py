"""Entry point for launching bundled MCP servers without scripts wrappers."""

from __future__ import annotations

import argparse
import importlib
import os
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
    # Formal Agent MCP servers are bundled first-party receivers.  They do
    # not route through the ordinary workspace/os-operation servers.
    "agent_activity": "src.services.agent_mcp_receiver_guard",
    "agent_dialogue": "src.services.agent_mcp_receiver_guard",
    "dialogue8": "src.services.agent_mcp_receiver_guard",
    "agent_dialogue8": "src.services.agent_mcp_receiver_guard",
}
if not Features.is_enterprise():
    SERVER_MODULES["media"] = "src.tools.external.media_mcp.server"

FORMAL_AGENT_MCP_SERVER_MODULE = "src.services.agent_mcp_receiver_guard"
FORMAL_AGENT_MCP_SERVER_KEYS = frozenset(
    {"agent_activity", "agent_dialogue", "dialogue8", "agent_dialogue8"}
)
FORMAL_AGENT_PROVIDER_SECRET_ENV_NAMES = frozenset(
    {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "XAI_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_COMPATIBLE_LOCAL_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GITHUB_TOKEN",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
    }
)


def _strip_formal_agent_provider_secrets() -> None:
    """Remove provider credentials before importing the formal receiver."""

    for name in FORMAL_AGENT_PROVIDER_SECRET_ENV_NAMES:
        os.environ.pop(name, None)


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

    if args.server in FORMAL_AGENT_MCP_SERVER_KEYS:
        # A formal receiver must not be imported through a user-supplied
        # Python module search path. The native caller separately validates
        # this exact entry module/argument shape before launching us.
        if any(os.environ.get(key, "").strip() for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")):
            raise SystemExit("formal Agent MCP rejects Python import shadowing environment")
        # Claude's OAuth/API environment belongs only to the provider process.
        # The bundled receiver receives opaque Agent markers and its explicit
        # database connection settings, never provider authentication.
        _strip_formal_agent_provider_secrets()
    _install_tools_namespace_stubs()
    module_name = SERVER_MODULES[args.server]
    if args.server in FORMAL_AGENT_MCP_SERVER_KEYS and module_name != FORMAL_AGENT_MCP_SERVER_MODULE:
        raise SystemExit("formal Agent MCP module binding is invalid")
    module = importlib.import_module(module_name)
    if args.server in FORMAL_AGENT_MCP_SERVER_KEYS:
        expected_receiver = (
            Path(__file__).resolve().parent
            / "services"
            / "agent_mcp_receiver_guard.py"
        )
        loaded_receiver = Path(getattr(module, "__file__", "")).resolve()
        if loaded_receiver != expected_receiver:
            raise SystemExit("formal Agent MCP receiver origin is not the bundled source")
        module.main(server_name=args.server)
    else:
        module.main()


if __name__ == "__main__":
    main()
