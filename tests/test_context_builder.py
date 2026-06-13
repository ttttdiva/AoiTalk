import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest

from src.llm.manager import AgentLLMClient
from src.memory.history import HistoryManager
from src.memory.models import ContextMemory, ProjectContextPack
from src.services.context_builder import ContextBuilder, ContextBundle
from src.services.context_memory_service import ContextMemoryService
from src.services.project_context_pack_service import ProjectContextPackService


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_context_bundle_render_omits_empty_and_respects_limit():
    bundle = ContextBundle(
        memory_context_block="## Dreaming Memory\n- alpha",
        project_context_block="",
        project_pack_block="## Project\n" + ("x" * 200),
        max_chars=80,
    )

    rendered = bundle.render_for_prompt()

    assert "## Dreaming Memory" in rendered
    assert len(rendered) <= 80


def test_project_context_pack_render_pack_dict():
    rendered = ProjectContextPackService.render_pack_dict(
        {
            "summary_md": "MVP context layer",
            "goals": ["Keep prompts compact"],
            "constraints": ["Do not change mobile sync"],
            "current_status": {"phase": "mvp"},
            "decisions": [{"title": "Use scoped table"}],
            "open_questions": ["Extractor timing"],
            "manual_notes": "Operator-edited note",
        }
    )

    assert "## Project Context Pack" in rendered
    assert "MVP context layer" in rendered
    assert "Keep prompts compact" in rendered
    assert "Operator-edited note" in rendered


def test_context_models_to_dict_are_prompt_ready():
    now = datetime.utcnow()
    project_id = uuid.uuid4()
    memory = ContextMemory(
        id=uuid.uuid4(),
        user_id="default_user",
        project_id=project_id,
        scope_type="project",
        scope_id=str(project_id),
        memory_type="decision",
        content="Use ContextBuilder for prompt injection",
        structured_data={"source": "test"},
        source_type="manual",
        confidence=1.0,
        importance=8,
        status="active",
        is_pinned=True,
        created_at=now,
        updated_at=now,
    )
    pack = ProjectContextPack(
        id=uuid.uuid4(),
        project_id=project_id,
        summary_md="summary",
        goals=["goal"],
        constraints=[],
        current_status={},
        active_task_snapshot=[],
        decisions=[],
        open_questions=[],
        manual_notes="",
        generated_from={},
        created_at=now,
        updated_at=now,
    )

    assert memory.to_dict()["user_id"] == "default_user"
    assert memory.to_dict()["project_id"] == str(project_id)
    assert pack.to_dict()["goals"] == ["goal"]


class _FakeResolver:
    async def resolve_context(self, **kwargs):
        return {
            "id": kwargs.get("project_id"),
            "name": "AoiTalk",
            "slug": "aoitalk",
        }


class _FakePackService:
    async def render_project_context_pack_for_prompt(self, project_id):
        return "## Project Context Pack\n- Summary: compact project state"


class _FakeContextMemoryService:
    async def get_memories_for_context(self, **kwargs):
        return [
            {
                "memory_type": "decision",
                "title": "Architecture",
                "content": "Use ContextBuilder for prompt injection",
                "importance": 9,
                "is_pinned": True,
            },
            {
                "memory_type": "fact",
                "content": "Already in Dreaming memory",
                "importance": 5,
                "is_pinned": False,
            },
        ]

    render_memories_for_prompt = staticmethod(
        ContextMemoryService.render_memories_for_prompt
    )


@pytest.mark.anyio
async def test_context_builder_combines_existing_project_pack_and_scoped_memories(
    monkeypatch,
):
    builder = ContextBuilder(
        context_memory_service=_FakeContextMemoryService(),
        project_context_pack_service=_FakePackService(),
        project_context_resolver=_FakeResolver(),
    )

    async def no_task_context(**kwargs):
        return ""

    async def no_session_context(session_id):
        return ""

    monkeypatch.setattr(builder, "_build_task_context_block", no_task_context)
    monkeypatch.setattr(builder, "_build_session_context_block", no_session_context)

    bundle = await builder.build_context(
        user_id="default_user",
        message="architecture",
        project_id=str(uuid.uuid4()),
    )
    rendered = bundle.render_for_prompt()

    assert "Selected project context:" in rendered
    assert "compact project state" in rendered
    assert "Use ContextBuilder for prompt injection" in rendered
    assert "Already in Dreaming memory" in rendered
    assert rendered.count("Already in Dreaming memory") == 1


