"""TRPG ルーム用 WebSocket ブロードキャスター

ルームIDごとに接続中の WebSocket を束ね、イベントを全接続へ配信する。
REST API 側のハンドラが状態を変更した後で broadcast を呼び出す。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Set

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class TRPGRoomBroadcaster:
    """ルーム単位の WebSocket ブロードキャスター（シングルトン）"""

    _instance: "TRPGRoomBroadcaster | None" = None

    def __init__(self) -> None:
        self._rooms: Dict[str, Set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def get(cls) -> "TRPGRoomBroadcaster":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def connect(self, room_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._rooms.setdefault(room_id, set()).add(ws)
        logger.info(
            "TRPG WS connect: room=%s, total=%d",
            room_id,
            len(self._rooms[room_id]),
        )

    async def disconnect(self, room_id: str, ws: WebSocket) -> None:
        async with self._lock:
            room = self._rooms.get(room_id)
            if room and ws in room:
                room.discard(ws)
                if not room:
                    self._rooms.pop(room_id, None)
        logger.info("TRPG WS disconnect: room=%s", room_id)

    async def broadcast(self, room_id: str, payload: Dict[str, Any]) -> None:
        """ルームの全 WebSocket に payload を送信する。"""
        targets: Set[WebSocket] = set()
        async with self._lock:
            room = self._rooms.get(room_id)
            if not room:
                return
            targets = set(room)

        text = json.dumps(payload, ensure_ascii=False, default=str)
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_text(text)
            except Exception as e:  # noqa: BLE001
                logger.debug("WS send failed: %s", e)
                dead.append(ws)

        if dead:
            async with self._lock:
                room = self._rooms.get(room_id)
                if room:
                    for ws in dead:
                        room.discard(ws)
                    if not room:
                        self._rooms.pop(room_id, None)

    def room_count(self, room_id: str) -> int:
        return len(self._rooms.get(room_id, set()))


async def _websocket_user_id(app_instance, websocket: WebSocket) -> str | None:
    """Best-effort user-id extraction for TRPG room authorization."""
    if app_instance is None or not getattr(app_instance, "auth_enabled", True):
        return None

    token = websocket.query_params.get("token")
    if token:
        try:
            from .auth_service import get_auth_service

            payload = get_auth_service().verify_token(token)
            if payload and payload.user_id:
                return str(payload.user_id)
        except Exception:
            return None

    cookie_header = websocket.headers.get("cookie")
    next_session = app_instance._get_cookie_from_header(
        cookie_header,
        app_instance.next_cookie_name,
    )
    next_payload = app_instance._decode_next_session_cookie(next_session)
    if next_payload and next_payload.get("sub"):
        return str(next_payload["sub"])

    session_id = app_instance._get_cookie_from_header(
        cookie_header,
        app_instance.cookie_name,
    ) or app_instance._get_cookie_from_header(
        cookie_header,
        app_instance.legacy_cookie_name,
    )
    serializer = app_instance._get_serializer()
    if not serializer or not session_id:
        return None
    try:
        data = serializer.loads(
            session_id,
            max_age=getattr(app_instance, "session_ttl_seconds", 60 * 60 * 24 * 7),
        )
        username = data.get("u")
        if not username:
            return None
        from ..memory.user_repository import UserRepository

        session = await app_instance._db_manager.get_session()
        try:
            user = await UserRepository.get_by_username(session, username)
            return str(user.id) if user else None
        finally:
            await session.close()
    except Exception:
        return None


def register_trpg_ws(app, app_instance=None) -> None:
    """FastAPI アプリに `/ws/trpg/{room_id}` を登録する。"""
    broadcaster = TRPGRoomBroadcaster.get()

    @app.websocket("/ws/trpg/{room_id}")
    async def trpg_room_ws(websocket: WebSocket, room_id: str):
        if app_instance is not None and not app_instance._authorize_websocket(websocket):
            await websocket.close(code=1008)
            return

        invite_code = websocket.query_params.get("invite_code")
        user_id = await _websocket_user_id(app_instance, websocket)
        try:
            from ..services.trpg_play_service import require_room_view_access

            if user_id:
                await require_room_view_access(
                    room_id,
                    user_id,
                    invite_code=invite_code,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("TRPG WS access denied: room=%s, error=%s", room_id, e)
            await websocket.close(code=1008)
            return

        await broadcaster.connect(room_id, websocket)
        try:
            # 初期 state_sync を返す
            try:
                from ..services.trpg_play_service import get_room

                snapshot = await get_room(room_id, log_limit=100)
                await websocket.send_text(
                    json.dumps(
                        {"type": "state_sync", "room": snapshot},
                        ensure_ascii=False,
                        default=str,
                    )
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("state_sync failed: %s", e)

            while True:
                # クライアントからのイベント（ping / request_sync / chat など軽量用途）
                data = await websocket.receive_json()
                msg_type = data.get("type")

                if msg_type == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
                elif msg_type == "request_sync":
                    try:
                        from ..services.trpg_play_service import get_room

                        snapshot = await get_room(room_id, log_limit=200)
                        await websocket.send_text(
                            json.dumps(
                                {"type": "state_sync", "room": snapshot},
                                ensure_ascii=False,
                                default=str,
                            )
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning("request_sync failed: %s", e)
                else:
                    # 未知メッセージは無視
                    logger.debug("unknown trpg ws msg: %s", msg_type)
        except WebSocketDisconnect:
            await broadcaster.disconnect(room_id, websocket)
        except Exception as e:  # noqa: BLE001
            logger.warning("TRPG WS error: %s", e)
            await broadcaster.disconnect(room_id, websocket)
