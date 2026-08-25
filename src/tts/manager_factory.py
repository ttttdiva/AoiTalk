"""Factory helpers for session-scoped TTS managers."""

from __future__ import annotations

from typing import Any, Mapping

from .engine_bootstrap import bootstrap_session_tts_manager, resolve_character_config
from .manager import TTSManager


def _config_as_dict(config: Any | None) -> dict[str, Any] | None:
    if config is None:
        return None
    if isinstance(config, dict):
        return config
    nested = getattr(config, "config", None)
    return nested if isinstance(nested, dict) else None


def create_session_tts_manager(config: dict[str, Any] | Any | None = None) -> TTSManager:
    """Create an empty session-scoped TTS manager."""

    return TTSManager(_config_as_dict(config))


async def create_bootstrapped_session_tts_manager(
    config: Any | None = None,
    *,
    character_name: str,
    character_config: Mapping[str, Any] | None = None,
) -> TTSManager:
    """Create and initialize a TTS manager for realtime character TTS."""

    manager = create_session_tts_manager(config)
    await bootstrap_session_tts_manager(
        manager,
        config=config,
        character_name=character_name,
        character_config=character_config,
    )
    return manager


__all__ = [
    "create_bootstrapped_session_tts_manager",
    "create_session_tts_manager",
    "resolve_character_config",
]
