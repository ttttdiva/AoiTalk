"""
Video streaming tools for YouTube and Niconico audio playback
"""

import re
import threading
import time
from typing import Optional
from ...core import tool as function_tool
from .stream_manager import get_stream_manager
import yt_dlp


# Global audio player instance (will be initialized on first use)
_audio_player = None
_player_lock = threading.Lock()


def _get_audio_player():
    """Get or create audio player instance"""
    global _audio_player
    if _audio_player is None:
        with _player_lock:
            if _audio_player is None:
                from src.audio.player import AudioPlayer
                _audio_player = AudioPlayer()
    return _audio_player


def _extract_video_id(url: str, platform: str) -> Optional[str]:
    """Extract video ID from URL
    
    Args:
        url: Video URL
        platform: Platform name ('youtube' or 'niconico')
        
    Returns:
        Video ID or None
    """
    if platform == 'youtube':
        # YouTube URL patterns
        patterns = [
            r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/v/)([a-zA-Z0-9_-]{11})',
            r'youtube\.com/shorts/([a-zA-Z0-9_-]{11})'
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
                
    elif platform == 'niconico':
        # Niconico URL patterns
        patterns = [
            r'nicovideo\.jp/watch/(sm\d+|nm\d+|so\d+)',
            r'nico\.ms/(sm\d+|nm\d+|so\d+)'
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
                
    return None


@function_tool
def search_and_play_youtube(query: str) -> str:
    """
    YouTubeで動画を検索して音声を再生する
    
    Args:
        query: 検索キーワード（例："枕がデカすぎルマ"）
        
    Returns:
        再生開始メッセージまたはエラーメッセージ
    """
    print(f"[search_and_play_youtube] Searching for: {query}")
    
    # Search YouTube for the video
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Search for videos
            search_query = f"ytsearch5:{query}"
            search_results = ydl.extract_info(search_query, download=False)
            
            if not search_results or 'entries' not in search_results or not search_results['entries']:
                return f"❌ 「{query}」に関する動画が見つかりませんでした。"
            
            # Get the first result
            first_result = search_results['entries'][0]
            video_id = first_result.get('id')
            title = first_result.get('title', 'Unknown')
            
            if not video_id:
                return "❌ 動画情報の取得に失敗しました。"
            
            # Construct YouTube URL
            url = f"https://www.youtube.com/watch?v={video_id}"
            print(f"[search_and_play_youtube] Found video: {title} ({url})")
            
            # Play the video using existing play_youtube_audio function
            # Since play_youtube_audio is a FunctionTool, we need to call its inner function
            return _play_youtube_audio_impl(url)
            
    except Exception as e:
        print(f"[search_and_play_youtube] Error searching YouTube: {e}")
        return f"❌ YouTube検索中にエラーが発生しました: {str(e)}"


def _play_youtube_audio_impl(url: str) -> str:
    """
    Internal implementation of play_youtube_audio
    """
    print(f"[play_youtube_audio] Called with URL: {url}")
    
    # Validate URL
    video_id = _extract_video_id(url, 'youtube')
    if not video_id:
        return "❌ 無効なYouTube URLです。正しいURLを入力してください。"
    
    print(f"[play_youtube_audio] Video ID extracted: {video_id}")
        
    # Stop any current playback
    audio_player = _get_audio_player()
    if audio_player.is_playing():
        audio_player.stop()
        time.sleep(0.2)  # Wait for cleanup
        
    # Get stream manager
    manager = get_stream_manager()
    
    # Extract audio in a separate thread to avoid blocking
    def extract_and_play():
        print(f"[play_youtube_audio] Starting extraction thread")
        result = manager.extract_audio(url, 'youtube')
        print(f"[play_youtube_audio] Extraction result: {result['status']}")
        
        if result['status'] == 'success':
            # Get audio data
            audio_data = manager.get_audio_data(result['file_path'])
            if audio_data:
                print(f"[play_youtube_audio] Audio data loaded, size: {len(audio_data)} bytes")
                # Set playing status
                manager.set_playing_status(True)
                
                # Play audio
                try:
                    print(f"[play_youtube_audio] Starting playback")
                    audio_player.play(audio_data, blocking=True)
                    print(f"[play_youtube_audio] Playback completed")
                finally:
                    manager.set_playing_status(False)
                    manager.cleanup()
            else:
                print("[VideoStream] Failed to read audio data")
        else:
            print(f"[VideoStream] Extraction failed: {result['message']}")
            
    # Start extraction and playback
    extract_thread = threading.Thread(target=extract_and_play, daemon=True)
    extract_thread.start()
    
    # Wait for video info to be available (max 10 seconds)
    for _ in range(20):
        time.sleep(0.5)
        info = manager.get_current_info()
        if info.get('title'):
            return f"🎵 YouTube動画「{info['title']}」の音声を再生開始しました！\n👤 アップロード: {info.get('uploader', 'Unknown')}"
    
    # If we still don't have info after 10 seconds, return waiting message
    return "⏳ YouTube動画の音声を抽出中です。しばらくお待ちください..."


@function_tool
def play_youtube_audio(url: str) -> str:
    """
    YouTubeの動画から音声を抽出して再生する
    
    Args:
        url: YouTube動画のURL
        
    Returns:
        再生開始メッセージまたはエラーメッセージ
    """
    return _play_youtube_audio_impl(url)


@function_tool
def search_and_play_niconico(query: str) -> str:
    """
    ニコニコ動画で動画を検索して音声を再生する
    
    Args:
        query: 検索キーワード
        
    Returns:
        再生開始メッセージまたはエラーメッセージ
    """
    print(f"[search_and_play_niconico] Searching for: {query}")
    
    try:
        # Search using yt-dlp with niconico search
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Search for videos on Niconico
            search_query = f"nicosearch:{query}"
            search_results = ydl.extract_info(search_query, download=False)
            
            if not search_results or 'entries' not in search_results or not search_results['entries']:
                return f"❌ 「{query}」に関するニコニコ動画が見つかりませんでした。"
            
            # Get the first result
            first_result = search_results['entries'][0]
            video_id = first_result.get('id')
            title = first_result.get('title', 'Unknown')
            
            if not video_id:
                return "❌ 動画情報の取得に失敗しました。"
            
            # Construct Niconico URL
            url = f"https://www.nicovideo.jp/watch/{video_id}"
            print(f"[search_and_play_niconico] Found video: {title} ({url})")
            
            # Play the video using existing play_niconico_audio function
            return _play_niconico_audio_impl(url)
            
    except Exception as e:
        print(f"[search_and_play_niconico] Error searching Niconico: {e}")
        return f"❌ ニコニコ動画検索中にエラーが発生しました: {str(e)}"


def _play_niconico_audio_impl(url: str) -> str:
    """
    ニコニコ動画から音声を抽出して再生する
    
    Args:
        url: ニコニコ動画のURL
        
    Returns:
        再生開始メッセージまたはエラーメッセージ
    """
    # Validate URL
    video_id = _extract_video_id(url, 'niconico')
    if not video_id:
        return "❌ 無効なニコニコ動画URLです。正しいURLを入力してください。"
        
    # Stop any current playback
    audio_player = _get_audio_player()
    if audio_player.is_playing():
        audio_player.stop()
        time.sleep(0.2)  # Wait for cleanup
        
    # Get stream manager
    manager = get_stream_manager()
    
    # Extract audio in a separate thread to avoid blocking
    def extract_and_play():
        result = manager.extract_audio(url, 'niconico')
        
        if result['status'] == 'success':
            # Get audio data
            audio_data = manager.get_audio_data(result['file_path'])
            if audio_data:
                # Set playing status
                manager.set_playing_status(True)
                
                # Play audio
                try:
                    audio_player.play(audio_data, blocking=True)
                finally:
                    manager.set_playing_status(False)
                    manager.cleanup()
            else:
                print("[VideoStream] Failed to read audio data")
        else:
            print(f"[VideoStream] Extraction failed: {result['message']}")
            
    # Start extraction and playback
    extract_thread = threading.Thread(target=extract_and_play, daemon=True)
    extract_thread.start()
    
    # Wait for video info to be available (max 10 seconds)
    for _ in range(20):
        time.sleep(0.5)
        info = manager.get_current_info()
        if info.get('title'):
            return f"🎵 ニコニコ動画「{info['title']}」の音声を再生開始しました！\n👤 投稿者: {info.get('uploader', 'Unknown')}"
    
    # If we still don't have info after 10 seconds, return waiting message
    return "⏳ ニコニコ動画の音声を抽出中です。しばらくお待ちください..."


@function_tool
def play_niconico_audio(url: str) -> str:
    """
    ニコニコ動画から音声を抽出して再生する
    
    Args:
        url: ニコニコ動画のURL
        
    Returns:
        再生開始メッセージまたはエラーメッセージ
    """
    return _play_niconico_audio_impl(url)


@function_tool
def stop_video_audio() -> str:
    """
    動画音声の再生を停止する
    
    Returns:
        停止メッセージ
    """
    audio_player = _get_audio_player()
    manager = get_stream_manager()
    
    if audio_player.is_playing() or manager.is_playing():
        audio_player.stop()
        manager.set_playing_status(False)
        manager.cleanup()
        return "⏹️ 動画音声の再生を停止しました。"
    else:
        return "現在再生中の動画音声はありません。"


@function_tool  
def get_video_playback_status() -> str:
    """
    動画音声の再生状態を確認する
    
    Returns:
        再生状態の詳細情報
    """
    print("[get_video_playback_status] Called")
    
    audio_player = _get_audio_player()
    manager = get_stream_manager()
    
    is_playing = audio_player.is_playing() or manager.is_playing()
    print(f"[get_video_playback_status] Audio player playing: {audio_player.is_playing()}")
    print(f"[get_video_playback_status] Manager playing: {manager.is_playing()}")
    
    if is_playing:
        info = manager.get_current_info()
        progress = manager.get_download_progress()
        
        status = f"🎵 再生中: {info.get('title', '不明')}\n"
        status += f"📹 プラットフォーム: {info.get('platform', '不明')}\n"
        status += f"👤 投稿者: {info.get('uploader', '不明')}\n"
        
        if progress < 100:
            status += f"⏳ ダウンロード進捗: {progress:.1f}%"
            
        return status
    else:
        # Check if there was recent playback
        info = manager.get_current_info()
        if info and info.get('title'):
            return f"📹 最後に再生した動画: {info.get('title', '不明')}\n現在は再生していません。"
        else:
            return "現在再生中の動画音声はありません。"