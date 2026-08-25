"""Shared TTS engine bootstrap for Pipeline and Voice Session paths."""

from __future__ import annotations

import os
import platform
import traceback
from typing import Any, Mapping

from .manager import TTSManager


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        return config.get(key, default)
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            return getter(key)
    return getattr(config, key, default)


def resolve_character_config(config: Any, character_name: str) -> dict[str, Any] | None:
    getter = getattr(config, "get_character_config", None)
    if not callable(getter):
        return None
    try:
        resolved = getter(character_name)
    except Exception:
        return None
    return dict(resolved) if isinstance(resolved, Mapping) else None


async def initialize_tts_engine(
    tts_manager: TTSManager,
    *,
    config: Any,
    preferred_engine: str,
    character_config: Mapping[str, Any],
) -> bool:
    """Initialize and register the preferred engine on ``tts_manager``."""

    engine_initialized = False
    normalized_engine = str(preferred_engine or "").strip().casefold()

    if normalized_engine == "voiceroid":
        voiceroid_engine = await tts_manager.create_voiceroid_engine(dict(character_config))
        if voiceroid_engine:
            tts_manager.register_engine("voiceroid", voiceroid_engine)
            tts_manager.set_engine("voiceroid")
            engine_initialized = True

    elif normalized_engine == "aivoice":
        aivoice_path = _config_get(config, "aivoice_engine_path")
        aivoice_engine = await tts_manager.create_aivoice_engine(aivoice_path)
        if aivoice_engine:
            tts_manager.register_engine("aivoice", aivoice_engine)
            tts_manager.set_engine("aivoice")
            engine_initialized = True

    elif normalized_engine == "aivisspeech":
        aivisspeech_path = os.getenv("AIVISSPEECH_ENGINE_PATH")
        if aivisspeech_path:
            if platform.system() == "Windows" and "$HOME" in aivisspeech_path:
                home_path = os.path.expanduser("~")
                aivisspeech_path = aivisspeech_path.replace("$HOME", home_path)
            aivisspeech_path = os.path.expandvars(aivisspeech_path)
            if not os.path.exists(aivisspeech_path):
                fallback_path = os.getenv("AIVISSPEECH_ENGINE_FALLBACK_PATH")
                if fallback_path:
                    if platform.system() == "Windows" and "$HOME" in fallback_path:
                        home_path = os.path.expanduser("~")
                        fallback_path = fallback_path.replace("$HOME", home_path)
                    fallback_path = os.path.expandvars(fallback_path)
                    if os.path.exists(fallback_path):
                        aivisspeech_path = fallback_path
        else:
            fallback_path = os.getenv("AIVISSPEECH_ENGINE_FALLBACK_PATH")
            if fallback_path:
                if platform.system() == "Windows" and "$HOME" in fallback_path:
                    home_path = os.path.expanduser("~")
                    fallback_path = fallback_path.replace("$HOME", home_path)
                aivisspeech_path = os.path.expandvars(fallback_path)

        if aivisspeech_path and os.path.exists(aivisspeech_path):
            aivisspeech_engine = await tts_manager.create_aivisspeech_engine(aivisspeech_path)
            if aivisspeech_engine:
                tts_manager.register_engine("aivisspeech", aivisspeech_engine)
                tts_manager.set_engine("aivisspeech")
                engine_initialized = True

    elif normalized_engine == "nijivoice":
        nijivoice_api_key = _config_get(config, "nijivoice_api_key")
        if nijivoice_api_key:
            nijivoice_engine = await tts_manager.create_nijivoice_engine(nijivoice_api_key)
            if nijivoice_engine:
                tts_manager.register_engine("nijivoice", nijivoice_engine)
                tts_manager.set_engine("nijivoice")
                engine_initialized = True

    elif normalized_engine == "voicevox":
        voicevox_path = _config_get(config, "voicevox_path")
        voicevox_engine = await tts_manager.create_voicevox_engine(voicevox_path)
        if voicevox_engine:
            tts_manager.register_engine("voicevox", voicevox_engine)
            tts_manager.set_engine("voicevox")
            engine_initialized = True

    elif normalized_engine == "irodori_tts":
        irodori_engine = await tts_manager.create_irodori_tts_engine()
        if irodori_engine:
            tts_manager.register_engine("irodori_tts", irodori_engine)
            tts_manager.set_engine("irodori_tts")
            engine_initialized = True

    elif normalized_engine == "miotts":
        miotts_engine = await tts_manager.create_miotts_engine()
        if miotts_engine:
            tts_manager.register_engine("miotts", miotts_engine)
            tts_manager.set_engine("miotts")
            engine_initialized = True

    if not engine_initialized:
        traceback.print_exc()
        raise RuntimeError(
            f"指定されたTTSエンジン '{preferred_engine}' の初期化に失敗しました"
        )

    character_name = str(character_config.get("name") or "Unknown")
    tts_manager.register_character(character_name, dict(character_config))
    return engine_initialized


async def resolve_character_config_async(
    config: Any,
    character_name: str,
) -> dict[str, Any] | None:
    resolved = resolve_character_config(config, character_name)
    if resolved:
        return resolved
    try:
        from ..services.character_service import get_character_for_prompt

        db_character = await get_character_for_prompt(str(character_name))
    except Exception:
        return None
    if not isinstance(db_character, Mapping):
        return None
    return TTSManager._db_character_to_voice_config(db_character, None)


async def bootstrap_session_tts_manager(
    tts_manager: TTSManager,
    *,
    config: Any,
    character_name: str,
    character_config: Mapping[str, Any] | None = None,
) -> TTSManager:
    """Initialize ``tts_manager`` with the character's preferred engine."""

    resolved_config = (
        dict(character_config)
        if isinstance(character_config, Mapping)
        else await resolve_character_config_async(config, character_name)
    )
    if not resolved_config:
        raise RuntimeError(
            f"Character '{character_name}' voice configuration is unavailable"
        )
    voice_config = resolved_config.get("voice")
    if not isinstance(voice_config, Mapping):
        voice_config = {}
    preferred_engine = str(voice_config.get("engine") or "voicevox")
    await initialize_tts_engine(
        tts_manager,
        config=config,
        preferred_engine=preferred_engine,
        character_config=resolved_config,
    )
    return tts_manager


__all__ = [
    "bootstrap_session_tts_manager",
    "initialize_tts_engine",
    "resolve_character_config",
    "resolve_character_config_async",
]
