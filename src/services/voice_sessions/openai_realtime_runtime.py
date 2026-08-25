"""OpenAI Realtime session.update builder from server-owned policy."""

from __future__ import annotations

from typing import Any

from .models import VoiceSessionMode, VoiceSessionPolicy


def build_realtime_session_update(
    policy: VoiceSessionPolicy,
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
    instructions: str | None = None,
) -> dict[str, Any]:
    """Build the ``session`` object for a sideband ``session.update`` event."""

    # GA Realtime ``session.update`` accepts a typed session object.  Keep the
    # type explicit so the sideband update cannot be interpreted as a legacy
    # session shape by the provider.
    update: dict[str, Any] = {"type": "realtime"}
    if tools is not None:
        update["tools"] = tools
    if tool_choice is not None:
        update["tool_choice"] = tool_choice
    resolved_instructions = instructions if instructions is not None else policy.instructions
    if resolved_instructions:
        update["instructions"] = resolved_instructions

    if policy.mode == VoiceSessionMode.REALTIME_CHARACTER_TTS:
        update["output_modalities"] = ["text"]
    else:
        update["output_modalities"] = ["audio"]

    audio: dict[str, Any] = {}
    input_audio: dict[str, Any] = {}
    if policy.input_transcription.enabled:
        input_audio["transcription"] = {"model": policy.input_transcription.model}
    turn_detection: dict[str, Any] = {"type": policy.turn_detection.type}
    if policy.turn_detection.eagerness:
        turn_detection["eagerness"] = policy.turn_detection.eagerness
    if policy.turn_detection.interrupt_response:
        turn_detection["interrupt_response"] = True
    input_audio["turn_detection"] = turn_detection
    audio["input"] = input_audio

    if policy.mode == VoiceSessionMode.REALTIME_NATIVE and policy.native_voice:
        audio["output"] = {"voice": policy.native_voice}

    if audio:
        update["audio"] = audio
    return update


__all__ = ["build_realtime_session_update"]
