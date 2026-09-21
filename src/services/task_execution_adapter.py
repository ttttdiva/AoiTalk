"""Execution adapter for internal Task work.

The adapter owns one domain invocation only; claiming, retries and AgentRun
attempt linkage remain in ``AgentWorkCoordinator``.  A deployment can inject
the existing TaskManagementService callback.  Without one, the adapter fails
closed instead of mutating a Task through a synthetic Agent-as-User identity.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any

from .agent_work_runtime import ExecutionOutcome, WorkClaim


class TaskExecutionAdapter:
    adapter_key = "task"
    execution_adapter = "task"

    def __init__(self, execute_callback: Callable[..., Any] | None = None) -> None:
        self.execute_callback = execute_callback

    def required_capabilities(self, claim: WorkClaim) -> tuple[str, ...]:
        del claim
        return ("project_write",)

    async def execute(
        self,
        claim: WorkClaim,
        *,
        coordinator: Any,
        session: Any | None = None,
        actor: Any | None = None,
        attempt: int | None = None,
        run: Any | None = None,
        run_id: str | None = None,
    ) -> ExecutionOutcome:
        callback = self.execute_callback
        if callback is None:
            callback = getattr(coordinator, "task_executor", None)
        if not callable(callback):
            return ExecutionOutcome(
                classification="blocked",
                error_code="task_execution_unavailable",
                error_message="no trusted Task service adapter is configured",
            )
        try:
            kwargs = {
                "claim": claim,
                "coordinator": coordinator,
                "session": session,
                "actor": actor,
                "attempt": attempt if attempt is not None else claim.attempt,
                "run": run,
                "run_id": run_id,
                "task_id": claim.task_id,
                "project_id": claim.project_id,
            }
            try:
                signature = inspect.signature(callback)
                if not any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                ):
                    kwargs = {
                        key: value
                        for key, value in kwargs.items()
                        if key in signature.parameters
                    }
            except (TypeError, ValueError):
                pass
            value = callback(**kwargs)
            if inspect.isawaitable(value):
                value = await value
            return ExecutionOutcome.from_value(value)
        except Exception as exc:
            return ExecutionOutcome(
                classification="transient",
                error_code="task_execution_failed",
                error_message=str(exc)[:500],
            )


__all__ = ["TaskExecutionAdapter"]
