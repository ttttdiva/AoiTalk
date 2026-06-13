"""Repository-owned workflow prompt loading for the agent harness."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..services.workflow_loader import WorkflowDefinition, load_workflow_from_md


class WorkflowRenderError(ValueError):
    """Raised when the workflow prompt references an unknown variable."""


@dataclass(frozen=True)
class HarnessWorkflow:
    path: Path
    prompt_template: str
    metadata: dict[str, Any] = field(default_factory=dict)


DEFAULT_PROMPT_TEMPLATE = """You are working on an AoiTalk task.

Identifier: {{ issue.identifier }}
Title: {{ issue.title }}
State: {{ issue.state }}

Description:
{{ issue.description }}
"""


def load_harness_workflow(path: Path) -> HarnessWorkflow:
    """Load a Markdown workflow using the existing AoiTalk workflow loader."""
    if not path.exists():
        return HarnessWorkflow(path=path, prompt_template=DEFAULT_PROMPT_TEMPLATE)

    workflow = load_workflow_from_md(path)
    if workflow is None:
        raise WorkflowRenderError(f"Failed to load workflow: {path}")

    metadata = getattr(workflow, "metadata", {}) or _workflow_metadata_fallback(workflow)
    return HarnessWorkflow(
        path=path,
        prompt_template=(workflow.content or DEFAULT_PROMPT_TEMPLATE).strip(),
        metadata=dict(metadata),
    )


def render_prompt(
    workflow: HarnessWorkflow,
    *,
    issue: Any,
    attempt: int | None = None,
) -> str:
    """Render a strict, small Liquid-like variable subset used by the harness."""

    issue_dict = issue.to_prompt_dict() if hasattr(issue, "to_prompt_dict") else dict(issue)
    context = {"issue": issue_dict, "attempt": attempt}

    def replace(match: re.Match[str]) -> str:
        expression = match.group(1).strip()
        value = _resolve_expression(expression, context)
        if value is None:
            return ""
        if isinstance(value, (list, dict)):
            return str(value)
        return str(value)

    return re.sub(r"{{\s*([^}]+?)\s*}}", replace, workflow.prompt_template).strip()


def _resolve_expression(expression: str, context: dict[str, Any]) -> Any:
    parts = [part.strip() for part in expression.split(".")]
    if not parts or parts[0] not in context:
        raise WorkflowRenderError(f"Unknown workflow variable: {expression}")
    value: Any = context[parts[0]]
    for part in parts[1:]:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise WorkflowRenderError(f"Unknown workflow variable: {expression}")
    return value


def _workflow_metadata_fallback(workflow: WorkflowDefinition) -> dict[str, Any]:
    return {
        "name": workflow.name,
        "description": workflow.description,
        "trigger": workflow.trigger,
        "schedule": workflow.schedule,
        "enabled": workflow.enabled,
    }
