# stdout/stderr リダイレクト — 全importより前に実行すること
# STDIO MCPサーバーでは stdout が JSON-RPC 通信に使用されるため、
# ライブラリの print 出力が混入しないようにする
import os
import sys

real_stdout = sys.stdout
sys.stdout = sys.stderr

# --- ここから安全にインポート可能 ---
import logging
from pathlib import Path

from dotenv import load_dotenv

# プロジェクトルートの .env を読み込む
# MCPサーバーは standalone プロセスとして起動されるため、
# 明示的に .env を読む必要がある
_project_root = Path(__file__).resolve().parents[4]  # src/tools/external/clickup_mcp -> project root
_env_path = _project_root / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

from mcp.server.fastmcp import FastMCP

# ログ設定 (stderr のみ)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("clickup-mcp")

# stdout を MCP プロトコル用に復元
sys.stdout = real_stdout
sys.stdout.reconfigure(line_buffering=True)

# FastMCP サーバーインスタンス
mcp = FastMCP("clickup")

# 共有クライアントの初期化
from .api_client import ClickUpAPIClient
from .db_client import ClickUpDBClient

api_client = ClickUpAPIClient()

db_client = ClickUpDBClient()
if db_client.enabled:
    logger.info("PostgreSQL DB機能: 有効")
else:
    logger.info("PostgreSQL DB機能: 無効（APIのみで動作）")

# ツール登録
from .tools import workspace, tasks, search, comments

workspace.register(mcp, api_client, db_client)
tasks.register(mcp, api_client, db_client)
search.register(mcp, api_client, db_client)
comments.register(mcp, api_client, db_client)

logger.info("ClickUp MCP サーバー初期化完了")


def main():
    """MCP サーバーを起動する。"""
    logger.info("ClickUp MCP サーバーを起動します...")
    mcp.run()
