from types import SimpleNamespace

from src.llm.openai_compatible_local_engine import (
    LOCAL_MODEL_LOADING_RESPONSE,
    OpenAICompatibleLocalClient,
    create_openai_compatible_local_client,
)
from src.tools.core import tool
from src.tools.registry import ToolRegistry


class DummyConfig:
    default_character = "zundamon"

    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        value = self.values
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


class EmptyRegistry:
    def __len__(self):
        return 0

    def get_all(self):
        return []


def _patch_common(monkeypatch, calls):
    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="OK", tool_calls=None)
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, base_url, api_key):
            self.base_url = base_url
            self.api_key = api_key
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("src.llm.openai_compatible_local_engine.OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        "src.llm.openai_compatible_local_engine.build_runtime_tool_registry",
        lambda config: EmptyRegistry(),
    )
    monkeypatch.setattr(
        "src.llm.openai_compatible_local_engine.build_unified_instructions",
        lambda **kwargs: "system prompt",
    )
    monkeypatch.setattr(
        "src.llm.openai_compatible_local_engine.get_user_custom_instructions_sync",
        lambda user_id: "",
    )


def _patch_loading_model_client(monkeypatch, calls):
    class LoadingModelError(Exception):
        status_code = 503
        body = {
            "error": {
                "message": "Loading model",
                "type": "unavailable_error",
                "code": 503,
            }
        }

        def __str__(self):
            return "Error code: 503 - {'error': {'message': 'Loading model'}}"

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            raise LoadingModelError()

    class FakeOpenAI:
        def __init__(self, base_url, api_key):
            self.base_url = base_url
            self.api_key = api_key
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("src.llm.openai_compatible_local_engine.OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        "src.llm.openai_compatible_local_engine.build_unified_instructions",
        lambda **kwargs: "system prompt",
    )
    monkeypatch.setattr(
        "src.llm.openai_compatible_local_engine.get_user_custom_instructions_sync",
        lambda user_id: "",
    )


def test_local_provider_omits_optional_openai_features_by_default(monkeypatch):
    calls = []
    _patch_common(monkeypatch, calls)

    client = OpenAICompatibleLocalClient(
        base_url="http://127.0.0.1:8080",
        model="qwen3.6-27b-dflash",
        api_key="dummy",
        config=None,
    )

    assert client.base_url == "http://127.0.0.1:8080/v1"
    assert client.generate_response("ping") == "OK"
    assert calls[0]["model"] == "qwen3.6-27b-dflash"
    assert "tools" not in calls[0]
    assert "tool_choice" not in calls[0]
    assert "response_format" not in calls[0]
    assert "extra_body" not in calls[0]


def test_create_local_provider_uses_nested_config(monkeypatch):
    calls = []
    _patch_common(monkeypatch, calls)
    config = DummyConfig(
        {
            "default_character": "zundamon",
            "llm_model": "qwen3.6-27b-dflash",
            "openai_compatible_local": {
                "base_url": "http://localhost:8080/v1",
                "api_key": "local-key",
                "enable_tools": False,
            },
        }
    )

    client = create_openai_compatible_local_client(config)

    assert client.base_url == "http://localhost:8080/v1"
    assert client.model_name == "qwen3.6-27b-dflash"
    assert client.api_key == "local-key"
    assert client.enable_tools is False


