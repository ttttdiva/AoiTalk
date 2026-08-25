"""Audience scoping helpers for WebSocket broadcasts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import Request

    from .server import WebChatServer


async def broadcast_llm_state_change(
    server: "WebChatServer",
    request: "Request",
    message: dict[str, Any],
) -> None:
    """Broadcast global LLM state changes to all connected clients.

    Global LLM mutations are admin-only when auth is enabled, so every client
    should observe the same server-wide runtime state.
    """

    _ = request
    await server.manager.broadcast(message)
