"""Content-free provider termination evidence shared with task settlement.

Missing metadata is unknown, never an inferred output limit. Tool-call finishes
are continuation receipts, not proof that the final answer completed.
"""

from collections.abc import Mapping
from typing import Any


_COMPLETE = frozenset({"stop", "eos_token", "end_turn", "stop_sequence"})
_CONTINUATION = frozenset({"tool_calls", "function_call"})
_INCOMPLETE = frozenset({"length", "max_tokens", "max_output_tokens", "content_filter"})
_INTERRUPTED = frozenset({"error", "cancelled", "interrupted"})
_FINISH_REASONS = _COMPLETE | _CONTINUATION | _INCOMPLETE | _INTERRUPTED
_TRANSPORTS = frozenset({"chat.completions", "chat.completions.stream", "responses"})
FAILED_TERMINAL_STATES = frozenset({"incomplete", "interrupted", "protocol_error"})
_STATES = frozenset({"complete", "continuation", "unknown"}) | FAILED_TERMINAL_STATES
_PROTOCOL_ERRORS = frozenset({"missing_tool_calls", "unconsumed_continuation", "review_tool_calls"})
_STATUSES = frozenset({"completed", "incomplete", "failed", "cancelled", "queued", "in_progress"})
_INCOMPLETE_REASONS = frozenset({"max_output_tokens", "content_filter"})


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _enum(value: Any, allowed: frozenset[str]) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and value in allowed else "other"


def normalize_chat_terminal(response: Any, *, transport: str = "chat.completions") -> dict[str, Any]:
    choices = _field(response, "choices")
    choice = choices[0] if isinstance(choices, (list, tuple)) and choices else None
    reason = _enum(_field(choice, "finish_reason"), _FINISH_REASONS)
    state = (
        "complete" if reason in _COMPLETE else
        "continuation" if reason in _CONTINUATION else
        "incomplete" if reason in _INCOMPLETE else
        "interrupted" if reason in _INTERRUPTED else "unknown"
    )
    return {
        "transport": transport if transport in _TRANSPORTS else "other",
        "state": state,
        "finish_reason": reason,
        "finish_reason_present": reason is not None,
        "finish_reason_known": reason is not None and reason != "other",
    }


def normalize_responses_terminal(response: Any) -> dict[str, Any]:
    status = _enum(_field(response, "status"), _STATUSES)
    reason = _enum(_field(_field(response, "incomplete_details"), "reason"), _INCOMPLETE_REASONS)
    return {
        "transport": "responses",
        "state": ("complete" if status == "completed" else
                  "incomplete" if status == "incomplete" else
                  "interrupted" if status in {"failed", "cancelled"} else "unknown"),
        "response_status": status,
        "incomplete_reason": reason,
    }


def interrupted_terminal(transport: str) -> dict[str, Any]:
    """Record an interrupted transport without guessing a provider finish."""
    evidence = normalize_chat_terminal(None, transport=transport)
    evidence["state"] = "interrupted"
    return evidence


def protocol_error_terminal(evidence: Any, reason: str) -> dict[str, Any]:
    """Keep the observed finish while reporting an unconsumed protocol turn."""
    return {
        **(sanitize_terminal_evidence(evidence) or {}),
        "state": "protocol_error",
        "protocol_error": _enum(reason, _PROTOCOL_ERRORS),
    }


def sanitize_terminal_evidence(value: Any) -> dict[str, Any] | None:
    """Project fixed enums/booleans only; no provider text, IDs or exceptions."""
    if not isinstance(value, Mapping):
        return None
    state = _enum(value.get("state"), _STATES)
    evidence = {
        "transport": _enum(value.get("transport"), _TRANSPORTS),
        "state": state if state in _STATES else "unknown",
    }
    for name, allowed in (("finish_reason", _FINISH_REASONS),
                          ("response_status", _STATUSES),
                          ("protocol_error", _PROTOCOL_ERRORS),
                          ("incomplete_reason", _INCOMPLETE_REASONS)):
        if name in value:
            evidence[name] = _enum(value.get(name), allowed)
    if "finish_reason" in evidence:
        reason = evidence["finish_reason"]
        evidence["finish_reason_present"] = reason is not None
        evidence["finish_reason_known"] = reason is not None and reason != "other"
    return evidence
