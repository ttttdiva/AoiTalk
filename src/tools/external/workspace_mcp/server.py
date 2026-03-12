# stdout/stderr リダイレクト — 全importより前に実行すること
import os
import sys

real_stdout = sys.stdout
sys.stdout = sys.stderr

import logging
from pathlib import Path

from dotenv import load_dotenv

_project_root = Path(__file__).resolve().parents[4]
_env_path = _project_root / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("workspace-mcp")

sys.stdout = real_stdout
sys.stdout.reconfigure(line_buffering=True)

mcp = FastMCP("workspace")

# file_explorer_service をインポート
from src.tools.file_explorer import file_explorer_service

from .tools import file_operations

file_operations.register(mcp, file_explorer_service)

logger.info("Workspace MCP サーバー初期化完了")


def main():
    """MCP サーバーを起動する。"""
    logger.info("Workspace MCP サーバーを起動します...")
    mcp.run()
