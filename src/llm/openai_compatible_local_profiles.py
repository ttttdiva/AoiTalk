"""Platform-aware profiles for OpenAI-compatible local LLM servers."""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

DEFAULT_OPENAI_COMPATIBLE_LOCAL_BASE_URL = "http://127.0.0.1:8080/v1"
EXO_BASE_URL = "http://127.0.0.1:52415/v1"
MLX_LM_BASE_URL = DEFAULT_OPENAI_COMPATIBLE_LOCAL_BASE_URL

EXO_MODEL_IDS = (
    "mlx-community/Llama-3.2-1B-Instruct-4bit",
    "mlx-community/Llama-3.2-3B-Instruct-4bit",
)

MLX_LM_MODEL_IDS = (
    "mlx-community/Mistral-7B-Instruct-v0.3-4bit",
)

_EXO_MODEL_SET = {model.casefold() for model in EXO_MODEL_IDS}
_MLX_LM_MODEL_SET = {model.casefold() for model in MLX_LM_MODEL_IDS}


def is_macos(platform_name: Optional[str] = None) -> bool:
    value = (platform_name or sys.platform or "").casefold()
    return value.startswith("darwin") or value == "macos"


def normalize_openai_compatible_base_url(base_url: str) -> str:
    clean = (base_url or DEFAULT_OPENAI_COMPATIBLE_LOCAL_BASE_URL).strip().rstrip("/")
    if clean.endswith("/v1"):
        return clean
    return f"{clean}/v1"


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if hasattr(config, "get"):
        value = config.get(key, None)
        if value is not None:
            return value
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    return default


def _configured_base_url(config: Any) -> str:
    return str(
        os.getenv("OPENAI_COMPATIBLE_LOCAL_BASE_URL")
        or _config_get(config, "openai_compatible_local.base_url")
        or _config_get(config, "openai_compatible_local_base_url")
        or ""
    ).strip()


def _is_default_base_url(base_url: str) -> bool:
    if not base_url:
        return True
    return (
        normalize_openai_compatible_base_url(base_url)
        == DEFAULT_OPENAI_COMPATIBLE_LOCAL_BASE_URL
    )


def local_server_profile_for_model(
    model: str,
    *,
    platform_name: Optional[str] = None,
) -> Optional[Dict[str, str]]:
    if not is_macos(platform_name):
        return None

    model_id = str(model or "").strip()
    normalized = model_id.casefold()
    if normalized in _EXO_MODEL_SET:
        return {
            "server": "exo",
            "server_label": "exo",
            "base_url": EXO_BASE_URL,
        }
    if normalized in _MLX_LM_MODEL_SET:
        return {
            "server": "mlx-lm",
            "server_label": "MLX LM",
            "base_url": MLX_LM_BASE_URL,
        }
    return None


def openai_compatible_local_base_url(
    config: Any = None,
    *,
    model: Optional[str] = None,
    platform_name: Optional[str] = None,
) -> str:
    configured = _configured_base_url(config)
    selected_model = str(
        model
        or _config_get(config, "openai_compatible_local.model")
        or _config_get(config, "llm_model")
        or ""
    ).strip()

    if configured and not _is_default_base_url(configured):
        return normalize_openai_compatible_base_url(configured)

    profile = local_server_profile_for_model(
        selected_model,
        platform_name=platform_name,
    )
    if profile:
        return normalize_openai_compatible_base_url(profile["base_url"])

    if configured:
        return normalize_openai_compatible_base_url(configured)
    return DEFAULT_OPENAI_COMPATIBLE_LOCAL_BASE_URL


def openai_compatible_local_discovery_base_urls(
    config: Any = None,
    *,
    model: Optional[str] = None,
    platform_name: Optional[str] = None,
) -> List[str]:
    urls = [
        openai_compatible_local_base_url(
            config,
            model=model,
            platform_name=platform_name,
        )
    ]
    if is_macos(platform_name):
        urls.extend([EXO_BASE_URL, MLX_LM_BASE_URL])

    result: List[str] = []
    seen = set()
    for url in urls:
        normalized = normalize_openai_compatible_base_url(url)
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def macos_openai_compatible_local_model_options(
    *,
    platform_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not is_macos(platform_name):
        return []

    return [
        {
            "id": EXO_MODEL_IDS[0],
            "label": "exo / Llama 3.2 1B Instruct 4bit",
            "description": "Routes to the exo OpenAI-compatible API on macOS.",
            "base_url": EXO_BASE_URL,
            "server": "exo",
            "server_label": "exo",
            "source": "platform-suggested",
            "source_label": "macOS/exo",
        },
        {
            "id": EXO_MODEL_IDS[1],
            "label": "exo / Llama 3.2 3B Instruct 4bit",
            "description": "Routes to the exo OpenAI-compatible API on macOS.",
            "base_url": EXO_BASE_URL,
            "server": "exo",
            "server_label": "exo",
            "source": "platform-suggested",
            "source_label": "macOS/exo",
        },
        {
            "id": MLX_LM_MODEL_IDS[0],
            "label": "MLX LM / Mistral 7B Instruct v0.3 4bit",
            "description": "Routes to the MLX LM OpenAI-compatible API on macOS.",
            "base_url": MLX_LM_BASE_URL,
            "server": "mlx-lm",
            "server_label": "MLX LM",
            "source": "platform-suggested",
            "source_label": "macOS/MLX",
        },
    ]
