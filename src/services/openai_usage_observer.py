"""Read-only, secret-safe OpenAI organization completions usage observer."""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

import httpx

OPENAI_USAGE_ENDPOINT = "https://api.openai.com/v1/organization/usage/completions"
OPENAI_USAGE_TIMEOUT_SECONDS = 8.0
OPENAI_USAGE_MAX_RETRIES = 1
OPENAI_USAGE_MAX_PAGES = 100
MAX_SAFE_INTEGER = 2**53 - 1
_SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


class OpenAIUsageObservationError(RuntimeError):
    """Fixed non-secret error code; never embeds a response or exception body."""


@dataclass(frozen=True)
class OpenAIUsageSnapshot:
    utc_day: str
    groups: dict[str, dict[str, int]]
    unknown_models: tuple[dict[str, Any], ...]
    pages: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "utc_day": self.utc_day,
            "groups": {key: dict(value) for key, value in self.groups.items()},
            "unknown_models": [dict(item) for item in self.unknown_models],
            "pages": self.pages,
        }


def utc_day_start(value: datetime) -> tuple[str, int]:
    current = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.date().isoformat(), int(start.timestamp())


def _count(value: Any, *, optional: bool = False) -> int:
    if optional and value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OpenAIUsageObservationError("parse_error")
    if isinstance(value, float) and (
        not math.isfinite(value)
        or value != int(value)
        or abs(value) > MAX_SAFE_INTEGER
    ):
        raise OpenAIUsageObservationError("parse_error")
    if value < 0 or value > MAX_SAFE_INTEGER:
        raise OpenAIUsageObservationError("parse_error")
    return int(value)


def _model_group(model: str, groups: Mapping[str, list[str]]) -> str | None:
    for group in ("1m", "10m"):
        for known_model in groups.get(group, ()):
            if model == known_model or model.startswith(f"{known_model}-"):
                return group
    return None


def _canonical_free_groups() -> dict[str, list[str]]:
    from .pricing.catalog import load_catalog_file

    catalog = load_catalog_file()
    groups = catalog.free_incentive_groups
    return {
        name: list(models)
        for name, models in groups.items()
        if name in {"1m", "10m"}
    }


def _empty_group() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "input_cached_tokens": 0,
        "total_tokens": 0,
        "requests": 0,
    }


def aggregate_usage_pages(
    pages: list[Mapping[str, Any]],
    *,
    utc_day: str,
    free_groups: Mapping[str, list[str]] | None = None,
) -> OpenAIUsageSnapshot:
    groups = free_groups if free_groups is not None else _canonical_free_groups()
    totals = {"1m": _empty_group(), "10m": _empty_group()}
    unknown: dict[str, dict[str, int]] = {}

    for page in pages:
        buckets = page.get("data")
        if not isinstance(buckets, list):
            raise OpenAIUsageObservationError("parse_error")
        for bucket in buckets:
            if not isinstance(bucket, Mapping) or not isinstance(bucket.get("results"), list):
                raise OpenAIUsageObservationError("parse_error")
            for result in bucket["results"]:
                if not isinstance(result, Mapping):
                    raise OpenAIUsageObservationError("parse_error")
                raw_model = result.get("model")
                if not isinstance(raw_model, str) or not _SAFE_MODEL_RE.fullmatch(raw_model):
                    raise OpenAIUsageObservationError("parse_error")
                model = raw_model
                # Incomplete bucket rows cannot be treated as zero: that would
                # understate organization usage and loosen quota admission.
                input_tokens = _count(result.get("input_tokens"))
                output_tokens = _count(result.get("output_tokens"))
                cached_tokens = _count(result.get("input_cached_tokens"), optional=True)
                requests = _count(result.get("num_model_requests"))
                if cached_tokens > input_tokens:
                    raise OpenAIUsageObservationError("parse_error")
                group = _model_group(model, groups)
                target = totals[group] if group is not None else unknown.setdefault(
                    model, _empty_group()
                )
                target["input_tokens"] += input_tokens
                target["output_tokens"] += output_tokens
                target["input_cached_tokens"] += cached_tokens
                target["total_tokens"] += input_tokens + output_tokens
                target["requests"] += requests

    unknown_models = tuple(
        {"model": model, **values}
        for model, values in sorted(unknown.items(), key=lambda item: item[0])
    )
    return OpenAIUsageSnapshot(
        utc_day=utc_day,
        groups=totals,
        unknown_models=unknown_models,
        pages=len(pages),
    )


async def _request_page(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    start_time: int,
    page: str | None,
) -> Mapping[str, Any]:
    params: list[tuple[str, str | int]] = [
        ("start_time", start_time),
        ("bucket_width", "1d"),
        ("group_by[]", "model"),
        ("limit", 31),
    ]
    if page is not None:
        params.append(("page", page))
    last_error: OpenAIUsageObservationError | None = None
    for attempt in range(OPENAI_USAGE_MAX_RETRIES + 1):
        try:
            response = await client.get(
                OPENAI_USAGE_ENDPOINT,
                params=params,
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            last_error = OpenAIUsageObservationError("timeout")
            if attempt < OPENAI_USAGE_MAX_RETRIES:
                continue
            raise last_error from None
        except httpx.RequestError:
            last_error = OpenAIUsageObservationError("network_error")
            if attempt < OPENAI_USAGE_MAX_RETRIES:
                continue
            raise last_error from None

        if 300 <= response.status_code < 400:
            raise OpenAIUsageObservationError("redirect_rejected")
        if response.status_code in {401, 403}:
            raise OpenAIUsageObservationError("unauthorized")
        if response.status_code >= 500:
            last_error = OpenAIUsageObservationError("http_5xx")
            if attempt < OPENAI_USAGE_MAX_RETRIES:
                continue
            raise last_error from None
        if response.status_code < 200 or response.status_code >= 300:
            raise OpenAIUsageObservationError(f"http_{response.status_code}")
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError):
            raise OpenAIUsageObservationError("parse_error") from None
        if not isinstance(payload, Mapping):
            raise OpenAIUsageObservationError("parse_error")
        return payload
    raise last_error or OpenAIUsageObservationError("network_error")


async def fetch_openai_usage(
    api_key: str,
    *,
    now: datetime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> OpenAIUsageSnapshot:
    """Fetch all model buckets for the current UTC day from the fixed endpoint."""
    if not isinstance(api_key, str) or not api_key.strip():
        raise OpenAIUsageObservationError("not_configured")
    utc_day, start_time = utc_day_start(now or datetime.now(timezone.utc))
    pages: list[Mapping[str, Any]] = []
    seen_pages: set[str] = set()
    page: str | None = None
    try:
        async with httpx.AsyncClient(
            timeout=OPENAI_USAGE_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            for _ in range(OPENAI_USAGE_MAX_PAGES):
                payload = await _request_page(
                    client,
                    api_key=api_key,
                    start_time=start_time,
                    page=page,
                )
                pages.append(payload)
                next_page = payload.get("next_page")
                if next_page in (None, ""):
                    if payload.get("has_more") is True:
                        raise OpenAIUsageObservationError("parse_error")
                    break
                if not isinstance(next_page, str) or len(next_page) > 512 or next_page in seen_pages:
                    raise OpenAIUsageObservationError("parse_error")
                seen_pages.add(next_page)
                page = next_page
            else:
                raise OpenAIUsageObservationError("parse_error")
    except OpenAIUsageObservationError:
        raise
    except Exception:
        # Do not propagate exception text, URLs, headers, or response bodies.
        raise OpenAIUsageObservationError("network_error") from None
    return aggregate_usage_pages(pages, utc_day=utc_day)
