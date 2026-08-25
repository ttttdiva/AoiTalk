"""Platform-aware profiles for OpenAI-compatible local LLM servers."""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_OPENAI_COMPATIBLE_LOCAL_BASE_URL = "http://127.0.0.1:8080/v1"
EXO_BASE_URL = "http://127.0.0.1:52415/v1"
MLX_LM_BASE_URL = DEFAULT_OPENAI_COMPATIBLE_LOCAL_BASE_URL

# llama.cpp/llama-server is deliberately modelled as one runtime of the
# existing OpenAI-compatible local provider.  Model-specific launch and UI
# metadata lives in this registry so adding another GGUF normally only needs a
# profile entry (rather than model-ID branches in each layer).
LLAMA_CPP_MUSE_MODEL_ALIAS = "muse-glimmer-30b"
LLAMA_CPP_MUSE_MODEL_FILENAME = "muse-glimmer-30B-kquant-17gb.gguf"
LLAMA_CPP_QWEN38_MODEL_ALIAS = "qwen3.8-27b-heretic-uncensored"
LLAMA_CPP_QWEN38_MODEL_FILENAME = "Qwen3.8-27B-Heretic-Q4_K_M.gguf"
LLAMA_CPP_QWEN38_OFFICIAL_MODEL_ALIAS = "qwen3.8-27b"
LLAMA_CPP_QWEN38_OFFICIAL_MODEL_FILENAME = "Qwen3.8-27B-UD-Q4_K_XL.gguf"
# Spelling aliases for callers that use the dotted model family notation.
LLAMA_CPP_QWEN3_8_MODEL_ALIAS = LLAMA_CPP_QWEN38_MODEL_ALIAS
LLAMA_CPP_QWEN3_8_MODEL_FILENAME = LLAMA_CPP_QWEN38_MODEL_FILENAME
LLAMA_CPP_GEMMA4_MODEL_ALIAS = "gemma-4-26b-a4b-it-qat-q4-0"
LLAMA_CPP_GEMMA4_MODEL_FILENAME = "gemma-4-26B_q4_0-it.gguf"
LLAMA_CPP_MELODY1437_MODEL_ALIAS = "melody1437-26b-a4b-v2.0"
LLAMA_CPP_MELODY1437_MODEL_FILENAME = "Melody1437-26B-A4B-v2.0-Q8_0.gguf"
LLAMA_CPP_DEFAULT_HOST = "127.0.0.1"
LLAMA_CPP_DEFAULT_PORT = 8080
LLAMA_CPP_DEFAULT_CONTEXT_SIZE = 131072
LLAMA_CPP_DEFAULT_GPU_LAYERS = 999
LLAMA_CPP_DEFAULT_READINESS_TIMEOUT = 180.0

# Qwen3.8 llama.cpp profiles expose reasoning effort through the Jinja
# chat-template contract.  Keep this metadata on the profile itself so the
# catalog, persistence, clients and session routes cannot drift apart.  The
# generic ``local-model`` endpoint deliberately has no entry here and keeps
# its legacy fast/thinking behaviour.
QWEN38_REASONING_EFFORT_OPTIONS = ("low", "medium", "xhigh")
QWEN38_REASONING_EFFORT_DEFAULT = "xhigh"
QWEN38_REASONING_EFFORT_WIRE = {
    "transport": "extra_body",
    "path": "chat_template_kwargs.reasoning_effort",
}

# MTP is intentionally described as profile metadata rather than inferred
# from a model-id substring.  ``mode=embedded`` means the selected GGUF is
# treated as carrying its NextN/MTP support and llama-server only needs the
# speculative decoder type.  ``mode=companion`` is reserved for profiles
# whose metadata names compatible draft GGUF filenames.  A profile may still
# advertise ``default_enabled`` for UI intent while ``supported`` is false;
# this is the explicit NO-NEXTN case for the Heretic Q4_K_M build below.
LLAMA_CPP_MTP_MODE_EMBEDDED = "embedded"
LLAMA_CPP_MTP_MODE_COMPANION = "companion"
LLAMA_CPP_MTP_MODE_UNAVAILABLE = "unavailable"

