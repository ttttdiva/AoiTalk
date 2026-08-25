"""LLM model catalog helpers for settings UI."""

from __future__ import annotations

import json
import math
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.llm.provider_mode_adapters import (
    FAST_THINKING_MODE_OPTIONS,
    ollama_mode_options_for_model,
)
from src.llm.provider_capabilities import ProviderCapabilities
from src.services.provider_runtime_ownership import (
    enrich_provider_payload,
    provider_runtime_ownership,
)
from src.llm.deployment_resolver import resolve_llm_deployment
from src.llm.sglang_url import enterprise_sglang_model, resolve_sglang_base_url
from src.llm.openai_compatible_local_profiles import (
    LLAMA_CPP_DEFAULT_CONTEXT_SIZE,
    LLAMA_CPP_DEFAULT_GPU_LAYERS,
    LLAMA_CPP_DEFAULT_HOST,
    LLAMA_CPP_DEFAULT_PORT,
    LLAMA_CPP_DEFAULT_READINESS_TIMEOUT,
    llama_cpp_model_profile,
    llama_cpp_profile_capabilities,
    llama_cpp_model_profiles,
    llama_cpp_profile_legacy_kind,
    llama_cpp_reasoning_effort_default,
    llama_cpp_reasoning_effort_metadata,
    llama_cpp_reasoning_effort_options,
    llama_cpp_runtime_declared,
    macos_openai_compatible_local_model_options,
    local_server_profile_for_model,
    openai_compatible_local_base_url,
    openai_compatible_local_discovery_base_urls,
)
from src.llm.openai_model_context_registry import (
    openai_model_context_spec,
)
from src.services.agent_team_v3 import (
    agent_team_v3_subagents,
    resolve_agent_team_v3_route,
)

LLM_PROVIDER_LABELS = {
    "gemini": "Gemini API",
    "openai": "OpenAI API",
    "openrouter": "OpenRouter",
    "deepseek": "DeepSeek API",
    "deepinfra": "DeepInfra",
    "kimi": "Kimi API",
    "ollama": "Ollama",
    "sglang": "SGLang",
    "openai_compatible_local": "ローカルOpenAI互換サーバー",
    "codex-cli": "Codex CLI",
    "claude-cli": "Claude Code",
    "antigravity-cli": "Antigravity CLI",
    "grok-cli": "Grok Build CLI",
}

LLM_ENGINE_OPTIONS = [
    {
        "provider": "gemini",
        "model": "gemini-3-flash-preview",
        "label": "Gemini 3 Flash Preview",
    },
    {
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "label": "Gemini 2.5 Flash",
    },
    {
        "provider": "gemini",
        "model": "gemini-2.5-pro",
        "label": "Gemini 2.5 Pro",
    },
    {"provider": "openai", "model": "gpt-4o", "label": "GPT-4o"},
    {"provider": "openai", "model": "gpt-4o-mini", "label": "GPT-4o mini"},
    {"provider": "openai", "model": "gpt-5.5", "label": "GPT-5.5"},
]

PROVIDER_MODEL_CONFIG_KEYS = {
    "gemini": ("gemini.model", "gemini_model"),
    "openai": ("openai.model", "openai_model"),
    "openrouter": ("openrouter.model", "openrouter_model"),
    "deepseek": ("deepseek.model", "deepseek_model"),
    "deepinfra": ("deepinfra.model", "deepinfra_model"),
    "kimi": ("kimi.model", "kimi_model"),
    "ollama": ("ollama.model", "ollama_model"),
    "sglang": ("sglang.model", "sglang_model"),
    "openai_compatible_local": (
        "openai_compatible_local.model",
        "openai_compatible_local_model",
    ),
    "codex-cli": ("codex_cli.model", "codex_model"),
    "claude-cli": ("claude_cli.model", "claude_model"),
    "antigravity-cli": ("antigravity_cli.model", "antigravity_cli_model"),
    "grok-cli": ("grok_cli.model", "grok_cli_model"),
}

PROVIDER_ORDER = [
    "openai",
    "gemini",
    "openrouter",
    "deepseek",
    "deepinfra",
    "kimi",
    "ollama",
    "sglang",
    "openai_compatible_local",
    "codex-cli",
    "claude-cli",
    "antigravity-cli",
    "grok-cli",
]


def model_supports_vision(provider: str, model: str) -> bool | None:
    """Return known vision support for a provider/model pair.

    True/False is reserved for known cloud model families. None means unknown,
    which callers should treat as requiring the recognition route.
    """
    provider_id = str(provider or "").strip().lower()
    model_id = str(model or "").strip().lower()
    if not provider_id or not model_id:
        return None
    local_or_unknown = {"sglang", "openai_compatible_local", "ollama"}
    if provider_id in local_or_unknown:
        return None
    if provider_id == "gemini":
        return True
    # CLI backends use file attachments rather than the app's native image
    # payload, so let the media-recognition route prepare the attachment.
    if provider_id in {"codex-cli", "antigravity-cli"}:
        return None
    if provider_id == "openai":
        if model_id.startswith(("gpt-4o", "gpt-4.1", "gpt-5", "o3", "o4")):
            return True
        return False
    if provider_id == "openrouter":
        if any(part in model_id for part in ("gpt-4o", "gpt-4.1", "gpt-5", "gemini", "claude-3", "claude-sonnet", "claude-opus")):
            return True
        return None
    if provider_id == "kimi":
        return model_id == "kimi-k3"
    if provider_id == "deepseek":
        return False
    if provider_id == "deepinfra":
        # DeepInfra exposes heterogeneous hosted models.  Keep the answer
        # unknown here and let per-model API metadata decide media support.
        return None
    if provider_id == "claude":
        if "claude-3" in model_id or "sonnet" in model_id or "opus" in model_id:
            return True
        return None
    if provider_id == "grok":
        if "vision" in model_id or "grok-2" in model_id or "grok-4" in model_id:
            return True
        return None
    return None

