"""Official, exact-match context metadata for OpenAI models.

The OpenAI ``/v1/models`` API intentionally does not expose a model's context
window or output limit.  This module is therefore a small, manually curated
snapshot of the values published in the OpenAI model documentation.  It is
kept separate from transport/catalog code so that the provenance and matching
rules remain auditable.

Only canonical model IDs which have an explicit documentation entry belong in
the registry.  In particular, this module does *not* infer limits from a
family prefix and does not match dated snapshots (or internal aliases) unless
that exact ID is added after it has been verified in the documentation.
When OpenAI publishes a new value, update the dated snapshot version and the
individual entry's source URL together with a test; do not add a speculative
model ID.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

# Update this value whenever an entry is reviewed against the official docs.
# It is metadata only; it is not used as a model-ID matcher.
OPENAI_CONTEXT_REGISTRY_SNAPSHOT = "2026-08-13"

GPT56_CONTEXT_WINDOW_TOKENS = 1_050_000
GPT56_MAX_OUTPUT_TOKENS = 128_000
GPT56_SOL_MODEL_DOC_URL = "https://developers.openai.com/api/docs/models/gpt-5.6-sol"
GPT56_TERRA_MODEL_DOC_URL = "https://developers.openai.com/api/docs/models/gpt-5.6-terra"
GPT56_LUNA_MODEL_DOC_URL = "https://developers.openai.com/api/docs/models/gpt-5.6-luna"
# Backwards-compatible name for callers that only need a representative GPT
# 5.6 documentation URL.  Registry entries below carry model-specific URLs.
GPT56_MODEL_DOC_URL = GPT56_LUNA_MODEL_DOC_URL


@dataclass(frozen=True)
class OpenAIModelContextSpec:
    """Documented limits for one exact OpenAI model ID."""

    model_id: str
    context_window_tokens: int
    max_output_tokens: int | None
    source_url: str
    snapshot: str = OPENAI_CONTEXT_REGISTRY_SNAPSHOT
    notes: str = ""


def _spec(
    model_id: str,
    *,
    source_url: str,
    notes: str = "",
) -> OpenAIModelContextSpec:
    return OpenAIModelContextSpec(
        model_id=model_id,
        context_window_tokens=GPT56_CONTEXT_WINDOW_TOKENS,
        max_output_tokens=GPT56_MAX_OUTPUT_TOKENS,
        source_url=source_url,
        notes=notes,
    )


# Exact IDs only.  ``gpt-5.6`` is the documented alias that routes to the
# ``gpt-5.6-sol`` model family; it is intentionally represented as its own
# exact entry rather than by a prefix rule.  The sol/terra/luna values are the
# values shown in the official GPT-5.6 comparison/guidance.
OPENAI_MODEL_CONTEXT_REGISTRY: Mapping[str, OpenAIModelContextSpec] = MappingProxyType(
    {
        "gpt-5.6": _spec(
            "gpt-5.6",
            source_url=GPT56_SOL_MODEL_DOC_URL,
            notes="Documented alias; OpenAI guidance routes this alias to gpt-5.6-sol.",
        ),
        "gpt-5.6-sol": _spec(
            "gpt-5.6-sol",
            source_url=GPT56_SOL_MODEL_DOC_URL,
        ),
        "gpt-5.6-terra": _spec(
            "gpt-5.6-terra",
            source_url=GPT56_TERRA_MODEL_DOC_URL,
        ),
        "gpt-5.6-luna": _spec(
            "gpt-5.6-luna",
            source_url=GPT56_LUNA_MODEL_DOC_URL,
        ),
    }
)


def _canonical_model_id(model_id: str | None) -> str:
    # Registry matching is deliberately byte-for-byte with the model ID sent
    # to the provider.  Do not trim or case-normalize: whitespace, casing, and
    # dated suffixes all identify an ID that was not explicitly documented.
    return model_id if isinstance(model_id, str) else ""


def openai_model_context_spec(model_id: str | None) -> OpenAIModelContextSpec | None:
    """Return metadata for an explicitly documented model ID, if known.

    This intentionally performs no prefix, wildcard, or dated-snapshot match.
    An unknown ID must remain unknown rather than inheriting a family's limit.
    """

    canonical = _canonical_model_id(model_id)
    if not canonical:
        return None
    return OPENAI_MODEL_CONTEXT_REGISTRY.get(canonical)


def openai_model_context_window_tokens(model_id: str | None) -> int | None:
    """Return a documented context window, or ``None`` when it is unknown."""

    spec = openai_model_context_spec(model_id)
    return spec.context_window_tokens if spec is not None else None


# Descriptive aliases make the registry convenient for callers and keep the
# public names stable if the implementation later moves to a generated file.
lookup_openai_model_context = openai_model_context_spec


__all__ = [
    "GPT56_CONTEXT_WINDOW_TOKENS",
    "GPT56_MAX_OUTPUT_TOKENS",
    "GPT56_MODEL_DOC_URL",
    "GPT56_SOL_MODEL_DOC_URL",
    "GPT56_TERRA_MODEL_DOC_URL",
    "GPT56_LUNA_MODEL_DOC_URL",
    "OPENAI_CONTEXT_REGISTRY_SNAPSHOT",
    "OPENAI_MODEL_CONTEXT_REGISTRY",
    "OpenAIModelContextSpec",
    "lookup_openai_model_context",
    "openai_model_context_spec",
    "openai_model_context_window_tokens",
]
