"""DeepInfra OpenAI-compatible chat contract helpers.

The model catalog is the source of model metadata, while this module owns the
small amount of provider-specific normalization needed at the generation
boundary.  In particular, a selected model ID is preserved verbatim; this
module never aliases one DeepInfra model to another.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


DEEPINFRA_DEFAULT_BASE_URL = "https://api.deepinfra.com/v1/openai"
DEEPINFRA_CHAT_COMPLETIONS_PATH = "/chat/completions"
DEEPINFRA_DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
DEEPINFRA_REASONING_EFFORT_OPTIONS = ("none", "low", "medium", "high")
DEEPINFRA_MODEL_CAPABILITIES_CONFIG_KEY = "runtime.target_model_capabilities"

_CAPABILITY_ALIASES = {
    "supports_tools": ("supports_tools", "tools", "tool_calling"),
    "supports_response_format": (
        "supports_response_format",
        "structured_output",
        "json_schema",
    ),
    "supports_extra_body": ("supports_extra_body",),
    "supports_reasoning": ("supports_reasoning", "reasoning"),
}


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    """Read a dotted config key from Config, TargetConfig, or a plain mapping."""

    getter = getattr(config, "get", None)
    if callable(getter):
        missing = object()
        try:
            value = getter(key, missing)
        except TypeError:
            value = missing
        if value is not missing:
            return value
    if isinstance(config, Mapping):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, Mapping) or part not in value:
                return default
            value = value[part]
        return value
    return default


def _catalog_model_metadata(config: Any | None, model: Any) -> Mapping[str, Any] | None:
    """Read exact model metadata from the catalog cache/static catalog.

    This is intentionally a local read.  Client construction must not perform
    a provider discovery request, and a catalog entry is considered only when
    its model ID matches the selected ID exactly.
    """

    selected = str(model or "").strip()
    if not selected:
        return None
    try:
        from ..services.llm_model_catalog import (
            STATIC_MODEL_CATALOG,
            cached_provider_models,
            load_model_catalog_cache,
        )

        candidates = list(
            cached_provider_models("deepinfra", load_model_catalog_cache())
        )
        candidates.extend(STATIC_MODEL_CATALOG.get("deepinfra", []))
        for item in candidates:
            if not isinstance(item, Mapping):
                continue
            item_id = str(item.get("id") or item.get("model") or "").strip()
            if item_id == selected:
                return item
    except Exception:
        return None
    return None


def _metadata_from_raw(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one projected/catalog metadata mapping."""

    result: dict[str, Any] = {}
    nested_capabilities = raw.get("capabilities")
    capability_sources = [raw]
    if isinstance(nested_capabilities, Mapping):
        capability_sources.append(nested_capabilities)
    for key, aliases in _CAPABILITY_ALIASES.items():
        for source in capability_sources:
            for alias in aliases:
                value = source.get(alias)
                if isinstance(value, bool):
                    result[key] = value
                    break
            if key in result:
                break

    raw_options = raw.get("reasoning_effort_options")
    if raw_options is None:
        reasoning = raw.get("reasoning")
        if isinstance(reasoning, Mapping):
            raw_options = reasoning.get("options") or reasoning.get(
                "effort_options"
            )
    if isinstance(raw_options, Sequence) and not isinstance(
        raw_options, (str, bytes, bytearray)
    ):
        options: list[str] = []
        for item in raw_options:
            value = str(item or "").strip().lower()
            if value and value not in options and len(options) < 16:
                options.append(value)
        # An explicit empty list is authoritative: it disables reasoning
        # controls for this model instead of borrowing the provider default.
        result["reasoning_effort_options"] = tuple(options)

    raw_default = raw.get("reasoning_effort_default")
    if raw_default is None:
        reasoning = raw.get("reasoning")
        if isinstance(reasoning, Mapping):
            raw_default = reasoning.get("default") or reasoning.get("effort_default")
    default = str(raw_default or "").strip().lower()
    if default and len(default) <= 32:
        result["reasoning_effort_default"] = default
    return result


def model_capabilities(
    config: Any | None,
    model: Any = None,
) -> dict[str, Any]:
    """Return the bounded model metadata projected onto a target config.

    API/catalog metadata is untrusted input.  Only booleans and a bounded list
    of non-empty effort labels are retained.  Nested ``capabilities`` and
    ``reasoning`` shapes are accepted because provider model-list responses use
    both forms across versions.
    """

    raw = _config_get(config, DEEPINFRA_MODEL_CAPABILITIES_CONFIG_KEY, None)
    selected_model = model
    if selected_model is None:
        selected_model = _config_get(config, "llm_model", None)
        if not selected_model:
            selected_model = _config_get(config, "deepinfra.model", None)
    catalog_metadata = _catalog_model_metadata(config, selected_model)
    result = _metadata_from_raw(catalog_metadata) if catalog_metadata else {}
    if isinstance(raw, Mapping):
        # A route may project only one capability (for example tools) while
        # the cache supplies reasoning metadata.  Merge exact route data over
        # the catalog entry instead of discarding the remaining fields.
        result.update(_metadata_from_raw(raw))
    return result


def model_capability(
    config: Any | None,
    name: str,
    default: bool | None = None,
    *,
    model: Any = None,
) -> bool | None:
    """Read one optional model capability without inventing an override."""

    value = model_capabilities(config, model).get(str(name or "").strip())
    return value if isinstance(value, bool) else default