@pytest.mark.anyio
async def test_context_builder_can_skip_project_prompt_context(monkeypatch):
    class _FailingResolver:
        async def resolve_context(self, **kwargs):
            raise AssertionError("project resolver should not run")

    class _FailingPackService:
        async def render_project_context_pack_for_prompt(self, project_id):
            raise AssertionError("project pack should not render")

    class _CapturingMemoryService:
        def __init__(self):
            self.kwargs = None

        async def get_memories_for_context(self, **kwargs):
            self.kwargs = kwargs
            return [
                {"content": "Keep this", "memory_type": "preference"},
                {"content": "General relevant memory", "memory_type": "fact"},
            ]

        render_memories_for_prompt = staticmethod(
            ContextMemoryService.render_memories_for_prompt
        )

    memory_service = _CapturingMemoryService()
    builder = ContextBuilder(
        context_memory_service=memory_service,
        project_context_pack_service=_FailingPackService(),
        project_context_resolver=_FailingResolver(),
    )

    async def no_task_context(**kwargs):
        return ""

    async def no_session_context(session_id):
        return ""

    monkeypatch.setattr(builder, "_build_task_context_block", no_task_context)
    monkeypatch.setattr(builder, "_build_session_context_block", no_session_context)

    bundle = await builder.build_context(
        user_id="default_user",
        message="architecture",
        project_id=str(uuid.uuid4()),
        include_project_context=False,
    )
    rendered = bundle.render_for_prompt()

    assert "Keep this" in rendered
    assert "General relevant memory" in rendered
    assert "Selected project context:" not in rendered
    assert "Project Context Pack" not in rendered
    assert memory_service.kwargs["project_id"] is None


def test_agent_llm_conversation_context_prefers_context_bundle(monkeypatch):
    client = AgentLLMClient.__new__(AgentLLMClient)
    client.current_session_id = None
    client.character_name = None
    client._current_context_bundle = ContextBundle(
        memory_context_block="## Runtime Context\n- context-builder block"
    )

    class _History:
        context_window_size = 10

        def get_all(self):
            return [{"role": "user", "content": "current question"}]

    client.history_manager = _History()
    monkeypatch.setattr(client, "_get_scenario_chat_context_sync", lambda: None)

    rendered = AgentLLMClient._build_conversation_context(client)

    assert "context-builder block" in rendered


@pytest.mark.anyio
async def test_agent_llm_loads_persisted_session_history_before_prompt(monkeypatch):
    client = AgentLLMClient.__new__(AgentLLMClient)
    client.current_session_id = "session-1"
    client._loaded_history_session_id = None
    client._memory_enabled = True
    client.history_manager = HistoryManager()
    client.character_name = None
    client._current_context_bundle = None

    class _Repo:
        async def get_session_messages(self, session_id):
            assert session_id == "session-1"
            return [
                SimpleNamespace(role="user", content="アクアマリンの硬度は？"),
                SimpleNamespace(role="assistant", content="7.5〜8.0です。"),
            ]

    class _MemoryManager:
        repository = _Repo()

        def is_initialized(self):
            return True

    client.memory_manager = _MemoryManager()
    monkeypatch.setattr(client, "_get_scenario_chat_context_sync", lambda: None)

    await AgentLLMClient._sync_history_with_current_session(client)
    client.history_manager.add_message("user", "ダイヤモンドは？")

    rendered = AgentLLMClient._build_conversation_context(client)

    assert "アクアマリンの硬度は？" in rendered
    assert "7.5〜8.0です。" in rendered
    assert "現在の質問: ダイヤモンドは？" in rendered
