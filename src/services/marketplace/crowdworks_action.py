"""Closed CrowdWorks policy/payload contracts for engagement automation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, model_validator


class CrowdWorksPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    search_urls: list[str] = Field(min_length=1, max_length=20)
    scan_interval_minutes: StrictInt = Field(default=30, ge=5, le=1440)
    max_pages_per_scan: StrictInt = Field(default=3, ge=1, le=20)
    max_opportunities_per_scan: StrictInt = Field(default=100, ge=1, le=500)
    max_applications_per_scan: StrictInt = Field(default=5, ge=1, le=50)
    min_expected_profit: StrictFloat = Field(default=0.0, ge=-1000000000.0, le=1000000000.0)
    min_profit_per_hour: StrictFloat = Field(default=0.0, ge=-1000000000.0, le=1000000000.0)
    currency: str = Field(default="JPY", pattern=r"^[A-Z]{3}$")
    # Deprecated legacy override.  The canonical model source is the Agent's
    # exact AgentRevision -> Agent Team -> Execution Profile -> Subagent route.
    # These fields are honoured only for the evaluation/application phases of
    # an Agent still on the ``manual`` (標準モデル) profile and are ignored as
    # soon as the Agent selects a Team Execution Profile.  Fulfillment never
    # uses them.  Kept so existing Policy revisions stay valid.
    llm_provider: str | None = Field(default=None, max_length=64)
    llm_model: str | None = Field(default=None, max_length=255)
    llm_effort: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def validate_urls(self):
        from urllib.parse import urlsplit

        normalized: list[str] = []
        for raw in self.search_urls:
            if not isinstance(raw, str):
                raise ValueError("search URL must be a string")
            value = raw.strip()
            parsed = urlsplit(value)
            host = (parsed.hostname or "").casefold()
            if parsed.scheme not in {"http", "https"} or host not in {"crowdworks.jp", "www.crowdworks.jp"}:
                raise ValueError("search URL must be a CrowdWorks URL")
            if value not in normalized:
                normalized.append(value)
        self.search_urls = normalized
        provider = (self.llm_provider or "").strip()
        model = (self.llm_model or "").strip()
        if bool(provider) != bool(model):
            raise ValueError("llm_provider and llm_model must be configured together")
        self.llm_provider = provider or None
        self.llm_model = model or None
        self.llm_effort = (self.llm_effort or "").strip() or None
        return self


class CrowdWorksApplicationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    opportunity_id: str = Field(min_length=1, max_length=64)
    application_draft_id: str = Field(min_length=1, max_length=64)
    source_url: str = Field(min_length=1, max_length=8192)
    message: str = Field(min_length=1, max_length=64000)
    offered_price: float | None = None
    currency: str | None = Field(default=None, max_length=16)
    delivery_estimate: str | None = Field(default=None, max_length=255)


def normalize_policy(value):
    return CrowdWorksPolicy.model_validate(value).model_dump()


def normalize_payload(value, policy):
    p = CrowdWorksPolicy.model_validate(policy)
    payload = CrowdWorksApplicationPayload.model_validate(value)
    from urllib.parse import urlsplit

    parsed = urlsplit(payload.source_url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or host not in {"crowdworks.jp", "www.crowdworks.jp"}:
        raise ValueError("source_url must be CrowdWorks")
    if payload.currency and payload.currency != p.currency:
        raise ValueError("currency outside policy")
    return payload.model_dump()


__all__ = [
    "CrowdWorksPolicy",
    "CrowdWorksApplicationPayload",
    "normalize_policy",
    "normalize_payload",
]
