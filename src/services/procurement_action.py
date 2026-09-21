"""Closed procurement inputs. Quotes are adapter evidence, never model input."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator


class ProcurementPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    fixed_item_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")
    allowed_item_refs: list[str] | None = Field(default=None, min_length=1, max_length=64)
    fixed_ship_to_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")
    allowed_ship_to_refs: list[str] | None = Field(default=None, min_length=1, max_length=64)
    min_quantity: StrictInt = Field(default=1, ge=1, le=10000)
    max_quantity: StrictInt = Field(ge=1, le=10000)
    default_quantity: StrictInt = Field(default=1, ge=1, le=10000)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    max_order_total_minor: StrictInt = Field(ge=1, le=100000000000)
    require_quote_before_execute: StrictBool = True

    @model_validator(mode="after")
    def bounded(self):
        import re
        for fixed, allowed in ((self.fixed_item_ref, self.allowed_item_refs), (self.fixed_ship_to_ref, self.allowed_ship_to_refs)):
            if bool(fixed) == bool(allowed):
                raise ValueError("exactly one fixed or allowed target required")
            if allowed and any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}", x) for x in allowed):
                raise ValueError("invalid target reference")
        if not self.min_quantity <= self.default_quantity <= self.max_quantity:
            raise ValueError("invalid quantity bounds")
        return self


class ProcurementPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    item_ref: str
    quantity: StrictInt
    ship_to_ref: str
    currency: str


class ProcurementQuote(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    quote_ref: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    item_ref: str
    quantity: StrictInt
    ship_to_ref: str
    currency: str
    total_minor: StrictInt = Field(ge=0)
    observed_at: datetime
    expires_at: datetime


def normalize_policy(value):
    return ProcurementPolicy.model_validate(value).model_dump(exclude_none=True)


def normalize_payload(value, policy):
    p = ProcurementPolicy.model_validate(policy)
    defaults = {"item_ref": p.fixed_item_ref, "ship_to_ref": p.fixed_ship_to_ref,
                "quantity": p.default_quantity, "currency": p.currency}
    value = dict(value)
    quote = value.pop("quote", None)
    payload = ProcurementPayload.model_validate({**defaults, **value})
    if payload.item_ref not in (p.allowed_item_refs or [p.fixed_item_ref]):
        raise ValueError("item outside policy")
    if payload.ship_to_ref not in (p.allowed_ship_to_refs or [p.fixed_ship_to_ref]):
        raise ValueError("destination outside policy")
    if payload.currency != p.currency or not p.min_quantity <= payload.quantity <= p.max_quantity:
        raise ValueError("quantity or currency outside policy")
    result = payload.model_dump()
    if quote is not None:
        import json
        normalized_quote = ProcurementQuote.model_validate_json(json.dumps(quote)).model_dump(mode="json")
        if any(normalized_quote[key] != item for key, item in result.items()):
            raise ValueError("quote target mismatch")
        result["quote"] = normalized_quote
    return result


def validate_quote(value, payload, policy, *, now=None, enforce_ceiling=True):
    if isinstance(value, dict) and isinstance(value.get("observed_at"), str):
        import json
        quote = ProcurementQuote.model_validate_json(json.dumps(value))
    else:
        quote = ProcurementQuote.model_validate(value)
    now = now or datetime.now(timezone.utc)
    def utc(dt):
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    if any(getattr(quote, key) != value for key, value in payload.items()):
        raise ValueError("quote target mismatch")
    if not now - timedelta(minutes=5) <= utc(quote.observed_at) <= now or utc(quote.expires_at) <= now:
        raise ValueError("quote expired")
    if enforce_ceiling and quote.total_minor > policy["max_order_total_minor"]:
        raise ValueError("quote exceeds policy")
    return quote
