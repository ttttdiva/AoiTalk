"""
Audio processing package

This module provides audio input/output functionality.
Feature Flag: FEATURE_VOICE_INPUT controls whether these components are loaded.
"""
from src.features import Features

# Conditional imports based on Feature Flags
if Features.voice_input():
    from .recorder import AudioRecorder
    from .player import AudioPlayer
    from .manager import SpeechRecognitionManager
    from .base import SpeechRecognizerInterface
    from .hallucination_filter import HallucinationFilter
    
    __all__ = [
        'AudioRecorder', 
        'AudioPlayer', 
        'SpeechRecognitionManager', 
        'SpeechRecognizerInterface',
        'HallucinationFilter'
    ]
else:
    # Feature disabled - provide None stubs
    AudioRecorder = None
    AudioPlayer = None
    SpeechRecognitionManager = None
    SpeechRecognizerInterface = None
    HallucinationFilter = None
    
    __all__ = [
        'AudioRecorder', 
        'AudioPlayer', 
        'SpeechRecognitionManager', 
        'SpeechRecognizerInterface',
        'HallucinationFilter'
    ]