"""Turn-scoped model effort. Never mutate the shared/user-global LLM config."""

from typing import Any


def validate_response_model_effort(provider: str, model: str, effort: Any) -> str:
    from ..services.llm_model_catalog import reasoning_effort_options_for_model

    value = str(effort or "").strip().lower()
    options = reasoning_effort_options_for_model(provider, model)
    if value not in options:
        raise ValueError(f"Unsupported effort for {provider}/{model}: {value}")
    return value


def apply_response_model_effort(config: Any, provider: str, model: str, effort: str) -> str:
    """Configure an already-cloned per-turn config; return the validated mode."""
    value = validate_response_model_effort(provider, model, effort)
    prefix = {
        "codex-cli": "codex_cli",
        "claude-cli": "claude_cli",
        "openai_compatible_local": "openai_compatible_local.llama_cpp",
    }.get(provider, provider)
    config.set(f"{prefix}.reasoning_effort", value)
    return value
