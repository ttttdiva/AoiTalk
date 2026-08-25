"""TRPG Play 実行系 WebSocket 接続管理。"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class TrpgPlayConnectionManager:
  """卓単位の Play WebSocket 接続を管理する。"""

  def __init__(self) -> None:
    self._connections: Dict[WebSocket, dict[str, str]] = {}
    self._by_session: Dict[str, Set[WebSocket]] = {}

  async def connect(
      self,
      websocket: WebSocket,
      *,
      session_id: str,
      user_id: str,
      participant_id: str,
  ) -> None:
    await websocket.accept()
    self._connections[websocket] = {
        "session_id": session_id,
        "user_id": user_id,
        "participant_id": participant_id,
    }
    self._by_session.setdefault(session_id, set()).add(websocket)
    logger.info("TRPG Play WS connected session=%s participant=%s", session_id, participant_id)

  def disconnect(self, websocket: WebSocket) -> None:
    context = self._connections.pop(websocket, None)
    if not context:
      return
    session_id = context.get("session_id")
    if session_id and session_id in self._by_session:
      self._by_session[session_id].discard(websocket)
      if not self._by_session[session_id]:
        del self._by_session[session_id]

  async def disconnect_participant(
      self,
      session_id: str,
      participant_id: str,
      *,
      code: int = 1008,
  ) -> None:
    targets = list(self._by_session.get(session_id, set()))
    for connection in targets:
      context = self._connections.get(connection)
      if not context or context.get("participant_id") != participant_id:
        continue
      try:
        await connection.close(code=code)
      except Exception:
        logger.debug(
            "TRPG Play WS close failed session=%s participant=%s",
            session_id,
            participant_id,
            exc_info=True,
        )
      self.disconnect(connection)

  async def broadcast_session(self, session_id: str, message: dict) -> None:
    targets = list(self._by_session.get(session_id, set()))
    for connection in targets:
      if connection not in self._connections:
        continue
      try:
        await connection.send_json(message)
      except Exception:
        self.disconnect(connection)

  async def send_whisper(
      self,
      session_id: str,
      message: dict,
      *,
      sender_participant_id: str,
      recipient_participant_ids: list[str],
  ) -> None:
    allowed = {sender_participant_id, *recipient_participant_ids}
    await self.send_to_participants(session_id, message, participant_ids=list(allowed))

  async def send_to_participants(
      self,
      session_id: str,
      message: dict,
      *,
      participant_ids: list[str],
  ) -> None:
    allowed = set(participant_ids)
    targets = list(self._by_session.get(session_id, set()))
    for connection in targets:
      if connection not in self._connections:
        continue
      context = self._connections.get(connection)
      if not context:
        continue
      participant_id = context.get("participant_id")
      if participant_id not in allowed:
        continue
      try:
        await connection.send_json(message)
      except Exception:
        self.disconnect(connection)


__all__ = ["TrpgPlayConnectionManager"]
