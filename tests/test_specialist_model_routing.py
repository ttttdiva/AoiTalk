import asyncio

from src.llm.specialist_delegate import (
    FilesystemDelegationRunner,
    SearchDelegationRunner,
    UtilityDelegationRunner,
)


def _base_config() -> dict:
    return {
        "llm_provider": "openai_compatible_local",
        "llm_model": "local-main-model",
        "agents": {
            "provider": "gemini",
            "model": "gemini-3-flash-preview",
            "search": {"enabled": True},
            "filesystem": {"enabled": True},
            "utility": {"enabled": True},
        },
        "search": {
            "provider": "openai",
            "x_enabled": False,
            "grok_x_enabled": False,
            "knowledge_enabled": False,
        },
        "memory": {"enabled": True, "enable_search": False},
        "model_sharing": {
            "enabled": False,
            "provider": "openai",
            "model": "gpt-4o",
        },
    }


def test_model_sharing_off_all_specialists_inherit_main_model():
    config = _base_config()

    search = SearchDelegationRunner(config)
    filesystem = FilesystemDelegationRunner(config)
    utility = UtilityDelegationRunner(config)

    assert search.provider == "openai_compatible_local"
    assert search.model == "local-main-model"
    assert filesystem.provider == "openai_compatible_local"
    assert filesystem.model == "local-main-model"
    assert utility.provider == "openai_compatible_local"
    assert utility.model == "local-main-model"


def test_model_sharing_on_openai_search_uses_external_model():
    config = _base_config()
    config["model_sharing"]["enabled"] = True

    search = SearchDelegationRunner(config)

    assert search.provider == "openai"
    assert search.model == "gpt-4o"


def test_model_sharing_on_local_search_does_not_use_external_model():
    config = _base_config()
    config["model_sharing"]["enabled"] = True
    config["search"]["provider"] = "local"

    search = SearchDelegationRunner(config)

    assert search.provider == "openai_compatible_local"


def test_model_sharing_on_filesystem_and_utility_still_inherit_main_model():
    config = _base_config()
    config["model_sharing"]["enabled"] = True

    filesystem = FilesystemDelegationRunner(config)
    utility = UtilityDelegationRunner(config)

    assert filesystem.provider == "openai_compatible_local"
    assert filesystem.model == "local-main-model"
    assert utility.provider == "openai_compatible_local"
    assert utility.model == "local-main-model"


def test_openai_search_model_sharing_approves_redacted_prompt(monkeypatch):
    config = _base_config()
    config["model_sharing"]["enabled"] = True
    seen = {}

    async def fake_request(prompt, **kwargs):
        seen["prompt"] = prompt
        seen.update(kwargs)
        return kwargs["redacted_prompt"]

    monkeypatch.setattr(
        "src.llm.specialist_delegate.request_external_model_prompt",
        fake_request,
    )

    runner = SearchDelegationRunner(config)
    approved = asyncio.run(
        runner._approve_external_model_request(
            r"Search for tanaka@example.com in C:\Projects\client\secret.txt"
        )
    )

    assert seen["provider"] == "openai"
    assert seen["model"] == "gpt-4o"
    assert seen["request_kind"] == "search_assistant"
    assert seen["prompt"] == r"Search for tanaka@example.com in C:\Projects\client\secret.txt"
    assert "tanaka@example.com" not in seen["redacted_prompt"]
    assert r"C:\Projects\client\secret.txt" not in seen["redacted_prompt"]
    assert approved == seen["redacted_prompt"]


def test_local_search_model_sharing_skips_external_prompt(monkeypatch):
    config = _base_config()
    config["model_sharing"]["enabled"] = True
    config["search"]["provider"] = "local"

    async def fail_request(*_args, **_kwargs):
        raise AssertionError("local search must not request external model approval")

    monkeypatch.setattr(
        "src.llm.specialist_delegate.request_external_model_prompt",
        fail_request,
    )

    runner = SearchDelegationRunner(config)
    approved = asyncio.run(
        runner._approve_external_model_request("local search request")
    )

    assert approved == "local search request"
