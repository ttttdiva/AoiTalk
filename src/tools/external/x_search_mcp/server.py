# stdout/stderr リダイレクト — 全importより前に実行すること
# MCP プロトコルは stdout で JSON-RPC を使用するため、ライブラリ出力は stderr へ
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
    load_dotenv(_env_path, override=True)

from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("x-search-mcp")

# stdout を復元（MCP JSON-RPC 通信用）
sys.stdout = real_stdout
sys.stdout.reconfigure(line_buffering=True)

# FastMCP サーバーインスタンス
mcp = FastMCP("x_search")

# 共有 API クライアント
from .api_client import XSearchAPIClient

api_client = XSearchAPIClient()

# ツール登録
from .tools import search, analysis

search.register(mcp, api_client)
analysis.register(mcp, api_client)

logger.info("X Search MCP サーバー初期化完了")


def main():
    """MCP サーバーを起動する。"""
    logger.info("X Search MCP サーバーを起動します...")
    mcp.run()
