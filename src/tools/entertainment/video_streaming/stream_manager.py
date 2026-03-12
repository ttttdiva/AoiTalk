"""
Video streaming manager for handling audio extraction and playback
"""

import os
import tempfile
import threading
import time
from typing import Optional, Dict, Any
import yt_dlp
from pathlib import Path
import wave
import subprocess
import json
import signal
import atexit
import shutil
from datetime import datetime, timedelta


class VideoStreamManager:
    """Manager for video streaming audio extraction and playback"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not hasattr(self, '_initialized'):
            self._initialized = True
            self._current_file = None
            self._current_info = {}
            self._is_playing = False
            self._audio_player = None
            self._download_progress = 0
            self._lock = threading.Lock()
            
            # Clean up old cache directories on startup
            self._cleanup_old_caches()
            
            # Create new temp directory
            self._temp_dir = tempfile.mkdtemp(prefix="aoitalk_video_")
            
            # Register cleanup handlers
            atexit.register(self._cleanup_all)
            # Signal handlers can only be set in the main thread
            try:
                signal.signal(signal.SIGTERM, self._signal_handler)
                signal.signal(signal.SIGINT, self._signal_handler)
            except ValueError:
                # Not in main thread, skip signal registration
                pass
            
            # Start periodic cleanup thread
            self._cleanup_thread = threading.Thread(target=self._periodic_cleanup, daemon=True)
            self._cleanup_thread.start()
            
            # yt-dlp options
            self._ydl_opts = {
                'format': 'bestaudio/best',
                'quiet': False,  # Show more output for debugging
                'no_warnings': False,
                'extract_flat': False,
                'outtmpl': os.path.join(self._temp_dir, '%(title)s.%(ext)s'),
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'wav',
                    'preferredquality': '0',
                }],
                'progress_hooks': [self._progress_hook],
                # Add timeout for downloads
                'socket_timeout': 30,
                # Limit file size for testing (10MB)
                'max_filesize': 10 * 1024 * 1024,
            }
            
    def _progress_hook(self, d):
        """Progress hook for yt-dlp downloads"""
        if d['status'] == 'downloading':
            if d.get('total_bytes'):
                self._download_progress = d['downloaded_bytes'] / d['total_bytes'] * 100
            elif d.get('total_bytes_estimate'):
                self._download_progress = d['downloaded_bytes'] / d['total_bytes_estimate'] * 100
        elif d['status'] == 'finished':
            self._download_progress = 100
            
    def extract_audio(self, url: str, platform: str = 'youtube') -> Dict[str, Any]:
        """Extract audio from video URL
        
        Args:
            url: Video URL
            platform: Platform name ('youtube' or 'niconico')
            
        Returns:
            Dict with status and file path or error message
        """
        # Check for Claude Code environment
        if os.environ.get('CLAUDE_CODE_ENVIRONMENT') == 'true':
            print(f"[VideoStream] Claude Code環境を検出 - シミュレーションモードで実行")
            return self._simulate_extraction(url, platform)
            
        try:
            with self._lock:
                # Clean up previous file
                if self._current_file and os.path.exists(self._current_file):
                    try:
                        os.remove(self._current_file)
                    except:
                        pass
                        
                self._download_progress = 0
                
                # Platform-specific options
                opts = self._ydl_opts.copy()
                
                if platform == 'niconico':
                    # Try to use cookies if available
                    cookies_file = os.path.expanduser('~/.aoitalk/niconico_cookies.txt')
                    if os.path.exists(cookies_file):
                        opts['cookiefile'] = cookies_file
                        
                # Extract audio
                with yt_dlp.YoutubeDL(opts) as ydl:
                    print(f"[VideoStream] Extracting audio from {platform}: {url}")
                    
                    # Extract info first
                    print(f"[VideoStream] Getting video info...")
                    info = ydl.extract_info(url, download=False)
                    print(f"[VideoStream] Video info obtained: {info.get('title', 'Unknown')}")
                    
                    self._current_info = {
                        'title': info.get('title', 'Unknown'),
                        'duration': info.get('duration', 0),
                        'uploader': info.get('uploader', 'Unknown'),
                        'platform': platform,
                        'url': url
                    }
                    
                    # Download and extract audio
                    print(f"[VideoStream] Starting download and extraction...")
                    ydl.download([url])
                    print(f"[VideoStream] Download completed")
                    
                    # Find the extracted WAV file
                    wav_files = list(Path(self._temp_dir).glob('*.wav'))
                    if not wav_files:
                        return {
                            'status': 'error',
                            'message': '音声ファイルの変換に失敗しました'
                        }
                        
                    # Use the most recent file
                    self._current_file = str(max(wav_files, key=os.path.getctime))
                    
                    return {
                        'status': 'success',
                        'file_path': self._current_file,
                        'info': self._current_info
                    }
                    
        except yt_dlp.utils.DownloadError as e:
            error_msg = str(e)
            if 'This video is only available for registered users' in error_msg:
                return {
                    'status': 'error',
                    'message': 'この動画は登録ユーザーのみ視聴可能です。認証が必要です。'
                }
            elif 'Video unavailable' in error_msg:
                return {
                    'status': 'error', 
                    'message': 'この動画は利用できません。URLを確認してください。'
                }
            else:
                return {
                    'status': 'error',
                    'message': f'ダウンロードエラー: {error_msg}'
                }
                
        except Exception as e:
            return {
                'status': 'error',
                'message': f'予期しないエラー: {str(e)}'
            }
            
    def get_audio_data(self, file_path: str) -> Optional[bytes]:
        """Get WAV audio data from file
        
        Args:
            file_path: Path to WAV file
            
        Returns:
            WAV audio data as bytes or None
        """
        try:
            with open(file_path, 'rb') as f:
                return f.read()
        except Exception as e:
            print(f"[VideoStream] Error reading audio file: {e}")
            return None
            
    def cleanup(self):
        """Clean up temporary files"""
        try:
            if self._current_file and os.path.exists(self._current_file):
                os.remove(self._current_file)
                self._current_file = None
                
            # Clean up temp directory
            for file in Path(self._temp_dir).glob('*'):
                try:
                    os.remove(file)
                except:
                    pass
                    
        except Exception as e:
            print(f"[VideoStream] Cleanup error: {e}")
            
    def get_current_info(self) -> Dict[str, Any]:
        """Get current video information"""
        return self._current_info.copy()
        
    def get_download_progress(self) -> float:
        """Get download progress (0-100)"""
        return self._download_progress
        
    def is_playing(self) -> bool:
        """Check if currently playing"""
        return self._is_playing
        
    def set_playing_status(self, status: bool):
        """Set playing status"""
        self._is_playing = status
        
    def _simulate_extraction(self, url: str, platform: str) -> Dict[str, Any]:
        """Simulate extraction for Claude Code environment
        
        Args:
            url: Video URL
            platform: Platform name
            
        Returns:
            Simulated result
        """
        print(f"[VideoStream] シミュレーション: {platform}の音声抽出を開始")
        
        # Simulate getting video info
        import time
        time.sleep(1)  # Simulate network delay
        
        # Create dummy info
        if 'dQw4w9WgXcQ' in url:
            self._current_info = {
                'title': 'Rick Astley - Never Gonna Give You Up (Simulated)',
                'duration': 213,
                'uploader': 'Rick Astley',
                'platform': platform,
                'url': url
            }
        else:
            self._current_info = {
                'title': 'シミュレーション動画',
                'duration': 180,
                'uploader': 'テストユーザー',
                'platform': platform,
                'url': url
            }
            
        print(f"[VideoStream] シミュレーション: 動画情報取得完了 - {self._current_info['title']}")
        
        # Create a small dummy WAV file
        dummy_wav = os.path.join(self._temp_dir, 'simulated.wav')
        
        # Generate minimal WAV file (1 second of silence)
        import struct
        sample_rate = 44100
        num_channels = 2
        bits_per_sample = 16
        duration = 1  # 1 second
        
        num_samples = sample_rate * duration
        
        with wave.open(dummy_wav, 'w') as wav_file:
            wav_file.setnchannels(num_channels)
            wav_file.setsampwidth(bits_per_sample // 8)
            wav_file.setframerate(sample_rate)
            
            # Write silence
            for _ in range(num_samples):
                wav_file.writeframes(struct.pack('<hh', 0, 0))
                
        self._current_file = dummy_wav
        print(f"[VideoStream] シミュレーション: ダミー音声ファイル作成完了")
        
        return {
            'status': 'success',
            'file_path': dummy_wav,
            'info': self._current_info
        }
    
    def _cleanup_old_caches(self):
        """Clean up old cache directories from previous runs"""
        try:
            temp_base = tempfile.gettempdir()
            current_time = datetime.now()
            
            # Find all aoitalk_video_ directories
            for item in Path(temp_base).iterdir():
                if item.is_dir() and item.name.startswith('aoitalk_video_'):
                    try:
                        # Check directory age (clean if older than 1 hour)
                        dir_age = current_time - datetime.fromtimestamp(item.stat().st_mtime)
                        if dir_age > timedelta(hours=1):
                            print(f"[VideoStream] Cleaning old cache directory: {item}")
                            shutil.rmtree(item)
                    except Exception as e:
                        print(f"[VideoStream] Error cleaning {item}: {e}")
                        
        except Exception as e:
            print(f"[VideoStream] Error during cache cleanup: {e}")
            
    def _periodic_cleanup(self):
        """Periodically clean up old cache files"""
        while True:
            try:
                time.sleep(1800)  # Every 30 minutes
                self._cleanup_old_caches()
            except Exception as e:
                print(f"[VideoStream] Periodic cleanup error: {e}")
                
    def _signal_handler(self, signum, frame):
        """Handle termination signals"""
        print(f"[VideoStream] Received signal {signum}, cleaning up...")
        self._cleanup_all()
        
    def _cleanup_all(self):
        """Clean up all temporary files and directories"""
        try:
            # Clean current files
            self.cleanup()
            
            # Remove temp directory
            if hasattr(self, '_temp_dir') and os.path.exists(self._temp_dir):
                shutil.rmtree(self._temp_dir)
                print(f"[VideoStream] Cleaned up temp directory: {self._temp_dir}")
                
        except Exception as e:
            print(f"[VideoStream] Error during cleanup: {e}")
    
    def __del__(self):
        """Cleanup on deletion"""
        self._cleanup_all()


# Singleton instance getter
def get_stream_manager() -> VideoStreamManager:
    """Get video stream manager instance"""
    return VideoStreamManager()