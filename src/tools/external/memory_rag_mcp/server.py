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
logger = logging.getLogger("memory-rag-mcp")

sys.stdout = real_stdout
sys.stdout.reconfigure(line_buffering=True)

mcp = FastMCP("memory_rag")

from .tools import memory, rag

memory.register(mcp)
rag.register(mcp)

logger.info("Memory/RAG MCP サーバー初期化完了")


def main():
    """MCP サーバーを起動する。"""
    logger.info("Memory/RAG MCP サーバーを起動します...")
    mcp.run()
