"""Platform-aware profiles for OpenAI-compatible local LLM servers."""

from __future__ import annotations

import copy
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_OPENAI_COMPATIBLE_LOCAL_BASE_URL = "http://127.0.0.1:8080/v1"
EXO_BASE_URL = "http://127.0.0.1:52415/v1"
MLX_LM_BASE_URL = DEFAULT_OPENAI_COMPATIBLE_LOCAL_BASE_URL
FREETOKEN_MINIMUM_VERSION = "0.1.2"
FREETOKEN_DEFAULT_HOST = "127.0.0.1"
FREETOKEN_DEFAULT_PORT = 1919
FREETOKEN_DEFAULT_READINESS_TIMEOUT = 180.0
FREETOKEN_BASE_URL = "http://127.0.0.1:1919/v1"
FREETOKEN_QWEN3_06B_MODEL_ID = "Qwen/Qwen3-0.6B"

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
LLAMA_CPP_QWEN38_OFFICIAL_MMPROJ_FILENAME = "mmproj-F16.gguf"
LLAMA_CPP_QWEN38_OFFICIAL_SOURCE_REVISION = "4ca720788d1e01f1bff70c033e0d0028fd02e502"
LLAMA_CPP_QWEN38_OFFICIAL_MODEL_SIZE_BYTES = 17_559_178_144
LLAMA_CPP_QWEN38_OFFICIAL_MODEL_SHA256 = (
    "3f227079003add2511437e5b1e94812e363385225bf6a9b47b0054a72bc8b01e"
)
LLAMA_CPP_QWEN38_OFFICIAL_MMPROJ_SIZE_BYTES = 927_607_488
LLAMA_CPP_QWEN38_OFFICIAL_MMPROJ_SHA256 = (
    "cbb841a9ee0636b2ec172f5bb8df2ea8dfeb01e90fe7c6126581d662a0b4e43e"
)
LLAMA_CPP_QWEN38_OFFICIAL_MTP_REPOSITORY = "ggml-org/Qwen3.8-27B-GGUF"
LLAMA_CPP_QWEN38_OFFICIAL_MTP_REVISION = "efbb3b1f70a21d97fd4495240648405f7228554f"
LLAMA_CPP_QWEN38_FLASH_NEXT_MODEL_ALIAS = "qwen3.8-flash-next"
LLAMA_CPP_QWEN38_FLASH_NEXT_MODEL_FILENAME = "Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf"
LLAMA_CPP_QWEN38_FLASH_NEXT_MODEL_FILENAMES = [
    "Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf",
    "Qwen3.8-Flash-Next-UD-IQ4_XS-00002-of-00003.gguf",
    "Qwen3.8-Flash-Next-UD-IQ4_XS-00003-of-00003.gguf",
]
# The qwen4exp NextN/MTP implementation is currently available only from
# llama.cpp PR #27836.  Keep this pin inside the nested MTP contract: the
# ordinary Flash-Next profile remains compatible with official b10660+.
LLAMA_CPP_QWEN38_FLASH_NEXT_MTP_REQUIRED_COMMIT = (
    "1d8de7c1b0c7d2febf8f983174d8e6a711e2b1af"
)
LLAMA_CPP_QWEN38_FLASH_NEXT_MTP_MODEL_FILENAME = (
    "Qwen3.8-Flash-Next-UD-IQ4_XS-MTP-00001-of-00004.gguf"
)
LLAMA_CPP_QWEN38_FLASH_NEXT_MTP_MODEL_FILENAMES = [
    "Qwen3.8-Flash-Next-UD-IQ4_XS-MTP-00001-of-00004.gguf",
    "Qwen3.8-Flash-Next-UD-IQ4_XS-MTP-00002-of-00004.gguf",
    "Qwen3.8-Flash-Next-UD-IQ4_XS-MTP-00003-of-00004.gguf",
    "Qwen3.8-Flash-Next-UD-IQ4_XS-MTP-00004-of-00004.gguf",
]
LLAMA_CPP_QWEN38_FLASH_NEXT_UNCENSORED_MODEL_ALIAS = (
    "qwen3.8-flash-next-uncensored"
)
LLAMA_CPP_QWEN38_FLASH_NEXT_UNCENSORED_MODEL_FILENAME = (
    "Qwen3.8-Flash-Next-Uncensored-IQ4_XS-00001-of-00003.gguf"
)
LLAMA_CPP_QWEN38_FLASH_NEXT_UNCENSORED_MODEL_FILENAMES = [
    "Qwen3.8-Flash-Next-Uncensored-IQ4_XS-00001-of-00003.gguf",
    "Qwen3.8-Flash-Next-Uncensored-IQ4_XS-00002-of-00003.gguf",
    "Qwen3.8-Flash-Next-Uncensored-IQ4_XS-00003-of-00003.gguf",
]
# Published Hugging Face file sizes are kept as constants for documentation
# and download-plan tests.  They are not used as a runtime trust decision:
# Hugging Face's signed metadata remains the source of truth for bytes.
LLAMA_CPP_QWEN38_FLASH_NEXT_UNCENSORED_MODEL_FILE_SIZES = [
    44_766_155_936,
    44_735_995_008,
    7_971_004_256,
]
# Spelling aliases for callers that use the dotted model family notation.
LLAMA_CPP_QWEN3_8_MODEL_ALIAS = LLAMA_CPP_QWEN38_MODEL_ALIAS
LLAMA_CPP_QWEN3_8_MODEL_FILENAME = LLAMA_CPP_QWEN38_MODEL_FILENAME
LLAMA_CPP_GEMMA4_MODEL_ALIAS = "gemma-4-26b-a4b-it-qat-q4-0"
LLAMA_CPP_GEMMA4_MODEL_FILENAME = "gemma-4-26B_q4_0-it.gguf"
LLAMA_CPP_MELODY1437_MODEL_ALIAS = "melody1437-26b-a4b-v2.0"
LLAMA_CPP_MELODY1437_MODEL_FILENAME = "Melody1437-26B-A4B-v2.0-Q8_0.gguf"
LLAMA_CPP_DARK_SCARLETT_27B_V2_MODEL_ALIAS = "dark-scarlett-27b-v2.0"
LLAMA_CPP_DARK_SCARLETT_27B_V2_MODEL_FILENAME = (
    "Dark-Scarlett-27B-v2.0-Q4_K_M.gguf"
)
LLAMA_CPP_DARK_SCARLETT_27B_V2_MMPROJ_FILENAME = (
    "MMPROJ-Dark-Scarlett-27B-v2.0-Q8_0.gguf"
)
LLAMA_CPP_DARK_SCARLETT_27B_V2_SOURCE_REVISION = (
    "5aa0350de88b22bfbb717de29e02f9666718c8ad"
)
LLAMA_CPP_DARK_SCARLETT_27B_V2_MODEL_SIZE_BYTES = 16_810_714_336
LLAMA_CPP_DARK_SCARLETT_27B_V2_MODEL_SHA256 = (
    "464c09dd5fb42bd8b8e61b8d3c6e368c75d1089a3cf5ed01bd47ac0c9b46739b"
)
LLAMA_CPP_DARK_SCARLETT_27B_V2_MMPROJ_SIZE_BYTES = 629_247_040
LLAMA_CPP_DARK_SCARLETT_27B_V2_MMPROJ_SHA256 = (
    "08af092b6bf0e29ef907170e2e0ef8c284333779962fcf7dac4a05a759056637"
)
LLAMA_CPP_DARK_SCARLETT_26B_A4B_V1_MODEL_ALIAS = "dark-scarlett-26b-a4b-v1.0"
LLAMA_CPP_DARK_SCARLETT_26B_A4B_V1_MODEL_FILENAME = (
    "Dark-Scarlett-v1.0-26B-A4B-Q4_K_M.gguf"
)
LLAMA_CPP_BONSAI2_MODEL_ALIAS = "bonsai-2-27b-ternary"
LLAMA_CPP_BONSAI2_MODEL_FILENAME = "Ternary-Bonsai-2-27B-PQ2_0.gguf"
LLAMA_CPP_BONSAI2_MMPROJ_FILENAME = "Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf"
LLAMA_CPP_BONSAI2_SOURCE_REVISION = "6ed5e12bf84b7a63069882c91dd9e9218647d17b"
LLAMA_CPP_BONSAI2_MODEL_SIZE_BYTES = 7_206_168_928
LLAMA_CPP_BONSAI2_MODEL_SHA256 = (
    "3907dc1658db1f78a9826bf8d5bcb8dc65db0d466388937af57f2294fae62ec1"
)
LLAMA_CPP_BONSAI2_MMPROJ_SIZE_BYTES = 629_246_976
LLAMA_CPP_BONSAI2_MMPROJ_SHA256 = (
    "6807ede61d570bb86ba34b756a0fa109edc33668604de867c6ea6d8f1d631903"
)
# Short spelling aliases keep imports consistent with other model-family
# constants while the versioned names above remain canonical.
LLAMA_CPP_DARK_SCARLETT_27B_MODEL_ALIAS = LLAMA_CPP_DARK_SCARLETT_27B_V2_MODEL_ALIAS
LLAMA_CPP_DARK_SCARLETT_27B_MODEL_FILENAME = LLAMA_CPP_DARK_SCARLETT_27B_V2_MODEL_FILENAME
LLAMA_CPP_DARK_SCARLETT_26B_A4B_MODEL_ALIAS = LLAMA_CPP_DARK_SCARLETT_26B_A4B_V1_MODEL_ALIAS
LLAMA_CPP_DARK_SCARLETT_26B_A4B_MODEL_FILENAME = LLAMA_CPP_DARK_SCARLETT_26B_A4B_V1_MODEL_FILENAME
LLAMA_CPP_DEFAULT_HOST = "127.0.0.1"
LLAMA_CPP_DEFAULT_PORT = 8080
LLAMA_CPP_DEFAULT_CONTEXT_SIZE = 131072
LLAMA_CPP_DEFAULT_GPU_LAYERS = 999
LLAMA_CPP_DEFAULT_READINESS_TIMEOUT = 180.0

