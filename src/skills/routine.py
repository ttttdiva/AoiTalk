"""Executable browser metadata inside the existing Skill parameter contract."""

from __future__ import annotations

import hashlib
import json
import re
from string import Formatter
from typing import Any, Literal
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROUTINE_PARAMETER = "__routine__"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def assert_public_inputs(value: Any) -> None:
    from ..security.skill_content_privacy import assert_no_secret_like_skill_content

    assert_no_secret_like_skill_content(value)
    # OTPs must be entered by the person in the browser, never in a tool call.
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key)).casefold()
            if (re.search(r"(^|_)(otp|totp|one_time_code|verification_code|pin)($|_)", normalized_key) or
                    any(marker in str(key) for marker in ("パスワード", "暗証番号", "認証コード", "確認コード", "ワンタイム"))):
                raise ValueError("routine_credentials_require_browser_handoff")
            assert_public_inputs(item)
    elif isinstance(value, list):
        for item in value:
            assert_public_inputs(item)


def public_url(url: str) -> bool:
    """Inspect query AND fragment, including nested/encoded OAuth credentials.

    Called before any observed URL reaches the planner, trace or durable proof.
    Ordinary public anchors are allowed; credentials require browser handoff.
    """
    try:
        decoded = str(url)
        if len(decoded) > 8192:
            return False
        for _ in range(8):
            expanded = unquote(decoded)
            if expanded == decoded:
                break
            decoded = expanded
        else:
            return False
        parsed = urlsplit(decoded)
        if parsed.username or parsed.password or parsed.scheme not in {"http", "https"}:
            return False
        for part in (parsed.query, parsed.fragment):
            pairs = parse_qsl(part.replace("?", "&"), keep_blank_values=True)
            for key, value in pairs:
                # Fragments can have a client router prefix ('#/oauth/token=...').
                key = key.rsplit("/", 1)[-1]
                assert_public_inputs({key: value or "present"})
                compact = re.sub(r"[^a-z0-9]", "", key.casefold())
                if compact in {"code", "idtoken", "authorizationcode", "authcode", "sessionid"}:
                    return False
                assert_public_inputs(value)
            # Also catch nested URL/string values and non-standard fragment
            # separators after percent decoding. Never echo the offending URL.
            if re.search(r"(?:^|[?#&;/\s])(?:access[_-]?token|id[_-]?token|refresh[_-]?token|"
                         r"password|passwd|otp|totp|verification[_-]?code|one[_-]?time[_-]?code|"
                         r"authorization[_-]?code|auth[_-]?code|code|session[_-]?id|"
                         r"token|secret|認証コード|パスワード|暗証番号)\s*[:=]", part, re.I):
                return False
        return bool(parsed.hostname)
    except (ValueError, TypeError):
        return False


def format_value(template: str, values: dict[str, Any]) -> str:
    for _, key, spec, conversion in Formatter().parse(template):
        if key is not None and (key not in values or spec or conversion):
            raise ValueError("routine_template_parameter_invalid")
    return template.format(**values)


class RoutineDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    intent: Literal["check", "commit"] = "check"
    start_url: str = Field(default="", max_length=8192)
    preparation_labels: list[str] = Field(default_factory=list, max_length=32)
    commit_label: str = Field(default="", max_length=256)
    confirmation: dict[str, str] = Field(default_factory=dict, max_length=16)
    confirmation_labels: dict[str, str] = Field(default_factory=dict, max_length=16)
    form_values: dict[str, str] = Field(default_factory=dict, max_length=32)
    completion_text: str = Field(min_length=1, max_length=1200)
    completion_url: str = Field(default="", max_length=8192)
    result_fields: dict[str, str] = Field(default_factory=dict, max_length=16)
    result_labels: dict[str, str] = Field(default_factory=dict, max_length=16)

    @model_validator(mode="after")
    def validate_contract(self):
        from .routine_facts import labels_for, normalized

        assert_public_inputs(self.model_dump())
        if not self.result_fields:
            raise ValueError("routine_business_evidence_fields_required")
        if self.intent == "commit":
            if not self.commit_label.strip() or not {"target", "datetime", "price"} <= self.confirmation.keys():
                raise ValueError("routine_confirmation_target_datetime_price_required")
            if not {"target", "datetime", "price"} <= self.result_fields.keys():
                raise ValueError("routine_result_target_datetime_price_required")
            if self.confirmation.keys() != self.result_fields.keys():
                raise ValueError("routine_confirmation_result_fact_keys_must_match")
            if " ".join(self.commit_label.split()) in {" ".join(v.split()) for v in self.preparation_labels}:
                raise ValueError("routine_commit_cannot_be_preparation")
        if any(not str(v).strip() or len(v) > 1200 for v in
               [*self.confirmation.values(), *self.result_fields.values(), *self.preparation_labels,
                *self.form_values.values()]):
            raise ValueError("routine_evidence_value_invalid")
        labels_for(self.confirmation, self.confirmation_labels)
        labels_for(self.result_fields, self.result_labels)
        if len({normalized(k) for k in self.form_values}) != len(self.form_values):
            raise ValueError("routine_form_label_ambiguous")
        return self

    def render(self, values: dict[str, Any]) -> "RoutineDefinition":
        payload = self.model_dump()
        for key in ("start_url", "commit_label", "completion_text", "completion_url"):
            payload[key] = format_value(payload[key], values)
        payload["preparation_labels"] = [format_value(v, values) for v in self.preparation_labels]
        for key in ("confirmation", "result_fields", "form_values"):
            payload[key] = {k: format_value(v, values) for k, v in payload[key].items()}
        rendered = RoutineDefinition.model_validate(payload)
        from .routine_facts import fact_value

        result = {key: fact_value(key, value) for key, value in rendered.result_fields.items()}
        confirmation = {key: fact_value(key, value) for key, value in rendered.confirmation.items()}
        if rendered.intent == "commit" and confirmation != result:
            raise ValueError("routine_confirmation_result_facts_must_match")
        for url in (rendered.start_url, rendered.completion_url):
            parsed = urlsplit(url)
            if url and (parsed.scheme not in {"http", "https"} or not parsed.hostname or
                        parsed.username or parsed.password or parsed.fragment):
                raise ValueError("routine_url_invalid")
            if url and not public_url(url):
                raise ValueError("routine_credentials_require_browser_handoff")
        return rendered


def routine_for(payload: dict[str, Any]) -> RoutineDefinition | None:
    raw = (payload.get("parameters") or {}).get(ROUTINE_PARAMETER)
    if raw is None:
        return None
    return RoutineDefinition.model_validate(raw.get("default", raw))


def effective_values(payload: dict[str, Any], overrides: dict[str, Any], input_text: str) -> dict[str, Any]:
    parameters = payload.get("parameters") or {}
    if set(overrides) - (set(parameters) - {ROUTINE_PARAMETER}):
        raise ValueError("routine_unknown_override")
    values = {k: (v.get("default", "") if isinstance(v, dict) else v)
              for k, v in parameters.items() if k != ROUTINE_PARAMETER}
    values.update(overrides)
    values["input"] = input_text
    assert_public_inputs(values)
    if any(not isinstance(v, (str, int, float, bool)) for v in values.values()):
        raise ValueError("routine_parameter_must_be_scalar")
    return values
