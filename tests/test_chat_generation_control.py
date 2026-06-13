import asyncio
import contextlib

import pytest

from src.api.server import WebChatServer


def make_server():
    server = WebChatServer.__new__(WebChatServer)
    server._conversation_generation_tasks = {}
    server._conversation_steering_queues = {}
    return server


@pytest.mark.asyncio
async def test_stop_generation_cancels_session_task():
    server = make_server()
    events = []

    async def broadcast_stream_event(event_type, data):
        events.append((event_type, data))

    server.broadcast_stream_event = broadcast_stream_event
    task = asyncio.create_task(asyncio.sleep(60))
    server._register_conversation_generation_task("session-1", task)

    result = await server._handle_stop_generation({"session_id": "session-1"})

    assert result == {"session_id": "session-1", "cancelled": 1}
    assert events == [
        (
            "stream_cancelled",
            {
                "session_id": "session-1",
                "status": "cancelled",
                "message": "応答生成を停止しました",
                "cancelled": 1,
            },
        )
    ]
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.asyncio
async def test_steer_generation_queues_and_consumes_instruction():
    server = make_server()
    events = []

    async def broadcast_stream_event(event_type, data):
        events.append((event_type, data))

    server.broadcast_stream_event = broadcast_stream_event

    result = await server._handle_steer_generation(
        {"session_id": "session-1", "message": "結論を短くして"}
    )

    assert result == {"session_id": "session-1", "queued": True}
    assert server.consume_generation_steering("session-1") == ["結論を短くして"]
    assert server.consume_generation_steering("session-1") == []
    assert events == [
        (
            "steering_update",
            {
                "session_id": "session-1",
                "status": "queued",
                "message": "追加指示を受け取りました",
            },
        )
    ]
