"""Admission check for limits that a finite native phase cannot enforce."""

from collections.abc import Mapping

from ..llm.generation_budget import GenerationBudget


class NativePhaseBudgetUnsupported(ValueError):
    """A hard limit cannot be enforced by the native phase transport."""

    code = "delegation_native_budget_unsupported"


def admit_native_phase_budget(*, dialogue_budget: Mapping | None, parent: GenerationBudget | None) -> None:
    """Reject inherited hard limits that native execution cannot account for.

    The caller retains the parent budget and its usage state; this check does
    not consume, replace, or mark any budget as known.
    """
    if dialogue_budget is not None and not isinstance(dialogue_budget, Mapping):
        raise ValueError("invalid dialogue budget")
    if any((dialogue_budget or {}).get(key) is not None for key in ("max_tokens", "max_cost_micros")):
        raise NativePhaseBudgetUnsupported("native phase cannot enforce dialogue token or cost limits")
    node = parent
    seen = set()
    while node is not None:
        if id(node) in seen:
            raise ValueError("cyclic generation budget")
        seen.add(id(node))
        if node.max_requests is not None:
            raise NativePhaseBudgetUnsupported("native phase cannot enforce inherited request limit")
        node = node.parent
