from src.services.llm_model_catalog import (
    build_engine_options,
    build_llm_mode_state,
    build_model_catalog,
    reasoning_effort_options_for_model,
    update_model_catalog_cache,
)


class DummyConfig:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeOllamaManager:
    def list_models(self):
        return {
            "models": [
                {
                    "name": "gemma4:e4b",
                    "size": 123,
                    "details": {"parameter_size": "8B"},
                },
                {
                    "name": "hf.co/Jackrong/Qwopus3.6-35B-A3B-v1-GGUF:Q4_K_M",
                    "size": 22_000_000_000,
                    "details": {"parameter_size": "35B"},
                }
            ]
        }


def test_codex_cli_refresh_does_not_mix_openai_model_api():
    calls = []

    def fake_fetch_json(*args, **kwargs):
        calls.append((args, kwargs))
        return {"data": [{"id": "gpt-3.5-turbo"}, {"id": "gpt-5.5"}]}

    catalog = build_model_catalog(
        DummyConfig({"openai_api_key": "key"}),
        include_remote=True,
        refresh_provider="codex-cli",
        fetch_json=fake_fetch_json,
    )

    codex = next(item for item in catalog["providers"] if item["id"] == "codex-cli")
    model_ids = [item["id"] for item in codex["models"]]

    assert calls == []
    assert "gpt-3.5-turbo" not in model_ids
    assert all(item["source"] == "cli-suggested" for item in codex["models"])


def test_codex_cli_default_candidate_prefers_non_spark_model():
    catalog = build_model_catalog(DummyConfig({}))

    codex = next(item for item in catalog["providers"] if item["id"] == "codex-cli")

    assert codex["models"][0]["id"] == "gpt-5-codex"
    assert codex["configured_model"] == "gpt-5-codex"


def test_cli_provider_settings_include_reasoning_effort():
    catalog = build_model_catalog(
        DummyConfig(
            {
                "codex_cli.reasoning_effort": "high",
                "claude_cli.reasoning_effort": "max",
            }
        )
    )

    codex = next(item for item in catalog["providers"] if item["id"] == "codex-cli")
    claude = next(item for item in catalog["providers"] if item["id"] == "claude-cli")
    gemini = next(item for item in catalog["providers"] if item["id"] == "gemini-cli")

    assert codex["settings"]["reasoning_effort"] == "high"
    assert codex["settings"]["reasoning_effort_options"] == ["low", "medium", "high", "xhigh"]
    assert claude["settings"]["reasoning_effort"] == "max"
    assert claude["settings"]["reasoning_effort_options"] == ["low", "medium", "high", "xhigh", "max"]
    assert gemini["settings"] == {}


def test_openai_reasoning_effort_options_are_model_specific():
    assert reasoning_effort_options_for_model("openai", "gpt-5") == [
        "minimal",
        "low",
        "medium",
        "high",
    ]
    assert reasoning_effort_options_for_model("openai", "gpt-5.1") == [
        "none",
        "low",
        "medium",
        "high",
    ]
    assert reasoning_effort_options_for_model("openai", "gpt-5.2") == [
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
    ]
    assert reasoning_effort_options_for_model("openai", "gpt-5.2-pro") == [
        "medium",
        "high",
        "xhigh",
    ]
    assert reasoning_effort_options_for_model("openai", "gpt-5.2-codex") == [
        "low",
        "medium",
        "high",
        "xhigh",
    ]
    assert reasoning_effort_options_for_model("openai", "gpt-5-pro") == ["high"]
    assert reasoning_effort_options_for_model("openai", "gpt-oss-20b") == [
        "low",
        "medium",
        "high",
    ]


def test_llm_mode_state_uses_reasoning_effort_for_reasoning_providers():
    state = build_llm_mode_state(
        DummyConfig(
            {
                "llm_provider": "openai",
                "llm_model": "gpt-5.2",
                "openai.reasoning_effort": "xhigh",
            }
        )
    )

    assert state["mode"] == "xhigh"
    assert state["kind"] == "reasoning_effort"
    assert state["available_modes"] == ["none", "low", "medium", "high", "xhigh"]


