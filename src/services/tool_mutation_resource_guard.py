"""Per-tool shared resource fencing for workspace and Project mutations.

The ordinary Chat turn may hold a shared Project lease for its whole lifetime,
but a natural-language turn is also allowed to select a mutation only after
the provider has chosen a concrete ``ToolDefinition``.  This module closes
that smaller window: the canonical Project/workspace key is resolved from the
server-bound turn scope, and the shared Agent Dialogue lease is held around
the callable itself.

The module deliberately does not make a raw tool argument a resource
authority.  ``project_id`` is accepted only when it agrees with the current
server-bound Project; filesystem paths are accepted as a target identity only
inside an already-issued ``AgentRunScope``.  Domain services retain their own
ACL and Project locks inside the callable.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import hashlib
import inspect
import os
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID


_CANONICAL_RESOURCE_KEY = re.compile(
    r"^(?:project:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}|workspace:[0-9a-f]{64})$",
    re.IGNORECASE,
)

# These names are kept here as a narrow, dependency-free fallback for tools
# created outside ``src.llm.tool_policy``.  The policy module remains the
# source of the complete mutation classification when it is importable.
_PROJECT_MUTATION_NAMES = frozenset(
    {
        "organize_project_information_from_folder",
        "patch_project_information_doc",
        "attach_project_information_reference",
        "upsert_project_qa_entry",
        "archive_project_qa_entry",
        "configure_project_management_files",
        "create_record_table",
        "append_record_rows",
        "update_record_row",
        "delete_record_rows",
        "delete_record_table",
        "create_task",
        "update_task",
        "delete_task",
        "assign_task",
        "schedule_task",
        "start_timer",
        "stop_timer",
        "log_time",
        "sync_issue_table",
        "sync_wbs_tasks",
    }
)
_DOCS_MUTATION_NAMES = frozenset(
    {
        "docs_attach_workspace_file",
        "docs_place_workspace_file",
        "docs_create_nodes",
        "docs_update_node",
        "docs_mutate",
        "inbox_update_item",
        "docs_move_node",
        "docs_archive_node",
    }
)
_FILESYSTEM_MUTATION_NAMES = frozenset(
    {
        "execute_command",
        "create_workspace_directory",
        "upload_workspace_file",
        "delete_workspace_item",
        "move_workspace_item",
        "copy_workspace_item",
        "upload_user_file",
        "delete_user_file",
        "create_file",
        "write_file",
        "delete_file",
        "append_to_file",
        "edit_file",
        "insert_to_file",
        "undo_edit",
    }
)
_WORKSPACE_MUTATION_NAMES = (
    _PROJECT_MUTATION_NAMES | _DOCS_MUTATION_NAMES | _FILESYSTEM_MUTATION_NAMES
)

# AgentDialogue delivery and external-action approval use their own durable
# conversation/approval fences.  They are state-changing tools, but they do
# not mutate the Project/workspace resource protected here.
_DIALOGUE_TOOL_NAMES = frozenset(
    {
        "list_agent_workers",
        "delegate_agent_work",
        "get_agent_work_status",
        "send_agent_work_message",
        "answer_agent_work_question",
        "escalate_agent_work_question",
        "stop_agent_work",
        "resume_agent_work",
    }
)
_EXTERNAL_TOOL_NAMES = frozenset(
    {
        "propose_external_action",
        "execute_external_action",
        "send_message",
        "send_email",
        "send_sms",
        "place_call",
        "transfer_call",
        "telephony_control",
    }
)
_RAW_RESOURCE_ARGUMENTS = frozenset(
    {
        "resource_key",
        "workspace_resource_key",
        "resource_hash",
        "workspace_hash",
        "lease_token",
    }
)
_PROJECT_ARGUMENTS = (
    "project_id",
    "project",
    "project_ref",
    "target_project_id",
)
_TASK_ARGUMENTS = ("task_id", "task", "target_task_id")
_RESOURCE_ID_ARGUMENTS = (
    "doc_id",
    "document_id",
    "node_id",
    "knowledge_node_id",
    "docs_node_id",
    "attachment_id",
    "file_id",
    "asset_id",
    "media_id",
    "artifact_id",
)
_PATH_ARGUMENTS = (
    "path",
    "file_path",
    "workspace_path",
    "working_directory",
    "cwd",
    "src",
    "dest",
    "source",
    "destination",
    "target_path",
)

_active_helper_leases: contextvars.ContextVar[
    dict[str, "_HelperLeaseState"] | None
] = contextvars.ContextVar(
    "tool_mutation_resource_guard_active_leases",
    default=None,
)
_sync_lease_context: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "tool_mutation_resource_guard_sync_lease_context",
    default=False,
)


class ToolMutationResourceDenied(PermissionError):
    """The concrete mutation has no server-authorized canonical resource."""


class ToolMutationResourceBusy(ToolMutationResourceDenied):
    """The canonical resource is currently leased by another turn."""


@dataclass(frozen=True, slots=True)
class CanonicalMutationResource:
    """One server-resolved shared resource identity."""

    key: str
    kind: str
    project_id: str | None = None
    workspace_root: Path | None = None


@dataclass(frozen=True, slots=True)
class ActiveLeaseProof:
    """Evidence that this execution may reuse an already-held resource lease."""

    resource_key: str
    source: str


@dataclass(frozen=True, slots=True)
class TaskWorkspaceOwnershipProof:
    """Evidence of canonical *task ownership* of a workspace key.

    Deliberately not an :class:`ActiveLeaseProof`: it never claims that an
    exclusive cwd lock is held, and lease-reuse paths must not accept it.
    """

    resource_key: str
    task_id: str
    run_id: str


@dataclass(slots=True)
class _HelperLeaseState:
    """Shared mutable proof copied by child Tasks through ContextVar state."""

    resource_key: str
    run_id: str | None
    owner_token: object
    active: bool = True

    def live_for(self, resource_key: str, current_run_id: str | None) -> bool:
        if not self.active or self.owner_token is None:
            return False
        if self.resource_key != resource_key:
            return False
        if self.run_id is not None:
            return bool(current_run_id and current_run_id == self.run_id)
        return True


def _tool_name(tool_definition: Any) -> str:
    value = tool_definition.get("name") if isinstance(tool_definition, Mapping) else getattr(tool_definition, "name", "")
    return str(value or "").rsplit(".", 1)[-1].strip().casefold()


def _metadata(tool_definition: Any, name: str, default: Any = None) -> Any:
    if isinstance(tool_definition, Mapping):
        return tool_definition.get(name, default)
    return getattr(tool_definition, name, default)


def _normalized_key(value: Any) -> str:
    key = str(value or "").strip().casefold()
    if not _CANONICAL_RESOURCE_KEY.fullmatch(key):
        raise ToolMutationResourceDenied("resource lease key is not canonical")
    return key


def _uuid(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return str(UUID(str(value).strip()))
    except (TypeError, ValueError, AttributeError):
        return None


def _id(value: Any) -> str:
    return str(value or "").strip().casefold()


def _run_id(context: Any) -> str | None:
    try:
        from .agent_run_service import get_current_agent_run_id

        value = get_current_agent_run_id()
        return str(value).strip() if value else None
    except Exception:
        return None


def _current_run_scope_id() -> str | None:
    """Return the lower native scope run id when one is server-bound."""

    try:
        from ..security.agent_run_scope import get_current_run_scope

        scope = get_current_run_scope()
        value = getattr(scope, "run_id", None) if scope is not None else None
        return str(value).strip() if value else None
    except Exception:
        return None


def _read_only_command_capability_proof(
    tool_definition: Any,
    context: Any,
) -> bool:
    """Accept only server-owned metadata for a read-only shell exception.

    Command text is deliberately absent from this decision.  The registry may
    attach the marker to a retained definition, or a trusted Agent execution
    config may carry it; neither value can be supplied through model tool
    arguments.
    """

    availability = _metadata(tool_definition, "availability")
    if isinstance(availability, Mapping) and availability.get(
        "read_only_command_capability"
    ) is True:
        return True
    config = getattr(context, "agent_execution_config", None)
    return isinstance(config, Mapping) and config.get(
        "read_only_command_capability"
    ) is True


def _assert_agent_workspace_run_identity(
    workspace: Any | None,
    workspace_scope: Mapping[str, Any] | None,
    context: Any,
) -> None:
    """Require one live run identity across workspace, tool scope and turn."""

    if workspace is None and workspace_scope is None:
        return
    workspace_run = _id(getattr(workspace, "run_id", None)) if workspace is not None else ""
    scope_run = _id(workspace_scope.get("run_id")) if workspace_scope is not None else ""
    current_run = _id(_run_id(context))
    if not workspace_run or not scope_run or not current_run:
        raise ToolMutationResourceDenied("Agent workspace run identity is incomplete")
    if not (workspace_run == scope_run == current_run):
        raise ToolMutationResourceDenied("Agent workspace run identity does not match the current run")
    native_scope_run = _id(_current_run_scope_id())
    if native_scope_run and native_scope_run != current_run:
        raise ToolMutationResourceDenied("Agent native run scope does not match the current run")


def _is_workspace_mutation(
    tool_definition: Any,
    arguments: Mapping[str, Any] | None = None,
) -> bool:
    """Classify only mutations that share Project/workspace state."""

    name = _tool_name(tool_definition)
    if name in _DIALOGUE_TOOL_NAMES or name in _EXTERNAL_TOOL_NAMES:
        return False
    side_effect = str(_metadata(tool_definition, "side_effect", "") or "").strip().casefold()
    owner = str(_metadata(tool_definition, "owner", "") or "").strip().casefold()
    if side_effect in {
        "agent_dialogue",
        "dialogue",
        "message",
        "external",
        "external_action",
        "network",
        "approval",
        "telephony",
    }:
        return False
    if owner in {
        "agent_dialogue",
        "dialogue",
        "external_action",
        "telephony",
        "operations",
        "media",
        "spotify",
        "browser_agent",
        "computer_use",
        "cloud_advisor",
        "search",
        "mcp",
    }:
        return False
    if name == "execute_command":
        # Shell redirection, interpreter snippets, and provider-specific
        # command syntax cannot be classified safely from raw text.  Every
        # command therefore uses the mutation resource fence; a server-owned
        # read-only command proof is checked later with the full turn context.
        return True
    if name in _WORKSPACE_MUTATION_NAMES:
        return True
    if owner in {"filesystem", "docs", "project_management"} and side_effect not in {
        "",
        "none",
        "read",
        "readonly",
        "read_only",
        "observe",
        "query",
    }:
        return True
    side_effect_tokens = {
        part.strip()
        for part in re.split(r"[,/\s]+", side_effect)
        if part.strip()
    }
    if side_effect_tokens.intersection(
        {
            "write",
            "writes",
            "mutation",
            "mutating",
            "database",
            "db",
            "project",
            "docs",
            "filesystem",
            "state",
            "process",
            "delete",
        }
    ):
        try:
            from ..llm.tool_policy import tool_definition_is_mutating

            return bool(tool_definition_is_mutating(name, tool_definition=tool_definition))
        except Exception:
            return True
    return False


def is_workspace_mutation(
    tool_definition: Any,
    arguments: Mapping[str, Any] | None = None,
) -> bool:
    """Public classification used by ``ToolDefinition`` and focused tests."""

    return _is_workspace_mutation(tool_definition, arguments)


def _is_filesystem_mutation(tool_definition: Any) -> bool:
    name = _tool_name(tool_definition)
    return name in _FILESYSTEM_MUTATION_NAMES


def _resource_key_for_workspace_root(root: Path) -> str:
    canonical = Path(root).resolve()
    material = os.path.normcase(os.path.normpath(str(canonical)))
    return f"workspace:{hashlib.sha256(material.encode('utf-8', 'surrogatepass')).hexdigest()}"


def _resource_key_for_user_namespace(user_id: str) -> str:
    """Return a canonical lease key for projectless owner-scoped mutations."""

    material = f"aoitalk-user-namespace:{str(user_id).casefold()}"
    return f"workspace:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _current_agent_workspace() -> Any | None:
    try:
        from .agent_dialogue_workspace import get_current_agent_dialogue_workspace

        return get_current_agent_dialogue_workspace()
    except Exception:
        return None


def _current_workspace_scope() -> Mapping[str, Any] | None:
    try:
        from ..llm.tool_policy import get_current_agent_workspace_scope

        value = get_current_agent_workspace_scope()
        return value if isinstance(value, Mapping) else None
    except Exception:
        return None


def _current_shared_scope() -> Mapping[str, Any] | None:
    try:
        from ..llm.tool_policy import get_current_shared_resource_scope

        value = get_current_shared_resource_scope()
        return value if isinstance(value, Mapping) else None
    except Exception:
        return None


def _current_turn() -> Any:
    try:
        from .turn_context import get_turn_context

        return get_turn_context()
    except Exception:
        return None


def _validate_target_ids(arguments: Mapping[str, Any], context: Any) -> None:
    """Reject model-supplied aliases that escape the current turn scope."""

    current_task = _id(getattr(context, "task_id", None))
    for key in _TASK_ARGUMENTS:
        supplied = arguments.get(key)
        if supplied in (None, ""):
            continue
        if current_task and _id(supplied) != current_task:
            raise ToolMutationResourceDenied("tool Task target is outside the current turn scope")

    # A resource/document UUID is still checked by the concrete service ACL.
    # When the turn has an explicit server-validated mention, however, do not
    # let a different model-provided ID replace that reference.
    try:
        from .turn_context import is_explicit_reference_in_turn
    except Exception:
        is_explicit_reference_in_turn = None
    for key in _RESOURCE_ID_ARGUMENTS:
        supplied = arguments.get(key)
        if supplied in (None, "") or not callable(is_explicit_reference_in_turn):
            continue
        aliases = {
            "doc_id": ("docs", "doc", "node"),
            "document_id": ("docs_document", "docs", "node"),
            "node_id": ("docs", "node", "knowledge"),
            "knowledge_node_id": ("docs", "node", "knowledge"),
            "docs_node_id": ("docs", "node", "knowledge"),
            "file_id": ("file", "workspace_file"),
            "attachment_id": ("attachment", "file"),
            "asset_id": ("asset", "media_asset", "media"),
            "media_id": ("asset", "media_asset", "media"),
            "artifact_id": ("artifact", "agent_artifact"),
        }[key]
        values = supplied if isinstance(supplied, (list, tuple, set, frozenset)) else (supplied,)
        for value in values:
            if not any(is_explicit_reference_in_turn(alias, str(value), context=context) for alias in aliases):
                # No explicit reference list means the service's own ACL is
                # still the canonical resolver.  A populated list is a hard
                # server boundary and must match it exactly.
                refs = getattr(context, "explicit_references", ()) or ()
                if refs:
                    raise ToolMutationResourceDenied("tool resource target was not authorized for this turn")


def _validate_scope_paths(tool_definition: Any, arguments: Mapping[str, Any], scope: Any) -> None:
    """Check every supplied filesystem target against the issued run scope."""

    paths = [(key, arguments.get(key)) for key in _PATH_ARGUMENTS if arguments.get(key) not in (None, "")]
    if not paths:
        return
    name = _tool_name(tool_definition)
    for key, raw in paths:
        # Path lists are not an authority; each element is checked individually
        # if a tool exposes one as a convenience.
        values = raw if isinstance(raw, (list, tuple, set, frozenset)) else (raw,)
        for value in values:
            if not isinstance(value, (str, os.PathLike)):
                raise ToolMutationResourceDenied("filesystem mutation path is invalid")
            if name == "execute_command" and key in {"working_directory", "cwd"}:
                decision = scope.check_command_cwd(value)
            elif name in {"move_workspace_item", "copy_workspace_item"} and key == "src":
                decision = scope.check_delete(value) if name == "move_workspace_item" else scope.check_read(value)
            else:
                decision = scope.check_mutation(value)
            if not bool(getattr(decision, "allowed", False)):
                raise ToolMutationResourceDenied("filesystem target is outside the current run scope")


def _canonical_project_from_turn(
    context: Any,
    arguments: Mapping[str, Any],
    shared: Mapping[str, Any] | None,
    *,
    bound_project_id: str | None = None,
) -> str | None:
    current = _uuid(getattr(context, "project_id", None))
    shared_project = _uuid(shared.get("project_id")) if shared else None
    if shared and shared.get("project_id") not in (None, "") and shared_project is None:
        raise ToolMutationResourceDenied("shared Project scope is invalid")
    supplied_values = [arguments.get(key) for key in _PROJECT_ARGUMENTS if arguments.get(key) not in (None, "")]
    supplied = None
    for value in supplied_values:
        parsed = _uuid(value)
        if parsed is None:
            raise ToolMutationResourceDenied("tool Project target is not canonical")
        if supplied is not None and parsed != supplied:
            raise ToolMutationResourceDenied("tool Project targets conflict")
        supplied = parsed
    if supplied is not None:
        if current is not None and supplied != current:
            raise ToolMutationResourceDenied("tool Project target is outside the current turn scope")
        if shared_project is not None and supplied != shared_project:
            raise ToolMutationResourceDenied("tool Project target is outside the active shared lease")
        if bound_project_id is not None and supplied != bound_project_id:
            raise ToolMutationResourceDenied("tool Project target is outside the active Agent workspace")
        if current is None and shared_project is None and bound_project_id is None:
            # A model argument may select a row, but it cannot create the
            # server authority that would make that row a writable resource.
            raise ToolMutationResourceDenied("tool Project target lacks a server-bound scope")
        return supplied
    if current is not None:
        if shared_project is not None and shared_project != current:
            raise ToolMutationResourceDenied("current Project differs from active shared lease")
        if bound_project_id is not None and bound_project_id != current:
            raise ToolMutationResourceDenied("current Project differs from active Agent workspace")
        return current
    if shared_project is not None:
        if bound_project_id is not None and bound_project_id != shared_project:
            raise ToolMutationResourceDenied("shared Project differs from active Agent workspace")
        return shared_project
    return bound_project_id


def canonical_resource_for_tool(
    tool_definition: Any,
    arguments: Mapping[str, Any] | None = None,
    *,
    context: Any | None = None,
) -> CanonicalMutationResource | None:
    """Resolve the canonical Project/workspace resource for one mutation.

    ``None`` means that this is a read-only/non-workspace mutation or a
    legacy unscoped direct call.  An authenticated scoped mutation without a
    canonical resource raises ``ToolMutationResourceDenied``.
    """

    if not _is_workspace_mutation(tool_definition, arguments):
        return None
    args = dict(arguments or {})
    context = context or _current_turn()
    if context is None:
        return None
    if _tool_name(tool_definition) == "execute_command" and _read_only_command_capability_proof(
        tool_definition,
        context,
    ):
        return None

    for key in _RAW_RESOURCE_ARGUMENTS:
        if args.get(key) not in (None, ""):
            raise ToolMutationResourceDenied("raw resource lease arguments are not authority")

    _validate_target_ids(args, context)
    shared = _current_shared_scope()
    workspace = _current_agent_workspace()
    workspace_scope = _current_workspace_scope()
    _assert_agent_workspace_run_identity(workspace, workspace_scope, context)

    # A server-bound Agent workspace is the narrowest current resource.  A
    # stale object is a hard deny; it must not silently fall back to Project.
    workspace_key: str | None = None
    workspace_root: Path | None = None
    if workspace is not None:
        if bool(getattr(workspace, "readonly", False)):
            raise ToolMutationResourceDenied("Agent workspace is read-only")
        raw_key = getattr(workspace, "resource_key", None)
        if not raw_key:
            raise ToolMutationResourceDenied("Agent mutation has no resource lease key")
        workspace_key = _normalized_key(raw_key)
        workspace_root = Path(getattr(workspace, "workspace_path"))
        if not bool(getattr(workspace, "lease_active", False)):
            raise ToolMutationResourceDenied("Agent workspace lease is no longer active")
        task_ownership = getattr(workspace, "task_ownership", None)
        if task_ownership is not None and not bool(getattr(task_ownership, "live", False)):
            # Cancelled/ended canonical task: a copied context must not fall
            # back to acquiring the cwd lock and mutating anyway.
            raise ToolMutationResourceDenied("canonical task ownership is no longer active")
    if workspace_scope is not None:
        if bool(workspace_scope.get("readonly")):
            raise ToolMutationResourceDenied("Agent workspace is read-only")
        scoped_key = workspace_scope.get("resource_key")
        if scoped_key not in (None, ""):
            scoped_key = _normalized_key(scoped_key)
            if workspace_key is not None and workspace_key != scoped_key:
                raise ToolMutationResourceDenied("Agent workspace lease identity changed")
            workspace_key = scoped_key

    shared_key: str | None = None
    if shared is not None:
        if not bool(shared.get("permitted_write")):
            raise ToolMutationResourceDenied("active Human resource lease does not permit writes")
        raw_shared_key = shared.get("resource_key")
        if raw_shared_key in (None, ""):
            raise ToolMutationResourceDenied("active Human resource lease has no canonical key")
        shared_key = _normalized_key(raw_shared_key)
        for context_name, scope_name in (
            ("session_id", "session_id"),
            ("user_id", "user_id"),
        ):
            current_value = _id(getattr(context, context_name, None))
            scoped_value = _id(shared.get(scope_name))
            if current_value and scoped_value and current_value != scoped_value:
                raise ToolMutationResourceDenied("active Human resource lease belongs to another turn")
        current_run = _id(_run_id(context))
        scoped_run = _id(shared.get("run_id"))
        if current_run and scoped_run and current_run != scoped_run:
            raise ToolMutationResourceDenied("active Human resource lease belongs to another run")

    bound_project_id = _uuid(getattr(workspace, "project_id", None)) if workspace is not None else None
    project_id = _canonical_project_from_turn(
        context,
        args,
        shared,
        bound_project_id=bound_project_id,
    )
    if shared_key and project_id is not None and shared_key != f"project:{project_id}":
        raise ToolMutationResourceDenied("active shared lease does not match the current Project")
    if workspace_key and project_id is not None and workspace_key.startswith("project:") and workspace_key != f"project:{project_id}":
        raise ToolMutationResourceDenied("Agent workspace lease does not match the current Project")
    if shared_key and workspace_key and shared_key != workspace_key:
        raise ToolMutationResourceDenied("Human and Agent resource leases do not match")

    # Filesystem tools with an issued run scope use the exact canonical root,
    # which is stronger than a model-provided path or resource hash alias.
    run_scope = None
    try:
        from ..security.agent_run_scope import get_current_run_scope

        run_scope = get_current_run_scope()
    except Exception:
        run_scope = None
    if run_scope is not None and _is_filesystem_mutation(tool_definition):
        _validate_scope_paths(tool_definition, args, run_scope)
        if workspace_key is None and shared_key is None:
            workspace_root = Path(run_scope.canonical_root)
            workspace_key = _resource_key_for_workspace_root(workspace_root)

    # ``create_app`` is the one context-free Apps mutation. It still needs a
    # real server-resolved owner namespace lease, but it has no Project or
    # workspace row yet from which to derive one. Hash the authenticated UUID
    # into the canonical workspace-key grammar without trusting model args.
    if (
        _tool_name(tool_definition) == "create_app"
        and workspace_key is None
        and shared_key is None
        and project_id is None
        and getattr(context, "agent_execution_binding", None) is None
    ):
        owner_id = _uuid(getattr(context, "user_id", None))
        if owner_id is None:
            raise ToolMutationResourceDenied("create_app requires a server-resolved owner")
        return CanonicalMutationResource(
            _resource_key_for_user_namespace(owner_id),
            "user",
        )

    selected = workspace_key or shared_key
    if selected is None and project_id is not None and not (_is_filesystem_mutation(tool_definition) and run_scope is not None):
        selected = f"project:{project_id}"
    # Personal Docs changesets carry a server-issued write token bound to the
    # authenticated actor and document root; Docs itself enforces that token,
    # its receipt, and database locks. When no Project scope exists, serialize
    # them in the actor's namespace instead of trusting the raw Docs target as
    # a generic resource key.
    if (
        selected is None
        and _tool_name(tool_definition) == "docs_mutate"
        and getattr(context, "agent_execution_binding", None) is None
    ):
        owner_id = _uuid(getattr(context, "user_id", None))
        if owner_id is not None:
            return CanonicalMutationResource(
                _resource_key_for_user_namespace(owner_id),
                "user",
            )
    if selected is None:
        # Preserve unscoped legacy direct calls.  A real turn with an Agent
        # binding must have an issued workspace/lease proof before mutation.
        if getattr(context, "agent_execution_binding", None) is not None:
            raise ToolMutationResourceDenied("Agent mutation has no canonical active resource")
        if any(
            getattr(context, name, None)
            for name in ("user_id", "session_id", "task_id")
        ):
            raise ToolMutationResourceDenied("mutation has no server-resolved resource")
        return None
    if getattr(context, "agent_execution_binding", None) is not None:
        if workspace is None:
            raise ToolMutationResourceDenied("Agent mutation has no active workspace lease")
        if not bool(getattr(workspace, "lease_active", False)):
            raise ToolMutationResourceDenied("Agent workspace lease is no longer active")
    if workspace_scope is not None and workspace_scope.get("resource_key") not in (None, ""):
        if workspace is None or not bool(getattr(workspace, "lease_active", False)):
            raise ToolMutationResourceDenied("Agent workspace scope has no active lease proof")
    if selected.startswith("project:"):
        return CanonicalMutationResource(selected, "project", project_id=selected.split(":", 1)[1])
    return CanonicalMutationResource(selected, "workspace", workspace_root=workspace_root)


def _active_lock_owned_by_turn(
    resource_key: str,
    *,
    context: Any | None = None,
) -> bool:
    """Check the existing Human whole-turn lock with owner/run proof.

    ``AppOperationLock.locked()`` only says that some task owns the file
    handle.  The owner task's ContextVar snapshot is the opaque proof that the
    lock belongs to this exact shared resource and current AgentRun.  This
    permits same-run child tasks without treating a lock left by an older run
    as reusable.
    """

    try:
        from .agent_dialogue_workspace import AgentDialogueWorkspaceManager
        from ..llm import tool_policy

        current_scope = _current_shared_scope()
        if not isinstance(current_scope, Mapping):
            return False
        current_scope_key = _normalized_key(current_scope.get("resource_key"))
        if current_scope_key != resource_key or not bool(current_scope.get("permitted_write")):
            return False
        current_run = _id(_run_id(context or _current_turn()))
        scoped_run = _id(current_scope.get("run_id"))
        if not current_run or not scoped_run or current_run != scoped_run:
            return False

        manager = AgentDialogueWorkspaceManager()
        lock = manager._lock_for(resource_key)  # type: ignore[attr-defined]
        if not bool(lock.locked()):
            return False
        owner = getattr(lock, "_owner", None)
        if not isinstance(owner, tuple) or len(owner) != 2:
            return False
        owner_loop, owner_task = owner
        if owner_loop is None or owner_loop.is_closed() or owner_task is None:
            return False
        if owner_task.done():
            return False
        scope_var = getattr(tool_policy, "_current_shared_resource_scope", None)
        get_context = getattr(owner_task, "get_context", None)
        if scope_var is None or not callable(get_context):
            return False
        owner_context = get_context()
        owner_scope = owner_context.get(scope_var)
        if not isinstance(owner_scope, Mapping):
            return False
        owner_key = _normalized_key(owner_scope.get("resource_key"))
        owner_run = _id(owner_scope.get("run_id"))
        return bool(
            owner_scope.get("permitted_write")
            and owner_key == resource_key
            and owner_run == current_run
        )
    except Exception:
        return False


def current_workspace_is_task_owned() -> bool:
    """Whether the bound Agent workspace was issued as a canonical task context."""

    workspace = _current_agent_workspace()
    return workspace is not None and getattr(workspace, "task_ownership", None) is not None


def _task_owned_filesystem_resource(tool_definition: Any, resource: CanonicalMutationResource) -> bool:
    """Task ownership replaces the cwd mutex for filesystem/command tools only.

    Project/Docs and other resource mutations in a task-owned context keep the
    ordinary lease acquisition.
    """

    return (
        resource.kind == "workspace"
        and _is_filesystem_mutation(tool_definition)
        and current_workspace_is_task_owned()
    )


def task_workspace_ownership_proof(
    resource_key: str,
    *,
    context: Any | None = None,
) -> TaskWorkspaceOwnershipProof | None:
    """Proof that a live canonical API task owns this workspace key.

    This is *task ownership*, deliberately separate from an exclusive cwd
    lock: it never implies that no other task uses the same cwd.  It needs the
    server-issued :class:`CanonicalTaskOwnership` carried by the bound
    workspace, still live (session open, not cancelled), and agreeing with the
    workspace, the scope, and the current turn's run and owner.
    """

    key = _normalized_key(resource_key)
    workspace = _current_agent_workspace()
    ownership = getattr(workspace, "task_ownership", None) if workspace is not None else None
    if ownership is None:
        return None
    try:
        from .agent_dialogue_workspace import CanonicalTaskOwnership

        if not isinstance(ownership, CanonicalTaskOwnership) or not ownership.live:
            return None
        turn = context or _current_turn()
        workspace_scope = _current_workspace_scope()
        if (
            workspace_scope is None
            or not bool(getattr(workspace, "lease_active", False))
            or bool(getattr(workspace, "readonly", False))
            or bool(workspace_scope.get("readonly"))
            or not key.startswith("workspace:")
            or _normalized_key(getattr(workspace, "resource_key", None)) != key
            or ownership.resource_key != key
        ):
            return None
        # Every applicable identity must be present (non-empty) and exactly
        # equal across the ownership snapshot, the bound workspace, the tool
        # scope and the current turn; absence is a mismatch, not a pass.
        identities = {
            "run": (
                _id(ownership.run_id),
                _id(getattr(workspace, "run_id", None)),
                _id(workspace_scope.get("run_id")),
                _id(_run_id(turn)),
            ),
            "owner": (_id(ownership.owner_id), _id(getattr(workspace, "owner_id", None)), _id(getattr(turn, "user_id", None))),
            "session": (
                _id(ownership.session_id),
                _id(getattr(workspace, "session_id", None)),
                _id(workspace_scope.get("session_id")),
                _id(getattr(turn, "session_id", None)),
            ),
            "agent": (
                _id(ownership.agent_id),
                _id(getattr(workspace, "agent_id", None)),
                _id(workspace_scope.get("agent_id")),
            ),
        }
        binding = getattr(turn, "agent_execution_binding", None)
        if binding is not None:
            identities["agent"] += (_id(getattr(binding, "agent_id", None)),)
        run_scope_id = _current_run_scope_id()
        if run_scope_id is not None:
            identities["run"] += (_id(run_scope_id),)
        for values in identities.values():
            if not values[0] or any(value != values[0] for value in values):
                return None
        _assert_agent_workspace_run_identity(workspace, workspace_scope, turn)
    except Exception:
        return None
    return TaskWorkspaceOwnershipProof(key, ownership.task_id, ownership.run_id)


def existing_active_lease_proof(
    resource_key: str,
    *,
    context: Any | None = None,
) -> ActiveLeaseProof | None:
    """Return proof for an exact active lease bound to this invocation."""

    key = _normalized_key(resource_key)
    if current_workspace_is_task_owned():
        # A task-owned context holds no exclusive cwd lock, so it can never be
        # presented as one; ownership is verified by the callers explicitly and
        # a failed ownership proof must not fall back to ``lease_active``.
        return None
    active = _active_helper_leases.get()
    state = active.get(key) if isinstance(active, dict) else None
    current_run = _id(_run_id(context or _current_turn()))
    if isinstance(state, _HelperLeaseState) and state.live_for(key, current_run):
        return ActiveLeaseProof(key, "tool_mutation_resource_guard")

    workspace = _current_agent_workspace()
    workspace_scope = _current_workspace_scope()
    try:
        _assert_agent_workspace_run_identity(
            workspace,
            workspace_scope,
            context or _current_turn(),
        )
    except ToolMutationResourceDenied:
        return None
    if workspace is not None:
        raw_workspace_key = getattr(workspace, "resource_key", None)
        if raw_workspace_key not in (None, "") and _normalized_key(raw_workspace_key) == key:
            if bool(getattr(workspace, "lease_active", False)) and not bool(getattr(workspace, "readonly", False)):
                return ActiveLeaseProof(key, "agent_dialogue_workspace")
            return None

    shared = _current_shared_scope()
    if (
        shared is not None
        and shared.get("resource_key") not in (None, "")
        and _normalized_key(shared.get("resource_key")) == key
    ):
        if not bool(shared.get("permitted_write")):
            return None
        # ``permitted_write`` is only a policy bit.  The same raw lock must be
        # held by the current turn before it can be reused.
        if _active_lock_owned_by_turn(key, context=context or _current_turn()):
            return ActiveLeaseProof(key, "human_shared_resource")
    return None


def _mark_helper_lease(
    key: str,
    *,
    run_id: str | None,
) -> tuple[
    contextvars.Token[dict[str, _HelperLeaseState] | None],
    dict[str, _HelperLeaseState],
    _HelperLeaseState,
]:
    inherited = _active_helper_leases.get()
    active = dict(inherited) if isinstance(inherited, dict) else {}
    state = _HelperLeaseState(
        resource_key=key,
        run_id=run_id,
        owner_token=object(),
    )
    active[key] = state
    token = _active_helper_leases.set(active)
    return token, active, state


def _release_helper_lease(
    handle: tuple[
        contextvars.Token[dict[str, _HelperLeaseState] | None],
        dict[str, _HelperLeaseState],
        _HelperLeaseState,
    ],
) -> None:
    token, active, state = handle
    state.active = False
    # Child Tasks inherit this same dict object. Removing the entry after
    # marking it inactive prevents a copied ContextVar from becoming a stale
    # proof after the parent lease has already released its OS lock.
    active.pop(state.resource_key, None)
    _active_helper_leases.reset(token)


def sync_lease_context_active() -> bool:
    """Whether a sync ``ToolDefinition`` is executing on the lease loop."""

    return bool(_sync_lease_context.get())


@asynccontextmanager
async def mutation_resource_lease(
    tool_definition: Any,
    arguments: Mapping[str, Any] | None = None,
    *,
    context: Any | None = None,
) -> AsyncIterator[CanonicalMutationResource | None]:
    """Hold the shared lease for exactly one concrete tool invocation."""

    resource = canonical_resource_for_tool(tool_definition, arguments, context=context)
    if resource is None:
        yield None
        return
    if _task_owned_filesystem_resource(tool_definition, resource):
        if task_workspace_ownership_proof(resource.key, context=context) is None:
            raise ToolMutationResourceDenied("canonical task ownership proof is unavailable")
        yield resource
        return
    if existing_active_lease_proof(resource.key, context=context) is not None:
        yield resource
        return
    shared = _current_shared_scope()
    if (
        shared is not None
        and shared.get("resource_key") not in (None, "")
        and _normalized_key(shared.get("resource_key")) == resource.key
    ):
        # An ambient shared-scope dictionary is policy metadata, not a lease
        # handle.  Never re-acquire (or treat ``permitted_write`` as proof)
        # when the original whole-turn lock is no longer active.
        raise ToolMutationResourceDenied("active Human resource lease proof is unavailable")

    try:
        from .agent_dialogue_workspace import shared_resource_lease

        async with shared_resource_lease(resource.key, timeout=0.0):
            handle = _mark_helper_lease(
                resource.key,
                run_id=_id(_run_id(context or _current_turn())) or None,
            )
            try:
                yield resource
            finally:
                _release_helper_lease(handle)
    except Exception as exc:
        from .agent_dialogue_workspace import AgentDialogueWorkspaceBusy

        if isinstance(exc, AgentDialogueWorkspaceBusy):
            raise ToolMutationResourceBusy(str(exc)) from exc
        raise


async def execute_with_mutation_resource_lease(
    tool_definition: Any,
    arguments: Mapping[str, Any],
    invoke: Callable[[], Any],
    *,
    context: Any | None = None,
) -> Any:
    """Async common entry used by ``ToolDefinition.execute_async``."""

    async with mutation_resource_lease(tool_definition, arguments, context=context):
        return await _maybe_await(invoke())


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _run_async_in_sync_context(coroutine: Any) -> Any:
    """Drive one lease-aware coroutine while retaining ContextVars."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    captured = contextvars.copy_context()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(captured.run, asyncio.run, coroutine).result()


