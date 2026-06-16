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


class ProjectContextClient(AsyncOnlyClient):
    config = {"llm_provider": "openai"}
    current_session_id = "session-1"
    current_project_id = "project-123"

    def _resolve_project_context_sync(self):
        return {"id": "project-123", "name": "Selected Project"}


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


@pytest.mark.asyncio
async def test_generate_response_only_defers_project_fact_reflection(monkeypatch):
    calls = []
    done = asyncio.Event()

    class FakeRunner:
        def __init__(self, config):
            self.config = config

        async def run_async(self, request, project_context=None):
            calls.append((request, project_context, self.config))
            done.set()
            return "案件情報を更新しました。"

    async def noop_dreaming(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "src.llm.tool_policy.looks_like_deferred_project_fact_request",
        lambda text: True,
    )
    monkeypatch.setattr(
        "src.llm.specialist_delegate.ProjectManagementDelegationRunner",
        FakeRunner,
    )

    client = ProjectContextClient()
    handler = ResponseHandler(client)
    monkeypatch.setattr(handler, "_process_dreaming_memory", noop_dreaming)

    response = await handler._generate_response_only(
        "task-1",
        "この案件、納期が8月に遅れるらしい。WBS直して",
        "web",
    )

    await asyncio.wait_for(done.wait(), timeout=1)

    assert response.startswith("async:")
    assert len(calls) == 1
    request, project_context, config = calls[0]
    assert "Deferred project fact reflection" in request
    assert "First call list_project_information" in request
    assert project_context == {"id": "project-123", "name": "Selected Project"}
    assert config == client.config
