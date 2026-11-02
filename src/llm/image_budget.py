"""Request-local bounds for verified host image inputs; never a wire credential."""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import math
import warnings
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any, Iterator, Mapping, Sequence

from PIL import Image, UnidentifiedImageError

# OpenAI vision and model documentation, checked 2026-11-01:
# https://developers.openai.com/api/docs/guides/images-vision
# https://developers.openai.com/api/docs/models/gpt-5.6-sol
# The bounded 1024-square envelope costs ceil(1024 patches * 1.2) + 1.
# Smaller images reserve the same envelope; unknown models stay unbounded.
_MODELS = frozenset({"gpt-5.6-sol", "gpt-5.6"})
_MIMES = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP", "image/gif": "GIF"}
_MEDIA_TYPES = frozenset({"image_url", "input_image", "input_audio", "input_file", "file", "audio", "video", "item_reference"})
_MAX_BYTES = 25 * 1024 * 1024
_IMAGE_TOKENS = math.ceil(1024 * 1.2) + 1


@dataclass(frozen=True)
class _Proof:
    model: str
    digest: str
    mime: str
    width: int
    height: int
    price: str
    cost_limit_micros: int | None = None
    frames: tuple[tuple[str, str], ...] = ()


_current: ContextVar[_Proof | None] = ContextVar("verified_image_budget", default=None)


