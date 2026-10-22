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
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from time import perf_counter
from typing import Any, AsyncIterator, Mapping

import httpx

from .outbound_privacy_service import EgressDescriptor, OutboundPrivacyGateway

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_DEFAULT_MODEL = "jev-1.13.0"
MAX_REQUEST_BYTES = 48_000
MAX_STATE_QUESTION_BYTES = 24_000
MAX_RESPONSE_BYTES = 131_072

_PURPOSE_DESCRIPTORS = {
    "verification": {
        "action": "jev_completion_verification",
        "tool": "agentic_completion",
        "source_kind": "completion_verification",
    },
    "browser": {
        "action": "jev_decision",
        "tool": "browser_agent",
        "source_kind": "browser_decision",
    },
    "search": {
        "action": "jev_search_rerank",
        "tool": "web_search",
        "source_kind": "search_rerank",
    },
    "tool_pack": {
        "action": "jev_tool_pack_route",
        "tool": "load_tool_pack",
        "source_kind": "tool_pack_decision",
    },
    "memory": {
        "action": "jev_memory_relevance",
        "tool": "scoped_memory",
        "source_kind": "memory_relevance",
    },
    "routing": {
        "action": "jev_model_route",
        "tool": "free_team",
        "source_kind": "free_team_routing",
    },
    "chat_intent": {
        "action": "jev_chat_intent_candidates",
        "tool": "chat_intent_candidates",
        "source_kind": "chat_intent_candidates",
    },
    "decision": {
        "action": "jev_decision",
        "tool": "bounded_decision",
        "source_kind": "bounded_decision",
    },
}


class JevError(RuntimeError):
    """A stable error code, never a provider's raw error or a secret."""

    def __init__(
        self, code: str, *, retry_after: float = 0.0, timeout_kind: str | None = None
    ) -> None:
        self.code = code
        self.retry_after = retry_after
        self.transport_phase: str | None = None
        self.executor_probe_state: str | None = None
        self.transport_timings_ms: dict[str, float] = {}
        self.timeout_kind = (
            timeout_kind
            if isinstance(timeout_kind, str)
            and timeout_kind
            in {"connect", "read", "write", "pool", "overall", "unknown"}
            else None
        )
        super().__init__(code)


class _TransportDiagnostics:
    """One request's fixed phases and timer lag; never retain trace arguments."""

    def __init__(self) -> None:
        self.phase = "unobserved"
        self.timings_ms: dict[str, float] = {"loop_lag": 0.0}
        self._phase_starts: dict[str, float] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._handle: asyncio.TimerHandle | None = None
        self._expected = 0.0
        self.executor_probe_state = "unavailable"
        self._executor_probe: asyncio.Future | None = None
        self._executor_submitted = 0.0

    async def trace(self, event_name: str, _info: Any) -> None:
        # _info can contain credentials, addresses, streams, or exceptions.
        # Do not read, copy, stringify, store, or log it.
        for phase in ("connect_tcp", "start_tls"):
            if event_name == f"connection.{phase}.started":
                self.phase = phase
                self._phase_starts[phase] = perf_counter()
                return
            if event_name in {f"connection.{phase}.complete", f"connection.{phase}.failed"}:
                self._finish_phase(phase)
                if phase == "start_tls" and event_name.endswith(".complete"):
                    self.phase = "connected"
                return
        if event_name in {"http11.send_request_headers.started", "http2.send_request_headers.started"}:
            self.phase = "connected"

    def _finish_phase(self, phase: str) -> None:
        started = self._phase_starts.pop(phase, None)
        if started is not None:
            self.timings_ms[phase] = self.timings_ms.get(phase, 0.0) + max(
                0.0, (perf_counter() - started) * 1000
            )

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._executor_submitted = perf_counter()
        try:
            # No draft, credentials or copied contextvars enter this worker.
            # Never await the probe or delay the actual provider call.
            self._executor_probe = self._loop.run_in_executor(None, perf_counter)
            self.executor_probe_state = "pending"
        except Exception:
            self._executor_probe = None
            self.executor_probe_state = "unavailable"
        self._expected = self._loop.time() + 0.1
        self._handle = self._loop.call_at(self._expected, self._tick)

    def _tick(self) -> None:
        if self._loop is None:
            return
        now = self._loop.time()
        self.timings_ms["loop_lag"] = max(
            self.timings_ms["loop_lag"], max(0.0, now - self._expected) * 1000
        )
        self._expected = now + 0.1
        self._handle = self._loop.call_at(self._expected, self._tick)

    def stop(self) -> None:
        probe, self._executor_probe = self._executor_probe, None
        if probe is not None:
            try:
                if probe.done() and not probe.cancelled():
                    worker_started = probe.result()
                    if type(worker_started) in {int, float} and math.isfinite(worker_started):
                        self.timings_ms["executor_queue"] = max(
                            0.0, (worker_started - self._executor_submitted) * 1000
                        )
                        self.executor_probe_state = "completed"
                    else:
                        self.executor_probe_state = "unavailable"
                else:
                    self.executor_probe_state = "pending"
            except Exception:
                self.executor_probe_state = "unavailable"
            finally:
                # Pending jobs are cancelled; an already-running clock read
                # returns immediately and cannot perform external work.
                if not probe.done():
                    probe.cancel()
        if self._loop is not None:
            self.timings_ms["loop_lag"] = max(
                self.timings_ms["loop_lag"],
                max(0.0, self._loop.time() - self._expected) * 1000,
            )
        if self._handle is not None:
            self._handle.cancel()
        self._handle = None
        self._loop = None
        for phase in tuple(self._phase_starts):
            self._finish_phase(phase)


