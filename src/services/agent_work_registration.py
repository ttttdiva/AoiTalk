"""Composition helpers for the common autonomous AgentWork runtime.

The helper is intentionally small and side-effect free until ``start`` is
called by the application lifespan.  Feature gates are evaluated at build
time and again by the coordinator, so stale configuration cannot register a
company/media worker in an Enterprise-disabled profile.
"""

from __future__ import annotations

from typing import Any

from ..features import Features
from .agent_work_runtime import AgentWorkCoordinator
from .task_work_source import TaskWorkSource
from .task_execution_adapter import TaskExecutionAdapter


def build_agent_work_coordinator(
    db_manager: Any,
    *,
    config: Any | None = None,
    code_adapter: Any | None = None,
    media_sources: list[Any] | None = None,
    media_adapters: list[Any] | None = None,
    task_executor: Any | None = None,
    employee_services: Any | None = None,
    **kwargs: Any,
) -> AgentWorkCoordinator | None:
    """Build the one common coordinator, or ``None`` when disabled."""

    if not Features.autonomous_agent_runtime():
        return None
    coordinator = AgentWorkCoordinator(
        db_manager,
        config=config,
        task_executor=task_executor,
        **kwargs,
    )
    if Features.virtual_company() or Features.code_agent():
        coordinator.register_source(TaskWorkSource())
        coordinator.register_adapter(TaskExecutionAdapter(task_executor))
    if Features.media_operations_autonomy():
        for source in media_sources or ():
            coordinator.register_source(source)
        for adapter in media_adapters or ():
            coordinator.register_adapter(adapter)
    if Features.virtual_company():
        from .ai_employee_platform import build_ai_employee_services, register_ai_employee_work

        services = employee_services if employee_services is not None else build_ai_employee_services(db_manager, config=config)
        register_ai_employee_work(coordinator, services)
    if code_adapter is not None and Features.code_agent():
        coordinator.register_adapter(code_adapter, adapter_key=getattr(code_adapter, "adapter_key", "code_agent"))
    return coordinator


__all__ = ["build_agent_work_coordinator"]
