# stdout/stderr リダイレクト — 全importより前に実行すること
import os
import sys
import threading

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
logger = logging.getLogger("media-mcp")

sys.stdout = real_stdout
sys.stdout.reconfigure(line_buffering=True)

mcp = FastMCP("media")

# AudioPlayer と StreamManager の遅延初期化
_audio_player = None
_player_lock = threading.Lock()


def _get_audio_player():
    global _audio_player
    if _audio_player is None:
        with _player_lock:
            if _audio_player is None:
                from src.audio.player import AudioPlayer
                _audio_player = AudioPlayer()
    return _audio_player


def _get_stream_manager():
    from src.tools.entertainment.video_streaming.stream_manager import get_stream_manager
    return get_stream_manager()


# ツール登録
from .tools import youtube, niconico, playback

youtube.register(mcp, _get_audio_player, _get_stream_manager)
niconico.register(mcp, _get_audio_player, _get_stream_manager)
playback.register(mcp, _get_audio_player, _get_stream_manager)

logger.info("Media MCP サーバー初期化完了")


def main():
    """MCP サーバーを起動する。"""
    logger.info("Media MCP サーバーを起動します...")
    mcp.run()
