"""Authenticated Live Voice HTTP adapter.

The browser never receives an OpenAI API key or ephemeral client secret. The
server-owned standard API key stays inside ``OpenAIRealtimeProvider`` and the
provider's unified ``/v1/realtime/calls`` exchange is kept behind these routes.
Existing ``/ws`` registration is untouched; transcript/progress events are
delivered to that channel through ``ConnectionManager.broadcast`` when
available.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Mapping

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ...services.live_voice_service import (
    MAX_EVENT_BODY_BYTES,
    MAX_SDP_BODY_BYTES,
    LiveVoiceActor,
    LiveVoiceError,
    LiveVoicePermissionError,
    LiveVoiceProviderError,
    LiveVoiceService,
)
from ...services.voice_sessions.access import (
    assert_conversation_write_access,
    assert_project_write_access,
)
from ..router_helpers import cookie_auth_dependency

if TYPE_CHECKING:
    from ..server import WebChatServer


class LiveVoiceSessionRequest(BaseModel):
    """Browser session setup payload.

    ``user_id`` is deliberately absent: the actor always comes from the
    authenticated request. ``session_id`` is accepted as a compatibility alias
    for the durable ConversationSession ID.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    conversation_session_id: str | None = Field(default=None, alias="session_id")
    project_id: str | None = None
    include_project_context: bool | None = None
    character_name: str | None = Field(default=None, max_length=200)
    provider: str = Field(default="openai_realtime", max_length=80)
    model: str | None = Field(default=None, max_length=160)
    voice: str | None = Field(default=None, max_length=80)
    instructions: str | None = Field(default=None, max_length=20_000)


