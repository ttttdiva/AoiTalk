"""Normalized runtime events for Voice Session orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class NormalizedVoiceEventType(StrEnum):
    SPEECH_STARTED = "speech_started"
    USER_TRANSCRIPT_FINAL = "user_transcript_final"
    ASSISTANT_TEXT_DELTA = "assistant_text_delta"
    ASSISTANT_TEXT_FINAL = "assistant_text_final"
    RESPONSE_STARTED = "response_started"
    RESPONSE_FINISHED = "response_finished"
    TOOL_CALL = "tool_call"
    RUNTIME_ERROR = "runtime_error"


@dataclass(frozen=True)
class NormalizedVoiceEvent:
    type: NormalizedVoiceEventType
    payload: dict[str, Any]


class VoiceConversationRuntime(Protocol):
    async def start(self, **kwargs: Any) -> Any: ...

    async def connect(self, **kwargs: Any) -> Any: ...

    async def interrupt(self, **kwargs: Any) -> Any: ...

    async def close(self, **kwargs: Any) -> Any: ...


_OPENAI_EVENT_MAP: dict[str, NormalizedVoiceEventType] = {
    "input_audio_buffer.speech_started": NormalizedVoiceEventType.SPEECH_STARTED,
    "conversation.item.input_audio_transcription.completed": NormalizedVoiceEventType.USER_TRANSCRIPT_FINAL,
    "conversation.item.input_audio_transcription.done": NormalizedVoiceEventType.USER_TRANSCRIPT_FINAL,
    "response.created": NormalizedVoiceEventType.RESPONSE_STARTED,
    "response.output_text.delta": NormalizedVoiceEventType.ASSISTANT_TEXT_DELTA,
    "response.output_text.done": NormalizedVoiceEventType.ASSISTANT_TEXT_FINAL,
    "response.audio_transcript.done": NormalizedVoiceEventType.ASSISTANT_TEXT_FINAL,
    "response.output_audio_transcript.done": NormalizedVoiceEventType.ASSISTANT_TEXT_FINAL,
    "response.done": NormalizedVoiceEventType.RESPONSE_FINISHED,
}


def normalize_openai_event(event: dict[str, Any]) -> NormalizedVoiceEvent | None:
    event_type = str(event.get("type") or "").strip()
    mapped = _OPENAI_EVENT_MAP.get(event_type)
    if mapped is None:
        if event_type.endswith("function_call_arguments.done"):
            return NormalizedVoiceEvent(
                type=NormalizedVoiceEventType.TOOL_CALL,
                payload=dict(event),
            )
        return None
    return NormalizedVoiceEvent(type=mapped, payload=dict(event))


__all__ = [
    "NormalizedVoiceEvent",
    "NormalizedVoiceEventType",
    "VoiceConversationRuntime",
    "normalize_openai_event",
]
