"""LLM adapter for best-effort conversation title generation."""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any, Optional

from .ephemeral_llm import generate_ephemeral_text_with_llm_client

logger = logging.getLogger(__name__)


_TITLE_OBSERVATION_STATE_FACTORIES: dict[str, Any] = {
    "_last_generation_metrics": lambda: None,
    "_last_context_snapshots": list,
    "_context_request_index": lambda: 0,
    "_last_tool_calls": list,
    "_last_audit_tool_calls": list,
    "_last_agentic_events": list,
    "_last_model_transcript": list,
    "_model_transcript": list,
    "_last_usage": dict,
    "_last_usage_records": list,
    "_last_tool_loop_messages": list,
    "_last_tool_loop_completion_confirmed": lambda: False,
    "_last_turn_tool_rounds_exhausted": lambda: False,
    "_last_turn_tool_loop_failed": lambda: False,
    "_current_context_bundle": lambda: None,
    "_current_context_budget": lambda: None,
    "_current_tool_hint_context": lambda: "",
    "_current_turn_system_content": lambda: "",
}


def _title_generation_client(llm_client: Any) -> Any:
    """Return a title-only view without sharing turn observation state."""

    try:
        scoped = copy.copy(llm_client)
    except Exception:
        return llm_client
    if scoped is llm_client:
        return llm_client

    for name, factory in _TITLE_OBSERVATION_STATE_FACTORIES.items():
        try:
            if hasattr(scoped, name):
                setattr(scoped, name, factory())
        except Exception:
            logger.debug("Unable to isolate title client field %s", name, exc_info=True)

    gateway = getattr(scoped, "_privacy_gateway", None)
    if gateway is not None:
        try:
            gateway_copy = copy.copy(gateway)
            for name, factory in {
                "_raw_to_alias": dict,
                "_alias_to_raw": dict,
                "_counters": dict,
                "audit": list,
            }.items():
                if hasattr(gateway_copy, name):
                    setattr(gateway_copy, name, factory())
            scoped._privacy_gateway = gateway_copy
        except Exception:
            logger.debug("Unable to isolate title privacy gateway", exc_info=True)

    return scoped


async def generate_title_with_llm_client(
    llm_client: Any,
    prompt: str,
    *,
    timeout_seconds: float = 20.0,
) -> Optional[str]:
    """Generate a title with an already-initialized LLM client.

    Title generation is an optional enhancement. It must not create another
    runtime client, persist a chat turn, or block the FastAPI event loop during
    WebUI startup.
    """
    if llm_client is None:
        return None

    if callable(getattr(llm_client, "is_runtime_known_unavailable", None)):
        if llm_client.is_runtime_known_unavailable():
            return None

    async def _generate() -> Optional[str]:
        title_client = _title_generation_client(llm_client)
        async_title_generate = getattr(title_client, "generate_title_async", None)
        if callable(async_title_generate):
            response = await async_title_generate(prompt)
            return str(response) if response else None

        sync_title_generate = getattr(title_client, "generate_title", None)
        if callable(sync_title_generate):
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: sync_title_generate(prompt),
            )
            return str(response) if response else None

        # 通常の generate_async / generate_response_async には履歴保存の
        # 副作用があるため、明示的に副作用なしと契約したAPIだけを使う。
        return await generate_ephemeral_text_with_llm_client(
            title_client,
            prompt,
            timeout_seconds=None,
        )

    try:
        return await asyncio.wait_for(_generate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        logger.warning("LLM title generation timed out after %.1f seconds", timeout_seconds)
    except Exception as exc:
        logger.warning("LLM title generation failed: %s", exc)
    return None