def test_local_provider_advertises_runtime_tools_without_provider_toggle(
    monkeypatch,
):
    calls = []
    registry = ToolRegistry()

    @tool
    def advanced_reasoning_assistant(request: str, redacted_request: str = "") -> str:
        """Delegate to an external model."""
        return "delegated"

    @tool
    def utility_assistant(request: str) -> str:
        """Delegate utility work."""
        return "utility"

    registry.register(advanced_reasoning_assistant)
    registry.register(utility_assistant)

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="OK", tool_calls=None)
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, base_url, api_key):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("src.llm.openai_compatible_local_engine.OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        "src.llm.openai_compatible_local_engine.build_runtime_tool_registry",
        lambda config: registry,
    )
    monkeypatch.setattr(
        "src.llm.openai_compatible_local_engine.build_unified_instructions",
        lambda **kwargs: "system prompt",
    )
    monkeypatch.setattr(
        "src.llm.openai_compatible_local_engine.get_user_custom_instructions_sync",
        lambda user_id: "",
    )

    client = OpenAICompatibleLocalClient(
        base_url="http://127.0.0.1:8080",
        model="qwopus3.6-35b-a3b",
        api_key="dummy",
        config={"use_tools": True, "model_sharing": {"enabled": True}},
        enable_tools=False,
    )

    assert client.generate_response("delegate this") == "OK"
    assert client.enable_tools is False
    assert [tool["function"]["name"] for tool in calls[0]["tools"]] == [
        "advanced_reasoning_assistant",
        "utility_assistant",
    ]
    assert calls[0]["tool_choice"] == "auto"


def test_local_provider_keeps_tools_omitted_when_global_tools_disabled(monkeypatch):
    calls = []
    registry = ToolRegistry()

    @tool
    def advanced_reasoning_assistant(request: str, redacted_request: str = "") -> str:
        """Delegate to an external model."""
        return "delegated"

    registry.register(advanced_reasoning_assistant)

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="OK", tool_calls=None)
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, base_url, api_key):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("src.llm.openai_compatible_local_engine.OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        "src.llm.openai_compatible_local_engine.build_runtime_tool_registry",
        lambda config: registry,
    )
    monkeypatch.setattr(
        "src.llm.openai_compatible_local_engine.build_unified_instructions",
        lambda **kwargs: "system prompt",
    )
    monkeypatch.setattr(
        "src.llm.openai_compatible_local_engine.get_user_custom_instructions_sync",
        lambda user_id: "",
    )

    client = OpenAICompatibleLocalClient(
        base_url="http://127.0.0.1:8080",
        model="qwopus3.6-35b-a3b",
        api_key="dummy",
        config=DummyConfig(
            {"use_tools": False, "model_sharing": {"enabled": True}}
        ),
        enable_tools=False,
    )

    assert client.generate_response("delegate this") == "OK"
    assert "tools" not in calls[0]
    assert "tool_choice" not in calls[0]


def test_loading_model_503_returns_readiness_message_without_compat_retry(monkeypatch):
    calls = []
    _patch_loading_model_client(monkeypatch, calls)

    client = OpenAICompatibleLocalClient(
        base_url="http://127.0.0.1:8080",
        model="qwopus3.6-35b-a3b",
        api_key="dummy",
        config=None,
        enable_response_format=True,
    )

    assert client.generate_response("ping") == LOCAL_MODEL_LOADING_RESPONSE
    assert len(calls) == 1
    assert calls[0]["response_format"] == {"type": "json_object"}


def test_loading_model_503_stream_returns_readiness_message(monkeypatch):
    calls = []
    _patch_loading_model_client(monkeypatch, calls)

    client = OpenAICompatibleLocalClient(
        base_url="http://127.0.0.1:8080",
        model="qwopus3.6-35b-a3b",
        api_key="dummy",
        config=None,
    )

    assert (
        "".join(client.generate_response("ping", stream=True))
        == LOCAL_MODEL_LOADING_RESPONSE
    )