# Runtime distributions are trusted selectors, not user-supplied repository
# settings.  The runtime manager resolves these identifiers through its own
# allowlist so existing llama.cpp profiles remain on stock llama.cpp while a
# future profile can opt into a compatible fork without model-ID branches.
LLAMA_CPP_RUNTIME_DISTRIBUTION_STOCK = "stock"
LLAMA_CPP_RUNTIME_DISTRIBUTION_PRISMML = "prismml"

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
        "source_revision": LLAMA_CPP_QWEN38_OFFICIAL_SOURCE_REVISION,
        "gguf_filename": LLAMA_CPP_QWEN38_OFFICIAL_MODEL_FILENAME,
        "gguf_size_bytes": LLAMA_CPP_QWEN38_OFFICIAL_MODEL_SIZE_BYTES,
        "gguf_sha256": LLAMA_CPP_QWEN38_OFFICIAL_MODEL_SHA256,
        "source_url": "https://huggingface.co/unsloth/Qwen3.8-27B-GGUF",
        "quantization": "UD-Q4_K_XL",
        "default_context_size": 32768,
        "native_context_size": 262144,
        "minimum_llama_cpp_build": 7990,
        "reasoning_tools_minimum_llama_cpp_build": 10227,
        "required_args": ["--jinja"],
        "default_args": ["--jinja"],
        "jinja_required": True,
        "auxiliary_artifacts": [
            {
                "id": "mmproj",
                "kind": "mmproj",
                "filename": LLAMA_CPP_QWEN38_OFFICIAL_MMPROJ_FILENAME,
                "required": True,
                "size_bytes": LLAMA_CPP_QWEN38_OFFICIAL_MMPROJ_SIZE_BYTES,
                "sha256": LLAMA_CPP_QWEN38_OFFICIAL_MMPROJ_SHA256,
            }
        ],
        "capabilities": {
            "reasoning": True,
            "tools": True,
            "media": {"image": True, "audio": False},
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
            "artifact_required": True,
            "artifact_repository": LLAMA_CPP_QWEN38_OFFICIAL_MTP_REPOSITORY,
            "artifact_revision": LLAMA_CPP_QWEN38_OFFICIAL_MTP_REVISION,
            "reason": (
                "ggml-org/Qwen3.8-27B-GGUFの公式MTP sidecar。"
                "UD-Q4_K_XL本体との互換性をllama-server b10437で実測確認済み。"
            ),
        },
    },
    LLAMA_CPP_QWEN38_FLASH_NEXT_MODEL_ALIAS: {
        "id": LLAMA_CPP_QWEN38_FLASH_NEXT_MODEL_ALIAS,
        "label": "Qwen3.8-Flash-Next UD-IQ4_XS",
        "description": (
            "unsloth/Qwen3.8-Flash-Next-GGUF の UD-IQ4_XS 3-shard GGUFを"
            " llama-serverで提供します。"
        ),
        "runtime": "llama_cpp",
        "served_alias": LLAMA_CPP_QWEN38_FLASH_NEXT_MODEL_ALIAS,
        "alias_locked": True,
        "source_repository": "unsloth/Qwen3.8-Flash-Next-GGUF",
        "source_url": "https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF",
        "gguf_filename": LLAMA_CPP_QWEN38_FLASH_NEXT_MODEL_FILENAME,
        "gguf_filenames": list(LLAMA_CPP_QWEN38_FLASH_NEXT_MODEL_FILENAMES),
        "gguf_shard_count": 3,
        "gguf_repository_subdir": "UD-IQ4_XS",
        # Keep the local base weights separate from the derived embedded-MTP
        # variant.  The repository subdirectory above remains the upstream
        # download path; this field is only the deterministic local storage
        # layout used by ManagedLocalRuntimeManager.
        "model_storage_subdir": "main",
        "quantization": "UD-IQ4_XS",
        "default_context_size": 16384,
        "native_context_size": 262144,
        "default_gpu_layers": "auto",
        # Official llama.cpp release b10660 is the first release with
        # Qwen3.8-Flash-Next (qwen4exp) support.  Keep this as a minimum build
        # rather than pinning the merge commit so later official builds work.
        "minimum_llama_cpp_build": 10660,
        # Flash-Next is exposed as an ordinary chat profile.  AoiTalk's
        # agentic completion verifier remains disabled for plain chat until
        # release-build compatibility is separately revalidated.  This is
        # independent of the upstream qwen4exp minimum-build gate.
        "chat_agentic_completion_review_enabled": False,
        "required_args": ["--jinja"],
        "default_args": ["--jinja"],
        "jinja_required": True,
        "mtp": {
            "supported": True,
            # PR #27836 is still draft; do not opt new users into a
            # source-built runtime and derived 4-shard artifact by default.
            "default_enabled": False,
            "mode": LLAMA_CPP_MTP_MODE_EMBEDDED,
            "required_llama_cpp_commit": LLAMA_CPP_QWEN38_FLASH_NEXT_MTP_REQUIRED_COMMIT,
            "embedded_variant": {
                "primary_filename": LLAMA_CPP_QWEN38_FLASH_NEXT_MTP_MODEL_FILENAME,
                "filenames": list(LLAMA_CPP_QWEN38_FLASH_NEXT_MTP_MODEL_FILENAMES),
            },
            "reason": (
                "Qwen3.8-Flash-Next MTPはllama.cpp PR #27836のexact headと、"
                "検証済み4-shard embedded variantが揃った場合だけ利用します。"
                "通常の3-shard baseは公式b10660以上でMTPなしのまま利用できます。"
            ),
        },
    },
    LLAMA_CPP_QWEN38_FLASH_NEXT_UNCENSORED_MODEL_ALIAS: {
        "id": LLAMA_CPP_QWEN38_FLASH_NEXT_UNCENSORED_MODEL_ALIAS,
        "label": "Qwen3.8-Flash-Next Uncensored IQ4_XS",
        "description": (
            "orcarouter/Qwen3.8-Flash-Next-Uncensored-GGUF の "
            "IQ4_XS GGUFを llama-serverで提供します。"
        ),
        "runtime": "llama_cpp",
        "served_alias": LLAMA_CPP_QWEN38_FLASH_NEXT_UNCENSORED_MODEL_ALIAS,
        "alias_locked": True,
        "source_repository": "orcarouter/Qwen3.8-Flash-Next-Uncensored-GGUF",
        "source_url": (
            "https://huggingface.co/orcarouter/"
            "Qwen3.8-Flash-Next-Uncensored-GGUF"
        ),
        # These artifacts live at repository root; do not add a synthetic
        # ``gguf_repository_subdir`` for this profile.
        "gguf_filename": LLAMA_CPP_QWEN38_FLASH_NEXT_UNCENSORED_MODEL_FILENAME,
        "gguf_filenames": list(
            LLAMA_CPP_QWEN38_FLASH_NEXT_UNCENSORED_MODEL_FILENAMES
        ),
        "gguf_shard_count": 3,
        "quantization": "IQ4_XS",
        "default_context_size": 16384,
        "native_context_size": 262144,
        "default_gpu_layers": "auto",
        "minimum_llama_cpp_build": 10660,
        "chat_agentic_completion_review_enabled": False,
        "required_args": ["--jinja"],
        "default_args": ["--jinja"],
        "jinja_required": True,
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
    LLAMA_CPP_DARK_SCARLETT_27B_V2_MODEL_ALIAS: {
        "id": LLAMA_CPP_DARK_SCARLETT_27B_V2_MODEL_ALIAS,
        "label": "Dark Scarlett 27B v2.0 Q4_K_M",
        "description": (
            "ReadyArt/Dark-Scarlett-27B-v2.0-GGUF の Q4_K_M GGUFと"
            " 専用Q8_0 mmprojを llama-serverで提供する画像対応profileです。"
        ),
        "runtime": "llama_cpp",
        "served_alias": LLAMA_CPP_DARK_SCARLETT_27B_V2_MODEL_ALIAS,
        "alias_locked": True,
        "source_repository": "ReadyArt/Dark-Scarlett-27B-v2.0-GGUF",
        "source_revision": LLAMA_CPP_DARK_SCARLETT_27B_V2_SOURCE_REVISION,
        "source_url": (
            "https://huggingface.co/ReadyArt/Dark-Scarlett-27B-v2.0-GGUF"
        ),
        "gguf_filename": LLAMA_CPP_DARK_SCARLETT_27B_V2_MODEL_FILENAME,
        "gguf_size_bytes": LLAMA_CPP_DARK_SCARLETT_27B_V2_MODEL_SIZE_BYTES,
        "gguf_sha256": LLAMA_CPP_DARK_SCARLETT_27B_V2_MODEL_SHA256,
        "quantization": "Q4_K_M",
        "default_context_size": 32768,
        "native_context_size": 262144,
        # Qwen3.5 vision was already exercised in upstream llama.cpp b8155.
        # Use the later specialized-parser build as a conservative managed
        # floor so image-capable Dark Scarlett never launches on text-only-era
        # or early multimodal runtimes.
        "minimum_llama_cpp_build": 10227,
        "required_args": ["--jinja"],
        "default_args": ["--jinja"],
        "jinja_required": True,
        "auxiliary_artifacts": [
            {
                "id": "mmproj",
                "kind": "mmproj",
                "filename": LLAMA_CPP_DARK_SCARLETT_27B_V2_MMPROJ_FILENAME,
                "required": True,
                "size_bytes": LLAMA_CPP_DARK_SCARLETT_27B_V2_MMPROJ_SIZE_BYTES,
                "sha256": LLAMA_CPP_DARK_SCARLETT_27B_V2_MMPROJ_SHA256,
            }
        ],
        "capabilities": {
            "reasoning": False,
            "tools": False,
            "media": {"image": True, "audio": False},
        },
    },
    LLAMA_CPP_DARK_SCARLETT_26B_A4B_V1_MODEL_ALIAS: {
        "id": LLAMA_CPP_DARK_SCARLETT_26B_A4B_V1_MODEL_ALIAS,
        "label": "Dark Scarlett 26B-A4B v1.0 Q4_K_M",
        "description": (
            "ReadyArt/Dark-Scarlett-v1.0-26B-A4B-GGUF の Q4_K_M GGUFを"
            " llama-serverで提供します。restricted optional の text-only profileです。"
        ),
        "runtime": "llama_cpp",
        "served_alias": LLAMA_CPP_DARK_SCARLETT_26B_A4B_V1_MODEL_ALIAS,
        "alias_locked": True,
        "source_repository": "ReadyArt/Dark-Scarlett-v1.0-26B-A4B-GGUF",
        "source_url": (
            "https://huggingface.co/ReadyArt/Dark-Scarlett-v1.0-26B-A4B-GGUF"
        ),
        "gguf_filename": LLAMA_CPP_DARK_SCARLETT_26B_A4B_V1_MODEL_FILENAME,
        "quantization": "Q4_K_M",
        "default_context_size": 32768,
        "native_context_size": 262144,
        "required_args": ["--jinja"],
        "default_args": ["--jinja"],
        "jinja_required": True,
        "capabilities": {
            "reasoning": False,
            "tools": False,
            "media": {"image": False, "audio": False},
        },
    },
    LLAMA_CPP_BONSAI2_MODEL_ALIAS: {
        "id": LLAMA_CPP_BONSAI2_MODEL_ALIAS,
        "label": "Bonsai 2 27B Ternary PQ2_0",
        "description": (
            "PrismML Ternary-Bonsai-2-27BのPQ2_0 GGUFを"
            " PrismML llama.cpp runtimeで提供します。"
        ),
        "runtime": "llama_cpp",
        "runtime_distribution": LLAMA_CPP_RUNTIME_DISTRIBUTION_PRISMML,
        "served_alias": LLAMA_CPP_BONSAI2_MODEL_ALIAS,
        "alias_locked": True,
        "source_repository": "prism-ml/Ternary-Bonsai-2-27B-gguf",
        "source_url": (
            "https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf"
        ),
        "source_revision": LLAMA_CPP_BONSAI2_SOURCE_REVISION,
        "gguf_filename": LLAMA_CPP_BONSAI2_MODEL_FILENAME,
        "gguf_size_bytes": LLAMA_CPP_BONSAI2_MODEL_SIZE_BYTES,
        "gguf_sha256": LLAMA_CPP_BONSAI2_MODEL_SHA256,
        "quantization": "PQ2_0",
        "default_context_size": 32768,
        "native_context_size": 262144,
        "default_gpu_layers": "auto",
        "minimum_llama_cpp_build": 10683,
        "required_args": ["--jinja"],
        "default_args": ["--jinja"],
        "jinja_required": True,
        "auxiliary_artifacts": [
            {
                "id": "mmproj",
                "kind": "mmproj",
                "filename": LLAMA_CPP_BONSAI2_MMPROJ_FILENAME,
                "required": True,
                "size_bytes": LLAMA_CPP_BONSAI2_MMPROJ_SIZE_BYTES,
                "sha256": LLAMA_CPP_BONSAI2_MMPROJ_SHA256,
            }
        ],
        "sampling_defaults": {
            "temperature": 1.0,
            "top_p": 0.95,
            "top_k": 20,
        },
        "capabilities": {
            "reasoning": True,
            "tools": True,
            "media": {"image": True, "audio": False},
        },
    },
}

