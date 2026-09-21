"""Typed TypeSafe/Jev decisions; no chat completion or browser authority.

Wire contract: https://docs.typesafe.ai/api
The only credential source is JEV_API_KEY in the startup environment. Every
attempt crosses the existing outbound transaction boundary. Neither provider
errors nor request bodies are included in exceptions or logs.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

from .outbound_privacy_service import EgressDescriptor, OutboundPrivacyGateway

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_DEFAULT_MODEL = "jev-1.13.0"
MAX_REQUEST_BYTES = 48_000
MAX_STATE_QUESTION_BYTES = 24_000
MAX_RESPONSE_BYTES = 131_072


class JevError(RuntimeError):
    """A stable error code, never a provider's raw error or a secret."""

    def __init__(self, code: str, *, retry_after: float = 0.0) -> None:
        self.code = code
        self.retry_after = retry_after
        super().__init__(code)


def _encoded(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise JevError("jev_invalid_request") from None


def _probability(value: Any) -> float:
    if (
        type(value) not in {int, float}
        or not 0 <= value <= 1
        or not math.isfinite(value)
    ):
        raise JevError("jev_invalid_response")
    return float(value)


def validate_questions(questions: Any) -> dict[str, Any]:
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 32:
        raise JevError("jev_invalid_questions")
    for key, question in questions.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_]{1,64}", key):
            raise JevError("jev_invalid_questions")
        if not isinstance(question, dict) or set(question) - {
            "type",
            "instructions",
            "criteria",
        }:
            raise JevError("jev_invalid_questions")
        if (
            not isinstance(question.get("instructions"), (str, dict, list))
            or not question["instructions"]
        ):
            raise JevError("jev_invalid_questions")
        kind, criteria = question.get("type"), question.get("criteria")
        if kind == "choice":
            if not isinstance(criteria, dict) or not 1 <= len(criteria) <= 255:
                raise JevError("jev_invalid_questions")
            if any(not isinstance(k, str) or not k or len(k) > 80 for k in criteria):
                raise JevError("jev_invalid_questions")
            if any(
                v is not None and not isinstance(v, (str, dict, list))
                for v in criteria.values()
            ):
                raise JevError("jev_invalid_questions")
        elif kind == "score":
            if not isinstance(criteria, list) or not 2 <= len(criteria) <= 10:
                raise JevError("jev_invalid_questions")
            if any(not isinstance(v, (str, dict, list)) for v in criteria):
                raise JevError("jev_invalid_questions")
        elif kind == "noul":
            if criteria is not None and (
                not isinstance(criteria, dict) or set(criteria) - {"true", "false"}
            ):
                raise JevError("jev_invalid_questions")
        else:
            raise JevError("jev_invalid_questions")
    _encoded(questions)
    return questions


@dataclass(frozen=True)
class JevDecision:
    model: str
    answers: Mapping[str, Any] = field(repr=False)
    input_tokens: int = 0
    output_tokens: int = 0


def parse_decision(data: Any, questions: dict[str, Any]) -> JevDecision:
    """Validate every answer against the caller-owned closed answer space."""
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise JevError("jev_invalid_response")
    if set(data["answers"]) != set(questions):
        raise JevError("jev_invalid_response")
    model = data.get("model")
    if not isinstance(model, str) or not re.fullmatch(r"jev-[\w.\-]{1,72}", model):
        raise JevError("jev_invalid_response")
    for key, question in questions.items():
        answer = data["answers"][key]
        kind = question["type"]
        if not isinstance(answer, dict) or answer.get("type") != kind:
            raise JevError("jev_invalid_response")
        if kind == "noul":
            _probability(answer.get("noul"))
            continue
        _probability(answer.get("confidence"))
        options = (
            set(question["criteria"])
            if kind == "choice"
            else {str(i) for i in range(len(question["criteria"]))}
        )
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != options:
            raise JevError("jev_invalid_response")
        values = [_probability(v) for v in probabilities.values()]
        if abs(sum(values) - 1.0) > 0.05:
            raise JevError("jev_invalid_response")
        if kind == "choice":
            choice = answer.get("choice")
            if not isinstance(choice, str) or choice not in options:
                raise JevError("jev_invalid_response")
            if probabilities[choice] + 0.011 < max(values):
                raise JevError("jev_invalid_response")
        else:
            score = answer.get("score")
            if (
                type(score) not in {int, float}
                or not 0 <= score <= len(options) - 1
                or not math.isfinite(score)
            ):
                raise JevError("jev_invalid_response")
            if (
                not isinstance(answer.get("legend"), dict)
                or set(answer["legend"]) != options
            ):
                raise JevError("jev_invalid_response")
    usage = data.get("usage", {})
    if not isinstance(usage, dict):
        raise JevError("jev_invalid_response")
    for name in ("input_tokens", "output_tokens"):
        count = usage.get(name, 0)
        if type(count) is not int or not 0 <= count <= 10_000_000:
            raise JevError("jev_invalid_response")
    return JevDecision(
        model,
        data["answers"],
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
    )


