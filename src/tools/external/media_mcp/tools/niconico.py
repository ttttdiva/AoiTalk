"""ニコニコ動画音声再生ツール"""

from __future__ import annotations

import re
import threading
import time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def _extract_niconico_id(url: str) -> Optional[str]:
    patterns = [
        r'nicovideo\.jp/watch/(sm\d+|nm\d+|so\d+)',
        r'nico\.ms/(sm\d+|nm\d+|so\d+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def register(mcp: FastMCP, get_audio_player, get_stream_manager):
    """ニコニコ動画ツールを MCP サーバーに登録する。"""

    def _play_niconico_impl(url: str) -> str:
        video_id = _extract_niconico_id(url)
        if not video_id:
            return "無効なニコニコ動画URLです。"

        audio_player = get_audio_player()
        if audio_player.is_playing():
            audio_player.stop()
            time.sleep(0.2)

        manager = get_stream_manager()

        def extract_and_play():
            result = manager.extract_audio(url, 'niconico')
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
                return f"ニコニコ動画「{info['title']}」の音声を再生開始しました！\n投稿者: {info.get('uploader', 'Unknown')}"

        return "ニコニコ動画の音声を抽出中です。しばらくお待ちください..."

    @mcp.tool()
    async def search_and_play_niconico(query: str) -> str:
        """ニコニコ動画で動画を検索して音声を再生する

        Args:
            query: 検索キーワード
        """
        import yt_dlp

        ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True}

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                search_results = ydl.extract_info(f"nicosearch:{query}", download=False)
                if not search_results or 'entries' not in search_results or not search_results['entries']:
                    return f"「{query}」に関するニコニコ動画が見つかりませんでした。"

                first_result = search_results['entries'][0]
                video_id = first_result.get('id')
                if not video_id:
                    return "動画情報の取得に失敗しました。"

                url = f"https://www.nicovideo.jp/watch/{video_id}"
                return _play_niconico_impl(url)

        except Exception as e:
            return f"ニコニコ動画検索中にエラーが発生しました: {str(e)}"

    @mcp.tool()
    async def play_niconico_audio(url: str) -> str:
        """ニコニコ動画から音声を抽出して再生する

        Args:
            url: ニコニコ動画のURL
        """
        return _play_niconico_impl(url)