def test_qwopus_fast_mode_disables_thinking(monkeypatch):
    calls = []
    _patch_common(monkeypatch, calls)

    client = OpenAICompatibleLocalClient(
        base_url="http://127.0.0.1:8080",
        model="qwopus3.6-35b-a3b",
        api_key="dummy",
        config=None,
    )
    client.set_llm_mode("fast")

    assert client.generate_response("ping") == "OK"
    assert calls[0]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_qwopus_thinking_empty_content_retries_without_thinking(monkeypatch):
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content="",
                                reasoning_content="thinking only",
                                tool_calls=None,
                            )
                        )
                    ]
                )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="</think>\n\nfinal", tool_calls=None)
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, base_url, api_key):
            self.base_url = base_url
            self.api_key = api_key
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("src.llm.openai_compatible_local_engine.OpenAI", FakeOpenAI)

    client = OpenAICompatibleLocalClient(
        base_url="http://127.0.0.1:8080",
        model="qwopus3.6-35b-a3b",
        api_key="dummy",
        config=None,
    )
    client.set_llm_mode("thinking")

    assert client.generate_response("ping") == "final"
    assert calls[0]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
    assert calls[1]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_local_provider_injects_required_project_delegation_before_parent_answer(monkeypatch):
    calls = []
    delegated_requests = []
    registry = ToolRegistry()

    @tool
    def project_management_assistant(request: str) -> str:
        """Delegate project management work."""
        delegated_requests.append(request)
        return "タスク操作を完了しました。\n- task_id: task-1\n- project_id: project-123"

    registry.register(project_management_assistant)

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="parent answer with task_id: task-1",
                            tool_calls=None,
                        )
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, base_url, api_key):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("src.llm.openai_compatible_local_engine.OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        "src.llm.openai_compatible_local_engine.build_runtime_tool_registry",
        lambda config: registry,
    )
    monkeypatch.setattr(
        "src.llm.openai_compatible_local_engine.build_unified_instructions",
        lambda **kwargs: "system prompt",
    )
    monkeypatch.setattr(
        "src.llm.openai_compatible_local_engine.get_user_custom_instructions_sync",
        lambda user_id: "",
    )

    client = OpenAICompatibleLocalClient(
        base_url="http://127.0.0.1:8080",
        model="qwopus3.6-35b-a3b",
        api_key="dummy",
        config=DummyConfig({"use_tools": True}),
        enable_tools=False,
    )

    response = client.generate_response("予約をタスクとして追加して")

    assert response == "parent answer with task_id: task-1"
    assert len(delegated_requests) == 1
    assert "Use the built-in project management tools" in delegated_requests[0]
    assert len(calls) == 1
    parent_user_message = calls[0]["messages"][-1]["content"]
    assert "Required Project Management Delegation Result" in parent_user_message
    assert "task_id: task-1" in parent_user_message
    assert "Current user request:" in parent_user_message


def test_local_provider_requires_utility_delegation_without_native_tools(monkeypatch):
    calls = []
    delegated_requests = []
    registry = ToolRegistry()

    @tool
    def utility_assistant(request: str) -> str:
        """Delegate utility work."""
        delegated_requests.append(request)
        return "2026-06-09 12:34 JST"

    registry.register(utility_assistant)

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="It is 2026-06-09 12:34 JST.",
                            tool_calls=None,
                        )
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, base_url, api_key):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("src.llm.openai_compatible_local_engine.OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        "src.llm.openai_compatible_local_engine.build_runtime_tool_registry",
        lambda config: registry,
    )
    monkeypatch.setattr(
        "src.llm.openai_compatible_local_engine.build_unified_instructions",
        lambda **kwargs: "system prompt",
    )
    monkeypatch.setattr(
        "src.llm.openai_compatible_local_engine.get_user_custom_instructions_sync",
        lambda user_id: "",
    )

    client = OpenAICompatibleLocalClient(
        base_url="http://127.0.0.1:8080",
        model="qwen3.6-27b-dflash",
        api_key="dummy",
        config=DummyConfig({"use_tools": True}),
        enable_tools=False,
    )

    response = client.generate_response("\u4eca\u306f\u4f55\u6642\uff1f")

    assert response == "It is 2026-06-09 12:34 JST."
    assert len(delegated_requests) == 1
    assert "Use the utility specialist tools" in delegated_requests[0]
    assert len(calls) == 1
    assert [tool["function"]["name"] for tool in calls[0]["tools"]] == [
        "utility_assistant"
    ]
    parent_user_message = calls[0]["messages"][-1]["content"]
    assert "Required Utility Delegation Result" in parent_user_message
    assert "2026-06-09 12:34 JST" in parent_user_message