def execute_with_mutation_resource_lease_sync(
    tool_definition: Any,
    arguments: Mapping[str, Any],
    invoke: Callable[[], Any],
    *,
    context: Any | None = None,
) -> Any:
    """Sync common entry used by ``ToolDefinition.execute``.

    The callable and any awaitable it returns run inside the lease.  When a
    synchronous caller is already on an event loop, the established copied
    ContextVar bridge is reused so turn identity and cancellation state do not
    disappear in the worker thread.
    """

    async def _run() -> Any:
        marker = _sync_lease_context.set(True)
        try:
            async with mutation_resource_lease(tool_definition, arguments, context=context):
                return await _maybe_await(invoke())
        finally:
            _sync_lease_context.reset(marker)

    # Avoid changing the historical unscoped sync path: only enter an async
    # bridge when this concrete mutation actually resolves a resource.
    resource = canonical_resource_for_tool(tool_definition, arguments, context=context)
    if resource is not None and _task_owned_filesystem_resource(tool_definition, resource):
        if task_workspace_ownership_proof(resource.key, context=context) is None:
            raise ToolMutationResourceDenied("canonical task ownership proof is unavailable")
        result = invoke()
        return _run_async_in_sync_context(result) if inspect.isawaitable(result) else result
    if resource is None or existing_active_lease_proof(resource.key, context=context) is not None:
        result = invoke()
        return _run_async_in_sync_context(result) if inspect.isawaitable(result) else result
    return _run_async_in_sync_context(_run())


