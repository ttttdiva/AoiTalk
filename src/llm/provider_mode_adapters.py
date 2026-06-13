"""Provider/model specific chat mode helpers."""

from __future__ import annotations

from typing import Optional


FAST_THINKING_MODE_OPTIONS = ["fast", "thinking"]
OLLAMA_GPT_OSS_REASONING_EFFORT_OPTIONS = ["low", "medium", "high"]


def normalize_model_id_for_mode(model: str) -> str:
    return str(model or "").strip().lower()


def is_ollama_gpt_oss_model(model: str) -> bool:
    model_id = normalize_model_id_for_mode(model)
    return model_id.startswith("gpt-oss") or "/gpt-oss" in model_id


def is_ollama_boolean_thinking_model(model: str) -> bool:
    model_id = normalize_model_id_for_mode(model)
    return (
        model_id.startswith("qwen3")
        or "qwen3" in model_id
        or model_id.startswith("deepseek-r1")
        or "deepseek-r1" in model_id
        or model_id.startswith("deepseek-v3.1")
        or "deepseek-v3.1" in model_id
    )


def ollama_mode_options_for_model(model: str) -> list[str]:
    if is_ollama_gpt_oss_model(model):
        return OLLAMA_GPT_OSS_REASONING_EFFORT_OPTIONS
    if is_ollama_boolean_thinking_model(model):
        return FAST_THINKING_MODE_OPTIONS
    return ["fast"]


def ollama_reasoning_effort_for_mode(model: str, mode: str) -> Optional[str]:
    """Map AoiTalk's Ollama mode to Ollama's OpenAI-compatible payload."""

    normalized_mode = str(mode or "").strip().lower()

    if is_ollama_gpt_oss_model(model):
        if normalized_mode in OLLAMA_GPT_OSS_REASONING_EFFORT_OPTIONS:
            return normalized_mode
        if normalized_mode == "thinking":
            return "medium"
        if normalized_mode == "fast":
            return "low"
        return "medium"

    if is_ollama_boolean_thinking_model(model):
        if normalized_mode == "thinking":
            return "medium"
        if normalized_mode == "fast":
            return "none"
        if normalized_mode in {"none", "low", "medium", "high"}:
            return normalized_mode

    return None
