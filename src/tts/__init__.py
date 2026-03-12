"""
Text-to-Speech package

This module provides TTS functionality.
Feature Flag: FEATURE_TTS_OUTPUT controls whether these components are loaded.
"""
from src.features import Features

# Conditional imports based on Feature Flags
if Features.tts_output():
    from .manager import TTSManager
    
    __all__ = ['TTSManager']
else:
    # Feature disabled - provide None stub
    TTSManager = None
    
    __all__ = ['TTSManager']