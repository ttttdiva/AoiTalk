"""
Gemini CLI backend implementation

Usage: gemini [-y/--yolo] -p "prompt"
Docs: https://google-gemini.github.io/gemini-cli/docs/cli/headless.html

MCP: Gemini CLI reads MCP config from ~/.gemini/settings.json
     No command-line option available.
"""

import base64
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from .base import CLIBackendBase

logger = logging.getLogger(__name__)


class GeminiCLIBackend(CLIBackendBase):
    """Gemini CLI backend implementation"""

    def __init__(self, model: Optional[str] = None):
        self._model = model
        super().__init__()

    def get_cli_command(self, prompt: str) -> List[str]:
        """Build Gemini CLI command

        Format: gemini [--yolo] [-m model] -p "prompt"
        """
        bin_path = os.getenv("GEMINI_BIN", "gemini")
        cmd = [bin_path]

        # --yolo: 全ツール呼び出しを自動承認
        if os.getenv("GEMINI_AUTO_APPROVE", "true").lower() == "true":
            cmd.append("--yolo")

        model = self._model or os.getenv("GEMINI_MODEL")
        if model:
            cmd.extend(["-m", model])

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

    def prepare_image_attachment(
        self, image_data: Dict[str, Any], cwd: Optional[Path] = None
    ) -> Optional[Tuple[str, Callable]]:
        """Save base64 image to a temp file and return '@filepath' suffix for Gemini CLI.

        Gemini CLI supports '@filepath' in the prompt to inject file contents,
        but only for files within the working directory (project sandbox).
        Temp files are therefore saved to {cwd}/cache/tmp/.

        Supported formats: PNG, JPG, GIF, WEBP, BMP (max 20MB).
        """
        data_url = image_data.get("data", "")
        if not data_url:
            return None

        try:
            # Strip data URL header if present
            if data_url.startswith("data:"):
                header, encoded = data_url.split(",", 1)
                # Infer extension from MIME type in header (e.g., "data:image/png;base64")
                mime_type = image_data.get("mimeType") or header.split(";")[0].split(":")[1]
            else:
                encoded = data_url
                mime_type = image_data.get("mimeType", "image/png")

            image_bytes = base64.b64decode(encoded)

            # Map MIME type to extension
            ext_map = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/jpg": ".jpg",
                "image/gif": ".gif",
                "image/webp": ".webp",
                "image/bmp": ".bmp",
            }
            ext = ext_map.get(mime_type, ".png")

            # Save within the project directory so Gemini CLI's path sandbox allows access.
            # Falls back to system temp if cwd is not provided.
            if cwd:
                tmp_dir = Path(cwd) / "cache" / "tmp"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                tmp = tempfile.NamedTemporaryFile(
                    suffix=ext, delete=False, dir=str(tmp_dir)
                )
            else:
                tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)

            tmp.write(image_bytes)
            tmp.close()
            tmp_path = Path(tmp.name)

            logger.info(
                f"[Gemini CLI] 画像を一時ファイルに保存: {tmp_path} "
                f"({mime_type}, {len(image_bytes)} bytes)"
            )

            def cleanup():
                try:
                    tmp_path.unlink(missing_ok=True)
                    logger.debug(f"[Gemini CLI] 一時画像ファイルを削除: {tmp_path}")
                except Exception as e:
                    logger.warning(f"[Gemini CLI] 一時ファイル削除失敗: {e}")

            # '@filepath' is appended to the prompt so Gemini CLI reads the image
            return (f" @{tmp_path}", cleanup)

        except Exception as e:
            logger.warning(f"[Gemini CLI] 画像処理失敗: {e}")
            return None

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
