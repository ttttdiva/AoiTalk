"""TRPG Play 用 WebSocket（/ws/play/{session_id}）。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


def register_trpg_play_websocket_routes(app: FastAPI, server: "WebChatServer") -> None:
    manager = getattr(server, "trpg_play_manager", None)
    if manager is None:
        logger.warning("TRPG Play connection manager is not configured")
        return

    @app.websocket("/ws/play/{session_id}")
    async def trpg_play_websocket(websocket: WebSocket, session_id: str):
        auth_enabled = getattr(server, "auth_enabled", None)
        if auth_enabled is not True and auth_enabled is not False:
            await websocket.close(code=1011)
            return
        if not await server._authorize_websocket(websocket):
            await websocket.close(code=1008)
            return

        ws_user_info = await server._get_user_info_from_websocket(websocket)
        if server.auth_enabled is not False and not ws_user_info:
            await websocket.close(code=1008)
            return

        user_id = str(ws_user_info.get("id") if ws_user_info else "default_user")
        try:
            session_uuid = UUID(str(session_id))
        except ValueError:
            await websocket.close(code=1008)
            return

        db = await server._db_manager.get_session()
        participant_id: str | None = None
        try:
            from ...services.trpg_play_service import TrpgPlayForbidden, TrpgPlayService

            service = TrpgPlayService(db, config=getattr(server, "config", None))
            participant = await service._participant_for_user(session_uuid, UUID(user_id))
            if participant is None:
                await websocket.close(code=1008)
                return
            participant_id = str(participant.id)
        except TrpgPlayForbidden:
            await websocket.close(code=1008)
            return
        except Exception:
            logger.exception("TRPG Play WS 認可に失敗しました")
            await websocket.close(code=1011)
            return
        finally:
            await db.close()

        if participant_id is None:
            await websocket.close(code=1008)
            return

        await manager.connect(
            websocket,
            session_id=str(session_uuid),
            user_id=user_id,
            participant_id=participant_id,
        )

        try:
            while True:
                data = await websocket.receive_json()
                message_type = str(data.get("type") or "").strip().lower()
                if message_type == "ping":
                    await websocket.send_json({"type": "pong"})
                elif message_type == "request_sync":
                    sync_db = await server._db_manager.get_session()
                    try:
                        from ...services.trpg_play_service import TrpgPlayService

                        sync_service = TrpgPlayService(
                            sync_db, config=getattr(server, "config", None)
                        )
                        detail = await sync_service.get_session_detail(
                            session_uuid, UUID(user_id)
                        )
                        await websocket.send_json({"type": "sync", "session": detail})
                    finally:
                        await sync_db.close()
                else:
                    logger.debug("Unknown TRPG Play WS message: %s", message_type)
        except WebSocketDisconnect:
            manager.disconnect(websocket)
        except Exception:
            logger.exception("TRPG Play WS error")
            manager.disconnect(websocket)
