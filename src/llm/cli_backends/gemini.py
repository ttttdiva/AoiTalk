"""
Gemini CLI backend implementation

Usage: gemini [-y/--yolo] -p "prompt"
Docs: https://google-gemini.github.io/gemini-cli/docs/cli/headless.html

MCP: Gemini CLI reads MCP config from ~/.gemini/settings.json
     No command-line option available.
"""

import logging
import os
from typing import List, Dict, Any
from .base import CLIBackendBase

logger = logging.getLogger(__name__)


class GeminiCLIBackend(CLIBackendBase):
    """Gemini CLI backend implementation"""

    def get_cli_command(self, prompt: str) -> List[str]:
        """Build Gemini CLI command

        Format: gemini [--yolo] -p "prompt"
        """
        bin_path = os.getenv("GEMINI_BIN", "gemini")
        cmd = [bin_path]

        # --yolo: 全ツール呼び出しを自動承認
        if os.getenv("GEMINI_AUTO_APPROVE", "true").lower() == "true":
            cmd.append("--yolo")

        if prompt:
            cmd.extend(["-p", prompt])

        return cmd

    def get_provider_name(self) -> str:
        return "Gemini CLI"

    def parse_output(self, raw_output: str) -> str:
        """Filter Gemini CLI specific output"""
        output = raw_output.strip()
        # Gemini CLI が出力する不要なメッセージを除去
        output = output.replace("Data collection is disabled.", "").strip()
        return output

    def get_mcp_args(self, mcp_servers: Dict[str, Any]) -> List[str]:
        """Gemini CLI does not support runtime MCP arguments.

        MCP servers must be configured in ~/.gemini/settings.json:
            {
              "mcpServers": {
                "server_name": {"command": "...", "args": [...], "env": {...}}
              }
            }
        """
        if mcp_servers:
            logger.info(
                f"[Gemini CLI] {len(mcp_servers)} MCP server(s) in config.yaml. "
                f"Gemini CLI requires MCP to be configured in ~/.gemini/settings.json"
            )
        return []