_LLAMA_CPP_RUNTIME_MARKER_KEYS = (
    "model_path",
    "model_alias",
    "profile",
    "profile_id",
    "runtime_profile",
    "runtime",
    "server_profile",
    "managed",
    "runtime_owned",
)

# Keep the profile IDs and served aliases in this one registry.  Values are
# intentionally plain dictionaries because they are serialized into the
# model catalog/API response.  ``default_context_size`` is the tested AoiTalk
# launch default; ``native_context_size`` records the model metadata without
# silently attempting to launch a 262k context on every host.
LLAMA_CPP_MODEL_PROFILES: Dict[str, Dict[str, Any]] = {
    LLAMA_CPP_MUSE_MODEL_ALIAS: {
        "id": LLAMA_CPP_MUSE_MODEL_ALIAS,
        "label": "Muse Glimmer 30B",
        "description": (
            "Muse Glimmer 30Bの公式推奨4-bit相当GGUFを llama-serverで提供します"
            "（GGUFは手動指定）。"
        ),
        "runtime": "llama_cpp",
        "served_alias": LLAMA_CPP_MUSE_MODEL_ALIAS,
        "alias_locked": True,
        "gguf_filename": LLAMA_CPP_MUSE_MODEL_FILENAME,
        "quantization": "k-quant 17GB（公式repoにliteral Q4_K_Mはありません）",
        "default_context_size": LLAMA_CPP_DEFAULT_CONTEXT_SIZE,
        "minimum_llama_cpp_build": 10353,
        "required_args": ["--jinja"],
        "jinja_required": True,
    },
    LLAMA_CPP_QWEN38_MODEL_ALIAS: {
        "id": LLAMA_CPP_QWEN38_MODEL_ALIAS,
        "label": "Qwen3.8-27B Heretic Abliterated Uncensored Q4_K_M",
        "description": (
            "0bserverx/Qwen3.8-27B-Heretic-Abliterated-Uncensored-GGUF の"
            " Q4_K_M GGUFを llama-serverで提供します。"
        ),
        "runtime": "llama_cpp",
        "served_alias": LLAMA_CPP_QWEN38_MODEL_ALIAS,
        "alias_locked": True,
        "source_repository": "0bserverx/Qwen3.8-27B-Heretic-Abliterated-Uncensored-GGUF",
        "gguf_filename": LLAMA_CPP_QWEN38_MODEL_FILENAME,
        "source_url": "https://huggingface.co/0bserverx/Qwen3.8-27B-Heretic-Abliterated-Uncensored-GGUF",
        "quantization": "Q4_K_M",
        "default_context_size": 32768,
        "native_context_size": 262144,
        "minimum_llama_cpp_build": 7990,
        "reasoning_tools_minimum_llama_cpp_build": 10227,
        "required_args": ["--jinja"],
        "default_args": ["--jinja"],
        "jinja_required": True,
        "capabilities": {
            "reasoning": True,
            "tools": True,
            "media": {"image": False, "audio": False},
        },
        "reasoning_effort_options": list(QWEN38_REASONING_EFFORT_OPTIONS),
        "reasoning_effort_default": QWEN38_REASONING_EFFORT_DEFAULT,
        "reasoning_effort_supports_disable": False,
        "reasoning_effort_wire": dict(QWEN38_REASONING_EFFORT_WIRE),
        "mtp": {
            "supported": False,
            "default_enabled": True,
            "mode": LLAMA_CPP_MTP_MODE_UNAVAILABLE,
            "companion_filenames": [],
            "reason": (
                "このQwen3.8 Heretic Q4_K_M GGUFは配布元でNO-NEXTNと明記されて"
                "おり、互換性を確認できるMTP/NextN artifactがありません。"
            ),
        },
    },
    LLAMA_CPP_QWEN38_OFFICIAL_MODEL_ALIAS: {
        "id": LLAMA_CPP_QWEN38_OFFICIAL_MODEL_ALIAS,
        "label": "Qwen3.8-27B 通常版 UD-Q4_K_XL",
        "description": (
            "unsloth/Qwen3.8-27B-GGUF の UD-Q4_K_XL GGUFを llama-serverで提供します。"
        ),
        "runtime": "llama_cpp",
        "served_alias": LLAMA_CPP_QWEN38_OFFICIAL_MODEL_ALIAS,
        "alias_locked": True,
        "source_repository": "unsloth/Qwen3.8-27B-GGUF",
        "gguf_filename": LLAMA_CPP_QWEN38_OFFICIAL_MODEL_FILENAME,
        "source_url": "https://huggingface.co/unsloth/Qwen3.8-27B-GGUF",
        "quantization": "UD-Q4_K_XL",
        "default_context_size": 32768,
        "native_context_size": 262144,
        "minimum_llama_cpp_build": 7990,
        "reasoning_tools_minimum_llama_cpp_build": 10227,
        "required_args": ["--jinja"],
        "default_args": ["--jinja"],
        "jinja_required": True,
        "capabilities": {
            "reasoning": True,
            "tools": True,
            "media": {"image": False, "audio": False},
        },
        "reasoning_effort_options": list(QWEN38_REASONING_EFFORT_OPTIONS),
        "reasoning_effort_default": QWEN38_REASONING_EFFORT_DEFAULT,
        "reasoning_effort_supports_disable": False,
        "reasoning_effort_wire": dict(QWEN38_REASONING_EFFORT_WIRE),
        "mtp": {
            # The official ggml-org sidecar was exercised with this exact
            # target on llama-server b10437.
            "supported": True,
            "default_enabled": True,
            "mode": LLAMA_CPP_MTP_MODE_COMPANION,
            "companion_filenames": ["mtp-Qwen3.8-27B-Q4_0.gguf"],
            "reason": (
                "ggml-org/Qwen3.8-27B-GGUFの公式MTP sidecar。"
                "UD-Q4_K_XL本体との互換性をllama-server b10437で実測確認済み。"
            ),
        },
    },
    LLAMA_CPP_GEMMA4_MODEL_ALIAS: {
        "id": LLAMA_CPP_GEMMA4_MODEL_ALIAS,
        "label": "Gemma 4 26B A4B IT QAT Q4_0",
        "description": (
            "Google 公式 QAT Q4_0 GGUF（google/gemma-4-26B-A4B-it-qat-q4_0-gguf）を"
            " llama-serverで提供します（GGUFは手動指定）。AoiTalk では mmproj を"
            " 管理しないため text-only です。"
        ),
        "runtime": "llama_cpp",
        "served_alias": LLAMA_CPP_GEMMA4_MODEL_ALIAS,
        "alias_locked": True,
        "source_repository": "google/gemma-4-26B-A4B-it-qat-q4_0-gguf",
        "source_url": "https://huggingface.co/google/gemma-4-26B-A4B-it-qat-q4_0-gguf",
        "gguf_filename": LLAMA_CPP_GEMMA4_MODEL_FILENAME,
        "quantization": "QAT Q4_0",
        "default_context_size": 32768,
        "native_context_size": 262144,
        # llama.cpp release b8637 (PR #21309: mmproj GGUF conversion); specialized parser is b8665 (#21418)
        "minimum_llama_cpp_build": 8637,
        "reasoning_tools_minimum_llama_cpp_build": 8665,
        "required_args": ["--jinja"],
        "jinja_required": True,
        "capabilities": {
            "reasoning": True,
            "tools": True,
            "media": {"image": False, "audio": False},
        },
    },
    LLAMA_CPP_MELODY1437_MODEL_ALIAS: {
        "id": LLAMA_CPP_MELODY1437_MODEL_ALIAS,
        "label": "Melody1437-26B-A4B v2.0 Q8_0",
        "description": (
            "ReadyArt/Melody1437-26B-A4B-v2.0-GGUF の Q8_0 GGUFを"
            " llama-serverで提供します。キャラクター対話・ロールプレイ向けの"
            " text-only profileです。"
        ),
        "runtime": "llama_cpp",
        "served_alias": LLAMA_CPP_MELODY1437_MODEL_ALIAS,
        "alias_locked": True,
        "source_repository": "ReadyArt/Melody1437-26B-A4B-v2.0-GGUF",
        "source_url": "https://huggingface.co/ReadyArt/Melody1437-26B-A4B-v2.0-GGUF",
        "gguf_filename": LLAMA_CPP_MELODY1437_MODEL_FILENAME,
        "quantization": "Q8_0",
        # The base Gemma 4 text config declares 262144 positions.  Keep the
        # managed launch default conservative, matching the existing Gemma 4
        # profile rather than reserving the native maximum on every host.
        "default_context_size": 32768,
        "native_context_size": 262144,
        # The repository ships an explicit Jinja chat template.  No
        # model-specific llama.cpp build gate is asserted: the model card and
        # GGUF metadata do not document one, so avoid copying Gemma's parser
        # minimum without evidence.
        "required_args": ["--jinja"],
        "default_args": ["--jinja"],
        "jinja_required": True,
        "capabilities": {
            "reasoning": False,
            "tools": False,
            "media": {"image": False, "audio": False},
        },
    },
}


