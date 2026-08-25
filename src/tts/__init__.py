"""
Text-to-Speech package

This module provides TTS functionality.
Feature Flag: FEATURE_TTS_OUTPUT controls whether these components are loaded.
"""

import importlib.util
from importlib import import_module
import logging

from src.features import Features

__all__ = ["TTSManager"]

_LAZY_IMPORTS = {
    "TTSManager": (".manager", "TTSManager"),
}
_TTS_OUTPUT_ENABLED = Features.tts_output()
_TTS_DEPENDENCIES_AVAILABLE = False


def __getattr__(name: str):
    if name not in _LAZY_IMPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if not _TTS_OUTPUT_ENABLED or not _TTS_DEPENDENCIES_AVAILABLE:
        return None

    module_name, attr_name = _LAZY_IMPORTS[name]
    try:
        module = import_module(module_name, __name__)
        value = getattr(module, attr_name)
    except ImportError as exc:
        logger.warning("Optional TTS dependencies are unavailable: %s", exc)
        return None
    globals()[name] = value
    return value


logger = logging.getLogger(__name__)
if _TTS_OUTPUT_ENABLED:
    try:
        _TTS_DEPENDENCIES_AVAILABLE = any(
            importlib.util.find_spec(name) is not None
            for name in ("voicevox", "vvclient")
        )
    except (ImportError, ModuleNotFoundError, ValueError):
        _TTS_DEPENDENCIES_AVAILABLE = False
    if not _TTS_DEPENDENCIES_AVAILABLE:
        logger.warning(
            "Optional TTS dependencies are unavailable: "
            "voicevox-client or vvclient is unavailable"
        )
