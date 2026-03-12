"""config_validator モジュールのテスト"""
import pytest
from pydantic import ValidationError

from src.config_validator import (
    ConfigValidator,
    TTSEngineConfig,
    SpeechRecognitionEngineConfig,
    MemoryConfig as ValidatorMemoryConfig,
    ReasoningConfig,
    Config as ValidatorConfig,
)


class TestTTSEngineConfig:
    def test_default_values(self):
        cfg = TTSEngineConfig(port=50021)
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 50021
        assert cfg.use_gpu is False

    def test_port_range(self):
        with pytest.raises(ValidationError):
            TTSEngineConfig(port=0)
        with pytest.raises(ValidationError):
            TTSEngineConfig(port=70000)


class TestReasoningConfig:
    def test_default_values(self):
        cfg = ReasoningConfig()
        assert cfg.enabled is False
        assert cfg.display_mode == "progress"

    def test_invalid_display_mode(self):
        with pytest.raises(ValidationError):
            ReasoningConfig(display_mode="invalid")

    def test_valid_display_modes(self):
        for mode in ("silent", "progress", "detailed", "debug"):
            cfg = ReasoningConfig(display_mode=mode)
            assert cfg.display_mode == mode


class TestValidatorConfig:
    def test_valid_gemini_config(self):
        cfg = ValidatorConfig(
            default_character="zundamon",
            llm_model="gemini-3-flash-preview",
            llm_provider="gemini",
            mode="terminal",
        )
        assert cfg.llm_model == "gemini-3-flash-preview"

    def test_invalid_mode(self):
        with pytest.raises(ValidationError):
            ValidatorConfig(
                default_character="zundamon",
                llm_model="gemini-3-flash-preview",
                llm_provider="gemini",
                mode="invalid",
            )

    def test_invalid_provider(self):
        with pytest.raises(ValidationError):
            ValidatorConfig(
                default_character="zundamon",
                llm_model="gemini-3-flash-preview",
                llm_provider="invalid_provider",
                mode="terminal",
            )

    def test_openai_provider_accepts_gpt_models(self):
        cfg = ValidatorConfig(
            default_character="zundamon",
            llm_model="gpt-4o",
            llm_provider="openai",
            mode="terminal",
        )
        assert cfg.llm_model == "gpt-4o"

    def test_openai_provider_accepts_o1_models(self):
        cfg = ValidatorConfig(
            default_character="zundamon",
            llm_model="o1-preview",
            llm_provider="openai",
            mode="terminal",
        )
        assert cfg.llm_model == "o1-preview"

    def test_sglang_any_model_accepted(self):
        cfg = ValidatorConfig(
            default_character="zundamon",
            llm_model="my-custom-model",
            llm_provider="sglang",
            mode="terminal",
        )
        assert cfg.llm_model == "my-custom-model"


class TestConfigValidatorMerge:
    def test_merge_flat(self):
        v = ConfigValidator()
        result = v.merge_configs({"a": 1, "b": 2}, {"b": 3, "c": 4})
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_merge_nested(self):
        v = ConfigValidator()
        base = {"x": {"a": 1, "b": 2}}
        override = {"x": {"b": 3, "c": 4}}
        result = v.merge_configs(base, override)
        assert result == {"x": {"a": 1, "b": 3, "c": 4}}

    def test_merge_does_not_mutate_base(self):
        v = ConfigValidator()
        base = {"a": 1}
        override = {"a": 2}
        v.merge_configs(base, override)
        assert base == {"a": 1}


class TestValidatorMemoryConfig:
    def test_defaults(self):
        cfg = ValidatorMemoryConfig()
        assert cfg.enabled is True
        assert cfg.max_context_tokens == 8000

    def test_similarity_threshold_range(self):
        with pytest.raises(ValidationError):
            ValidatorMemoryConfig(similarity_threshold=1.5)
        with pytest.raises(ValidationError):
            ValidatorMemoryConfig(similarity_threshold=-0.1)