class JevDecisionService:
    """Reusable Choice/Score/Noul adapter, with bounded and reviewed retries."""

    def __init__(
        self,
        gateway: OutboundPrivacyGateway,
        *,
        model: str = JEV_DEFAULT_MODEL,
        timeout_seconds: float = 12.0,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(model, str) or not re.fullmatch(r"jev-[\w.\-]{1,72}", model):
            raise JevError("jev_invalid_model")
        if type(max_retries) is not int or not 0 <= max_retries <= 2:
            raise JevError("jev_invalid_retry_budget")
        if (
            type(timeout_seconds) not in {int, float}
            or not 0 < timeout_seconds <= 30
            or not math.isfinite(timeout_seconds)
        ):
            raise JevError("jev_invalid_timeout")
        self.gateway = gateway
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._transport = transport
        self.attempts = 0
        self.input_tokens = 0
        self.output_tokens = 0

    async def evaluate(self, state: Any, questions: dict[str, Any]) -> JevDecision:
        key = str(os.environ.get("JEV_API_KEY", "")).strip()
        if not key:
            raise JevError("jev_credential_missing")
        if not key.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in key):
            raise JevError("jev_credential_invalid")
        # Freeze a separate authority copy; review callbacks never receive it.
        questions = json.loads(_encoded(validate_questions(questions)))
        if not isinstance(state, (str, dict, list)):
            raise JevError("jev_invalid_request")
        payload = json.loads(
            _encoded({"model": self.model, "state": state, "questions": questions})
        )
        self._validate_size(payload)
        descriptor = EgressDescriptor(
            action="jev_decision",
            transport="https.post",
            destination=JEV_ENDPOINT,
            provider="jev",
            tool="browser_agent",
            model=self.model,
        )

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                self.timeout_seconds, connect=min(5.0, self.timeout_seconds)
            ),
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        ) as client:

            async def send(final: Any) -> dict[str, Any]:
                # The gateway's final, reviewed payload is the sole wire value.
                # Redaction or human editing may change text, never the model,
                # question types, question IDs, or candidate IDs.
                if (
                    not isinstance(final, dict)
                    or set(final) != {"model", "state", "questions"}
                    or final["model"] != self.model
                ):
                    raise JevError("jev_egress_binding_changed")
                final_questions = validate_questions(final["questions"])
                if set(final_questions) != set(questions):
                    raise JevError("jev_egress_binding_changed")
                for qid, original in questions.items():
                    edited = final_questions[qid]
                    if edited["type"] != original["type"]:
                        raise JevError("jev_egress_binding_changed")
                    if original["type"] == "choice" and set(edited["criteria"]) != set(
                        original["criteria"]
                    ):
                        raise JevError("jev_egress_binding_changed")
                    if original["type"] == "score" and len(edited["criteria"]) != len(
                        original["criteria"]
                    ):
                        raise JevError("jev_egress_binding_changed")
                self._validate_size(final)
                if key in _encoded(final).decode("utf-8"):
                    raise JevError("jev_credential_in_payload")
                self.attempts += 1
                try:
                    async with client.stream(
                        "POST",
                        JEV_ENDPOINT,
                        headers={"Authorization": f"Bearer {key}"},
                        json=final,
                    ) as response:
                        if response.status_code != 200:
                            retry_after = 0.0
                            try:
                                parsed = float(response.headers.get("retry-after", "0"))
                                if math.isfinite(parsed):
                                    retry_after = max(0.0, min(4.0, parsed))
                            except ValueError:
                                pass
                            code = {
                                401: "jev_credential_invalid",
                                403: "jev_access_denied",
                                422: "jev_request_rejected",
                                429: "jev_rate_limited",
                                529: "jev_overloaded",
                            }.get(response.status_code, "jev_http_failed")
                            raise JevError(code, retry_after=retry_after)
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > MAX_RESPONSE_BYTES:
                                raise JevError("jev_response_too_large")
                        try:
                            return json.loads(content)
                        except (ValueError, UnicodeError):
                            raise JevError("jev_invalid_response") from None
                except httpx.TimeoutException:
                    raise JevError("jev_timeout") from None
                except httpx.HTTPError:
                    raise JevError("jev_transport_failed") from None

            async def bounded_send(final: Any) -> dict[str, Any]:
                # Bound network time after privacy review, never the human
                # review itself: waiting for approval is not a provider outage.
                try:
                    return await asyncio.wait_for(send(final), timeout=self.timeout_seconds)
                except asyncio.TimeoutError:
                    raise JevError("jev_timeout") from None

            for attempt in range(self.max_retries + 1):
                try:
                    raw = await self.gateway.execute(
                        payload,
                        provider="jev",
                        descriptor=descriptor,
                        sender=bounded_send,
                        base_url="https://api.typesafe.ai",
                        source_kind="browser_decision",
                        model=self.model,
                    )
                    decision = parse_decision(raw, questions)
                    self.input_tokens += decision.input_tokens
                    self.output_tokens += decision.output_tokens
                    return decision
                except JevError as exc:
                    if (
                        exc.code not in {"jev_rate_limited", "jev_overloaded"}
                        or attempt == self.max_retries
                    ):
                        raise
                    await asyncio.sleep(
                        max(exc.retry_after, min(4.0, 0.5 * 2**attempt))
                    )
        raise JevError("jev_retry_exhausted")

    @staticmethod
    def _validate_size(payload: dict[str, Any]) -> None:
        if len(_encoded(payload)) > MAX_REQUEST_BYTES:
            raise JevError("jev_request_too_large")
        state_bytes = len(_encoded(payload["state"]))
        if any(
            state_bytes + len(_encoded(q)) > MAX_STATE_QUESTION_BYTES
            for q in payload["questions"].values()
        ):
            raise JevError("jev_state_too_large")