def llama_cpp_model_profile(
    model: str | None = None,
    *,
    model_path: str | None = None,
    served_alias: str | None = None,
) -> Optional[Dict[str, Any]]:
    """Return a copy of the known llama.cpp profile for a selection.

    Lookup accepts the stable AoiTalk model ID, served alias, or a GGUF
    filename.  Unknown IDs intentionally return ``None`` so generic external
    OpenAI-compatible servers retain their existing behaviour.
    """

    # Prefer the explicit selected model ID.  This prevents stale runtime
    # aliases from a previous profile (for example Muse -> Qwen hot switch)
    # from changing the new model's defaults.
    candidates = [str(model or "").strip().casefold()]
    if not candidates[0]:
        candidates = []
    candidates.extend(
        [
            str(served_alias or "").strip().casefold(),
            Path(str(model_path or "")).name.strip().casefold(),
        ]
    )
    for candidate in candidates:
        if not candidate:
            continue
        for key, profile in LLAMA_CPP_MODEL_PROFILES.items():
            aliases = {
                str(key).casefold(),
                str(profile.get("id") or "").casefold(),
                str(profile.get("served_alias") or "").casefold(),
                str(profile.get("gguf_filename") or "").casefold(),
                str(profile.get("filename") or "").casefold(),
                str(profile.get("model_filename") or "").casefold(),
                str(profile.get("official_filename") or "").casefold(),
            }
            if candidate in aliases:
                return copy.deepcopy(profile)
    return None


