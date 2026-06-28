"""LLM adapter for best-effort conversation title generation."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def generate_title_with_llm_client(
    llm_client: Any,
    prompt: str,
    *,
    timeout_seconds: float = 20.0,
) -> Optional[str]:
    """Generate a title with an already-initialized LLM client.

    Title generation is an optional enhancement. It must not create another
    runtime client or block the FastAPI event loop during WebUI startup.
    """
    if llm_client is None:
        return None

    async def _generate() -> Optional[str]:
        async_title_generate = getattr(llm_client, "generate_title_async", None)
        if callable(async_title_generate):
            response = await async_title_generate(prompt)
            return str(response) if response else None

        sync_title_generate = getattr(llm_client, "generate_title", None)
        if callable(sync_title_generate):
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: sync_title_generate(prompt),
            )
            return str(response) if response else None

        if hasattr(llm_client, "generate_async"):
            response = await llm_client.generate_async(prompt)
            return str(response) if response else None

        if hasattr(llm_client, "generate_response_async"):
            response = await llm_client.generate_response_async(prompt)
            return str(response) if response else None

        sync_generate = getattr(llm_client, "generate", None)
        if not callable(sync_generate):
            sync_generate = getattr(llm_client, "generate_response", None)
        if not callable(sync_generate):
            return None

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, lambda: sync_generate(prompt))
        return str(response) if response else None

    try:
        return await asyncio.wait_for(_generate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        logger.warning("LLM title generation timed out after %.1f seconds", timeout_seconds)
    except Exception as exc:
        logger.warning("LLM title generation failed: %s", exc)
    return None
