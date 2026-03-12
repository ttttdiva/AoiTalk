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
_project_root = Path(__file__).resolve().parents[4]  # src/tools/external/utility_mcp -> project root
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
logger = logging.getLogger("utility-mcp")

# stdout を MCP プロトコル用に復元
sys.stdout = real_stdout
sys.stdout.reconfigure(line_buffering=True)

# FastMCP サーバーインスタンス
mcp = FastMCP("utility")

# 環境変数
openweather_api_key = os.getenv("OPENWEATHER_API_KEY", "")

# ツール登録
from .tools import time_tools, calculation, weather

time_tools.register(mcp)
calculation.register(mcp)
weather.register(mcp, openweather_api_key)

logger.info("Utility MCP サーバー初期化完了")


def main():
    """MCP サーバーを起動する。"""
    logger.info("Utility MCP サーバーを起動します...")
    mcp.run()
