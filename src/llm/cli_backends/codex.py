"""
OpenAI Codex CLI backend implementation

Usage: codex exec "prompt" [--full-auto]
Docs: https://developers.openai.com/codex/noninteractive/

MCP: Codex CLI reads MCP config from ~/.codex/config.toml
     No command-line option available.
"""

import logging
import os
from typing import List, Dict, Any
from .base import CLIBackendBase

logger = logging.getLogger(__name__)


class CodexCLIBackend(CLIBackendBase):
    """Codex CLI backend implementation"""

    def get_cli_command(self, prompt: str) -> List[str]:
        """Build Codex CLI command

        Format: codex exec "prompt" [--full-auto]
        """
        bin_path = os.getenv("CODEX_BIN", "codex")
        cmd = [bin_path, "exec"]

        if prompt:
            cmd.append(prompt)

        # --full-auto: 全操作を自動承認
        if os.getenv("CODEX_AUTO_APPROVE", "true").lower() == "true":
            cmd.append("--full-auto")

        return cmd

    def get_provider_name(self) -> str:
        return "Codex CLI"

    def get_mcp_args(self, mcp_servers: Dict[str, Any]) -> List[str]:
        """Codex CLI does not support runtime MCP arguments.

        MCP servers must be configured in ~/.codex/config.toml:
            [mcp_servers.server_name]
            command = "..."
            args = [...]
            env = {KEY = "value"}
        """
        if mcp_servers:
            logger.info(
                f"[Codex CLI] {len(mcp_servers)} MCP server(s) in config.yaml. "
                f"Codex CLI requires MCP to be configured in ~/.codex/config.toml"
            )
        return []
