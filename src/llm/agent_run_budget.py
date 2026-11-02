"""Run-wide safety budgets for typed Agent ``chat_turn`` generation.

The request/tool loops in the provider adapters are shorter lived than an
AgentRun: review and continuation callbacks can create fresh loops while the
durable Run remains the same.  This module keeps their confirmed usage and
attempt counts in one process-local ledger keyed by the trusted AgentRun id.
"""

from __future__ import annotations

import threading
from contextvars import ContextVar, Token
from dataclasses import dataclass
from decimal import Decimal
import math
import re
from typing import Any, Mapping


DEFAULT_AGENT_RUN_MAX_TOTAL_TOKENS = 200_000
DEFAULT_AGENT_RUN_MAX_TOOL_CALLS = 24
AGENT_RUN_TOKEN_BUDGET_CODE = "run_budget_exceeded:tokens"
AGENT_RUN_COST_BUDGET_CODE = "run_budget_exceeded:cost"
AGENT_RUN_TOOL_CALL_BUDGET_CODE = "run_budget_exceeded:tool_calls"
AGENT_RUN_TOKEN_USAGE_UNAVAILABLE_CODE = "run_budget_usage_unavailable:tokens"


def _positive_limit(value: Any, default: int) -> int:
    """Read a positive finite integer setting; invalid values use the default."""

    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if value > 0 else default
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer() and value > 0:
            return int(value)
        return default
    if isinstance(value, Decimal):
        if value.is_finite() and value == value.to_integral_value() and value > 0:
            try:
                return int(value)
            except (TypeError, ValueError, OverflowError):
                return default
        return default
    if isinstance(value, str):
        text = value.strip()
        if text.isdecimal():
            try:
                parsed = int(text)
            except (TypeError, ValueError, OverflowError):
                return default
            return parsed if parsed > 0 else default
    return default


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, None)
        except TypeError:
            try:
                value = getter(key)
            except Exception:
                value = None
        except Exception:
            value = None
        if value is not None:
            return value
    if isinstance(config, Mapping):
        current: Any = config
        for part in key.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return default
            current = current[part]
        return current
    return default


def _token_total(usage: Mapping[str, Any] | None) -> int | None:
    """Return confirmed token usage without estimating missing provider data."""

    if not isinstance(usage, Mapping):
        return None
    try:
        if usage.get("total_tokens") is not None and usage.get("total_tokens_source") != "derived":
            return max(0, int(usage["total_tokens"]))
        input_tokens = usage.get("input_tokens")
        if input_tokens is None:
            input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("output_tokens")
        if output_tokens is None:
            output_tokens = usage.get("completion_tokens")
        # A partial receipt cannot bound total input + output usage.  Zero is
        # accepted only when the provider explicitly reports that side.
        if input_tokens is None or output_tokens is None:
            return None
        reasoning_tokens = usage.get("reasoning_tokens", 0) or 0
        total = int(input_tokens) + int(output_tokens)
        if usage.get("output_includes_reasoning") is not True:
            total += int(reasoning_tokens)
        return max(0, total)
    except (TypeError, ValueError, OverflowError):
        return None


def is_http_429_error(error: BaseException) -> bool:
    """Recognize a confirmed HTTP 429 without interpreting its retry policy."""

    status = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    if isinstance(status, bool):
        return False
    if isinstance(status, int):
        return status == 429
    return isinstance(status, str) and status.strip() == "429"


def _finite_dialogue_token_limit(dialogue_budget: Any) -> int | None:
    if not isinstance(dialogue_budget, Mapping):
        return None
    raw = dialogue_budget.get("max_tokens")
    if raw in (None, ""):
        return None
    normalized = _positive_limit(raw, -1)
    return normalized if normalized > 0 else None


