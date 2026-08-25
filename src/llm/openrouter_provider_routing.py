"""OpenRouter のモデル別 provider routing 設定。"""

from __future__ import annotations

import os
import re
import urllib.parse
import json
import urllib.request
from typing import Any, Callable, Dict, List, Optional


MODEL_PROVIDER_OPTIONS_CONFIG_KEY = "openrouter.model_provider_options"
_PROVIDER_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_PROVIDER_OPTION_KEYS = {"only", "order", "allow_fallbacks", "zdr"}


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is not None and hasattr(config, "get"):
        try:
            value = config.get(key, None)
        except Exception:  # noqa: BLE001
            value = None
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


def _normalize_slug_list(value: Any, *, field: str, strict: bool) -> List[str]:
    if not isinstance(value, (list, tuple)):
        if strict:
            raise ValueError(f"{field} は provider slug の配列で指定してください")
        return []
    result: List[str] = []
    for item in value:
        slug = str(item or "").strip()
        if not slug:
            continue
        if not _PROVIDER_SLUG_PATTERN.fullmatch(slug):
            if strict:
                raise ValueError(f"{field} に不正な provider slug があります: {slug}")
            continue
        if slug not in result:
            result.append(slug)
    return result


def normalize_provider_options(
    value: Any,
    *,
    strict: bool = False,
) -> Dict[str, Any]:
    """Normalize the provider object sent in an OpenRouter request.

    Empty arrays are removed so an empty policy remains equivalent to
    OpenRouter's automatic routing. Boolean ``False`` values are retained.
    """

    if value is None:
        return {}
    if not isinstance(value, dict):
        if strict:
            raise ValueError("provider は object で指定してください")
        return {}

    if strict:
        unknown = set(value) - _PROVIDER_OPTION_KEYS
        if unknown:
            raise ValueError(f"未対応の OpenRouter provider 設定です: {sorted(unknown)}")

    result: Dict[str, Any] = {}
    for field in ("only", "order"):
        if field in value:
            normalized = _normalize_slug_list(value.get(field), field=field, strict=strict)
            if normalized:
                result[field] = normalized

    for field in ("allow_fallbacks", "zdr"):
        if field not in value:
            continue
        boolean_value = value.get(field)
        if not isinstance(boolean_value, bool):
            if strict:
                raise ValueError(f"{field} は boolean で指定してください")
            continue
        result[field] = boolean_value
    return result


