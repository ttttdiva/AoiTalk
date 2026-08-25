"""Resolve server-owned Voice Session policy."""

from __future__ import annotations

import os
from typing import Any, Mapping

from ..live_voice_service import (
    DEFAULT_REALTIME_MODEL,
    DEFAULT_REALTIME_VOICE,
    LiveVoiceActor,
    LiveVoiceError,
)
from .models import (
    InputTranscriptionPolicy,
    TurnDetectionPolicy,
    VoiceSessionMode,
    VoiceSessionPolicy,
)


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping) and "." in key:
        current: Any = config
        for part in key.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return default
            current = current[part]
        return current
    if hasattr(config, "get"):
        return config.get(key, default)
    if isinstance(config, Mapping):
        return config.get(key, default)
    return default


_DEFAULT_ALLOWED_MODES = (
    VoiceSessionMode.PIPELINE.value,
    VoiceSessionMode.REALTIME_NATIVE.value,
    VoiceSessionMode.REALTIME_CHARACTER_TTS.value,
)


class VoiceSessionPolicyResolver:
    """Build canonical VoiceSessionPolicy from server config and ACL context."""

    @classmethod
    def resolve(
        cls,
        *,
        config: Any,
        actor: LiveVoiceActor,
        requested_mode: str | VoiceSessionMode | None = None,
        character_name: str | None = None,
        legacy_model: str | None = None,
        legacy_voice: str | None = None,
        legacy_instructions: str | None = None,
        allow_legacy_overrides: bool = False,
    ) -> VoiceSessionPolicy:
        del actor, character_name  # ACL enforced elsewhere; character affects instructions upstream.

        voice_sessions_cfg = _config_get(config, "voice_sessions", None)
        if isinstance(voice_sessions_cfg, Mapping) and "allowed_modes" in voice_sessions_cfg:
            allowed_raw = voice_sessions_cfg.get("allowed_modes")
            if not isinstance(allowed_raw, list):
                raise LiveVoiceError("Invalid voice_sessions.allowed_modes", status_code=500)
            allowed = {str(item).strip() for item in allowed_raw if str(item).strip()}
        else:
            allowed = set(_DEFAULT_ALLOWED_MODES)

        if not allowed:
            raise LiveVoiceError("No voice session modes are enabled", status_code=400)

        default_mode = str(
            _config_get(
                config,
                "voice_sessions.default_mode",
                VoiceSessionMode.REALTIME_NATIVE.value,
            )
            or VoiceSessionMode.REALTIME_NATIVE.value
        ).strip()
        mode_raw = str(requested_mode or default_mode).strip()
        alias_map = {
            "realtime": VoiceSessionMode.REALTIME_NATIVE.value,
            "live_voice": VoiceSessionMode.REALTIME_NATIVE.value,
        }
        mode_raw = alias_map.get(mode_raw, mode_raw)
        if mode_raw not in allowed:
            raise LiveVoiceError(f"Unsupported voice session mode: {mode_raw}", status_code=400)
        mode = VoiceSessionMode(mode_raw)

        realtime_cfg = _config_get(config, "voice_sessions.realtime", {}) or {}
        character_tts_cfg = _config_get(config, "voice_sessions.character_tts", {}) or {}
        turn_cfg = realtime_cfg.get("turn_detection", {}) if isinstance(realtime_cfg, Mapping) else {}
        transcription_cfg = (
            realtime_cfg.get("input_transcription", {})
            if isinstance(realtime_cfg, Mapping)
            else {}
        )

        if allow_legacy_overrides:
            model = (
                str(legacy_model or "").strip()
                or str(
                    realtime_cfg.get("model")
                    or os.getenv("OPENAI_REALTIME_MODEL")
                    or DEFAULT_REALTIME_MODEL
                ).strip()
            )
            voice = (
                str(legacy_voice or "").strip()
                or str(
                    realtime_cfg.get("native_voice")
                    or os.getenv("OPENAI_REALTIME_VOICE")
                    or DEFAULT_REALTIME_VOICE
                ).strip()
            )
            instructions = str(legacy_instructions or "").strip() or None
        else:
            model = str(
                realtime_cfg.get("model")
                or os.getenv("OPENAI_REALTIME_MODEL")
                or DEFAULT_REALTIME_MODEL
            ).strip()
            voice = str(
                realtime_cfg.get("native_voice")
                or os.getenv("OPENAI_REALTIME_VOICE")
                or DEFAULT_REALTIME_VOICE
            ).strip()
            instructions = None

        return VoiceSessionPolicy(
            mode=mode,
            realtime_model=model if mode != VoiceSessionMode.PIPELINE else None,
            native_voice=voice if mode == VoiceSessionMode.REALTIME_NATIVE else None,
            turn_detection=TurnDetectionPolicy(
                type=str(turn_cfg.get("type") or "semantic_vad"),
                interrupt_response=bool(turn_cfg.get("interrupt_response", True)),
                eagerness=str(turn_cfg.get("eagerness")).strip()
                if turn_cfg.get("eagerness")
                else None,
            ),
            input_transcription=InputTranscriptionPolicy(
                enabled=bool(transcription_cfg.get("enabled", True)),
                model=str(transcription_cfg.get("model") or "gpt-4o-transcribe"),
            ),
            tools_profile=str(realtime_cfg.get("tools_profile") or "voice"),
            instructions=instructions,
            interrupt_response=bool(turn_cfg.get("interrupt_response", True)),
            segment_max_chars=int(character_tts_cfg.get("segment_max_chars") or 180),
            segment_max_wait_ms=int(character_tts_cfg.get("segment_max_wait_ms") or 450),
            tts_queue_depth=int(character_tts_cfg.get("queue_depth") or 2),
        )


__all__ = ["VoiceSessionPolicyResolver"]
