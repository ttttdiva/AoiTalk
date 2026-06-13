"""Helpers for generating and broadcasting web chat session titles."""

from __future__ import annotations

import inspect
from typing import Optional

from .chat_turn_persistence import ChatTurnPersistence
from ..services.conversation_title_llm import generate_title_with_llm_client
from ..services.conversation_title_service import ensure_conversation_title


async def _generate_title_with_web_llm(web_interface, prompt: str) -> Optional[str]:
    server = getattr(web_interface, "server", None)
    llm_client = getattr(server, "_llm_client", None)
    return await generate_title_with_llm_client(llm_client, prompt)


async def maybe_generate_and_broadcast_session_title(
    *,
    web_interface,
    session_id: Optional[str],
    chat_persistence: ChatTurnPersistence,
    config,
    log_prefix: str,
) -> None:
    if not web_interface or not session_id:
        return
    repo = getattr(chat_persistence.memory_manager, "repository", None)
    if not repo or not all(
        hasattr(repo, name)
        for name in ("get_session_by_id", "get_session_messages", "update_session_title")
    ):
        return

    try:
        generated = await ensure_conversation_title(
            repo=repo,
            session_id=session_id,
            llm_generator=lambda prompt: _generate_title_with_web_llm(
                web_interface, prompt
            ),
        )
        if not generated:
            return

        broadcaster = getattr(web_interface, "broadcast_stream_event", None)
        if not broadcaster:
            return
        result = broadcaster(
            "conversation_title_updated",
            {
                "session_id": session_id,
                "title": generated.title,
                "source": generated.source,
            },
        )
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        print(f"[{log_prefix}] セッションタイトル生成エラー: {exc}")
