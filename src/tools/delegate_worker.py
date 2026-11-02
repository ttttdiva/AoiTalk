"""External worker delegation through the durable AgentDialogue service."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .core import ToolDefinition, ToolParam


WORKER_KINDS = ("codex", "antigravity", "claude", "grok", "muse")
_MAX_SCOPE_PATHS = 64
_MAX_PATH_CHARS = 512


def _json_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _actor_id() -> str:
    from ..services.turn_context import get_turn_context

    value = get_turn_context().user_id
    if not value:
        raise PermissionError("an authenticated human context is required")
    return str(value)


def _delegate_service():
    from ..services.agent_dialogue_service import (
        AgentDialogueService,
        get_agent_dialogue_dispatch_bridge,
    )
    from ..services.turn_context import get_turn_context

    config = get_turn_context().agent_execution_config
    bridge = config.get("agent_dialogue_dispatch_bridge") if isinstance(config, Mapping) else None
    if bridge is None:
        bridge = get_agent_dialogue_dispatch_bridge()
    return AgentDialogueService(
        config=getattr(bridge, "config", None),
        dispatch_bridge=bridge,
    )


def _agent_capabilities() -> set[str]:
    from ..llm.tool_policy import get_current_agent_tool_scope

    scope = get_current_agent_tool_scope()
    if not isinstance(scope, Mapping):
        return set()
    return {
        str(item).strip().casefold()
        for item in scope.get("capabilities", ())
        if str(item).strip()
    }


def _worker_kind_permitted(kind: str) -> bool:
    from ..llm.tool_policy import current_agent_worker_kinds
    from ..services.agent_delegate_policy import worker_delegate_required_capabilities

    capabilities = _agent_capabilities()
    revision_kinds = current_agent_worker_kinds()
    if revision_kinds is not None:
        return kind in set(revision_kinds)
    return any(
        required <= capabilities
        for mode in ("read", "write")
        if (required := worker_delegate_required_capabilities(kind, mode))
    )


def _effective_path_scope(workspace_scope: Any) -> dict[str, Any]:
    """Canonicalize every requested path inside the bound caller run scope."""

    from ..llm.tool_policy import get_current_agent_workspace_scope
    from ..security.agent_run_scope import AgentRunScope, get_current_run_scope
    from ..services.agent_dialogue_workspace import get_current_agent_dialogue_workspace

    if not isinstance(workspace_scope, Mapping):
        raise ValueError("workspace_scope is required")
    mode = str(workspace_scope.get("mode") or "").strip().casefold()
    raw_paths = workspace_scope.get("paths")
    if mode not in {"read", "write"} or not isinstance(raw_paths, list):
        raise ValueError("workspace_scope must contain mode=read|write and paths")
    if not raw_paths or len(raw_paths) > _MAX_SCOPE_PATHS:
        raise ValueError("workspace_scope.paths must contain 1 to 64 paths")

    run_scope = get_current_run_scope()
    if not isinstance(run_scope, AgentRunScope):
        raise PermissionError("a server-issued AgentRunScope is required")
    workspace = get_current_agent_dialogue_workspace()
    active_workspace_scope = get_current_agent_workspace_scope()
    if mode == "write":
        if run_scope.workspace_access_level != "write":
            raise PermissionError("caller AgentRunScope is read-only")
        if isinstance(active_workspace_scope, Mapping) and (
            active_workspace_scope.get("readonly") is True
            or active_workspace_scope.get("task_mode") == "read_only"
        ):
            raise PermissionError("caller Agent workspace is read-only")
        if workspace is not None:
            workspace.assert_lease_active()
            if workspace.readonly:
                raise PermissionError("caller AgentDialogue workspace is read-only")

    capabilities = _agent_capabilities()
    if mode == "write":
        required = {"workspace_write", "command_execute"}
        if not required <= capabilities:
            raise PermissionError("write delegation requires workspace_write and command_execute")
    elif "workspace_read" not in capabilities:
        raise PermissionError("read delegation requires workspace_read")

    root = Path(run_scope.canonical_root).resolve()
    workspace_root = None
    if workspace is not None:
        workspace.assert_lease_active()
        workspace_root = Path(workspace.workspace_path).resolve()
    edit_scope = (
        active_workspace_scope.get("edit_scope")
        if isinstance(active_workspace_scope, Mapping)
        else None
    )
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_paths:
        if not isinstance(raw, str):
            raise ValueError("workspace_scope.paths entries must be strings")
        value = raw.strip().replace("\\", "/")
        if (
            not value
            or len(value) > _MAX_PATH_CHARS
            or value.startswith("/")
            or re.match(r"^[A-Za-z]:", value)
            or any(char in value for char in "*?[]\x00\r\n")
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("workspace_scope.paths must be clean relative paths")
        lexical = Path(*value.split("/"))
        try:
            resolved = run_scope.resolve(lexical, base_dir=root)
            if mode == "write":
                run_scope.assert_mutation_allowed(resolved, "write")
            else:
                run_scope.assert_read_allowed(resolved)
        except Exception as exc:
            raise PermissionError("requested path is outside the caller AgentRunScope") from exc
        if not _is_within(resolved, root):
            raise PermissionError("requested path is outside the caller AgentRunScope root")
        if workspace_root is not None and not _is_within(resolved, workspace_root):
            raise PermissionError("requested path is outside the caller AgentDialogue workspace")
        if not resolved.exists() or not resolved.is_dir():
            # CLI providers receive path-scoped directories. A file path would
            # require a different mount plan; rejecting it avoids silently
            # widening access to its parent directory.
            raise ValueError("workspace_scope.paths must name existing directories")
        if mode == "write" and edit_scope is not None:
            if not _fixed_edit_scope_permits(edit_scope, resolved, root):
                raise PermissionError("requested path exceeds the caller's fixed edit scope")
        relative = resolved.relative_to(root).as_posix()
        key = relative.casefold()
        if key not in seen:
            seen.add(key)
            normalized.append(relative)
    return {"mode": mode, "root": str(root), "paths": normalized}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _fixed_edit_scope_permits(edit_scope: Any, path: Path, root: Path) -> bool:
    if bool(getattr(edit_scope, "whole", False)):
        return False
    relative = path.relative_to(root).as_posix().casefold().rstrip("/")
    protected = getattr(edit_scope, "protected", ()) or ()
    for token in protected:
        value = str(token or "").replace("\\", "/").casefold()
        value = value.rstrip("/")
        if value and (relative == value or relative.startswith(value + "/")):
            return False
    if not bool(getattr(edit_scope, "limited", False)):
        return True
    editable = getattr(edit_scope, "editable", ()) or ()
    for token in editable:
        value = str(token or "").replace("\\", "/").casefold()
        directory = value.endswith("/")
        value = value.rstrip("/")
        if not value:
            continue
        if relative == value or (directory and relative.startswith(value + "/")):
            return True
    return False


def _bounded_budgets(value: Any) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("budgets must be an object")
    allowed = {"max_tokens": 1_000_000, "max_cost_micros": 1_000_000_000_000, "max_attempts": 16}
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError("budgets contains unsupported fields")
    result: dict[str, int] = {}
    for key, maximum in allowed.items():
        item = value.get(key)
        if item is None:
            continue
        if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= maximum:
            raise ValueError(f"budgets.{key} is invalid")
        result[key] = item
    return result


async def _delegate_worker(
    kind: str,
    instruction: str,
    workspace_scope: dict[str, Any],
    model: str | None = None,
    effort: str | None = None,
    timeout_seconds: int | None = None,
    budgets: dict[str, Any] | None = None,
) -> str:
    normalized_kind = str(kind or "").strip().casefold()
    if normalized_kind not in WORKER_KINDS:
        return _json_result({"success": False, "error": "worker_kind_invalid"})
    from ..services.turn_context import get_turn_context

    context = get_turn_context()
    if not str(context.agent_id or "").strip() or not str(context.agent_revision_id or "").strip():
        return _json_result({"success": False, "error": "source_agent_required"})
    if not _worker_kind_permitted(normalized_kind):
        return _json_result({"success": False, "error": "worker_kind_not_permitted"})
    try:
        normalized_scope = _effective_path_scope(workspace_scope)
        normalized_budgets = _bounded_budgets(budgets)
        timeout = None
        if timeout_seconds is not None:
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, int)
                or not 1 <= timeout_seconds <= 3600
            ):
                raise ValueError("timeout_seconds is invalid")
            timeout = timeout_seconds
        safe_model = str(model).strip()[:160] if model is not None else None
        safe_effort = str(effort).strip()[:64] if effort is not None else None
        if not str(instruction or "").strip() or len(str(instruction)) > 30_000:
            raise ValueError("instruction is required and must be bounded")
        if normalized_kind == "muse" and (safe_model is not None or safe_effort is not None):
            raise ValueError("muse does not accept model or effort")
        service = _delegate_service()
        result = await service.delegate_worker_work(
            actor_user_id=_actor_id(),
            actor_role=None,
            kind=normalized_kind,
            instruction=str(instruction),
            workspace_scope=normalized_scope,
            model=safe_model or None,
            effort=safe_effort or None,
            timeout_seconds=timeout,
            budgets=normalized_budgets,
            project_id=context.project_id,
            context_project_hint_id=context.context_project_hint_id,
        )
        return _json_result(result)
    except Exception as exc:  # noqa: BLE001 - only safe codes cross the tool boundary
        code = getattr(exc, "code", None)
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", code):
            code = "worker_delegate_failed"
        return _json_result({"success": False, "error": code})


def build_delegate_worker_tool(
    allowed_kinds: Sequence[str] | None = None,
) -> ToolDefinition | None:
    """Build the request-specialized external worker tool definition."""

    if allowed_kinds is None:
        # Registry construction can happen outside a request. In that case
        # keep a bounded base definition and let request exposure specialize
        # it. If an Agent scope is active, publish only its legacy-compatible
        # capability-backed kinds.
        try:
            from ..llm.tool_policy import get_current_agent_tool_scope

            active_scope = get_current_agent_tool_scope()
        except Exception:
            active_scope = None
        if isinstance(active_scope, Mapping):
            capabilities = {
                str(item).strip().casefold()
                for item in active_scope.get("capabilities", ())
                if str(item).strip()
            }
            from ..services.agent_delegate_policy import worker_delegate_required_capabilities

            kinds = [
                kind
                for kind in WORKER_KINDS
                if any(
                    required <= capabilities
                    for mode in ("read", "write")
                    if (required := worker_delegate_required_capabilities(kind, mode))
                )
            ]
            if not kinds:
                return None
        else:
            kinds = list(WORKER_KINDS)
    else:
        requested = {str(item).strip().casefold() for item in allowed_kinds}
        kinds = [kind for kind in WORKER_KINDS if kind in requested]
        if not kinds:
            return None
    kind_text = "|".join(kinds)
    return ToolDefinition(
        name="delegate_worker",
        description=(
            f"Start one bounded external worker task using only an allowed kind ({kind_text}). "
            "Choose only a kind shown in the schema. The worker gets exactly the requested "
            "workspace paths and read/write mode; it cannot delegate again. Returns a dialogue_id "
            "for status and control tools. Use read mode unless file changes are required. "
            "max_tokens and max_cost_micros are settled from the provider receipt after execution; "
            "an uninterruptible overrun is recorded post hoc. max_attempts and timeout_seconds "
            "are enforced during execution."
        ),
        function=_delegate_worker,
        parameters=[
            ToolParam("kind", "string", description="External worker kind allowed for this AgentRevision.", enum=kinds),
            ToolParam("instruction", "string", description="Bounded task instructions for the worker."),
            ToolParam(
                "workspace_scope",
                "object",
                description="Exact workspace access requested by this task.",
                schema={
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["read", "write"]},
                        "paths": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": _MAX_SCOPE_PATHS,
                            "items": {"type": "string", "maxLength": _MAX_PATH_CHARS},
                            "description": "Workspace-relative directory paths; paths are checked against the caller's live AgentRunScope.",
                        },
                    },
                    "required": ["mode", "paths"],
                    "additionalProperties": False,
                },
            ),
            ToolParam("model", "string", required=False, description="Optional model override; must match the pinned worker route."),
            ToolParam("effort", "string", required=False, description="Optional reasoning effort supported by the pinned route."),
            ToolParam(
                "timeout_seconds",
                "integer",
                required=False,
                description="Maximum worker duration in seconds (1 to 3600).",
                schema={"type": "integer", "minimum": 1, "maximum": 3600},
            ),
            ToolParam(
                "budgets",
                "object",
                required=False,
                description=(
                    "Optional worker limits. max_attempts and timeout_seconds are enforced during "
                    "execution; token/cost ceilings are settled from the provider receipt and any "
                    "uninterruptible overrun is recorded post hoc."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "max_tokens": {"type": "integer", "minimum": 1, "maximum": 1_000_000},
                        "max_cost_micros": {"type": "integer", "minimum": 1, "maximum": 1_000_000_000_000},
                        "max_attempts": {"type": "integer", "minimum": 1, "maximum": 16},
                    },
                    "additionalProperties": False,
                },
            ),
        ],
        is_async=True,
        risk="medium",
        side_effect="agent_dialogue",
        requires_approval=False,
        timeout_seconds=30,
        supports_parallel=False,
        owner="aoi_tools",
    )


__all__ = ["WORKER_KINDS", "build_delegate_worker_tool"]
