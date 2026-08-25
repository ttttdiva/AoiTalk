"""Parent lifecycle helpers for run-scoped background processes.

The OS tool registry owns process handles and scope filtering.  This small
service keeps lifecycle/publication callers out of the LLM-facing tools module:
the parent passes its immutable :class:`AgentRunScope` when a run ends, and a
publication adapter can perform a read-only drained preflight before invoking
its Git transport.
"""

from __future__ import annotations

from typing import Any

from ..security.agent_run_scope import AgentRunScope
from ..tools.os_operations.background_jobs import (
    BackgroundJobError,
    BackgroundJobRegistry,
    get_background_job_registry,
)


def _registry(registry: BackgroundJobRegistry | None) -> BackgroundJobRegistry:
    return registry if registry is not None else get_background_job_registry()


def close_agent_run_background_jobs(
    scope: AgentRunScope,
    *,
    registry: BackgroundJobRegistry | None = None,
    remove: bool = False,
) -> list[dict[str, Any]]:
    """Terminate jobs owned by *scope* at the parent run lifecycle boundary."""

    if not isinstance(scope, AgentRunScope):
        raise TypeError("scope must be an AgentRunScope")
    return _registry(registry).close_scoped_jobs(scope, remove=remove)


def preflight_agent_run_background_jobs(
    scope: AgentRunScope,
    *,
    registry: BackgroundJobRegistry | None = None,
) -> dict[str, Any]:
    """Return a publication-safe decision for one run's active jobs."""

    if not isinstance(scope, AgentRunScope):
        raise TypeError("scope must be an AgentRunScope")
    return _registry(registry).preflight_scoped_jobs(scope)


def assert_agent_run_background_jobs_drained(
    scope: AgentRunScope,
    *,
    registry: BackgroundJobRegistry | None = None,
) -> dict[str, Any]:
    """Raise ``BackgroundJobError`` until all owned jobs have stopped."""

    if not isinstance(scope, AgentRunScope):
        raise TypeError("scope must be an AgentRunScope")
    return _registry(registry).assert_no_active_scoped_jobs(scope)


def finish_agent_run_background_jobs(
    scope: AgentRunScope,
    *,
    registry: BackgroundJobRegistry | None = None,
    remove: bool = False,
) -> dict[str, Any]:
    """Parent run-end hook: close owned jobs, then verify the drain."""

    closed = close_agent_run_background_jobs(scope, registry=registry, remove=remove)
    preflight = assert_agent_run_background_jobs_drained(scope, registry=registry)
    return {"closed": closed, **preflight}


# Names used by parent/controller integrations that describe the same seam.
close_scoped_jobs = close_agent_run_background_jobs
close_scoped_jobs_for_run = close_agent_run_background_jobs
preflight_scoped_jobs = preflight_agent_run_background_jobs
preflight_before_publication = preflight_agent_run_background_jobs
assert_scoped_jobs_drained = assert_agent_run_background_jobs_drained
finish_run_background_jobs = finish_agent_run_background_jobs


__all__ = [
    "BackgroundJobError",
    "assert_agent_run_background_jobs_drained",
    "assert_scoped_jobs_drained",
    "close_agent_run_background_jobs",
    "close_scoped_jobs",
    "close_scoped_jobs_for_run",
    "finish_agent_run_background_jobs",
    "finish_run_background_jobs",
    "preflight_agent_run_background_jobs",
    "preflight_scoped_jobs",
    "preflight_before_publication",
]