def normalize_model_provider_options(
    value: Any,
    *,
    strict: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Normalize the persisted ``model slug -> provider object`` mapping."""

    if value is None:
        return {}
    if not isinstance(value, dict):
        if strict:
            raise ValueError("モデル別 OpenRouter provider 設定は object で指定してください")
        return {}

    result: Dict[str, Dict[str, Any]] = {}
    for raw_model, raw_options in value.items():
        model = str(raw_model or "").strip()
        if not model:
            if strict:
                raise ValueError("モデルslugが空の OpenRouter provider 設定があります")
            continue
        # Accept the natural persisted shape and tolerate a nested provider
        # object from early callers without sending that wrapper to OpenRouter.
        if isinstance(raw_options, dict) and isinstance(raw_options.get("provider"), dict):
            raw_options = raw_options["provider"]
        options = normalize_provider_options(raw_options, strict=strict)
        if options:
            result[model] = options
    return result


def provider_options_for_model(config: Any, model: str) -> Dict[str, Any]:
    model_id = str(model or "").strip()
    if not model_id:
        return {}
    configured = _config_get(config, MODEL_PROVIDER_OPTIONS_CONFIG_KEY, {})
    normalized = normalize_model_provider_options(configured)
    return dict(normalized.get(model_id) or {})


def merge_provider_options_into_extra_body(
    extra_body: Any,
    config: Any,
    model: str,
) -> Dict[str, Any]:
    """Merge a model policy into an existing OpenRouter extra_body.

    Existing provider constraints (for example the free-team zero-price
    constraint) remain authoritative while model-level fields are preserved.
    """

    merged = dict(extra_body) if isinstance(extra_body, dict) else {}
    configured = provider_options_for_model(config, model)
    if not configured:
        return merged
    existing = merged.get("provider")
    if isinstance(existing, dict):
        merged["provider"] = {**configured, **existing}
    else:
        merged["provider"] = configured
    return merged


def default_fetch_json(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 5.0,
) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _openrouter_api_url(config: Any) -> str:
    base_url = str(
        _config_get(config, "openrouter.base_url")
        or os.environ.get("OPENROUTER_BASE_URL")
        or "https://openrouter.ai/api/v1"
    ).rstrip("/")
    return base_url


def _endpoint_url(config: Any, model: str) -> Optional[str]:
    author, separator, slug = str(model or "").strip().partition("/")
    if not separator or not author or not slug:
        return None
    return (
        f"{_openrouter_api_url(config)}/models/"
        f"{urllib.parse.quote(author, safe='')}/"
        f"{urllib.parse.quote(slug, safe=':')}/endpoints"
    )


def _provider_directory_url(config: Any) -> str:
    return f"{_openrouter_api_url(config)}/providers"


def _provider_directory(payload: Any) -> Dict[str, str]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return {}
    result: Dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or "").strip()
        name = str(item.get("name") or "").strip()
        if slug and name:
            result[name.casefold()] = slug
    return result


def _endpoint_provider_values(endpoint: Dict[str, Any]) -> tuple[str, str]:
    raw_name = endpoint.get("provider_name")
    name = ""
    slug = ""
    if isinstance(raw_name, dict):
        name = str(raw_name.get("name") or "").strip()
        slug = str(
            raw_name.get("slug")
            or raw_name.get("provider_slug")
            or raw_name.get("id")
            or ""
        ).strip()
    elif isinstance(raw_name, str):
        name = raw_name.strip()

    # provider_tag is the exact endpoint slug on newer endpoint payloads;
    # provider_slug is the stable base slug on older payloads.
    for key in ("provider_tag", "provider_slug", "tag", "provider"):
        candidate = endpoint.get(key)
        if isinstance(candidate, dict):
            slug = slug or str(candidate.get("slug") or candidate.get("id") or "").strip()
            name = name or str(candidate.get("name") or "").strip()
        elif isinstance(candidate, str) and candidate.strip() and candidate.strip().lower() != "default":
            slug = candidate.strip()
            break
    return slug, name


def parse_provider_candidates(
    endpoint_payload: Any,
    provider_payload: Any = None,
) -> List[Dict[str, str]]:
    """Extract exact provider slugs and display labels from endpoint metadata."""

    data = endpoint_payload.get("data") if isinstance(endpoint_payload, dict) else None
    if not isinstance(data, dict) and isinstance(endpoint_payload, dict):
        data = endpoint_payload
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(endpoints, list):
        return []

    directory = _provider_directory(provider_payload)
    result: List[Dict[str, str]] = []
    seen: set[str] = set()
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        slug, name = _endpoint_provider_values(endpoint)
        if not slug and name:
            slug = directory.get(name.casefold(), "")
        if not slug or not _PROVIDER_SLUG_PATTERN.fullmatch(slug) or slug in seen:
            continue
        seen.add(slug)
        result.append({"slug": slug, "label": name or slug})
    return result


def fetch_provider_candidates(
    config: Any,
    model: str,
    *,
    fetch_json: Callable[..., Dict[str, Any]] = default_fetch_json,
) -> List[Dict[str, str]]:
    """Fetch providers for one model from OpenRouter endpoint metadata."""

    url = _endpoint_url(config, model)
    if not url:
        return []
    api_key = str(
        _config_get(config, "openrouter_api_key")
        or os.environ.get("OPENROUTER_API_KEY")
        or ""
    ).strip()
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    endpoint_payload = fetch_json(url, headers=headers, timeout=5.0)
    candidates = parse_provider_candidates(endpoint_payload)
    # Some endpoint responses expose only the display name. Resolve that name
    # through the official provider directory instead of persisting a label.
    data = endpoint_payload.get("data") if isinstance(endpoint_payload, dict) else None
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    needs_directory = isinstance(endpoints, list) and any(
        isinstance(endpoint, dict)
        and not _endpoint_provider_values(endpoint)[0]
        and bool(_endpoint_provider_values(endpoint)[1])
        for endpoint in endpoints
    )
    if candidates and not needs_directory:
        return candidates
    provider_payload = fetch_json(
        _provider_directory_url(config),
        headers=headers,
        timeout=5.0,
    )
    return parse_provider_candidates(endpoint_payload, provider_payload)
