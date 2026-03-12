"""
Claude Code CLI backend implementation

Usage: claude -p "prompt" [--output-format json] [--model model] [--max-turns N]
       claude -p "prompt" --mcp-config '{"mcpServers": {...}}'
Docs: https://code.claude.com/docs/en/headless
"""

import json
import logging
import os
import platform
from typing import List, Dict, Any, Optional
from .base import CLIBackendBase

logger = logging.getLogger(__name__)


class ClaudeCLIBackend(CLIBackendBase):
    """Claude Code CLI backend implementation"""

    def __init__(self, model: Optional[str] = None):
        self._model = model
        super().__init__()

    def get_cli_command(self, prompt: str) -> List[str]:
        """Build Claude Code CLI command

        Format: claude -p "prompt" --output-format json [--model X] [--max-turns N]
        """
        bin_path = os.getenv("CLAUDE_BIN", "claude")
        cmd = [bin_path]

        if prompt:
            cmd.extend(["-p", prompt])

        # JSON出力で結果を構造化
        cmd.extend(["--output-format", "json"])

        # モデル指定
        model = self._model or os.getenv("CLAUDE_MODEL")
        if model:
            cmd.extend(["--model", model])

        # ターン数制限
        max_turns = os.getenv("CLAUDE_MAX_TURNS")
        if max_turns:
            cmd.extend(["--max-turns", max_turns])

        # 許可するツール
        allowed_tools = os.getenv("CLAUDE_ALLOWED_TOOLS")
        if allowed_tools:
            cmd.extend(["--allowedTools", allowed_tools])

        return cmd

    def get_provider_name(self) -> str:
        return "Claude Code"

    def parse_output(self, raw_output: str) -> str:
        """Parse Claude Code JSON output, extract result field"""
        output = raw_output.strip()
        try:
            data = json.loads(output)
            if isinstance(data, dict) and "result" in data:
                return data["result"]
        except (json.JSONDecodeError, TypeError):
            pass
        return output

    def get_mcp_args(self, mcp_servers: Dict[str, Any]) -> List[str]:
        """Generate --mcp-config argument for Claude Code

        Claude Code supports MCP via:
            claude -p "prompt" --mcp-config '{"mcpServers": {...}}'

        Converts AoiTalk config.yaml MCP format to Claude Code's mcpServers format.
        Environment variable placeholders (${VAR}) are resolved to actual values.
        """
        if not mcp_servers:
            return []

        is_windows = platform.system() == "Windows"
        platform_key = "windows" if is_windows else "linux"

        claude_mcp_servers = {}
        for name, server_config in mcp_servers.items():
            platform_config = server_config.get(platform_key, {})
            command = platform_config.get("command")
            args = platform_config.get("args", [])

            if not command:
                logger.warning(f"[Claude Code] MCP server '{name}': no {platform_key} command, skipping")
                continue

            # 環境変数プレースホルダーを解決
            env_raw = server_config.get("env", {})
            env_resolved = {}
            for key, value in env_raw.items():
                if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                    env_var = value[2:-1]
                    resolved = os.getenv(env_var, "")
                    if resolved:
                        env_resolved[key] = resolved
                    else:
                        logger.debug(f"[Claude Code] MCP '{name}': env var {env_var} not set, skipping")
                elif value is not None:
                    env_resolved[key] = str(value)

            claude_mcp_servers[name] = {
                "command": command,
                "args": args,
            }
            if env_resolved:
                claude_mcp_servers[name]["env"] = env_resolved

        if not claude_mcp_servers:
            return []

        mcp_config_json = json.dumps({"mcpServers": claude_mcp_servers}, ensure_ascii=False)
        logger.info(f"[Claude Code] MCP config: {len(claude_mcp_servers)} server(s) configured")
        return ["--mcp-config", mcp_config_json]
