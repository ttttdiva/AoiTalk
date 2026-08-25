"""
キーワード検出器実装
"""

from ....features import Features

__all__ = []

if Features.entertainment():
    from .spotify_detector import SpotifyLLMKeywordDetector

    __all__.append("SpotifyLLMKeywordDetector")