# Keep this registry deliberately small. Entries belong here only when the
# FreeToken serve contract and the exact upstream model repository have both
# been confirmed. FreeToken serves a local model directory, so no individual
# safetensors filename is inferred.
FREETOKEN_MODEL_PROFILES: Dict[str, Dict[str, Any]] = {
    FREETOKEN_QWEN3_06B_MODEL_ID: {
        "id": FREETOKEN_QWEN3_06B_MODEL_ID,
        "label": "Qwen3 0.6B / FreeToken",
        "description": "FreeToken公式確認用の最小managed profile。",
        "runtime": "freetoken",
        "source_repository": "Qwen/Qwen3-0.6B",
        "source_url": "https://huggingface.co/Qwen/Qwen3-0.6B",
        "model_format": "safetensors",
        "minimum_freetoken_version": FREETOKEN_MINIMUM_VERSION,
        "capabilities": {
            "reasoning": True,
            "tools": False,
            "media": {"image": False, "audio": False},
        },
    },
}


def freetoken_model_profile(
    model: str | None = None,
) -> Optional[Dict[str, Any]]:
    """Return a copy of one trusted FreeToken model profile."""

    candidate = str(model or "").strip().casefold()
    if not candidate:
        return None
    for key, profile in FREETOKEN_MODEL_PROFILES.items():
        identities = {
            str(key).casefold(),
            str(profile.get("id") or "").casefold(),
            str(profile.get("source_repository") or "").casefold(),
        }
        if candidate in identities:
            return copy.deepcopy(profile)
    return None