# Compatibility aliases used by callers/tests that describe this as a guard.
def canonical_resource_key_for_tool(
    tool_definition: Any,
    arguments: Mapping[str, Any] | None = None,
    *,
    context: Any | None = None,
) -> str | None:
    resource = canonical_resource_for_tool(tool_definition, arguments, context=context)
    return resource.key if resource is not None else None


resolve_canonical_resource = canonical_resource_for_tool
resource_key_for_tool = canonical_resource_key_for_tool
tool_mutation_resource_lease = mutation_resource_lease
per_tool_resource_lease = mutation_resource_lease
acquire_tool_mutation_lease = mutation_resource_lease
run_with_mutation_resource_lease = execute_with_mutation_resource_lease
run_with_mutation_resource_lease_sync = execute_with_mutation_resource_lease_sync


__all__ = [
    "ActiveLeaseProof",
    "CanonicalMutationResource",
    "ToolMutationResourceBusy",
    "ToolMutationResourceDenied",
    "acquire_tool_mutation_lease",
    "canonical_resource_key_for_tool",
    "TaskWorkspaceOwnershipProof",
    "current_workspace_is_task_owned",
    "task_workspace_ownership_proof",
    "canonical_resource_for_tool",
    "existing_active_lease_proof",
    "execute_with_mutation_resource_lease",
    "execute_with_mutation_resource_lease_sync",
    "is_workspace_mutation",
    "mutation_resource_lease",
    "per_tool_resource_lease",
    "resolve_canonical_resource",
    "run_with_mutation_resource_lease",
    "run_with_mutation_resource_lease_sync",
    "resource_key_for_tool",
    "sync_lease_context_active",
    "tool_mutation_resource_lease",
]
