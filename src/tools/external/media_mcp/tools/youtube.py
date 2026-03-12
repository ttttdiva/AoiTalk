"""YouTube音声再生ツール"""

from __future__ import annotations

import re
import threading
import time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def _extract_youtube_id(url: str) -> Optional[str]:
    patterns = [
        r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/v/)([a-zA-Z0-9_-]{11})',
        r'youtube\.com/shorts/([a-zA-Z0-9_-]{11})'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def register(mcp: FastMCP, get_audio_player, get_stream_manager):
    """YouTubeツールを MCP サーバーに登録する。"""

    def _play_youtube_impl(url: str) -> str:
        video_id = _extract_youtube_id(url)
        if not video_id:
            return "無効なYouTube URLです。"

        audio_player = get_audio_player()
        if audio_player.is_playing():
            audio_player.stop()
            time.sleep(0.2)

        manager = get_stream_manager()

        def extract_and_play():
            result = manager.extract_audio(url, 'youtube')
            if result['status'] == 'success':
                audio_data = manager.get_audio_data(result['file_path'])
                if audio_data:
                    manager.set_playing_status(True)
                    try:
                        audio_player.play(audio_data, blocking=True)
                    finally:
                        manager.set_playing_status(False)
                        manager.cleanup()

        extract_thread = threading.Thread(target=extract_and_play, daemon=True)
        extract_thread.start()

        for _ in range(20):
            time.sleep(0.5)
            info = manager.get_current_info()
            if info.get('title'):
                return f"YouTube動画「{info['title']}」の音声を再生開始しました！\n投稿者: {info.get('uploader', 'Unknown')}"

        return "YouTube動画の音声を抽出中です。しばらくお待ちください..."

    @mcp.tool()
    async def search_and_play_youtube(query: str) -> str:
        """YouTubeで動画を検索して音声を再生する

        Args:
            query: 検索キーワード
        """
        import yt_dlp

        ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True}

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                search_results = ydl.extract_info(f"ytsearch5:{query}", download=False)
                if not search_results or 'entries' not in search_results or not search_results['entries']:
                    return f"「{query}」に関する動画が見つかりませんでした。"

                first_result = search_results['entries'][0]
                video_id = first_result.get('id')
                if not video_id:
                    return "動画情報の取得に失敗しました。"

                url = f"https://www.youtube.com/watch?v={video_id}"
                return _play_youtube_impl(url)

        except Exception as e:
            return f"YouTube検索中にエラーが発生しました: {str(e)}"

    @mcp.tool()
    async def play_youtube_audio(url: str) -> str:
        """YouTubeの動画から音声を抽出して再生する

        Args:
            url: YouTube動画のURL
        """
        return _play_youtube_impl(url)
