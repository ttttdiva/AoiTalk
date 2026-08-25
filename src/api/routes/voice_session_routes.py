"""Unified Voice Session HTTP adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from ...services.live_voice_service import (
    MAX_SDP_BODY_BYTES,
    LiveVoiceActor,
    LiveVoiceError,
    LiveVoicePermissionError,
)
from ...services.voice_sessions.service import VoiceSessionService
from ..router_helpers import cookie_auth_dependency
from .live_voice_routes import _http_error, _maybe_await, _read_bounded_body

if TYPE_CHECKING:
    from ..server import WebChatServer


class VoiceSessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_session_id: str | None = None
    project_id: str | None = None
    include_project_context: bool | None = None
    character_name: str | None = Field(default=None, max_length=200)
    mode: str | None = Field(default=None, max_length=80)


class VoiceSessionWebRtcRequest(BaseModel):
    sdp: str = Field(min_length=1, max_length=MAX_SDP_BODY_BYTES)


def register_voice_session_routes(
    app: FastAPI,
    server: "WebChatServer",
    *,
    service: VoiceSessionService | None = None,
) -> VoiceSessionService:
    runtime = service or VoiceSessionService.from_server(server)
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    async def _actor(request: Request) -> LiveVoiceActor:
        try:
            user_info = await _maybe_await(server._get_user_info_from_request(request))
        except Exception:
            user_info = None
        try:
            return LiveVoiceActor.from_user_info(user_info)
        except LiveVoiceError as exc:
            if isinstance(exc, LiveVoicePermissionError) and "Authenticated" in str(exc):
                raise HTTPException(status_code=401, detail="Not authenticated") from exc
            raise _http_error(exc) from exc

    @app.get("/api/voice-sessions")
    async def list_voice_sessions(
        request: Request,
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        actor = await _actor(request)
        return {"success": True, "sessions": await runtime.list_sessions(actor)}

    @app.post("/api/voice-sessions")
    async def create_voice_session(
        payload: VoiceSessionCreateRequest,
        request: Request,
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        actor = await _actor(request)
        try:
            return await runtime.start_session(
                actor=actor,
                mode=payload.mode,
                conversation_session_id=payload.conversation_session_id,
                character_name=(
                    payload.character_name or getattr(server, "character_name", "assistant")
                ),
                project_id=payload.project_id,
                include_project_context=payload.include_project_context,
            )
        except LiveVoiceError as exc:
            raise _http_error(exc) from exc

    @app.get("/api/voice-sessions/{voice_session_id}")
    async def get_voice_session(
        voice_session_id: str,
        request: Request,
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        actor = await _actor(request)
        try:
            session = await runtime.get_session(voice_session_id, actor)
        except LiveVoiceError as exc:
            raise _http_error(exc) from exc
        return {"success": True, "session": session}

    @app.post("/api/voice-sessions/{voice_session_id}/webrtc")
    async def connect_voice_session_webrtc(
        voice_session_id: str,
        request: Request,
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        actor = await _actor(request)
        body = await _read_bounded_body(request, MAX_SDP_BODY_BYTES)
        try:
            payload = VoiceSessionWebRtcRequest.model_validate_json(body)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid WebRTC payload") from exc
        try:
            result = await runtime.connect_webrtc(
                voice_session_id,
                actor,
                sdp=payload.sdp,
            )
        except LiveVoiceError as exc:
            raise _http_error(exc) from exc
        return {"success": True, **dict(result)}

    @app.post("/api/voice-sessions/{voice_session_id}/interrupt")
    async def interrupt_voice_session_route(
        voice_session_id: str,
        request: Request,
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        actor = await _actor(request)
        try:
            return await runtime.interrupt(voice_session_id, actor)
        except LiveVoiceError as exc:
            raise _http_error(exc) from exc

    @app.delete("/api/voice-sessions/{voice_session_id}")
    async def delete_voice_session(
        voice_session_id: str,
        request: Request,
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        actor = await _actor(request)
        try:
            return await runtime.close_session(voice_session_id, actor)
        except LiveVoiceError as exc:
            raise _http_error(exc) from exc

    @app.websocket("/api/voice-sessions/{voice_session_id}/audio")
    async def voice_session_audio_websocket(
        websocket: WebSocket,
        voice_session_id: str,
    ) -> None:
        if not await _maybe_await(server._authorize_websocket(websocket)):
            await websocket.close(code=1008)
            return
        user_info = await _maybe_await(server._get_user_info_from_websocket(websocket))
        try:
            actor = LiveVoiceActor.from_user_info(user_info)
        except LiveVoiceError:
            await websocket.close(code=1008)
            return
        try:
            session = await runtime.validate_audio_websocket(voice_session_id, actor)
        except LiveVoiceError:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            await runtime.run_audio_websocket(session, websocket)
        except WebSocketDisconnect:
            return
        except LiveVoiceError:
            try:
                await websocket.close(code=1011)
            except Exception:
                pass

    return runtime


__all__ = ["VoiceSessionCreateRequest", "register_voice_session_routes"]