@dataclass(frozen=True)
class AgentRunBudgetFailure:
    code: str
    kind: str
    measured_tokens: int | None
    token_limit: int
    request_count: int
    tool_call_count: int
    tool_call_limit: int
    token_usage_state: str = "known"
    cost_limit_micros: int | None = None
    measured_cost_micros: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "kind": self.kind,
            "request_count": max(0, int(self.request_count)),
            "token_usage_state": (
                self.token_usage_state
                if self.token_usage_state in {"known", "partial", "unknown"}
                else "unknown"
            ),
        }
        if self.kind == "cost":
            payload["cost_limit_micros"] = max(
                1, int(self.cost_limit_micros or 0)
            )
            if self.measured_cost_micros is not None:
                payload["measured_cost_micros"] = max(
                    0, int(self.measured_cost_micros)
                )
        elif self.kind in {"tokens", "usage_unavailable"}:
            payload["token_limit"] = max(1, int(self.token_limit))
            if self.measured_tokens is not None:
                payload["measured_tokens"] = max(0, int(self.measured_tokens))
            if self.kind == "usage_unavailable":
                payload["failure_reason"] = "usage_unavailable"
        elif self.kind == "cost":
            payload["cost_limit_micros"] = max(1, int(self.cost_limit_micros or 1))
            if self.measured_cost_micros is not None:
                payload["measured_cost_micros"] = max(0, int(self.measured_cost_micros))
        else:
            payload["tool_call_count"] = max(0, int(self.tool_call_count))
            payload["tool_call_limit"] = max(1, int(self.tool_call_limit))
            payload["token_limit"] = max(1, int(self.token_limit))
            if self.measured_tokens is not None:
                payload["measured_tokens"] = max(0, int(self.measured_tokens))
        return payload


@dataclass(frozen=True)
class AgentRunToolFailureCircuit:
    """Safe terminal receipt for repeated failures of one tool in a Run."""

    code: str
    kind: str
    tool_name: str
    failure_streak: int


class AgentRunBudgetStop(RuntimeError):
    """Internal signal used to prevent a provider request after its Run cap."""

    def __init__(
        self,
        failure: AgentRunBudgetFailure | AgentRunToolFailureCircuit,
    ) -> None:
        self.failure = failure
        super().__init__(failure.code)


