import asyncio

import pytest

from src.assistant.response_handler import ResponseHandler


class AsyncOnlyClient:
    def __init__(self):
        self.called = False

    async def generate_response_async(self, text, **kwargs):
        self.called = True
        await asyncio.sleep(0)
        return f"async:{text}"


class SyncOnlyClient:
    def __init__(self):
        self.called = False

    def generate_response(self, text, **kwargs):
        self.called = True
        return f"sync:{text}"


@pytest.mark.asyncio
async def test_generate_response_only_uses_async_client_without_streaming_hook():
    client = AsyncOnlyClient()
    handler = ResponseHandler(client)

    response = await handler._generate_response_only("task-1", "hello", "web")

    assert response == "async:hello"
    assert client.called is True


@pytest.mark.asyncio
async def test_generate_response_only_offloads_sync_client():
    client = SyncOnlyClient()
    handler = ResponseHandler(client)

    response = await handler._generate_response_only("task-1", "hello", "web")

    assert response == "sync:hello"
    assert client.called is True