def reasoning_effort_options(config: Any | None, model: str = "") -> tuple[str, ...]:
    """Return model metadata options, falling back to the provider contract."""

    metadata = model_capabilities(config, model)
    if metadata.get("supports_reasoning") is False:
        return ()
    options = metadata.get("reasoning_effort_options")
    if isinstance(options, tuple):
        return options

    # Keep the catalog as the authoritative provider/model extension point,
    # while retaining the bounded DeepInfra contract when a caller constructs
    # a client without a catalog import or metadata projection.
    try:
        from ..services.llm_model_catalog import reasoning_effort_options_for_model

        discovered = reasoning_effort_options_for_model("deepinfra", model)
        if discovered:
            return tuple(
                str(value).strip().lower()
                for value in discovered
                if str(value).strip()
            )
    except Exception:
        pass
    return DEEPINFRA_REASONING_EFFORT_OPTIONS


def reasoning_effort_default(
    config: Any | None,
    model: str = "",
    options: tuple[str, ...] | None = None,
) -> str:
    """Resolve a metadata default while preserving a safe provider fallback."""

    selected_options = options or reasoning_effort_options(config, model)
    if not selected_options:
        return ""
    metadata_default = str(
        model_capabilities(config, model).get("reasoning_effort_default") or ""
    ).strip().lower()
    if metadata_default in selected_options:
        return metadata_default
    if "high" in selected_options:
        return "high"
    return selected_options[0]


def resolve_reasoning_effort(
    config: Any | None,
    model: str,
    requested: Any = None,
    *,
    agent_team_route: bool = False,
) -> str:
    """Resolve one effort value without mapping metadata to another mode.

    An explicit Agent Team route is fail-closed when its effort is absent or
    unsupported.  For ordinary requests, metadata-backed profiles also fail
    closed; the legacy provider default is retained only when no model profile
    was projected.
    """

    options = reasoning_effort_options(config, model)
    if not options:
        return ""
    value = str(requested or "").strip().lower()
    if not value:
        return "" if agent_team_route else reasoning_effort_default(config, model, options)
    if value in options:
        return value
    metadata = model_capabilities(config, model)
    if metadata:
        return ""
    return "" if agent_team_route else reasoning_effort_default(config, model, options)


def normalize_model_id(model: Any, *, default_if_empty: bool = False) -> str:
    """Trim a selected ID without remapping it to another provider model."""

    value = str(model or "").strip()
    if value or not default_if_empty:
        return value
    return DEEPINFRA_DEFAULT_MODEL


def normalize_base_url(base_url: Any = None) -> str:
    """Normalize a DeepInfra OpenAI base URL, preserving custom hosts."""

    value = str(base_url or "").strip().rstrip("/")
    if not value:
        return DEEPINFRA_DEFAULT_BASE_URL
    suffix = DEEPINFRA_CHAT_COMPLETIONS_PATH
    if value.casefold().endswith(suffix):
        value = value[: -len(suffix)].rstrip("/")
    return value


def chat_completions_url(base_url: Any = None) -> str:
    """Return the exact endpoint used by the OpenAI-compatible client."""

    return f"{normalize_base_url(base_url)}{DEEPINFRA_CHAT_COMPLETIONS_PATH}"


def sanitize_chat_payload(
    payload: Mapping[str, Any],
    *,
    config: Any | None = None,
    model: Any = None,
) -> dict[str, Any]:
    """Apply model metadata to a DeepInfra chat payload.

    The selected model is required and copied exactly after whitespace trim.
    Unsupported model capabilities are removed at this last local boundary so
    SDK extension fields cannot resurrect a disabled feature on the wire.
    """

    if not isinstance(payload, Mapping):
        raise TypeError("DeepInfra chat payload must be a mapping")
    result = dict(payload)
    selected_model = normalize_model_id(
        result.get("model") if model is None else model
    )
    if not selected_model:
        raise ValueError("DeepInfra chat payload requires a model ID")
    result["model"] = selected_model

    if model_capability(config, "supports_tools", model=model) is False:
        for key in ("tools", "tool_choice", "functions", "function_call"):
            result.pop(key, None)
    if model_capability(config, "supports_response_format", model=model) is False:
        result.pop("response_format", None)

    options = reasoning_effort_options(config, selected_model)
    top_level_effort = str(result.get("reasoning_effort") or "").strip().lower()
    if top_level_effort and top_level_effort not in options:
        result.pop("reasoning_effort", None)

    extra_body = result.get("extra_body")
    if isinstance(extra_body, Mapping):
        extra_body = dict(extra_body)
        if model_capability(config, "supports_extra_body", model=model) is False:
            result.pop("extra_body", None)
        else:
            # ``extra_body`` is flattened into the SDK JSON body.  Remove
            # standard keys here so provider extensions cannot override the
            # reviewed model or resurrect disabled capabilities.
            extra_body.pop("model", None)
            if model_capability(config, "supports_tools", model=model) is False:
                for key in ("tools", "tool_choice", "functions", "function_call"):
                    extra_body.pop(key, None)
            if model_capability(config, "supports_response_format", model=model) is False:
                extra_body.pop("response_format", None)
            effort = str(extra_body.get("reasoning_effort") or "").strip().lower()
            if effort and effort not in options:
                extra_body.pop("reasoning_effort", None)
            if extra_body:
                result["extra_body"] = extra_body
            else:
                result.pop("extra_body", None)
    return result


__all__ = [
    "DEEPINFRA_CHAT_COMPLETIONS_PATH",
    "DEEPINFRA_DEFAULT_BASE_URL",
    "DEEPINFRA_DEFAULT_MODEL",
    "DEEPINFRA_MODEL_CAPABILITIES_CONFIG_KEY",
    "DEEPINFRA_REASONING_EFFORT_OPTIONS",
    "chat_completions_url",
    "model_capabilities",
    "model_capability",
    "normalize_base_url",
    "normalize_model_id",
    "reasoning_effort_default",
    "reasoning_effort_options",
    "resolve_reasoning_effort",
    "sanitize_chat_payload",
]
