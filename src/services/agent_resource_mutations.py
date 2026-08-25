"""Normalize durable Agent Run mutations into chat-facing resource cards."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any
from uuid import UUID


TASK_MUTATION_OPERATIONS = {
    "create_task": "created",
    "update_task": "updated",
    "assign_task": "updated",
    "schedule_task": "updated",
    "delete_task": "deleted",
}

DOCS_MUTATION_OPERATIONS = {
    "docs_ensure_inbox": "created",
    "docs_attach_workspace_file": "created",
    "docs_place_workspace_file": "created",
    "docs_create_nodes": "created",
    "docs_update_node": "updated",
    "inbox_update_item": "updated",
    "docs_move_node": "moved",
    "docs_archive_node": "archived",
}

MEMORY_MUTATION_OPERATIONS = {
    "memory_upsert": "created",
    "memory_update": "updated",
    "memory_forget": "forgotten",
    "memory_move_scope": "moved",
    "memory_promote_to_project_information": "promoted",
}


def _field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _first_text(*values: Any) -> str | None:
    for value in values:
        normalized = _text(value)
        if normalized:
            return normalized
    return None


def _json_result(value: Any) -> Any:
    if isinstance(value, (Mapping, list)):
        return value
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return _text(value)


def _tool_name(value: Any) -> str:
    raw = _text(_field(value, "tool_name")) or ""
    return raw.rsplit(".", 1)[-1].strip().lower()


def _is_failed_call(call: Any, result: Any) -> bool:
    # A tool wrapper may return {"success": false} instead of raising. Treat
    # that result as failed even when the provider marked the function call as
    # completed normally.
    return (
        _field(call, "success") is False
        or (
            isinstance(result, Mapping)
            and result.get("success") is False
        )
    )


def _base_mutation(
    *,
    resource_type: str,
    resource_id: Any,
    title: Any,
    operation: str,
    arguments: Mapping[str, Any],
    result: Mapping[str, Any],
    call: Any,
) -> dict[str, Any] | None:
    normalized_id = _text(resource_id)
    if not normalized_id:
        return None

    task = result.get("task") if isinstance(result.get("task"), Mapping) else {}
    source = {**task, **result}
    normalized_title = _first_text(title, source.get("title"), arguments.get("title"))
    project_name = _first_text(
        source.get("project_name"),
        source.get("project"),
    )
    if not project_name:
        project_argument = _text(arguments.get("project"))
        # A UUID is useful for authorization but not as a compact project label.
        if project_argument:
            try:
                UUID(project_argument)
            except (TypeError, ValueError):
                if len(project_argument) < 80:
                    project_name = project_argument

    occurred_at = _iso(
        _field(call, "ended_at")
        or _field(call, "created_at")
        or _field(call, "started_at")
    )
    mutation: dict[str, Any] = {
        "resource_type": resource_type,
        "resource_id": normalized_id,
        "title": normalized_title,
        "operation": operation,
        "success": True,
        "project_name": project_name,
        "start_at": _first_text(source.get("start_at"), arguments.get("start_at")),
        "due_date": _first_text(source.get("due_date"), arguments.get("due_date")),
        "end_at": _first_text(source.get("end_at"), arguments.get("end_at")),
        "occurred_at": occurred_at,
    }
    if resource_type == "task":
        all_day = source.get("all_day", arguments.get("all_day"))
        if isinstance(all_day, bool):
            mutation["all_day"] = all_day
    else:
        mutation["updated_at"] = _first_text(
            source.get("updated_at"),
            source.get("created_at"),
        )
    return mutation


def _mutations_for_call(call: Any) -> list[dict[str, Any]]:
    tool_name = _tool_name(call)
    operation = TASK_MUTATION_OPERATIONS.get(tool_name)
    resource_type = "task" if operation else None
    if operation is None:
        operation = DOCS_MUTATION_OPERATIONS.get(tool_name)
        resource_type = "docs_node" if operation else None
    if operation is None:
        operation = MEMORY_MUTATION_OPERATIONS.get(tool_name)
        resource_type = "memory" if operation else None
    if operation is None or resource_type is None:
        return []

    # mutation_confirmed is deliberately strict: cards must be backed by the
    # same confirmation flag used by the Agent Run audit trail.
    if _field(call, "mutation_confirmed") is not True:
        return []
    result_value = _json_result(_field(call, "result"))
    if _is_failed_call(call, result_value):
        return []
    if tool_name != "docs_create_nodes" and not isinstance(result_value, Mapping):
        return []
    if (
        tool_name in {"docs_ensure_inbox", "docs_attach_workspace_file"}
        and isinstance(result_value, Mapping)
        and result_value.get("created") is not True
    ):
        return []
    if tool_name == "docs_place_workspace_file":
        docs_result = (
            result_value.get("docs")
            if isinstance(result_value, Mapping)
            else None
        )
        if (
            not isinstance(docs_result, Mapping)
            or docs_result.get("success") is False
            or docs_result.get("created") is not True
        ):
            return []
        result_value = docs_result
    arguments = _field(call, "arguments")
    arguments = arguments if isinstance(arguments, Mapping) else {}

    result_items: list[Mapping[str, Any]]
    if tool_name == "docs_create_nodes":
        raw_items = (
            result_value.get("created")
            if isinstance(result_value, Mapping)
            else None
        )
        if not isinstance(raw_items, list):
            return []
        result_items = [item for item in raw_items if isinstance(item, Mapping)]
    else:
        result_items = [
            result_value if isinstance(result_value, Mapping) else {}
        ]

    mutations: list[dict[str, Any]] = []
    for item in result_items:
        task = item.get("task") if isinstance(item.get("task"), Mapping) else {}
        source = {**task, **item}
        if resource_type == "task":
            resource_id = _first_text(
                source.get("id"),
                source.get("task_id"),
                arguments.get("task_id"),
            )
        else:
            resource_id = _first_text(
                source.get("id"),
                source.get("node_id"),
                source.get("memory_id"),
                arguments.get("node_id"),
                arguments.get("memory_id"),
            )
        mutation = _base_mutation(
            resource_type=resource_type,
            resource_id=resource_id,
            title=source.get("title"),
            operation=operation,
            arguments=arguments,
            result=source,
            call=call,
        )
        if mutation is not None:
            mutations.append(mutation)
    return mutations


def _merge_mutation(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(previous)
    for key, value in current.items():
        if value is not None and value != "":
            merged[key] = value
    # The last successful operation describes the final durable state.
    merged["operation"] = current["operation"]
    merged["success"] = True
    return merged


def build_agent_resource_mutations(
    tool_calls: Iterable[Any],
) -> list[dict[str, Any]]:
    """Return compact, deduplicated resource mutations for a run.

    The input is the durable AgentRunToolCall relationship (or compatible
    dictionaries in tests). No resource is fetched here; all display fields
    come from the already-recorded tool result/arguments.
    """

    by_resource: dict[tuple[str, str], dict[str, Any]] = {}
    for call in tool_calls or []:
        for mutation in _mutations_for_call(call):
            key = (mutation["resource_type"], mutation["resource_id"])
            previous = by_resource.get(key)
            by_resource[key] = (
                _merge_mutation(previous, mutation)
                if previous is not None
                else mutation
            )

    mutations = list(by_resource.values())
    for mutation in mutations:
        if not _text(mutation.get("title")):
            mutation["title"] = "名称未取得"
    return mutations


__all__ = ["build_agent_resource_mutations"]
