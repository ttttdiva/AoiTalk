"""
Audio processing package

This module provides audio input/output functionality.
Feature Flag: FEATURE_VOICE_INPUT controls whether these components are loaded.
"""
from src.features import Features

__all__ = [
    "AudioRecorder",
    "AudioPlayer",
    "SpeechRecognitionManager",
    "SpeechRecognizerInterface",
    "HallucinationFilter",
]

_LAZY_IMPORTS = {
    "AudioRecorder": (".recorder", "AudioRecorder"),
    "AudioPlayer": (".player", "AudioPlayer"),
    "SpeechRecognitionManager": (".manager", "SpeechRecognitionManager"),
    "SpeechRecognizerInterface": (".base", "SpeechRecognizerInterface"),
    "HallucinationFilter": (".hallucination_filter", "HallucinationFilter"),
}


def __getattr__(name: str):
    if name not in _LAZY_IMPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if not Features.voice_input():
        return None

    module_name, attr_name = _LAZY_IMPORTS[name]
    from importlib import import_module

    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
