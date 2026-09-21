"""Common-runtime entry point for the existing code-oriented Agent Harness.

The implementation lives beside the harness assets so importing the common
runtime does not pull in API/server modules.  This thin module provides the
repository-level service import expected by WS02/WS04 registration code while
keeping workspace, privacy, and Enterprise sandbox behavior in one place.
"""

from __future__ import annotations

from ..agent_harness.adapter import (
    AgentHarnessExecutionAdapter,
    CodeExecutionAdapter,
    CodeAgentExecutionAdapter,
    ExecutionAdapter,
    PreparedCodeAgentRun,
    normalize_run_result,
    normalize_work_item,
)

__all__ = [
    "AgentHarnessExecutionAdapter",
    "CodeExecutionAdapter",
    "CodeAgentExecutionAdapter",
    "ExecutionAdapter",
    "PreparedCodeAgentRun",
    "normalize_run_result",
    "normalize_work_item",
]