def _http_error(exc: LiveVoiceError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


async def _maybe_await(value: Any) -> Any:
    import inspect

    return await value if inspect.isawaitable(value) else value


async def _read_bounded_body(request: Request, max_bytes: int) -> bytes:
    """Read a request incrementally without buffering an unbounded body.

    Live Voice endpoints require an explicit Content-Length so reverse proxies
    cannot hide an oversized chunked upload. The stream is still bounded at
    ``max_bytes`` for a correctly declared request.
    """

    declared = request.headers.get("content-length")
    if declared is None:
        raise HTTPException(
            status_code=411,
            detail="Content-Length is required for Live Voice payloads",
        )
    try:
        declared_length = int(declared)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
    if declared_length < 0:
        raise HTTPException(status_code=400, detail="Invalid Content-Length")
    if declared_length > max_bytes:
        raise HTTPException(status_code=413, detail="Live Voice payload is too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not isinstance(chunk, bytes):
            chunk = bytes(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="Live Voice payload is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def register_live_voice_routes(
    app: FastAPI,
    server: "WebChatServer",
    *,
    service: LiveVoiceService | None = None,
) -> LiveVoiceService:
    """Register authenticated Live Voice routes and return the runtime service."""

    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)
    runtime = service or LiveVoiceService.from_server(server)
    runtime._server = server
    # Tests and integrations may pass a service explicitly; expose it on the
    # server so lifecycle code can close/inspect the same registry.
    try:
        server.live_voice_service = runtime
    except Exception:
        pass
    # WebChatServer owns the FastAPI lifespan; await sideband/TTL tasks before
    # provider shutdown instead of relying on event-loop cancellation.
    shutdown_hooks = getattr(server, "_shutdown_background_tasks", None)
    if isinstance(shutdown_hooks, list) and not getattr(
        server, "_live_voice_shutdown_registered", False
    ):
        async def _shutdown_live_voice() -> None:
            await runtime.close()

        shutdown_hooks.append(_shutdown_live_voice)
        try:
            server._live_voice_shutdown_registered = True
        except Exception:
            pass

    async def _current_actor(request: Request) -> LiveVoiceActor:
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

    @app.get("/api/live-voice/sessions")
    async def list_live_voice_sessions(
        request: Request,
        _: None = Depends(require_auth),
    ) -> JSONResponse:
        actor = await _current_actor(request)
        return JSONResponse(
            {
                "success": True,
                "sessions": await runtime.list_sessions(actor),
            }
        )

    @app.post("/api/live-voice/session")
    @app.post("/api/live-voice/session/start")
    @app.post("/api/live-voice/sessions")
    @app.post("/api/live-voice/token")
    @app.post("/api/live-voice/client-secret")
    async def start_live_voice_session(
        payload: LiveVoiceSessionRequest,
        request: Request,
        _: None = Depends(require_auth),
    ) -> JSONResponse:
        actor = await _current_actor(request)
        if payload.provider.strip() != "openai_realtime":
            raise HTTPException(status_code=400, detail="Unsupported Live Voice provider")
        await assert_conversation_write_access(
            server,
            payload.conversation_session_id,
            actor,
        )
        await assert_project_write_access(server, payload.project_id, actor)
        try:
            result = await runtime.start_session(
                actor=actor,
                conversation_session_id=payload.conversation_session_id,
                character_name=(payload.character_name or getattr(server, "character_name", "assistant")),
                project_id=payload.project_id,
                include_project_context=payload.include_project_context,
                model=payload.model,
                voice=payload.voice,
                instructions=payload.instructions,
            )
        except LiveVoiceError as exc:
            raise _http_error(exc) from exc
        session = result.get("session") if isinstance(result, Mapping) else None
        session = session if isinstance(session, Mapping) else {}
        # ``session_id`` at the top level is the short-lived Live Voice runtime
        # ID used by the browser's /sdp and /events requests. The durable ID is
        # explicit as ``conversation_session_id``.
        live_id = str(
            session.get("voice_session_id")
            or session.get("live_session_id")
            or session.get("id")
            or result.get("voice_session_id")
            or ""
        )
        response = {
            "success": True,
            "id": live_id,
            "session_id": live_id,
            "live_session_id": live_id,
            "conversation_session_id": session.get("conversation_session_id"),
            **result,
        }
        return JSONResponse(response)

    @app.get("/api/live-voice/sessions/{live_session_id}")
    async def get_live_voice_session(
        live_session_id: str,
        request: Request,
        _: None = Depends(require_auth),
    ) -> JSONResponse:
        actor = await _current_actor(request)
        try:
            session = await runtime.get_session(live_session_id, actor)
        except LiveVoiceError as exc:
            raise _http_error(exc) from exc
        return JSONResponse({"success": True, "session": session.to_dict()})

    @app.post("/api/live-voice/sessions/{live_session_id}/sdp")
    @app.post("/api/live-voice/sessions/{live_session_id}/connect")
    @app.post("/api/live-voice/sdp")
    async def connect_live_voice_sdp(
        request: Request,
        _: None = Depends(require_auth),
    ) -> JSONResponse:
        actor = await _current_actor(request)
        content_type = str(request.headers.get("content-type") or "").casefold()
        session_id = request.path_params.get("live_session_id") or request.query_params.get("session_id")
        sdp = ""
        body: bytes
        body = await _read_bounded_body(request, MAX_SDP_BODY_BYTES)
        if "json" in content_type:
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (ValueError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=400, detail="Invalid SDP JSON payload") from exc
            if not isinstance(parsed, Mapping):
                raise HTTPException(status_code=400, detail="Invalid SDP payload")
            session_id = session_id or str(parsed.get("session_id") or parsed.get("live_session_id") or "")
            sdp = str(parsed.get("sdp") or parsed.get("offer") or "")
            # ``client_secret`` is accepted for browser compatibility but never
            # logged, echoed, or used as an AoiTalk credential.
        else:
            sdp = body.decode("utf-8", errors="replace")
        if not str(session_id or "").strip():
            raise HTTPException(status_code=400, detail="Live Voice session_id is required")
        try:
            result = await runtime.connect_unified_call(
                str(session_id),
                actor,
                sdp=sdp,
            )
        except LiveVoiceError as exc:
            raise _http_error(exc) from exc
        return JSONResponse({"success": True, **dict(result)})

    @app.post("/api/live-voice/sessions/{live_session_id}/events")
    async def handle_live_voice_event(
        live_session_id: str,
        request: Request,
        _: None = Depends(require_auth),
    ) -> JSONResponse:
        actor = await _current_actor(request)
        raw = await _read_bounded_body(request, MAX_EVENT_BODY_BYTES)
        try:
            event = json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid Realtime event JSON") from exc
        if not isinstance(event, Mapping):
            raise HTTPException(status_code=400, detail="Realtime event must be an object")
        try:
            result = await runtime.handle_event(live_session_id, actor, event)
        except LiveVoiceError as exc:
            raise _http_error(exc) from exc
        return JSONResponse(result)

    @app.post("/api/live-voice/sessions/{live_session_id}/end")
    @app.post("/api/live-voice/sessions/{live_session_id}/stop")
    @app.post("/api/live-voice/sessions/{live_session_id}/close")
    @app.delete("/api/live-voice/sessions/{live_session_id}")
    async def close_live_voice_session(
        live_session_id: str,
        request: Request,
        _: None = Depends(require_auth),
    ) -> JSONResponse:
        actor = await _current_actor(request)
        try:
            result = await runtime.close_session(live_session_id, actor)
        except LiveVoiceError as exc:
            raise _http_error(exc) from exc
        return JSONResponse(result)

    return runtime


__all__ = ["LiveVoiceSessionRequest", "register_live_voice_routes"]
