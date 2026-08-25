"""Voice Session domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..live_voice_service import LiveVoiceActor, LiveVoiceSession


class VoiceSessionMode(StrEnum):
    PIPELINE = "pipeline"
    REALTIME_NATIVE = "realtime_native"
    REALTIME_CHARACTER_TTS = "realtime_character_tts"


class VoiceSessionStatus(StrEnum):
    ACTIVE = "active"
    CONNECTING = "connecting"
    INTERRUPTING = "interrupting"
    ENDED = "ended"
    FAILED = "failed"


@dataclass(frozen=True)
class TurnDetectionPolicy:
    type: str = "semantic_vad"
    interrupt_response: bool = True
    eagerness: str | None = None


@dataclass(frozen=True)
class InputTranscriptionPolicy:
    enabled: bool = True
    model: str = "gpt-4o-transcribe"


@dataclass(frozen=True)
class VoiceSessionPolicy:
    mode: VoiceSessionMode

    realtime_model: str | None = None
    native_voice: str | None = None

    turn_detection: TurnDetectionPolicy = field(default_factory=TurnDetectionPolicy)
    input_transcription: InputTranscriptionPolicy = field(
        default_factory=InputTranscriptionPolicy
    )

    tools_profile: str = "voice"
    instructions: str | None = None

    interrupt_response: bool = True

    segment_max_chars: int = 180
    segment_max_wait_ms: int = 450
    tts_queue_depth: int = 2


VoiceActor = LiveVoiceActor


@dataclass
class VoiceSessionRuntimeHandles:
    """Optional runtime/output handles attached to a live session."""

    generation_id: str | None = None
    response_id: str | None = None
    item_id: str | None = None


# Compatibility aliases for gradual migration.
LiveVoiceSessionCompat = LiveVoiceSession


def session_capabilities(mode: VoiceSessionMode) -> dict[str, bool]:
    return {
        "webrtc": mode in {
            VoiceSessionMode.REALTIME_NATIVE,
            VoiceSessionMode.REALTIME_CHARACTER_TTS,
        },
        "custom_audio": mode == VoiceSessionMode.REALTIME_CHARACTER_TTS,
        "pipeline_status": mode == VoiceSessionMode.PIPELINE,
    }


def voice_session_snapshot(
    session: LiveVoiceSession,
    *,
    mode: VoiceSessionMode,
    policy: VoiceSessionPolicy | None = None,
) -> dict[str, Any]:
    """Browser-safe snapshot for the unified Voice Session API."""

    payload: dict[str, Any] = {
        "voice_session_id": session.id,
        "conversation_session_id": session.conversation_session_id,
        "mode": str(mode),
        "status": session.status,
        "provider": session.provider,
        "model": session.model,
        "voice": session.voice,
        "project_id": session.project_id,
        "include_project_context": session.include_project_context,
        "character_name": session.character_name,
        "privacy_mode": session.privacy_mode,
        "effective_privacy_mode": session.privacy_mode,
        "created_at": session.created_at.isoformat(),
        "last_activity_at": session.last_activity_at.isoformat(),
        "agent_run_id": session.agent_run_id,
        "call_id": session.call_id,
        "event_count": session.event_count,
        "last_event_type": session.last_event_type,
        "capabilities": session_capabilities(mode),
    }
    if policy is not None:
        payload["interrupt_response"] = policy.interrupt_response
    return payload


__all__ = [
    "InputTranscriptionPolicy",
    "LiveVoiceSessionCompat",
    "TurnDetectionPolicy",
    "VoiceActor",
    "VoiceSessionMode",
    "VoiceSessionPolicy",
    "VoiceSessionRuntimeHandles",
    "VoiceSessionStatus",
    "session_capabilities",
    "voice_session_snapshot",
]
