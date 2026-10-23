"""Request-local time and cancellation budget for generation."""

from __future__ import annotations

import math
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from .generation_cancellation import GenerationCancelled, get_current_generation_cancellation


@dataclass
class GenerationBudget:
    timeout_seconds: float | None
    started_at: float = field(default_factory=time.monotonic)
    cancelled: threading.Event = field(default_factory=threading.Event)
    worker_tracked: bool = False
    worker_pending: threading.Event = field(default_factory=threading.Event)
    request_count: int = 0
    enable_thinking: bool | None = None
    response_model: str | None = None
    max_requests: int | None = None
    interruptible_transport: bool = False
    parent: GenerationBudget | None = field(default=None, repr=False)
    request_lock: Any = field(default_factory=threading.Lock, repr=False)
    # Request-local command ceiling; None keeps every legacy caller inactive.
    command_timeout_seconds: float | None = None

    def checkpoint(self) -> float | None:
        inherited = self.parent.checkpoint() if self.parent is not None else None
        handle = get_current_generation_cancellation()
        if self.cancelled.is_set() or (handle and handle.cancel_requested.is_set()):
            raise GenerationCancelled("ephemeral_generation_cancelled")
        if self.timeout_seconds is None:
            return inherited
        remaining = self.timeout_seconds - (time.monotonic() - self.started_at)
        if remaining <= 0:
            raise TimeoutError("ephemeral_generation_timeout")
        return min(remaining, inherited) if inherited is not None else remaining

    def expired(self) -> bool:
        """True once this budget's own wall-clock allowance is spent (cancellation is not consulted)."""
        if self.timeout_seconds is None:
            return False
        return self.timeout_seconds - (time.monotonic() - self.started_at) <= 0

    def record_request(self, payload: dict[str, Any]) -> None:
        self.checkpoint()
        with self.request_lock:
            if self.max_requests is not None and self.request_count >= self.max_requests:
                raise GenerationCancelled("ephemeral_request_budget_exhausted")
            if self.parent is not None:
                self.parent.record_request(payload)
            self.request_count += 1
        extra = payload.get("extra_body")
        template = extra.get("chat_template_kwargs") if isinstance(extra, dict) else None
        thinking = template.get("enable_thinking") if isinstance(template, dict) else None
        self.enable_thinking = thinking if isinstance(thinking, bool) else None

    def provenance(self, status: str) -> dict[str, Any]:
        result = {"status": status}
        if self.worker_tracked or self.request_count:
            result["request_count"] = self.request_count
        if self.worker_tracked:
            result["worker_pending"] = self.worker_pending.is_set()
        if self.timeout_seconds is not None:
            result["timeout_seconds"] = self.timeout_seconds
        if self.enable_thinking is not None:
            result["enable_thinking"] = self.enable_thinking
        if self.response_model:
            result["response_model"] = self.response_model[:160]
        return result


current_generation_budget: ContextVar[GenerationBudget | None] = ContextVar(
    "ephemeral_generation_budget", default=None
)


def command_budget_active() -> bool:
    """True when the current budget chain carries a request-local command ceiling."""
    node = current_generation_budget.get()
    while node is not None and node.command_timeout_seconds is None:
        node = node.parent
    return node is not None


def bounded_command_timeout(requested: float | None) -> float | None:
    """Narrow a command/background lifetime by the active request-local command budget.

    Inactive (no budget carrying ``command_timeout_seconds``): returns ``requested``
    unchanged.  Active: checkpoints the existing budget (no new clock) and returns
    ``min(requested, command_timeout_seconds, remaining)``.  Raises ``TimeoutError``
    or ``GenerationCancelled`` when no positive time remains, so callers never launch.
    """
    budget = current_generation_budget.get()
    node = budget
    while node is not None and node.command_timeout_seconds is None:
        node = node.parent
    if node is None or budget is None:
        return requested
    remaining = budget.checkpoint()
    candidates = [float(node.command_timeout_seconds)]
    if requested is not None:
        candidates.append(float(requested))
    if remaining is not None:
        candidates.append(float(remaining))
    value = min(candidates)
    if not math.isfinite(value) or value <= 0:
        raise TimeoutError("ephemeral_generation_timeout")
    return value


def record_bounded_model_request(payload: dict[str, Any]) -> GenerationBudget | None:
    budget = current_generation_budget.get()
    if budget is None:
        return None
    # A child without its own limit still counts against a limited parent.
    node = budget
    while node is not None and node.max_requests is None:
        node = node.parent
    if node is None:
        return None
    budget.record_request(payload)
    return budget


def constrain_model_request_extra_body(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep SDK extension fields from overriding the reviewed request."""
    result = dict(payload)
    reserved = set(result) | {
        "max_tokens", "max_completion_tokens", "max_output_tokens", "max_new_tokens", "num_predict",
        "n", "candidate_count", "num_return_sequences", "model", "messages", "input", "instructions",
        "tools", "tool_choice", "functions", "function_call", "stream",
    }
    extra = result.get("extra_body")
    if isinstance(extra, dict):
        extra = {key: value for key, value in extra.items() if key not in reserved}
        if extra:
            result["extra_body"] = extra
        else:
            result.pop("extra_body", None)
    return result


def safe_generation_provenance(value: Any) -> dict[str, Any]:
    """Keep only bounded execution facts; never payload or reasoning text."""
    if not isinstance(value, dict):
        return {}
    result = {}
    if value.get("status") in {"completed", "empty", "failed", "timed_out", "cancelled", "unsupported"}:
        result["status"] = value["status"]
    count = value.get("request_count")
    if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 1000:
        result["request_count"] = count
    timeout = value.get("timeout_seconds")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and math.isfinite(timeout) and 0 < timeout <= 1800:
        result["timeout_seconds"] = timeout
    if isinstance(value.get("worker_pending"), bool):
        result["worker_pending"] = value["worker_pending"]
    if isinstance(value.get("enable_thinking"), bool):
        result["enable_thinking"] = value["enable_thinking"]
    model = value.get("response_model")
    if isinstance(model, str) and model.strip():
        result["response_model"] = model.strip()[:160]
    return result
