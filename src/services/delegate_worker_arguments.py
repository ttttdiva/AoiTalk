"""Normalize the model-controlled portion of a worker delegation request.

This does not authorize a worker or construct any trusted execution scope.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .agent_dialogue_budget import normalize_dialogue_budgets


WORKER_KINDS = ("codex", "antigravity", "claude", "grok", "muse")


class DelegateWorkerArgumentError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def normalize_worker_arguments(
    kind: str, budgets: Mapping[str, Any] | None
) -> tuple[str, dict[str, Any]]:
    if not isinstance(kind, str) or not kind.isascii() or kind.strip().casefold() not in WORKER_KINDS:
        raise DelegateWorkerArgumentError("worker_kind_invalid")
    # The service may validate again; omit unset values because the canonical
    # input validator distinguishes an absent ceiling from an explicit null.
    normalized = normalize_dialogue_budgets(budgets).to_dict()
    return kind.strip().casefold(), {k: v for k, v in normalized.items() if v is not None}
