"""再生制御ツール"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP, get_audio_player, get_stream_manager):
    """再生制御ツールを MCP サーバーに登録する。"""

    @mcp.tool()
    async def stop_video_audio() -> str:
        """動画音声の再生を停止する"""
        audio_player = get_audio_player()
        manager = get_stream_manager()

        if audio_player.is_playing() or manager.is_playing():
            audio_player.stop()
            manager.set_playing_status(False)
            manager.cleanup()
            return "動画音声の再生を停止しました。"
        else:
            return "現在再生中の動画音声はありません。"

    @mcp.tool()
    async def get_video_playback_status() -> str:
        """動画音声の再生状態を確認する"""
        audio_player = get_audio_player()
        manager = get_stream_manager()

        is_playing = audio_player.is_playing() or manager.is_playing()

        if is_playing:
            info = manager.get_current_info()
            progress = manager.get_download_progress()

            status = f"再生中: {info.get('title', '不明')}\n"
            status += f"プラットフォーム: {info.get('platform', '不明')}\n"
            status += f"投稿者: {info.get('uploader', '不明')}\n"

            if progress < 100:
                status += f"ダウンロード進捗: {progress:.1f}%"

            return status
        else:
            info = manager.get_current_info()
            if info and info.get('title'):
                return f"最後に再生した動画: {info.get('title', '不明')}\n現在は再生していません。"
            else:
                return "現在再生中の動画音声はありません。"