def test_ollama_mode_state_is_model_specific():
    gpt_oss = build_llm_mode_state(
        DummyConfig({"llm_provider": "ollama", "llm_model": "gpt-oss:20b"})
    )
    qwen = build_llm_mode_state(
        DummyConfig({"llm_provider": "ollama", "llm_model": "qwen3:32b"})
    )
    gemma = build_llm_mode_state(
        DummyConfig({"llm_provider": "ollama", "llm_model": "gemma4:e4b"})
    )

    assert gpt_oss["available_modes"] == ["low", "medium", "high"]
    assert gpt_oss["mode"] == "medium"
    assert gpt_oss["kind"] == "response_mode"
    assert qwen["available_modes"] == ["fast", "thinking"]
    assert qwen["mode"] == "fast"
    assert gemma["available_modes"] == ["fast"]
    assert gemma["mode"] == "fast"


def test_llm_mode_state_falls_back_to_fast_thinking_for_response_mode():
    state = build_llm_mode_state(
        DummyConfig({"llm_provider": "gemini", "llm_model": "gemini-3-flash-preview"})
    )

    assert state["mode"] == "fast"
    assert state["kind"] == "response_mode"
    assert state["available_modes"] == ["fast", "thinking"]


def test_gemini_cli_refresh_does_not_use_gemini_api_as_cli_catalog():
    calls = []

    def fake_fetch_json(*args, **kwargs):
        calls.append((args, kwargs))
        return {"models": [{"name": "models/gemini-dynamic"}]}

    catalog = build_model_catalog(
        DummyConfig({"gemini_api_key": "key"}),
        include_remote=True,
        refresh_provider="gemini-cli",
        fetch_json=fake_fetch_json,
    )

    gemini_cli = next(item for item in catalog["providers"] if item["id"] == "gemini-cli")
    model_ids = [item["id"] for item in gemini_cli["models"]]

    assert calls == []
    assert "gemini-dynamic" not in model_ids
    assert all(item["source"] == "cli-suggested" for item in gemini_cli["models"])


def test_openai_refresh_marks_provider_api_models():
    def fake_fetch_json(*args, **kwargs):
        return {"data": [{"id": "gpt-4.1"}, {"id": "not-chat-model"}]}

    catalog = build_model_catalog(
        DummyConfig({"openai_api_key": "key"}),
        include_remote=True,
        refresh_provider="openai",
        fetch_json=fake_fetch_json,
    )

    openai = next(item for item in catalog["providers"] if item["id"] == "openai")
    gpt41 = next(item for item in openai["models"] if item["id"] == "gpt-4.1")

    assert gpt41["source"] == "provider-api"
    assert gpt41["source_label"] == "API取得"
    assert openai["source"] == "remote"


def test_cached_openai_refresh_models_are_reused_without_refresh():
    cache = update_model_catalog_cache(
        {},
        "openai",
        [
            {
                "id": "gpt-dynamic",
                "label": "GPT Dynamic",
                "source": "provider-api",
                "source_label": "API取得",
            }
        ],
    )

    catalog = build_model_catalog(DummyConfig({}), cached_catalog=cache)
    openai = next(item for item in catalog["providers"] if item["id"] == "openai")
    cached = next(item for item in openai["models"] if item["id"] == "gpt-dynamic")

    assert cached["source"] == "provider-cache"
    assert cached["source_label"] == "前回取得"
    assert openai["source"] == "cached"
    assert openai["cached_at"]


def test_cli_providers_ignore_catalog_cache():
    cache = update_model_catalog_cache(
        {},
        "codex-cli",
        [
            {
                "id": "gpt-cli-dynamic",
                "source": "provider-api",
                "source_label": "API取得",
            }
        ],
    )

    catalog = build_model_catalog(DummyConfig({}), cached_catalog=cache)
    codex = next(item for item in catalog["providers"] if item["id"] == "codex-cli")

    assert "gpt-cli-dynamic" not in [item["id"] for item in codex["models"]]
    assert codex["source"] == "cli-suggested"


def test_updating_catalog_cache_does_not_mutate_input_cache():
    original = {"providers": {}}

    updated = update_model_catalog_cache(
        original,
        "openai",
        [{"id": "gpt-dynamic", "source": "provider-api"}],
    )

    assert original == {"providers": {}}
    assert updated != original


