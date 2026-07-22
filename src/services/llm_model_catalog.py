"""LLM model catalog helpers for settings UI."""

from __future__ import annotations

import json
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
from src.llm.openai_compatible_local_profiles import (
    macos_openai_compatible_local_model_options,
    local_server_profile_for_model,
    openai_compatible_local_base_url,
    openai_compatible_local_discovery_base_urls,
)
from src.services.agent_team_service import (
    AGENT_TEAM_MEMBER_KEYS,
    AGENT_TEAM_MEMBER_LABELS,
    agent_team_members_by_provider,
)

LLM_PROVIDER_LABELS = {
    "gemini": "Gemini API",
    "openai": "OpenAI API",
    "openrouter": "OpenRouter",
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
    "ollama": [
        {"id": "gpt-oss:20b", "label": "gpt-oss:20b"},
        {"id": "gpt-oss:120b", "label": "gpt-oss:120b"},
        {"id": "qwen3:32b", "label": "qwen3:32b"},
        {"id": "deepseek-r1:14b", "label": "deepseek-r1:14b"},
        {"id": "llama3.2:3b", "label": "llama3.2:3b"},
        {"id": "gemma3:12b", "label": "gemma3:12b"},
        {"id": "gemma4:e4b", "label": "gemma4:e4b"},
    ],
    "sglang": [
        {"id": "openai/gpt-oss-20b", "label": "openai/gpt-oss-20b"},
        {"id": "openai/gpt-oss-120b", "label": "openai/gpt-oss-120b"},
        {"id": "deepseek-ai/DeepSeek-R1", "label": "DeepSeek-R1"},
        {"id": "Qwen/Qwen3-32B", "label": "Qwen3-32B"},
        {
            "id": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            "label": "Qwen3 Coder 30B A3B Instruct",
        },
        {"id": "meta-llama/Llama-3.3-70B-Instruct", "label": "Llama 3.3 70B Instruct"},
        {"id": "default", "label": "default"},
    ],
    "openai_compatible_local": [
        {
            "id": "local-model",
            "label": "カスタムローカルサーバー",
            "description": "AoiTalkでは自動起動せず、Base URLに入力した起動済みOpenAI互換サーバーへ接続",
        },
        {
            "id": "qwopus3.6-35b-a3b",
            "label": "Qwopus3.6-35B-A3B Q4_K_M",
            "description": "任意のローカルGGUFを llama-server で起動する OpenAI 互換ローカル用",
        },
        {
            "id": "qwen3.6-27b-dflash",
            "label": "Qwen3.6-27B (DFlash)",
            "description": "DFlash 対応 llama-server 側のモデルID例",
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
    ),
    "kimi": ProviderCapabilities(
        supports_stream=True,
        supports_tools=True,
        supports_response_format=False,
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
    "kimi",
    "sglang",
    "openai_compatible_local",
}

REMOTE_MODEL_SOURCES = {"provider-api", "service-api"}

DEFAULT_MODEL_CATALOG_CACHE_PATH = Path("cache") / "llm_model_catalog.json"

CODEX_REASONING_EFFORT_OPTIONS = ["low", "medium", "high", "xhigh"]
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
}

OLLAMA_INCOMPATIBLE_MODEL_IDS = {
    "hf.co/jackrong/qwopus3.6-35b-a3b-v1-gguf:q4_k_m",
}


LOCAL_MODEL_LABELS = {
    "qwopus3.6-35b-a3b": "Qwopus3.6-35B-A3B Q4_K_M",
    "luce-dflash": "Qwen3.6-27B (Luce DFlash server alias)",
    "qwen3.6-27b": "Qwen3.6-27B",
    "qwen3.6-27b-dflash": "Qwen3.6-27B (DFlash)",
}

AGENT_TEAM_MEMBER_LABEL_BY_KEY = AGENT_TEAM_MEMBER_LABELS


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
    if hasattr(config, "get"):
        value = config.get(key, None)
        if value is not None:
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
        catalog_items = macos_openai_compatible_local_model_options() + catalog_items

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
    return [
        model_option(item["id"], source="provider-api", source_label="API取得")
        for item in data.get("data", [])
        if isinstance(item, dict)
        and item.get("id")
        and str(item["id"]).startswith(allowed_prefixes)
    ]


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
    sglang_config = _config_get(cfg, "sglang", {}) or {}
    port = sglang_config.get("port", 30000) if isinstance(sglang_config, dict) else 30000
    base_url = (
        _config_get(cfg, "sglang_base_url")
        or os.environ.get("SGLANG_BASE_URL")
        or f"http://localhost:{port}/v1"
    ).rstrip("/")
    try:
        data = fetch_json(f"{base_url}/models", timeout=1.5)
    except Exception:
        return []
    return [
        model_option(item.get("id"), source="service-api", source_label="API取得")
        for item in data.get("data", [])
        if isinstance(item, dict) and item.get("id")
    ]


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
            profile = local_server_profile_for_model(str(model_id)) or {}
            server_meta = {
                key: value
                for key, value in profile.items()
                if key in {"server", "server_label"}
            }
            models.append(
                model_option(
                    model_id,
                    source="service-api",
                    source_label="API取得",
                    base_url=base_url,
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

    media_capabilities = {
        "openai": {"image": True, "audio": False},
        "gemini": {"image": True, "audio": True},
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
    return sort_provider_models(provider, annotated_models), remote_error


def provider_settings(provider: str, cfg: Any) -> Dict[str, Any]:
    if provider == "openai_compatible_local":
        api_key = (
            _config_get(cfg, "openai_compatible_local.api_key")
            or _config_get(cfg, "openai_compatible_local_api_key")
            or os.environ.get("OPENAI_COMPATIBLE_LOCAL_API_KEY")
            or ""
        )
        return {
            "base_url": openai_compatible_local_base_url(cfg),
            "api_key_configured": bool(api_key),
            "api_key_placeholder": "dummy" if not api_key else "設定済み",
            "enable_tools": bool(
                _config_get(cfg, "openai_compatible_local.enable_tools", False)
            ),
            "enable_response_format": bool(
                _config_get(
                    cfg, "openai_compatible_local.enable_response_format", False
                )
            ),
            "enable_extra_body": bool(
                _config_get(cfg, "openai_compatible_local.enable_extra_body", False)
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
        sglang_config = _config_get(cfg, "sglang", {}) or {}
        port = sglang_config.get("port", 30000) if isinstance(sglang_config, dict) else 30000
        return {
            "base_url": _config_get(
                cfg,
                "sglang_base_url",
                f"http://localhost:{port}/v1",
            ),
            "api_key_configured": bool(_config_get(cfg, "sglang_api_key", "")),
            "api_key_placeholder": "dummy",
            "enable_tools": True,
        }
    if provider == "codex-cli":
        return {
            "reasoning_effort": _config_get(
                cfg, "codex_cli.reasoning_effort", "medium"
            ),
            "reasoning_effort_options": CODEX_REASONING_EFFORT_OPTIONS,
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
        return CODEX_REASONING_EFFORT_OPTIONS
    if provider_id == "claude-cli":
        return CLAUDE_REASONING_EFFORT_OPTIONS
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
    if provider_id in {"gemini", "sglang", "openai_compatible_local"}:
        return FAST_THINKING_MODE_OPTIONS
    return []


def llm_mode_kind_for_provider(provider: str, model: str) -> str:
    provider_id = str(provider or "").strip().lower()
    if provider_id in {"codex-cli", "claude-cli"}:
        return "reasoning_effort"
    if provider_id in {"openai", "kimi"} and reasoning_effort_options_for_model(provider, model):
        return "reasoning_effort"
    return "response_mode"


def default_llm_mode_for_options(options: List[str]) -> str:
    if "medium" in options:
        return "medium"
    if "fast" in options:
        return "fast"
    return options[0] if options else "fast"


def build_llm_mode_state(
    cfg: Any,
    *,
    client: Any = None,
) -> Dict[str, Any]:
    provider = str(_config_get(cfg, "llm_provider", "openai") or "openai")
    model = str(_config_get(cfg, "llm_model", "gpt-4o") or "gpt-4o")
    options = reasoning_effort_options_for_model(provider, model)
    if not options:
        options = FAST_THINKING_MODE_OPTIONS

    kind = llm_mode_kind_for_provider(provider, model)
    if provider == "codex-cli":
        current = _config_get(cfg, "codex_cli.reasoning_effort", None)
    elif provider == "claude-cli":
        current = _config_get(cfg, "claude_cli.reasoning_effort", None)
    elif provider == "openai" and kind == "reasoning_effort":
        current = _config_get(cfg, "openai.reasoning_effort", None)
    elif client is not None and hasattr(client, "get_llm_mode"):
        current = client.get_llm_mode()
    else:
        current = None

    mode = str(current or "").strip()
    if mode not in options:
        mode = default_llm_mode_for_options(options)

    return {
        "mode": mode,
        "available_modes": options,
        "labels": {value: LLM_MODE_LABELS.get(value, value) for value in options},
        "kind": kind,
        "provider": provider,
        "model": model,
    }


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
    for provider, members in agent_team_members_by_provider(cfg).items():
        for member in members:
            member_key = str(member.get("member_key") or "").strip()
            if member_key not in AGENT_TEAM_MEMBER_KEYS:
                continue
            result.setdefault(provider, []).append(
                {
                    "member_key": member_key,
                    "model": str(member.get("model") or "").strip(),
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
    team_models = agent_team_models_by_provider(cfg)
    providers = []
    refresh_target = (refresh_provider or "").strip() or None

    for provider in PROVIDER_ORDER:
        should_refresh = include_remote and (refresh_target is None or refresh_target == provider)
        models, error = provider_models(
            provider,
            cfg,
            include_remote=should_refresh,
            cached_models=cached_provider_models(provider, cached_catalog),
            ollama_model_manager=ollama_model_manager,
            fetch_json=fetch_json,
        )
        saved_model = provider_saved_model(provider, cfg)
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
        for configured_member in reversed(team_models.get(provider, [])):
            configured_model = configured_member["model"]
            if any(m["id"] == configured_model for m in models):
                continue
            member_key = configured_member["member_key"]
            member_label = AGENT_TEAM_MEMBER_LABEL_BY_KEY.get(member_key, member_key)
            models.insert(
                0,
                model_option(
                    configured_model,
                    configured_model,
                    custom_current=True,
                    source="agent-team-configured",
                    source_label=f"Agent Team: {member_label}",
                ),
            )
        configured_provider_model = saved_model
        if not configured_provider_model and models:
            configured_provider_model = str(models[0]["id"])
        models = enrich_model_reasoning_options(provider, models)
        providers.append(
            {
                "id": provider,
                "label": LLM_PROVIDER_LABELS.get(provider, provider),
                "models": models,
                "configured_model": configured_provider_model or "",
                "supports_custom_model": True,
                "capabilities": PROVIDER_CAPABILITIES.get(
                    provider, ProviderCapabilities()
                ).to_dict(),
                "settings": provider_settings(provider, cfg),
                "source": _provider_source(models, should_refresh),
                "refreshed": should_refresh,
                "cached_at": cached_provider_updated_at(provider, cached_catalog),
                "error": error,
            }
        )
    providers.insert(
        0,
        {
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
        },
    )
    return {
        "current": {"provider": current_p, "model": current_m},
        "providers": providers,
    }


def _has_provider_key(cfg: Any, provider: str) -> bool:
    key_map = {
        "gemini": ("gemini_api_key", "GEMINI_API_KEY"),
        "openai": ("openai_api_key", "OPENAI_API_KEY"),
        "openrouter": ("openrouter_api_key", "OPENROUTER_API_KEY"),
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
    result = []

    for provider in catalog["providers"]:
        provider_id = provider["id"]
        if provider_id in {"openai", "gemini", "openrouter", "kimi"} and not _has_provider_key(
            cfg,
            provider_id,
        ):
            continue

        models = provider.get("models") or []
        model_id = configured_provider_model(provider_id, cfg, models)
        result.append(
            {
                "provider": provider_id,
                "model": model_id,
                "label": header_engine_label(provider_id, model_id),
            }
        )

    current_model_supported = not (
        current_p == "ollama" and _is_ollama_incompatible_model(current_m)
    )
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
