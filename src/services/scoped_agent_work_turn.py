"""One bounded turn for an already admitted, pinned Agent execution.

The caller owns Work/Run admission and workspace lease validation.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import hashlib
import json
import math
from dataclasses import asdict, dataclass, is_dataclass, replace
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from ..llm.agent_run_budget import AgentRunToolFailureCircuit, peek_agent_run_budget
from ..llm.agentic_completion import generation_failure_kind
from ..llm.generation_error import GenerationErrorKind, GenerationFailure
from ..llm.multimodal import normalize_image_payloads
from ..llm.generation_budget import GenerationBudget, current_generation_budget
from ..llm.generation_cancellation import GenerationCancelled
from ..llm.frozen_candidate_transport import frozen_candidate_transport
from .agent_route_fallback_policy import RoutePreflightFailure, should_fallback_after_preflight_failure
from .model_roles_service import route_billing_class
from ..llm.provider_terminal import sanitize_terminal_evidence
from .agent_delegation_media_inputs import MediaInputPin, ResolvedMediaInput
from .agent_team_video_review_inputs import DerivedVideoFrame
from ..llm.image_budget import _validated_image
from .llm_model_catalog import model_supports_vision
from .media_asset_service import IMAGE_MAX_BYTES, MIME_EXTENSIONS
from .agent_run_service import reset_current_agent_run_id, set_current_agent_run_id
from .chat_execution_adapter import (
    ChatAgentExecution,
    bind_chat_agent_execution,
    build_agent_chat_turn_client,
)
from .pricing.providers import is_cli_provider
from .session_llm_generation import cleanup_ephemeral_llm_client
from .turn_context import reset_turn_context, set_turn_context
from ..llm.tool_policy import FILESYSTEM_TOOL_NAMES
from .runtime_policy_utils import _SAFE_FAILURE_CODES

_SAFE_OBSERVED_TOOL_NAMES = frozenset(FILESYSTEM_TOOL_NAMES)


def safe_tool_failure_observation(value: Any) -> dict[str, Any]:
    """Whitelist only bounded, content-free circuit facts."""
    if not isinstance(value, dict):
        return {}
    name = value.get("tool_name")
    count = value.get("failure_streak")
    if type(name) is not str or name not in _SAFE_OBSERVED_TOOL_NAMES:
        return {}
    if type(count) is not int or not 1 <= count <= 1000:
        return {}
    result = {"tool_name": name, "failure_streak": count}
    code = value.get("error_code")
    if type(code) is str and code in _SAFE_FAILURE_CODES:
        result["error_code"] = code
    return result


class ScopedAgentMediaUnsupported(NotImplementedError):
    """The selected route cannot deliver validated images."""


_SAFE_STOP_CODES = frozenset(
    value for name, value in vars(GenerationErrorKind).items()
    if name.isupper() and isinstance(value, str)
) | frozenset({
    "run_budget_exceeded:tokens", "run_budget_exceeded:cost",
    "run_budget_exceeded:tool_calls", "run_budget_usage_unavailable:tokens",
    "run_budget_stopped", "generation_stopped", "provider_incomplete",
    "provider_interrupted", "provider_protocol_error", "provider_unknown",
    "provider_continuation", "no eligible fallback candidate",
    "empty or unsupported generation response",
})


def safe_scoped_stop_code(value: Any) -> str:
    return value if isinstance(value, str) and value in _SAFE_STOP_CODES else "unknown"


_BLOCKED_SOURCES = frozenset({
    "runtime_capability", "agent_authority", "tool_policy", "required_delegate_plan_receipt",
})


def safe_blocked_source(value: Any) -> str | None:
    return value if type(value) is str and value in _BLOCKED_SOURCES else None


def _generation_blocked_source(failure: Any) -> str | None:
    try:
        if type(failure) is not GenerationFailure or failure.kind != GenerationErrorKind.TOOL_EXECUTION_BLOCKED:
            return None
        detail = failure.technical_detail
        if type(detail) is not str:
            return None
        return next((source for source in _BLOCKED_SOURCES
                     if detail == f"tool_execution_blocked source={source}"), None)
    except Exception:
        return None


class ScopedAgentWorkTurnStopped(RuntimeError):
    """The provider or framework stopped without a successful final answer."""

    def __init__(self, reason: str, *, blocked_source: str | None = None):
        super().__init__(reason)
        self.code = safe_scoped_stop_code(reason)
        self.blocked_source = safe_blocked_source(blocked_source)


def _failure_observation(client: Any, run_id: str, budget: GenerationBudget, caps: dict) -> dict[str, Any]:
    """Snapshot run-keyed observations before client cleanup; never infer usage."""
    observed: dict[str, Any] = {"status": "partial"}

    def number(value: Any) -> bool:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)

    try:
        count = budget.request_count
        if number(count):
            observed["request_count"] = count
    except Exception:
        pass
    try:
        cap = caps.get("provider_max_output_tokens")
        if number(cap):
            observed["output_cap"] = cap
    except Exception:
        pass
    ledger = None
    try:
        ledger = peek_agent_run_budget(run_id)
        state = ledger.usage_state() if ledger is not None else {}
        if isinstance(state, dict):
            counters = {key: state[key] for key in ("request_count", "tool_call_count", "measured_tokens", "measured_cost_micros")
                        if key in state and number(state[key])}
            if counters:
                observed["budget_counters"] = counters
    except Exception:
        pass
    state = {}
    try:
        getter = getattr(client, "peek_completed_agent_run_state", None)
        state = getter(run_id) if callable(getter) else {}
        usage = state.get("usage") if isinstance(state, dict) else None
        if isinstance(usage, dict):
            # Completed state can contain a derived total; retain only reported totals.
            keys = ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens", "cost_micros")
            safe = {key: usage[key] for key in keys if key in usage and number(usage[key])}
            if usage.get("total_tokens_source") == "reported" and number(usage.get("total_tokens")):
                safe["total_tokens"] = usage["total_tokens"]
            if safe:
                observed["usage"] = safe
    except Exception:
        pass
    try:
        failure = getattr(ledger, "failure", None)
        if type(failure) is AgentRunToolFailureCircuit:
            circuit = {"tool_name": failure.tool_name, "failure_streak": failure.failure_streak}
            from ..llm.native_runtime import ToolExecutionRecord
            records = state.get("tool_calls") if isinstance(state, dict) else None
            if isinstance(records, (list, tuple)):
                for record in reversed(records[-100:]):
                    if type(record) is ToolExecutionRecord and record.tool == failure.tool_name:
                        if (record.executed is True and record.synthetic is False
                                and type(record.result) is str and len(record.result) <= 65536):
                            try:
                                payload = json.loads(record.result)
                            except (ValueError, TypeError):
                                payload = None
                            if isinstance(payload, dict) and payload.get("success") is False:
                                circuit["error_code"] = payload.get("error_code")
                        break
            safe_circuit = safe_tool_failure_observation(circuit)
            if safe_circuit:
                observed["tool_failure"] = safe_circuit
    except Exception:
        pass
    return observed


@dataclass(frozen=True)
class ScopedAgentWorkTurnResult:
    text: str
    evidence: dict[str, Any]
    request_count: int
    status: str


def _positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _cap(budget: dict[str, Any], key: str, requested: int) -> None:
    existing = budget.get(key)
    if existing not in (None, ""):
        try:
            parsed = int(existing)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f"invalid existing {key}") from exc
        if parsed <= 0:
            raise ValueError(f"invalid existing {key}")
        requested = min(requested, parsed)
    budget[key] = requested


async def execute_frozen_candidates(route: Any, config: Any, attempt: Any, *, start_index: int = 0) -> tuple[Any, list[dict[str, Any]], Any]:
    """Execute each admitted candidate once, preserving the frozen route policy."""
    candidates = route.candidate_routes
    if candidates is None:
        return await attempt(0, route), [], route
    attempts: list[dict[str, Any]] = []
    last_failure = None
    for index, candidate in enumerate(candidates):
        if index < start_index:
            continue
        if index and not route.allow_paid_fallback:
            try:
                billing = route_billing_class(config, candidate)
            except Exception:
                billing = "paid"
            if billing == "paid":
                continue
        if index == 0:
            selected = replace(route, selected_candidate_index=0)
            provider, model = route.provider, route.model
        else:
            kind = candidate.get("kind")
            provider = "routing-profile" if kind == "pool" else str(candidate.get("provider") or "").strip()
            model = "free-team" if kind == "pool" else str(candidate.get("model") or "").strip()
            if kind not in {"llm", "pool"} or not provider or (not model and provider != "muse-remote"):
                raise ValueError("invalid frozen candidate")
            backend = ("pool" if kind == "pool" else "cli" if is_cli_provider(provider)
                       else "remote" if provider == "muse-remote"
                       else "local" if provider in {"ollama", "llama-cpp", "sglang"} else "api")
            selected = replace(
                route, provider=provider, model=model or None,
                effort=candidate.get("effort") or "", pool_id=candidate.get("pool_id") if kind == "pool" else None,
                backend=backend,
                selected_candidate_index=index,
            )
        receipt = {"candidate_index": index, "provider": provider, "model": model,
                   "effort": candidate.get("effort") or None}
        try:
            with frozen_candidate_transport():
                result = await attempt(index, selected)
        except RoutePreflightFailure as exc:
            receipt["failure_code"] = exc.code
            attempts.append(receipt)
            if not should_fallback_after_preflight_failure(route.fallback_on, exc):
                exc.candidate_attempts = list(attempts)
                raise
            last_failure = exc
            continue
        except Exception as exc:
            receipt["failure_code"] = "unknown"
            attempts.append(receipt)
            exc.candidate_attempts = list(attempts)
            raise
        receipt["failure_code"] = None
        attempts.append(receipt)
        return result, attempts, selected
    if last_failure is not None:
        last_failure.candidate_attempts = list(attempts)
        raise last_failure
    raise ScopedAgentWorkTurnStopped("no eligible fallback candidate")


async def build_chat_client_with_frozen_preflight(builder: Any, source: Any, config: Any, execution: Any, **kwargs: Any) -> Any:
    """Select a candidate when the exact primary factory reports a trusted preflight failure."""
    if execution.route.candidate_routes is None:
        return await asyncio.to_thread(builder, source, config, execution, **kwargs)

    async def attempt(index: int, selected: Any) -> Any:
        if index and selected.backend in {"cli", "remote", "pool"}:
            raise NotImplementedError("secondary native, remote or pool workspace admission is not supported")
        return await asyncio.to_thread(builder, source, config, replace(execution, route=selected), **kwargs)

    client, receipts, selected = await execute_frozen_candidates(execution.route, config, attempt)
    client._frozen_candidate_execution = execution
    client._frozen_candidate_start_index = selected.selected_candidate_index
    client._frozen_candidate_initial_receipts = receipts[:-1]
    return client


async def invoke_scoped_agent_work_turn(
    *,
    execution: ChatAgentExecution,
    source_client: Any,
    config: Any,
    owner_user_id: str,
    session_id: str,
    project_id: str | None,
    run_id: str,
    instruction: str,
    max_output_tokens: int,
    max_requests: int | None,
    max_tool_rounds: int | None,
    timeout_seconds: float = 120,
    validated_media_inputs: Sequence[ResolvedMediaInput | DerivedVideoFrame] = (),
    client_builder: Any = None,
) -> ScopedAgentWorkTurnResult:
    """Execute a finite API/local turn using the supplied immutable route."""
    for name, value in (
        ("owner_user_id", owner_user_id),
        ("session_id", session_id),
        ("run_id", run_id),
        ("instruction", instruction),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} is required")
    _positive_int(max_output_tokens, "max_output_tokens")
    if max_requests is not None:
        _positive_int(max_requests, "max_requests")
    if max_tool_rounds is not None:
        _positive_int(max_tool_rounds, "max_tool_rounds")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be finite and positive")
    binding = execution.binding
    if str(binding.session_id) != session_id:
        raise ValueError("execution session does not match")
    binding_owner = getattr(binding, "owner_user_id", None)
    if binding_owner is not None and str(binding_owner) != owner_user_id:
        raise ValueError("execution owner does not match")
    route = execution.route
    if str(route.backend).casefold() not in {"api", "local"} or is_cli_provider(
        route.provider
    ):
        raise NotImplementedError("only API/local routes are supported")
    if (
        not route.provider
        or str(route.provider).casefold() == "routing-profile"
        or not route.model
    ):
        raise ValueError("an exact primary route is required")
    image_data = None
    image_hashes = ()
    sampled_video = None
    if validated_media_inputs:
        inputs = tuple(validated_media_inputs)
        if any(type(item) is DerivedVideoFrame for item in inputs):
            if len(inputs) > 3 or any(type(item) is not DerivedVideoFrame for item in inputs):
                raise ValueError("invalid sampled video inputs")
            pin = inputs[0].source_pin
            if type(pin) is not MediaInputPin:
                raise ValueError("invalid video pin")
            identifiers = (pin.asset_revision_id, pin.output_id, pin.work_id, pin.agent_run_id,
                           pin.plan_id, pin.generation_run_id)
            digests = (pin.bytes_sha256, pin.revision_hash, pin.plan_hash,
                       pin.generation_run_hash, pin.output_provenance_hash)
            if (any(not isinstance(value, UUID) for value in identifiers)
                    or any(type(value) is not str or len(value) != 64
                           or any(c not in "0123456789abcdef" for c in value) for value in digests)):
                raise ValueError("invalid video pin")
            previous = -1.0
            for frame in inputs:
                timestamp = frame.timestamp_seconds
                if (frame.source_pin != pin or frame.source_video_sha256 != pin.bytes_sha256
                        or type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                        or timestamp < 0 or timestamp <= previous or frame.mime_type != "image/jpeg"
                        or not isinstance(frame.data, bytes) or not 0 < len(frame.data) <= IMAGE_MAX_BYTES
                        or hashlib.sha256(frame.data).hexdigest() != frame.frame_sha256):
                    raise ValueError("invalid video frame")
                _validated_image(frame.data, frame.mime_type)
                previous = timestamp
            provenance = [frame.provenance() for frame in inputs]
            sampled_video = {"source_pin": provenance[0]["source_pin"],
                             "source_video_sha256": pin.bytes_sha256,
                             "frames": provenance, "scope": "sampled_visual_only"}
        images = []
        hashes = []
        for item in inputs:
            if (not isinstance(item, ResolvedMediaInput) and type(item) is not DerivedVideoFrame
                    or not isinstance(item.data, bytes)
                    or not 0 < len(item.data) <= IMAGE_MAX_BYTES
                    or item.mime_type not in MIME_EXTENSIONS or not item.mime_type.startswith("image/")):
                raise ValueError("invalid validated media input")
            hashes.append(hashlib.sha256(item.data).hexdigest())
            encoded = base64.b64encode(item.data).decode("ascii")
            images.append({"data": f"data:{item.mime_type};base64,{encoded}",
                           "mimeType": item.mime_type, "name": str((item.source_pin if type(item) is DerivedVideoFrame else item.pin).asset_revision_id)})
        image_data = {"images": normalize_image_payloads({"images": images})}
        if len(image_data["images"]) != len(inputs):
            raise ScopedAgentMediaUnsupported("media input conversion is unsupported")
        image_hashes = tuple(hashes)
    for field, expected in (
        ("agent_id", execution.agent_id),
        ("agent_revision_id", execution.agent_revision_id),
    ):
        pinned = getattr(route, field, None)
        if pinned is not None and str(pinned) != str(expected):
            raise ValueError(f"route {field} does not match binding")
    caps = dict(execution.dialogue_budget or {})
    _cap(caps, "provider_max_output_tokens", max_output_tokens)
    if max_tool_rounds is not None:
        _cap(caps, "provider_max_tool_rounds", max_tool_rounds)
    round_cap = caps.get("provider_max_tool_rounds")
    if round_cap not in (None, ""):
        try:
            round_cap = _positive_int(int(round_cap), "provider_max_tool_rounds")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid existing provider_max_tool_rounds") from exc
    else:
        round_cap = None
    execution = replace(execution, dialogue_budget=caps)
    budget = GenerationBudget(
        float(timeout_seconds),
        max_requests=max_requests,
        track_request_count=True,
        interruptible_transport=True,
        parent=current_generation_budget.get(),
    )
    context_token = set_turn_context(
        user_id=owner_user_id,
        session_id=session_id,
        project_id=project_id,
        agent_id=execution.agent_id,
        agent_revision_id=execution.agent_revision_id,
    )
    run_token = set_current_agent_run_id(run_id)
    budget_token = current_generation_budget.set(budget)
    async def attempt(execution: ChatAgentExecution) -> ScopedAgentWorkTurnResult:
        client = None
        try:
            async with bind_chat_agent_execution(
                execution,
                user_id=owner_user_id,
                session_id=session_id,
                project_id=project_id,
                config=config,
            ):
                builder = client_builder or build_agent_chat_turn_client
                client = builder(
                    source_client,
                    config,
                    execution,
                    session_id=session_id,
                    project_id=project_id,
                    run_id=run_id,
                    tool_generation_max_tokens=caps["provider_max_output_tokens"],
                )
                if inspect.isawaitable(client):
                    pending_client, client = client, None
                    try:
                        builder_deadline = asyncio.timeout(budget.checkpoint())
                    except BaseException:
                        if inspect.iscoroutine(pending_client):
                            pending_client.close()
                        raise
                    try:
                        async with builder_deadline:
                            client = await pending_client
                    except TimeoutError as exc:
                        if builder_deadline.expired() and isinstance(exc.__cause__, asyncio.CancelledError):
                            exc.interruption_reason = "outer_deadline"
                        raise
                # The factory copies these fields from the long-lived source client.
                # This request's admitted scope must take precedence over stale state.
                client.current_session_id = session_id
                client.session_user_id = owner_user_id
                client.current_project_id = project_id
                # Native's work profile takes the maximum of these three settings;
                # narrow all three on the request-local target config as well.
                target_config = getattr(client, "config", None)
                setter = getattr(target_config, "set", None)
                getter = getattr(target_config, "get", None)
                if round_cap is not None and callable(setter) and callable(getter):
                    for key in (
                        "agentic_completion.max_tool_rounds",
                        "agentic_completion.work_max_rounds",
                        "agentic_completion.autonomous_work_max_rounds",
                        "agentic_completion.assisted_work_max_rounds",
                    ):
                        old = getter(key, None)
                        cap = round_cap
                        if old not in (None, ""):
                            cap = min(cap, _positive_int(int(old), key))
                        setter(key, cap)
                runner = getattr(client, "_turn_runner", None)
                if (
                    round_cap is not None
                    and runner is not None
                    and getattr(runner, "max_tool_rounds", None) is not None
                ):
                    runner.max_tool_rounds = min(
                        _positive_int(runner.max_tool_rounds, "runner.max_tool_rounds"),
                        round_cap,
                    )
                remaining = budget.checkpoint()
                deadline = asyncio.timeout(remaining)
                try:
                    async with deadline:
                        options = {"max_tokens": caps["provider_max_output_tokens"]}
                        if image_data is not None:
                            options["image_data"] = image_data
                        response = await client.generate_response_async(instruction, **options)
                except TimeoutError as exc:
                    if deadline.expired() and isinstance(exc.__cause__, asyncio.CancelledError):
                        exc.interruption_reason = "outer_deadline"
                    raise
                ledger = peek_agent_run_budget(run_id)
                failure = getattr(ledger, "failure", None)
                if failure is not None:
                    raise ScopedAgentWorkTurnStopped(
                        str(getattr(failure, "code", "run_budget_stopped"))
                    )
                failure = getattr(client, "_last_generation_failure", None)
                if failure is not None:
                    try:
                        reason = getattr(failure, "interruption_reason", None)
                    except Exception:
                        reason = None
                    if reason in ("request_limit", "budget_deadline"):
                        exc = GenerationCancelled() if reason == "request_limit" else TimeoutError()
                        exc.interruption_reason = reason
                        raise exc
                    try:
                        failure_kind = generation_failure_kind(failure) or "generation_stopped"
                    except Exception:
                        failure_kind = "generation_stopped"
                    raise ScopedAgentWorkTurnStopped(
                        failure_kind,
                        blocked_source=_generation_blocked_source(failure),
                    )
                evidence_fn = getattr(client, "peek_completed_agent_run_state", None)
                evidence = evidence_fn(run_id) if callable(evidence_fn) else {}
                if not isinstance(evidence, dict):
                    evidence = {}
                if evidence.get("failure"):
                    raise ScopedAgentWorkTurnStopped(str(evidence["failure"]))
                # Retain bounded, non-content audit evidence before the ephemeral client is closed.
                records = (evidence.get("tool_calls") or getattr(client, "_last_audit_tool_calls", None)
                           or getattr(client, "_last_tool_calls", None) or [])
                projected = []
                for record in list(records)[:40]:
                    if is_dataclass(record) and not isinstance(record, type):
                        record = asdict(record)
                    if not isinstance(record, dict):
                        continue
                    encoded = json.dumps(record, sort_keys=True, default=str, ensure_ascii=False).encode()
                    item = {"sha256": hashlib.sha256(encoded).hexdigest()}
                    for key in ("name", "tool", "tool_name", "status", "tool_call_id", "id"):
                        value = record.get(key)
                        if isinstance(value, str):
                            item[key] = value[:160]
                    for key in ("executed", "synthetic", "success"):
                        if isinstance(record.get(key), bool):
                            item[key] = record[key]
                    projected.append(item)
                usage = evidence.get("usage") or getattr(client, "_last_usage", None) or {}
                safe_usage = {key: value for key, value in usage.items()
                              if isinstance(key, str) and len(key) <= 80
                              and isinstance(value, (int, float)) and not isinstance(value, bool)
                              and math.isfinite(value) and value >= 0} if isinstance(usage, dict) else {}
                evidence = {"tool_calls": projected, "usage": safe_usage}
                terminal = sanitize_terminal_evidence(
                    getattr(client, "_last_provider_terminal_evidence", None)
                )
                if terminal is not None:
                    evidence = {**evidence, "provider_terminal": terminal}
                    if terminal["state"] != "complete":
                        raise ScopedAgentWorkTurnStopped(f"provider_{terminal['state']}")
                if not isinstance(response, str) or not response.strip():
                    raise ScopedAgentWorkTurnStopped(
                        "empty or unsupported generation response"
                    )
                if image_data is not None:
                    selected = execution.route
                    evidence["media_request"] = {
                        "delivery": "client_image_data_request",
                        "route": {"provider": selected.provider, "model": selected.model,
                                  "effort": selected.effort, "backend": selected.backend},
                        "image_count": len(image_hashes),
                        "bytes_sha256": list(image_hashes),
                    }
                if sampled_video is not None:
                    evidence["sampled_video"] = sampled_video
                return ScopedAgentWorkTurnResult(
                    response,
                    evidence,
                    budget.request_count,
                    "complete" if terminal else "unknown",
                )
        except (ScopedAgentWorkTurnStopped, TimeoutError, GenerationCancelled) as exc:
            try:
                exc.observation = _failure_observation(client, run_id, budget, caps)
            except Exception:
                pass
            raise
        except RoutePreflightFailure as exc:
            ledger = peek_agent_run_budget(run_id)
            evidence_fn = getattr(client, "peek_completed_agent_run_state", None)
            evidence = evidence_fn(run_id) if callable(evidence_fn) else {}
            tool_calls = (evidence.get("tool_calls") if isinstance(evidence, dict) else None)
            tool_calls = (tool_calls or getattr(client, "_last_audit_tool_calls", None)
                          or getattr(client, "_last_tool_calls", None))
            if getattr(ledger, "failure", None) is not None or tool_calls:
                raise RoutePreflightFailure(exc.code, request_started=True) from exc
            raise
        finally:
            if client is not None:
                await cleanup_ephemeral_llm_client(client)

    try:
        from .free_team_service import openai_billing_mode

        free_image_contract = bool(
            image_data is not None and route.provider == "openai"
            and openai_billing_mode(config, route.model) == "complimentary"
        )
        async def run_candidate(_index: int, selected: Any) -> ScopedAgentWorkTurnResult:
            if image_data is not None and (selected.backend in {"cli", "remote", "pool"} or is_cli_provider(selected.provider) or selected.provider not in {"openai", "openrouter", "deepinfra", "openai_compatible_local"} or model_supports_vision(selected.provider, selected.model) is not True):
                raise ScopedAgentMediaUnsupported("selected route does not support vision")
            if selected.backend in {"cli", "remote", "pool"} or is_cli_provider(selected.provider):
                raise NotImplementedError("finite scoped turns require an API/local route")
            if free_image_contract:
                if (selected.provider != "openai"
                        or openai_billing_mode(config, selected.model) != "complimentary"):
                    raise ScopedAgentMediaUnsupported("complimentary image route changed")
                from .pricing import get_pricing_engine
                from ..llm.image_budget import bind_image_cost_limit, image_budget_scope, video_frames_budget_scope

                pricing = get_pricing_engine()
                await pricing.ensure_loaded()
                scope_factory = video_frames_budget_scope if sampled_video is not None else image_budget_scope
                scope = scope_factory(selected.model, inputs,
                                           price_rule=pricing.resolve("openai", selected.model))
                try:
                    scope.__enter__()
                except ValueError as exc:
                    raise ScopedAgentMediaUnsupported("image budget proof unavailable") from exc
                try:
                    try:
                        bind_image_cost_limit(caps)
                    except ValueError as exc:
                        raise ScopedAgentMediaUnsupported("image cost proof unavailable") from exc
                    return await attempt(replace(execution, route=selected))
                finally:
                    scope.__exit__(None, None, None)
            return await attempt(replace(execution, route=selected))

        result, receipts, selected = await execute_frozen_candidates(route, config, run_candidate)
        if route.candidate_routes is None:
            return result
        return replace(result, evidence={**result.evidence, "candidate_attempts": receipts,
                                         "selected_route": selected.provenance()})
    finally:
        current_generation_budget.reset(budget_token)
        reset_current_agent_run_id(run_token)
        reset_turn_context(context_token)