STATIC_MODEL_CATALOG = {
    "gemini": [
        {
            "id": "gemini-3.1-pro-preview",
            "label": "Gemini 3.1 Pro Preview",
            "description": "複雑な推論・コーディング向け",
        },
        {
            "id": "gemini-3-flash-preview",
            "label": "Gemini 3 Flash Preview",
            "description": "高性能・低コストの汎用モデル",
        },
        {
            "id": "gemini-3.1-flash-lite",
            "label": "Gemini 3.1 Flash-Lite",
            "description": "高速・低コスト",
        },
        {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
        {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
        {
            "id": "gemini-2.5-flash-lite",
            "label": "Gemini 2.5 Flash-Lite",
        },
    ],
    "openai": [
        {"id": "gpt-5.5", "label": "GPT-5.5"},
        {"id": "gpt-5.5-pro", "label": "GPT-5.5 pro"},
        {"id": "gpt-5.4", "label": "GPT-5.4"},
        {"id": "gpt-5.4-mini", "label": "GPT-5.4 mini"},
        {"id": "gpt-5.4-nano", "label": "GPT-5.4 nano"},
        {"id": "gpt-5-mini", "label": "GPT-5 mini"},
        {"id": "gpt-4.1", "label": "GPT-4.1"},
    ],
    "openrouter": [
        {"id": "openai/gpt-5.5", "label": "OpenAI: GPT-5.5"},
        {"id": "openai/gpt-5.4", "label": "OpenAI: GPT-5.4"},
        {
            "id": "anthropic/claude-opus-4.1",
            "label": "Anthropic: Claude Opus 4.1",
        },
        {
            "id": "anthropic/claude-sonnet-4.5",
            "label": "Anthropic: Claude Sonnet 4.5",
        },
        {
            "id": "google/gemini-3.1-pro-preview",
            "label": "Google: Gemini 3.1 Pro Preview",
        },
        {
            "id": "google/gemini-2.5-flash",
            "label": "Google: Gemini 2.5 Flash",
        },
        {
            "id": "deepseek/deepseek-v4-flash",
            "label": "DeepSeek V4 Flash (OpenRouter)",
        },
    ],
    "kimi": [
        {
            "id": "kimi-k3",
            "label": "Kimi K3",
            "description": "Moonshot AI公式APIの長文・推論・画像対応モデル",
            "context_length": 1048576,
            "supports_reasoning": True,
            "media": {"image": True, "audio": False},
        },
    ],
    "deepseek": [
        {
            "id": "deepseek-v4-flash",
            "label": "DeepSeek V4 Flash",
            "description": "DeepSeek公式APIの高速・低コスト推論モデル",
            "context_length": 1048576,
            "supports_reasoning": True,
            "media": {"image": False, "audio": False},
        },
        {
            "id": "deepseek-v4-pro",
            "label": "DeepSeek V4 Pro",
            "description": "DeepSeek公式APIの高性能推論モデル",
            "context_length": 1048576,
            "supports_reasoning": True,
            "media": {"image": False, "audio": False},
        },
    ],
    "deepinfra": [
        {
            "id": "deepseek-ai/DeepSeek-V4-Flash",
            "label": "DeepSeek V4 Flash (DeepInfra)",
            "description": "DeepInfra公式APIの高速・低コスト推論モデル",
            "context_length": 1048576,
            "supports_reasoning": True,
            "media": {"image": False, "audio": False},
        },
        {
            "id": "deepseek-ai/DeepSeek-V4-Pro",
            "label": "DeepSeek V4 Pro (DeepInfra)",
            "description": "DeepInfra公式APIの高性能推論モデル",
            "context_length": 1048576,
            "supports_reasoning": True,
            "media": {"image": False, "audio": False},
        },
    ],
    "ollama": [
        {"id": "gpt-oss:20b", "label": "gpt-oss:20b"},
        {"id": "gpt-oss:120b", "label": "gpt-oss:120b"},
        {"id": "deepseek-r1:14b", "label": "deepseek-r1:14b"},
        {"id": "llama3.2:3b", "label": "llama3.2:3b"},
        {"id": "gemma3:12b", "label": "gemma3:12b"},
        {"id": "gemma4:e4b", "label": "gemma4:e4b"},
    ],
    "sglang": [
        {
            "id": "google/gemma-4-E4B-it",
            "label": "Gemma 4 E4B IT",
            "description": "Linux/WSLの初期SGLangモデル（BF16）",
            "context_length": 32768,
            "supports_reasoning": True,
            "media": {"image": True, "audio": False},
        },
        {"id": "openai/gpt-oss-20b", "label": "openai/gpt-oss-20b"},
        {"id": "openai/gpt-oss-120b", "label": "openai/gpt-oss-120b"},
        {"id": "deepseek-ai/DeepSeek-R1", "label": "DeepSeek-R1"},
        {"id": "meta-llama/Llama-3.3-70B-Instruct", "label": "Llama 3.3 70B Instruct"},
        {"id": "default", "label": "default"},
    ],
    "openai_compatible_local": [
        {
            "id": "local-model",
            "label": "カスタムローカルサーバー",
            "description": "AoiTalkでは自動起動せず、Base URLに入力した起動済みOpenAI互換サーバーへ接続",
        },
    ],
    "codex-cli": [
        {
            "id": "gpt-5-codex",
            "label": "GPT-5-Codex",
            "description": "Codex CLI 用の候補。CLIからの一覧取得ではありません。",
        },
        {
            "id": "gpt-5.3-codex-spark",
            "label": "GPT-5.3-Codex Spark",
            "description": "Codex CLI 用の候補。CLIからの一覧取得ではありません。",
        },
        {"id": "gpt-5.5", "label": "GPT-5.5"},
        {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol"},
        {"id": "gpt-5.6-luna", "label": "GPT-5.6 Luna"},
        {"id": "gpt-5.4", "label": "GPT-5.4"},
        {"id": "gpt-5.4-mini", "label": "GPT-5.4 mini"},
        {"id": "gpt-5-mini", "label": "GPT-5 mini"},
    ],
    "claude-cli": [
        {
            "id": "default",
            "label": "default",
            "description": "Claude Code の既定モデルを使う alias",
        },
        {"id": "sonnet", "label": "sonnet", "description": "Claude Code alias"},
        {"id": "opus", "label": "opus", "description": "Claude Code alias"},
        {"id": "haiku", "label": "haiku", "description": "Claude Code alias"},
    ],
    "antigravity-cli": [
        {
            "id": "default",
            "label": "default",
            "description": "Antigravity CLI の既定モデルを使用します。",
        },
        {
            "id": "Gemini 3.5 Flash (High)",
            "label": "Gemini 3.5 Flash (High)",
            "description": "Antigravity CLI 用の候補。agy models から取得できない環境向けの静的候補です。",
        },
        {
            "id": "Gemini 3.5 Flash (Medium)",
            "label": "Gemini 3.5 Flash (Medium)",
            "description": "Antigravity CLI 用の候補。agy models から取得できない環境向けの静的候補です。",
        },
        {"id": "Gemini 3.5 Flash (Low)", "label": "Gemini 3.5 Flash (Low)"},
        {"id": "Gemini 3.1 Pro (High)", "label": "Gemini 3.1 Pro (High)"},
        {"id": "Gemini 3.1 Pro (Low)", "label": "Gemini 3.1 Pro (Low)"},
        {
            "id": "Claude Sonnet 4.6 (Thinking)",
            "label": "Claude Sonnet 4.6 (Thinking)",
        },
        {
            "id": "Claude Opus 4.6 (Thinking)",
            "label": "Claude Opus 4.6 (Thinking)",
        },
    ],
    "grok-cli": [
        {
            "id": "grok-build",
            "label": "grok-build",
            "description": "Grok Build CLI の既定コーディングモデル。",
        },
        {
            "id": "grok-build-0.1",
            "label": "grok-build-0.1",
            "description": "Grok Build 0.1。CLI側の設定で利用可能な場合に指定します。",
        },
        {"id": "grok-4.5", "label": "grok-4.5"},
    ],
}


def _llama_cpp_model_catalog_entries() -> List[Dict[str, Any]]:
    """Convert registered llama.cpp profiles into provider model options."""

    entries: List[Dict[str, Any]] = []
    for profile in llama_cpp_model_profiles():
        runtime_profile = dict(profile)
        model_id = str(runtime_profile.get("id") or runtime_profile.get("served_alias") or "").strip()
        label = str(runtime_profile.get("label") or model_id)
        description = str(runtime_profile.get("description") or "").strip()
        native_context = runtime_profile.get("native_context_size")
        # Keep legacy descriptive fields outside runtime_profile for older UI
        # clients; the canonical runtime_profile remains schema-stable.
        details = dict(runtime_profile)
        details.pop("id", None)
        details.pop("label", None)
        details.pop("description", None)
        if runtime_profile.get("gguf_filename"):
            details.update(
                {
                    "official_filename": runtime_profile["gguf_filename"],
                    "model_filename": runtime_profile["gguf_filename"],
                    "filename": runtime_profile["gguf_filename"],
                }
            )
        capabilities = runtime_profile.get("capabilities")
        if isinstance(capabilities, dict):
            details.update(
                {
                    "supports_reasoning": bool(capabilities.get("reasoning")),
                    "supports_tools": bool(capabilities.get("tools")),
                    "supports_media": bool(
                        isinstance(capabilities.get("media"), dict)
                        and any(capabilities["media"].values())
                    ),
                }
            )
        # MTP is profile-owned metadata.  Keep the nested shape intact so the
        # settings UI can distinguish capability/default/artifact contracts
        # without hard-coding Qwen model IDs.  The runtime settings projection
        # below adds canonical availability/status for the active selection.
        mtp_metadata = runtime_profile.get("mtp")
        entry: Dict[str, Any] = {
            "id": model_id,
            "label": label,
            "description": description,
            "details": details,
            "runtime_profile": runtime_profile,
            "base_url": f"http://{LLAMA_CPP_DEFAULT_HOST}:{LLAMA_CPP_DEFAULT_PORT}/v1",
            "runtime": "llama_cpp",
        }
        if isinstance(mtp_metadata, dict):
            entry["mtp"] = dict(mtp_metadata)
        effort_metadata = llama_cpp_reasoning_effort_metadata(profile=runtime_profile)
        if effort_metadata:
            entry["reasoning_effort_options"] = list(effort_metadata["options"])
            entry["reasoning_effort_default"] = effort_metadata["default"]
            entry["reasoning_effort_supports_disable"] = bool(
                effort_metadata["supports_disable"]
            )
            entry["reasoning_effort_wire"] = (
                dict(effort_metadata["wire"])
                if isinstance(effort_metadata.get("wire"), dict)
                else None
            )
        if native_context:
            entry["context_length"] = native_context
        if isinstance(capabilities, dict):
            entry["supports_reasoning"] = bool(capabilities.get("reasoning"))
            entry["supports_tools"] = bool(capabilities.get("tools"))
            media = capabilities.get("media")
            if isinstance(media, dict):
                entry["media"] = dict(media)
        entries.append(entry)
    return entries

PROVIDER_CAPABILITIES = {
    "openai": ProviderCapabilities(
        supports_stream=True,
        supports_tools=True,
        supports_response_format=True,
    ),
    "openrouter": ProviderCapabilities(
        supports_stream=True,
        supports_tools=True,
        supports_response_format=False,
        supports_extra_body=True,
    ),
    "kimi": ProviderCapabilities(
        supports_stream=True,
        supports_tools=True,
        supports_response_format=False,
    ),
    "deepseek": ProviderCapabilities(
        supports_stream=True,
        supports_tools=True,
        supports_response_format=True,
        supports_extra_body=True,
    ),
    "deepinfra": ProviderCapabilities(
        supports_stream=True,
        supports_tools=True,
        supports_response_format=True,
        supports_extra_body=True,
    ),
    "gemini": ProviderCapabilities(
        supports_stream=True,
        supports_tools=True,
        supports_response_format=True,
    ),
    "ollama": ProviderCapabilities(
        supports_stream=True,
        supports_tools=True,
        supports_response_format=True,
        supports_model_pull=True,
        supports_model_delete=True,
    ),
    "sglang": ProviderCapabilities(
        supports_stream=True,
        supports_tools=True,
        supports_extra_body=True,
    ),
    "openai_compatible_local": ProviderCapabilities(
        supports_stream=True,
        supports_tools=True,
        supports_response_format=True,
        supports_extra_body=True,
    ),
    "codex-cli": ProviderCapabilities(),
    "claude-cli": ProviderCapabilities(),
    "antigravity-cli": ProviderCapabilities(),
    "grok-cli": ProviderCapabilities(),
}

STATIC_SOURCE_BY_PROVIDER = {
    "codex-cli": ("cli-suggested", "CLI候補"),
    "claude-cli": ("cli-suggested", "CLI候補"),
    "antigravity-cli": ("cli-suggested", "CLI候補"),
    "grok-cli": ("cli-suggested", "CLI候補"),
    "ollama": ("pull-suggested", "Pull候補"),
}

CACHEABLE_REMOTE_PROVIDERS = {
    "openai",
    "gemini",
    "openrouter",
    "deepseek",
    "deepinfra",
    "kimi",
    "sglang",
    "openai_compatible_local",
}

REMOTE_MODEL_SOURCES = {"provider-api", "service-api"}

DEFAULT_MODEL_CATALOG_CACHE_PATH = Path("cache") / "llm_model_catalog.json"

CODEX_REASONING_EFFORT_OPTIONS = ["low", "medium", "high", "xhigh"]
CODEX_GPT56_SOL_REASONING_EFFORT_OPTIONS = [
    *CODEX_REASONING_EFFORT_OPTIONS,
    "max",
    "ultra",
]
CODEX_GPT56_LUNA_REASONING_EFFORT_OPTIONS = [
    *CODEX_REASONING_EFFORT_OPTIONS,
    "max",
]
CLAUDE_REASONING_EFFORT_OPTIONS = ["low", "medium", "high", "xhigh", "max"]
OPENAI_FULL_REASONING_EFFORT_OPTIONS = ["none", "low", "medium", "high", "xhigh"]
OPENAI_GPT56_REASONING_EFFORT_OPTIONS = [
    "none",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
]
OPENAI_GPT5_REASONING_EFFORT_OPTIONS = ["minimal", "low", "medium", "high"]
OPENAI_GPT51_REASONING_EFFORT_OPTIONS = ["none", "low", "medium", "high"]
OPENAI_GPT52_PRO_REASONING_EFFORT_OPTIONS = ["medium", "high", "xhigh"]
OPENAI_STANDARD_REASONING_EFFORT_OPTIONS = ["low", "medium", "high"]
OPENAI_CODEX_REASONING_EFFORT_OPTIONS = ["low", "medium", "high", "xhigh"]

LLM_MODE_LABELS = {
    "fast": "Fast",
    "thinking": "Thinking",
    "none": "None",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "XHigh",
    "max": "Max",
    "ultra": "Ultra",
}

OLLAMA_INCOMPATIBLE_MODEL_IDS = {
    "hf.co/jackrong/qwopus3.6-35b-a3b-v1-gguf:q4_k_m",
}


LOCAL_MODEL_LABELS: dict[str, str] = {}

def model_option(model_id: str, label: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    model_id_text = str(model_id)
    option = {
        "id": model_id_text,
        "label": label or LOCAL_MODEL_LABELS.get(model_id_text) or model_id_text,
    }
    option.update(extra)
    return option


def dedupe_models(models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result = []
    for model in models:
        model_id = str(model.get("id") or model.get("model") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        next_model = dict(model)
        next_model["id"] = model_id
        next_model.setdefault("label", model_id)
        next_model.setdefault("source", "static-suggested")
        next_model.setdefault("source_label", "候補")
        result.append(next_model)
    return result


_OPENAI_GPT_VERSION_RE = re.compile(r"^gpt-(\d+)(?:\.(\d+))?(?:-|$)")
_DATED_MODEL_RE = re.compile(r"-\d{4}-\d{2}-\d{2}(?:-|$)")
_OPENAI_VARIANT_ORDER = {
    "": 0,
    "sol": 1,
    "terra": 2,
    "luna": 3,
    "pro": 4,
    "mini": 5,
    "nano": 6,
    "codex": 7,
}


def _openai_model_sort_key(model: Dict[str, Any]) -> Tuple[Any, ...]:
    """Put recent numbered GPT families first while preserving unrelated API order."""

    model_id = str(model.get("id") or "").strip().lower()
    match = _OPENAI_GPT_VERSION_RE.match(model_id)
    if not match:
        return (1,)

    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    suffix = model_id[match.end() :]
    variant = suffix.split("-", 1)[0] if suffix else ""
    if re.fullmatch(r"\d{4}", variant):
        variant = ""
    variant_order = _OPENAI_VARIANT_ORDER.get(variant, 50)
    is_dated_snapshot = bool(_DATED_MODEL_RE.search(model_id))
    return (
        0,
        -major,
        -minor,
        is_dated_snapshot,
        variant_order,
        model_id,
    )


def sort_provider_models(
    provider: str,
    models: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if provider != "openai":
        return models
    return sorted(models, key=_openai_model_sort_key)


def enrich_model_reasoning_options(
    provider: str,
    models: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    result = []
    for model in models:
        next_model = dict(model)
        options = reasoning_effort_options_for_model(
            provider,
            str(next_model.get("id") or ""),
        )
        if options:
            next_model["reasoning_effort_options"] = options
        result.append(next_model)
    return result


def _normalize_positive_token_limit(value: Any) -> int | None:
    """Return a finite positive integer token limit, or ``None``.

    Provider metadata is external input.  Treat booleans, zero/negative
    values, NaN/Inf, fractional limits, and non-numeric values as absent
    rather than allowing them to leak into the public catalog or raise while
    building it.
    """

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer() or value <= 0:
            return None
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(parsed) or not parsed.is_integer() or parsed <= 0:
            return None
        return int(parsed)
    return None


_OPENAI_CONTEXT_METADATA_KEYS = (
    "context_window_tokens",
    "context_length",
    "max_context_length",
    "context_window",
    "max_output_tokens",
)


def _sanitize_openai_context_metadata(model: Dict[str, Any]) -> None:
    """Normalize or remove untrusted provider context metadata in-place."""

    for key in _OPENAI_CONTEXT_METADATA_KEYS:
        if key not in model:
            continue
        normalized = _normalize_positive_token_limit(model.get(key))
        if normalized is None:
            model.pop(key, None)
        else:
            model[key] = normalized


def enrich_openai_model_context_metadata(
    provider: str,
    models: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Supplement OpenAI catalog entries from the official context registry.

    OpenAI's model-list endpoint does not publish context limits.  For an
    exact registry ID we add the documented value, while preserving any
    context metadata already supplied by a provider/proxy or a cached entry.
    Unknown IDs are intentionally left untouched.  ``context_length`` is kept
    as a compatibility alias for the existing settings UI; the canonical API
    field is ``context_window_tokens``.
    """

    if str(provider or "").strip().casefold() != "openai":
        return models

    enriched: List[Dict[str, Any]] = []
    for model in models:
        next_model = dict(model)
        # Unknown model IDs must not leak NaN/Inf or other non-JSON metadata
        # into the public catalog.  Sanitizing before registry lookup also
        # makes the known-ID precedence below operate on one safe type.
        _sanitize_openai_context_metadata(next_model)
        raw_model_id = next_model.get("id")
        if raw_model_id is None:
            raw_model_id = next_model.get("model")
        # ``dedupe_models`` keeps the provider label while canonicalizing the
        # display ``id``.  For OpenAI API/cache entries a label that differs
        # only by surrounding whitespace is the raw ID returned by the
        # provider; use it to avoid attaching registry data to that
        # non-exact identifier without mistaking a human display label for an
        # ID.
        if next_model.get("source") in {"provider-api", "provider-cache"}:
            raw_label = next_model.get("label")
            display_id = next_model.get("id")
            if (
                isinstance(raw_label, str)
                and isinstance(display_id, str)
                and raw_label != display_id
                and raw_label.strip() == display_id
            ):
                raw_model_id = raw_label
        model_id = raw_model_id if isinstance(raw_model_id, str) else ""
        spec = openai_model_context_spec(model_id)
        if spec is not None:
            # ``provider_models`` and ``build_model_catalog`` both run this
            # supplement in their respective public paths.  Preserve the
            # provenance marker emitted by an earlier pass instead of
            # reclassifying the official value as provider metadata merely
            # because the canonical field is now populated.
            already_official = (
                next_model.get("context_window_source") == "official-registry"
                and next_model.get("context_window_source_url") == spec.source_url
                and next_model.get("context_window_registry_snapshot")
                == spec.snapshot
            )
            # A valid canonical provider value wins over the compatibility
            # alias, which wins over legacy max_context_length.  Invalid
            # provider values are treated as absent and replaced by the
            # official registry value.
            canonical_window = _normalize_positive_token_limit(
                next_model.get("context_window_tokens")
            )
            alias_window = _normalize_positive_token_limit(
                next_model.get("context_length")
            )
            legacy_window = _normalize_positive_token_limit(
                next_model.get("max_context_length")
            )
            compat_window = _normalize_positive_token_limit(
                next_model.get("context_window")
            )
            provider_window = (
                None
                if already_official
                else canonical_window
                or alias_window
                or legacy_window
                or compat_window
            )
            window = provider_window or spec.context_window_tokens
            next_model["context_window_tokens"] = window
            next_model["context_length"] = window
            if provider_window is None:
                next_model["context_window_source"] = "official-registry"
                next_model["context_window_source_url"] = spec.source_url
                next_model["context_window_registry_snapshot"] = spec.snapshot
            else:
                next_model["context_window_source"] = "provider-api"
                next_model.pop("context_window_source_url", None)
                next_model.pop("context_window_registry_snapshot", None)
            output_tokens = _normalize_positive_token_limit(
                next_model.get("max_output_tokens")
            )
            next_model["max_output_tokens"] = (
                output_tokens
                if output_tokens is not None
                else spec.max_output_tokens
            )
        enriched.append(next_model)
    return enriched


def load_model_catalog_cache(
    cache_path: Path | str = DEFAULT_MODEL_CATALOG_CACHE_PATH,
) -> Dict[str, Any]:
    path = Path(cache_path)
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_model_catalog_cache(
    cache: Dict[str, Any],
    cache_path: Path | str = DEFAULT_MODEL_CATALOG_CACHE_PATH,
) -> None:
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _cache_entry(cache: Optional[Dict[str, Any]], provider: str) -> Dict[str, Any]:
    if provider not in CACHEABLE_REMOTE_PROVIDERS:
        return {}
    providers = cache.get("providers") if isinstance(cache, dict) else None
    if not isinstance(providers, dict):
        return {}
    entry = providers.get(provider)
    return entry if isinstance(entry, dict) else {}


def cached_provider_models(
    provider: str,
    cache: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    entry = _cache_entry(cache, provider)
    raw_models = entry.get("models")
    if not isinstance(raw_models, list):
        return []

    models: List[Dict[str, Any]] = []
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or item.get("model") or "").strip()
        if not model_id:
            continue
        next_item = dict(item)
        next_item["id"] = model_id
        next_item.setdefault("label", item.get("label") or model_id)
        next_item["source"] = "provider-cache"
        next_item["source_label"] = "前回取得"
        models.append(next_item)
    return dedupe_models(models)


def cached_provider_updated_at(
    provider: str,
    cache: Optional[Dict[str, Any]],
) -> Optional[str]:
    updated_at = _cache_entry(cache, provider).get("updated_at")
    return str(updated_at) if updated_at else None


def update_model_catalog_cache(
    cache: Optional[Dict[str, Any]],
    provider: str,
    models: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if provider not in CACHEABLE_REMOTE_PROVIDERS:
        return cache if isinstance(cache, dict) else {}

    remote_models = [
        dict(model)
        for model in models
        if str(model.get("source") or "") in REMOTE_MODEL_SOURCES
    ]
    if not remote_models:
        return cache if isinstance(cache, dict) else {}

    next_cache = dict(cache or {})
    providers = next_cache.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    else:
        providers = dict(providers)
    next_cache["version"] = 1
    next_cache["providers"] = providers
    providers[provider] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "models": dedupe_models(remote_models),
    }
    return next_cache


def default_fetch_json(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 4.0,
) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    getter = getattr(config, "get", None)
    if callable(getter):
        missing = object()
        try:
            value = getter(key, missing)
        except TypeError:
            value = missing
        if value is not missing:
            return value
    if isinstance(config, dict):
        value = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    return default


def _static_models(provider: str) -> List[Dict[str, Any]]:
    source, source_label = STATIC_SOURCE_BY_PROVIDER.get(
        provider,
        ("static-suggested", "候補"),
    )
    catalog_items = list(STATIC_MODEL_CATALOG.get(provider, []))
    if provider == "openai_compatible_local":
        catalog_items = (
            macos_openai_compatible_local_model_options()
            + catalog_items
            + _llama_cpp_model_catalog_entries()
        )

    models = []
    for item in catalog_items:
        extra = {
            key: value
            for key, value in item.items()
            if key not in {"id", "model", "label"}
        }
        item_source = extra.pop("source", source)
        item_source_label = extra.pop("source_label", source_label)
        models.append(
            model_option(
                item.get("id") or item.get("model"),
                item.get("label"),
                **extra,
                source=item_source,
                source_label=item_source_label,
            )
        )
    return models


def _is_ollama_incompatible_model(model_id: Optional[str]) -> bool:
    return str(model_id or "").strip().lower() in OLLAMA_INCOMPATIBLE_MODEL_IDS


def _openrouter_models(fetch_json: Callable[..., Dict[str, Any]]) -> List[Dict[str, Any]]:
    data = fetch_json("https://openrouter.ai/api/v1/models", timeout=5.0)
    models = []
    for item in data.get("data", []):
        model_id = item.get("id")
        if not model_id:
            continue
        models.append(
            model_option(
                model_id,
                item.get("name") or model_id,
                context_length=item.get("context_length"),
                source="provider-api",
                source_label="API取得",
            )
        )
    return models


def _kimi_models(
    cfg: Any,
    fetch_json: Callable[..., Dict[str, Any]],
) -> List[Dict[str, Any]]:
    api_key = _config_get(cfg, "kimi_api_key") or os.environ.get("MOONSHOT_API_KEY")
    if not api_key:
        return []
    base_url = str(
        _config_get(cfg, "kimi_base_url")
        or os.environ.get("MOONSHOT_BASE_URL")
        or _config_get(cfg, "kimi.base_url", "https://api.moonshot.ai/v1")
        or "https://api.moonshot.ai/v1"
    ).rstrip("/")
    data = fetch_json(
        f"{base_url}/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=5.0,
    )
    models: List[Dict[str, Any]] = []
    for item in data.get("data", []):
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        models.append(
            model_option(
                model_id,
                model_id,
                source="provider-api",
                source_label="API取得",
                context_length=item.get("context_length"),
                supports_reasoning=bool(item.get("supports_reasoning", False)),
                supports_video_in=bool(item.get("supports_video_in", False)),
                media={
                    "image": bool(item.get("supports_image_in", False)),
                    "audio": False,
                },
            )
        )
    return models


def _deepseek_models(
    cfg: Any,
    fetch_json: Callable[..., Dict[str, Any]],
) -> List[Dict[str, Any]]:
    api_key = _config_get(cfg, "deepseek_api_key") or os.environ.get(
        "DEEPSEEK_API_KEY"
    )
    if not api_key:
        return []
    base_url = str(
        _config_get(cfg, "deepseek_base_url")
        or os.environ.get("DEEPSEEK_BASE_URL")
        or _config_get(cfg, "deepseek.base_url", "https://api.deepseek.com")
        or "https://api.deepseek.com"
    ).rstrip("/")
    data = fetch_json(
        f"{base_url}/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=5.0,
    )
    models: List[Dict[str, Any]] = []
    for item in data.get("data", []):
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        models.append(
            model_option(
                model_id,
                item.get("name") or model_id,
                source="provider-api",
                source_label="API取得",
                context_length=item.get("context_length") or 1048576,
                supports_reasoning=bool(item.get("supports_reasoning", True)),
                media={"image": False, "audio": False},
            )
        )
    return models


DEEPINFRA_CHAT_BASE_URL = "https://api.deepinfra.com/v1/openai"
DEEPINFRA_MODELS_URL = "https://api.deepinfra.com/v1/models"


def _deepinfra_models_url(cfg: Any) -> str:
    """Resolve the model-list endpoint without blindly appending to chat base."""

    explicit = str(
        _config_get(cfg, "deepinfra.models_url")
        or os.environ.get("DEEPINFRA_MODELS_URL")
        or ""
    ).strip()
    if explicit:
        return explicit.rstrip("/")

    chat_base = str(
        _config_get(cfg, "deepinfra.base_url")
        or _config_get(cfg, "deepinfra_base_url")
        or os.environ.get("DEEPINFRA_BASE_URL")
        or DEEPINFRA_CHAT_BASE_URL
    ).rstrip("/")
    if chat_base.endswith("/openai"):
        return f"{chat_base[:-len('/openai')]}/models"
    # Never send a custom proxy token to the official endpoint.  A custom
    # deployment must opt into its own list endpoint explicitly.
    return ""


def _deepinfra_metadata_text(
    item: Dict[str, Any],
    metadata: Dict[str, Any],
    *keys: str,
) -> str:
    values: List[str] = []
    for key in keys:
        value = item.get(key)
        if value is None:
            value = metadata.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, (list, tuple, set)):
            values.extend(str(part) for part in value if part is not None)
        elif isinstance(value, dict):
            values.extend(str(part) for part in value.keys())
    return " ".join(values).strip().lower()


def _deepinfra_bool(item: Dict[str, Any], metadata: Dict[str, Any], *keys: str) -> Optional[bool]:
    for key in keys:
        value = item.get(key)
        if value is None:
            value = metadata.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return bool(value)
    return None


def _deepinfra_models(
    cfg: Any,
    fetch_json: Callable[..., Dict[str, Any]],
) -> List[Dict[str, Any]]:
    api_key = _config_get(cfg, "deepinfra_api_key") or os.environ.get("DEEPINFRA_TOKEN")
    models_url = _deepinfra_models_url(cfg)
    if not api_key or not models_url:
        return []

    data = fetch_json(
        models_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=5.0,
    )
    raw_items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(raw_items, list) and isinstance(data, dict):
        raw_items = data.get("models")
    if not isinstance(raw_items, list):
        return []

    models: List[Dict[str, Any]] = []
    non_text_pattern = re.compile(r"(?:embedding|image|audio|speech|whisper|moderation|rerank|transcrib)", re.I)
    for item in raw_items:
        if not isinstance(item, dict) or item.get("deprecated") is True:
            continue
        model_id = str(
            item.get("id")
            or item.get("model")
            or item.get("model_name")
            or item.get("name")
            or ""
        ).strip()
        if not model_id:
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        capability_text = _deepinfra_metadata_text(
            item,
            metadata,
            "tags",
            "capabilities",
            "supported_tasks",
        )
        declared_type = str(
            item.get("reported_type")
            or item.get("task")
            or item.get("task_type")
            or item.get("type")
            or metadata.get("reported_type")
            or metadata.get("task")
            or metadata.get("type")
            or ""
        ).strip().lower()
        if declared_type and non_text_pattern.search(declared_type):
            continue
        if declared_type and not any(token in declared_type for token in ("text", "chat", "generation", "causal")):
            continue

        supports_reasoning = _deepinfra_bool(item, metadata, "supports_reasoning", "reasoning")
        if supports_reasoning is None and re.search(r"(?:reasoning|think|thinking)", capability_text):
            supports_reasoning = True
        if supports_reasoning is None and "deepseek" in model_id.lower():
            supports_reasoning = True
        supports_image = _deepinfra_bool(
            item,
            metadata,
            "supports_image_in",
            "supports_image",
            "vision",
            "supports_vision",
        )
        supports_audio = _deepinfra_bool(
            item,
            metadata,
            "supports_audio_in",
            "supports_audio",
        )
        media = {}
        if supports_image is not None:
            media["image"] = supports_image
        elif re.search(r"(?:vision|visual|multimodal|image-understanding|ocr)", capability_text):
            media["image"] = True
        if supports_audio is not None:
            media["audio"] = supports_audio
        elif re.search(r"(?:audio|speech|whisper|transcrib)", capability_text):
            media["audio"] = True
        supports_tools = _deepinfra_bool(item, metadata, "supports_tools", "tools", "tool_calling")
        supports_response_format = _deepinfra_bool(
            item,
            metadata,
            "supports_response_format",
            "structured_output",
            "json_schema",
        )
        extra: Dict[str, Any] = {}
        if supports_tools is not None:
            extra["supports_tools"] = supports_tools
        if supports_response_format is not None:
            extra["supports_response_format"] = supports_response_format
        models.append(
            model_option(
                model_id,
                item.get("display_name") or item.get("label") or item.get("name") or model_id,
                context_length=item.get("context_length") or item.get("max_context_length") or metadata.get("context_length"),
                supports_reasoning=supports_reasoning,
                media=media,
                source="provider-api",
                source_label="API取得",
                **extra,
            )
        )
    return models


def _openai_models(
    cfg: Any,
    fetch_json: Callable[..., Dict[str, Any]],
) -> List[Dict[str, Any]]:
    api_key = _config_get(cfg, "openai_api_key") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return []
    data = fetch_json(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=5.0,
    )
    allowed_prefixes = ("gpt-", "o3", "o4", "codex")
    models: List[Dict[str, Any]] = []
    raw_items = data.get("data", []) if isinstance(data, dict) else []
    if not isinstance(raw_items, list):
        raw_items = []
    for item in raw_items:
        if (
            not isinstance(item, dict)
            or not item.get("id")
            or not str(item["id"]).startswith(allowed_prefixes)
        ):
            continue
        # The official endpoint currently omits these fields, but compatible
        # proxies and cached responses may provide them.  Preserve that
        # metadata so the registry supplement does not overwrite an explicit
        # provider value.
        extra = {
            key: item[key]
            for key in (
                "context_window_tokens",
                "context_length",
                "max_context_length",
                "context_window",
                "max_output_tokens",
            )
            if item.get(key) is not None
        }
        models.append(
            model_option(
                item["id"],
                source="provider-api",
                source_label="API取得",
                **extra,
            )
        )
    return models


def _gemini_models(
    cfg: Any,
    fetch_json: Callable[..., Dict[str, Any]],
) -> List[Dict[str, Any]]:
    api_key = (
        _config_get(cfg, "gemini_api_key")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )
    if not api_key:
        return []
    query = urllib.parse.urlencode({"key": api_key})
    data = fetch_json(
        f"https://generativelanguage.googleapis.com/v1beta/models?{query}",
        timeout=5.0,
    )
    models = []
    for item in data.get("models", []):
        name = str(item.get("name") or "")
        if not name.startswith("models/"):
            continue
        model_id = name.split("/", 1)[1]
        methods = item.get("supportedGenerationMethods") or []
        if methods and "generateContent" not in methods:
            continue
        models.append(
            model_option(
                model_id,
                item.get("displayName") or model_id,
                source="provider-api",
                source_label="API取得",
            )
        )
    return models


def _sglang_models(
    cfg: Any,
    fetch_json: Callable[..., Dict[str, Any]],
) -> List[Dict[str, Any]]:
    base_url = resolve_sglang_base_url(cfg)
    try:
        data = fetch_json(f"{base_url}/models", timeout=1.5)
    except Exception:
        return []
    return [
        model_option(item.get("id"), source="service-api", source_label="API取得")
        for item in data.get("data", [])
        if isinstance(item, dict) and item.get("id")
    ]


def _enterprise_sglang_model(cfg: Any) -> str:
    """Return the single model that the Enterprise SGLang service serves."""
    try:
        from src.llm.sglang_url import enterprise_sglang_model

        return enterprise_sglang_model(cfg)
    except RuntimeError:
        # The central Enterprise/profile boundary could not be established;
        # do not advertise arbitrary SGLang models from a failed lookup.
        raise


def _openai_compatible_local_models(
    cfg: Any,
    fetch_json: Callable[..., Dict[str, Any]],
) -> List[Dict[str, Any]]:
    api_key = (
        _config_get(cfg, "openai_compatible_local.api_key")
        or _config_get(cfg, "openai_compatible_local_api_key")
        or os.environ.get("OPENAI_COMPATIBLE_LOCAL_API_KEY")
        or "dummy"
    )
    models: List[Dict[str, Any]] = []
    for base_url in openai_compatible_local_discovery_base_urls(cfg):
        try:
            data = fetch_json(
                f"{base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                timeout=1.5,
            )
        except Exception:
            continue
        for item in data.get("data", []):
            if not isinstance(item, dict) or not item.get("id"):
                continue
            model_id = item.get("id")
            llama_profile = llama_cpp_model_profile(str(model_id))
            profile = local_server_profile_for_model(str(model_id)) or {}
            server_meta = {
                key: value
                for key, value in profile.items()
                if key in {"server", "server_label"}
            }
            profile_metadata: Dict[str, Any] = {}
            if llama_profile:
                profile_metadata = {
                    "runtime": "llama_cpp",
                    "runtime_profile": llama_profile,
                    "details": dict(llama_profile),
                }
                if isinstance(llama_profile.get("mtp"), dict):
                    profile_metadata["mtp"] = dict(llama_profile["mtp"])
                capabilities = llama_cpp_profile_capabilities(
                    profile=llama_profile
                )
                if isinstance(capabilities, dict):
                    profile_metadata.update(
                        {
                            "supports_reasoning": bool(
                                capabilities.get("reasoning")
                            ),
                            "supports_tools": bool(capabilities.get("tools")),
                            "supports_media": bool(
                                isinstance(capabilities.get("media"), dict)
                                and any(capabilities["media"].values())
                            ),
                        }
                    )
                    media = capabilities.get("media")
                    if isinstance(media, dict):
                        profile_metadata["media"] = dict(media)
            models.append(
                model_option(
                    model_id,
                    source="service-api",
                    source_label="API取得",
                    base_url=base_url,
                    **profile_metadata,
                    **server_meta,
                )
            )
    return models


def provider_models(
    provider: str,
    cfg: Any,
    *,
    include_remote: bool = False,
    cached_models: Optional[List[Dict[str, Any]]] = None,
    ollama_model_manager: Any = None,
    fetch_json: Callable[..., Dict[str, Any]] = default_fetch_json,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    models = dedupe_models((cached_models or []) + _static_models(provider))
    remote_error = None

    if include_remote:
        try:
            if provider == "openrouter":
                remote_models = _openrouter_models(fetch_json)
                if remote_models:
                    models = dedupe_models(remote_models + _static_models(provider))
            elif provider == "kimi":
                models = dedupe_models(_kimi_models(cfg, fetch_json) + models)
            elif provider == "deepseek":
                models = dedupe_models(_deepseek_models(cfg, fetch_json) + models)
            elif provider == "deepinfra":
                models = dedupe_models(_deepinfra_models(cfg, fetch_json) + models)
            elif provider == "openai":
                models = dedupe_models(_openai_models(cfg, fetch_json) + models)
            elif provider == "gemini":
                models = dedupe_models(_gemini_models(cfg, fetch_json) + models)
            elif provider == "sglang":
                models = dedupe_models(_sglang_models(cfg, fetch_json) + models)
            elif provider == "openai_compatible_local":
                models = dedupe_models(
                    _openai_compatible_local_models(cfg, fetch_json) + models
                )
            # CLI providers intentionally do not reuse generic provider APIs.
        except Exception as exc:
            remote_error = str(exc)

    if provider == "ollama" and ollama_model_manager is not None:
        try:
            installed = ollama_model_manager.list_models()
            installed_models = []
            for model_info in installed.get("models", []):
                name = model_info.get("name") or model_info.get("model")
                if _is_ollama_incompatible_model(name):
                    continue
                if name:
                    installed_models.append(
                        model_option(
                            name,
                            str(name),
                            installed=True,
                            size=model_info.get("size"),
                            details=model_info.get("details"),
                            source="installed",
                            source_label="インストール済み",
                        )
                    )
            models = dedupe_models(installed_models + models)
        except Exception as exc:
            remote_error = str(exc)

    if provider == "sglang":
        enterprise_model = _enterprise_sglang_model(cfg)
        if enterprise_model:
            models = [
                model
                for model in models
                if str(model.get("id") or model.get("model") or "").strip()
                == enterprise_model
            ]
            if not models:
                models = [
                    model_option(
                        enterprise_model,
                        enterprise_model,
                        source="provider-configured",
                        source_label="Enterprise設定",
                    )
                ]

    media_capabilities = {
        "openai": {"image": True, "audio": False},
        "gemini": {"image": True, "audio": True},
        "deepseek": {"image": False, "audio": False},
        # DeepInfra hosts heterogeneous models; dynamic per-model metadata is
        # merged below.  Static seed entries still carry their known media flags.
        "deepinfra": {},
        "codex-cli": {"image": True, "audio": False},
        "antigravity-cli": {"image": True, "audio": True},
        "claude": {"image": True, "audio": False},
        "grok": {"image": True, "audio": False},
    }.get(provider, {"image": False, "audio": False})
    annotated_models = [
        {
            **model,
            "media": {
                **media_capabilities,
                **(model.get("media") if isinstance(model.get("media"), dict) else {}),
            },
        }
        for model in dedupe_models(models)
    ]
    annotated_models = enrich_openai_model_context_metadata(provider, annotated_models)
    return sort_provider_models(provider, annotated_models), remote_error


_MTP_RUNTIME_FIELDS = (
    "mtp_enabled",
    "mtp_model_path",
    "mtp_supported",
    "mtp_available",
    "mtp_status",
    "mtp_reason",
    "mtp_artifact_path",
    "mtp_resolved_model_path",
    "mtp_mode",
)


def _llama_cpp_mtp_projection(
    canonical_runtime: Any,
    canonical_settings: Any,
    profile: Optional[Dict[str, Any]],
    *,
    external: bool = False,
) -> Dict[str, Any]:
    """Merge profile MTP metadata with the resolver's canonical state.

    Runtime versions may expose the computed fields at the top-level result,
    inside ``settings``, or (during a rolling upgrade) only expose the profile
    contract.  Prefer explicit resolver values, preserving ``False`` and
    empty-string clears, then fall back to profile defaults without making
    artifact availability a prerequisite for the base runtime.
    """

    runtime = canonical_runtime if isinstance(canonical_runtime, dict) else {}
    settings = canonical_settings if isinstance(canonical_settings, dict) else {}
    profile_mtp = profile.get("mtp") if isinstance(profile, dict) else None
    profile_mtp = profile_mtp if isinstance(profile_mtp, dict) else {}

    def _pick(key: str, default: Any = None) -> Any:
        for source in (runtime, settings):
            if key in source and source[key] is not None:
                return source[key]
        return default

    if external:
        external_status = str(runtime.get("mtp_status") or "not_applicable")
        external_reason = str(runtime.get("mtp_reason") or "").strip()
        return {
            "mtp_enabled": False,
            "mtp_model_path": "",
            "mtp_supported": False,
            "mtp_available": False,
            "mtp_status": external_status,
            "mtp_reason": external_reason
            or "local-modelは外部OpenAI互換serverのため、AoiTalkはMTPを管理しません。",
            "mtp_artifact_path": "",
            "mtp_resolved_model_path": "",
            "mtp_mode": "unavailable",
        }

    supported_value = _pick("mtp_supported", profile_mtp.get("supported", False))
    supported = bool(supported_value)
    default_enabled = bool(profile_mtp.get("default_enabled", False))
    enabled_value = _pick("mtp_enabled", default_enabled if supported else False)
    enabled = bool(enabled_value)
    artifact_path = _pick("mtp_artifact_path", None)
    if artifact_path is None:
        artifact_path = _pick("mtp_model_path", "")
    artifact_path = str(artifact_path or "")
    resolved_artifact_path = _pick("mtp_resolved_model_path", artifact_path)
    resolved_artifact_path = str(resolved_artifact_path or "")
    mode = str(_pick("mtp_mode", profile_mtp.get("mode", "unavailable")) or "unavailable")
    model_path = _pick("mtp_model_path", "")
    model_path = str(model_path or "")
    # A stale user path is not meaningful for embedded or explicitly
    # unavailable profiles. Keep it out of the catalog/UI projection so a
    # Qwen profile switch cannot make an incompatible artifact look selected.
    if not supported or mode != "companion":
        model_path = ""
        if mode != "companion":
            artifact_path = ""
            resolved_artifact_path = ""

    available_value = _pick("mtp_available", None)
    status_value = _pick("mtp_status", None)
    reason_value = _pick("mtp_reason", None)
    available = bool(available_value) if available_value is not None else False
    if available_value is None:
        status_text = str(status_value or "").strip().lower()
        available = status_text in {"available", "ready", "enabled"}
        if artifact_path and enabled and supported and not status_text:
            # A resolver that predates explicit availability can still expose
            # a resolved artifact path as an affirmative signal.
            available = True

    status = str(status_value or "").strip()
    if not status:
        if not supported:
            status = "unsupported"
        elif not enabled:
            status = "disabled"
        elif available:
            status = "available"
        else:
            status = "unavailable"

    reason = reason_value
    if reason is None or str(reason).strip() == "":
        reason = profile_mtp.get("reason") or profile_mtp.get("ui_notice")
    reason = str(reason).strip() if reason is not None else None
    return {
        "mtp_enabled": enabled,
        "mtp_model_path": model_path,
        "mtp_supported": supported,
        "mtp_available": available,
        "mtp_status": status,
        "mtp_reason": reason,
        "mtp_artifact_path": artifact_path,
        "mtp_resolved_model_path": resolved_artifact_path,
        "mtp_mode": mode,
    }


def _llama_cpp_catalog_settings(cfg: Any, model: Optional[str] = None) -> Dict[str, Any]:
    """Expose the persisted llama.cpp contract to the settings UI."""

    # Keep catalog/UI metadata on the same resolver contract used by engine
    # switching and session targets.  The import is intentionally lazy to
    # avoid coupling the service-manager package to catalog construction at
    # module import time.
    from src.service_manager._local_llm_servers import resolve_llama_cpp_runtime

    canonical_runtime = resolve_llama_cpp_runtime(cfg, model=model)
    canonical_settings = canonical_runtime.get("settings")
    canonical_settings = (
        canonical_settings if isinstance(canonical_settings, dict) else {}
    )

    raw = _config_get(cfg, "openai_compatible_local.llama_cpp", {})
    raw = dict(raw) if isinstance(raw, dict) else {}
    selected_model = str(
        model
        or _config_get(cfg, "openai_compatible_local.model", "")
        or _config_get(cfg, "llm_model", "")
        or ""
    ).strip()
    model_profile = llama_cpp_model_profile(selected_model)
    external_local_model = selected_model.casefold() == "local-model"
    # A registered profile describes request capabilities and launch defaults,
    # but it does not by itself establish process ownership.  In particular,
    # an Enterprise operator-owned router deliberately selects a known Qwen or
    # Gemma profile while setting ``auto_start=false``.  Keep ownership on the
    # central provider/runtime contract so catalog projections cannot redirect
    # the configured endpoint to llama.cpp's managed host/port.
    ownership = provider_runtime_ownership(
        "openai_compatible_local",
        cfg,
        model=selected_model,
    )
    managed_runtime = bool(ownership.managed_runtime)
    runtime_declared = bool(
        not external_local_model and llama_cpp_runtime_declared(raw)
    )
    if external_local_model:
        # ``local-model`` is an operator-owned endpoint.  Keep stale managed
        # keys out of the catalog/UI projection so a profile switch cannot
        # make the external server look launchable by AoiTalk.
        canonical_settings = {
            **canonical_settings,
            "model_path": "",
            "model_alias": "",
            "executable": "",
        }

    # Preserve explicit persisted input values when talking to an older
    # resolver that has not yet added the MTP fields to its canonical result.
    # The current resolver wins whenever it supplies a field, including an
    # explicit ``False`` or an empty artifact path.
    mtp_resolution_settings = dict(canonical_settings)
    # Only the toggle is user-persisted. The artifact path is resolver output
    # and must not be resurrected from stale raw configuration.
    for key in ("mtp_enabled",):
        if key in raw:
            mtp_resolution_settings[key] = raw[key]
    mtp_projection = _llama_cpp_mtp_projection(
        canonical_runtime,
        mtp_resolution_settings,
        model_profile,
        external=external_local_model,
    )

    def _value(key: str, default: Any) -> Any:
        env_names = {
            "executable": ("LLAMA_CPP_EXECUTABLE", "LLAMA_SERVER_EXE"),
            "model_path": ("LLAMA_CPP_MODEL_PATH",),
            "model_alias": ("LLAMA_CPP_MODEL_ALIAS",),
            "host": ("LLAMA_CPP_HOST",),
            "port": ("LLAMA_CPP_PORT",),
            "context_size": ("LLAMA_CPP_CONTEXT_SIZE",),
            "gpu_layers": ("LLAMA_CPP_GPU_LAYERS",),
            "extra_args": ("LLAMA_CPP_EXTRA_ARGS",),
            "auto_start": ("LLAMA_CPP_AUTO_START",),
            "readiness_timeout": ("LLAMA_CPP_READINESS_TIMEOUT",),
        }.get(key, ())
        if key == "model_path" and llama_cpp_profile_legacy_kind(model_profile) == "muse":
            env_names = (*env_names, "MUSE_GLIMMER_MODEL_PATH")
        env_value = next(
            (
                os.getenv(name)
                for name in env_names
                if os.getenv(name) is not None and str(os.getenv(name)).strip() != ""
            ),
            None,
        )
        if env_value is not None:
            return env_value
        raw_value = raw.get(key)
        if raw_value is None or (isinstance(raw_value, str) and raw_value.strip() == ""):
            return default
        return raw_value

    host = str(_value("host", LLAMA_CPP_DEFAULT_HOST) or LLAMA_CPP_DEFAULT_HOST)
    base_host = "127.0.0.1" if host in {"0.0.0.0", "::", "[::]", "::0"} else host
    try:
        port = int(_value("port", LLAMA_CPP_DEFAULT_PORT))
    except (TypeError, ValueError):
        port = LLAMA_CPP_DEFAULT_PORT
    if not (1 <= port <= 65535):
        port = LLAMA_CPP_DEFAULT_PORT
    base_host_for_url = (
        base_host if ":" not in base_host or base_host.startswith("[") else f"[{base_host}]"
    )
    base_url = f"http://{base_host_for_url}:{port}/v1"
    if not managed_runtime or (not model_profile and not runtime_declared):
        # A local-model selection is an external OpenAI-compatible endpoint;
        # preserve its configured URL instead of exposing the llama.cpp
        # host/port defaults merely because the nested mapping has ``None``
        # placeholders left by a profile switch.  The same rule applies to a
        # known profile attached to an operator-owned router (auto_start=false)
        # because profile metadata must never imply process ownership.
        base_url = openai_compatible_local_base_url(cfg, model=selected_model)
    context_size = _value(
        "context_size",
        int(model_profile.get("default_context_size"))
        if model_profile and model_profile.get("default_context_size")
        else LLAMA_CPP_DEFAULT_CONTEXT_SIZE,
    )
    gpu_layers = _value("gpu_layers", LLAMA_CPP_DEFAULT_GPU_LAYERS)
    timeout = _value("readiness_timeout", LLAMA_CPP_DEFAULT_READINESS_TIMEOUT)
    try:
        context_size = int(context_size)
    except (TypeError, ValueError):
        context_size = LLAMA_CPP_DEFAULT_CONTEXT_SIZE
    try:
        gpu_layers = int(gpu_layers)
    except (TypeError, ValueError):
        gpu_layers = LLAMA_CPP_DEFAULT_GPU_LAYERS
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = LLAMA_CPP_DEFAULT_READINESS_TIMEOUT
    auto_start = _value("auto_start", True)
    if isinstance(auto_start, str):
        auto_start = auto_start.strip().lower() in {"1", "true", "yes", "on"}
    else:
        auto_start = bool(auto_start)
    extra_args = _value(
        "extra_args",
        list(model_profile.get("default_args") or []) if model_profile else [],
    )
    if isinstance(extra_args, str):
        extra_args = [extra_args]
    elif not isinstance(extra_args, list):
        extra_args = list(extra_args) if isinstance(extra_args, tuple) else []
    model_alias = str(
        _value(
            "model_alias",
            str(model_profile.get("served_alias") or selected_model)
            if model_profile
            else selected_model,
        )
        or (str(model_profile.get("served_alias") or selected_model) if model_profile else selected_model)
    )
    profile_details = dict(model_profile or {})
    effort_metadata = llama_cpp_reasoning_effort_metadata(profile=model_profile)
    effective_effort = (
        str(canonical_settings.get("reasoning_effort") or "").strip().lower()
        if effort_metadata
        else ""
    )
    if effort_metadata and effective_effort not in effort_metadata["options"]:
        effective_effort = str(effort_metadata["default"])
    legacy_kind = llama_cpp_profile_legacy_kind(model_profile)
    return {
        "runtime": "llama_cpp"
        if managed_runtime and (model_profile or runtime_declared)
        else "external",
        "server_profile": "llama.cpp" if (model_profile or runtime_declared) else "custom",
        "process_owner": ownership.process_owner,
        "managed_runtime": managed_runtime,
        "ownership": ownership.to_dict(),
        "runtime_profile": model_profile,
        "reasoning_effort": effective_effort or None,
        "reasoning_effort_options": list(effort_metadata["options"])
        if effort_metadata
        else [],
        "reasoning_effort_default": effort_metadata["default"]
        if effort_metadata
        else None,
        "reasoning_effort_supports_disable": bool(effort_metadata["supports_disable"])
        if effort_metadata
        else False,
        "reasoning_effort_wire": (
            dict(effort_metadata["wire"])
            if effort_metadata and isinstance(effort_metadata.get("wire"), dict)
            else None
        ),
        "runtime_state": str(canonical_runtime.get("state") or "unmanaged"),
        "runtime_error": canonical_runtime.get("error"),
        "model_path_source": str(canonical_runtime.get("model_path_source") or "missing"),
        "model_path_status": str(canonical_runtime.get("model_path_status") or "not_applicable"),
        "executable_status": str(canonical_runtime.get("executable_status") or "not_applicable"),
        "minimum_build": canonical_runtime.get("minimum_build"),
        "base_url": base_url,
        "executable": str(canonical_settings.get("executable", _value("executable", "")) or ""),
        "model_path": str(canonical_settings.get("model_path", _value("model_path", "")) or ""),
        "model_alias": str(canonical_settings.get("model_alias", model_alias) or model_alias),
        "host": str(canonical_settings.get("host", host) or host),
        "port": canonical_settings.get("port", port),
        "context_size": canonical_settings.get("context_size", context_size),
        "gpu_layers": canonical_settings.get("gpu_layers", gpu_layers),
        "extra_args": [str(item) for item in canonical_settings.get("extra_args", extra_args)],
        "auto_start": bool(canonical_settings.get("auto_start", auto_start)),
        "readiness_timeout": canonical_settings.get("readiness_timeout", timeout),
        "readiness_timeout_seconds": canonical_settings.get("readiness_timeout", timeout),
        **mtp_projection,
        "profile_id": str(profile_details.get("id") or ""),
        "served_alias": str(profile_details.get("served_alias") or ""),
        "mtp": (
            dict(profile_details.get("mtp"))
            if isinstance(profile_details.get("mtp"), dict)
            else None
        ),
        "alias_locked": bool(profile_details.get("alias_locked")),
        "official_filename": str(
            profile_details.get("gguf_filename")
            or profile_details.get("official_filename")
            or ""
        ),
        "model_filename": str(
            profile_details.get("gguf_filename")
            or profile_details.get("model_filename")
            or ""
        ),
        "filename": str(
            profile_details.get("gguf_filename")
            or profile_details.get("filename")
            or ""
        ),
        "quantization": profile_details.get("quantization"),
        "native_context_length": profile_details.get("native_context_size")
        or profile_details.get("native_context_length"),
        "native_context_size": profile_details.get("native_context_size"),
        "gguf_filename": profile_details.get("gguf_filename"),
        "source_repository": profile_details.get("source_repository"),
        "source_url": profile_details.get("source_url"),
        "minimum_llama_cpp_build": profile_details.get("minimum_llama_cpp_build"),
        "reasoning_tools_minimum_llama_cpp_build": profile_details.get(
            "reasoning_tools_minimum_llama_cpp_build"
        ),
        "required_args": list(profile_details.get("required_args") or []),
        "jinja_required": bool(profile_details.get("jinja_required")),
        "supports_reasoning": bool(
            profile_details.get("supports_reasoning")
            if "supports_reasoning" in profile_details
            else (
                profile_details.get("capabilities", {}).get("reasoning")
                if isinstance(profile_details.get("capabilities"), dict)
                else False
            )
        ),
        "supports_tools": bool(
            profile_details.get("supports_tools")
            if "supports_tools" in profile_details
            else (
                profile_details.get("capabilities", {}).get("tools")
                if isinstance(profile_details.get("capabilities"), dict)
                else False
            )
        ),
        "supports_media": bool(
            profile_details.get("supports_media")
            if "supports_media" in profile_details
            else (
                any(profile_details.get("capabilities", {}).get("media", {}).values())
                if isinstance(profile_details.get("capabilities", {}).get("media"), dict)
                else False
            )
        ),
        # Legacy names remain readable for existing clients only when the
        # corresponding profile exists; never leak Muse values to another ID.
        "muse_model_filename": (
            str(
                profile_details.get("gguf_filename")
                or profile_details.get("filename")
                or ""
            )
            if legacy_kind == "muse"
            else ""
        ),
        "muse_minimum_llama_cpp_build": (
            profile_details.get("minimum_llama_cpp_build")
            if legacy_kind == "muse"
            else None
        ),
    }


def provider_settings(
    provider: str,
    cfg: Any,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    if provider == "openai_compatible_local":
        api_key = (
            _config_get(cfg, "openai_compatible_local.api_key")
            or _config_get(cfg, "openai_compatible_local_api_key")
            or os.environ.get("OPENAI_COMPATIBLE_LOCAL_API_KEY")
            or ""
        )
        runtime_settings = _llama_cpp_catalog_settings(cfg, model)
        selected_runtime_model = str(
            model
            or _config_get(cfg, "openai_compatible_local.model", "")
            or _config_get(cfg, "llm_model", "")
            or ""
        ).strip().casefold()
        selected_profile = llama_cpp_model_profile(model or selected_runtime_model)
        ownership = provider_runtime_ownership(
            "openai_compatible_local",
            cfg,
            model=model or selected_runtime_model,
        )
        # ``managed_runtime`` is the sole lifecycle/endpoint ownership gate.
        # A known profile remains useful for capability and request metadata,
        # even when its endpoint is owned by an external router.
        runtime_owned = bool(ownership.managed_runtime)
        effort_metadata = llama_cpp_reasoning_effort_metadata(profile=selected_profile)
        configured_tools = bool(
            _config_get(cfg, "openai_compatible_local.enable_tools", False)
        )
        if _llama_cpp_profile_capability(
            model or selected_runtime_model,
            "tools",
        ) is False:
            configured_tools = False
        return {
            "base_url": runtime_settings["base_url"]
            if runtime_owned
            else openai_compatible_local_base_url(cfg, model=model),
            "api_key_configured": bool(api_key),
            "api_key_placeholder": "dummy" if not api_key else "設定済み",
            "enable_tools": configured_tools,
            "enable_response_format": bool(
                _config_get(
                    cfg, "openai_compatible_local.enable_response_format", False
                )
            ),
            "enable_extra_body": bool(
                _config_get(cfg, "openai_compatible_local.enable_extra_body", False)
            ),
            "runtime": runtime_settings["runtime"],
            "runtime_settings": runtime_settings,
            "llama_cpp": runtime_settings,
            "runtime_profile": selected_profile,
            "process_owner": ownership.process_owner,
            "managed_runtime": runtime_owned,
            "ownership": ownership.to_dict(),
            # Keep the active selection's MTP projection available both in
            # the canonical llama_cpp settings object and at provider level
            # for clients that do not recursively inspect runtime_settings.
            "mtp": (
                dict(runtime_settings.get("mtp"))
                if isinstance(runtime_settings.get("mtp"), dict)
                else None
            ),
            **{
                key: runtime_settings.get(key)
                for key in _MTP_RUNTIME_FIELDS
            },
            "reasoning_effort": runtime_settings.get("reasoning_effort")
            if effort_metadata
            else None,
            "reasoning_effort_options": list(effort_metadata["options"])
            if effort_metadata
            else [],
            "reasoning_effort_default": effort_metadata["default"]
            if effort_metadata
            else None,
            "reasoning_effort_supports_disable": bool(effort_metadata["supports_disable"])
            if effort_metadata
            else False,
            "reasoning_effort_wire": (
                dict(effort_metadata["wire"])
                if effort_metadata and isinstance(effort_metadata.get("wire"), dict)
                else None
            ),
        }
    if provider == "ollama":
        return {
            "base_url": _config_get(cfg, "ollama.base_url", "http://127.0.0.1:11434/v1"),
            "api_key_configured": bool(_config_get(cfg, "ollama.api_key", "")),
            "api_key_placeholder": "ollama",
            "enable_tools": bool(_config_get(cfg, "ollama.enable_tools", False)),
        }
    if provider == "openrouter":
        api_key = _config_get(cfg, "openrouter_api_key") or os.environ.get(
            "OPENROUTER_API_KEY", ""
        )
        return {
            "base_url": _config_get(
                cfg, "openrouter.base_url", "https://openrouter.ai/api/v1"
            ),
            "api_key_configured": bool(api_key),
            "api_key_placeholder": "設定済み" if api_key else "",
            "enable_tools": True,
        }
    if provider == "deepseek":
        api_key = _config_get(cfg, "deepseek_api_key") or os.environ.get(
            "DEEPSEEK_API_KEY", ""
        )
        return {
            "base_url": (
                _config_get(cfg, "deepseek_base_url")
                or os.environ.get("DEEPSEEK_BASE_URL")
                or _config_get(cfg, "deepseek.base_url", "https://api.deepseek.com")
                or "https://api.deepseek.com"
            ),
            "api_key_configured": bool(api_key),
            "api_key_placeholder": "設定済み" if api_key else "",
            "reasoning_effort": _config_get(cfg, "deepseek.reasoning_effort", "high"),
            "reasoning_effort_options": ["none", "high", "max"],
            "enable_tools": True,
        }
    if provider == "deepinfra":
        api_key = _config_get(cfg, "deepinfra_api_key") or os.environ.get(
            "DEEPINFRA_TOKEN", ""
        )
        return {
            "base_url": (
                _config_get(cfg, "deepinfra.base_url")
                or _config_get(cfg, "deepinfra_base_url")
                or os.environ.get("DEEPINFRA_BASE_URL")
                or DEEPINFRA_CHAT_BASE_URL
            ),
            "api_key_configured": bool(api_key),
            "api_key_placeholder": "設定済み" if api_key else "",
            "reasoning_effort": _config_get(cfg, "deepinfra.reasoning_effort", "high"),
            "reasoning_effort_options": ["none", "low", "medium", "high"],
            "enable_tools": True,
        }
    if provider == "kimi":
        api_key = _config_get(cfg, "kimi_api_key") or os.environ.get(
            "MOONSHOT_API_KEY", ""
        )
        return {
            "base_url": _config_get(
                cfg, "kimi_base_url"
            ) or os.environ.get("MOONSHOT_BASE_URL") or _config_get(
                cfg, "kimi.base_url", "https://api.moonshot.ai/v1"
            ),
            "api_key_configured": bool(api_key),
            "api_key_placeholder": "設定済み" if api_key else "",
            "reasoning_effort": "max",
            "reasoning_effort_options": ["max"],
            "enable_tools": True,
        }
    if provider == "sglang":
        sglang_base_url = resolve_sglang_base_url(cfg)
        return {
            "base_url": sglang_base_url,
            "api_key_configured": bool(_config_get(cfg, "sglang_api_key", "")),
            "api_key_placeholder": "dummy",
            "enable_tools": True,
        }
    if provider == "codex-cli":
        selected_model = str(
            model
            or _config_get(cfg, "codex_cli.model", "gpt-5-codex")
            or "gpt-5-codex"
        )
        return {
            "reasoning_effort": _config_get(
                cfg, "codex_cli.reasoning_effort", "medium"
            ),
            "reasoning_effort_options": reasoning_effort_options_for_model(
                provider, selected_model
            ),
        }
    if provider == "claude-cli":
        return {
            "reasoning_effort": _config_get(
                cfg, "claude_cli.reasoning_effort", "medium"
            ),
            "reasoning_effort_options": CLAUDE_REASONING_EFFORT_OPTIONS,
        }
    if provider == "grok-cli":
        return {
            "api_key_configured": bool(os.environ.get("XAI_API_KEY")),
        }
    return {}


def _normalize_model_id_for_effort(model: str) -> str:
    value = str(model or "").strip().lower()
    if "/" in value and value.startswith("openai/"):
        value = value.split("/", 1)[1]
    return value


def _llama_cpp_profile_capability(
    model: str,
    capability: str,
) -> Optional[bool]:
    """Return an explicitly declared capability for a known llama profile.

    ``None`` is intentionally distinct from ``False``: an unknown/external
    local endpoint, and legacy profiles without a capability declaration,
    must retain the provider's historical behaviour.  Only an explicit
    profile value can disable a generic feature.
    """

    capabilities = llama_cpp_profile_capabilities(model)
    if not isinstance(capabilities, dict) or capability not in capabilities:
        return None
    return bool(capabilities.get(capability))


def _gpt5_minor_version(model_id: str) -> Optional[int]:
    match = re.match(r"^gpt-5\.(\d+)(?:[-.]|$)", model_id)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None
def _is_gpt5_pro_model(model_id: str) -> bool:
    return bool(re.search(r"-pro(?:-|$)", model_id))


def reasoning_effort_options_for_model(provider: str, model: str) -> List[str]:
    """Return user-selectable effort values for the active provider/model."""

    provider_id = str(provider or "").strip().lower()
    model_id = _normalize_model_id_for_effort(model)

    if provider_id == "codex-cli":
        if model_id == "gpt-5.6-sol" or model_id.startswith("gpt-5.6-sol-"):
            return CODEX_GPT56_SOL_REASONING_EFFORT_OPTIONS
        if model_id == "gpt-5.6-luna" or model_id.startswith("gpt-5.6-luna-"):
            return CODEX_GPT56_LUNA_REASONING_EFFORT_OPTIONS
        return CODEX_REASONING_EFFORT_OPTIONS
    if provider_id == "claude-cli":
        return CLAUDE_REASONING_EFFORT_OPTIONS
    if provider_id == "deepseek":
        return ["none", "high", "max"]
    if provider_id == "deepinfra":
        return ["none", "low", "medium", "high"]
    if provider_id == "kimi" and model_id == "kimi-k3":
        return ["max"]
    if provider_id == "openai":
        if "-codex" in model_id or model_id.startswith("codex"):
            return OPENAI_CODEX_REASONING_EFFORT_OPTIONS
        minor = _gpt5_minor_version(model_id)
        if _is_gpt5_pro_model(model_id):
            if model_id.startswith("gpt-5-pro"):
                return ["high"]
            if minor is not None and minor >= 2:
                return OPENAI_GPT52_PRO_REASONING_EFFORT_OPTIONS
        if minor == 6:
            return OPENAI_GPT56_REASONING_EFFORT_OPTIONS
        if model_id == "gpt-5.1":
            return OPENAI_GPT51_REASONING_EFFORT_OPTIONS
        if minor is not None and minor >= 2:
            return OPENAI_FULL_REASONING_EFFORT_OPTIONS
        if model_id == "gpt-5" or model_id.startswith("gpt-5-"):
            return OPENAI_GPT5_REASONING_EFFORT_OPTIONS
        if model_id.startswith("gpt-oss"):
            return OPENAI_STANDARD_REASONING_EFFORT_OPTIONS
    if provider_id == "ollama":
        return ollama_mode_options_for_model(model)
    if provider_id == "openai_compatible_local":
        # A known profile may explicitly declare that it has no reasoning
        # contract.  Do not expose the generic local fast/thinking fallback
        # for that profile; unknown external ``local-model`` keeps it.
        if _llama_cpp_profile_capability(model, "reasoning") is False:
            return []
        managed_options = llama_cpp_reasoning_effort_options(model)
        return managed_options or FAST_THINKING_MODE_OPTIONS
    if provider_id in {"gemini", "sglang"}:
        return FAST_THINKING_MODE_OPTIONS
    return []


def llm_mode_kind_for_provider(provider: str, model: str) -> str:
    provider_id = str(provider or "").strip().lower()
    if provider_id in {"codex-cli", "claude-cli"}:
        return "reasoning_effort"
    if provider_id in {"openai", "deepseek", "deepinfra", "kimi"} and reasoning_effort_options_for_model(provider, model):
        return "reasoning_effort"
    if provider_id == "openai_compatible_local":
        if _llama_cpp_profile_capability(model, "reasoning") is False:
            return "response_mode"
        if llama_cpp_reasoning_effort_metadata(model=model):
            return "reasoning_effort"
    return "response_mode"


def default_llm_mode_for_options(options: List[str]) -> str:
    if "medium" in options:
        return "medium"
    if "fast" in options:
        return "fast"
    return options[0] if options else "fast"


def reasoning_effort_default_for_model(provider: str, model: str) -> Optional[str]:
    """Return a profile/catalog default without changing persisted config."""

    if str(provider or "").strip().lower() == "openai_compatible_local":
        if _llama_cpp_profile_capability(model, "reasoning") is False:
            return None
        return llama_cpp_reasoning_effort_default(model)
    return None


def build_llm_mode_state(
    cfg: Any,
    *,
    client: Any = None,
) -> Dict[str, Any]:
    provider = str(_config_get(cfg, "llm_provider", "openai") or "openai")
    model = str(_config_get(cfg, "llm_model", "gpt-4o") or "gpt-4o")
    deployment = resolve_llm_deployment(cfg)
    if deployment is not None:
        provider = deployment.effective_provider
        model = deployment.effective_model
    options = reasoning_effort_options_for_model(provider, model)
    local_reasoning_disabled = (
        provider.strip().lower() == "openai_compatible_local"
        and _llama_cpp_profile_capability(model, "reasoning") is False
    )
    if not options and not local_reasoning_disabled:
        options = FAST_THINKING_MODE_OPTIONS

    kind = llm_mode_kind_for_provider(provider, model)
    if provider == "codex-cli":
        current = _config_get(cfg, "codex_cli.reasoning_effort", None)
    elif provider == "claude-cli":
        current = _config_get(cfg, "claude_cli.reasoning_effort", None)
    elif provider == "openai" and kind == "reasoning_effort":
        current = _config_get(cfg, "openai.reasoning_effort", None)
    elif provider == "deepseek" and kind == "reasoning_effort":
        current = _config_get(cfg, "deepseek.reasoning_effort", "high")
    elif provider == "deepinfra" and kind == "reasoning_effort":
        current = _config_get(cfg, "deepinfra.reasoning_effort", "high")
    elif provider == "openai_compatible_local" and kind == "reasoning_effort":
        current = _config_get(
            cfg,
            "openai_compatible_local.llama_cpp.reasoning_effort",
            None,
        )
    elif client is not None and hasattr(client, "get_llm_mode"):
        current = client.get_llm_mode()
    else:
        current = None

    mode = str(current or "").strip()
    if local_reasoning_disabled:
        # Keep the state explicit rather than inventing ``fast`` for a model
        # whose profile declares no reasoning/thinking capability.
        mode = ""
    elif mode not in options:
        mode = reasoning_effort_default_for_model(provider, model) or default_llm_mode_for_options(options)

    state = {
        "mode": mode,
        "available_modes": options,
        "labels": {value: LLM_MODE_LABELS.get(value, value) for value in options},
        "kind": kind,
        "provider": provider,
        "model": model,
    }
    if deployment is not None:
        state["deployment"] = deployment.metadata()
    return state


def _provider_source(models: List[Dict[str, Any]], refreshed: bool) -> str:
    sources = {str(model.get("source") or "") for model in models}
    if "provider-api" in sources or "service-api" in sources:
        return "remote"
    if "provider-cache" in sources:
        return "cached"
    if "installed" in sources:
        return "installed"
    if "cli-suggested" in sources:
        return "cli-suggested"
    if "platform-suggested" in sources:
        return "platform-suggested"
    return "static" if not refreshed else "static-suggested"


def provider_saved_model(provider: str, cfg: Any) -> Optional[str]:
    current_p = _config_get(cfg, "llm_provider", "openai")
    current_m = _config_get(cfg, "llm_model", "gpt-4o")
    if provider == current_p and current_m:
        return str(current_m)

    for key in PROVIDER_MODEL_CONFIG_KEYS.get(provider, ()):
        value = _config_get(cfg, key)
        if value:
            return str(value)

    return None


def agent_team_models_by_provider(cfg: Any) -> Dict[str, List[Dict[str, str]]]:
    result: Dict[str, List[Dict[str, str]]] = {}
    for subagent in agent_team_v3_subagents(cfg, include_disabled=False):
        subagent_id = str(subagent.get("subagent_id") or "").strip()
        if not subagent_id:
            continue
        route = resolve_agent_team_v3_route(cfg, subagent_id) or {}
        provider = str(route.get("provider") or "").strip()
        model = str(route.get("model") or "").strip()
        if not provider or not model:
            continue
        result.setdefault(provider, []).append(
            {
                "subagent_id": subagent_id,
                "name": str(subagent.get("name") or subagent_id),
                "model": model,
                "llm_profile_id": None,
                "execution_profile_id": str(route.get("execution_profile_id") or "") or None,
            }
        )
    return result


def build_model_catalog(
    cfg: Any,
    *,
    ollama_model_manager: Any = None,
    include_remote: bool = False,
    refresh_provider: Optional[str] = None,
    cached_catalog: Optional[Dict[str, Any]] = None,
    fetch_json: Callable[..., Dict[str, Any]] = default_fetch_json,
) -> Dict[str, Any]:
    current_p = _config_get(cfg, "llm_provider", "openai")
    current_m = _config_get(cfg, "llm_model", "gpt-4o")
    deployment = resolve_llm_deployment(cfg)
    team_models = agent_team_models_by_provider(cfg)
    providers = []
    refresh_target = (refresh_provider or "").strip() or None

    for provider in PROVIDER_ORDER:
        provider_available = True
        availability_reason: Optional[str] = None
        if deployment is not None:
            provider_available, availability_reason = deployment.provider_available(provider)

        should_refresh = (
            provider_available
            and include_remote
            and (refresh_target is None or refresh_target == provider)
        )
        fixed_provider = bool(
            deployment is not None
            and deployment.fixed
            and provider == deployment.effective_provider
        )
        if not provider_available:
            # Do not even call provider_models for blocked providers.  This is
            # important for an Enterprise deployment where stale ``sglang``
            # configuration must not result in a /models request.
            models, error = [], None
        elif fixed_provider:
            models, error = [
                model_option(
                    deployment.effective_model,
                    deployment.effective_model,
                    source="deployment-configured",
                    source_label="Enterprise deployment",
                    provider_configured=True,
                )
            ], None
            should_refresh = False
        else:
            models, error = provider_models(
                provider,
                cfg,
                include_remote=should_refresh,
                cached_models=cached_provider_models(provider, cached_catalog),
                ollama_model_manager=ollama_model_manager,
                fetch_json=fetch_json,
            )
        saved_model = provider_saved_model(provider, cfg) if provider_available else None
        enterprise_model = (
            enterprise_sglang_model(cfg)
            if provider == "sglang" and deployment is None
            else ""
        )
        if fixed_provider and deployment is not None:
            saved_model = deployment.effective_model
            enterprise_model = ""
        if enterprise_model:
            # Stale DB/team values must never be reinserted after the provider
            # list has been narrowed to the model actually served by SGLang.
            saved_model = enterprise_model
        if provider == "ollama" and _is_ollama_incompatible_model(saved_model):
            saved_model = None
        if saved_model and not any(m["id"] == saved_model for m in models):
            server_profile = (
                local_server_profile_for_model(saved_model)
                if provider == "openai_compatible_local"
                else None
            ) or {}
            models.insert(
                0,
                model_option(
                    saved_model,
                    saved_model,
                    **server_profile,
                    custom_current=provider == current_p,
                    provider_configured=True,
                    source="provider-configured",
                    source_label="現在の設定" if provider == current_p else "保存済み設定",
                ),
            )
        configured_subagents = (
            []
            if enterprise_model or fixed_provider or not provider_available
            else team_models.get(provider, [])
        )
        for configured_subagent in reversed(configured_subagents):
            configured_model = configured_subagent["model"]
            if any(m["id"] == configured_model for m in models):
                continue
            subagent_id = str(configured_subagent.get("subagent_id") or "").strip()
            subagent_label = str(configured_subagent.get("name") or subagent_id)
            models.insert(
                0,
                model_option(
                    configured_model,
                    configured_model,
                    custom_current=True,
                    source="agent-team-configured",
                    source_label=f"Agent Team Subagent: {subagent_label}",
                ),
            )
        configured_provider_model = saved_model
        if not configured_provider_model and models:
            configured_provider_model = str(models[0]["id"])
        models = enrich_openai_model_context_metadata(provider, models)
        models = enrich_model_reasoning_options(provider, models)
        settings = provider_settings(provider, cfg, configured_provider_model)
        if fixed_provider and deployment is not None:
            settings = dict(settings)
            settings.update(
                {
                    "base_url": deployment.effective_base_url,
                    "server_profile": deployment.server_profile,
                    "enable_tools": deployment.tool_capability,
                }
            )
        provider_payload = {
            "id": provider,
            "label": LLM_PROVIDER_LABELS.get(provider, provider),
            "models": models,
            "configured_model": configured_provider_model or "",
            "supports_custom_model": not (
                (provider == "sglang" and bool(enterprise_model))
                or fixed_provider
                or not provider_available
            ),
            "capabilities": PROVIDER_CAPABILITIES.get(
                provider, ProviderCapabilities()
            ).to_dict(),
            "settings": settings,
            "source": _provider_source(models, should_refresh),
            "refreshed": should_refresh,
            "cached_at": cached_provider_updated_at(provider, cached_catalog),
            "error": error,
        }
        if deployment is not None:
            provider_payload.update(
                {
                    "available": provider_available,
                    "unavailable": not provider_available,
                    "availability_reason": availability_reason,
                }
            )
        ollama_status = None
        if provider == "ollama" and ollama_model_manager is not None:
            status_getter = getattr(ollama_model_manager, "status", None)
            if callable(status_getter):
                try:
                    ollama_status = status_getter()
                except Exception:
                    ollama_status = None
        provider_payload = enrich_provider_payload(
            provider_payload,
            cfg,
            ollama_status=ollama_status,
        )
        providers.append(provider_payload)
    routing_provider_payload = {
        "id": "routing-profile",
        "label": "AoiTalk",
        "disabled": not bool(
            _config_get(cfg, "routing_profiles.free-team.enabled", True)
        ),
        "selection_kind": "routing_profile",
        "models": [
            model_option(
                "free-team",
                "無料Team",
                description="複数の無料API枠・プロモーションクレジット・CLI枠を安全に自動使用します",
                selection_kind="routing_profile",
                routing_profile_id="free-team",
            )
        ],
        "configured_model": "free-team",
        "supports_custom_model": False,
        "capabilities": ProviderCapabilities(
            supports_stream=True,
            supports_tools=True,
            supports_response_format=True,
            supports_model_pull=False,
            supports_model_delete=False,
            supports_extra_body=False,
        ).to_dict(),
        "settings": {},
        "source": "virtual",
        "refreshed": False,
        "cached_at": None,
        "error": None,
    }
    if deployment is not None:
        routing_available, routing_reason = deployment.provider_available("routing-profile")
        routing_provider_payload.update(
            {
                "available": routing_available,
                "unavailable": not routing_available,
                "availability_reason": routing_reason,
            }
        )
    providers.insert(0, routing_provider_payload)
    result = {
        "current": {"provider": current_p, "model": current_m},
        "providers": providers,
    }
    if deployment is not None:
        result["deployment"] = deployment.metadata()
    return result


def _has_provider_key(cfg: Any, provider: str) -> bool:
    key_map = {
        "gemini": ("gemini_api_key", "GEMINI_API_KEY"),
        "openai": ("openai_api_key", "OPENAI_API_KEY"),
        "openrouter": ("openrouter_api_key", "OPENROUTER_API_KEY"),
        "deepseek": ("deepseek_api_key", "DEEPSEEK_API_KEY"),
        "deepinfra": ("deepinfra_api_key", "DEEPINFRA_TOKEN"),
        "kimi": ("kimi_api_key", "MOONSHOT_API_KEY"),
    }
    config_key, env_key = key_map.get(provider, ("", ""))
    if not config_key:
        return True
    return bool(_config_get(cfg, config_key) or os.environ.get(env_key, ""))


def configured_provider_model(
    provider: str,
    cfg: Any,
    models: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Return the one model currently assigned to a provider for header switching."""

    current_p = _config_get(cfg, "llm_provider", "openai")
    current_m = _config_get(cfg, "llm_model", "gpt-4o")
    if provider == current_p and current_m:
        if provider == "ollama" and _is_ollama_incompatible_model(current_m):
            return ""
        return str(current_m)

    for key in PROVIDER_MODEL_CONFIG_KEYS.get(provider, ()):
        value = _config_get(cfg, key)
        if value:
            if provider == "ollama" and _is_ollama_incompatible_model(value):
                continue
            return str(value)

    for model in models or []:
        model_id = str(model.get("id") or model.get("model") or "").strip()
        if model_id:
            return model_id

    return "default"


def header_engine_label(provider: str, model: str) -> str:
    if provider == "routing-profile" and model == "free-team":
        return "無料Team"
    provider_label = LLM_PROVIDER_LABELS.get(provider, provider)
    return f"{provider_label} ({model})" if model else provider_label


def build_engine_options(
    cfg: Any,
    *,
    ollama_model_manager: Any = None,
) -> List[Dict[str, str]]:
    """Build compact header options: one selectable item per provider."""

    catalog = build_model_catalog(
        cfg,
        ollama_model_manager=ollama_model_manager,
        include_remote=False,
    )
    current_p = _config_get(cfg, "llm_provider", "openai")
    current_m = _config_get(cfg, "llm_model", "gpt-4o")
    deployment = resolve_llm_deployment(cfg)
    result = []

    for provider in catalog["providers"]:
        provider_id = provider["id"]
        if deployment is not None:
            available, availability_reason = deployment.provider_available(provider_id)
            if not available:
                # Header options are actionable choices, not diagnostics.  A
                # blocked provider remains visible in the full catalog with an
                # availability reason, but must not be selectable here.
                continue
        if provider_id in {"openai", "gemini", "openrouter", "deepseek", "deepinfra", "kimi"} and not _has_provider_key(
            cfg,
            provider_id,
        ):
            continue

        models = provider.get("models") or []
        model_id = configured_provider_model(provider_id, cfg, models)
        option = {
                "provider": provider_id,
                "model": model_id,
                "label": header_engine_label(provider_id, model_id),
            }
        if deployment is not None:
            option["available"] = True
            option["availability_reason"] = None
        result.append(option)

    current_model_supported = not (
        current_p == "ollama" and _is_ollama_incompatible_model(current_m)
    )
    deployment = resolve_llm_deployment(cfg)
    if deployment is not None and current_p not in deployment.allowed_provider_ids:
        current_model_supported = False
    if current_model_supported and not any(
        option["provider"] == current_p and option["model"] == current_m
        for option in result
    ):
        result.insert(
            0,
            {
                "provider": str(current_p),
                "model": str(current_m),
                "label": header_engine_label(str(current_p), str(current_m)),
            },
        )

    return result
