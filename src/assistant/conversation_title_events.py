"""Helpers for generating and broadcasting web chat session titles."""

from __future__ import annotations

import copy
import inspect
import logging
from typing import Any, Optional

from .chat_turn_persistence import ChatTurnPersistence
from ..services.conversation_title_llm import generate_title_with_llm_client
from ..services.conversation_title_service import ensure_conversation_title

logger = logging.getLogger(__name__)


async def _generate_title_with_turn_client(llm_client, prompt: str) -> Optional[str]:
    """そのターンで答えたクライアントだけで題名を作る。別モデルは作らない。"""

    if llm_client is None:
        return None
    return await generate_title_with_llm_client(llm_client, prompt)


def _provider_model_config_keys(provider: str) -> tuple[str, ...]:
    return {
        "codex-cli": ("codex_cli.model",),
        "claude-cli": ("claude_cli.model",),
        "antigravity-cli": ("antigravity_cli.model",),
        "grok-cli": ("grok_cli.model",),
        "ollama": ("ollama_model", "ollama.model"),
        "sglang": ("sglang_model", "sglang.model"),
        "openai_compatible_local": ("openai_compatible_local.model",),
    }.get(provider, (f"{provider}.model",))


def session_has_explicit_main_route(session: Any) -> bool:
    from ..services.conversation_session_selection import (
        read_session_llm_settings,
        session_main_route_override_for_binding,
    )

    settings = read_session_llm_settings(getattr(session, "context", None))
    route = session_main_route_override_for_binding(settings)
    provider = str((route or {}).get("provider") or "").strip()
    model = str((route or {}).get("model") or "").strip()
    return bool(provider and model and provider != "routing-profile")


async def generate_title_with_session_route(
    config: Any,
    session: Any,
    prompt: str,
) -> Optional[str]:
    """明示されたセッション route のモデルで題名を作る。

    route が無い、またはクライアントを作れないときは None。起動時の既定
    クライアントへは落とさない。
    """

    from ..llm.manager import create_llm_client
    from ..llm.response_model_effort import apply_response_model_effort
    from ..services.conversation_session_selection import (
        read_session_llm_settings,
        session_main_route_override_for_binding,
    )

    if not session_has_explicit_main_route(session):
        return None
    settings = read_session_llm_settings(getattr(session, "context", None))
    route = session_main_route_override_for_binding(settings) or {}
    provider = str(route.get("provider") or "").strip()
    model = str(route.get("model") or "").strip()
    effort = str(route.get("effort") or "").strip()
    try:
        cloned = copy.copy(config)
        cloned.config = copy.deepcopy(config.config)
        cloned.set("llm_provider", provider)
        cloned.set("llm_model", model)
        cloned.set("response_model_selection_active", True)
        for key in _provider_model_config_keys(provider):
            cloned.set(key, model)
        if effort:
            apply_response_model_effort(cloned, provider, model, effort)
        client = create_llm_client(cloned)
    except Exception:
        logger.warning(
            "Session route title client was not created",
            exc_info=True,
        )
        return None
    try:
        return await generate_title_with_llm_client(client, prompt)
    finally:
        cleanup = getattr(client, "cleanup", None)
        if callable(cleanup):
            try:
                result = cleanup()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug("Title client cleanup failed", exc_info=True)


async def maybe_generate_and_broadcast_session_title(
    *,
    web_interface,
    session_id: Optional[str],
    chat_persistence: ChatTurnPersistence,
    config,
    log_prefix: str,
    llm_client=None,
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
            llm_generator=lambda prompt: _generate_title_with_turn_client(
                llm_client, prompt
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
