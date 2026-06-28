"""WebSocket 接続管理 (server.py から移設)"""

import logging
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections with per-user session support"""

    def __init__(self) -> None:
        self.active_connections: Set[WebSocket] = set()
        self.connection_contexts: Dict[WebSocket, Dict[str, Optional[str]]] = {}
        self._latest_session_id: Optional[str] = None
        # Legacy shared chat history (for backward compatibility when auth disabled)
        self.chat_history: List[dict] = []
        self.max_history = 100

        # Per-user sessions for multi-user support
        # Maps user_id -> WebSession (when WEB_SESSION_AVAILABLE)
        self.user_sessions: Dict[str, Any] = {}

    async def connect(
        self,
        websocket: WebSocket,
        *,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None:
        """Accept new WebSocket connection"""
        await websocket.accept()
        self.active_connections.add(websocket)
        self.connection_contexts[websocket] = {
            "user_id": user_id,
            "session_id": session_id,
        }
        if session_id:
            self._latest_session_id = session_id
        logger.info(
            f"Client connected. Total connections: {len(self.active_connections)}"
        )

        # Send chat history to new client
        await websocket.send_json({"type": "chat_history", "data": self.chat_history})

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove WebSocket connection"""
        context = self.connection_contexts.get(websocket, {})
        self.active_connections.discard(websocket)
        self.connection_contexts.pop(websocket, None)
        if context.get("session_id") == self._latest_session_id:
            self._latest_session_id = self._resolve_any_active_session_id()
        logger.info(
            f"Client disconnected. Total connections: {len(self.active_connections)}"
        )

    def _resolve_any_active_session_context(self) -> Optional[Dict[str, Optional[str]]]:
        for context in self.connection_contexts.values():
            session_id = context.get("session_id")
            if session_id:
                return dict(context)
        return None

    def _resolve_any_active_session_id(self) -> Optional[str]:
        context = self._resolve_any_active_session_context()
        return context.get("session_id") if context else None

    def get_latest_session_context(self) -> Optional[Dict[str, Optional[str]]]:
        """Return context for the most recent connected chat session."""
        if self._latest_session_id:
            for context in self.connection_contexts.values():
                if context.get("session_id") == self._latest_session_id:
                    return dict(context)
            self._latest_session_id = None

        context = self._resolve_any_active_session_context()
        self._latest_session_id = context.get("session_id") if context else None
        return context

    def get_latest_session_id(self) -> Optional[str]:
        """Return the most recent connected chat session, if it is still active."""
        context = self.get_latest_session_context()
        return context.get("session_id") if context else None

    async def broadcast(
        self,
        message: dict,
        *,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        """Send message to matching connected clients."""
        target_session_id = session_id
        if target_session_id is None:
            target_session_id = message.get("session_id")
        if target_session_id is None and isinstance(message.get("data"), dict):
            target_session_id = message["data"].get("session_id")
        disconnected = []
        for connection in self.active_connections:
            context = self.connection_contexts.get(connection, {})
            if target_session_id and context.get("session_id") != target_session_id:
                continue
            if user_id and context.get("user_id") != user_id:
                continue
            try:
                await connection.send_json(message)
            except (OSError, ConnectionError) as e:
                logger.info(f"Client connection lost during broadcast: {e}")
                disconnected.append(connection)
            except Exception as e:
                logger.error(f"Error sending message: {e}")
                disconnected.append(connection)

        # Remove disconnected clients
        for conn in disconnected:
            self.disconnect(conn)

    def add_to_history(self, entry: dict) -> None:
        """Add message to chat history"""
        self.chat_history.append(entry)
        if len(self.chat_history) > self.max_history:
            self.chat_history = self.chat_history[-self.max_history :]

    def clear_history(self) -> None:
        """Clear chat history"""
        self.chat_history.clear()