def freetoken_model_profiles() -> List[Dict[str, Any]]:
    return [copy.deepcopy(profile) for profile in FREETOKEN_MODEL_PROFILES.values()]


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
            shard_filenames = profile.get("gguf_filenames")
            if isinstance(shard_filenames, (list, tuple)):
                aliases.update(
                    Path(str(filename)).name.strip().casefold()
                    for filename in shard_filenames
                    if str(filename).strip()
                )
            if candidate in aliases:
                return copy.deepcopy(profile)
    return None


def llama_cpp_model_profiles() -> List[Dict[str, Any]]:
    """Return all registered model profiles as independent dictionaries."""

    return [copy.deepcopy(profile) for profile in LLAMA_CPP_MODEL_PROFILES.values()]


def llama_cpp_runtime_distribution(
    model: str | None = None,
    *,
    profile: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the trusted runtime-distribution selector for a profile.

    Existing profiles intentionally default to the stock distribution.  The
    returned value is only a logical identifier; repository, release and
    asset trust decisions stay in the runtime manager's allowlist.
    """

    selected = profile
    if selected is None and model:
        selected = llama_cpp_model_profile(model)
    if not isinstance(selected, dict):
        return LLAMA_CPP_RUNTIME_DISTRIBUTION_STOCK
    value = str(
        selected.get("runtime_distribution")
        or LLAMA_CPP_RUNTIME_DISTRIBUTION_STOCK
    ).strip().lower()
    return value or LLAMA_CPP_RUNTIME_DISTRIBUTION_STOCK


def _llama_cpp_safe_metadata_filename(value: Any, *, field: str) -> str:
    """Normalize one trusted profile filename without permitting path input."""

    if not isinstance(value, str):
        raise ValueError(f"{field} must be a filename string")
    filename = value.strip()
    path = Path(filename)
    if (
        not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
        or ":" in filename
        or any(char in filename for char in "*?[]")
        or path.is_absolute()
        or path.name != filename
        # ``Path`` on POSIX does not consider ``C:\\foo`` absolute; reject
        # a drive-qualified spelling explicitly for cross-platform safety.
        or bool(re.match(r"^[A-Za-z]:", filename))
    ):
        raise ValueError(f"{field} must be a safe basename")
    return filename


def _llama_cpp_safe_metadata_filenames(
    value: Any,
    *,
    field: str,
) -> list[str]:
    """Normalize an exact filename list and reject duplicates/traversal."""

    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{field} must be a non-empty filename list")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        filename = _llama_cpp_safe_metadata_filename(
            item,
            field=f"{field}[{index}]",
        )
        duplicate_key = filename.casefold()
        if duplicate_key in seen:
            raise ValueError(f"{field} contains duplicate filenames")
        seen.add(duplicate_key)
        result.append(filename)
    return result


_LLAMA_CPP_AUXILIARY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def llama_cpp_auxiliary_artifacts(
    model: str | None = None,
    *,
    profile: Optional[Dict[str, Any]] = None,
) -> list[Dict[str, Any]]:
    """Return a validated generic sidecar-artifact contract.

    This contract is intentionally separate from the MTP metadata: mmproj and
    future launch-time sidecars are ordinary required/optional files, while
    MTP remains a speculative-decoding-specific contract.
    """

    selected = profile
    if selected is None and model:
        selected = llama_cpp_model_profile(model)
    if not isinstance(selected, dict):
        return []
    raw_items = selected.get("auxiliary_artifacts")
    if raw_items is None:
        return []
    if not isinstance(raw_items, (list, tuple)):
        raise ValueError("auxiliary_artifacts must be a list")

    result: list[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    mmproj_count = 0
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise ValueError(f"auxiliary_artifacts[{index}] must be an object")
        artifact_id = str(raw.get("id") or "").strip().lower()
        kind = str(raw.get("kind") or "").strip().lower()
        if not _LLAMA_CPP_AUXILIARY_ID_PATTERN.fullmatch(artifact_id):
            raise ValueError(f"auxiliary_artifacts[{index}].id is invalid")
        if not _LLAMA_CPP_AUXILIARY_ID_PATTERN.fullmatch(kind):
            raise ValueError(f"auxiliary_artifacts[{index}].kind is invalid")
        if artifact_id in seen_ids:
            raise ValueError("auxiliary_artifacts contains duplicate ids")
        seen_ids.add(artifact_id)
        filename = _llama_cpp_safe_metadata_filename(
            raw.get("filename"),
            field=f"auxiliary_artifacts[{index}].filename",
        )
        required = _llama_cpp_metadata_bool(raw.get("required"), default=False)
        normalized: Dict[str, Any] = {
            "id": artifact_id,
            "kind": kind,
            "filename": filename,
            "required": required,
        }
        if raw.get("size_bytes") not in (None, ""):
            size = raw.get("size_bytes")
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                raise ValueError(
                    f"auxiliary_artifacts[{index}].size_bytes is invalid"
                )
            normalized["size_bytes"] = size
        if raw.get("sha256") not in (None, ""):
            digest = str(raw.get("sha256") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(
                    f"auxiliary_artifacts[{index}].sha256 is invalid"
                )
            normalized["sha256"] = digest
        repository_subdir = raw.get("repository_subdir")
        if repository_subdir not in (None, ""):
            if not isinstance(repository_subdir, str):
                raise ValueError(
                    f"auxiliary_artifacts[{index}].repository_subdir is invalid"
                )
            subdir = repository_subdir.strip()
            parts = subdir.split("/")
            if (
                not subdir
                or "\\" in subdir
                or subdir.startswith("/")
                or any(part in {"", ".", ".."} for part in parts)
                or (len(parts[0]) >= 2 and parts[0][1] == ":")
            ):
                raise ValueError(
                    f"auxiliary_artifacts[{index}].repository_subdir is invalid"
                )
            normalized["repository_subdir"] = subdir
        if kind == "mmproj":
            mmproj_count += 1
        result.append(normalized)
    if mmproj_count > 1:
        raise ValueError("auxiliary_artifacts may contain at most one mmproj")
    return copy.deepcopy(result)


def llama_cpp_mmproj_metadata(
    model: str | None = None,
    *,
    profile: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Return the one validated mmproj sidecar, if declared."""

    artifacts = llama_cpp_auxiliary_artifacts(model, profile=profile)
    for artifact in artifacts:
        if artifact.get("kind") == "mmproj":
            return artifact
    return None


def _llama_cpp_mtp_embedded_variant(raw: Any) -> Optional[Dict[str, Any]]:
    """Return a safe copy of the optional embedded variant declaration."""

    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("mtp.embedded_variant must be an object")
    primary = _llama_cpp_safe_metadata_filename(
        raw.get("primary_filename"),
        field="mtp.embedded_variant.primary_filename",
    )
    filenames = _llama_cpp_safe_metadata_filenames(
        raw.get("filenames"),
        field="mtp.embedded_variant.filenames",
    )
    if filenames[0] != primary:
        raise ValueError(
            "mtp.embedded_variant.primary_filename must be the first filename"
        )
    return {
        "primary_filename": primary,
        "filenames": filenames,
    }


def _llama_cpp_metadata_bool(value: Any, *, default: bool = False) -> bool:
    """Parse metadata booleans without treating ``"false"`` as truthy."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value) if value in (0, 1) else default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


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
    if filenames:
        normalized_filenames = _llama_cpp_safe_metadata_filenames(
            filenames,
            field="mtp.companion_filenames",
        )
    else:
        normalized_filenames = []
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
    result = {
        "supported": _llama_cpp_metadata_bool(raw.get("supported")),
        "default_enabled": _llama_cpp_metadata_bool(raw.get("default_enabled")),
        "mode": mode,
        "companion_filenames": normalized_filenames,
        "artifact_required": _llama_cpp_metadata_bool(
            raw.get("artifact_required")
            if raw.get("artifact_required") is not None
            else raw.get("required")
        ),
        "reason": reason,
    }
    minimum_value = raw.get("minimum_llama_cpp_build")
    if minimum_value not in (None, ""):
        if isinstance(minimum_value, bool):
            raise ValueError(
                "mtp.minimum_llama_cpp_build must be a positive integer"
            )
        if isinstance(minimum_value, float) and not minimum_value.is_integer():
            raise ValueError(
                "mtp.minimum_llama_cpp_build must be a positive integer"
            )
        try:
            minimum = int(minimum_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "mtp.minimum_llama_cpp_build must be a positive integer"
            ) from exc
        if minimum <= 0:
            raise ValueError(
                "mtp.minimum_llama_cpp_build must be a positive integer"
            )
        result["minimum_llama_cpp_build"] = minimum

    required_commit = str(raw.get("required_llama_cpp_commit") or "").strip().lower()
    if required_commit:
        if not re.fullmatch(r"[0-9a-f]{7,40}", required_commit):
            raise ValueError("mtp.required_llama_cpp_commit must be a git SHA")
        result["required_llama_cpp_commit"] = required_commit

    embedded_variant = _llama_cpp_mtp_embedded_variant(
        raw.get("embedded_variant")
    )
    if embedded_variant is not None:
        if mode != LLAMA_CPP_MTP_MODE_EMBEDDED:
            raise ValueError("mtp.embedded_variant requires mode=embedded")
        result["embedded_variant"] = embedded_variant
    if "required" in raw:
        result["required"] = _llama_cpp_metadata_bool(raw.get("required"))
    return copy.deepcopy(result)


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
    "freetoken",
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


def managed_local_runtime_for_model(
    config: Any = None,
    model: str | None = None,
) -> Optional[str]:
    """Return llama_cpp/freetoken for a managed nested runtime, else None.

    The selected model identity is authoritative. Stale nested runtime state
    from a previous selection is never allowed to override a trusted profile.
    ``local-model`` remains the explicit operator-owned external sentinel.
    """

    selected_model = str(
        model
        or _config_get(config, "openai_compatible_local.model", "")
        or _config_get(config, "llm_model", "")
        or ""
    ).strip()
    if not selected_model or selected_model.casefold() == "local-model":
        return None

    if freetoken_model_profile(selected_model) is not None:
        return "freetoken"
    if llama_cpp_model_profile(selected_model) is not None:
        return "llama_cpp"

    # Preserve the existing generic llama.cpp/custom-GGUF compatibility, but
    # only when the persisted served alias explicitly identifies this model.
    # A stale model_path alone is insufficient to claim a new selection.
    raw_llama = _config_get(config, "openai_compatible_local.llama_cpp", {})
    if isinstance(raw_llama, dict) and llama_cpp_runtime_declared(raw_llama):
        alias = str(
            os.getenv("LLAMA_CPP_MODEL_ALIAS")
            or raw_llama.get("model_alias")
            or ""
        ).strip()
        if alias and alias.casefold() == selected_model.casefold():
            return "llama_cpp"

    return None


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


def _freetoken_runtime_base_url(
    config: Any,
    *,
    model: str = "",
) -> Optional[str]:
    """Resolve the managed FreeToken loopback endpoint for a trusted profile."""

    if managed_local_runtime_for_model(config, model) != "freetoken":
        return None

    raw = _config_get(config, "openai_compatible_local.freetoken", {})
    raw = raw if isinstance(raw, dict) else {}
    configured = _configured_base_url(config)
    auto_start = raw.get("auto_start", True)
    manual_endpoint = (
        isinstance(auto_start, str)
        and auto_start.strip().lower() in {"0", "false", "no", "off"}
    ) or auto_start is False

    # A known profile can still be connected to an operator-owned endpoint.
    # In that case preserve the explicitly configured non-default URL.
    if manual_endpoint and configured and not _is_default_base_url(configured):
        return None

    host = str(raw.get("host") or FREETOKEN_DEFAULT_HOST).strip()
    if not host:
        host = FREETOKEN_DEFAULT_HOST
    try:
        port = int(raw.get("port", FREETOKEN_DEFAULT_PORT))
    except (TypeError, ValueError):
        port = FREETOKEN_DEFAULT_PORT
    if not (1 <= port <= 65535):
        port = FREETOKEN_DEFAULT_PORT
    if host in {"0.0.0.0", "::", "[::]", "::0"}:
        host = "127.0.0.1"
    host_for_url = (
        host if ":" not in host or host.startswith("[") else f"[{host}]"
    )
    return normalize_openai_compatible_base_url(
        f"http://{host_for_url}:{port}"
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
    # An Enterprise external deployment owns this endpoint outside AoiTalk.
    # Check it before the model-profile runtime resolver, otherwise a stale
    # llama.cpp profile can redirect host.docker.internal to 127.0.0.1:8080.
    try:
        from src.llm.deployment_resolver import resolve_llm_deployment

        deployment = resolve_llm_deployment(config)
        if (
            deployment is not None
            and deployment.backend == "external"
            and deployment.effective_provider == "openai_compatible_local"
            and deployment.effective_base_url
        ):
            return normalize_openai_compatible_base_url(
                deployment.effective_base_url
            )
    except Exception:
        # Preserve the historical local resolver when deployment metadata is
        # unavailable during lightweight import/test paths.
        pass

    configured = _configured_base_url(config)
    selected_model = str(
        model
        or _config_get(config, "openai_compatible_local.model")
        or _config_get(config, "llm_model")
        or ""
    ).strip()

    freetoken_runtime_url = _freetoken_runtime_base_url(
        config,
        model=selected_model,
    )
    if freetoken_runtime_url:
        return freetoken_runtime_url

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
    operator_owned_external = False
    try:
        from src.llm.deployment_resolver import resolve_llm_deployment

        deployment = resolve_llm_deployment(config)
        operator_owned_external = bool(
            deployment is not None
            and deployment.backend == "external"
            and deployment.effective_provider == "openai_compatible_local"
        )
    except Exception:
        pass
    urls = [
        openai_compatible_local_base_url(
            config,
            model=model,
            platform_name=platform_name,
        )
    ]
    if is_macos(platform_name) and not operator_owned_external:
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
    selected_model = str(
        _config_get(config, "openai_compatible_local.model", "")
        or _config_get(config, "llm_model", "")
        or ""
    ).strip()
    managed_runtime = managed_local_runtime_for_model(
        config,
        selected_model,
    )
    if configured == "auto":
        if managed_runtime == "freetoken":
            name = "freetoken"
        elif managed_runtime == "llama_cpp":
            name = "llama.cpp"
        elif provider == "sglang" or ":30000" in url or "sglang" in url:
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
        "freetoken": "server-managed",
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
            "freetoken": "response_usage",
            "ollama": "ollama_native_or_openai_compatible_response",
        }.get(name, "response_usage"),
        "supports_keep_alive": name == "ollama",
        "supports_session_affinity": name in {"sglang", "vllm", "llama.cpp"},
        "capability_detection": configured == "auto",
    }
