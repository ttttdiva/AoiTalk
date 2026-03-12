"""
Speech recognition engines package
"""
# Only export base interface - engines will be loaded lazily
from ..base import SpeechRecognizerInterface

__all__ = ['SpeechRecognizerInterface']

# Note: Engines are now registered lazily in manager.py
# This avoids importing all engines at startup