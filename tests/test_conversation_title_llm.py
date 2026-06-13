import asyncio

import pytest

from src.services.conversation_title_llm import generate_title_with_llm_client


class AsyncTitleClient:
    async def generate_async(self, prompt: str) -> str:
        return f"async:{prompt}"


class SyncTitleClient:
    def generate(self, prompt: str) -> str:
        return f"sync:{prompt}"


class SlowTitleClient:
    async def generate_async(self, prompt: str) -> str:
        await asyncio.sleep(0.05)
        return f"slow:{prompt}"


@pytest.mark.asyncio
async def test_generate_title_with_async_client():
    result = await generate_title_with_llm_client(AsyncTitleClient(), "topic")

    assert result == "async:topic"


@pytest.mark.asyncio
async def test_generate_title_with_sync_client_uses_executor():
    result = await generate_title_with_llm_client(SyncTitleClient(), "topic")

    assert result == "sync:topic"


@pytest.mark.asyncio
async def test_generate_title_with_llm_client_timeout_returns_none():
    result = await generate_title_with_llm_client(
        SlowTitleClient(),
        "topic",
        timeout_seconds=0.001,
    )

    assert result is None
