"""
Video streaming audio extraction module for YouTube and Niconico
"""

from .video_streaming_tools import (
    search_and_play_youtube,
    play_youtube_audio,
    search_and_play_niconico,
    play_niconico_audio,
    stop_video_audio,
    get_video_playback_status
)

__all__ = [
    'search_and_play_youtube',
    'play_youtube_audio',
    'search_and_play_niconico',
    'play_niconico_audio', 
    'stop_video_audio',
    'get_video_playback_status'
]