def _validated_image(data: bytes, mime: str) -> tuple[int, int]:
    if not isinstance(data, bytes) or not 0 < len(data) <= _MAX_BYTES or mime not in _MIMES:
        raise ValueError("unbounded image")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != _MIMES[mime] or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("image format mismatch")
                width, height = image.size
                if not (0 < width <= 1024 and 0 < height <= 1024):
                    raise ValueError("image dimensions unsupported")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except (OSError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ValueError("invalid image") from exc
    return width, height


def validate_image_price_rule(model: str, rule: Any) -> None:
    """Reject missing, non-token, tiered or unbounded pricing metadata.

    The documented image tokens use ordinary input-token pricing.  A rule is
    still required even when the current reservation is in token units.
    Pricing reference: https://developers.openai.com/api/docs/pricing (2026-11-01).
    """
    from .openai_model_context_registry import openai_model_context_spec

    spec = openai_model_context_spec(model)
    if (model not in _MODELS or spec is None or spec.model_id != model
            or type(spec.context_window_tokens) is not int or spec.context_window_tokens <= 0
            or rule is None or getattr(rule, "provider", None) != "openai"
            or getattr(rule, "canonical_model", None) != "gpt-5.6-sol"
            or getattr(rule, "pricing_kind", None) != "flat_token"
            or getattr(rule, "tiers", None)):
        raise ValueError("image price unavailable")
    if model != rule.canonical_model and model not in (getattr(rule, "aliases", ()) or ()):
        raise ValueError("image price model mismatch")
    rates = getattr(rule, "rates", None)
    long_context = getattr(rule, "long_context", None)
    if not isinstance(rates, Mapping) or not isinstance(long_context, Mapping):
        raise ValueError("image price modifiers unavailable")
    try:
        values = [Decimal(str(rates[key])) for key in ("input", "output", "cached_input", "cache_write")]
        multipliers = [Decimal(str(long_context[key])) for key in ("input_multiplier", "output_multiplier")]
        threshold = long_context["threshold_tokens"]
        if (any(not value.is_finite() or value <= 0 for value in values)
                or any(not value.is_finite() or value < 1 for value in multipliers)
                or type(threshold) is not int or threshold <= 0):
            raise ValueError("invalid image price")
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise ValueError("image price unavailable") from exc


def serialize_image_price_rule(rule: Any, model: str) -> dict[str, Any]:
    """Freeze the exact token rule without an editable request-side override."""
    validate_image_price_rule(model, rule)
    if (set(rule.rates) != {"input", "output", "cached_input", "cache_write"}
            or set(rule.long_context) != {"threshold_tokens", "input_multiplier", "output_multiplier"}
            or getattr(rule, "tool_rates", None)):
        raise ValueError("unsupported image price modifier")
    rule_id, version = getattr(rule, "rule_id", None), getattr(rule, "catalog_version", None)
    if not isinstance(rule_id, str) or not rule_id or not isinstance(version, str) or not version:
        raise ValueError("image price provenance unavailable")
    return {"model": model, "rule_id": rule_id, "catalog_version": version,
            "rates": {key: str(rule.rates[key]) for key in ("input", "output", "cached_input", "cache_write")},
            "long_context": {"threshold_tokens": rule.long_context["threshold_tokens"],
                             "input_multiplier": str(rule.long_context["input_multiplier"]),
                             "output_multiplier": str(rule.long_context["output_multiplier"])}}


def _image_price_rule(value: Any):
    from datetime import datetime
    from ..services.pricing import PricingRuleSnapshot

    if (not isinstance(value, Mapping) or set(value) != {"model", "rule_id", "catalog_version", "rates", "long_context"}
            or not isinstance(value.get("rates"), Mapping) or not isinstance(value.get("long_context"), Mapping)):
        raise ValueError("invalid image price projection")
    try:
        rule = PricingRuleSnapshot(
            rule_id=value["rule_id"], provider="openai", canonical_model="gpt-5.6-sol", aliases=("gpt-5.6",),
            pricing_kind="flat_token", rates={k: Decimal(str(v)) for k, v in value["rates"].items()},
            tiers=(), long_context={"threshold_tokens": value["long_context"]["threshold_tokens"],
                "input_multiplier": Decimal(str(value["long_context"]["input_multiplier"])),
                "output_multiplier": Decimal(str(value["long_context"]["output_multiplier"]))},
            tool_rates={}, effective_from=datetime(2000, 1, 1), effective_to=None,
            source="https://developers.openai.com/api/docs/pricing", catalog_version=value["catalog_version"])
        if serialize_image_price_rule(rule, value["model"]) != dict(value):
            raise ValueError("noncanonical image price projection")
        return rule
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise ValueError("invalid image price projection") from exc


def validate_image_price_projection(value: Any) -> dict[str, Any]:
    _image_price_rule(value)
    return dict(value)


def current_image_price() -> dict[str, Any] | None:
    proof = _current.get()
    return json.loads(proof.price) if proof else None


def bind_image_cost_limit(budget: Mapping[str, Any]) -> None:
    """Bind only the existing server-projected cost ceiling to this request."""
    proof = _current.get()
    if proof is None:
        return
    keys = ("max_cost_micros", "provider_reserved_cost_micros")
    supplied = [budget[key] for key in keys if budget.get(key) is not None]
    if not supplied:
        return
    price = json.loads(proof.price)
    card = budget.get("server_rate_card")
    if (not isinstance(card, Mapping) or card.get("provider") != "openai"
            or card.get("model") != proof.model or card.get("currency") != "USD"
            or card.get("image_price") != price
            or any(type(value) is not int or value < 0 for value in supplied)):
        raise ValueError("image cost proof unavailable")
    limit = min(supplied)
    if budget.get("provider_reserved_cost_micros") is None:
        spent, held = budget.get("cost_micros"), budget.get("reserved_cost_micros")
        if (budget.get("usage_state") != "known" or type(spent) is not int or spent < 0
                or type(held) is not int or held < 0):
            raise ValueError("image remaining cost unavailable")
        limit = max(0, limit - spent - held)
    if proof.cost_limit_micros is not None:
        limit = min(limit, proof.cost_limit_micros)
    _current.set(replace(proof, cost_limit_micros=limit))


def current_image_cost_limit() -> int | None:
    proof = _current.get()
    return proof.cost_limit_micros if proof else None


def image_price_cost_micros(price: Mapping[str, Any], *, input_tokens: int,
                            output_tokens: int, cached_tokens: int, cache_write_tokens: int) -> int:
    """Price actual token counts through the existing pricing engine."""
    from ..services.pricing import PricingEngine, UsageInput

    counts = (input_tokens, output_tokens, cached_tokens, cache_write_tokens)
    if any(type(value) is not int or value < 0 for value in counts) or cached_tokens + cache_write_tokens > input_tokens:
        raise ValueError("invalid image usage")
    rule = _image_price_rule(price)
    engine = PricingEngine([rule])
    result = engine.calculate(UsageInput(provider="openai", requested_model=price["model"],
        input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens))
    if result.is_unknown or result.pricing_status != "priced":
        raise ValueError("image price unavailable")
    return int((result.list_total_cost * Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING))


def quote_image_cost_micros(price: Mapping[str, Any], input_tokens: int, output_tokens: int) -> int:
    """Reserve the highest input mode plus a micro-unit for engine rounding."""
    return 1 + max(image_price_cost_micros(price, input_tokens=input_tokens, output_tokens=output_tokens,
                                           cached_tokens=cached, cache_write_tokens=write)
                   for cached, write in ((0, 0), (0, input_tokens), (input_tokens, 0)))


def actual_image_cost_micros(price: Mapping[str, Any], usage: Mapping[str, Any]) -> int | None:
    """Price only a complete reported receipt; missing components stay unknown."""
    from ..services.pricing import PricingEngine

    if not isinstance(usage, Mapping):
        return None
    try:
        rule = _image_price_rule(price)
        if PricingEngine([rule]).resolve("openai", usage.get("resolved_model")) != rule:
            return None
    except (ValueError, TypeError):
        return None
    fields = ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens", "total_tokens")
    values = [usage.get(key) for key in fields]
    if any(type(value) is not int or value < 0 for value in values):
        return None
    input_tokens, output_tokens, cached_tokens, cache_write_tokens, total_tokens = values
    if (usage.get("total_tokens_source") != "reported" or total_tokens != input_tokens + output_tokens
            or cached_tokens + cache_write_tokens > input_tokens):
        return None
    try:
        return image_price_cost_micros(price, input_tokens=input_tokens, output_tokens=output_tokens,
                                       cached_tokens=cached_tokens, cache_write_tokens=cache_write_tokens)
    except ValueError:
        return None


def normalize_openai_image_response(response: Any, price: Mapping[str, Any]) -> dict[str, Any] | None:
    """Normalize a complete official Chat Completions receipt, or retain unknown.

    OpenAI documents omitted cached_tokens and cache_write_tokens as default 0:
    https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
    (checked 2026-11-01). The enclosing details object must be present.
    """
    from ..services.pricing import PricingEngine

    missing = object()

    def field(obj: Any, name: str) -> Any:
        if isinstance(obj, Mapping):
            return obj.get(name, missing)
        fields_set = getattr(obj, "model_fields_set", None)
        if not isinstance(fields_set, (set, frozenset)) or name not in fields_set:
            return missing
        return getattr(obj, name, missing)

    try:
        rule = _image_price_rule(price)
        model = field(response, "model")
        if (field(response, "object") != "chat.completion"
                or field(response, "service_tier") != "default"
                or not isinstance(model, str)
                or PricingEngine([rule]).resolve("openai", model) != rule):
            return None
        usage = field(response, "usage")
        details = field(usage, "prompt_tokens_details")
        if usage is missing or details is missing or details is None:
            return None
        if not isinstance(details, Mapping) and not isinstance(
                getattr(details, "model_fields_set", None), (set, frozenset)):
            return None
        input_tokens = field(usage, "prompt_tokens")
        output_tokens = field(usage, "completion_tokens")
        total = field(usage, "total_tokens")
        cached = field(details, "cached_tokens")
        write = field(details, "cache_write_tokens")
        defaults = []
        if cached is missing:
            cached = 0
            defaults.append("cached_tokens")
        if write is missing:
            write = 0
            defaults.append("cache_write_tokens")
        audio = field(details, "audio_tokens")
        if audio is not missing and (type(audio) is not int or audio != 0):
            return None
        completion_details = field(usage, "completion_tokens_details")
        if completion_details is not missing and completion_details is not None:
            if not isinstance(completion_details, Mapping) and not isinstance(
                    getattr(completion_details, "model_fields_set", None), (set, frozenset)):
                return None
            output_audio = field(completion_details, "audio_tokens")
            if output_audio is not missing and (type(output_audio) is not int or output_audio != 0):
                return None
        counts = (input_tokens, output_tokens, total, cached, write)
        if (any(type(value) is not int or value < 0 for value in counts)
                or total != input_tokens + output_tokens or cached + write > input_tokens):
            return None
        cost = image_price_cost_micros(price, input_tokens=input_tokens, output_tokens=output_tokens,
                                       cached_tokens=cached, cache_write_tokens=write)
        return {"resolved_model": model, "input_tokens": input_tokens, "output_tokens": output_tokens,
                "total_tokens": total, "total_tokens_source": "reported", "cached_tokens": cached,
                "cache_write_tokens": write, "defaulted_fields": defaults,
                "cost_micros": cost, "currency": "USD"}
    except (ValueError, TypeError, KeyError):
        return None


@contextmanager
def image_budget_scope(model: str, resolved_inputs: Sequence[Any], *, price_rule: Any) -> Iterator[None]:
    """Bind one resolved, pinned image and its selected exact model to this task."""
    from src.services.agent_delegation_media_inputs import ResolvedMediaInput

    validate_image_price_rule(model, price_rule)
    if model not in _MODELS or len(resolved_inputs) != 1:
        raise ValueError("unbounded model or image count")
    item = resolved_inputs[0]
    if not isinstance(item, ResolvedMediaInput):
        raise ValueError("unverified image")
    digest = hashlib.sha256(item.data).hexdigest() if isinstance(item.data, bytes) else ""
    if digest != getattr(item.pin, "bytes_sha256", None):
        raise ValueError("image pin mismatch")
    width, height = _validated_image(item.data, item.mime_type)
    price = serialize_image_price_rule(price_rule, model)
    token = _current.set(_Proof(model, digest, item.mime_type, width, height,
                                json.dumps(price, sort_keys=True, separators=(",", ":"))))
    try:
        yield
    finally:
        _current.reset(token)


@contextmanager
def video_frames_budget_scope(model: str, frames: Sequence[Any], *, price_rule: Any) -> Iterator[None]:
    """Bound host-derived samples for visual review, without authorizing their source."""
    from uuid import UUID

    from src.services.agent_delegation_media_inputs import MediaInputPin
    from src.services.agent_team_video_review_inputs import DerivedVideoFrame

    validate_image_price_rule(model, price_rule)
    if not isinstance(frames, (tuple, list)) or not 1 <= len(frames) <= 3:
        raise ValueError("unbounded frame count")
    if any(not isinstance(frame, DerivedVideoFrame) for frame in frames):
        raise ValueError("unverified frame")
    pin = frames[0].source_pin
    if not isinstance(pin, MediaInputPin):
        raise ValueError("invalid video pin")
    identifiers = (pin.asset_revision_id, pin.output_id, pin.work_id, pin.agent_run_id,
                   pin.plan_id, pin.generation_run_id)
    hashes = (pin.bytes_sha256, pin.revision_hash, pin.plan_hash,
              pin.generation_run_hash, pin.output_provenance_hash)
    if (any(not isinstance(value, UUID) for value in identifiers)
            or any(not isinstance(value, str) or len(value) != 64
                   or any(c not in "0123456789abcdef" for c in value) for value in hashes)):
        raise ValueError("invalid video pin")
    verified = []
    previous = -1.0
    for frame in frames:
        timestamp = frame.timestamp_seconds
        if (frame.source_pin != pin or frame.source_video_sha256 != pin.bytes_sha256
                or type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                or timestamp < 0 or timestamp <= previous or frame.mime_type != "image/jpeg"):
            raise ValueError("invalid video frame provenance")
        if not isinstance(frame.data, bytes) or hashlib.sha256(frame.data).hexdigest() != frame.frame_sha256:
            raise ValueError("frame digest mismatch")
        width, height = _validated_image(frame.data, frame.mime_type)
        verified.append((frame.frame_sha256, frame.mime_type))
        previous = timestamp
    price = serialize_image_price_rule(price_rule, model)
    token = _current.set(_Proof(model, verified[0][0], verified[0][1], width, height,
                                json.dumps(price, sort_keys=True, separators=(",", ":")),
                                frames=tuple(verified)))
    try:
        yield
    finally:
        _current.reset(token)


def initial_image_tokens() -> int | None:
    """Return the active validated bound, or None when this is a text request."""
    proof = _current.get()
    return _IMAGE_TOKENS * (len(proof.frames) or 1) if proof is not None else None


def bounded_image_text_tokens(payload: Mapping[str, Any]) -> int:
    """Use one byte per token plus framing after verified image bytes are removed."""
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    framed = payload.get("messages") or payload.get("input")
    count = len(framed) if isinstance(framed, (list, tuple)) else 1 if framed is not None else 0
    return len(serialized.encode("utf-8")) + 64 * (1 + count)


def sanitize_image_payload(payload: Mapping[str, Any], model: str) -> tuple[dict[str, Any], int]:
    """Remove only verified image bytes from a copy used for text estimation."""
    proof = _current.get()
    if "model" in payload and payload["model"] != model:
        raise ValueError("payload model mismatch")
    if proof is not None and payload.get("service_tier") not in (None, "auto", "default"):
        raise ValueError("unsupported image billing tier")
    if proof is not None and (payload.get("prompt_cache_options") is not None
            or payload.get("prompt_cache_retention") is not None
            or (isinstance(payload.get("extra_body"), Mapping)
                and payload["extra_body"].get("prompt_cache_retention") is not None)):
        raise ValueError("unsupported image cache policy")
    seen = 0

    def visit(value: Any, path: tuple[Any, ...] = ()) -> Any:
        nonlocal seen
        if isinstance(value, Mapping):
            if proof is not None and "prompt_cache_breakpoint" in value:
                raise ValueError("unsupported image cache breakpoint")
            kind = value.get("type")
            if kind in _MEDIA_TYPES:
                if not (len(path) == 4 and path[0] == "messages" and type(path[1]) is int
                        and path[2] == "content" and type(path[3]) is int):
                    raise ValueError("unsupported image location")
                if kind != "image_url" or proof is None or model != proof.model:
                    raise ValueError("unbounded media")
                if set(value) != {"type", "image_url"}:
                    raise ValueError("unsupported image fields")
                obj = value["image_url"]
                if not isinstance(obj, Mapping) or set(obj) - {"url", "detail"}:
                    raise ValueError("unsupported image URL")
                if obj.get("detail", "auto") not in {"auto", "high"}:
                    raise ValueError("unsupported image detail")
                url = obj.get("url")
                expected = proof.frames or ((proof.digest, proof.mime),)
                if seen >= len(expected):
                    raise ValueError("unbounded image count")
                digest, mime = expected[seen]
                prefix = f"data:{mime};base64,"
                if (not isinstance(url, str) or not url.startswith(prefix)
                        or len(url) > len(prefix) + 4 * ((_MAX_BYTES + 2) // 3)):
                    raise ValueError("image URL mismatch")
                try:
                    data = base64.b64decode(url[len(prefix):], validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise ValueError("invalid image encoding") from exc
                if hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError("image digest mismatch")
                seen += 1
                # Keep a structural placeholder so framing remains counted.
                return {"type": "text", "text": "[verified image]"}
            return {key: visit(item, (*path, key)) for key, item in value.items()}
        if isinstance(value, list):
            return [visit(item, (*path, index)) for index, item in enumerate(value)]
        if isinstance(value, tuple):
            return tuple(visit(item, (*path, index)) for index, item in enumerate(value))
        return value

    result = visit(payload)
    if proof is not None and (model != proof.model or seen != (len(proof.frames) or 1)):
        raise ValueError("image proof does not match request")
    return result, _IMAGE_TOKENS * seen