def test_catalog_exposes_saved_provider_model_when_provider_is_not_current():
    catalog = build_model_catalog(
        DummyConfig(
            {
                "llm_provider": "gemini",
                "llm_model": "gemini-3-flash-preview",
                "openai.model": "gpt-5.3",
            }
        )
    )

    openai = next(item for item in catalog["providers"] if item["id"] == "openai")
    saved = next(item for item in openai["models"] if item["id"] == "gpt-5.3")

    assert openai["configured_model"] == "gpt-5.3"
    assert saved["source_label"] == "保存済み設定"


def test_ollama_catalog_separates_installed_and_pull_candidates():
    catalog = build_model_catalog(
        DummyConfig({}),
        ollama_model_manager=FakeOllamaManager(),
    )

    ollama = next(item for item in catalog["providers"] if item["id"] == "ollama")
    installed = next(item for item in ollama["models"] if item["id"] == "gemma4:e4b")
    pull_candidate = next(item for item in ollama["models"] if item["id"] == "gpt-oss:20b")
    model_ids = [item["id"] for item in ollama["models"]]

    assert installed["source"] == "installed"
    assert installed["source_label"] == "インストール済み"
    assert pull_candidate["source"] == "pull-suggested"
    assert pull_candidate["source_label"] == "Pull候補"
    assert "hf.co/Jackrong/Qwopus3.6-35B-A3B-v1-GGUF:Q4_K_M" not in model_ids


def test_openai_compatible_local_catalog_exposes_settings_and_capabilities():
    catalog = build_model_catalog(
        DummyConfig(
            {
                "llm_provider": "openai_compatible_local",
                "llm_model": "qwen3.6-27b-dflash",
                "openai_compatible_local": {
                    "base_url": "http://127.0.0.1:8080/v1",
                    "api_key": "dummy",
                    "enable_tools": False,
                    "enable_response_format": False,
                },
            }
        )
    )

    provider = next(
        item for item in catalog["providers"] if item["id"] == "openai_compatible_local"
    )

    assert provider["configured_model"] == "qwen3.6-27b-dflash"
    assert provider["label"] == "ローカルOpenAI互換サーバー"
    assert provider["settings"]["base_url"] == "http://127.0.0.1:8080/v1"
    assert provider["settings"]["enable_tools"] is False
    assert provider["capabilities"]["supports_stream"] is True
    assert provider["capabilities"]["supports_model_pull"] is False
    custom = next(item for item in provider["models"] if item["id"] == "local-model")
    assert custom["label"] == "カスタムローカルサーバー"


def test_qwopus_is_openai_compatible_local_candidate_not_ollama_candidate():
    ollama_tag = "hf.co/Jackrong/Qwopus3.6-35B-A3B-v1-GGUF:Q4_K_M"
    local_model_id = "qwopus3.6-35b-a3b"

    catalog = build_model_catalog(
        DummyConfig({"llm_provider": "ollama", "llm_model": ollama_tag}),
        ollama_model_manager=FakeOllamaManager(),
    )

    ollama = next(item for item in catalog["providers"] if item["id"] == "ollama")
    local = next(
        item for item in catalog["providers"] if item["id"] == "openai_compatible_local"
    )

    assert ollama_tag not in [item["id"] for item in ollama["models"]]
    assert ollama["configured_model"] != ollama_tag
    assert local_model_id in [item["id"] for item in local["models"]]


def test_header_engine_options_are_one_item_per_provider():
    options = build_engine_options(
        DummyConfig({"llm_provider": "codex-cli", "llm_model": "gpt-5.5"}),
        ollama_model_manager=FakeOllamaManager(),
    )

    codex_options = [item for item in options if item["provider"] == "codex-cli"]

    assert len(codex_options) == 1
    assert codex_options[0] == {
        "provider": "codex-cli",
        "model": "gpt-5.5",
        "label": "Codex CLI (gpt-5.5)",
    }
    assert len(options) == len({item["provider"] for item in options})


def test_header_engine_options_use_saved_provider_model():
    options = build_engine_options(
        DummyConfig(
            {
                "llm_provider": "gemini",
                "llm_model": "gemini-3-flash-preview",
                "gemini_api_key": "key",
                "codex_cli.model": "gpt-5-codex",
            }
        ),
        ollama_model_manager=FakeOllamaManager(),
    )

    codex = next(item for item in options if item["provider"] == "codex-cli")

    assert codex["model"] == "gpt-5-codex"
    assert codex["label"] == "Codex CLI (gpt-5-codex)"