class _RejectCookies(DefaultCookiePolicy):
    """A shared API transport must never retain another request's cookies."""

    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


def _http_timeout(seconds: float) -> httpx.Timeout:
    if (
        type(seconds) not in {int, float}
        or not 0 < seconds <= 30
        or not math.isfinite(seconds)
    ):
        raise JevError("jev_invalid_timeout")
    return httpx.Timeout(seconds, connect=min(5.0, seconds))


def _new_http_client(
    timeout: httpx.Timeout,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    shared: bool = False,
) -> httpx.AsyncClient:
    options: dict[str, Any] = {}
    if shared:
        options["cookies"] = CookieJar(policy=_RejectCookies())
        options["limits"] = httpx.Limits(
            max_connections=16, max_keepalive_connections=16, keepalive_expiry=60.0
        )
    # One factory for standalone, run-scoped, and app-owned clients. Auth is
    # deliberately absent: evaluate binds the current key on every request.
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
        **options,
    )


class JevClientPool:
    """App-owned transport only; gateways, credentials and state stay per call."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 12.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = _http_timeout(timeout_seconds)
        self._transport = transport
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise JevError("jev_session_loop_mismatch")
        self._loop = loop

    def get_client(self) -> httpx.AsyncClient:
        if self._loop is None:
            raise JevError("jev_client_pool_unavailable")
        if self._loop is not asyncio.get_running_loop():
            raise JevError("jev_session_loop_mismatch")
        if self._client is None:
            # No await between the check and assignment: concurrent borrowers
            # on the owning loop create exactly one client without a lock.
            self._client = _new_http_client(self._timeout, self._transport, shared=True)
        if self._client.is_closed:
            raise JevError("jev_client_pool_unavailable")
        return self._client

    async def aclose(self) -> None:
        if self._loop is None:
            return
        if self._loop is not asyncio.get_running_loop():
            raise JevError("jev_session_loop_mismatch")
        client, self._client = self._client, None
        self._loop = None
        if client is not None:
            await client.aclose()


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
        capture_timing: bool = False,
        capture_transport: bool = False,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        purpose: str = "browser",
        client_pool: JevClientPool | None = None,
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
        if purpose not in _PURPOSE_DESCRIPTORS:
            raise JevError("jev_invalid_purpose")
        self.purpose = purpose
        self.gateway = gateway
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._transport = transport
        self._capture_transport = capture_transport
        self._transport_diagnostics: _TransportDiagnostics | None = None
        if client_pool is not None and transport is not None:
            raise JevError("jev_invalid_client_configuration")
        self._client_pool = client_pool
        self._session_client: httpx.AsyncClient | None = None
        self._session_loop: asyncio.AbstractEventLoop | None = None
        self.attempts = 0
        self.input_tokens = 0
        self.output_tokens = 0
        # Opt-in, request-local numeric diagnostics. Existing callers do not
        # sample the clock or change their wire contract/pooling behavior.
        self.timings_ms: dict[str, float] = (
            {"gateway": 0.0, "provider": 0.0} if capture_timing else {}
        )

    def _new_client(self) -> httpx.AsyncClient:
        return _new_http_client(self._request_timeout(), self._transport)

    def _request_timeout(self) -> httpx.Timeout:
        return _http_timeout(self.timeout_seconds)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[JevDecisionService]:
        """Pool connections within one awaited run on the current event loop.

        The client is allocated on the first evaluate call, so unused services
        do not need a pool. The owned pool closes when the run succeeds, fails,
        or is cancelled. Finish all calls before leaving this scope. Overlapping
        sessions are not supported.
        """
        if self._session_loop is not None:
            raise JevError("jev_session_active")
        self._session_loop = asyncio.get_running_loop()
        try:
            yield self
        finally:
            client, self._session_client = self._session_client, None
            self._session_loop = None
            if client is not None:
                await client.aclose()

    @asynccontextmanager
    async def _evaluation_client(self) -> AsyncIterator[httpx.AsyncClient]:
        if (
            self._session_loop is not None
            and self._session_loop is not asyncio.get_running_loop()
        ):
            raise JevError("jev_session_loop_mismatch")
        if self._client_pool is not None:
            # A request/session borrows the app client but never owns its
            # lifetime. Cancelling this evaluation closes its stream only.
            yield self._client_pool.get_client()
            return
        if self._session_loop is not None:
            if self._session_client is None:
                self._session_client = self._new_client()
            yield self._session_client
        else:
            # Preserve evaluate-only callers: no persistent pool to dispose of.
            async with self._new_client() as client:
                yield client

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
        purpose = _PURPOSE_DESCRIPTORS[self.purpose]
        descriptor = EgressDescriptor(
            action=purpose["action"],
            transport="https.post",
            destination=JEV_ENDPOINT,
            provider="jev",
            tool=purpose["tool"],
            model=self.model,
        )
        diagnostics = _TransportDiagnostics() if self._capture_transport else None
        self._transport_diagnostics = diagnostics

        async with self._evaluation_client() as client:

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
                        auth=None,
                        follow_redirects=False,
                        timeout=self._request_timeout(),
                        extensions={"trace": diagnostics.trace} if diagnostics is not None else None,
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
                except httpx.TimeoutException as exc:
                    timeout_kind = next(
                        (kind for error_type, kind in (
                            (httpx.ConnectTimeout, "connect"),
                            (httpx.ReadTimeout, "read"),
                            (httpx.WriteTimeout, "write"),
                            (httpx.PoolTimeout, "pool"),
                        ) if isinstance(exc, error_type)),
                        "unknown",
                    )
                    raise JevError("jev_timeout", timeout_kind=timeout_kind) from None
                except httpx.HTTPError:
                    raise JevError("jev_transport_failed") from None

            evaluation_provider_ms = 0.0

            async def bounded_send(final: Any) -> dict[str, Any]:
                nonlocal evaluation_provider_ms
                # Bound network time after privacy review, never the human
                # review itself: waiting for approval is not a provider outage.
                started = perf_counter() if self.timings_ms else None
                if diagnostics is not None:
                    diagnostics.start()
                try:
                    return await asyncio.wait_for(send(final), timeout=self.timeout_seconds)
                except asyncio.TimeoutError:
                    raise JevError("jev_timeout", timeout_kind="overall") from None
                finally:
                    if diagnostics is not None:
                        diagnostics.stop()
                    if started is not None:
                        elapsed_ms = max(
                            0.0, (perf_counter() - started) * 1000
                        )
                        evaluation_provider_ms += elapsed_ms
                        self.timings_ms["provider"] += elapsed_ms

            for attempt in range(self.max_retries + 1):
                try:
                    started = perf_counter() if self.timings_ms else None
                    previous_provider_ms = evaluation_provider_ms
                    try:
                        raw = await self.gateway.execute(
                            payload,
                            provider="jev",
                            descriptor=descriptor,
                            sender=bounded_send,
                            base_url="https://api.typesafe.ai",
                            source_kind=purpose["source_kind"],
                            model=self.model,
                        )
                    finally:
                        if started is not None:
                            # Gateway includes protection/review and invokes
                            # bounded_send. Subtract only this attempt's send
                            # time, leaving retries/backoff outside both metrics.
                            self.timings_ms["gateway"] += max(
                                0.0,
                                (perf_counter() - started) * 1000
                                - (evaluation_provider_ms - previous_provider_ms),
                            )
                    decision = parse_decision(raw, questions)
                    self.input_tokens += decision.input_tokens
                    self.output_tokens += decision.output_tokens
                    return decision
                except JevError as exc:
                    if diagnostics is not None and exc.code == "jev_timeout":
                        exc.transport_phase = diagnostics.phase
                        exc.executor_probe_state = diagnostics.executor_probe_state
                        exc.transport_timings_ms = dict(diagnostics.timings_ms)
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