def llama_cpp_model_profiles() -> List[Dict[str, Any]]:
    """Return all registered model profiles as independent dictionaries."""

    return [copy.deepcopy(profile) for profile in LLAMA_CPP_MODEL_PROFILES.values()]


def llama_cpp_mtp_metadata(
    model: str | None = None,
    *,
    profile: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Return the selected managed profile's MTP contract.

    The nested profile metadata is the single source of truth for whether
    MTP is supported, whether the UI should default the toggle on, and which
    (if any) companion draft filenames may be discovered.  Unknown profiles,
    including the external ``local-model`` endpoint, return ``None``.
    """

    selected = profile
    if selected is None and model:
        selected = llama_cpp_model_profile(model)
    if not isinstance(selected, dict):
        return None
    raw = selected.get("mtp")
    if not isinstance(raw, dict):
        return {
            "supported": False,
            "default_enabled": False,
            "mode": LLAMA_CPP_MTP_MODE_UNAVAILABLE,
            "companion_filenames": [],
            "reason": "選択したllama.cpp profileはMTP metadataを宣言していません。",
        }
    filenames = raw.get("companion_filenames")
    if filenames in (None, "", []):
        # Accept the singular alias used by catalog/UI payloads while
        # normalizing all runtime discovery through one canonical list.
        filenames = raw.get("artifact_filename") or raw.get("companion_filename")
    if isinstance(filenames, str):
        filenames = [filenames]
    elif not isinstance(filenames, (list, tuple)):
        filenames = []
    normalized_filenames = [
        str(filename).strip()
        for filename in filenames
        if str(filename).strip()
    ]
    mode = str(raw.get("mode") or "").strip().lower()
    if mode not in {
        LLAMA_CPP_MTP_MODE_EMBEDDED,
        LLAMA_CPP_MTP_MODE_COMPANION,
        LLAMA_CPP_MTP_MODE_UNAVAILABLE,
    }:
        mode = LLAMA_CPP_MTP_MODE_UNAVAILABLE
    reason = str(raw.get("reason") or "").strip()
    if not reason:
        reason = (
            "選択したllama.cpp profileに互換性のあるMTP artifactが宣言されていません。"
        )
    return {
        "supported": bool(raw.get("supported")),
        "default_enabled": bool(raw.get("default_enabled")),
        "mode": mode,
        "companion_filenames": normalized_filenames,
        "reason": reason,
    }


def llama_cpp_profile_capabilities(
    model: str | None = None,
    *,
    profile: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Return a managed profile's declared capabilities as a deep copy.

    ``None`` is intentional for unknown/external profiles so callers can
    preserve the generic ``local-model`` behaviour instead of treating an
    absent declaration as an affirmative capability claim.
    """

    selected = profile
    if selected is None and model:
        selected = llama_cpp_model_profile(model)
    if not isinstance(selected, dict):
        return None
    capabilities = selected.get("capabilities")
    if not isinstance(capabilities, dict):
        return None
    return copy.deepcopy(capabilities)


def llama_cpp_reasoning_effort_metadata(
    model: str | None = None,
    *,
    profile: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Return the canonical reasoning-effort contract for a managed profile.

    Unknown profiles (including the operator-owned ``local-model`` endpoint)
    return ``None`` so callers can retain their existing generic local mode
    semantics.  A deep copy keeps API/session projections from mutating the
    registry.
    """

    selected = profile
    if selected is None and model:
        selected = llama_cpp_model_profile(model)
    if not isinstance(selected, dict):
        return None
    options = selected.get("reasoning_effort_options")
    default = str(selected.get("reasoning_effort_default") or "").strip()
    wire = selected.get("reasoning_effort_wire")
    if not isinstance(options, (list, tuple)) or not options or not default:
        return None
    normalized_options = [str(value).strip() for value in options if str(value).strip()]
    if default not in normalized_options:
        return None
    return {
        "options": normalized_options,
        "default": default,
        "supports_disable": bool(selected.get("reasoning_effort_supports_disable")),
        # Keep malformed/missing wire metadata visible to runtime callers.
        # They must fail closed instead of silently falling back to a
        # hard-coded transport/path.
        "wire": copy.deepcopy(wire) if isinstance(wire, dict) else None,
    }


def llama_cpp_reasoning_effort_request_extra_body(
    model: str | None = None,
    effort: str | None = None,
    *,
    profile: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Project a managed profile's effort onto its declared wire contract.

    The profile metadata is the sole source for transport and nested path.
    Unknown profiles and malformed wire metadata return ``None`` so callers
    can retain generic local behaviour or fail closed, respectively; no
    default wire shape is inferred here.
    """

    metadata = llama_cpp_reasoning_effort_metadata(model, profile=profile)
    if not metadata:
        return None
    selected_effort = str(
        metadata["default"] if effort is None else effort
    ).strip().lower()
    options = metadata.get("options") or []
    if selected_effort not in options:
        raise ValueError(
            "Unsupported reasoning effort for managed local profile: "
            f"{effort!r}; expected one of {options}"
        )

    wire = metadata.get("wire")
    if not isinstance(wire, dict):
        return None
    transport_value = wire.get("transport")
    path_value = wire.get("path")
    if not isinstance(transport_value, str) or not isinstance(path_value, str):
        return None
    transport = transport_value.strip().lower()
    path = path_value.strip()
    path_parts = path.split(".") if path else []
    if transport != "extra_body" or not path_parts or any(
        not part.strip() or part.strip() in {".", ".."} for part in path_parts
    ):
        return None

    value: Any = selected_effort
    for part in reversed(path_parts):
        value = {part.strip(): value}
    return value if isinstance(value, dict) else None


def llama_cpp_reasoning_effort_options(model: str | None = None) -> List[str]:
    metadata = llama_cpp_reasoning_effort_metadata(model)
    return list(metadata["options"]) if metadata else []


def llama_cpp_reasoning_effort_default(model: str | None = None) -> Optional[str]:
    metadata = llama_cpp_reasoning_effort_metadata(model)
    return str(metadata["default"]) if metadata else None


def llama_cpp_profile_legacy_kind(profile: Optional[Dict[str, Any]]) -> str:
    """Return a legacy compatibility kind without leaking model IDs.

    This is intentionally kept in the profile registry module; orchestration
    and catalog layers should consume profile metadata/helpers rather than
    comparing a Muse/Qwen model ID directly.
    """

    if not profile:
        return ""
    return (
        "muse"
        if str(profile.get("id") or "").casefold()
        == LLAMA_CPP_MUSE_MODEL_ALIAS
        else ""
    )


def llama_cpp_runtime_declared(settings: Any) -> bool:
    """Return whether nested settings meaningfully opt into llama.cpp.

    A config overlay may retain keys with ``None`` values after a profile
    switch.  Treat that shape as unset rather than inferring ownership from
    dictionary truthiness; only runtime markers (path, alias, executable,
    profile/runtime metadata, or an explicit managed flag) opt into the
    generic llama.cpp endpoint.
    """

    def _meaningful(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, dict):
            return any(_meaningful(item) for item in value.values())
        if isinstance(value, (list, tuple, set)):
            return any(_meaningful(item) for item in value)
        return True

    if not isinstance(settings, dict):
        return False
    for key in _LLAMA_CPP_RUNTIME_MARKER_KEYS:
        value = settings.get(key)
        if key in {"runtime", "server_profile"}:
            marker = str(value or "").strip().casefold().replace(".", "_")
            if marker in {"", "external", "external_http", "custom", "none", "null"}:
                continue
        if _meaningful(value):
            return True
    return False

EXO_MODEL_IDS = (
    "mlx-community/Llama-3.2-1B-Instruct-4bit",
    "mlx-community/Llama-3.2-3B-Instruct-4bit",
)

MLX_LM_MODEL_IDS = (
    "mlx-community/Mistral-7B-Instruct-v0.3-4bit",
)

_EXO_MODEL_SET = {model.casefold() for model in EXO_MODEL_IDS}
_MLX_LM_MODEL_SET = {model.casefold() for model in MLX_LM_MODEL_IDS}
SUPPORTED_SERVER_PROFILES = (
    "auto",
    "sglang",
    "vllm",
    "llama.cpp",
    "ollama",
    "lm-studio",
    "custom",
)


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


def _llama_cpp_runtime_base_url(
    config: Any,
    *,
    model: str = "",
) -> Optional[str]:
    """Resolve the llama.cpp host/port pair when that runtime owns selection."""

    raw = _config_get(config, "openai_compatible_local.llama_cpp", {})
    # A lightweight config object (for example during first-run or tests) may
    # not materialize the defaults tree yet.  Muse selection and the explicit
    # LLAMA_CPP_* environment overrides still identify this runtime without a
    # persisted nested dictionary.
    if not isinstance(raw, dict):
        raw = {}
    selected_model = str(
        model
        or _config_get(config, "openai_compatible_local.model", "")
        or _config_get(config, "llm_model", "")
        or ""
    ).strip()
    # ``local-model`` is the user-managed external OpenAI-compatible
    # endpoint.  Even a stale nested alias/path must never redirect it to the
    # llama.cpp host/port defaults.
    if selected_model.casefold() == "local-model":
        return None
    configured = _configured_base_url(config)
    auto_start_value = os.getenv("LLAMA_CPP_AUTO_START")
    if auto_start_value is None:
        auto_start_value = raw.get("auto_start", True)
    manual_endpoint = (
        isinstance(auto_start_value, str)
        and auto_start_value.strip().lower() in {"0", "false", "no", "off"}
    ) or auto_start_value is False
    # ``auto_start=false`` is the explicit contract for an operator-owned
    # OpenAI-compatible endpoint.  Known profile metadata still applies to
    # request formatting, but it must not redirect a configured non-default
    # endpoint to llama.cpp's managed host/port.
    if manual_endpoint and configured and not _is_default_base_url(configured):
        return None
    alias = str(os.getenv("LLAMA_CPP_MODEL_ALIAS") or "").strip()
    if not alias:
        raw_alias = raw.get("model_alias")
        if raw_alias is not None and str(raw_alias).strip():
            alias = str(raw_alias).strip()
    profile = llama_cpp_model_profile(selected_model)
    if profile is None and not (
        llama_cpp_runtime_declared(raw)
        or (alias and selected_model.casefold() == alias.casefold())
    ):
        return None
    host = str(
        os.getenv("LLAMA_CPP_HOST")
        or raw.get("host")
        or LLAMA_CPP_DEFAULT_HOST
    ).strip() or LLAMA_CPP_DEFAULT_HOST
    port_value = os.getenv("LLAMA_CPP_PORT") or raw.get("port", LLAMA_CPP_DEFAULT_PORT)
    try:
        port = int(port_value)
    except (TypeError, ValueError):
        port = LLAMA_CPP_DEFAULT_PORT
    if not (1 <= port <= 65535):
        port = LLAMA_CPP_DEFAULT_PORT
    if host in {"0.0.0.0", "::", "[::]", "::0"}:
        host = "127.0.0.1"
    host_for_url = host if ":" not in host or host.startswith("[") else f"[{host}]"
    return normalize_openai_compatible_base_url(f"http://{host_for_url}:{port}/v1")


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

    llama_runtime_url = _llama_cpp_runtime_base_url(
        config,
        model=selected_model,
    )
    if llama_runtime_url:
        return llama_runtime_url

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


def openai_compatible_server_profile(
    config: Any = None,
    *,
    base_url: str = "",
    provider: str = "openai_compatible_local",
) -> Dict[str, Any]:
    """Resolve a conservative server profile.

    ``auto`` only detects a provider; it never sends server-specific cache
    parameters.  Such parameters are sent only for an explicit profile.
    """
    configured = str(
        _config_get(config, "openai_compatible_local.server_profile", "auto") or "auto"
    ).strip().lower()
    aliases = {"llamacpp": "llama.cpp", "llama-cpp": "llama.cpp", "lmstudio": "lm-studio"}
    configured = aliases.get(configured, configured)
    if configured not in SUPPORTED_SERVER_PROFILES:
        configured = "auto"

    url = (base_url or "").casefold()
    if configured == "auto":
        if provider == "sglang" or ":30000" in url or "sglang" in url:
            name = "sglang"
        elif provider == "ollama" or ":11434" in url or "ollama" in url:
            name = "ollama"
        elif "vllm" in url:
            name = "vllm"
        elif "llama" in url or "llama.cpp" in url:
            name = "llama.cpp"
        elif "lmstudio" in url or "lm-studio" in url:
            name = "lm-studio"
        else:
            name = "auto"
    else:
        name = configured

    cache_mode = {
        "sglang": "radix",
        "vllm": "automatic_prefix_caching",
        "llama.cpp": "slot_kv_cache",
        "ollama": "cache_prompt",
        "lm-studio": "server-managed",
        "custom": "custom",
        "auto": "unknown",
    }[name]
    cache_config = _config_get(config, "openai_compatible_local.cache", {}) or {}
    configured_cache_mode = (
        str(cache_config.get("mode", "auto") or "auto").strip().lower()
        if isinstance(cache_config, dict)
        else "auto"
    )
    if configured_cache_mode not in {"", "auto"}:
        cache_mode = configured_cache_mode
    cache_supported = name != "auto" and configured_cache_mode not in {
        "disabled",
        "off",
        "none",
    }
    extra_body = cache_config.get("extra_body", {}) if isinstance(cache_config, dict) else {}
    return {
        "name": name,
        "cache_mode": cache_mode,
        "cache_supported": cache_supported,
        "request_extra_body": dict(extra_body) if isinstance(extra_body, dict) else {},
        "metrics_source": {
            "sglang": "server_metrics_if_available",
            "vllm": "server_metrics_if_available",
            "llama.cpp": "response_timings",
            "ollama": "ollama_native_or_openai_compatible_response",
        }.get(name, "response_usage"),
        "supports_keep_alive": name == "ollama",
        "supports_session_affinity": name in {"sglang", "vllm", "llama.cpp"},
        "capability_detection": configured == "auto",
    }
