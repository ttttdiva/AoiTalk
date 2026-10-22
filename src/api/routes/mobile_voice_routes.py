"""Authenticated, bounded HTTP voice I/O for native chat (no server microphone)."""

from __future__ import annotations

import base64
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ...services.live_voice_service import LiveVoiceActor, LiveVoiceError
from ...services.mobile_voice_service import (
    MAX_AUDIO_BYTES,
    MAX_SPEECH_CHARS,
    MobileVoiceError,
    MobileVoiceService,
)
from ...services.voice_sessions.access import (
    assert_conversation_write_access,
    assert_project_write_access,
)
from ..router_helpers import cookie_auth_dependency
from .live_voice_routes import _maybe_await


class MobileVoiceScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_session_id: str | None = Field(default=None, max_length=200)
    project_id: str | None = Field(default=None, max_length=200)


class MobileVoiceTranscriptionRequest(MobileVoiceScope):
    audio_base64: str = Field(min_length=1, max_length=4 * ((MAX_AUDIO_BYTES + 2) // 3))
    mime_type: Literal[
        "audio/mp4", "audio/m4a", "audio/x-m4a", "audio/wav", "audio/webm"
    ] = "audio/mp4"


class MobileVoiceTranscriptionResponse(BaseModel):
    text: str


class MobileVoiceSpeechRequest(MobileVoiceScope):
    text: str = Field(min_length=1, max_length=MAX_SPEECH_CHARS)
    character_name: str = Field(min_length=1, max_length=200)


class MobileVoiceSpeechResponse(BaseModel):
    audio_base64: str
    mime_type: Literal["audio/wav"] = "audio/wav"


class MobileVoiceCapabilities(BaseModel):
    input_enabled: bool
    output_enabled: bool
    recognition_engine: str
    max_recording_seconds: int
    max_speech_chars: int


def register_mobile_voice_routes(
    app: FastAPI, server: Any, *, service: Any = None
) -> Any:
    runtime = service or getattr(server, "_mobile_voice_service", None)
    if runtime is None:
        runtime = MobileVoiceService(server)
    # Voice Session pipeline and native/mobile routes share the same bounded
    # ASR/TTS workers. This prevents duplicate Whisper/VOICEROID model loads.
    server._mobile_voice_service = runtime
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    async def actor(
        request: Request, _: None = Depends(require_auth)
    ) -> LiveVoiceActor:
        info = await _maybe_await(server._get_user_info_from_request(request))
        try:
            return LiveVoiceActor.from_user_info(info)
        except LiveVoiceError as exc:
            raise HTTPException(status_code=401, detail="認証が必要です。") from exc

    async def scope(payload: MobileVoiceScope, current: LiveVoiceActor) -> None:
        await assert_conversation_write_access(
            server, payload.conversation_session_id, current
        )
        await assert_project_write_access(server, payload.project_id, current)

    @app.get(
        "/api/mobile-voice/capabilities",
        response_model=MobileVoiceCapabilities,
        operation_id="get_mobile_voice_capabilities",
    )
    async def capabilities(_: LiveVoiceActor = Depends(actor)) -> Any:
        return runtime.capabilities()

    @app.post(
        "/api/mobile-voice/transcriptions",
        response_model=MobileVoiceTranscriptionResponse,
        operation_id="transcribe_mobile_voice",
    )
    async def transcribe(
        payload: MobileVoiceTranscriptionRequest,
        current: LiveVoiceActor = Depends(actor),
    ) -> Any:
        await scope(payload, current)
        try:
            return {
                "text": await runtime.transcribe(
                    payload.audio_base64, payload.mime_type
                )
            }
        except MobileVoiceError as exc:
            # MobileVoiceError contains only application-authored public messages,
            # never raw upstream exceptions. Preserve them through the generic
            # HTTP 5xx boundary, which intentionally redacts arbitrary strings.
            return JSONResponse(
                status_code=exc.status_code, content={"detail": str(exc)}
            )

    @app.post(
        "/api/mobile-voice/speech",
        response_model=MobileVoiceSpeechResponse,
        operation_id="synthesize_mobile_voice",
    )
    async def speech(
        payload: MobileVoiceSpeechRequest, current: LiveVoiceActor = Depends(actor)
    ) -> Any:
        await scope(payload, current)
        text = payload.text.strip()
        if not text:
            raise HTTPException(status_code=422, detail="読み上げる文章が空です。")
        try:
            audio = await runtime.synthesize(text, payload.character_name)
            return {
                "audio_base64": base64.b64encode(audio).decode("ascii"),
                "mime_type": "audio/wav",
            }
        except MobileVoiceError as exc:
            # MobileVoiceError contains only application-authored public messages,
            # never raw upstream exceptions. Preserve them through the generic
            # HTTP 5xx boundary, which intentionally redacts arbitrary strings.
            return JSONResponse(
                status_code=exc.status_code, content={"detail": str(exc)}
            )

    hooks = getattr(server, "_shutdown_background_tasks", None)
    if isinstance(hooks, list):
        hooks.append(runtime.close)
    return runtime
