"""Turn-scoped model effort. Never mutate the shared/user-global LLM config."""

from typing import Any


def validate_response_model_effort(provider: str, model: str, effort: Any) -> str:
    from ..services.llm_model_catalog import reasoning_effort_options_for_model
    from .openai_compatible_local_profiles import llama_cpp_thinking_mode

    value = str(effort or "").strip().lower()
    if (
        str(provider or "").strip().lower() == "openai_compatible_local"
        and llama_cpp_thinking_mode(model) == "always"
        and value in {"fast", "thinking", "always"}
    ):
        # Existing response-model routes may still carry the generic local
        # fast/thinking labels.  Preserve switch compatibility while keeping
        # the provider-owned always-thinking contract canonical.
        return "always"
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