class AgentRunBudgetLedger:
    """Thread-safe counters shared by every provider request in one AgentRun."""

    def __init__(
        self,
        *,
        run_id: str,
        token_limit: int,
        tool_call_limit: int,
        cost_limit_micros: int | None = None,
    ) -> None:
        self.run_id = str(run_id)
        self.token_limit = max(1, int(token_limit))
        self.tool_call_limit = max(1, int(tool_call_limit))
        self.cost_limit_micros = (
            max(1, int(cost_limit_micros)) if cost_limit_micros else None
        )
        self.request_count = 0
        self.tool_call_count = 0
        self.measured_tokens = 0
        self._has_token_measurement = False
        self.token_usage_unknown = False
        self._delegation_reservations: dict[str, int] = {}
        self._delegated_reserved_tokens = 0
        self.measured_cost_micros = 0
        self._has_cost_measurement = False
        self.cost_usage_unknown = False
        self._delegation_cost_reservations: dict[str, int] = {}
        self._delegated_reserved_cost_micros = 0
        self._delegation_settlements: dict[str, dict[str, Any]] = {}
        self._delegation_overrun_tokens = 0
        self._delegation_overrun_cost_micros = 0
        self._delegation_token_usage_unknown = False
        self._delegation_cost_usage_unknown = False
        self._active_requests = 0
        self._failure: AgentRunBudgetFailure | AgentRunToolFailureCircuit | None = None
        self._tool_failure_streaks: dict[str, int] = {}
        self._lock = threading.RLock()

    @property
    def failure(self) -> AgentRunBudgetFailure | AgentRunToolFailureCircuit | None:
        with self._lock:
            return self._failure

    def tighten(
        self,
        *,
        token_limit: int | None = None,
        tool_call_limit: int | None = None,
        cost_limit_micros: int | None = None,
    ) -> None:
        with self._lock:
            if token_limit is not None and token_limit > 0:
                self.token_limit = min(self.token_limit, int(token_limit))
            if tool_call_limit is not None and tool_call_limit > 0:
                self.tool_call_limit = min(self.tool_call_limit, int(tool_call_limit))
            if cost_limit_micros is not None and cost_limit_micros > 0:
                self.cost_limit_micros = min(
                    self.cost_limit_micros or int(cost_limit_micros),
                    int(cost_limit_micros),
                )
            if self._failure is None and self.tool_call_count > self.tool_call_limit:
                self._failure = self._make_failure("tool_calls")
            elif (
                self._failure is None
                and not self.token_usage_unknown
                and self._has_token_measurement
                and self.measured_tokens + self._delegated_reserved_tokens
                >= self.token_limit
            ):
                self._failure = self._make_failure("tokens")
            elif (
                self._failure is None
                and self.cost_limit_micros is not None
                and not self.cost_usage_unknown
                and self._has_cost_measurement
                and self.measured_cost_micros + self._delegated_reserved_cost_micros
                >= self.cost_limit_micros
            ):
                self._failure = self._make_failure("cost")

    def begin_provider_request(self) -> bool:
        """Admit one physical provider attempt, or fail closed at the ceiling."""

        with self._lock:
            if self._failure is not None:
                return False
            if (
                not self.token_usage_unknown
                and self._has_token_measurement
                and self.measured_tokens + self._delegated_reserved_tokens
                >= self.token_limit
            ):
                self._failure = self._make_failure("tokens")
                return False
            if self.cost_limit_micros is not None and not self.cost_usage_unknown:
                if (
                    self._has_cost_measurement
                    and self.measured_cost_micros
                    + self._delegated_reserved_cost_micros
                    >= self.cost_limit_micros
                ):
                    self._failure = self._make_failure("cost")
                    return False
            if self._delegated_reserved_cost_micros:
                if self.cost_usage_unknown:
                    self._failure = self._make_failure("usage_unavailable")
                    return False
            self.request_count += 1
            self._active_requests += 1
            return True

    def record_provider_response(
        self,
        usage: Mapping[str, Any] | None,
        *,
        token_usage_optional: bool = False,
        usage_incomplete: bool = False,
        cost_reservation_key: str | None = None,
    ) -> AgentRunBudgetFailure | None:
        """Add one response receipt after usage normalization has completed."""

        with self._lock:
            if self._active_requests:
                self._active_requests -= 1
            token_total = _token_total(usage)
            cost_total: int | None = None
            if isinstance(usage, Mapping) and usage.get("cost_micros") is not None:
                raw_cost = usage.get("cost_micros")
                if not isinstance(raw_cost, bool):
                    try:
                        parsed_cost = int(raw_cost)
                        if parsed_cost >= 0 and str(raw_cost).strip() == str(parsed_cost):
                            cost_total = parsed_cost
                    except (TypeError, ValueError, OverflowError):
                        cost_total = None
            if cost_reservation_key is not None and cost_total is not None:
                # Release and record while holding the same lock: another
                # provider request must not see a temporarily free balance.
                if not self.release_delegation_cost_micros(cost_reservation_key):
                    cost_total = None
            if token_total is None:
                # Unknown is not zero. The tool-call cap remains active while
                # explicitly unmetered CLI routes may continue without token
                # comparison. Metered routes fail closed before another call.
                self.token_usage_unknown = True
                if (
                    (not token_usage_optional or self._delegated_reserved_tokens)
                    and self._failure is None
                ):
                    self._failure = self._make_failure("usage_unavailable")
            elif usage_incomplete:
                self.token_usage_unknown = True
            if self.cost_limit_micros is not None:
                if cost_total is None:
                    self.cost_usage_unknown = True
                    if self._delegated_reserved_cost_micros and self._failure is None:
                        self._failure = self._make_failure("usage_unavailable")
                else:
                    self.measured_cost_micros += cost_total
                    self._has_cost_measurement = True
                    if (
                        self.cost_limit_micros is not None
                        and (self._delegated_reserved_cost_micros or cost_reservation_key is not None)
                        and self.measured_cost_micros
                        + self._delegated_reserved_cost_micros
                        > self.cost_limit_micros
                        and self._failure is None
                    ):
                        self._failure = self._make_failure("cost")
            if token_total is None:
                return self._failure
            self.measured_tokens += token_total
            self._has_token_measurement = True
            if (usage_incomplete and (not token_usage_optional or self._delegated_reserved_tokens)
                    and self._failure is None):
                self._failure = self._make_failure("usage_unavailable")
            if (
                self._failure is None
                and not self.token_usage_unknown
                and self.measured_tokens + self._delegated_reserved_tokens
                > self.token_limit
            ):
                self._failure = self._make_failure("tokens")
            return self._failure

    def mark_provider_usage_unknown(self) -> None:
        with self._lock:
            if self._active_requests:
                self._active_requests -= 1
            self.token_usage_unknown = True
            if self.cost_limit_micros is not None:
                self.cost_usage_unknown = True
            if (self._delegated_reserved_tokens or self._delegated_reserved_cost_micros) and self._failure is None:
                self._failure = self._make_failure("usage_unavailable")

    def abort_provider_request(self) -> None:
        """Release a reserved request that was rejected before provider send."""

        with self._lock:
            if self._active_requests:
                self._active_requests -= 1

    def mark_token_usage_unavailable(self) -> AgentRunBudgetFailure:
        """Latch the safe terminal failure when Run usage evidence is incomplete."""

        with self._lock:
            if self._active_requests:
                self._active_requests -= 1
            self.token_usage_unknown = True
            if self._failure is None:
                self._failure = self._make_failure("usage_unavailable")
            return self._failure

    def record_tool_call(self) -> bool:
        """Count one requested call at intake, including rejected attempts."""

        with self._lock:
            self.tool_call_count += 1
            if self._failure is not None:
                return False
            if self.tool_call_count > self.tool_call_limit:
                self._failure = self._make_failure("tool_calls")
                return False
            return True

    def record_tool_outcome(
        self,
        tool_name: str,
        success: bool,
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        """Track per-tool failed-call streaks across all loops for this Run.

        A successful call resets only that tool's streak.  The third failure
        opens the existing terminal tool-failure circuit, regardless of the
        individual failure codes, argument signatures, or reused provider
        call IDs. ``tool_call_id`` is accepted as trace context only and never
        deduplicates a real tool execution across the Run.
        """

        _ = tool_call_id
        raw_name = str(tool_name or "").strip().rsplit(".", 1)[-1].casefold()
        safe_name = (
            raw_name
            if re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,127}", raw_name)
            else "unknown_tool"
        )
        with self._lock:
            if success:
                self._tool_failure_streaks.pop(safe_name, None)
                return isinstance(self._failure, AgentRunToolFailureCircuit)
            streak = self._tool_failure_streaks.get(safe_name, 0) + 1
            self._tool_failure_streaks[safe_name] = streak
            if streak >= 3 and self._failure is None:
                self._failure = AgentRunToolFailureCircuit(
                    code="tool_failure_circuit_open",
                    kind="tool_failure_circuit_open",
                    tool_name=safe_name,
                    failure_streak=streak,
                )
            return isinstance(self._failure, AgentRunToolFailureCircuit)

    def restore_persisted_state(
        self,
        *,
        request_count: int,
        tool_call_count: int,
        measured_tokens: int | None,
        token_usage_state: str,
    ) -> None:
        """Restore counters from durable Run evidence before resumed admission."""

        try:
            restored_requests = max(0, int(request_count))
            restored_tool_calls = max(0, int(tool_call_count))
            restored_tokens = (
                None if measured_tokens is None else max(0, int(measured_tokens))
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("persisted AgentRun budget counters are invalid") from exc
        if token_usage_state not in {"known", "partial", "unknown"}:
            raise ValueError("persisted AgentRun token state is invalid")
        if token_usage_state == "known" and restored_tokens is None:
            raise ValueError("known persisted AgentRun token usage is missing")

        with self._lock:
            if (
                self.request_count
                or self.tool_call_count
                or self.measured_tokens
                or self._has_token_measurement
                or self._failure is not None
                or self._active_requests
            ):
                raise ValueError("AgentRun budget ledger is not empty")
            self.request_count = restored_requests
            self.tool_call_count = restored_tool_calls
            if restored_tokens is not None:
                self.measured_tokens = restored_tokens
                self._has_token_measurement = True
            self.token_usage_unknown = token_usage_state != "known"

            if self.tool_call_count > self.tool_call_limit:
                self._failure = self._make_failure("tool_calls")
            elif (
                token_usage_state == "known"
                and self._has_token_measurement
                and self.measured_tokens >= self.token_limit
            ):
                self._failure = self._make_failure("tokens")

    def count_unexecuted_tool_calls(self, count: int) -> None:
        """Count the rest of a provider batch after its first rejected call."""

        try:
            amount = max(0, int(count))
        except (TypeError, ValueError, OverflowError):
            amount = 0
        if not amount:
            return
        with self._lock:
            self.tool_call_count += amount
            if self._failure is None and self.tool_call_count > self.tool_call_limit:
                self._failure = self._make_failure("tool_calls")
            elif self._failure is not None and self._failure.kind == "tool_calls":
                self._failure = self._make_failure("tool_calls")

    def usage_state(self) -> dict[str, Any]:
        with self._lock:
            result: dict[str, Any] = {
                "token_limit": self.token_limit,
                "tool_call_limit": self.tool_call_limit,
                "tool_call_count": self.tool_call_count,
                "request_count": self.request_count,
                "token_usage_state": (
                    "partial"
                    if self.token_usage_unknown and self._has_token_measurement
                    else "unknown"
                    if self.token_usage_unknown
                    else "known"
                ),
            }
            if self._has_token_measurement:
                result["measured_tokens"] = self.measured_tokens
            if self._delegation_token_usage_unknown:
                result["delegation_token_usage_state"] = "unknown"
            if self._delegated_reserved_tokens:
                result["delegated_reserved_tokens"] = self._delegated_reserved_tokens
            if self._delegation_overrun_tokens:
                result["delegation_overrun_tokens"] = self._delegation_overrun_tokens
            if self.cost_limit_micros is not None:
                result["cost_limit_micros"] = self.cost_limit_micros
                if self._has_cost_measurement:
                    result["measured_cost_micros"] = self.measured_cost_micros
                if self._delegated_reserved_cost_micros:
                    result["delegated_reserved_cost_micros"] = (
                        self._delegated_reserved_cost_micros
                    )
                if self._delegation_overrun_cost_micros:
                    result["delegation_overrun_cost_micros"] = (
                        self._delegation_overrun_cost_micros
                    )
                if self._delegation_cost_usage_unknown:
                    result["delegation_cost_usage_state"] = "unknown"
                if self.cost_usage_unknown:
                    result["cost_usage_state"] = "unknown"
                elif self._has_cost_measurement:
                    result["cost_usage_state"] = "known"
            return result

    def remaining_delegation_tokens(self) -> int | None:
        """Return known remaining run capacity, including child reservations."""

        with self._lock:
            # A fresh ledger has no provider receipt yet, but its measured
            # usage is still zero.  Treat that state as known so a child can
            # reserve its default ceiling before the caller's first request.
            if self.token_usage_unknown:
                return None
            return max(
                0,
                self.token_limit
                - self.measured_tokens
                - self._delegated_reserved_tokens,
            )

    def reserve_delegation_tokens(self, reservation_key: str, tokens: int) -> int | None:
        """Reserve a child Dialogue ceiling against known caller run capacity.

        ``None`` means usage is unknown, the key conflicts, or the requested
        ceiling exceeds the remaining run budget. Repeating the same key and
        amount is idempotent.
        """

        key = str(reservation_key or "").strip()
        if not key or isinstance(tokens, bool):
            return None
        try:
            amount = int(tokens)
        except (TypeError, ValueError, OverflowError):
            return None
        if amount < 0:
            return None
        with self._lock:
            if self.token_usage_unknown:
                return None
            existing = self._delegation_reservations.get(key)
            if existing is not None:
                if existing != amount:
                    return None
                return max(
                    0,
                    self.token_limit
                    - self.measured_tokens
                    - self._delegated_reserved_tokens,
                )
            remaining = max(
                0,
                self.token_limit
                - self.measured_tokens
                - self._delegated_reserved_tokens,
            )
            if amount > remaining:
                return None
            self._delegation_reservations[key] = amount
            self._delegated_reserved_tokens += amount
            return remaining - amount

    def release_delegation_tokens(self, reservation_key: str) -> bool:
        """Release one child reservation exactly once after failed admission."""

        key = str(reservation_key or "").strip()
        if not key:
            return False
        with self._lock:
            amount = self._delegation_reservations.pop(key, None)
            if amount is None:
                return False
            self._delegated_reserved_tokens = max(
                0, self._delegated_reserved_tokens - amount
            )
            return True

    @staticmethod
    def _non_negative_int(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed < 0:
            return None
        return parsed

    def settle_delegation_tokens(
        self,
        reservation_key: str,
        actual_tokens: int | None,
    ) -> dict[str, Any] | None:
        """Settle a child token reservation after its provider has returned.

        The child provider is uninterruptible from this ledger's point of
        view.  The reservation is therefore removed only at settlement, the
        actual receipt is charged, and any excess is retained as post hoc
        overrun evidence.  Repeated settlement of the same key is idempotent.
        """

        key = str(reservation_key or "").strip()
        if not key:
            return None
        parsed = self._non_negative_int(actual_tokens)
        with self._lock:
            cache_key = f"tokens:{key}"
            cached = self._delegation_settlements.get(cache_key)
            if cached is not None:
                return dict(cached)
            reserved = self._delegation_reservations.pop(key, None)
            if reserved is None:
                return None
            self._delegated_reserved_tokens = max(
                0, self._delegated_reserved_tokens - reserved
            )
            overrun = False
            if parsed is None:
                # The provider already ran. Charge the reserved ceiling as a
                # conservative estimate and retain an explicit unknown marker;
                # do not turn a missing receipt into a permanent caller Run
                # failure or release capacity for an unbounded retry.
                parsed = reserved
                self._delegation_token_usage_unknown = True
            else:
                overrun = parsed > reserved
                if overrun:
                    self._delegation_overrun_tokens += parsed - reserved
            self.measured_tokens += parsed
            self._has_token_measurement = True
            if (
                self._failure is None
                and self.measured_tokens + self._delegated_reserved_tokens
                > self.token_limit
            ):
                self._failure = self._make_failure("tokens")
            result = {
                "reservation_key": key,
                "reserved_tokens": reserved,
                "actual_tokens": None if actual_tokens is None else parsed,
                "charged_tokens": parsed,
                "overrun": overrun,
                "overrun_tokens": max(0, parsed - reserved),
                "remaining_tokens": max(
                    0,
                    self.token_limit
                    - self.measured_tokens
                    - self._delegated_reserved_tokens,
                ),
            }
            self._delegation_settlements[cache_key] = dict(result)
            return result

    def remaining_delegation_cost_micros(self) -> int | None:
        """Return known remaining cost capacity, or ``None`` if unmetered/unknown."""

        with self._lock:
            if (
                self.cost_limit_micros is None
                or self.cost_usage_unknown
            ):
                return None
            return max(
                0,
                self.cost_limit_micros
                - self.measured_cost_micros
                - self._delegated_reserved_cost_micros,
            )

    def reserve_delegation_cost_micros(
        self,
        reservation_key: str,
        amount: int,
    ) -> int | None:
        """Reserve a child cost ceiling against known caller-run cost usage."""

        key = str(reservation_key or "").strip()
        if not key or isinstance(amount, bool):
            return None
        try:
            requested = int(amount)
        except (TypeError, ValueError, OverflowError):
            return None
        if requested < 0:
            return None
        with self._lock:
            if (
                self.cost_limit_micros is None
                or self.cost_usage_unknown
            ):
                return None
            existing = self._delegation_cost_reservations.get(key)
            if existing is not None:
                if existing != requested:
                    return None
                return max(
                    0,
                    self.cost_limit_micros
                    - self.measured_cost_micros
                    - self._delegated_reserved_cost_micros,
                )
            remaining = max(
                0,
                self.cost_limit_micros
                - self.measured_cost_micros
                - self._delegated_reserved_cost_micros,
            )
            if requested > remaining:
                return None
            self._delegation_cost_reservations[key] = requested
            self._delegated_reserved_cost_micros += requested
            return remaining - requested

    def release_delegation_cost_micros(self, reservation_key: str) -> bool:
        key = str(reservation_key or "").strip()
        if not key:
            return False
        with self._lock:
            amount = self._delegation_cost_reservations.pop(key, None)
            if amount is None:
                return False
            self._delegated_reserved_cost_micros = max(
                0,
                self._delegated_reserved_cost_micros - amount,
            )
            return True

    def settle_delegation_cost_micros(
        self,
        reservation_key: str,
        actual_cost_micros: int | None,
    ) -> dict[str, Any] | None:
        """Settle a child cost reservation and retain any post hoc overrun."""

        key = str(reservation_key or "").strip()
        if not key:
            return None
        parsed = self._non_negative_int(actual_cost_micros)
        with self._lock:
            cache_key = f"cost:{key}"
            cached = self._delegation_settlements.get(cache_key)
            if cached is not None:
                return dict(cached)
            reserved = self._delegation_cost_reservations.pop(key, None)
            if reserved is None:
                return None
            self._delegated_reserved_cost_micros = max(
                0, self._delegated_reserved_cost_micros - reserved
            )
            overrun = False
            if parsed is None:
                parsed = reserved
                self._delegation_cost_usage_unknown = True
            else:
                overrun = parsed > reserved
                if overrun:
                    self._delegation_overrun_cost_micros += parsed - reserved
            self.measured_cost_micros += parsed
            self._has_cost_measurement = True
            if (
                self._failure is None
                and self.cost_limit_micros is not None
                and self.measured_cost_micros
                + self._delegated_reserved_cost_micros
                > self.cost_limit_micros
            ):
                self._failure = self._make_failure("cost")
            result = {
                "reservation_key": key,
                "reserved_cost_micros": reserved,
                "actual_cost_micros": None if actual_cost_micros is None else parsed,
                "charged_cost_micros": parsed,
                "overrun": overrun,
                "overrun_cost_micros": max(0, parsed - reserved),
                "remaining_cost_micros": (
                    max(
                        0,
                        (self.cost_limit_micros or 0)
                        - self.measured_cost_micros
                        - self._delegated_reserved_cost_micros,
                    )
                    if self.cost_limit_micros is not None
                    else None
                ),
            }
            self._delegation_settlements[cache_key] = dict(result)
            return result

    def settle_delegation(
        self,
        reservation_key: str,
        *,
        actual_tokens: int | None = None,
        actual_cost_micros: int | None = None,
    ) -> dict[str, Any] | None:
        """Settle both dimensions of one child reservation atomically enough.

        Token and cost reservations use separate counters for compatibility
        with existing callers.  This convenience method returns both
        receipts and keeps a missing dimension untouched when no reservation
        exists for it.
        """

        token = self.settle_delegation_tokens(reservation_key, actual_tokens)
        cost = self.settle_delegation_cost_micros(
            reservation_key, actual_cost_micros
        )
        if token is None and cost is None:
            return None
        return {
            "reservation_key": str(reservation_key or "").strip(),
            "tokens": token,
            "cost": cost,
            "overrun": bool(
                (token and token.get("overrun"))
                or (cost and cost.get("overrun"))
            ),
        }

    def _make_failure(self, kind: str) -> AgentRunBudgetFailure:
        code = {
            "tokens": AGENT_RUN_TOKEN_BUDGET_CODE,
            "cost": AGENT_RUN_COST_BUDGET_CODE,
            "tool_calls": AGENT_RUN_TOOL_CALL_BUDGET_CODE,
            "usage_unavailable": AGENT_RUN_TOKEN_USAGE_UNAVAILABLE_CODE,
        }.get(kind, AGENT_RUN_TOOL_CALL_BUDGET_CODE)
        return AgentRunBudgetFailure(
            code=code,
            kind=kind,
            measured_tokens=(
                self.measured_tokens if self._has_token_measurement else None
            ),
            token_limit=self.token_limit,
            request_count=self.request_count,
            tool_call_count=self.tool_call_count,
            tool_call_limit=self.tool_call_limit,
            token_usage_state=(
                "partial"
                if self.token_usage_unknown and self._has_token_measurement
                else "unknown"
                if self.token_usage_unknown
                else "known"
            ),
            cost_limit_micros=self.cost_limit_micros,
            measured_cost_micros=(
                self.measured_cost_micros if self._has_cost_measurement else None
            ),
        )


def budget_failure_generation_failure(
    failure: AgentRunBudgetFailure | AgentRunToolFailureCircuit | None,
) -> Any | None:
    """Project a fixed budget code into the common generation failure type."""

    if failure is None:
        return None
    if isinstance(failure, AgentRunToolFailureCircuit):
        from .generation_error import tool_failure_circuit_open_failure

        return tool_failure_circuit_open_failure(
            family="agent_run",
            signature=failure.tool_name,
        )
    from .generation_error import GenerationFailure

    return GenerationFailure(
        kind=(
            "run_budget_usage_unavailable"
            if failure.code == AGENT_RUN_TOKEN_USAGE_UNAVAILABLE_CODE
            else "run_budget_exceeded"
        ),
        user_message=(
            "Run全体のツール呼び出し上限に達したため、自動処理を停止しました。"
            if failure.kind == "tool_calls"
            else "Run全体のcost上限に達したため、自動処理を停止しました。"
            if failure.kind == "cost"
            else "Run全体のtoken usageを確認できないため、自動処理を停止しました。"
            if failure.kind == "usage_unavailable"
            else "Run全体のcost上限に達したため、自動処理を停止しました。"
            if failure.kind == "cost"
            else "Run全体のtoken上限に達したため、自動処理を停止しました。"
        ),
        technical_detail=failure.code,
    )


def safe_budget_failure_payload(
    failure: AgentRunBudgetFailure | AgentRunToolFailureCircuit | None,
) -> dict[str, Any] | None:
    """Return a content-free persistence payload for an allowlisted failure."""

    if failure is None:
        return None
    if failure.code not in {
        AGENT_RUN_TOKEN_BUDGET_CODE,
        AGENT_RUN_COST_BUDGET_CODE,
        AGENT_RUN_TOOL_CALL_BUDGET_CODE,
        AGENT_RUN_TOKEN_USAGE_UNAVAILABLE_CODE,
    }:
        return None
    return failure.to_dict()


_RUN_BUDGETS: dict[str, AgentRunBudgetLedger] = {}
_RUN_BUDGETS_LOCK = threading.RLock()
_SESSION_AGENT_RUN_BUDGET_ENABLED: ContextVar[bool] = ContextVar(
    "aoitalk_session_agent_run_budget_enabled",
    default=False,
)


def enable_session_agent_run_budget() -> Token:
    """Enable the run-wide limits for one trusted P4 Aoi Chat invocation."""

    return _SESSION_AGENT_RUN_BUDGET_ENABLED.set(True)


def reset_session_agent_run_budget(token: Token) -> None:
    _SESSION_AGENT_RUN_BUDGET_ENABLED.reset(token)


def resolve_agent_run_budget_limits(
    config: Any | None,
    dialogue_budget: Any | None = None,
) -> tuple[int, int, int | None]:
    token_limit = _positive_limit(
        _config_get(config, "runtime.agent_run_max_total_tokens"),
        DEFAULT_AGENT_RUN_MAX_TOTAL_TOKENS,
    )
    tool_call_limit = _positive_limit(
        _config_get(config, "runtime.agent_run_max_tool_calls"),
        DEFAULT_AGENT_RUN_MAX_TOOL_CALLS,
    )
    dialogue_token_limit = _finite_dialogue_token_limit(dialogue_budget)
    if dialogue_token_limit is not None:
        token_limit = min(token_limit, dialogue_token_limit)
    cost_limit: int | None = None
    if isinstance(dialogue_budget, Mapping):
        cost_limit = _positive_limit(
            dialogue_budget.get("max_cost_micros"),
            -1,
        )
        if cost_limit <= 0:
            cost_limit = None
    return token_limit, tool_call_limit, cost_limit


def get_agent_run_budget(
    client: Any | None = None,
    *,
    run_id: str | None = None,
    config: Any | None = None,
    dialogue_budget: Any | None = None,
    require_typed_agent: bool = True,
) -> AgentRunBudgetLedger | None:
    """Return the ledger for the current trusted AgentRun, creating it once.

    A reused Character client can serve concurrent Runs. Keying by the trusted
    Run id keeps their counters separate while review/tool continuations under
    the same id share one ledger.
    """

    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        try:
            from ..services.agent_run_service import get_current_agent_run_id

            normalized_run_id = str(get_current_agent_run_id() or "").strip()
        except Exception:
            normalized_run_id = ""
    if not normalized_run_id:
        return None

    if config is None:
        config = getattr(client, "config", None)
    if dialogue_budget is None:
        dialogue_budget = getattr(client, "_agent_dialogue_budget", None)

    if require_typed_agent:
        try:
            from ..services.turn_context import get_turn_context

            turn = get_turn_context()
            typed_agent = bool(
                getattr(turn, "agent_id", None)
                or getattr(turn, "agent_revision_id", None)
                or getattr(turn, "agent_execution_binding", None)
                or getattr(client, "agent_id", None)
                or getattr(client, "agent_revision_id", None)
                or _SESSION_AGENT_RUN_BUDGET_ENABLED.get()
            )
        except Exception:
            typed_agent = bool(
                getattr(client, "agent_id", None)
                or getattr(client, "agent_revision_id", None)
            )
        if not typed_agent and not isinstance(dialogue_budget, Mapping):
            return None

    token_limit, tool_call_limit, cost_limit = resolve_agent_run_budget_limits(
        config,
        dialogue_budget,
    )
    with _RUN_BUDGETS_LOCK:
        ledger = _RUN_BUDGETS.get(normalized_run_id)
        if ledger is None:
            ledger = AgentRunBudgetLedger(
                run_id=normalized_run_id,
                token_limit=token_limit,
                tool_call_limit=tool_call_limit,
                cost_limit_micros=cost_limit,
            )
            _RUN_BUDGETS[normalized_run_id] = ledger
        else:
            ledger.tighten(
                token_limit=token_limit,
                tool_call_limit=tool_call_limit,
                cost_limit_micros=cost_limit,
            )
        return ledger


def discard_agent_run_budget(run_id: str | None) -> None:
    normalized = str(run_id or "").strip()
    if normalized:
        with _RUN_BUDGETS_LOCK:
            _RUN_BUDGETS.pop(normalized, None)


def peek_agent_run_budget(run_id: str | None) -> AgentRunBudgetLedger | None:
    normalized = str(run_id or "").strip()
    if not normalized:
        return None
    with _RUN_BUDGETS_LOCK:
        return _RUN_BUDGETS.get(normalized)


def reset_agent_run_budgets_for_tests() -> None:
    """Clear the process-local ledger registry for isolated tests."""

    with _RUN_BUDGETS_LOCK:
        _RUN_BUDGETS.clear()
