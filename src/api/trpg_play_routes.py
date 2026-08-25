"""TRPG Play 実行系 API（/api/trpg/sessions）。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Mapping
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..services.trpg_play_image_service import run_play_image_background
from ..services.trpg_play_service import (
    TrpgPlayConflict,
    TrpgPlayError,
    TrpgPlayForbidden,
    TrpgPlayService,
    filter_private_state_for_gm,
)

logger = logging.getLogger(__name__)


async def broadcast_private_state_update(
    play_connection_manager: Any,
    service: TrpgPlayService,
    session_id: UUID,
    private_state: dict[str, Any],
) -> None:
    """所有者へ全件、他 GM へは filter 済み（空 entries も含む）を送る。"""
    if play_connection_manager is None:
        return
    owner_participant_id = str(private_state.get("participant_id") or "")
    if not owner_participant_id:
        return
    await play_connection_manager.send_to_participants(
        str(session_id),
        {"type": "private_state", "private_state": private_state},
        participant_ids=[owner_participant_id],
    )
    filtered = filter_private_state_for_gm(private_state.get("state"))
    participants = await service._participants_for(session_id)
    gm_ids = [str(item.id) for item in participants if item.role == "gm"]
    gm_ids = [item for item in gm_ids if item != owner_participant_id]
    if not gm_ids:
        return
    gm_payload = {
        "participant_id": owner_participant_id,
        "state": filtered,
        "updated_at": private_state.get("updated_at"),
    }
    await play_connection_manager.send_to_participants(
        str(session_id),
        {"type": "private_state", "private_state": gm_payload, "gm_view": True},
        participant_ids=gm_ids,
    )


class SessionCreateRequest(BaseModel):
    work_id: UUID
    gm_mode: str = "human"
    title: str | None = None


class JoinRequest(BaseModel):
    invite_code: str
    display_name: str
    role: str = "player"
    story_character_id: UUID | None = None


class ActionRequest(BaseModel):
    kind: str = "action"
    text: str


class DiceRequest(BaseModel):
    expression: str
    note: str | None = None


class WhisperCreateRequest(BaseModel):
    body: str
    recipient_participant_ids: list[UUID] = Field(default_factory=list)


class PrivateStatePatchRequest(BaseModel):
    state: dict[str, Any] = Field(default_factory=dict)


class SnapshotPatchRequest(BaseModel):
    snapshot: dict[str, Any] = Field(default_factory=dict)


class ImageSettingsPatchRequest(BaseModel):
    image_settings: dict[str, Any] = Field(default_factory=dict)


class ImageGenerateRequest(BaseModel):
    prompt: str | None = None


def create_trpg_play_router(
    app_instance: Any | None = None,
    *,
    get_db_manager: Any | None = None,
    get_user_from_request: Any | None = None,
    require_auth_dependency: Any | None = None,
    config: Any | None = None,
    get_llm_client: Any | None = None,
    play_connection_manager: Any | None = None,
) -> APIRouter:
    if app_instance is not None:
        get_db_manager = get_db_manager or (lambda: app_instance._db_manager)
        get_user_from_request = get_user_from_request or app_instance._get_user_info_from_request
        config = config if config is not None else getattr(app_instance, "config", None)
        get_llm_client = get_llm_client or (lambda: getattr(app_instance, "_llm_client", None))
        play_connection_manager = play_connection_manager or getattr(
            app_instance, "trpg_play_manager", None
        )
        if require_auth_dependency is None:
            from .router_helpers import cookie_auth_dependency

            require_auth_dependency = cookie_auth_dependency(app_instance._enforce_cookie_auth)

    if get_db_manager is None or get_user_from_request is None:
        raise TypeError("get_db_manager と get_user_from_request が必要です")

    if require_auth_dependency is None:
        async def require_auth_dependency(_: Request) -> None:  # type: ignore[no-redef]
            return None

    router = APIRouter(prefix="/api/trpg/sessions", tags=["trpg-play"])

    async def current_user_id(request: Request) -> UUID:
        result = get_user_from_request(request)
        if inspect.isawaitable(result):
            result = await result
        raw = result.get("id") if isinstance(result, Mapping) else result
        if not raw:
            raise HTTPException(status_code=401, detail="ユーザーを特定できません")
        try:
            return UUID(str(raw))
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="ユーザーIDが不正です") from exc

    async def open_session():
        return await get_db_manager().get_session()

    def fail(exc: Exception) -> None:
        if isinstance(exc, TrpgPlayForbidden):
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if isinstance(exc, TrpgPlayConflict):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if isinstance(exc, TrpgPlayError):
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        raise exc

    async def broadcast_events(session_id: str, events: list[dict[str, Any]]) -> None:
        if play_connection_manager is None:
            return
        for event in events:
            await play_connection_manager.broadcast_session(
                session_id,
                {"type": "event", "event": event},
            )

    def schedule_play_image_jobs(service: TrpgPlayService) -> None:
        jobs = service.drain_image_jobs()
        if not jobs:
            return

        async def broadcast_event(session_id: str, event_dict: dict[str, Any]) -> None:
            await broadcast_events(session_id, [event_dict])

        for job in jobs:
            async def _runner(j=job) -> None:
                try:
                    await run_play_image_background(
                        j,
                        config=config,
                        broadcast_event=broadcast_event,
                    )
                except Exception:
                    logger.warning(
                        "TRPG Play 画像バックグラウンド起動に失敗: %s",
                        j.event_id,
                        exc_info=True,
                    )

            asyncio.create_task(_runner())

    @router.post("", dependencies=[Depends(require_auth_dependency)])
    async def create_session(request: Request, payload: SessionCreateRequest):
        user_id = await current_user_id(request)
        db = await open_session()
        try:
            service = TrpgPlayService(
                db,
                get_llm_client() if callable(get_llm_client) else None,
                config=config,
            )
            session_row = await service.create_session(
                user_id=user_id,
                work_id=payload.work_id,
                gm_mode=payload.gm_mode,
                title=payload.title,
            )
            await db.commit()
            return session_row.to_dict()
        except Exception as exc:
            await db.rollback()
            fail(exc)
        finally:
            await db.close()

    @router.get("", dependencies=[Depends(require_auth_dependency)])
    async def list_sessions(request: Request):
        user_id = await current_user_id(request)
        db = await open_session()
        try:
            service = TrpgPlayService(db, config=config)
            sessions = await service.list_sessions(user_id)
            return {"sessions": [item.to_dict() for item in sessions], "count": len(sessions)}
        finally:
            await db.close()

    @router.get("/{session_id}", dependencies=[Depends(require_auth_dependency)])
    async def get_session(session_id: UUID, request: Request):
        user_id = await current_user_id(request)
        db = await open_session()
        try:
            service = TrpgPlayService(db, config=config)
            return await service.get_session_detail(session_id, user_id)
        except Exception as exc:
            fail(exc)
        finally:
            await db.close()

    @router.post("/{session_id}/join", dependencies=[Depends(require_auth_dependency)])
    async def join_session(session_id: UUID, request: Request, payload: JoinRequest):
        user_id = await current_user_id(request)
        db = await open_session()
        try:
            service = TrpgPlayService(db, config=config)
            participant = await service.join_session(
                session_id,
                user_id=user_id,
                invite_code=payload.invite_code,
                display_name=payload.display_name,
                role=payload.role,
                story_character_id=payload.story_character_id,
            )
            await db.commit()
            if play_connection_manager is not None:
                await play_connection_manager.broadcast_session(
                    str(session_id),
                    {"type": "join", "participant": participant.to_dict()},
                )
            return participant.to_dict()
        except Exception as exc:
            await db.rollback()
            fail(exc)
        finally:
            await db.close()

    @router.post("/{session_id}/leave", dependencies=[Depends(require_auth_dependency)])
    async def leave_session(session_id: UUID, request: Request):
        user_id = await current_user_id(request)
        db = await open_session()
        try:
            service = TrpgPlayService(db, config=config)
            participant = await service.leave_session(session_id, user_id)
            participants = await service._participants_for(session_id)
            await db.commit()
            if play_connection_manager is not None:
                await play_connection_manager.broadcast_session(
                    str(session_id),
                    {
                        "type": "leave",
                        "participant_id": str(participant.id),
                        "participants": [item.to_dict() for item in participants],
                    },
                )
                await play_connection_manager.disconnect_participant(
                    str(session_id),
                    str(participant.id),
                )
            return participant.to_dict()
        except Exception as exc:
            await db.rollback()
            fail(exc)
        finally:
            await db.close()

    @router.get("/{session_id}/private-state", dependencies=[Depends(require_auth_dependency)])
    async def get_private_state(session_id: UUID, request: Request):
        user_id = await current_user_id(request)
        db = await open_session()
        try:
            service = TrpgPlayService(db, config=config)
            return await service.get_own_private_state(session_id, user_id)
        except Exception as exc:
            fail(exc)
        finally:
            await db.close()

    @router.patch("/{session_id}/private-state", dependencies=[Depends(require_auth_dependency)])
    async def patch_private_state(
        session_id: UUID,
        request: Request,
        payload: PrivateStatePatchRequest,
    ):
        user_id = await current_user_id(request)
        db = await open_session()
        try:
            service = TrpgPlayService(db, config=config)
            private_state = await service.patch_own_private_state(
                session_id,
                user_id,
                payload.state,
            )
            await db.commit()
            await broadcast_private_state_update(
                play_connection_manager,
                service,
                session_id,
                private_state,
            )
            return private_state
        except Exception as exc:
            await db.rollback()
            fail(exc)
        finally:
            await db.close()

    @router.get("/{session_id}/private-states", dependencies=[Depends(require_auth_dependency)])
    async def list_gm_private_states(session_id: UUID, request: Request):
        user_id = await current_user_id(request)
        db = await open_session()
        try:
            service = TrpgPlayService(db, config=config)
            items = await service.list_gm_visible_private_states(session_id, user_id)
            return {"private_states": items, "count": len(items)}
        except Exception as exc:
            fail(exc)
        finally:
            await db.close()

    @router.post("/{session_id}/start", dependencies=[Depends(require_auth_dependency)])
    async def start_session(session_id: UUID, request: Request):
        user_id = await current_user_id(request)
        db = await open_session()
        try:
            service = TrpgPlayService(
                db,
                get_llm_client() if callable(get_llm_client) else None,
                config=config,
            )
            session_row = await service.start_session(session_id, user_id)
            events = await service.list_events(session_id, user_id, limit=5)
            await db.commit()
            if play_connection_manager is not None:
                await play_connection_manager.broadcast_session(
                    str(session_id),
                    {"type": "snapshot", "session": session_row.to_dict()},
                )
                for event in events:
                    await play_connection_manager.broadcast_session(
                        str(session_id),
                        {"type": "event", "event": event},
                    )
            schedule_play_image_jobs(service)
            return session_row.to_dict()
        except Exception as exc:
            await db.rollback()
            fail(exc)
        finally:
            await db.close()

    @router.post("/{session_id}/end", dependencies=[Depends(require_auth_dependency)])
    async def end_session(session_id: UUID, request: Request):
        user_id = await current_user_id(request)
        db = await open_session()
        try:
            service = TrpgPlayService(db, config=config)
            session_row = await service.end_session(session_id, user_id)
            await db.commit()
            if play_connection_manager is not None:
                await play_connection_manager.broadcast_session(
                    str(session_id),
                    {"type": "ended", "session": session_row.to_dict()},
                )
            return session_row.to_dict()
        except Exception as exc:
            await db.rollback()
            fail(exc)
        finally:
            await db.close()

    @router.post("/{session_id}/actions", dependencies=[Depends(require_auth_dependency)])
    async def post_action(session_id: UUID, request: Request, payload: ActionRequest):
        user_id = await current_user_id(request)
        db = await open_session()
        try:
            service = TrpgPlayService(
                db,
                get_llm_client() if callable(get_llm_client) else None,
                config=config,
            )
            events = await service.post_action(
                session_id,
                user_id,
                kind=payload.kind,
                text=payload.text,
            )
            participants = await service._participants_for(session_id)
            event_dicts = service._event_dicts(events, participants)
            await db.commit()
            await broadcast_events(str(session_id), event_dicts)
            schedule_play_image_jobs(service)
            return {"events": event_dicts}
        except Exception as exc:
            await db.rollback()
            fail(exc)
        finally:
            await db.close()

    @router.post("/{session_id}/dice", dependencies=[Depends(require_auth_dependency)])
    async def roll_dice(session_id: UUID, request: Request, payload: DiceRequest):
        user_id = await current_user_id(request)
        db = await open_session()
        try:
            service = TrpgPlayService(db, config=config)
            event = await service.roll_dice(
                session_id,
                user_id,
                expression=payload.expression,
                note=payload.note,
            )
            participants = await service._participants_for(session_id)
            event_dict = service._event_dicts([event], participants)[0]
            await db.commit()
            await broadcast_events(str(session_id), [event_dict])
            return {"event": event_dict}
        except Exception as exc:
            await db.rollback()
            fail(exc)
        finally:
            await db.close()

    @router.get("/{session_id}/events", dependencies=[Depends(require_auth_dependency)])
    async def list_events(
        session_id: UUID,
        request: Request,
        limit: int = Query(100, ge=1, le=200),
        before_id: UUID | None = None,
    ):
        user_id = await current_user_id(request)
        db = await open_session()
        try:
            service = TrpgPlayService(db, config=config)
            events = await service.list_events(
                session_id,
                user_id,
                limit=limit,
                before_id=before_id,
            )
            return {"events": events, "count": len(events)}
        except Exception as exc:
            fail(exc)
        finally:
            await db.close()

    @router.get("/{session_id}/whispers", dependencies=[Depends(require_auth_dependency)])
    async def list_whispers(session_id: UUID, request: Request):
        user_id = await current_user_id(request)
        db = await open_session()
        try:
            service = TrpgPlayService(db, config=config)
            whispers = await service.list_whispers(session_id, user_id)
            return {"whispers": whispers, "count": len(whispers)}
        except Exception as exc:
            fail(exc)
        finally:
            await db.close()

    @router.post("/{session_id}/whispers", dependencies=[Depends(require_auth_dependency)])
    async def post_whisper(session_id: UUID, request: Request, payload: WhisperCreateRequest):
        user_id = await current_user_id(request)
        db = await open_session()
        try:
            service = TrpgPlayService(db, config=config)
            whisper = await service.post_whisper(
                session_id,
                user_id,
                body=payload.body,
                recipient_participant_ids=payload.recipient_participant_ids,
            )
            whisper_dict = whisper.to_dict(
                recipient_participant_ids=[str(item) for item in payload.recipient_participant_ids]
            )
            await db.commit()
            if play_connection_manager is not None:
                await play_connection_manager.send_whisper(
                    str(session_id),
                    {"type": "whisper", "whisper": whisper_dict},
                    sender_participant_id=str(whisper.sender_participant_id),
                    recipient_participant_ids=[
                        str(item) for item in payload.recipient_participant_ids
                    ],
                )
            return whisper_dict
        except Exception as exc:
            await db.rollback()
            fail(exc)
        finally:
            await db.close()

    @router.patch("/{session_id}/snapshot", dependencies=[Depends(require_auth_dependency)])
    async def patch_snapshot(session_id: UUID, request: Request, payload: SnapshotPatchRequest):
        user_id = await current_user_id(request)
        db = await open_session()
        try:
            service = TrpgPlayService(db, config=config)
            session_row = await service.patch_snapshot(
                session_id,
                user_id,
                payload.snapshot,
            )
            await db.commit()
            session_dict = session_row.to_dict()
            if play_connection_manager is not None:
                await play_connection_manager.broadcast_session(
                    str(session_id),
                    {"type": "snapshot", "session": session_dict},
                )
            return session_dict
        except Exception as exc:
            await db.rollback()
            fail(exc)
        finally:
            await db.close()

    @router.patch("/{session_id}/image-settings", dependencies=[Depends(require_auth_dependency)])
    async def patch_image_settings(
        session_id: UUID,
        request: Request,
        payload: ImageSettingsPatchRequest,
    ):
        user_id = await current_user_id(request)
        db = await open_session()
        try:
            service = TrpgPlayService(db, config=config)
            session_row = await service.patch_image_settings(
                session_id,
                user_id,
                payload.image_settings,
            )
            await db.commit()
            session_dict = session_row.to_dict()
            if play_connection_manager is not None:
                await play_connection_manager.broadcast_session(
                    str(session_id),
                    {"type": "snapshot", "session": session_dict},
                )
            return session_dict
        except Exception as exc:
            await db.rollback()
            fail(exc)
        finally:
            await db.close()

    @router.post("/{session_id}/images/generate", dependencies=[Depends(require_auth_dependency)])
    async def generate_session_image(
        session_id: UUID,
        request: Request,
        payload: ImageGenerateRequest,
    ):
        user_id = await current_user_id(request)
        db = await open_session()
        try:
            service = TrpgPlayService(
                db,
                get_llm_client() if callable(get_llm_client) else None,
                config=config,
            )
            event, media_id = await service.generate_image_manual(
                session_id,
                user_id,
                prompt=payload.prompt,
            )
            participants = await service._participants_for(session_id)
            event_dict = service._event_dicts([event], participants)[0]
            await db.commit()
            await broadcast_events(str(session_id), [event_dict])
            schedule_play_image_jobs(service)
            return {"event": event_dict, "media_id": media_id}
        except Exception as exc:
            await db.rollback()
            fail(exc)
        finally:
            await db.close()

    return router


__all__ = ["broadcast_private_state_update", "create_trpg_play_router"]
