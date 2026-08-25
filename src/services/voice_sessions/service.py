"""Voice Session orchestration over Live Voice runtime internals."""

from __future__ import annotations

from typing import Any, Mapping

from fastapi import HTTPException

from ..live_voice_service import (
    DEFAULT_REALTIME_VOICE,
    LiveVoiceActor,
    LiveVoiceError,
    LiveVoiceService,
)
from .access import assert_conversation_write_access, assert_project_write_access
from .models import VoiceSessionMode, voice_session_snapshot
from .policy import VoiceSessionPolicyResolver


class VoiceSessionService:
    """Resolve policy and expose the unified Voice Session API."""

    def __init__(
        self,
        live_voice: LiveVoiceService,
        *,
        config: Any | None = None,
        server: Any | None = None,
    ) -> None:
        self._live = live_voice
        self._config = config if config is not None else live_voice.config
        self._server = server

    @classmethod
    def from_server(cls, server: Any) -> "VoiceSessionService":
        from ..live_voice_service import LiveVoiceService

        live_voice = LiveVoiceService.from_server(server)
        return cls(live_voice, config=getattr(server, "config", None), server=server)

    async def start_session(
        self,
        *,
        actor: LiveVoiceActor,
        conversation_session_id: str | None = None,
        character_name: str = "assistant",
        project_id: str | None = None,
        include_project_context: bool | None = None,
        mode: str | VoiceSessionMode | None = None,
    ) -> dict[str, Any]:
        return await self._start_session_internal(
            actor=actor,
            conversation_session_id=conversation_session_id,
            character_name=character_name,
            project_id=project_id,
            include_project_context=include_project_context,
            mode=mode,
            allow_legacy_overrides=False,
            legacy_model=None,
            legacy_voice=None,
            legacy_instructions=None,
        )

    async def start_legacy_session(
        self,
        *,
        actor: LiveVoiceActor,
        conversation_session_id: str | None = None,
        character_name: str = "assistant",
        project_id: str | None = None,
        include_project_context: bool | None = None,
        mode: str | VoiceSessionMode | None = None,
        model: str | None = None,
        voice: str | None = None,
        instructions: str | None = None,
    ) -> dict[str, Any]:
        return await self._start_session_internal(
            actor=actor,
            conversation_session_id=conversation_session_id,
            character_name=character_name,
            project_id=project_id,
            include_project_context=include_project_context,
            mode=mode,
            allow_legacy_overrides=True,
            legacy_model=model,
            legacy_voice=voice,
            legacy_instructions=instructions,
        )

    async def _start_session_internal(
        self,
        *,
        actor: LiveVoiceActor,
        conversation_session_id: str | None = None,
        character_name: str = "assistant",
        project_id: str | None = None,
        include_project_context: bool | None = None,
        mode: str | VoiceSessionMode | None = None,
        allow_legacy_overrides: bool,
        legacy_model: str | None,
        legacy_voice: str | None,
        legacy_instructions: str | None,
    ) -> dict[str, Any]:
        await self._live._reserve_actor_start(actor)
        try:
            server = self._server or getattr(self._live, "_server", None)
            needs_conversation_acl = bool(
                str(conversation_session_id or "").strip()
            ) and actor.role != "admin"
            needs_project_acl = bool(str(project_id or "").strip()) and actor.role != "admin"
            if needs_conversation_acl or needs_project_acl:
                if server is None:
                    raise LiveVoiceError(
                        "Voice session server context is unavailable",
                        status_code=503,
                    )
                await assert_conversation_write_access(
                    server,
                    conversation_session_id,
                    actor,
                )
                await assert_project_write_access(server, project_id, actor)

            policy = VoiceSessionPolicyResolver.resolve(
                config=self._config,
                actor=actor,
                requested_mode=mode,
                character_name=character_name,
                legacy_model=legacy_model,
                legacy_voice=legacy_voice,
                legacy_instructions=legacy_instructions,
                allow_legacy_overrides=allow_legacy_overrides,
            )
            if policy.mode == VoiceSessionMode.PIPELINE:
                raise LiveVoiceError(
                    "Pipeline voice is not started through voice-sessions yet",
                    status_code=400,
                )

            resolved_voice = (
                str(policy.native_voice or legacy_voice or DEFAULT_REALTIME_VOICE).strip()
                or DEFAULT_REALTIME_VOICE
            )
            result = await self._live._start_session_unreserved(
                actor=actor,
                conversation_session_id=conversation_session_id,
                character_name=character_name,
                project_id=project_id,
                include_project_context=include_project_context,
                model=policy.realtime_model,
                voice=resolved_voice,
                instructions=policy.instructions,
                policy=policy,
                mode=policy.mode,
            )
        except HTTPException as exc:
            if exc.status_code != 409:
                await self._live._record_start_failure(actor)
            raise LiveVoiceError(str(exc.detail), status_code=exc.status_code) from exc
        except Exception as exc:
            if not (isinstance(exc, LiveVoiceError) and exc.status_code == 409):
                await self._live._record_start_failure(actor)
            raise
        else:
            session_payload = result.get("session")
            if isinstance(session_payload, Mapping):
                voice_session_id = str(
                    session_payload.get("voice_session_id")
                    or session_payload.get("id")
                    or ""
                ).strip()
                live_session = await self._live.get_session(voice_session_id, actor)
                if allow_legacy_overrides:
                    enriched = live_session.to_dict()
                else:
                    enriched = voice_session_snapshot(
                        live_session,
                        mode=policy.mode,
                        policy=policy,
                    )
                result = dict(result)
                result["session"] = enriched
                result["mode"] = str(policy.mode)
                result["voice_session_id"] = enriched.get("voice_session_id")
                result["conversation_session_id"] = enriched.get("conversation_session_id")
            return result
        finally:
            await self._live._release_actor_start(actor)

    async def get_session(
        self, voice_session_id: str, actor: LiveVoiceActor
    ) -> dict[str, Any]:
        session = await self._live.get_session(voice_session_id, actor)
        return voice_session_snapshot(
            session,
            mode=self._session_mode(session),
            policy=session.policy,
        )

    async def list_sessions(self, actor: LiveVoiceActor) -> list[dict[str, Any]]:
        await self._live._expire_sessions()
        async with self._live._lock:
            sessions = list(self._live._sessions.values())
        if actor.role != "admin":
            sessions = [item for item in sessions if item.actor.user_id == actor.user_id]
        sessions.sort(key=lambda item: item.last_activity_at, reverse=True)
        return [
            voice_session_snapshot(
                item,
                mode=self._session_mode(item),
                policy=item.policy,
            )
            for item in sessions
        ]

    async def close_session(
        self, voice_session_id: str, actor: LiveVoiceActor
    ) -> dict[str, Any]:
        return await self._live.close_session(voice_session_id, actor)

    async def connect_webrtc(
        self,
        voice_session_id: str,
        actor: LiveVoiceActor,
        *,
        sdp: str,
    ) -> Mapping[str, Any]:
        return await self._live.connect_unified_call(voice_session_id, actor, sdp=sdp)

    async def interrupt(
        self,
        voice_session_id: str,
        actor: LiveVoiceActor,
        *,
        spoken_prefix: str = "",
        partial_unknown: bool = False,
    ) -> dict[str, Any]:
        session = await self._live._active_session(voice_session_id, actor)
        interrupt_result = await self._live.interrupt_voice_session(
            session,
            spoken_prefix=spoken_prefix,
            partial_unknown=partial_unknown,
        )
        return {
            "success": True,
            "voice_session_id": session.id,
            "interrupt": interrupt_result,
            "session": voice_session_snapshot(
                session,
                mode=self._session_mode(session),
                policy=session.policy,
            ),
        }

    async def validate_audio_websocket(
        self,
        voice_session_id: str,
        actor: LiveVoiceActor,
    ) -> Any:
        return await self._live.validate_audio_websocket(voice_session_id, actor)

    async def run_audio_websocket(self, session: Any, websocket: Any) -> None:
        await self._live.run_audio_websocket(session, websocket)

    @staticmethod
    def _session_mode(session: Any) -> VoiceSessionMode:
        raw = str(getattr(session, "mode", "") or VoiceSessionMode.REALTIME_NATIVE.value)
        try:
            return VoiceSessionMode(raw)
        except ValueError:
            return VoiceSessionMode.REALTIME_NATIVE


__all__ = ["VoiceSessionService"]
