"""Authenticated generic Agent task admission and API provider execution.

The durable lifecycle is owned by ``AgentWorkCoordinator`` and the pinned
``AgentRun``. This adapter only resolves the private task session, binds the
existing Chat Agent execution context, and invokes the ordinary API client.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import json
import logging
import os
import posixpath
import re
import stat
import subprocess
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import desc, func, select

from ..tools.core import ToolPolicyDeniedError
from .agent_work_runtime import ExecutionOutcome, WorkCandidate
from ..llm.provider_rate_governor import is_temporary_429_failure
from .provider_failure_metadata import extract_provider_failure, safe_usage_snapshot, terminal_failure_event_fields

logger = logging.getLogger(__name__)

AGENT_TASK_ADAPTER_KEY = "api_agent_task"
AGENT_TASK_SOURCE_TYPE = AGENT_TASK_ADAPTER_KEY
_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ROUTE_EXPECTATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_ITEMS = 20
_MAX_ITEM_CHARS = 1000
_TASK_TIMEOUT_SECONDS = 3600


class AgentTaskInvocationError(RuntimeError):
    def __init__(self, code: str, message: str, http_status: int = 409) -> None:
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


@contextmanager
def _agent_selection_errors() -> Iterator[None]:
    """Map Agent selection refusals onto the task API error contract.

    A user who may not use the selected Agent (another user's Agent, an
    inactive Agent, a missing Project grant) gets the same ``permission_denied``
    403 as the other task authority refusals instead of an HTTP 500.  An
    unknown Agent is reported the same way so its existence is not revealed.
    """

    from .conversation_agent_service import (
        ConversationAgentAuthorizationError,
        ConversationAgentConflictError,
        ConversationAgentError,
        ConversationAgentNotFoundError,
    )

    try:
        yield
    except (ConversationAgentAuthorizationError, ConversationAgentNotFoundError):
        raise AgentTaskInvocationError(
            "permission_denied",
            "The selected Agent is not available to this user",
            403,
        ) from None
    except ConversationAgentConflictError:
        raise AgentTaskInvocationError(
            "agent_conflict", "The selected Agent cannot be bound right now", 409
        ) from None
    except ConversationAgentError:
        raise AgentTaskInvocationError(
            "invalid_input", "The selected Agent is invalid", 422
        ) from None


def _resolve_task_operator_route(config: Any, pins: Any) -> Any:
    """Keep canonical role selection failures inside the task API contract."""
    from .agent_model_routing import AgentModelRouteError, resolve_agent_role_route

    try:
        return resolve_agent_role_route(config, pins, "operator")
    except AgentModelRouteError as exc:
        raise AgentTaskInvocationError(
            exc.code, "The selected Agent has no usable API task operator route", 422
        ) from None


def _clean_text(value: Any, *, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise AgentTaskInvocationError("invalid_input", f"{name} must be a non-empty string up to {maximum} characters", 422)
    return value.strip()


def _string_list(value: Any, *, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_ITEMS:
        raise AgentTaskInvocationError("invalid_input", f"{name} must contain at most {_MAX_ITEMS} strings", 422)
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or len(item) > _MAX_ITEM_CHARS:
            raise AgentTaskInvocationError("invalid_input", f"{name} items must be strings up to {_MAX_ITEM_CHARS} characters", 422)
        item = item.strip()
        if item:
            result.append(item)
    return tuple(result)


def _workspace_files(value: Any, workspace: str, *, name: str) -> tuple[str, ...]:
    """Validate explicit files: Windows-safe relative names, in scope, unique."""

    items = _string_list(value, name=name)
    root = Path(workspace)
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        relative = _clean_output_relative(item)
        if relative is None or _output_file(root, relative)[1] is not True:
            raise AgentTaskInvocationError("invalid_input", f"{name} must be distinct files inside the workspace", 422)
        key = relative.casefold() if os.name == "nt" else relative
        if key in seen:
            raise AgentTaskInvocationError("invalid_input", f"{name} must be distinct files inside the workspace", 422)
        seen.add(key)
        result.append(relative)
    return tuple(result)


def _required_outputs(value: Any, workspace: str) -> tuple[str, ...]:
    return _workspace_files(value, workspace, name="required_outputs")


def _edit_targets(value: Any, workspace: str) -> tuple[str, ...] | None:
    """Explicit editable files; ``None`` (omitted) keeps the legacy prose-derived scope."""

    if value is None:
        return None
    return _workspace_files(value, workspace, name="edit_targets")


def _original_request_text(prompt: str) -> str:
    """Goal/Constraints/Completion-criteria text of a persisted task prompt.

    Framework sections (required outputs, verification, workspace root) are not
    caller prose and never feed the authority parser.
    """

    sections = re.split(
        r"^(Goal|Constraints|Completion criteria|Required output files[^\n]*|Verification requirement|Workspace root):[ \t]*$",
        prompt,
        flags=re.MULTILINE,
    )
    if len(sections) == 1:
        return prompt.split("\nWorkspace root:", 1)[0]
    caller = {"Goal", "Constraints", "Completion criteria"}
    kept = [sections[i + 1] for i in range(1, len(sections) - 1, 2) if sections[i] in caller]
    return "\n".join(item.strip("\n") for item in kept)


def _edit_scope_for_request(request: Mapping[str, Any]) -> Any:
    """The frozen file scope of a stored task, or one derived from its ORIGINAL prompt.

    Present metadata must be valid (malformed fails closed).  Tasks admitted
    before the field existed re-derive it deterministically, on every load,
    from the durable original prompt (the Run objective is immutable and only
    its Goal/Constraints/Completion-criteria sections are read), so the result
    is the same across restarts and depends on nothing the framework adds
    later.  The derived value is not written back.
    """

    from ..llm.tool_policy import FixedEditScope, StructuredEditScopeError, fixed_edit_scope_from_prose

    try:
        if "edit_scope" in request:
            return FixedEditScope.from_payload(request["edit_scope"])
        return fixed_edit_scope_from_prose(_original_request_text(str(request.get("prompt") or "")))
    except StructuredEditScopeError:
        raise AgentTaskInvocationError("execution_binding_mismatch", "The task edit scope is unavailable or malformed", 409) from None


def _canonical_uuid(value: Any, *, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise AgentTaskInvocationError("invalid_input", f"{label} must be a UUID", 422) from None


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            pass
    value = config
    for part in key.split("."):
        if isinstance(value, Mapping):
            value = value.get(part, default)
        else:
            value = getattr(value, part, default)
        if value is default:
            return default
    return value


def _approved_workspace_roots(config: Any) -> tuple[Path, ...]:
    """Read the existing task workspace allowlist without expanding it."""

    configured = _config_get(config, "local_agent_tasks.allowed_roots")
    if isinstance(configured, str):
        configured = [configured]
    if not isinstance(configured, (list, tuple)) or not configured:
        # Preserve the existing configured default used by local task admission.
        from .local_agent_tasks import default_allowed_roots

        values = default_allowed_roots(config)
    else:
        values = []
        for raw in configured:
            if not isinstance(raw, (str, os.PathLike)) or not str(raw).strip():
                continue
            try:
                candidate = Path(os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(raw)))))
            except (OSError, TypeError, ValueError):
                continue
            if candidate.is_dir():
                values.append(candidate)
    roots: list[Path] = []
    for root in values:
        try:
            resolved = Path(root).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _workspace_path(config: Any, raw: Any) -> Path:
    from .command_workspace_confinement import inspect_existing_workspace
    from .local_agent_tasks import LocalAgentTaskError

    _ = config
    try:
        return inspect_existing_workspace(raw)
    except LocalAgentTaskError as exc:
        raise AgentTaskInvocationError(exc.code, exc.message, exc.http_status) from None


def _path_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(str(path)), os.path.normcase(str(root)))) == os.path.normcase(str(root))
    except (OSError, ValueError):
        return False


def _normalise_request(payload: Mapping[str, Any], config: Any) -> dict[str, Any]:
    goal = _clean_text(payload.get("goal"), name="goal", maximum=8000)
    workspace = _workspace_path(config, payload.get("workspace"))
    constraints = _string_list(payload.get("constraints", ()), name="constraints")
    criteria = _string_list(payload.get("completion_criteria", ()), name="completion_criteria")
    mode = payload.get("mode")
    if mode is not None and (not isinstance(mode, str) or mode not in {"read_only", "mutate"}):
        raise AgentTaskInvocationError("invalid_input", "mode must be read_only or mutate", 422)
    required_outputs = _required_outputs(payload.get("required_outputs", ()), str(workspace))
    if required_outputs and mode == "read_only":
        raise AgentTaskInvocationError("invalid_input", "required_outputs requires mutate mode", 422)
    edit_targets = _edit_targets(payload.get("edit_targets"), str(workspace))
    if edit_targets and mode == "read_only":
        raise AgentTaskInvocationError("invalid_input", "edit_targets requires mutate mode", 422)
    allow_commands = payload.get("allow_commands")
    if allow_commands is not None and not isinstance(allow_commands, bool):
        raise AgentTaskInvocationError("invalid_input", "allow_commands must be a boolean", 422)
    if allow_commands is True and mode == "read_only":
        raise AgentTaskInvocationError("permission_denied", "allow_commands requires mutate mode", 403)
    if mode == "mutate":
        from ..llm.tool_policy import structured_task_whole_mutation_forbidden

        if structured_task_whole_mutation_forbidden("\n".join([goal, *constraints, *criteria])):
            raise AgentTaskInvocationError("invalid_input", "mode=mutate conflicts with a whole-task read-only instruction", 422)
    allow_git_commit = payload.get("allow_git_commit")
    allow_git_push = payload.get("allow_git_push")
    if allow_git_commit is None:
        allow_git_commit = False
    if allow_git_push is None:
        allow_git_push = False
    if not isinstance(allow_git_commit, bool) or not isinstance(allow_git_push, bool):
        raise AgentTaskInvocationError("invalid_input", "Git permissions must be booleans", 422)
    if (allow_git_commit or allow_git_push) and (mode != "mutate" or allow_commands is not True):
        raise AgentTaskInvocationError(
            "permission_denied",
            "Git writes require explicitly requested mode=mutate and allow_commands=true",
            403,
        )
    agent_id = _canonical_uuid(payload.get("agent_id"), label="agent_id")
    expected: dict[str, str] = {}
    for name in ("provider", "model", "effort"):
        value = payload.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise AgentTaskInvocationError("invalid_input", f"{name} is invalid", 422)
        normalized = value.strip()
        if name == "provider" and (
            "://" in normalized
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", normalized)
        ):
            raise AgentTaskInvocationError("invalid_input", "provider must be a provider identifier, not a URL", 422)
        if name != "effort" and not _ROUTE_EXPECTATION_RE.fullmatch(normalized):
            raise AgentTaskInvocationError("invalid_input", f"{name} is invalid", 422)
        if name == "effort" and (len(normalized) > 40 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", normalized)):
            raise AgentTaskInvocationError("invalid_input", "effort is invalid", 422)
        expected[name] = normalized
    requested = _string_list(payload.get("required_capabilities", ()), name="required_capabilities")
    if any(not _CAPABILITY_RE.fullmatch(item) for item in requested) or len(set(requested)) != len(requested):
        raise AgentTaskInvocationError("invalid_input", "required_capabilities contains an invalid or duplicate capability", 422)
    explicit_verification = payload.get("requires_command_verification")
    if explicit_verification is not None and not isinstance(explicit_verification, bool):
        raise AgentTaskInvocationError("invalid_input", "requires_command_verification must be a boolean", 422)
    requires_verification = _requires_command_verification(
        {
            "goal": goal,
            "completion_criteria": list(criteria),
            "requires_command_verification": explicit_verification,
            "allow_commands": allow_commands,
        }
    )
    if requires_verification and allow_commands is False:
        source = "requires_command_verification=true" if explicit_verification is True else "the goal/completion criteria ask to run tests, build or lint"
        raise AgentTaskInvocationError(
            "invalid_input",
            f"{source} but allow_commands=false forbids running commands; "
            "set allow_commands=true, or remove the execution requirement (use requires_command_verification=false for a static review)",
            422,
        )
    normalized = {
        "goal": goal,
        "workspace": str(workspace),
        "constraints": list(constraints),
        "completion_criteria": list(criteria),
        "mode": mode,
        "allow_commands": allow_commands,
        "agent_id": agent_id,
        "expected_route": expected,
        "required_capabilities": list(requested),
        "requires_command_verification": requires_verification,
    }
    if required_outputs:
        # Only a non-empty contract joins the request, so every pre-existing
        # request keeps its fingerprint (idempotent replays stay equal).
        normalized["required_outputs"] = list(required_outputs)
    if edit_targets is not None:
        # Present (even empty) is part of the request identity; omitted keeps
        # every pre-existing request fingerprint.
        normalized["edit_targets"] = list(edit_targets)
    execution_profile_id = payload.get("execution_profile_id")
    if execution_profile_id is not None:
        if not isinstance(execution_profile_id, str) or not execution_profile_id:
            raise AgentTaskInvocationError("agent_execution_profile_invalid", "execution_profile_id is invalid", 422)
        # Only an explicit selection joins the request identity; omitted/null
        # keeps every pre-existing request fingerprint byte-identical.
        normalized["execution_profile_id"] = execution_profile_id
    if allow_git_commit:
        normalized["allow_git_commit"] = True
    if allow_git_push:
        normalized["allow_git_push"] = True
    tool_generation = payload.get("tool_generation_max_tokens")
    if tool_generation is not None:
        if isinstance(tool_generation, bool) or not isinstance(tool_generation, int) or not 1 <= tool_generation <= 4096:
            raise AgentTaskInvocationError("invalid_input", "tool_generation_max_tokens must be an integer between 1 and 4096", 422)
        # Explicit per-task opt-in; only a present value joins the identity.
        normalized["tool_generation_max_tokens"] = tool_generation
    return normalized


def _request_fingerprint(request: Mapping[str, Any]) -> str:
    canonical = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _task_id_for(owner_id: str, idempotency_key: str | None) -> str:
    return (
        uuid.uuid5(uuid.NAMESPACE_URL, f"aoitalk-api-agent-task:{owner_id}:{idempotency_key}").hex
        if idempotency_key
        else uuid.uuid4().hex
    )


def _admission_revision(task_id: str) -> str:
    """Stable AgentWork uniqueness key for one owner-scoped task identity."""

    return hashlib.sha256(f"api-agent-task-admission:v1:{task_id}".encode("utf-8")).hexdigest()


def _task_prompt(request: Mapping[str, Any]) -> str:
    sections = ["Complete the requested task using the registered AoiTalk tools. Do not claim an action succeeded unless its tool result confirms it.", "\nGoal:\n" + str(request["goal"])]
    if request.get("constraints"):
        sections.append("\nConstraints:\n" + "\n".join(f"- {item}" for item in request["constraints"]))
    if request.get("completion_criteria"):
        sections.append("\nCompletion criteria:\n" + "\n".join(f"- {item}" for item in request["completion_criteria"]))
    if request.get("required_outputs"):
        sections.append(
            "\nRequired output files (the task is not complete until each is saved with a file tool):\n"
            + "\n".join(f"- {item}" for item in request["required_outputs"])
        )
    if request.get("requires_command_verification"):
        sections.append("\nVerification requirement:\nRun the requested verification commands after the last edit and report their confirmed results.")
    sections.extend(("\nWorkspace root:\n" + str(request["workspace"]), "\nDo not read, write, or execute outside the assigned workspace. Do not expose credentials or secret values."))
    if request.get("allow_git_commit"):
        sections.append(
            "\nGit commit permission:\nYou may stage only explicit task-changed paths and create a normal commit. "
            "Do not stage all files or include unrelated pre-existing changes."
        )
    if request.get("allow_git_push"):
        sections.append(
            "\nGit push permission:\nYou may push one explicit ordinary branch refspec to a configured remote "
            "after completing the requested work. Force, delete, mirror, tags, and remote/config changes are not allowed."
        )
    return "\n".join(sections)


_TEST_EXECUTION_RE = re.compile(
    r"\b(?:run|execute|rerun|use|perform|verify|validate|check|confirm|ensure)\b"
    r"[^.!?\n]{0,80}\b(?:tests?|pytest|unittest)\b",
    re.IGNORECASE,
)
_BUILD_EXECUTION_RE = re.compile(
    r"\b(?:run|execute|rerun|perform|invoke)\b[^.!?\n]{0,60}"
    r"\b(?:builds?|lint(?:er|ing)?|type-?checks?|type-?checking|compil\w+"
    r"|(?:verification|check)\s+(?:commands?|scripts?|suite))\b"
    r"|\b(?:builds?|lint|type-?checks?)\b[^.!?\n]{0,40}\b(?:pass(?:es)?|succe\w+|green|clean)\b",
    re.IGNORECASE,
)
_NEGATION_BEFORE_RE = re.compile(
    r"\b(?:no|not|never|without|cannot|can't|don't|dont|skip|avoid|unable)\b|n't\b",
    re.IGNORECASE,
)


def _unnegated_match(pattern: re.Pattern[str], sentences: Sequence[str]) -> bool:
    """Whether a sentence asks for the matched action and does not negate it."""

    for sentence in sentences:
        for match in pattern.finditer(sentence):
            if not _NEGATION_BEFORE_RE.search(sentence[max(0, match.start() - 40):match.end()]):
                return True
    return False


def _requires_command_verification(request: Mapping[str, Any]) -> bool:
    # Structured caller intent takes precedence over language heuristics.
    explicit = request.get("requires_command_verification")
    if isinstance(explicit, bool):
        return explicit
    text = "\n".join(
        [str(request.get("goal") or ""), *(str(item) for item in request.get("completion_criteria") or ())]
    )
    sentences = [part for part in re.split(r"[.!?\n]+", text) if part.strip()]
    test_execution = _unnegated_match(_TEST_EXECUTION_RE, sentences)
    # A bare "verification"/"build"/"check" word is prose (e.g. "explicit
    # verification limitations"); only an execution verb or a pass/success
    # requirement on a build-like step asks for a command.
    other_verification = _unnegated_match(_BUILD_EXECUTION_RE, sentences)
    japanese_execution = any(
        not re.search(
            r"(?:実行|再実行)(?:しない|せず|しなく|する必要はない|方法|手順|する方法|する手順|(?:は|が)?不要|禁止)",
            clause,
        )
        and re.search(
            r"(?:テスト|pytest|unittest)[^。！？\n、,;]{0,80}(?:実行|再実行|通る|通す|成功|合格|パス)"
            r"|(?:実行|再実行)[^。！？\n、,;]{0,40}(?:テスト|pytest|unittest)", clause, re.IGNORECASE,
        )
        for clause in re.split(r"[。！？\n、,;]", text)
    )
    criteria_verification = any(
        re.search(r"\b(?:tests?|pytest|unittest)\b[^.!?\n]{0,80}\b(?:pass(?:es|ed)?|succeed|successful|green)\b", str(item), re.IGNORECASE)
        for item in request.get("completion_criteria") or ()
    )
    return bool(test_execution or other_verification or japanese_execution or criteria_verification)


def _receipt_mapping(receipt: Any, field: str) -> Mapping[str, Any]:
    value = getattr(receipt, field, {})
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, Mapping) else {}
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, Mapping) else {}


_RECEIPT_LITERAL_LIMIT = 200_000


def _decode_receipt_value(value: Any) -> Any:
    """Undo the serialisation layers a durable tool result may carry.

    The unified runtime stores a dict tool result as ``str(result)``, so the
    receipt text is a Python ``repr`` (``{'success': True, 'message': 'File
    edited successfully: D:\\\\Dev\\\\...\\n\\n...'}``).  Other adapters store
    JSON.  Both, and a quoted string inside either, are decoded here; any
    other text is returned unchanged.
    """

    for _ in range(4):
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text or text[0] not in "{[\"'" or len(text) > _RECEIPT_LITERAL_LIMIT:
            return value
        try:
            value = json.loads(text)
            continue
        except (TypeError, ValueError):
            pass
        try:
            decoded = ast.literal_eval(text)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            return value
        if not isinstance(decoded, (str, Mapping, list, tuple)):
            return value
        value = decoded
    return value


def _receipt_result_payload(receipt: Any) -> Mapping[str, Any]:
    value = _decode_receipt_value(getattr(receipt, "result", None))
    return value if isinstance(value, Mapping) else {}


def _receipt_result_text(receipt: Any) -> str:
    raw = getattr(receipt, "result", None)
    value = _decode_receipt_value(raw)
    payload = value if isinstance(value, Mapping) else {}
    for key in ("output", "stdout", "result", "message", "error"):
        candidate = payload.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    if isinstance(value, str):
        return value
    return raw if isinstance(raw, str) else ""


def _command_exit_payload(receipt: Any) -> Mapping[str, Any]:
    """Decode a command result, including the ``Error: <msg>\n{json}`` wrapper."""

    payload = _receipt_result_payload(receipt)
    if payload:
        return payload
    raw = getattr(receipt, "result", None)
    if isinstance(raw, str):
        index = raw.find("\n{")
        if index >= 0:
            decoded = _decode_receipt_value(raw[index + 1:])
            if isinstance(decoded, Mapping):
                return decoded
    return {}


def _receipt_command_nonzero_exit(receipt: Any) -> int | None:
    """Return the exit code of a launched foreground command that exited nonzero.

    Timeouts, background jobs, typed errors and refusals are not an ordinary
    exit and return ``None`` so they stay unresolved.
    """

    if str(getattr(receipt, "tool_name", "") or "").rsplit(".", 1)[-1].casefold() != "execute_command":
        return None
    if bool(getattr(receipt, "success", False)) or _receipt_was_blocked(receipt):
        return None
    metadata = _receipt_mapping(receipt, "result_metadata")
    if metadata.get("executed") is not True:
        return None
    payload = _command_exit_payload(receipt)
    if metadata.get("job_id") or payload.get("job_id"):
        return None
    if metadata.get("timed_out") is True or payload.get("timed_out") is True:
        return None
    if payload.get("error_code") or metadata.get("error_code"):
        return None
    return_code = payload.get("return_code", metadata.get("return_code"))
    if type(return_code) is not int or return_code == 0:
        return None
    return return_code


_ARGUMENT_REJECTION_MESSAGE = "Tool arguments were rejected by the server contract."
_ARGUMENT_REJECTION_KEY = "<argument-rejected>"
_ARGUMENT_REJECTION_CODES = frozenset({
    "tool_argument_invalid",
    "tool_argument_unknown",
    "tool_argument_conflict",
    "tool_argument_missing",
})


def _argument_rejected_before_execution(receipt: Any) -> bool:
    """True only for a server-authored schema rejection of a read-only call.

    Authority/policy/approval/circuit refusals, synthetic results, writes and
    commands are never classified here, so they stay unresolved.
    """

    if bool(getattr(receipt, "success", False)) or _receipt_was_blocked(receipt):
        return False
    metadata = _receipt_mapping(receipt, "result_metadata")
    if metadata.get("executed") is not False or metadata.get("synthetic") is not False:
        return False
    if metadata.get("blocked_reason"):
        return False
    tool_name = str(getattr(receipt, "tool_name", "") or "").rsplit(".", 1)[-1].casefold()
    if tool_name not in _recoverable_read_tool_names():
        return False
    codes = {
        str(metadata.get("error_code") or "").strip(),
        str(_receipt_result_payload(receipt).get("error_code") or "").strip(),
    } - {""}
    if codes and not codes <= _ARGUMENT_REJECTION_CODES:
        return False
    # The persisted projection wraps the fixed server text as ``Error: <text>``.
    # Only that exact known wrapper is normalised; free-form text never matches.
    known = {_ARGUMENT_REJECTION_MESSAGE, f"Error: {_ARGUMENT_REJECTION_MESSAGE}"}
    return any(
        str(text or "").strip() in known
        for text in (metadata.get("error"), _receipt_result_text(receipt))
    )


def _argument_rejection_target(receipt: Any) -> str:
    """Safe target digest recorded with the rejection ("" for older receipts)."""

    return str(_argument_rejection_provenance(receipt).get("target_digest") or "").strip()


def _argument_rejection_provenance(receipt: Any) -> Mapping[str, Any]:
    provenance = _receipt_mapping(receipt, "result_metadata").get("argument_rejection")
    return provenance if isinstance(provenance, Mapping) else {}


_CORRELATION_FIELDS = (
    "query_digest",
    "start_digest",
    "end_digest",
    "offset_digest",
    "max_chars_digest",
)


def _rejection_scope_signature(receipt: Any) -> tuple[str, ...]:
    provenance = _argument_rejection_provenance(receipt)
    return tuple(str(provenance.get(field) or "") for field in _CORRELATION_FIELDS)


def _retry_answers_rejection(
    rejected: Any, retry_arguments: Mapping[str, Any], tool_name: str = ""
) -> bool:
    """A same-target success corrects the rejection only for the same request.

    A rejected search query or line range must be answered by a success with
    that same query/range; a different search is not a correction.
    """

    from ..tools.core import argument_correlation

    wanted = _argument_rejection_provenance(rejected)
    actual = argument_correlation(retry_arguments, tool_name)
    return all(
        not wanted.get(field) or wanted.get(field) == actual.get(field)
        for field in _CORRELATION_FIELDS
    )


def _receipt_action_key(receipt: Any) -> tuple[str, str]:
    tool_name = str(getattr(receipt, "tool_name", "") or "").rsplit(".", 1)[-1].casefold()
    if _argument_rejected_before_execution(receipt):
        # Without recorded target provenance nothing can prove a later call
        # corrected this one, so the key is unique to the receipt.
        target = _argument_rejection_target(receipt) or f"unproven:{id(receipt)}"
        return tool_name, f"{_ARGUMENT_REJECTION_KEY}{target}"
    arguments = dict(_receipt_mapping(receipt, "arguments"))
    if tool_name == "execute_command":
        evidence = {key: arguments.get(key) for key in ("command", "working_directory")}
    elif tool_name in {"edit_file", "create_file", "append_to_file", "insert_to_file", "delete_file"}:
        evidence = {"path": arguments.get("path")}
    elif tool_name in {"read_file", "search_files", "list_directory", "get_repo_map"}:
        evidence = {key: arguments.get(key) for key in ("path", "query", "start_line", "end_line") if key in arguments}
    else:
        evidence = arguments
    canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return tool_name, canonical


def _receipt_background_requested(arguments: Mapping[str, Any]) -> bool:
    """Read run_in_background from a receipt without treating "False" as true."""

    value = arguments.get("run_in_background")
    if isinstance(value, str):
        return value.strip().casefold() == "true"
    return bool(value)


def _foreground_command_succeeded(receipt: Any) -> bool:
    """Accept command-test evidence only after a foreground process exit."""

    arguments = _receipt_mapping(receipt, "arguments")
    metadata = _receipt_mapping(receipt, "result_metadata")
    payload = _receipt_result_payload(receipt)
    if _receipt_background_requested(arguments) or metadata.get("job_id"):
        return False
    return_code = metadata.get(
        "return_code",
        metadata.get("exit_code", payload.get("return_code", payload.get("exit_code"))),
    )
    timed_out = metadata.get("timed_out", payload.get("timed_out"))
    if type(return_code) is not int or return_code != 0 or timed_out is True:
        return False
    return bool(_receipt_result_text(receipt).strip())


def _completed_background_command_succeeded(
    launch_receipt: Any,
    receipts: Sequence[Any],
) -> bool:
    """Require a matching completed read before accepting a background test."""

    arguments = _receipt_mapping(launch_receipt, "arguments")
    metadata = _receipt_mapping(launch_receipt, "result_metadata")
    payload = _receipt_result_payload(launch_receipt)
    if not _receipt_background_requested(arguments):
        return False
    job_id = str(metadata.get("job_id") or payload.get("job_id") or "").strip()
    if not job_id:
        return False
    for receipt in receipts:
        if not bool(getattr(receipt, "success", False)):
            continue
        tool_name = str(getattr(receipt, "tool_name", "") or "").rsplit(".", 1)[-1].casefold()
        if tool_name != "read_command_output":
            continue
        read_arguments = _receipt_mapping(receipt, "arguments")
        read_metadata = _receipt_mapping(receipt, "result_metadata")
        read_payload = _receipt_result_payload(receipt)
        if str(read_arguments.get("job_id") or "").strip() != job_id:
            continue
        exit_code = read_metadata.get("exit_code", read_payload.get("exit_code"))
        read_status = read_metadata.get("status", read_payload.get("status"))
        finalization_error = read_metadata.get(
            "finalization_error", read_payload.get("finalization_error")
        )
        result = _receipt_result_text(receipt)
        if (
            str(read_status or "").casefold() == "exited"
            and type(exit_code) is int
            and exit_code == 0
            and not finalization_error
            and "Exit code: 0" in result
            and bool(result.strip())
        ):
            return True
    return False


def _close_run_background_jobs(scope: Any) -> tuple[list[dict[str, Any]], bool]:
    """Use the existing scoped registry to drain children before run settlement."""

    from .agent_run_background_jobs import finish_agent_run_background_jobs
    from ..tools.os_operations.background_jobs import get_background_job_registry

    registry = get_background_job_registry()
    try:
        before = registry.list_jobs(scope)
        running_ids = {
            str(job.get("job_id") or "")
            for job in before
            if isinstance(job, Mapping)
            and str(job.get("status") or "").casefold() in {"running", "termination_failed"}
        }
        finished = finish_agent_run_background_jobs(scope, registry=registry)
        closed = finished.get("closed", [])
        active = int(finished.get("active_count", 0) or 0)
        jobs = registry.list_jobs(scope)
    except Exception as exc:
        raise AgentTaskInvocationError(
            "background_job_settlement_failed",
            "Run-scoped background commands could not be drained",
            409,
        ) from exc
    if active or any(
        str(job.get("status") or "").casefold() in {"running", "termination_failed"}
        or bool(job.get("finalization_error"))
        for job in jobs
        if isinstance(job, Mapping)
    ):
        raise AgentTaskInvocationError(
            "background_job_settlement_failed",
            "Run-scoped background commands did not reach a verified terminal state",
            409,
        )
    results = [dict(job) for job in closed if isinstance(job, Mapping)]
    interrupted = any(
        bool(job.get("timed_out"))
        or (
            str(job.get("job_id") or "") in running_ids
            and str(job.get("status") or "").casefold() == "killed"
        )
        for job in results
    )
    return results, interrupted


_MAX_CORRECTIVE_TURNS = 2
_OUTPUT_BASELINE_EVENT = "api_agent_task.required_output_baseline"
# Only these gaps can be closed by a correction; unresolved failures, policy
# denials and a missing command verification are never corrected.
_CORRECTABLE_EVIDENCE_CODES = frozenset({
    "task_no_successful_tool_receipt",
    "task_filesystem_mutation_receipt_required",
    "task_required_output_missing",
})
_OUTPUT_WRITE_TOOLS = frozenset({"create_file", "append_to_file", "edit_file", "insert_to_file"})
_REPARSE_POINT_ATTRIBUTE = 0x400
_WINDOWS_RESERVED_NAMES = frozenset({
    "con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10)),
})


def _clean_output_relative(item: str) -> str | None:
    """Normalised ``a/b.md`` form of a workspace-relative file name, or ``None``.

    Rejects rooted/drive/UNC forms, ``..``, alternate data streams, Windows
    reserved device names and trailing dots/spaces, on every platform.
    """

    text = item.strip().replace("\\", "/")
    if not text or len(text) > 260 or "\x00" in text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        return None
    for part in parts:
        if (
            part == ".."
            or ":" in part
            or any(ord(char) < 32 or char in '<>"|?*' for char in part)
            or part != part.rstrip(". ")
            or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES
        ):
            return None
    return "/".join(parts)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (
        os.path.islink(path)
        or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE)
    )


def _output_file(root: Path, relative: str) -> tuple[Path | None, bool]:
    """Scope-valid target ``(path, safe)``; ``safe`` is False for any link/escape.

    No component below the workspace may be a symlink, junction or other
    reparse point, so the evidence read cannot leave the workspace.
    """

    try:
        resolved_root = root.resolve()
        current = resolved_root
        for part in relative.split("/"):
            current = current / part
            if _is_link_or_reparse(current):
                return None, False
        target = current.resolve()
    except (OSError, RuntimeError):
        return None, False
    return target, _path_within(target, resolved_root) and target != resolved_root


_OUTPUT_HASH_CHUNK = 1024 * 1024


def _output_digest(root: Path, relative: str, cancelled: Any = None) -> str:
    """Streamed sha256 of one output file.

    ``""`` means absent, ``"unsafe"`` a link/escape/non-regular file and
    ``"unverified"`` content that could not be read in full.  Memory is bounded
    by one chunk; ``cancelled`` is polled between chunks.  There is no
    size-and-mtime shortcut: content that cannot be hashed never validates.
    """

    target, safe = _output_file(root, relative)
    if not safe or target is None:
        return "unsafe"
    try:
        if not target.exists():
            return ""
        if not target.is_file():
            return "unsafe"
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            while True:
                if callable(cancelled) and cancelled():
                    raise AgentTaskInvocationError("cancelled", "Task cancellation was requested", 409)
                chunk = handle.read(_OUTPUT_HASH_CHUNK)
                if not chunk:
                    return digest.hexdigest()
                digest.update(chunk)
    except FileNotFoundError:
        return ""
    except OSError:
        return "unverified"


def _required_output_baseline(request: Mapping[str, Any], cancelled: Any = None) -> dict[str, str]:
    """Digests of every required output before the task acts (``""`` when absent)."""

    root = Path(str(request.get("workspace") or ""))
    return {item: _output_digest(root, item, cancelled) for item in request.get("required_outputs") or ()}


def _command_provenance(request: Mapping[str, Any], receipts: Sequence[Any]) -> bool:
    """A permitted command ran to a verified clean end (not background-only/uncertain)."""

    if request.get("allow_commands") is not True:
        return False
    return any(
        bool(getattr(receipt, "success", False))
        and str(getattr(receipt, "tool_name", "")).rsplit(".", 1)[-1].casefold() == "execute_command"
        and (
            _foreground_command_succeeded(receipt)
            or _completed_background_command_succeeded(receipt, receipts)
        )
        for receipt in receipts
    )


def _required_output_evidence(
    request: Mapping[str, Any], receipts: Sequence[Any], cancelled: Any = None
) -> dict[str, str | None]:
    """Per required output: ``"file_tool"``, ``"command_correlated"`` or ``None`` (not proven).

    An output is proven only when it is a safe, non-empty regular file whose
    streamed content digest differs from the baseline taken before the task's
    first model turn AND the task has write provenance.  ``file_tool`` means a
    successful file-tool write receipt named that exact path.
    ``command_correlated`` means only that a permitted command completed
    cleanly while the content changed: command receipts carry no filesystem
    delta, so this is a correlation, not proof of what the command wrote.  An
    unchanged pre-existing file (a rewrite with identical content, or a touch)
    never satisfies a create/change deliverable, and a missing baseline fails.
    """

    outputs = [str(item) for item in request.get("required_outputs") or ()]
    workspace = str(request.get("workspace") or "")
    baseline = request.get("required_output_baseline")
    baseline = baseline if isinstance(baseline, Mapping) else {}
    write_names = _OUTPUT_WRITE_TOOLS & _filesystem_mutation_receipt_names()
    written: set[str] = set()
    for receipt in receipts:
        if not bool(getattr(receipt, "success", False)):
            continue
        if str(getattr(receipt, "tool_name", "")).rsplit(".", 1)[-1].casefold() not in write_names:
            continue
        arguments = _receipt_mapping(receipt, "arguments")
        key = _workspace_path_key(str(arguments.get("path") or arguments.get("file_path") or ""), workspace)
        if key:
            written.add(key)
    by_command = _command_provenance(request, receipts) if outputs else False
    root = Path(workspace)
    evidence: dict[str, str | None] = {}
    for item in outputs:
        provenance = None
        if baseline.get(item) not in (None, "unsafe", "unverified"):
            via_tool = _workspace_path_key(item, workspace) in written
            if via_tool or by_command:
                target, safe = _output_file(root, item)
                current = _output_digest(root, item, cancelled)
                try:
                    nonempty = bool(safe and target is not None and target.is_file() and target.stat().st_size > 0)
                except OSError:
                    nonempty = False
                if nonempty and current not in ("", "unsafe", "unverified") and current != baseline[item]:
                    provenance = "file_tool" if via_tool else "command_correlated"
        evidence[item] = provenance
    return evidence


def _required_output_gaps(request: Mapping[str, Any], receipts: Sequence[Any], cancelled: Any = None) -> list[str]:
    """Required outputs not proven created or changed by this task."""

    return [item for item, provenance in _required_output_evidence(request, receipts, cancelled).items() if provenance is None]


def _successful_mutation_count(receipts: Sequence[Any]) -> int:
    """Successful non-command filesystem mutations; reads and commands are not progress."""

    names = _filesystem_mutation_receipt_names() - {"execute_command"}
    return sum(
        1 for receipt in receipts
        if bool(getattr(receipt, "success", False))
        and str(getattr(receipt, "tool_name", "")).rsplit(".", 1)[-1].casefold() in names
    )


def _correction_made_progress(
    gaps_before: Sequence[str], gaps_after: Sequence[str], rows_before: Sequence[Any], rows_after: Sequence[Any]
) -> bool:
    """True when a correction closed a required-output gap (saved before-snapshot vs now).

    A new file-tool mutation is secondary progress.  New reads, or a command
    that changed no required output, are not progress.
    """

    if len(set(gaps_after)) < len(set(gaps_before)):
        return True
    return _successful_mutation_count(rows_after) > _successful_mutation_count(rows_before)


def _scope_path_label(path: Any, workspace: Path) -> str:
    candidate = Path(path)
    try:
        relative = candidate.resolve().relative_to(workspace.resolve())
    except (OSError, ValueError):
        return str(candidate)
    return "." if str(relative) == "." else relative.as_posix()


def _runtime_capability_guidance(request: Mapping[str, Any], scope: Any) -> str:
    """Per-run notes built from the roots actually issued, appended to the task input.

    Informational only: every policy check, the failure ledger and ``executed``
    flags are unchanged.  It rides on the user turn (never the system prompt or
    tool schemas), so those stable prefixes are untouched, and it is derived
    from this run's scope alone, so nothing carries over between runs.  A write
    grant is never described as a delete grant; each line states exactly what
    the scope holds.
    """

    workspace = Path(str(request.get("workspace") or "."))
    write_roots = tuple(getattr(scope, "write_roots", ()) or ())
    delete_roots = tuple(getattr(scope, "delete_roots", ()) or ())
    scratch_roots = tuple(getattr(scope, "scratch_roots", ()) or ())
    # A scope defaults command_roots even when commands were never requested.
    command_roots = tuple(getattr(scope, "command_roots", ()) or ()) if request.get("allow_commands") is True else ()
    if not (write_roots or scratch_roots or command_roots):
        return ""

    def labels(roots: Sequence[Any]) -> str:
        return ", ".join(_scope_path_label(root, workspace) for root in roots)

    # The frozen admission-time file scope (never the prose of this turn).
    fixed = None
    if request.get("edit_scope") is not None:
        from ..llm.tool_policy import FixedEditScope, StructuredEditScopeError

        try:
            fixed = FixedEditScope.from_payload(request["edit_scope"])
        except StructuredEditScopeError:
            fixed = None
    targeted = fixed is not None and fixed.limited
    lines = ["", "", "Runtime capability notes for this run (from the issued scope; informational):"]
    if targeted and write_roots:
        if fixed.editable:
            lines.append(
                "- File tools can edit only these task files: "
                + ", ".join(fixed.editable)
                + ". An edit to any other file is denied; do not retry it."
            )
        else:
            lines.append("- This task grants no file edits; file-tool writes are denied.")
    else:
        lines.append(f"- File tools can write under: {labels(write_roots)}." if write_roots else "- Write locations for file tools: none.")
    if fixed is not None and fixed.protected:
        lines.append("- Read-only files: " + ", ".join(fixed.protected) + ".")
    if delete_roots and targeted:
        lines.append("- File deletion, where granted, applies only to those same task files.")
    elif delete_roots:
        lines.append(f"- File deletion is granted under: {labels(delete_roots)}.")
    else:
        lines.append(
            "- File deletion is outside this scope (a write grant covers writes alone); "
            "change a file's content with an edit or create instead of removing it."
        )
    if targeted:
        lines.append("- No scratch or probe files: create nothing outside the editable task files.")
    elif scratch_roots:
        lines.append(
            f"- Disposable probe files belong in the issued scratch location: {labels(scratch_roots)}; "
            "clean up there."
        )
    else:
        lines.append("- Scratch location: none issued, so work stays in the task files themselves.")
    if command_roots:
        lines.append(
            f"- Commands run within: {labels(command_roots)}. Output redirection with > or >> is outside what "
            "commands may do; take the command's stdout, or save content with a file tool."
        )
    return "\n".join(lines)


def _corrective_note(gaps: Sequence[str]) -> str:
    """The whole next user input of a corrective turn: the live history already holds the work."""

    lines = ["Continuation required: your last reply ended while these required output files were not yet created or changed:"]
    lines.extend(f"- {item}" for item in gaps)
    lines.append(
        "Do not repeat edits already applied. Create or change the missing files now with the registered tools, "
        "then report only what tool results confirm."
    )
    return "\n".join(lines)


def _native_history_usable(client: Any) -> bool:
    """The live provider history is non-empty and ends with the assistant's answer.

    A corrective turn continues that exact in-memory history (tool results
    unchanged).  When it is missing the task fails closed instead of
    regenerating blind.
    """

    manager = getattr(client, "history_manager", None)
    getter = getattr(manager, "get_model_messages", None)
    if not callable(getter):
        return False
    try:
        messages = getter()
    except Exception:
        return False
    return bool(messages) and isinstance(messages[-1], Mapping) and messages[-1].get("role") == "assistant"


_COMMAND_WORKSPACE_CHANGED = "changed"
_COMMAND_WORKSPACE_UNCHANGED = "unchanged"
_COMMAND_WORKSPACE_UNKNOWN = "unknown"
_WORKSPACE_OBSERVATION_COMPLETE = "complete"
_WORKSPACE_OBSERVATION_PARTIAL = "partial"
_WORKSPACE_OBSERVATION_UNAVAILABLE = "unavailable"
_WORKSPACE_OBSERVATION_MAX_ENTRIES = 20000
_WORKSPACE_OBSERVATION_MAX_BYTES = 64 * 1024 * 1024
_WORKSPACE_OBSERVATION_BUDGET_SECONDS = 5.0
_WORKSPACE_OBSERVATION_SKIP_DIRS = frozenset({".git"})
_WINDOWS_REPARSE_POINT = 0x400


def _hidden_subprocess_options() -> dict[str, Any]:
    """Windows no-console launch option for captured child processes.

    Frequent Git calls must not flash console windows and steal focus; other
    platforms keep the default launch behaviour.
    """

    if os.name != "nt":
        return {}
    return {"creationflags": int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)}


def _git_observed_paths(root: Path, deadline: float) -> list[str] | None:
    """Git tracked plus non-ignored untracked paths, or None when not a Git root.

    Only a workspace that is itself the repository top level is trusted to define
    its scope through Git; anything else uses the bounded full walk.
    """

    def run(*args: str) -> bytes | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            done = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True, timeout=remaining, check=False,
                stdin=subprocess.DEVNULL,
                **_hidden_subprocess_options(),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None

    top = run("rev-parse", "--show-toplevel")
    if top is None:
        return None
    try:
        if Path(top.decode("utf-8", "surrogateescape").strip()).resolve() != root.resolve():
            return None
    except (OSError, ValueError):
        return None
    listing = run("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listing is None or len(listing) > _WORKSPACE_OBSERVATION_MAX_BYTES:
        return None
    return [item.decode("utf-8", "surrogateescape") for item in listing.split(bytes([0])) if item]


_SCOPE_KIND_WORKSPACE = "workspace"
_SCOPE_KIND_OWNED_FILES = "owned_files"
_OWNED_SCOPE_MAX_PATHS = 256
_GLOB_CHARACTERS = frozenset("*?[]{}")


def _scope_identity_sha(kind: str, workspace: Any, paths: Sequence[str] = ()) -> str:
    """Digest of scope kind + canonical workspace + sorted paths (not content)."""

    try:
        canonical = os.path.normcase(str(Path(str(workspace)).resolve()))
    except (OSError, ValueError, RuntimeError):
        canonical = os.path.normcase(str(workspace))
    body = json.dumps(
        {"kind": kind, "workspace": canonical, "paths": sorted(paths)},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _owned_file_scope_paths(request: Mapping[str, Any]) -> tuple[str, ...] | None:
    """Exact files a mutate task owns, or None to keep whole-workspace observation.

    Built from the persisted, validated ``FixedEditScope`` (never the raw
    ``edit_targets``): only an explicit, enumerated, limited scope with at
    least one file qualifies.  ``required_outputs`` are unioned in for
    observation only; they never widen write authority.  An unspecified or
    empty scope is not a read-only proof, so it stays conservative, as does
    any entry that is not a plain relative file path.
    """

    if request.get("mode") != "mutate":
        return None
    try:
        scope = _edit_scope_for_request(request)
    except AgentTaskInvocationError:
        return None
    if not (scope.explicit and scope.limited) or scope.whole or scope.unenumerated or not scope.editable:
        return None
    paths: set[str] = set()
    for raw in (*scope.editable, *(request.get("required_outputs") or ())):
        text = str(raw or "").strip().replace("\\", "/")
        if (
            not text
            or text.endswith("/")
            or text.startswith("/")
            or "\x00" in text
            or ":" in text
            or any(ch in _GLOB_CHARACTERS for ch in text)
        ):
            return None
        normalized = posixpath.normpath(text)
        if normalized in {".", ".."} or normalized.startswith("../"):
            return None
        paths.add(normalized)
    if not paths or len(paths) > _OWNED_SCOPE_MAX_PATHS:
        return None
    return tuple(sorted(paths))


def _expected_owned_scope_sha(request: Mapping[str, Any]) -> str | None:
    paths = _owned_file_scope_paths(request)
    if paths is None:
        return None
    return _scope_identity_sha(_SCOPE_KIND_OWNED_FILES, request.get("workspace"), paths)


def _workspace_content_observation(
    workspace: Any,
    required_outputs: Sequence[str] = (),
    *,
    scope_paths: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Bounded content observation of the task workspace at one instant.

    With ``scope_paths`` only those exact owned files are observed (full
    bytes, link/reparse rejection, missing recorded as deletion, same budgets);
    a directory or unobservable entry makes the observation ``partial``.  The
    result carries ``scope_kind``/``scope_sha256`` so equal digests are only
    comparable inside the same scope.  This says the owned files are unchanged,
    not that the repository is.

    Scope: in a Git workspace root, the tracked plus non-ignored untracked files
    unioned with the explicit ``required_outputs`` (observed even when ignored);
    otherwise a bounded full walk.  Dependency/cache trees that the repository
    itself ignores (``node_modules``, ``venv``, ``.local``) are therefore out of
    scope, while tracked source and required outputs can never be dropped by
    directory name or caller input.  Content of every in-scope regular file is
    hashed in full; a missing in-scope path is a recorded deletion.

    ``state`` is ``complete`` only when the whole scope was observed within the
    entry/byte/time budgets.  Any cap hit, read or directory error, Git failure,
    required output that escapes the workspace, or an unobservable entry
    (junction/reparse point, special file, non-Git symlink) yields ``partial``
    with no digest, which callers treat as *unproven*, never *unchanged*.
    ``.git`` bookkeeping is skipped.  This is not whole-OS isolation: writes
    outside the observed scope are not seen.
    """

    scope_kind = _SCOPE_KIND_OWNED_FILES if scope_paths is not None else _SCOPE_KIND_WORKSPACE
    observation: dict[str, Any] = {
        "state": _WORKSPACE_OBSERVATION_UNAVAILABLE,
        "digest": "",
        "files": 0,
        "scope_kind": scope_kind,
        "scope_sha256": _scope_identity_sha(scope_kind, workspace, scope_paths or ()),
    }
    if not str(workspace or "").strip():
        return observation
    try:
        root = Path(str(workspace))
        if not root.is_dir():
            return observation
    except (OSError, ValueError):
        return observation

    nul = bytes([0])
    deadline = time.monotonic() + _WORKSPACE_OBSERVATION_BUDGET_SECONDS
    digest = hashlib.sha256()
    entries = 0
    files = 0
    hashed_bytes = 0
    complete = True
    walk_errors: list[OSError] = []

    def over_budget() -> bool:
        return entries > _WORKSPACE_OBSERVATION_MAX_ENTRIES or time.monotonic() > deadline

    def record_file(relative: str, *, allow_link: bool) -> bool:
        nonlocal files, hashed_bytes
        path = os.path.join(root, relative)
        digest.update(b"f" + nul + relative.encode("utf-8", "surrogateescape") + nul)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            digest.update(b"missing" + nul)
            return True
        except OSError:
            return False
        if bool(getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT):
            return False
        if stat.S_ISLNK(info.st_mode):
            if not allow_link:
                return False
            try:
                digest.update(b"link" + nul + os.readlink(path).encode("utf-8", "surrogateescape") + nul)
            except OSError:
                return False
            return True
        if stat.S_ISDIR(info.st_mode):
            digest.update(b"dir" + nul)  # submodule / gitlink entry
            return True
        if not stat.S_ISREG(info.st_mode):
            return False
        try:
            with open(path, "rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    hashed_bytes += len(chunk)
                    if hashed_bytes > _WORKSPACE_OBSERVATION_MAX_BYTES or time.monotonic() > deadline:
                        return False
                    digest.update(hashlib.sha256(chunk).digest())
        except OSError:
            return False
        files += 1
        digest.update(nul)
        return True

    def unobservable(path: str) -> bool:
        try:
            info = os.lstat(path)
        except OSError:
            return True
        return bool(getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT) or not (
            stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
        )

    try:
        required: list[str] = []
        root_resolved = root.resolve()
        for item in required_outputs or ():
            try:
                required.append((root / str(item)).resolve().relative_to(root_resolved).as_posix())
            except ValueError:
                complete = False
        git_paths = _git_observed_paths(root, deadline) if complete and scope_paths is None else None
        if complete and scope_paths is not None:
            for relative in sorted(scope_paths):
                entries += 1
                target = root_resolved / relative
                try:
                    target.resolve().relative_to(root_resolved)
                except (OSError, ValueError, RuntimeError):
                    complete = False
                    break
                parent = root_resolved
                for part in Path(relative).parts[:-1]:
                    parent = parent / part
                    if os.path.lexists(parent) and unobservable(str(parent)):
                        complete = False
                        break
                if not complete or over_budget() or os.path.isdir(target) or not record_file(relative, allow_link=False):
                    complete = False
                    break
        elif complete and git_paths is not None:
            for relative in sorted({*git_paths, *required}):
                entries += 1
                if over_budget() or not record_file(relative, allow_link=True):
                    complete = False
                    break
        elif complete and scope_paths is None:
            for current, dirnames, filenames in os.walk(root, onerror=walk_errors.append):
                entries += 1 + len(dirnames)
                if walk_errors or over_budget():
                    complete = False
                    break
                dirnames[:] = sorted(name for name in dirnames if name not in _WORKSPACE_OBSERVATION_SKIP_DIRS)
                if any(unobservable(os.path.join(current, name)) for name in dirnames):
                    complete = False
                    break
                for name in dirnames:
                    relative_dir = Path(current, name).relative_to(root).as_posix()
                    digest.update(b"d" + nul + relative_dir.encode("utf-8", "surrogateescape") + nul)
                for name in sorted(filenames):
                    entries += 1
                    if over_budget() or not record_file(
                        Path(current, name).relative_to(root).as_posix(), allow_link=False
                    ):
                        complete = False
                        break
                if not complete:
                    break
    except (OSError, ValueError):
        complete = False

    observation["files"] = files
    if complete and not walk_errors:
        observation["state"] = _WORKSPACE_OBSERVATION_COMPLETE
        observation["digest"] = digest.hexdigest()
    else:
        observation["state"] = _WORKSPACE_OBSERVATION_PARTIAL
    return observation


def _command_workspace_evidence(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    *,
    background: bool = False,
) -> dict[str, Any]:
    """Derive the durable workspace-mutation evidence stored on a command receipt.

    ``changed`` and ``unchanged`` claim only that two complete bounded
    observations of the workspace were equal or not.  Every other combination is
    ``unknown``: a background launch keeps running after its receipt, so its
    completion cannot be observed here.
    """

    before_state = str((before or {}).get("state") or _WORKSPACE_OBSERVATION_UNAVAILABLE)
    after_state = str((after or {}).get("state") or _WORKSPACE_OBSERVATION_UNAVAILABLE)
    before_digest = str((before or {}).get("digest") or "")
    after_digest = str((after or {}).get("digest") or "")
    # Legacy (whole-workspace) evidence has NO scope field on either side.  Any
    # scope field at all demands a complete, well-formed, identical scope on
    # both sides; a missing or mismatched half is never downgraded to legacy.
    scope_fields = ("scope_kind", "scope_sha256")
    any_scope_field = any(
        field in observation
        for observation in (before or {}, after or {})
        for field in scope_fields
    )
    before_kind = (before or {}).get("scope_kind")
    after_kind = (after or {}).get("scope_kind")
    before_scope = (before or {}).get("scope_sha256")
    after_scope = (after or {}).get("scope_sha256")
    scope_valid = (
        all(isinstance(value, str) for value in (before_kind, after_kind, before_scope, after_scope))
        and bool(re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,63}", str(before_kind)))
        and before_kind == after_kind
        and bool(re.fullmatch(r"[0-9a-f]{64}", str(before_scope)))
        and before_scope == after_scope
    )
    scoped = any_scope_field and scope_valid
    same_scope = not any_scope_field or scope_valid
    if (
        not background
        and same_scope
        and before_state == _WORKSPACE_OBSERVATION_COMPLETE
        and after_state == _WORKSPACE_OBSERVATION_COMPLETE
        and before_digest
        and after_digest
    ):
        mutation = (
            _COMMAND_WORKSPACE_UNCHANGED
            if before_digest == after_digest
            else _COMMAND_WORKSPACE_CHANGED
        )
    else:
        mutation = _COMMAND_WORKSPACE_UNKNOWN
    evidence = {
        "command_workspace_mutation": mutation,
        "command_workspace_before_state": before_state,
        "command_workspace_after_state": after_state,
        "command_workspace_before_digest": before_digest[:64],
        "command_workspace_after_digest": after_digest[:64],
        "command_workspace_files": int((after or {}).get("files") or 0),
    }
    if scoped:
        evidence.update({
            "command_workspace_before_scope_kind": before_kind,
            "command_workspace_after_scope_kind": after_kind,
            "command_workspace_before_scope_sha256": before_scope,
            "command_workspace_after_scope_sha256": after_scope,
        })
    return evidence


def _command_workspace_mutation(receipt: Any, request: Mapping[str, Any] | None = None) -> str:
    """Return the recorded workspace evidence for one command receipt.

    Missing, malformed or partial evidence reads as ``unknown`` so that the
    completion gate fails closed instead of assuming the command was read-only.
    """

    metadata = _receipt_mapping(receipt, "result_metadata")
    state = str(metadata.get("command_workspace_mutation") or "").strip().casefold()
    if state == _COMMAND_WORKSPACE_CHANGED:
        return state
    if state == _COMMAND_WORKSPACE_UNCHANGED:
        # The label alone is never trusted: it needs complete before/after
        # observations with the same well-formed digest.
        before = str(metadata.get("command_workspace_before_digest") or "")
        after = str(metadata.get("command_workspace_after_digest") or "")
        if (
            metadata.get("command_workspace_before_state") == _WORKSPACE_OBSERVATION_COMPLETE
            and metadata.get("command_workspace_after_state") == _WORKSPACE_OBSERVATION_COMPLETE
            and re.fullmatch(r"[0-9a-f]{64}", before)
            and before == after
            and _receipt_scope_consistent(metadata, request)
        ):
            return state
    return _COMMAND_WORKSPACE_UNKNOWN


def _receipt_scope_consistent(metadata: Mapping[str, Any], request: Mapping[str, Any] | None) -> bool:
    """Whether an ``unchanged`` receipt's observation scope is trustworthy.

    A receipt without scope keys is legacy (whole-workspace) evidence.  With
    scope keys, both sides must name one scope; an owned-files scope must also
    equal the scope reconstructed from this task's persisted edit scope.
    """

    keys = (
        "command_workspace_before_scope_kind",
        "command_workspace_after_scope_kind",
        "command_workspace_before_scope_sha256",
        "command_workspace_after_scope_sha256",
    )
    if not any(key in metadata for key in keys):
        return True
    kind_before, kind_after, sha_before, sha_after = (metadata.get(key) for key in keys)
    if not all(isinstance(value, str) for value in (kind_before, kind_after, sha_before, sha_after)):
        return False
    if kind_before != kind_after or sha_before != sha_after or not re.fullmatch(r"[0-9a-f]{64}", sha_before):
        return False
    if kind_before == _SCOPE_KIND_WORKSPACE:
        return True
    if kind_before == _SCOPE_KIND_OWNED_FILES:
        expected = _expected_owned_scope_sha(request) if request is not None else None
        return expected is not None and expected == sha_before
    return False


def _command_tool_name(receipt: Any) -> str:
    """Normalized tool name, matching the naming used by the receipt gates."""

    return str(getattr(receipt, "tool_name", "")).rsplit(".", 1)[-1].casefold()


def _required_outputs_verdict(request: Mapping[str, Any], receipts: Sequence[Any]) -> tuple[bool, str]:
    if _required_output_gaps(request, receipts):
        return False, "task_required_output_missing"
    return True, "verified_tool_evidence"


def _completion_evidence_satisfied(request: Mapping[str, Any], receipts: Sequence[Any]) -> tuple[bool, str]:
    """Require a fresh successful command receipt after the last mutation.

    Mutation points are file-tool edits plus every command receipt that did not
    record a complete before/after workspace observation proving unchanged
    content (changed, unknown, partial, background launch, or no evidence).
    Verification must be a later successful command (or a completed background
    read whose launch-to-read window was proven unchanged), so the last command
    can never verify itself unless it is proven read-only.  Command text, exit
    status and stdout are never read as mutation evidence; nothing is replayed.
    Ordering inside one receipt (a command that both writes and validates) is
    not observable: such a command is a mutation point, not a verification.
    The before/after snapshots are not atomic and final artifacts are not bound
    to the verification digest: a write racing the observation window, or one
    outside the observed scope, is not detected, so this does not guarantee
    final-artifact freshness or delayed-write prevention.

    ``required_outputs`` is the only completion contract beyond tool receipts:
    see ``_required_output_gaps``.  Neither the goal text nor the model's final
    wording is parsed for deliverables or promises.
    """

    ledger = _tool_failure_ledger(receipts, request)
    successful = ledger.successful
    if not successful:
        return False, "task_no_successful_tool_receipt"
    if ledger.unresolved:
        return False, "task_unresolved_tool_failure"
    if request.get("mode") == "mutate" and not any(
        bool(getattr(receipt, "success", False))
        and str(getattr(receipt, "tool_name", "")).rsplit(".", 1)[-1].casefold()
        in _filesystem_mutation_receipt_names()
        for receipt in successful
    ):
        return False, "task_filesystem_mutation_receipt_required"
    if request.get("requires_command_verification"):
        # Mutation points: file-tool edits, and every command receipt whose own
        # bounded observation did not prove the workspace content unchanged
        # (changed, unknown, partial, background launch, or no evidence at all).
        # Success or exit status does not matter: a failed command can write too.
        last_mutation = max((
            index for index, receipt in enumerate(receipts)
            if (
                bool(getattr(receipt, "success", False))
                and _command_tool_name(receipt) != "execute_command"
                and _command_tool_name(receipt) in _filesystem_mutation_receipt_names()
            )
            or (
                _command_tool_name(receipt) == "execute_command"
                and _command_workspace_mutation(receipt, request) != _COMMAND_WORKSPACE_UNCHANGED
            )
        ), default=-1)
        # Verification must come after that point and itself be a command (or a
        # completed background read) proven not to have changed the workspace.
        for index in range(last_mutation + 1, len(receipts)):
            receipt = receipts[index]
            if not bool(getattr(receipt, "success", False)):
                continue
            if _command_tool_name(receipt) == "execute_command":
                if _foreground_command_succeeded(receipt):
                    return _required_outputs_verdict(request, receipts)
            elif (
                _command_tool_name(receipt) == "read_command_output"
                and _command_workspace_mutation(receipt, request) == _COMMAND_WORKSPACE_UNCHANGED
                and any(
                    _completed_background_command_succeeded(launch, [launch, receipt])
                    for launch in receipts[:index]
                    if _command_tool_name(launch) == "execute_command"
                )
            ):
                return _required_outputs_verdict(request, receipts)
        return False, "task_foreground_command_verification_required"
    if _required_output_gaps(request, receipts):
        return False, "task_required_output_missing"
    return True, "verified_tool_evidence"


_FAILURE_LIST_LIMIT = 8
# Framework-authored refusals: authority/approval blocks and tool policy
# denials.  Neither ran the tool, so neither is a recoverable read failure.
_BLOCKED_RESULT_PREFIXES = ("Tool execution blocked `", "Tool policy blocked `")
_SAFE_FAILURE_CODE_RE = re.compile(r"[a-z][a-z0-9_]{2,63}")
_FAILURE_REASON_TAG_RE = re.compile(r"^Tool execution failed\. \(reason: ([a-z][a-z0-9_]{2,63})\)$")
_ERROR_CODE_FIELD_RE = re.compile(
    r"['\"]error_code['\"]\s*:\s*['\"]([a-z][a-z0-9_]{2,63})['\"]"
)


def _structured_error_code(*values: Any) -> str:
    """Return a structured error_code field; never a free-form ``(reason:)`` tag."""

    for value in values:
        if isinstance(value, Mapping):
            candidate = str(value.get("error_code") or "").strip()
            if _SAFE_FAILURE_CODE_RE.fullmatch(candidate):
                return candidate
        elif isinstance(value, str) and value.strip():
            tagged = _ERROR_CODE_FIELD_RE.search(value)
            if tagged:
                return tagged.group(1)
    return ""


def _recoverable_read_tool_names() -> frozenset[str]:
    """Registered read-only tools whose executed failure is recoverable.

    A failed read leaves no side effect, so the model may continue with an
    alternative. Anything in a mutation family (including ``execute_command``)
    is excluded even if it also appears in a read family.
    """

    from ..llm import tool_policy

    read = (
        tool_policy.FILESYSTEM_READ_TOOL_NAMES
        | tool_policy.DOCS_READ_TOOL_NAMES
        | tool_policy.SEARCH_TOOL_NAMES
        | tool_policy.PROJECT_MANAGEMENT_READ_TOOL_NAMES
        | tool_policy.OPERATIONS_READ_TOOL_NAMES
        | tool_policy.MEDIA_OPERATIONS_READ_TOOL_NAMES
        | {"read_command_output"}
    )
    mutation = (
        tool_policy.FILESYSTEM_MUTATION_TOOL_NAMES
        | tool_policy.COMMAND_TOOL_NAMES
        | tool_policy.DOCS_MUTATION_TOOL_NAMES
        | tool_policy.PROJECT_MANAGEMENT_MUTATION_TOOL_NAMES
        | tool_policy.OPERATIONS_MUTATION_TOOL_NAMES
        | tool_policy.MEDIA_OPERATIONS_MUTATION_TOOL_NAMES
    )
    return frozenset(
        str(name).rsplit(".", 1)[-1].casefold() for name in read - mutation
    )


def _receipt_was_blocked(receipt: Any) -> bool:
    text = str(getattr(receipt, "result", "") or "")
    return text.startswith(_BLOCKED_RESULT_PREFIXES) and "`: " in text


def _recoverable_tool_failure(receipt: Any, tool_name: str) -> bool:
    """An executed read-only failure never blocks completion by itself.

    Refusals (authority, policy, approval, circuit breaker) are recorded with
    ``executed=false`` or a blocked result and always stay unresolved.
    """

    if tool_name not in _recoverable_read_tool_names() or _receipt_was_blocked(receipt):
        return False
    return _receipt_mapping(receipt, "result_metadata").get("executed") is True


def _failure_reason_code(receipt: Any) -> str:
    """Return only a code-shaped reason; never free-form tool output."""

    metadata = _receipt_mapping(receipt, "result_metadata")
    payload = _receipt_result_payload(receipt)
    error = str(metadata.get("error") or "").strip()
    candidates = (
        error,
        str(metadata.get("error_code") or "").strip(),
        str(payload.get("error_code") or "").strip(),
        _structured_error_code(payload, getattr(receipt, "result", None)),
    )
    for candidate in candidates:
        if _SAFE_FAILURE_CODE_RE.fullmatch(candidate):
            return candidate
    # Runtime/tool failures may carry an opted-in fixed reason tag.
    # Free-form ``(reason: secret)`` in tool bodies is intentionally ignored.
    for text in (error, _receipt_result_text(receipt).strip()):
        tagged = _FAILURE_REASON_TAG_RE.search(text)
        if tagged:
            return tagged.group(1)
    return ""


def _tool_failure_entry(receipt: Any, tool_name: str) -> dict[str, Any]:
    text = str(getattr(receipt, "result", "") or "")
    metadata = _receipt_mapping(receipt, "result_metadata")
    code = _failure_reason_code(receipt)
    receipt_id = getattr(receipt, "id", None)
    entry: dict[str, Any] = {
        "tool": tool_name[:80],
        "denied": False,
        "reason": None,
        "receipt_id": str(receipt_id) if receipt_id is not None else None,
    }
    if _receipt_was_blocked(receipt):
        entry["denied"] = True
        entry["reason"] = text.split("`: ", 1)[1].splitlines()[0][:160]
    elif metadata.get("executed") is False:
        # Refused before execution, e.g. ``agent_authority_denied``.
        entry["denied"] = True
        provenance = metadata.get("argument_rejection")
        recorded = str(provenance.get("code") or "") if isinstance(provenance, Mapping) else ""
        entry["reason"] = code or (
            (recorded if _SAFE_FAILURE_CODE_RE.fullmatch(recorded) else "tool_argument_rejected")
            if _argument_rejected_before_execution(receipt)
            else "tool_not_executed"
        )
    else:
        nonzero = "command_nonzero_exit" if _receipt_command_nonzero_exit(receipt) is not None else ""
        entry["reason"] = code or nonzero or "tool_execution_failed"
    return entry


@dataclass(frozen=True)
class _ToolFailureLedger:
    successful: tuple[Any, ...]
    recovered: tuple[dict[str, Any], ...]
    unresolved: tuple[dict[str, Any], ...]
    diagnostic: tuple[dict[str, Any], ...] = ()


def _normalise_receipt_path(value: Any) -> str:
    path = _unescape_receipt_fragment(str(value or "")).strip().strip("`'\"").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    path = path.rstrip("/")
    # A doubled separator (``D:/Dev//a``) is not a different file; a leading
    # ``//`` (UNC) is kept.
    path = path[:2] + re.sub(r"/{2,}", "/", path[2:])
    # Windows drive paths are case-insensitive; POSIX paths are compared as written.
    return path.casefold() if re.match(r"^[A-Za-z]:/", path) else path


def _is_absolute_receipt_path(path: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:/", path)) or path.startswith("/")


def _paths_are_same_file(left: Any, right: Any) -> bool:
    """True when two resolved absolute tool paths are the same file.

    Relative arguments and suffix matches are not accepted, so a same-named
    file in another folder never counts as the same target.
    """

    first = _normalise_receipt_path(left)
    second = _normalise_receipt_path(right)
    return bool(first) and first == second and _is_absolute_receipt_path(first)


_ESCAPED_DRIVE_PATH_RE = re.compile(r"[A-Za-z]:\\\\")
_ESCAPE_SEQUENCE_RE = re.compile(r"\\(u[0-9A-Fa-f]{4}|.)", re.DOTALL)
_SIMPLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", "'": "'", '"': '"'}


def _unescape_receipt_fragment(text: str, *, message: bool = False) -> str:
    """Undo string-literal escaping left in a receipt fragment.

    A Windows drive path never legitimately contains ``C:\\\\``, so that
    signature (or literal ``\\n`` with no real line break) marks text that is
    still escaped one or more times.
    """

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if len(token) == 5 and token[0] == "u":
            return chr(int(token[1:], 16))
        return _SIMPLE_ESCAPES.get(token, match.group(0))

    for _ in range(4):
        escaped = bool(_ESCAPED_DRIVE_PATH_RE.search(text)) or (
            # Only a whole tool message is known to end its path with a line
            # break; a bare relative path may contain ``\\n`` (``docs\\new.md``).
            message
            and "\n" not in text
            and "\\n" in text
            and not re.search(r"[A-Za-z]:\\(?!\\)", text)
        )
        if not escaped:
            break
        text = _ESCAPE_SEQUENCE_RE.sub(replace, text)
    return text


def _receipt_path_after(text: str, marker: str, end_marker: str | None = None) -> str:
    """Path the framework message names after ``marker`` (escaping removed)."""

    text = _unescape_receipt_fragment(str(text or ""), message=True)
    lowered = text.casefold()
    start = lowered.find(marker)
    if start < 0:
        return ""
    rest = text[start + len(marker):]
    if end_marker is not None:
        end = rest.casefold().find(end_marker)
        if end < 0:
            return ""
        rest = rest[:end]
    named = rest.splitlines()[0].strip() if rest.strip() else ""
    named = named.strip().strip("'\"`")
    if named.endswith("."):
        named = named[:-1].rstrip()
    return named


def _edited_file_path(receipt: Any) -> str:
    """Resolved path the edit tool reports in its success text."""

    return _receipt_path_after(_receipt_result_text(receipt), "file edited successfully:")


def _existing_file_create_paths(receipt: Any, tool_name: str) -> tuple[str, ...]:
    """Paths of a create that ran and stopped because that file already exists.

    Permission refusals and other create errors are not this case. The path in
    the framework message carries the resolved absolute path.
    """

    if tool_name != "create_file" or _receipt_was_blocked(receipt):
        return ()
    metadata = _receipt_mapping(receipt, "result_metadata")
    if metadata.get("executed") is not True:
        return ()
    text = "\n".join(
        part
        for part in (
            _receipt_result_text(receipt),
            str(metadata.get("error") or ""),
        )
        if str(part or "").strip()
    )
    named = _receipt_path_after(
        text, "file already exists:", "use edit_file to modify existing files"
    )
    return (named,) if _is_absolute_receipt_path(_normalise_receipt_path(named)) else ()


def _applied_edit_content(receipt: Any, tool_name: str) -> str:
    """Content a successful edit_file receipt shows was applied to its path.

    The argument supplies the replacement and the success text must contain it,
    so a bare success flag is not treated as the file's final content.
    """

    if tool_name != "edit_file" or not bool(getattr(receipt, "success", False)):
        return ""
    if _receipt_mapping(receipt, "result_metadata").get("executed") is False:
        return ""
    content = _receipt_mapping(receipt, "arguments").get("new_str")
    if not isinstance(content, str):
        return ""
    # The edit tool writes ``new_str.expandtabs()`` and echoes it that way.
    applied = next((line.strip() for line in content.expandtabs().splitlines() if line.strip()), "")
    text = _receipt_result_text(receipt)
    if not applied or (
        applied not in text
        and applied not in _unescape_receipt_fragment(text, message=True)
    ):
        return ""
    return content


def _forget_pending_index(pending: dict[tuple[str, str], list[Any]], index: int) -> None:
    for key, items in list(pending.items()):
        kept = [item for item in items if item[0] != index]
        if kept:
            pending[key] = kept
        else:
            pending.pop(key, None)


def _requested_target_text(request: Mapping[str, Any] | None) -> str:
    """Task prose that names requested files, without the workspace root line."""

    if not request:
        return ""
    if request.get("goal"):
        return "\n".join(
            [
                str(request.get("goal") or ""),
                *(str(item) for item in request.get("constraints") or ()),
                *(str(item) for item in request.get("completion_criteria") or ()),
            ]
        )
    prompt = str(request.get("prompt") or "")
    return prompt.split("\nWorkspace root:", 1)[0]


def _requested_target_paths(request: Mapping[str, Any] | None) -> tuple[str, ...]:
    text = _requested_target_text(request)
    if not text.strip():
        return ()
    from ..llm.tool_policy import _task_path_tokens

    return tuple(sorted(_task_path_tokens(text)))


def _workspace_path_key(path: str, workspace: str) -> str:
    value = str(path or "").strip().strip("`'\"")
    if not value:
        return ""
    candidate = Path(value)
    if not candidate.is_absolute():
        if not workspace:
            return os.path.normpath(value).replace("\\", "/").casefold()
        candidate = Path(workspace) / candidate
    return os.path.normcase(os.path.normpath(str(candidate))).replace("\\", "/")


def _receipt_target_matches(receipt: Any, targets: Sequence[str], workspace: str) -> tuple[str, ...]:
    """Requested targets this receipt's path names exactly (bare names: by file name in the workspace)."""

    if not targets:
        return ()
    arguments = _receipt_mapping(receipt, "arguments")
    raw = str(arguments.get("path") or arguments.get("file_path") or "")
    key = _workspace_path_key(raw, workspace)
    if not key:
        return ()
    root = _workspace_path_key(".", workspace) if workspace else ""
    matched: list[str] = []
    for token in targets:
        token_key = _workspace_path_key(token, workspace)
        if token_key and token_key == key:
            matched.append(token)
        elif (
            "/" not in token
            and key.rsplit("/", 1)[-1].casefold() == token.casefold()
            and (not root or key.startswith(root.rstrip("/") + "/"))
        ):
            matched.append(token)
    return tuple(matched)


def _tool_failure_ledger(
    receipts: Sequence[Any], request: Mapping[str, Any] | None = None
) -> _ToolFailureLedger:
    """Separate recoverable intermediate tool failures from unresolved ones.

    A failure is recovered when a later receipt for the same action succeeded,
    or when it is an executed read-only failure. An executed create_file that
    failed only because the target file already exists is also recovered by a
    later successful edit_file of that same file whose receipt shows the
    applied content. A different file, a refused create, or an edit that does
    not carry that content does not recover it. Write, command and refused
    failures otherwise stay unresolved until the same action succeeds.

    A read failure on a file the task names is not recoverable by unrelated
    progress (for example a ``list_directory``): it stays unresolved until a
    later successful receipt names that same file.  Receipts themselves are
    never changed.
    """

    targets = _requested_target_paths(request)
    workspace = str((request or {}).get("workspace") or "")
    successful: list[Any] = []
    pending: dict[tuple[str, str], list[Any]] = {}
    pending_existing_creates: list[tuple[int, Any, tuple[str, ...]]] = []
    pending_target_reads: list[tuple[int, Any, tuple[str, ...]]] = []
    recovered: list[tuple[int, Any, str]] = []
    for index, receipt in enumerate(receipts):
        key = _receipt_action_key(receipt)
        if bool(getattr(receipt, "success", False)):
            successful.append(receipt)
            from ..tools.core import argument_target_digest

            retry_target = argument_target_digest(_receipt_mapping(receipt, "arguments"))
            rejected = []
            rejection_key = (key[0], f"{_ARGUMENT_REJECTION_KEY}{retry_target}")
            if retry_target:
                retry_arguments = _receipt_mapping(receipt, "arguments")
                candidates = pending.pop(rejection_key, [])
                rejected = [
                    item for item in candidates
                    if _retry_answers_rejection(item[1], retry_arguments, key[0])
                ]
                kept = [item for item in candidates if item not in rejected]
                if kept:
                    pending[rejection_key] = kept
            for earlier_index, earlier in [*pending.pop(key, []), *rejected]:
                recovered.append((earlier_index, earlier, key[0]))
                pending_existing_creates = [
                    item for item in pending_existing_creates if item[0] != earlier_index
                ]
                pending_target_reads = [
                    item for item in pending_target_reads if item[0] != earlier_index
                ]
            named = _receipt_target_matches(receipt, targets, workspace)
            if named:
                still_unread: list[tuple[int, Any, tuple[str, ...]]] = []
                for earlier_index, earlier, tokens in pending_target_reads:
                    if set(tokens) & set(named):
                        recovered.append(
                            (earlier_index, earlier, _receipt_action_key(earlier)[0])
                        )
                        _forget_pending_index(pending, earlier_index)
                    else:
                        still_unread.append((earlier_index, earlier, tokens))
                pending_target_reads = still_unread
            applied = _applied_edit_content(receipt, key[0])
            edit_path = _edited_file_path(receipt) if applied else ""
            if edit_path:
                still_pending: list[tuple[int, Any, tuple[str, ...]]] = []
                for earlier_index, earlier, paths in pending_existing_creates:
                    if any(_paths_are_same_file(edit_path, path) for path in paths):
                        recovered.append((earlier_index, earlier, "create_file"))
                        _forget_pending_index(pending, earlier_index)
                    else:
                        still_pending.append((earlier_index, earlier, paths))
                pending_existing_creates = still_pending
        elif _recoverable_tool_failure(receipt, key[0]):
            named = _receipt_target_matches(receipt, targets, workspace)
            if named:
                pending.setdefault(key, []).append((index, receipt))
                pending_target_reads.append((index, receipt, named))
            else:
                recovered.append((index, receipt, key[0]))
        else:
            pending.setdefault(key, []).append((index, receipt))
            create_paths = _existing_file_create_paths(receipt, key[0])
            if create_paths:
                pending_existing_creates.append((index, receipt, create_paths))
    def _latest_per_scope(items: list) -> list:
        # Distinct rejected scopes (query/range) stay separately unresolved.
        latest: dict[tuple[str, ...], tuple[int, Any]] = {}
        for index, receipt in items:
            latest[_rejection_scope_signature(receipt)] = (index, receipt)
        return list(latest.values())

    unresolved = sorted(
        (
            (index, receipt, key[0])
            for key, items in pending.items()
            for index, receipt in _latest_per_scope(items)
        ),
        key=lambda item: item[0],
    )
    # A launched foreground command that exited nonzero is a settled
    # diagnostic observation (failing tests, ``grep`` exit 1, a negative test).
    # It stays visible but cannot by itself make completion impossible;
    # command-start failures, timeouts and refusals remain unresolved.
    diagnostic = [
        item for item in unresolved if _receipt_command_nonzero_exit(item[1]) is not None
    ]
    unresolved = [item for item in unresolved if item not in diagnostic]
    return _ToolFailureLedger(
        successful=tuple(successful),
        recovered=tuple(
            _tool_failure_entry(receipt, name)
            for _, receipt, name in sorted(recovered, key=lambda item: item[0])
        ),
        unresolved=tuple(_tool_failure_entry(receipt, name) for _, receipt, name in unresolved),
        diagnostic=tuple(_tool_failure_entry(receipt, name) for _, receipt, name in diagnostic),
    )


def _tool_failure_summary(ledger: _ToolFailureLedger) -> dict[str, Any]:
    return {
        "recovered_count": len(ledger.recovered),
        "recovered": [dict(item) for item in ledger.recovered[:_FAILURE_LIST_LIMIT]],
        "unresolved_count": len(ledger.unresolved),
        "unresolved": [dict(item) for item in ledger.unresolved[:_FAILURE_LIST_LIMIT]],
        "diagnostic_count": len(ledger.diagnostic),
        "diagnostic": [dict(item) for item in ledger.diagnostic[:_FAILURE_LIST_LIMIT]],
    }


def _failure_detail(
    reason: str, receipts: Sequence[Any], request: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Secret-free failure summary: reason, unresolved tools and kept progress.

    Only tool names, receipt ids and the framework-authored reason code are
    exposed; tool arguments and free-form tool output may carry secrets and
    are never copied.
    """

    ledger = _tool_failure_ledger(receipts, request)
    successful_tools: list[str] = []
    for receipt in ledger.successful:
        name = _receipt_action_key(receipt)[0]
        if name not in successful_tools:
            successful_tools.append(name)
    detail: dict[str, Any] = {
        "reason": reason,
        "unresolved_tools": [dict(item) for item in ledger.unresolved[:_FAILURE_LIST_LIMIT]],
        "successful_tool_count": len(ledger.successful),
        "successful_tools": successful_tools[:_FAILURE_LIST_LIMIT],
    }
    if reason == "task_required_output_missing" and request:
        detail["missing_required_outputs"] = _required_output_gaps(request, receipts)
    return detail


def _filesystem_mutation_receipt_names() -> frozenset[str]:
    """Return the current registered filesystem mutation family."""

    from ..llm.tool_policy import FILESYSTEM_MUTATION_TOOL_NAMES

    return frozenset(str(name).rsplit(".", 1)[-1].casefold() for name in FILESYSTEM_MUTATION_TOOL_NAMES)


def _route_projection(route: Any) -> dict[str, str]:
    return {name: str(getattr(route, name, "") or "") for name in ("provider", "model", "effort")}


def _pins_with_execution_selection(config: Any, pins: Any, execution_profile_id: Any) -> Any:
    """Apply the shared server ExecutionProfile selection to revision pins."""

    from .agent_model_routing import AgentModelRouteError, resolve_agent_execution_selection

    try:
        return pins.with_execution_selection(
            resolve_agent_execution_selection(config, pins, execution_profile_id)
        )
    except AgentModelRouteError as exc:
        raise AgentTaskInvocationError(exc.code, "The requested ExecutionProfile is not allowed", 422) from None


def _assert_api_route(route: Any, expected: Mapping[str, str], *, explicit: bool = False) -> dict[str, str]:
    values = _route_projection(route)
    provider = values["provider"].casefold()
    backend = str(getattr(route, "backend", "") or "").casefold()
    if explicit:
        # An explicit ExecutionProfile must land on a registered API adapter;
        # the provider-name suffix alone is never trusted.
        from .agent_model_routing import AgentModelRouteError, assert_registered_route_backend

        try:
            assert_registered_route_backend(provider, backend, ingress="api")
        except AgentModelRouteError as exc:
            raise AgentTaskInvocationError(exc.code, "The selected route has no registered API adapter", 409) from None
    if backend != "api" or provider.endswith("-cli"):
        raise AgentTaskInvocationError("cli_route_not_supported", "CLI provider routes are owned by the CLI session API", 409)
    if not values["provider"] or not values["model"]:
        raise AgentTaskInvocationError("agent_route_unavailable", "The pinned Agent route is unavailable", 409)
    for key, supplied in expected.items():
        current = values[key].casefold() if key == "provider" else values[key]
        wanted = supplied.casefold() if key == "provider" else supplied
        if current != wanted:
            raise AgentTaskInvocationError("agent_route_mismatch", "Expected route does not match the pinned active AgentRevision", 409)
    return values


def _capability_model_entry(config: Any, provider: str, model: str) -> tuple[bool, bool | None]:
    """Return (registered, model-level supports_tools) without remote discovery."""

    from .llm_model_catalog import (
        PROVIDER_CAPABILITIES,
        agent_team_models_by_provider,
        provider_models,
    )

    provider_capability = PROVIDER_CAPABILITIES.get(provider)
    if provider_capability is None or getattr(provider_capability, "supports_tools", False) is not True:
        return False, False
    try:
        entries, _ = provider_models(provider, config, include_remote=False, include_runtime_status=False)
    except Exception:
        entries = []
    matching = [item for item in entries if str(item.get("id") or item.get("model") or "").strip() == model]
    team_models = agent_team_models_by_provider(config).get(provider, ())
    if any(str(item.get("id") or item.get("model") or "").strip() == model for item in team_models if isinstance(item, Mapping)):
        return True, None
    if not matching:
        return False, None
    metadata = matching[0]
    detail = metadata.get("details") if isinstance(metadata.get("details"), Mapping) else {}
    profile = metadata.get("runtime_profile") if isinstance(metadata.get("runtime_profile"), Mapping) else {}
    values = (metadata.get("supports_tools"), detail.get("supports_tools"), profile.get("supports_tools"))
    for value in values:
        if value is False:
            return True, False
        if value is True:
            return True, True
    return True, None


def _require_model_registry_entry(config: Any, provider: str, model: str) -> bool | None:
    registered, model_support = _capability_model_entry(config, provider, model)
    if not registered:
        raise AgentTaskInvocationError("tool_capability_unknown", "Tool-calling capability is unknown for the pinned provider/model", 409)
    if model_support is False:
        raise AgentTaskInvocationError("tool_calling_unsupported", "The pinned model does not support tool calling", 409)
    return model_support


def _ensure_registered_tools_support(
    config: Any,
    provider: str,
    model: str,
    *,
    client_supports_tools: bool | None = None,
) -> None:
    model_support = _require_model_registry_entry(config, provider, model)
    if model_support is not True and client_supports_tools is not True:
        raise AgentTaskInvocationError(
            "tool_capability_unknown",
            "Exact model capability metadata is missing and the target client has not confirmed tool support",
            409,
        )


def _install_task_command_policy(client: Any, request: Mapping[str, Any]) -> None:
    """Apply the request's narrow Git permissions inside the existing command policy."""

    if not request["allow_commands"]:
        return
    registry = getattr(client, "_tool_registry", None)
    definitions = getattr(registry, "_tools", None)
    definition = definitions.get("execute_command") if isinstance(definitions, Mapping) else None
    if definition is None:
        raise AgentTaskInvocationError("command_tool_unavailable", "The registered command tool is unavailable", 409)

    from dataclasses import replace
    from .local_agent_tasks import (
        LocalAgentTaskSpec,
        _git_add_paths_are_scoped,
        _git_index_paths,
        _git_worktree_paths,
        _git_write_command_details,
        check_command,
    )

    original = definition.function
    workspace = Path(request["workspace"])
    allow_git_commit = bool(request.get("allow_git_commit", False))
    baseline_staged = _git_index_paths(workspace) if allow_git_commit else set()
    baseline_dirty = _git_worktree_paths(workspace) if allow_git_commit else set()
    task_added_paths: set[str] = set()
    policy_spec = LocalAgentTaskSpec(
        goal="",
        workspace=str(request["workspace"]),
        mode=str(request["mode"]),
        allow_commands=True,
        allow_git_commit=allow_git_commit,
        allow_git_push=bool(request.get("allow_git_push", False)),
    )

    def guarded_execute_command(*args: Any, **kwargs: Any) -> Any:
        command = str(kwargs.get("command") or (args[0] if args else ""))
        denial = check_command(
            command,
            spec=policy_spec,
            workspace=workspace,
        )
        if denial:
            raise ToolPolicyDeniedError(denial)
        git_write = _git_write_command_details(command)
        add_paths: set[str] | None = None
        if git_write is not None:
            operation, rest = git_write
            cwd_value = kwargs.get("working_directory", args[1] if len(args) > 1 else None)
            if cwd_value is not None and str(cwd_value).strip() not in {"", "."}:
                cwd = Path(str(cwd_value))
                if not cwd.is_absolute():
                    cwd = workspace / cwd
                if os.path.normcase(os.path.realpath(cwd)) != os.path.normcase(os.path.realpath(workspace)):
                    raise ToolPolicyDeniedError("Git writes must run at the assigned repository root")
            if operation in {"add", "commit"}:
                if baseline_staged is None or baseline_dirty is None or baseline_staged:
                    raise ToolPolicyDeniedError("Git commit is blocked while pre-existing staged changes are present")
                if operation == "add":
                    add_paths = _git_add_paths_are_scoped(rest, workspace)
                    if add_paths is None or add_paths.intersection(baseline_dirty):
                        raise ToolPolicyDeniedError("Git add may include only clean task-scoped files")
                else:
                    staged_now = _git_index_paths(workspace)
                    if (
                        staged_now is None
                        or not staged_now
                        or staged_now.intersection(baseline_dirty)
                        or not staged_now.issubset(task_added_paths)
                    ):
                        raise ToolPolicyDeniedError("Git commit may include only files explicitly staged during this task")

        result = original(*args, **kwargs)
        if git_write is not None and git_write[0] == "add" and add_paths is not None:
            success = isinstance(result, Mapping) and result.get("success") is True
            if success:
                staged_now = _git_index_paths(workspace)
                if (
                    staged_now is None
                    or staged_now.intersection(baseline_dirty or set())
                    or not staged_now.issubset(task_added_paths | add_paths)
                ):
                    return {"success": False, "error": "Git add changed staged paths outside the task's explicit files"}
                task_added_paths.update(staged_now - (baseline_staged or set()))
        return result

    registry.register(replace(definition, function=guarded_execute_command))


def _task_concurrency_key(task_id: str) -> str:
    """Per-task admission fence: one task never runs twice, others never wait on it.

    The shared cwd is not a task identity.  Cross-task file safety is the edit
    scope plus the owned-file observation, not a workspace-wide admission lock.
    """

    return f"api_agent_task:{task_id}"


def _owner_uuid(value: Any) -> str:
    return _canonical_uuid(value, label="owner")


_RESTART_NO_PROGRESS_LIMIT = 2
_RESTART_NOTE_RECEIPT_LIMIT = 40
_RESTART_HISTORY_CHAR_LIMIT = 24_000
_RESTART_HISTORY_RESULT_LIMIT = 2_000
_ABSENT_FILE_DIGEST = "absent"
# Marks where a restart continuation attempt begins in the Run's events; re-marking
# a running Run emits no second ``run.started``.
_RESTART_ATTEMPT_EVENT = "api_agent_task.restart_attempt"


def _llm_timing_run_usage(run_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    """AgentRun usage carried by one completed provider request's timing event.

    Only a finished generation request with real counters yields usage.  The
    key is stable per request/round/attempt, so a replayed event is merged once
    by ``AgentRunService.record_event`` while a retried request stays distinct.
    """

    if raw.get("phase") != "end" or raw.get("stage") != "generation":
        return {}
    usage = safe_usage_snapshot(
        {key: raw.get(key) for key in ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens")}
    )
    if not usage:
        return {}
    request_id = str(raw.get("request_id") or "").strip()
    if not request_id:
        return {}
    ordinal = raw.get("sender_attempt_ordinal", raw.get("request_ordinal"))
    key = f"timing:{run_id}:{request_id}:{raw.get('round')}:{ordinal}"
    return {"usage": {k: int(v) for k, v in usage.items()}, "usage_key": key[:255]}


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return _ABSENT_FILE_DIGEST


def _file_mutation_fingerprint(tool: str, arguments: Mapping[str, Any], workspace: str) -> dict[str, str]:
    """Digests of an edit/create target taken immediately before the tool runs.

    ``file_before_sha256`` is the file's bytes (or ``absent``). The expected
    after-digest is computed with the file editor's own match resolution and
    encoder, only for a create of a missing file or an edit with exactly one
    match; otherwise it is omitted and the outcome stays unknown. Only the
    digests and the workspace-relative path are kept; never file content.
    """

    raw = arguments.get("path")
    if tool not in {"edit_file", "create_file"} or not workspace or not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        from ..security.agent_run_scope import get_current_run_scope

        root = Path(workspace).resolve()
        scope = get_current_run_scope()
        # Resolve exactly as the editor does inside the bound run scope.
        target = (
            Path(scope.assert_mutation_allowed(raw, "create" if tool == "create_file" else "edit"))
            if scope is not None
            else (Path(raw) if Path(raw).is_absolute() else root / raw)
        ).resolve()
        if not _path_within(target, root):
            return {}
        data: bytes | None = target.read_bytes()
    except FileNotFoundError:
        data = None
    except Exception:
        return {}
    fingerprint = {
        "file_path": target.relative_to(root).as_posix(),
        "file_before_sha256": hashlib.sha256(data).hexdigest() if data is not None else _ABSENT_FILE_DIGEST,
    }
    expected: bytes | None = None
    if tool == "create_file":
        content = arguments.get("content")
        if data is None and isinstance(content, str):
            expected = content.encode("utf-8")
    else:
        old, new = arguments.get("old_str"), arguments.get("new_str")
        if data is not None and isinstance(old, str) and isinstance(new, str) and old and old != new:
            # Run the editor's own match resolution and encoder on the bytes about
            # to be edited, so the digest is exactly what str_replace writes.
            from ..tools.os_operations import file_edit_helpers as edit
            from ..tools.os_operations.file_editor import FileEditor

            try:
                editor = FileEditor()
                text, encoding = editor._read_file_with_encoding(target)
                resolution = edit.resolve_matches(text, old, new)
                if len(resolution.matches) == 1:
                    match = resolution.matches[0]
                    expected = FileEditor._encode_like(
                        text[: match.start] + resolution.new + text[match.end :], encoding
                    )
            except Exception:
                expected = None
    if expected is not None:
        fingerprint["file_expected_after_sha256"] = hashlib.sha256(expected).hexdigest()
    return fingerprint


def _reconcile_file_mutation(start: Mapping[str, Any], workspace: str) -> str:
    """Compare the target's current bytes with the digests its tool_start recorded."""

    relative = start.get("file_path")
    before = start.get("file_before_sha256")
    expected = start.get("file_expected_after_sha256")
    if not (workspace and isinstance(relative, str) and relative and isinstance(before, str) and before):
        return "unknown"
    root = Path(workspace).resolve()
    try:
        target = (root / relative).resolve()
        if not _path_within(target, root):
            return "unknown"
        current = _file_digest(target)
    except (OSError, ValueError):
        return "unknown"
    if current == before:
        return "not_applied"
    if isinstance(expected, str) and current == expected:
        return "applied"
    return "unknown"


def _restart_recovery_plan(
    metadata: Mapping[str, Any],
    *,
    run_status: str,
    receipts: Sequence[Any],
    starts: Sequence[Mapping[str, Any]],
    confirmed: frozenset[str] = frozenset(),
) -> tuple[str, str, str, list[dict[str, Any]]]:
    """Decide how an interrupted task re-enters AgentWork.

    Completed operations are the Run's durable receipts. A ``tool_start``
    whose operation id has no receipt is in flight: a read (or any call of a
    read_only task) is safe to run again, an edit/create is settled by its
    recorded file digests, and anything else stays ``unknown``. An unknown
    operation continues only after the owner confirmed that exact operation
    id, and is then reported to the model as still unknown.
    """

    if metadata.get("cancellation_requested"):
        return "cancelled", "cancelled", "Task cancellation was requested before the restart", []
    if metadata.get("admission_state") != "ready" or run_status not in {"queued", "running", "awaiting_approval"}:
        return "uncertain", "interrupted_by_restart", "The interrupted API Agent Run cannot be continued", []
    if any(
        not bool(getattr(receipt, "success", False))
        and _tool_failure_entry(receipt, _receipt_action_key(receipt)[0])["denied"]
        for receipt in receipts
    ):
        # A denial is never worked around by a fresh model turn.
        return "blocked", "tool_policy_denied", "A task tool call was denied before the restart", []
    recorded = {str(getattr(receipt, "tool_call_id", "") or "") for receipt in receipts} - {""}
    reads = _recoverable_read_tool_names()
    workspace = str(metadata.get("workspace") or "")
    operations: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for start in starts:
        operation_id = str(start.get("operation_id") or start.get("tool_call_id") or "")[:160]
        if operation_id and operation_id in recorded:
            continue
        tool = str(start.get("tool") or "").rsplit(".", 1)[-1].casefold()[:80]
        if tool in reads or metadata.get("mode") != "mutate":
            # A read_only task holds no write capability; rerunning is safe.
            outcome = "read_retry"
        else:
            outcome = _reconcile_file_mutation(start, workspace)
            if outcome == "unknown" and operation_id and operation_id in confirmed:
                outcome = "unknown_confirmed"
        entry = {"tool": tool, "operation_id": operation_id, "outcome": outcome}
        arguments = start.get("tool_args") if isinstance(start.get("tool_args"), Mapping) else {}
        path = start.get("file_path") or arguments.get("path")
        if isinstance(path, str) and len(path) <= 500:
            entry["path"] = path
        operations.append(entry)
        if outcome == "unknown":
            unresolved.append(f"{tool or 'tool'} {operation_id or '(no operation id)'}")
    if unresolved:
        return (
            "awaiting_approval",
            "restart_reconciliation_required",
            "These operations have an unknown outcome after a restart; inspect the workspace and confirm "
            "their operation ids to continue: " + ", ".join(unresolved),
            operations,
        )
    return "retry_wait", "restart_continuation", "Continuing the interrupted task from its recorded outcomes", operations


def _restart_progress(
    metadata: Mapping[str, Any], receipts: Sequence[Any], operations: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Any], bool]:
    """Flat Work metadata for one restart, and whether restarts stopped making progress.

    A new receipt since the previous restart is progress, however many
    restarts happened. Only consecutive restarts with the same receipt count
    and the same interrupted operations count as no progress.
    """

    pending = sorted(str(item.get("operation_id") or item.get("tool") or "") for item in operations)
    unchanged = (
        "restart_receipt_count" in metadata
        and int(metadata.get("restart_receipt_count") or 0) == len(receipts)
        and list(metadata.get("restart_pending_operations") or ()) == pending
    )
    stalls = int(metadata.get("restart_no_progress") or 0) + 1 if unchanged else 0
    return {
        "restart_continuations": int(metadata.get("restart_continuations") or 0) + 1,
        "restart_receipt_count": len(receipts),
        "restart_pending_operations": pending,
        "restart_no_progress": stalls,
        "restart_operations": [dict(item) for item in operations],
    }, stalls > _RESTART_NO_PROGRESS_LIMIT


async def _requeue_for_continuation(
    session: Any,
    row: Any,
    coordinator: Any,
    *,
    now: datetime,
    run_id: Any,
    code: str,
    payload: Mapping[str, Any],
    actor_kind: str = "service",
    actor_id: str | None = None,
) -> None:
    """Return the Work to the queue with the same Run linked.

    The next claim re-checks source, authority and the task fences, and
    ``create_agent_run`` re-enters this non-terminal Run instead of a new one.
    """

    previous_state = str(row.state or "")
    row.state = "retry_wait"
    row.max_attempts = int(row.attempt_count or 0) + 1
    row.lease_owner = row.lease_token = row.lease_expires_at = None
    row.not_before = row.next_attempt_at = now
    row.outcome_classification = None
    row.completed_at = None
    row.safe_error_code = code
    row.blocker_code = None
    row.escalation_reason = None
    row.heartbeat_at = row.updated_at = now
    if isinstance(row.budget_reservation_json, Mapping) and row.budget_reservation_json:
        row.budget_reservation_json = coordinator._settle_budget(
            dict(row.budget_reservation_json),
            ExecutionOutcome(classification="transient"),
            state="retry_wait",
        )
    await coordinator._append_event_in_session(
        session,
        row,
        event_type="work.retry_wait",
        actor_kind=actor_kind,
        actor_id=actor_id or coordinator.worker_id,
        agent_run_id=run_id,
        from_state=previous_state,
        to_state="retry_wait",
        safe_error_code=code,
        payload={"error_code": code, **payload},
    )


def _restart_continuation_note(receipts: Sequence[Any], operations: Sequence[Mapping[str, Any]]) -> str:
    """Prompt section telling a resumed model turn what is already settled."""

    lines = [
        "\nRestart continuation:",
        "A service restart interrupted this task. Continue it in the same Run from the durable outcomes below "
        "and the recorded progress in the conversation history. Do not repeat a completed operation; its recorded "
        "result already counts. Re-read a file when you need its current content.",
    ]
    if receipts:
        lines.append("Completed operations:")
        for receipt in list(receipts)[-_RESTART_NOTE_RECEIPT_LIMIT:]:
            name, evidence = _receipt_action_key(receipt)
            status = "succeeded" if bool(getattr(receipt, "success", False)) else "failed"
            lines.append(f"- {name} {evidence[:200]} (operation {getattr(receipt, 'tool_call_id', None) or '-'}): {status}")
    outcomes = {
        "read_retry": "interrupted read; run it again if you still need it",
        "applied": "file digest proves it was applied; do not repeat it",
        "not_applied": "file digest proves it was not applied; apply it only if it is still required",
        "unknown_confirmed": (
            "outcome still unknown; the owner chose to continue. Inspect the workspace and confirm the actual "
            "state before relying on or repeating it"
        ),
        "unknown": "outcome unknown; inspect the workspace before repeating it",
    }
    interrupted = [item for item in operations if isinstance(item, Mapping)]
    if interrupted:
        lines.append("Operations interrupted before a recorded result:")
        for item in interrupted:
            target = f" {item['path']}" if item.get("path") else ""
            lines.append(
                f"- {item.get('tool')}{target} (operation {item.get('operation_id') or '-'}): "
                f"{outcomes.get(str(item.get('outcome')), outcomes['unknown'])}"
            )
    return "\n".join(lines)


def _restart_attempt_messages(events: Sequence[tuple[str, Any]], receipts: Sequence[Any]) -> list[dict[str, Any]]:
    """One interrupted attempt as provider-format model messages.

    Mid-run assistant text comes from the Run's ``assistant_text`` events;
    each tool call is an assistant ``tool_calls`` message paired with its
    ``tool`` result, taken from the secret-redacted receipts. An operation
    with no receipt is paired with an explicit interrupted-outcome result so
    the transcript stays valid. Hidden reasoning is never recorded. Newest
    units are kept within the bound.
    """

    by_operation = {
        str(receipt.tool_call_id): receipt for receipt in receipts if getattr(receipt, "tool_call_id", None)
    }
    units: list[list[dict[str, Any]]] = []
    pending_text = ""
    for index, (event_type, payload) in enumerate(events):
        if not isinstance(payload, Mapping):
            continue
        if event_type == "api_agent_task.corrective_turn":
            if pending_text:
                units.append([{"role": "assistant", "content": pending_text}])
                pending_text = ""
            missing = str(payload.get("missing") or "")[:1000]
            units.append([{
                "role": "user",
                "content": "Continuation required: these required output files are still not saved: " + missing,
            }])
        elif event_type == "api_agent_task.assistant_text" and isinstance(payload.get("text"), str):
            if pending_text:
                units.append([{"role": "assistant", "content": pending_text}])
            pending_text = payload["text"].strip()
        elif event_type == "api_agent_task.tool_start":
            operation_id = str(payload.get("operation_id") or payload.get("tool_call_id") or f"restart-op-{index}")[:160]
            name = str(payload.get("tool") or "tool")[:80]
            arguments = payload.get("tool_args") if isinstance(payload.get("tool_args"), Mapping) else {}
            receipt = by_operation.get(operation_id)
            if receipt is None:
                result = "No result was recorded for this call (for example a service restart came first); the outcome is unknown."
            else:
                result = _receipt_result_text(receipt).strip() or (
                    "succeeded" if bool(getattr(receipt, "success", False)) else "failed"
                )
                if len(result) > _RESTART_HISTORY_RESULT_LIMIT:
                    result = result[:_RESTART_HISTORY_RESULT_LIMIT].rstrip() + "\n... (truncated)"
            units.append([
                {
                    "role": "assistant",
                    "content": pending_text,
                    "tool_calls": [{
                        "id": operation_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(dict(arguments), ensure_ascii=False, default=str),
                        },
                    }],
                },
                {"role": "tool", "tool_call_id": operation_id, "name": name, "content": result},
            ])
            pending_text = ""
    if pending_text:
        units.append([{"role": "assistant", "content": pending_text}])
    kept: list[list[dict[str, Any]]] = []
    total = 0
    for unit in reversed(units):
        size = sum(len(str(message.get("content") or "")) + len(str(message.get("tool_calls") or "")) for message in unit)
        if total + size > _RESTART_HISTORY_CHAR_LIMIT:
            break
        kept.append(unit)
        total += size
    return [message for unit in reversed(kept) for message in unit]


class AgentTaskWorkSource:
    source_type = AGENT_TASK_SOURCE_TYPE

    async def discover(self, session: Any, *, now: datetime | None = None) -> Sequence[WorkCandidate]:
        return ()

    async def refresh(self, session: Any, claim: Any) -> bool:
        from ..memory.models import AgentWorkItem

        try:
            row = await session.get(AgentWorkItem, uuid.UUID(str(claim.work_item_id)))
            metadata = getattr(row, "metadata_json", None) if row is not None else None
            if not isinstance(metadata, Mapping):
                return False
            return bool(
                row.source_type == AGENT_TASK_SOURCE_TYPE
                and row.source_id == claim.source_id
                and row.execution_adapter == AGENT_TASK_ADAPTER_KEY
                and metadata.get("admission_state") == "ready"
                and metadata.get("admission_revision") == claim.source_revision
                and metadata.get("owner_user_id") == claim.metadata.get("owner_user_id")
                and metadata.get("session_id") == claim.conversation_session_id
                and str(row.assigned_agent_id or "") == str(claim.assigned_agent_id or "")
                and str(row.agent_revision_id or "") == str(claim.agent_revision_id or "")
                and tuple(row.required_capabilities_json or ()) == tuple(claim.required_capabilities)
            )
        except Exception:
            return False


class AgentTaskExecutionAdapter:
    adapter_key = AGENT_TASK_ADAPTER_KEY

    def __init__(self, service: "AgentTaskInvocationService") -> None:
        self.service = service

    def required_capabilities(self, claim: Any) -> tuple[str, ...]:
        return tuple(claim.required_capabilities)

    async def recover_stale(self, session: Any, row: Any, *, coordinator: Any, now: datetime) -> bool:
        if str(row.execution_adapter or "") != AGENT_TASK_ADAPTER_KEY or row.state not in {"claimed", "running"}:
            return False
        metadata = dict(row.metadata_json) if isinstance(row.metadata_json, Mapping) else {}
        run_id = row.active_agent_run_id
        run_status, receipts, starts = await self.service._restart_evidence(session, row.id, run_id)
        state, code, message, operations = _restart_recovery_plan(
            metadata, run_status=run_status, receipts=receipts, starts=starts
        )
        if state in {"retry_wait", "awaiting_approval"}:
            progress, stalled = _restart_progress(metadata, receipts, operations)
            row.metadata_json = {**metadata, **progress}
            if stalled:
                state, code, message = (
                    "uncertain",
                    "restart_no_progress",
                    "Repeated restarts recorded no new progress for this task",
                )
        if state == "retry_wait" and row.deadline is not None and row.deadline <= now:
            # The hard deadline is not extended by a restart.  Requeueing would
            # also put not_before past the deadline (a schedule-window CHECK
            # violation that rolls back, and so wedges, the whole recovery).
            # Settle like the claim-time deadline sweep; nothing is replayed.
            await coordinator._transition_locked(
                session, row, "blocked", token=None,
                error_code="deadline_expired", error_message="work item deadline has expired",
            )
            return True
        if state == "retry_wait":
            await _requeue_for_continuation(
                session,
                row,
                coordinator,
                now=now,
                run_id=run_id,
                code=code,
                payload={"recovered_after_restart": True, "restart_continuation": row.metadata_json["restart_continuations"]},
            )
            return True
        await coordinator._transition_locked(session, row, state, token=None, error_code=code, error_message=message)
        if state == "awaiting_approval":
            # Keep the Run open: settlement reconciliation projects it as
            # awaiting approval and the owner's resume re-enters it.
            row.active_agent_run_id = run_id
        return True

    async def execute(self, claim: Any, *, coordinator: Any, run_id: str, run: Mapping[str, Any], **_: Any) -> ExecutionOutcome:
        return await self.service.execute(claim, coordinator=coordinator, run_id=run_id, run=run)


_BUDGET_POLL_SECONDS = 0.05


async def _generate_within_budget(budget: Any, cancel_handle: Any, generate: Any) -> Any:
    """Await one provider call without letting it outlive the task budget.

    A provider call has no checkpoint between its awaits, so the SDK's own
    timeout (minutes) would otherwise bound it.  Budget expiry or a
    cancellation request ends the wait: the run's cancel flag is raised so
    late tool/worker mutations are fenced, and the call is cancelled and
    drained.  The budget's interruptible transport closes the client socket of
    a local HTTP call; that is not proof that a server/GPU or a tool side
    effect stopped, and the wrapper never assumes either.
    """

    budget.checkpoint()  # an expired or cancelled budget never starts a call
    call = asyncio.ensure_future(generate())
    try:
        while True:
            done, _ = await asyncio.wait({call}, timeout=_BUDGET_POLL_SECONDS)
            if done:
                result = call.result()
                # A result that completed while the budget expired or a cancel
                # arrived must not bypass it; the caller's durable receipts stay.
                budget.checkpoint()
                return result
            budget.checkpoint()
    except BaseException:
        if not call.done():
            if cancel_handle is not None:
                cancel_handle.request_cancel()
            call.cancel()
            while not call.done():
                try:
                    await asyncio.wait({call})
                except asyncio.CancelledError:
                    continue
        if call.done() and not call.cancelled():
            call.exception()
        raise


class AgentTaskInvocationService:
    def __init__(self, db_manager: Any, config: Any, coordinator: Any, *, client_builder: Any = None) -> None:
        self.db = db_manager
        self.config = config
        self.coordinator = coordinator
        self.client_builder = client_builder
        self._submit_lock = asyncio.Lock()
        self.source = AgentTaskWorkSource()
        self.adapter = AgentTaskExecutionAdapter(self)

    async def _session(self) -> Any:
        value = self.db.get_session()
        return await value if inspect.isawaitable(value) else value

    async def migrate_workspace_fence_keys(self, *, limit: int = 200) -> int:
        """Move queued tasks from the old cwd-wide key to their per-task key.

        Only an unleased ``pending`` task whose admission is ``ready`` and that
        still carries the legacy ``workspace:`` concurrency key is touched, under
        a row lock and a compare-and-set.  Claimed, running, awaiting-approval
        and uncertain work is left exactly as it is; identity, attempt, source,
        metadata, Run, deadline and cancellation are never rewritten.
        """

        from sqlalchemy import update

        from ..memory.models import AgentWorkItem

        session = await self._session()
        moved = 0
        try:
            rows = (await session.execute(
                select(AgentWorkItem)
                .where(
                    AgentWorkItem.source_type == AGENT_TASK_SOURCE_TYPE,
                    AgentWorkItem.execution_adapter == AGENT_TASK_ADAPTER_KEY,
                    AgentWorkItem.state == "pending",
                    AgentWorkItem.lease_owner.is_(None),
                    AgentWorkItem.lease_token.is_(None),
                    AgentWorkItem.concurrency_key.like("workspace:%"),
                )
                .order_by(AgentWorkItem.created_at)
                .limit(max(1, min(int(limit), 1000)))
                .with_for_update(skip_locked=True)
            )).scalars().all()
            for row in rows:
                metadata = row.metadata_json if isinstance(row.metadata_json, Mapping) else {}
                task_id = str(row.source_id or "")
                if metadata.get("admission_state") != "ready" or metadata.get("task_id") != task_id or not task_id:
                    continue
                if metadata.get("cancellation_requested"):
                    continue
                fence = await session.execute(
                    update(AgentWorkItem)
                    .where(
                        AgentWorkItem.id == row.id,
                        AgentWorkItem.state == "pending",
                        AgentWorkItem.lease_owner.is_(None),
                        AgentWorkItem.lease_token.is_(None),
                        AgentWorkItem.concurrency_key == row.concurrency_key,
                        AgentWorkItem.updated_at == row.updated_at,
                    )
                    .values(concurrency_key=_task_concurrency_key(task_id), updated_at=AgentWorkItem.updated_at)
                    .execution_options(synchronize_session=False)
                )
                moved += int(fence.rowcount or 0)
            await session.commit()
            return moved
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    def attach(self) -> None:
        if self.coordinator is None or not self.coordinator._runtime_enabled():
            raise AgentTaskInvocationError("agent_work_unavailable", "The shared AgentWork runtime is unavailable", 503)
        if self.coordinator.adapters.get(AGENT_TASK_ADAPTER_KEY) is not self.adapter:
            self.coordinator.register_source(self.source)
            self.coordinator.register_adapter(self.adapter)

    async def _authorise_caps(
        self,
        agent_id: str,
        revision_id: str,
        capabilities: tuple[str, ...],
        *,
        required: bool = True,
    ) -> tuple[str, ...]:
        from .agent_authority import AgentAuthorityResolver
        from .agent_team_v3 import AGENT_TEAM_CAPABILITY_CATALOG

        unknown = [item for item in capabilities if item not in AGENT_TEAM_CAPABILITY_CATALOG]
        if unknown:
            raise AgentTaskInvocationError("unknown_capability", "A requested capability is not registered", 422)
        resolver = AgentAuthorityResolver(self.db, config=self.config)
        authorized: list[str] = []
        for capability in capabilities:
            decision = await resolver.resolve(
                agent_id=agent_id,
                revision_id=revision_id,
                required_capability=capability,
                tool_capabilities=capabilities,
                harness_capabilities=capabilities,
            )
            if not bool(getattr(decision, "allowed", False)):
                if required:
                    raise AgentTaskInvocationError("permission_denied", "Current Agent authority does not permit this task scope", 403)
                continue
            authorized.append(capability)
        return tuple(authorized)

    async def _create_private_session(
        self,
        owner: str,
        task_id: str,
        *,
        work_id: str,
        request_fingerprint: str,
        admission_nonce: str,
    ) -> str:
        from ..memory.conversation_repository import ConversationRepository
        from ..memory.models import AgentWorkItem

        session = await self._session()
        try:
            async def link_admitting_work(db_session: Any, conversation: Any) -> None:
                work = (await db_session.execute(
                    select(AgentWorkItem)
                    .where(AgentWorkItem.id == uuid.UUID(work_id))
                    .with_for_update()
                )).scalars().first()
                metadata = work.metadata_json if work is not None and isinstance(work.metadata_json, Mapping) else {}
                if (
                    work is None
                    or work.state != "pending"
                    or metadata.get("admission_state") != "admitting"
                    or metadata.get("admission_nonce") != admission_nonce
                    or metadata.get("request_fingerprint") != request_fingerprint
                ):
                    raise AgentTaskInvocationError("agent_work_unavailable", "The task admission fence changed", 409)
                session_id = str(conversation.id)
                work.conversation_session_id = conversation.id
                work.metadata_json = {**dict(metadata), "session_id": session_id}
                work.updated_at = datetime.utcnow()

            row = await ConversationRepository(session).create_session(
                user_id=owner,
                character_name="",
                title=f"Agent task {task_id[:8]}",
                owner_participant={"participant_id": owner, "role": "owner"},
                initialize_agent=link_admitting_work,
            )
            return str(row.id)
        finally:
            await session.close()

    async def _find_work(self, task_id: str, *, owner: str | None = None) -> Any:
        from ..memory.models import AgentWorkItem

        session = await self._session()
        try:
            row = (await session.execute(
                select(AgentWorkItem).where(
                    AgentWorkItem.source_type == AGENT_TASK_SOURCE_TYPE,
                    AgentWorkItem.source_id == task_id,
                    AgentWorkItem.execution_adapter == AGENT_TASK_ADAPTER_KEY,
                ).limit(1)
            )).scalars().first()
            if row is None:
                raise AgentTaskInvocationError("not_found", "task not found", 404)
            metadata = row.metadata_json if isinstance(row.metadata_json, Mapping) else {}
            if owner is not None and metadata.get("owner_user_id") != owner:
                raise AgentTaskInvocationError("not_found", "task not found", 404)
            return row
        finally:
            await session.close()

    async def _task_projection(self, task_id: str, *, owner: str) -> dict[str, Any]:
        row = await self._find_work(task_id, owner=owner)
        metadata = row.metadata_json if isinstance(row.metadata_json, Mapping) else {}
        state = str(row.state or "pending")
        status = {
            "pending": "queued", "claimed": "running", "running": "running",
            "retry_wait": "waiting", "awaiting_approval": "waiting_approval",
            "succeeded": "completed", "failed": "failed", "dead_letter": "failed",
            "blocked": "failed", "cancelled": "cancelled", "uncertain": "interrupted",
        }.get(state, "waiting")
        if metadata.get("admission_state") != "ready" and state == "pending":
            status = "interrupted"
        receipts = (
            await self._run_receipts(row, metadata)
            if status in {"completed", "failed"}
            else ()
        )
        evidence_request = (
            await self._evidence_request(row, metadata)
            if status in {"completed", "failed"}
            else None
        )
        return {
            "task_id": task_id,
            "status": status,
            "work_item_id": str(row.id),
            "agent_run_id": str(metadata.get("agent_run_id") or row.active_agent_run_id or "") or None,
            "agent_id": str(row.assigned_agent_id or ""),
            "agent_revision_id": str(row.agent_revision_id or ""),
            "cancellation_requested": bool(
                metadata.get("cancellation_requested") and state in {"claimed", "running"}
            ),
            # Operations the owner must inspect and confirm before ``resume``.
            "reconciliation_required": (
                [
                    {key: item.get(key) for key in ("tool", "operation_id", "path")}
                    for item in metadata.get("restart_operations") or ()
                    if isinstance(item, Mapping) and item.get("outcome") == "unknown"
                ]
                if state == "awaiting_approval" and getattr(row, "safe_error_code", None) == "restart_reconciliation_required"
                else None
            ),
            "result": await self._final_result(row, owner=owner) if status == "completed" else None,
            "failure": (
                await self._failure_projection(row, receipts, owner=owner, request=evidence_request)
                if status == "failed"
                else None
            ),
            # Recovered vs unresolved tool failures; raw receipts stay intact.
            "tool_failures": (
                _tool_failure_summary(_tool_failure_ledger(receipts, evidence_request))
                if status in {"completed", "failed"}
                else None
            ),
        }

    async def _evidence_request(self, work: Any, metadata: Mapping[str, Any]) -> dict[str, Any] | None:
        """The prompt/workspace the completion gate judged, for the same ledger view."""

        from ..memory.models import AgentRun

        try:
            run_id = uuid.UUID(str(metadata.get("agent_run_id") or work.active_agent_run_id or ""))
        except (ValueError, TypeError):
            return None
        session = await self._session()
        try:
            run = await session.get(AgentRun, run_id)
            if run is None or str(run.work_item_id or "") != str(work.id):
                return None
            return {
                "prompt": str(getattr(run, "objective", "") or ""),
                "workspace": str(metadata.get("workspace") or ""),
                "mode": str(metadata.get("mode") or "read_only"),
                "allow_commands": bool(metadata.get("allow_commands")),
                "required_outputs": [str(item) for item in metadata.get("required_outputs") or ()],
                "required_output_baseline": await self._load_output_baseline(run_id) or {},
            }
        finally:
            await session.close()

    async def _run_receipts(self, work: Any, metadata: Mapping[str, Any]) -> Sequence[Any]:
        """Raw per-call receipts of the linked Run, in execution order."""

        try:
            run_id = uuid.UUID(str(metadata.get("agent_run_id") or work.active_agent_run_id or ""))
        except (ValueError, TypeError):
            return ()
        return await self._receipts_for_run(run_id)

    async def _receipts_for_run(self, run_id: Any, session: Any = None) -> Sequence[Any]:
        from ..memory.models import AgentRunToolCall

        owned = session is None
        session = await self._session() if owned else session
        try:
            return (await session.execute(
                select(AgentRunToolCall)
                .where(AgentRunToolCall.run_id == uuid.UUID(str(run_id)))
                .order_by(AgentRunToolCall.created_at, AgentRunToolCall.id)
            )).scalars().all()
        finally:
            if owned:
                await session.close()

    async def _run_attempt_events(self, run_id: Any, session: Any = None) -> list[list[tuple[str, Any]]]:
        """The Run's progress events split into attempts at each restart continuation.

        The Run event keeps the redacted tool arguments and file digests; the
        bounded Work event projection drops relative paths.
        """

        from ..memory.models import AgentRunEvent

        owned = session is None
        session = await self._session() if owned else session
        try:
            rows = (await session.execute(
                select(AgentRunEvent.event_type, AgentRunEvent.payload)
                .where(
                    AgentRunEvent.run_id == uuid.UUID(str(run_id)),
                    AgentRunEvent.event_type.in_((
                        _RESTART_ATTEMPT_EVENT, "api_agent_task.tool_start", "api_agent_task.assistant_text",
                        "api_agent_task.corrective_turn",
                    )),
                )
                .order_by(AgentRunEvent.sequence)
            )).all()
        finally:
            if owned:
                await session.close()
        attempts: list[list[tuple[str, Any]]] = [[]]
        for event_type, payload in rows:
            if event_type == _RESTART_ATTEMPT_EVENT:
                attempts.append([])
            else:
                attempts[-1].append((event_type, payload))
        return attempts

    async def _restart_evidence(
        self, session: Any, work_id: Any, run_id: Any
    ) -> tuple[str, Sequence[Any], list[dict[str, Any]]]:
        """Run status, durable receipts and the last attempt's tool starts, read in one transaction."""

        from ..memory.models import AgentRun

        run = await session.get(AgentRun, run_id) if run_id else None
        if run is None or str(run.work_item_id or "") != str(work_id):
            return "", (), []
        # Earlier attempts' interrupted operations were settled by their restart.
        last_attempt = (await self._run_attempt_events(run.id, session))[-1]
        starts = [
            dict(payload)
            for event_type, payload in last_attempt
            if event_type == "api_agent_task.tool_start" and isinstance(payload, Mapping)
        ]
        return str(run.status or ""), await self._receipts_for_run(run.id, session), starts

    async def _failure_projection(
        self,
        work: Any,
        receipts: Sequence[Any],
        *,
        owner: str,
        request: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        reason = str(work.safe_error_code or "agent_task_execution_failed")[:96]
        detail = _failure_detail(reason, receipts, request)
        # The Agent's last answer is kept even when the task goal is not met;
        # it is never promoted to ``result``.
        final_response = await self._linked_run_text(
            work, owner=owner, run_status="failed", key="final_response"
        )
        detail["final_response"] = final_response if final_response and final_response.strip() else None
        return detail

    async def _final_result(self, work: Any, *, owner: str) -> dict[str, Any] | None:
        """Expose only the final answer from the owner-scoped, linked successful Run.

        Work.result_summary_json is bounded metadata and can redact an entire code
        example or truncate the answer. The canonical Run retains the display text.
        Never project arbitrary Run result keys, prompts, or runtime trace fields.
        """
        response = await self._linked_run_text(work, owner=owner, run_status="succeeded", key="response")
        return {"response": response} if response is not None else None

    async def _linked_run_text(self, work: Any, *, owner: str, run_status: str, key: str) -> str | None:
        """Return one sanitized answer field from the owner-scoped linked Run."""

        from ..memory.models import AgentRun
        from .agent_run_service import sanitize_assistant_display_text

        metadata = work.metadata_json if isinstance(work.metadata_json, Mapping) else {}
        try:
            run_id = uuid.UUID(str(metadata.get("agent_run_id") or ""))
        except (ValueError, TypeError):
            return None
        session = await self._session()
        try:
            run = await session.get(AgentRun, run_id)
            if (
                run is None or run.status != run_status
                or str(run.user_id or "") != owner
                or str(run.work_item_id or "") != str(work.id)
                or str(run.session_id or "") != str(metadata.get("session_id") or "")
                or str(run.agent_id or "") != str(work.assigned_agent_id)
                or str(run.agent_revision_id or "") != str(work.agent_revision_id)
            ):
                return None
            result = run.result if isinstance(run.result, Mapping) else {}
            response = result.get(key)
            if not isinstance(response, str):
                return None
            return sanitize_assistant_display_text(response)
        finally:
            await session.close()

    async def get(self, task_id: str, *, owner: Any) -> dict[str, Any]:
        return await self._task_projection(task_id, owner=_owner_uuid(owner))

    async def submit(self, payload: Mapping[str, Any], *, owner: Any, actor_role: str, idempotency_key: str | None) -> tuple[dict[str, Any], bool]:
        owner_id = _owner_uuid(owner)
        request = _normalise_request(payload, self.config)
        key = idempotency_key
        if not isinstance(key, str) or not _IDEMPOTENCY_RE.fullmatch(key):
            raise AgentTaskInvocationError("invalid_idempotency_key", "Idempotency-Key is invalid", 422)
        fingerprint = _request_fingerprint(request)
        task_id = _task_id_for(owner_id, key)
        self.attach()
        async with self._submit_lock:
            try:
                existing = await self._find_work(task_id, owner=owner_id)
            except AgentTaskInvocationError as exc:
                if exc.code != "not_found":
                    raise
                existing = None
            if existing is not None:
                metadata = existing.metadata_json if isinstance(existing.metadata_json, Mapping) else {}
                if metadata.get("request_fingerprint") != fingerprint:
                    raise AgentTaskInvocationError("idempotency_conflict", "Idempotency-Key was already used for a different request", 409)
                return await self._task_projection(task_id, owner=owner_id), False

            from .agent_model_routing import AgentRoutingPins
            from .agent_run_service import AgentRunService, _binding_audit_projection
            from .conversation_agent_service import ConversationAgentService

            agents = ConversationAgentService(self.db, config=self.config)
            with _agent_selection_errors():
                agent, revision = await agents.validate_agent_selection(
                    agent_id=request["agent_id"],
                    actor_user_id=owner_id,
                    actor_role=actor_role,
                )
            pins = _pins_with_execution_selection(
                self.config,
                AgentRoutingPins.from_revision(revision),
                request.get("execution_profile_id"),
            )
            route = _resolve_task_operator_route(self.config, pins)
            resolved_route = _assert_api_route(route, request["expected_route"], explicit=request.get("execution_profile_id") is not None)
            ceiling = set(getattr(revision, "capability_ceiling_json", None) or ())
            if not ceiling:
                ceiling = set(getattr(revision, "capability_ceiling", None) or ())
            requested_capabilities = set(request["required_capabilities"])
            missing = requested_capabilities - ceiling
            if missing:
                raise AgentTaskInvocationError("permission_denied", "Requested task capabilities exceed the pinned AgentRevision ceiling", 403)
            from .agent_team_v3 import AGENT_TEAM_CAPABILITY_CATALOG

            unknown_capabilities = requested_capabilities - set(AGENT_TEAM_CAPABILITY_CATALOG)
            if unknown_capabilities:
                raise AgentTaskInvocationError("unknown_capability", "A requested capability is not registered", 422)
            registered_ceiling = ceiling & set(AGENT_TEAM_CAPABILITY_CATALOG)
            caps = set(await self._authorise_caps(
                request["agent_id"],
                str(revision.id),
                tuple(sorted(registered_ceiling)),
                required=False,
            ))
            missing = requested_capabilities - caps
            if missing:
                raise AgentTaskInvocationError("permission_denied", "Current Agent authority does not permit all requested task capabilities", 403)
            if "workspace_read" not in caps:
                raise AgentTaskInvocationError("permission_denied", "Current Agent authority does not permit workspace_read", 403)
            if request["mode"] == "read_only":
                caps = {
                    capability
                    for capability in caps
                    if AGENT_TEAM_CAPABILITY_CATALOG[capability].get("access") != "write"
                }
            if request["allow_commands"] is False:
                caps.discard("command_execute")
            if requested_capabilities - caps:
                raise AgentTaskInvocationError("permission_denied", "The requested read-only or command scope conflicts with required capabilities", 403)
            if request["allow_commands"] is True and "command_execute" not in caps:
                raise AgentTaskInvocationError("permission_denied", "Current Agent authority does not permit command_execute", 403)
            if "workspace_read" not in caps:
                raise AgentTaskInvocationError("permission_denied", "Current Agent authority does not permit workspace_read", 403)
            if request["mode"] is None:
                request["mode"] = "mutate" if caps.intersection({"workspace_write", "command_execute"}) else "read_only"
            if request.get("required_outputs") and request["mode"] != "mutate":
                raise AgentTaskInvocationError("invalid_input", "required_outputs requires mutate mode", 422)
            if request.get("edit_targets") and request["mode"] != "mutate":
                raise AgentTaskInvocationError("invalid_input", "edit_targets requires mutate mode", 422)
            edit_scope_payload = None
            if request["mode"] == "mutate":
                # Frozen once from the caller's original text, before any Work/Run
                # exists; the runtime and restarts only ever read this value.
                from ..llm.tool_policy import StructuredEditScopeError, build_fixed_edit_scope

                try:
                    edit_scope_payload = build_fixed_edit_scope(
                        "\n".join([request["goal"], *request["constraints"], *request["completion_criteria"]]),
                        edit_targets=request.get("edit_targets"),
                        required_outputs=request.get("required_outputs") or (),
                    ).to_payload()
                except StructuredEditScopeError as exc:
                    raise AgentTaskInvocationError("invalid_input", str(exc), 422) from None
            if request["allow_commands"] is None:
                request["allow_commands"] = "command_execute" in caps
            if request.get("edit_targets") == [] and request["mode"] == "mutate" and not request["allow_commands"]:
                # A mutate task that may write no file and run no command can never
                # produce the mutation receipt its settlement requires.
                raise AgentTaskInvocationError(
                    "invalid_input",
                    "edit_targets=[] with no commands grants no mutation; use mode=read_only for a task that writes nothing",
                    422,
                )
            caps = tuple(sorted(caps))
            request["required_capabilities"] = list(caps)
            await self._evaluate_task_workspace(
                request,
                agent_id=str(agent.id),
                agent_revision_id=str(revision.id),
                owner_user_id=owner_id,
                command=False,
            )
            if request["allow_commands"]:
                await self._resolve_admitted_command_workspace(
                    request,
                    agent_id=str(agent.id),
                    agent_revision_id=str(revision.id),
                    owner_user_id=owner_id,
                )
            _require_model_registry_entry(self.config, resolved_route["provider"], resolved_route["model"])

            deadline = datetime.utcnow() + timedelta(seconds=_TASK_TIMEOUT_SECONDS)
            admission_nonce = uuid.uuid4().hex
            admission_revision = _admission_revision(task_id)
            candidate = WorkCandidate(
                source_type=AGENT_TASK_SOURCE_TYPE,
                source_id=task_id,
                source_revision=admission_revision,
                intent_key="invoke",
                domain="api_agent_task",
                conversation_session_id=None,
                assigned_agent_id=request["agent_id"],
                agent_revision_id=revision.id,
                required_capabilities=caps,
                execution_adapter=AGENT_TASK_ADAPTER_KEY,
                max_attempts=1,
                not_before=deadline,
                deadline=deadline,
                concurrency_key=_task_concurrency_key(task_id),
                metadata={
                    "task_id": task_id,
                    "owner_user_id": owner_id,
                    "request_fingerprint": fingerprint,
                    "admission_revision": admission_revision,
                    "workspace": request["workspace"],
                    "mode": request["mode"],
                    "allow_commands": request["allow_commands"],
                    "allow_git_commit": bool(request.get("allow_git_commit", False)),
                    "allow_git_push": bool(request.get("allow_git_push", False)),
                    **({"tool_generation_max_tokens": request["tool_generation_max_tokens"]} if request.get("tool_generation_max_tokens") else {}),
                    "requires_command_verification": _requires_command_verification(request),
                    **({"required_outputs": list(request["required_outputs"])} if request.get("required_outputs") else {}),
                    **({"edit_scope": edit_scope_payload} if edit_scope_payload is not None else {}),
                    "required_capabilities": list(caps),
                    "route": resolved_route,
                    "admission_state": "admitting",
                    "admission_nonce": admission_nonce,
                },
            )
            from ..memory.models import AgentWorkItem

            work_session = await self._session()
            try:
                work = await self.coordinator.materialize(candidate, session=work_session)
                if not work or not work.get("id"):
                    raise AgentTaskInvocationError("agent_work_unavailable", "The task could not be admitted to AgentWork", 503)
                work_id = str(work["id"])
                from ..memory.models import AgentWorkItem

                existing_row = (await work_session.execute(
                    select(AgentWorkItem)
                    .where(AgentWorkItem.id == uuid.UUID(work_id))
                    .with_for_update()
                )).scalars().first()
                if existing_row is None:
                    raise AgentTaskInvocationError("agent_work_unavailable", "The task Work item is unavailable", 503)
                stored = existing_row.metadata_json if isinstance(existing_row.metadata_json, Mapping) else {}
                if stored.get("request_fingerprint") != fingerprint or stored.get("owner_user_id") != owner_id:
                    raise AgentTaskInvocationError("idempotency_conflict", "The task identity is already bound to a different request", 409)
                if stored.get("admission_revision") != admission_revision:
                    raise AgentTaskInvocationError("idempotency_conflict", "The task identity is already bound to a different admission revision", 409)
                if stored.get("workspace") != request["workspace"]:
                    # A clipped or dropped workspace must never reach execution.
                    raise AgentTaskInvocationError("invalid_workspace", "workspace could not be stored intact", 422)
                if stored.get("tool_generation_max_tokens") != request.get("tool_generation_max_tokens"):
                    # An admitted execution control must survive durable storage intact.
                    raise AgentTaskInvocationError("invalid_input", "tool_generation_max_tokens could not be stored intact", 422)
                await work_session.commit()
            except BaseException:
                await work_session.rollback()
                raise
            finally:
                await work_session.close()
            # materialize() is the durable cross-process idempotency fence.
            # Only its first writer may continue admission; a crashed partial
            # admission remains visible as interrupted and is never replayed.
            if stored.get("admission_nonce") != admission_nonce:
                return await self._task_projection(task_id, owner=owner_id), False

            # Work owns the idempotency slot before any private session exists.
            # The session id is linked atomically with session creation so a
            # crash cannot leave an untracked private conversation.
            session_id = await self._create_private_session(
                owner_id,
                task_id,
                work_id=work_id,
                request_fingerprint=fingerprint,
                admission_nonce=admission_nonce,
            )
            with _agent_selection_errors():
                bound = await agents.bind_session_agent(
                    session_id=session_id,
                    agent_id=request["agent_id"],
                    actor_user_id=owner_id,
                    actor_role=actor_role,
                    expected_agent_id=None,
                    execution_profile_id=request.get("execution_profile_id"),
                )
            binding = bound.binding
            if binding is None:
                raise AgentTaskInvocationError("agent_revision_missing", "The selected Agent has no immutable binding", 409)
            pinned_route = _resolve_task_operator_route(self.config, binding.routing_pins())
            pinned_projection = _assert_api_route(
                pinned_route,
                request["expected_route"],
                explicit=bool(binding.execution_selection.is_explicit),
            )
            if pinned_projection != resolved_route or str(binding.agent_revision_id) != str(revision.id):
                raise AgentTaskInvocationError("agent_route_mismatch", "The pinned route changed during task admission", 409)
            if binding.execution_selection.selected_execution_profile_id != pins.execution_profile_id:
                # The immutable binding must carry exactly the preflighted selection.
                raise AgentTaskInvocationError("agent_route_mismatch", "The pinned ExecutionProfile changed during task admission", 409)
            await self._authorise_caps(request["agent_id"], binding.agent_revision_id, caps)

            binding_session = await self._session()
            try:
                row = (await binding_session.execute(
                    select(AgentWorkItem)
                    .where(AgentWorkItem.id == uuid.UUID(work_id))
                    .with_for_update()
                )).scalars().first()
                metadata = row.metadata_json if row is not None and isinstance(row.metadata_json, Mapping) else {}
                if (
                    row is None
                    or row.state != "pending"
                    or metadata.get("admission_state") != "admitting"
                    or metadata.get("admission_nonce") != admission_nonce
                    or metadata.get("request_fingerprint") != fingerprint
                    or metadata.get("session_id") != session_id
                ):
                    raise AgentTaskInvocationError("agent_work_unavailable", "The task admission fence changed", 409)
                row.metadata_json = {
                    **dict(metadata),
                    "binding_id": binding.binding_id,
                    "binding_snapshot_hash": binding.snapshot_hash,
                    "route": resolved_route,
                }
                row.updated_at = datetime.utcnow()
                await binding_session.commit()
            except BaseException:
                await binding_session.rollback()
                raise
            finally:
                await binding_session.close()

            manifest = {
                "schema_version": 2,
                "source": AGENT_TASK_SOURCE_TYPE,
                "agent_id": request["agent_id"],
                "agent_revision_id": binding.agent_revision_id,
                "work_item_id": work_id,
                "team_id": binding.agent_team_id,
                "execution_profile_id": binding.execution_profile_id,
                "capabilities": list(caps),
                "route_snapshot": binding.routing_pins().route_snapshot.get("roles", binding.routing_pins().route_snapshot),
            }
            if binding.execution_selection.is_explicit:
                from .agent_model_routing import execution_manifest_route_fields

                # Revision-default tasks keep the historical manifest/hash.
                manifest.update(execution_manifest_route_fields(binding.routing_pins().route_snapshot))
            run = await AgentRunService(self.db, config=self.config).create_run(
                session_id=session_id,
                user_id=owner_id,
                agent_id=request["agent_id"],
                agent_revision_id=binding.agent_revision_id,
                work_item_id=work_id,
                work_item_attempt=1,
                run_type="autonomous_work",
                title=f"Agent task {task_id[:8]}",
                objective=_task_prompt(request),
                provider=resolved_route["provider"],
                model=resolved_route["model"],
                acting_subagent_id=pinned_route.acting_subagent_id,
                execution_manifest=manifest,
                metadata={
                    "api_agent_task": {"task_id": task_id, "request_fingerprint": fingerprint},
                    "agent_binding": _binding_audit_projection(binding),
                    "resolved_route": resolved_route,
                    "workspace": request["workspace"],
                    "mode": request["mode"],
                    "allow_commands": request["allow_commands"],
                    "allow_git_commit": bool(request.get("allow_git_commit", False)),
                    "allow_git_push": bool(request.get("allow_git_push", False)),
                    "required_capabilities": list(caps),
                },
            )
            run_id = str(run["id"])
            link_session = await self._session()
            try:
                row = (await link_session.execute(
                    select(AgentWorkItem)
                    .where(AgentWorkItem.id == uuid.UUID(work_id))
                    .with_for_update()
                )).scalars().first()
                metadata = row.metadata_json if row is not None and isinstance(row.metadata_json, Mapping) else {}
                if (
                    row is None
                    or row.state != "pending"
                    or row.active_agent_run_id is not None
                    or metadata.get("admission_nonce") != admission_nonce
                    or metadata.get("request_fingerprint") != fingerprint
                    or metadata.get("admission_revision") != admission_revision
                    or metadata.get("session_id") != session_id
                    or metadata.get("binding_id") != binding.binding_id
                    or metadata.get("binding_snapshot_hash") != binding.snapshot_hash
                ):
                    raise AgentTaskInvocationError("agent_work_unavailable", "The task admission fence changed", 409)
                row.active_agent_run_id = uuid.UUID(run_id)
                row.not_before = datetime.utcnow()
                row.metadata_json = {**dict(metadata), "admission_state": "ready", "agent_run_id": run_id}
                row.updated_at = datetime.utcnow()
                await link_session.commit()
            except BaseException:
                await link_session.rollback()
                try:
                    await AgentRunService(self.db, config=self.config).fail_run(
                        run_id,
                        "Task Work linkage failed before execution",
                        metadata={"classification": "uncertain", "safe_error_code": "agent_work_unavailable"},
                    )
                except Exception:
                    logger.debug("Could not close an unlinked API Agent task Run", exc_info=True)
                raise
            finally:
                await link_session.close()
            return await self._task_projection(task_id, owner=owner_id), True

    async def _checkpoint(self, claim: Any, run_id: str, request: Mapping[str, Any], run: Mapping[str, Any]) -> float | None:
        """Re-verify the execution fence; return seconds left to the Work's hard deadline (None: no deadline)."""

        from ..memory.models import AgentRun, AgentWorkItem

        session = await self._session()
        try:
            work = await session.get(AgentWorkItem, uuid.UUID(claim.work_item_id))
            active_run = await session.get(AgentRun, uuid.UUID(run_id))
            metadata = getattr(work, "metadata_json", None) if work is not None else None
            if not isinstance(metadata, Mapping) or active_run is None:
                raise AgentTaskInvocationError("lease_lost", "The task execution fence is unavailable", 409)
            if (
                work.state != "running"
                or work.lease_token != claim.lease_token
                or work.lease_owner != claim.lease_owner
                or work.attempt_count != claim.attempt
                or work.lease_expires_at is None
                or work.lease_expires_at <= datetime.utcnow()
                or str(work.active_agent_run_id or "") != run_id
                or metadata.get("task_id") != claim.source_id
                or metadata.get("owner_user_id") != str(active_run.user_id or "")
                or metadata.get("session_id") != str(active_run.session_id or "")
                or metadata.get("admission_revision") != claim.source_revision
                or metadata.get("admission_state") != "ready"
                or active_run.status != "running"
                or str(active_run.work_item_id or "") != claim.work_item_id
                or str(active_run.agent_id or "") != str(claim.assigned_agent_id or "")
                or str(active_run.agent_revision_id or "") != str(claim.agent_revision_id or "")
                or tuple(work.required_capabilities_json or ()) != tuple(claim.required_capabilities)
            ):
                raise AgentTaskInvocationError("lease_lost", "The task execution fence changed", 409)
            remaining: float | None = None
            if work.deadline is not None:
                remaining = (work.deadline - datetime.utcnow()).total_seconds()
                if remaining <= 0:
                    raise AgentTaskInvocationError("timeout", "The task deadline elapsed", 409)
            if metadata.get("cancellation_requested"):
                raise AgentTaskInvocationError("cancelled", "Task cancellation was requested", 409)
        finally:
            await session.close()
        await self._authorise_caps(str(claim.assigned_agent_id), str(claim.agent_revision_id), tuple(claim.required_capabilities))
        admitted = await self._evaluate_task_workspace(
            request,
            agent_id=str(claim.assigned_agent_id),
            agent_revision_id=str(claim.agent_revision_id),
            owner_user_id=str(request.get("owner_user_id") or ""),
            command=False,
        )
        if str(admitted) != str(Path(request["workspace"])):
            raise AgentTaskInvocationError("workspace_not_allowed", "workspace is no longer approved", 403)
        return remaining

    async def _task_request_for_run(self, claim: Any, run_id: str, run: Mapping[str, Any]) -> dict[str, Any]:
        metadata = await self._find_work(claim.source_id)
        stored = metadata.metadata_json if isinstance(metadata.metadata_json, Mapping) else {}
        if stored.get("admission_state") != "ready" or stored.get("agent_run_id") != run_id:
            raise AgentTaskInvocationError("execution_binding_mismatch", "The task admission is incomplete", 409)
        try:
            prompt = str(getattr(run, "objective", "") or run.get("objective") or "")
        except AttributeError:
            prompt = str(run.get("objective") or "")
        if not prompt:
            raise AgentTaskInvocationError("execution_binding_mismatch", "The task prompt is unavailable", 409)
        stored_route = dict(stored.get("route") or {})
        # AgentWork's bounded JSON normalizer omits empty strings.  A pinned
        # route with no effort selection therefore round-trips without that
        # key; restore its canonical empty value before exact route comparison.
        stored_route.setdefault("effort", "")
        request = {
            "task_id": claim.source_id,
            "admission_revision": str(stored.get("admission_revision") or ""),
            "owner_user_id": str(stored.get("owner_user_id") or ""),
            "session_id": str(stored.get("session_id") or ""),
            "workspace": str(stored.get("workspace") or ""),
            "mode": str(stored.get("mode") or "read_only"),
            "allow_commands": bool(stored.get("allow_commands")),
            "allow_git_commit": bool(stored.get("allow_git_commit")),
            "allow_git_push": bool(stored.get("allow_git_push")),
            **({"tool_generation_max_tokens": int(stored["tool_generation_max_tokens"])} if stored.get("tool_generation_max_tokens") else {}),
            "requires_command_verification": bool(stored.get("requires_command_verification")),
            "required_outputs": [str(item) for item in stored.get("required_outputs") or ()],
            # Captured once at first execution (a Run event), never at admission.
            "required_output_baseline": {},
            "required_capabilities": tuple(stored.get("required_capabilities") or ()),
            "route": stored_route,
            "binding_id": str(stored.get("binding_id") or ""),
            "binding_snapshot_hash": str(stored.get("binding_snapshot_hash") or ""),
            "prompt": prompt,
            "request_fingerprint": str(stored.get("request_fingerprint") or ""),
            # Only a restart re-queue grants a later attempt of the same Run.
            "restart_operations": (
                list(stored.get("restart_operations") or ())
                if int(getattr(claim, "attempt", 1) or 1) > 1
                else None
            ),
        }
        if "edit_scope" in stored:
            request["edit_scope"] = stored["edit_scope"]
        if request["mode"] == "mutate":
            # Validated (or re-derived from the durable original prompt) before any model
            # turn; malformed metadata fails closed with a binding mismatch.
            request["edit_scope"] = _edit_scope_for_request(request).to_payload()
        return request

    async def _load_output_baseline(self, run_id: Any, session: Any = None) -> dict[str, str] | None:
        """The durable first-execution baseline of the Run's required outputs, if captured."""

        from ..memory.models import AgentRunEvent

        owned = session is None
        session = await self._session() if owned else session
        try:
            payload = (await session.execute(
                select(AgentRunEvent.payload)
                .where(
                    AgentRunEvent.run_id == uuid.UUID(str(run_id)),
                    AgentRunEvent.event_type == _OUTPUT_BASELINE_EVENT,
                )
                .order_by(AgentRunEvent.sequence)
                .limit(1)
            )).scalars().first()
        finally:
            if owned:
                await session.close()
        baseline = payload.get("baseline") if isinstance(payload, Mapping) else None
        if not isinstance(baseline, Mapping):
            return None
        return {str(key): str(value) for key, value in baseline.items()}

    async def _ensure_output_baseline(self, request: dict[str, Any], run_id: str, cancelled: Any) -> None:
        """Capture the required-output digests once, before any model turn or tool.

        Runs inside the workspace lease, so another queued task cannot write a
        required output between this snapshot and the model's first tool.  A
        restart reuses the stored event.  The event is written unsanitized by
        this code path, read back and compared; any mismatch or write failure
        stops the task rather than weakening the comparison.
        """

        if not request.get("required_outputs"):
            return
        baseline = await self._load_output_baseline(run_id)
        if baseline is None:
            baseline = await asyncio.to_thread(_required_output_baseline, request, cancelled)
            if any(value in ("unsafe", "unverified") for value in baseline.values()):
                raise AgentTaskInvocationError(
                    "required_output_unverifiable", "A required output could not be safely read before the task", 409
                )
            from .agent_run_service import AgentRunService

            recorded = await AgentRunService(self.db, config=self.config).record_event(
                run_id, event_type=_OUTPUT_BASELINE_EVENT, payload={"baseline": baseline}
            )
            if recorded is None or await self._load_output_baseline(run_id) != baseline:
                raise AgentTaskInvocationError("audit_write_failed", "The required-output baseline could not be durably recorded", 409)
        request["required_output_baseline"] = baseline

    async def _completion_evidence(
        self, run_id: str, request: Mapping[str, Any]
    ) -> tuple[bool, str, dict[str, Any]]:
        rows = await self._receipts_for_run(run_id)
        accepted, reason = await asyncio.to_thread(_completion_evidence_satisfied, request, rows)
        return accepted, reason, _tool_failure_summary(_tool_failure_ledger(rows, request))

    async def _continue_after_restart(self, run_id: str, request: dict[str, Any]) -> None:
        """Re-enter an interrupted Run from its durable outcomes, never a blind replay.

        A file mutation the recovery proved applied gets its receipt under the
        original operation id, so completion evidence counts it exactly once.
        """

        operations = request.get("restart_operations")
        if operations is None:
            return
        from .agent_run_service import AgentRunService

        run_service = AgentRunService(self.db, config=self.config)
        recorded = {str(receipt.tool_call_id or "") for receipt in await self._receipts_for_run(run_id)}
        for item in operations:
            if (
                not isinstance(item, Mapping)
                or item.get("outcome") != "applied"
                or not item.get("operation_id")
                or item["operation_id"] in recorded
            ):
                continue
            receipt = await run_service.record_tool_call(
                run_id,
                tool_name=str(item["tool"]),
                arguments={"path": item.get("path")},
                result={"success": True, "message": "Reconciled after restart: the file digest equals this edit's expected result"},
                success=True,
                tool_call_id=str(item["operation_id"]),
                metadata={
                    "executed": True,
                    "restart_reconciled": True,
                    "task_mode": "mutate",
                    "task_authorized_capability": "workspace_write",
                },
            )
            if receipt is None:
                raise AgentTaskInvocationError("audit_write_failed", "The reconciled tool result could not be durably recorded", 409)
        if await run_service.record_event(
            run_id,
            event_type=_RESTART_ATTEMPT_EVENT,
            payload={"restart_operations": len(operations)},
        ) is None:
            raise AgentTaskInvocationError("audit_write_failed", "The restart continuation could not be durably recorded", 409)
        request["intent_prompt"] = request["prompt"]
        request["prompt"] += _restart_continuation_note(await self._receipts_for_run(run_id), operations)

    async def _prime_restart_history(self, client: Any, request: Mapping[str, Any], run_id: str) -> None:
        """Give a resumed model turn the interrupted attempts through the chat history path.

        A running task stores no turns in its private session, and the native
        runtime keeps its provider messages in memory only. On continuation the
        task intent and each earlier attempt's assistant text and paired tool
        calls/results (rebuilt from the Run's events and redacted receipts, no
        hidden reasoning) are saved there once (stable message ids) as ordinary
        assistant messages with a ``model_transcript``, and read back by the
        chat history loader, which applies its own bounds. The provider's exact
        message objects are not durable, so this is a faithful rebuild, not a
        byte-identical replay.
        """

        if request.get("restart_operations") is None:
            return
        from ..assistant.chat_turn_persistence import ChatTurnPersistence

        session_id = request["session_id"]
        persistence = ChatTurnPersistence()
        receipts = await self._receipts_for_run(run_id)
        # The last segment is the attempt now running.
        attempts = (await self._run_attempt_events(run_id))[:-1]

        def message_id(kind: str) -> str:
            return str(uuid.uuid5(uuid.NAMESPACE_URL, f"aoitalk:api-agent-task-restart:{run_id}:{kind}"))

        if await persistence.load_message(uuid.UUID(message_id("intent"))) is None:
            await persistence.save_user_message(
                session_id=session_id,
                content=str(request.get("intent_prompt") or request["prompt"]),
                message_id=message_id("intent"),
            )
        # Each saved attempt carries the cumulative provider transcript, the
        # shape the history loader merges without duplicating earlier turns.
        transcript: list[dict[str, Any]] = [{"role": "user", "content": str(request.get("intent_prompt") or request["prompt"])}]
        for index, events in enumerate(attempts, start=1):
            transcript = [*transcript, *_restart_attempt_messages(events, receipts)]
            tail = transcript[-1]
            closing = (
                str(tail["content"]) if tail.get("role") == "assistant" and not tail.get("tool_calls") and tail.get("content")
                else "Work continued before the service restart; see the recorded tool results."
            )
            if tail.get("role") != "assistant" or tail.get("tool_calls") or not tail.get("content"):
                transcript = [*transcript, {"role": "assistant", "content": closing}]
            if await persistence.load_message(uuid.UUID(message_id(f"attempt-{index}"))) is None:
                await persistence.save_assistant_message(
                    session_id=session_id,
                    content=closing,
                    metadata={"model_transcript": [dict(item) for item in transcript]},
                    message_id=message_id(f"attempt-{index}"),
                )
        history = await persistence.load_prompt_history(session_id=session_id)
        persistence.apply_prompt_history_to_client(client, session_id=session_id, prompt_history=history)

    async def _command_owner_record(self, owner_user_id: str) -> tuple[str, bool]:
        from ..memory.models import User

        session = await self._session()
        try:
            try:
                user_id = uuid.UUID(str(owner_user_id))
            except (TypeError, ValueError, AttributeError):
                return "", False
            user = await session.get(User, user_id)
            if user is None or not bool(getattr(user, "is_active", False)):
                return "", False
            return str(getattr(user, "role", "") or ""), True
        finally:
            await session.close()

    async def _evaluate_task_workspace(
        self,
        request: Mapping[str, Any],
        *,
        agent_id: str,
        agent_revision_id: str,
        owner_user_id: str | None = None,
        command: bool,
    ) -> Any:
        from .command_workspace_confinement import evaluate_task_workspace
        from .local_agent_tasks import LocalAgentTaskError

        owner = str(owner_user_id or request.get("owner_user_id") or "")
        role, active = await self._command_owner_record(owner)
        try:
            return evaluate_task_workspace(
                self.config,
                str(request["workspace"]),
                owner_user_id=owner,
                owner_role=role,
                owner_is_active=active,
                agent_id=agent_id,
                agent_revision_id=agent_revision_id,
                command=command,
            )
        except LocalAgentTaskError as exc:
            raise AgentTaskInvocationError(exc.code, exc.message, exc.http_status) from None

    async def _resolve_admitted_command_workspace(
        self,
        request: Mapping[str, Any],
        *,
        agent_id: str,
        agent_revision_id: str,
        owner_user_id: str | None = None,
    ) -> Any:
        return await self._evaluate_task_workspace(
            request,
            agent_id=agent_id,
            agent_revision_id=agent_revision_id,
            owner_user_id=owner_user_id,
            command=True,
        )

    async def _strict_command_scope(self, claim: Any, run_id: str, request: Mapping[str, Any], run: Mapping[str, Any]) -> Any:
        """Issue and preflight only the established file-scoped harness lane."""

        from ..agent_harness.runner import _scoped_backend
        from ..security.harness_execution_scope import ResourceLimits
        from .agent_authority import AgentAuthorityResolver
        from .harness_execution_scope_service import (
            build_agent_work_harness_scope,
            release_created_scratch_roots,
        )

        decision = await AgentAuthorityResolver(self.db, config=self.config).resolve(
            agent_id=str(claim.assigned_agent_id),
            revision_id=str(claim.agent_revision_id),
            required_capability="command_execute",
            tool_capabilities=tuple(claim.required_capabilities),
            harness_capabilities=tuple(claim.required_capabilities),
        )
        if (
            not bool(getattr(decision, "allowed", False))
            or not getattr(decision, "organization_id", None)
            or str(getattr(decision, "agent_id", "")) != str(claim.assigned_agent_id)
            or str(getattr(decision, "agent_revision_id", "")) != str(claim.agent_revision_id)
        ):
            raise AgentTaskInvocationError("permission_denied", "Current Agent command authority is unavailable", 403)
        workspace_root = await self._resolve_admitted_command_workspace(
            request,
            agent_id=str(claim.assigned_agent_id),
            agent_revision_id=str(claim.agent_revision_id),
        )
        # The Windows API-task path intentionally runs under the current OS
        # user token. WSL2/bubblewrap remains the explicit default elsewhere;
        # neither model input nor task payload can select this backend.
        backend_kind = "native_windows" if os.name == "nt" else "wsl_bwrap"
        await self._checkpoint(claim, run_id, request, run)
        scope = build_agent_work_harness_scope(
            workspace_path=request["workspace"],
            harness_workspace_root=workspace_root,
            resource_limits=ResourceLimits(
                timeout_seconds=900,
                max_processes=32,
                max_output_bytes=256 * 1024,
                max_disk_bytes=256 * 1024 * 1024,
                max_background_seconds=900,
            ),
            agent_id=str(claim.assigned_agent_id),
            organization_id=str(decision.organization_id),
            project_id=None,
            app_id=None,
            run_id=run_id,
            audit_id=f"api-agent-task:{claim.work_item_id}:{claim.attempt}",
            mode="workspace_write",
            delete_workspace=False,
            backend_kind=backend_kind,
        )
        try:
            try:
                prepared = await asyncio.to_thread(
                    _scoped_backend,
                    scope.to_agent_run_scope(),
                    Path(request["workspace"]),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise AgentTaskInvocationError("command_runtime_unavailable", "The server-selected command runtime is unavailable", 409) from exc
            if isinstance(prepared, str):
                raise AgentTaskInvocationError("command_runtime_unavailable", "The server-selected command runtime is unavailable", 409)
            await self._checkpoint(claim, run_id, request, run)
        except BaseException:
            # The scope never reaches execute(); remove the scratch it created.
            release_created_scratch_roots(scope)
            raise
        return scope

    async def execute(self, claim: Any, *, coordinator: Any, run_id: str, run: Mapping[str, Any]) -> ExecutionOutcome:
        from ..llm.generation_budget import GenerationBudget, current_generation_budget
        from ..llm.generation_error import ModelOutputIncompleteError
        from ..llm.generation_cancellation import (
            GenerationCancelled,
            register_generation_cancellation,
            release_generation_cancellation,
            reset_current_generation_cancellation,
            set_current_generation_cancellation,
        )
        from ..llm.provider_terminal import (
            FAILED_TERMINAL_STATES,
            protocol_error_terminal,
            sanitize_terminal_evidence,
        )
        from ..security.agent_run_scope import AgentRunScope, run_scope_context
        from ..security.harness_execution_scope import harness_execution_scope_context
        from ..services.agent_run_service import reset_current_agent_run_id, set_current_agent_run_id
        from .agent_dialogue_workspace import AgentDialogueWorkspaceManager, TaskOwnershipSpec
        from .agent_run_service import AgentRunService
        from .chat_execution_adapter import bind_chat_agent_execution, build_agent_chat_turn_client, resolve_chat_agent_execution
        from .turn_context import reset_turn_context, set_turn_context

        request: dict[str, Any] = {}
        cancel_handle = None
        scope = None
        issued_harness_scope = None
        tool_scope_token = None
        read_stagnation_token = None
        read_stagnation_policy = None
        workspace_scope_token = None
        task_policy_denial: str | None = None
        corrective_turns = 0
        rows_before_correction: Sequence[Any] = ()
        budget_token = None
        budget = None
        budget_timed_out = False
        provider_terminal: dict[str, Any] | None = None
        event_provider_terminal: dict[str, Any] | None = None
        try:
            request = await self._task_request_for_run(claim, run_id, run)
            if (
                request["task_id"] != claim.source_id
                or request["admission_revision"] != claim.source_revision
                or request["required_capabilities"] != tuple(claim.required_capabilities)
                or str(run.get("user_id") or "") != request["owner_user_id"]
                or str(run.get("session_id") or "") != request["session_id"]
            ):
                raise AgentTaskInvocationError("execution_binding_mismatch", "The durable AgentRun does not match the task", 409)
            await self._checkpoint(claim, run_id, request, run)
            await self._continue_after_restart(run_id, request)
            execution = await resolve_chat_agent_execution(
                session_id=request["session_id"],
                run=run,
                config=self.config,
                db_manager=self.db,
                run_id=run_id,
            )
            if execution is None or execution.binding.binding_id != request["binding_id"] or execution.binding.snapshot_hash != request["binding_snapshot_hash"]:
                raise AgentTaskInvocationError("agent_binding_mismatch", "The immutable Agent binding changed", 409)
            route = _assert_api_route(
                execution.route,
                request["route"],
                explicit=bool(getattr(getattr(execution.binding, "execution_selection", None), "is_explicit", False)),
            )
            if route != request["route"]:
                raise AgentTaskInvocationError("agent_route_mismatch", "The pinned Agent route changed", 409)
            _require_model_registry_entry(self.config, route["provider"], route["model"])
            issued_harness_scope = await self._strict_command_scope(claim, run_id, request, run) if request["allow_commands"] else None
            scope = issued_harness_scope.to_agent_run_scope() if issued_harness_scope is not None else AgentRunScope.for_repository(
                request["workspace"],
                run_id=run_id,
                workspace_access_level="write" if request["mode"] == "mutate" else "read",
                command_roots=(request["workspace"],) if request["allow_commands"] else (),
            )
            workspace_manager = AgentDialogueWorkspaceManager()
            client_builder = self.client_builder or build_agent_chat_turn_client
            source_client = SimpleNamespace(
                config=self.config,
                current_session_id=request["session_id"],
                session_user_id=request["owner_user_id"],
                current_project_id=None,
                current_include_project_context=False,
                external_persistence_enabled=False,
            )
            context_token = set_turn_context(
                user_id=request["owner_user_id"],
                session_id=request["session_id"],
                include_project_context=False,
                suppress_automatic_context=False,
            )
            run_token = set_current_agent_run_id(run_id)
            cancel_handle = register_generation_cancellation(run_id)
            cancel_token = set_current_generation_cancellation(cancel_handle)
            from ..llm.generation_policy import (
                GenerationProfile,
                generation_policy_for_profile,
            )

            task_generation_policy = generation_policy_for_profile(
                GenerationProfile.AUTONOMOUS_WORK
            )
            client = None
            owns_client = False
            try:
                # Task ownership is proven by the checkpointed Work/Run/attempt, not
                # by the cwd mutex, so independent canonical tasks may share a cwd.
                async with workspace_manager.session(
                    task_ownership=TaskOwnershipSpec(
                        task_id=request["task_id"],
                        work_item_id=str(getattr(claim, "work_item_id", "") or ""),
                        attempt=int(getattr(claim, "attempt", 0) or 0),
                        cancelled=lambda: cancel_handle is not None and cancel_handle.cancel_requested.is_set(),
                    ),
                    session_id=request["session_id"],
                    agent_id=execution.agent_id,
                    provider_id=route["provider"],
                    model_id=route["model"],
                    run_id=run_id,
                    owner_id=request["owner_user_id"],
                    readonly=request["mode"] == "read_only",
                    workspace_path=request["workspace"],
                    authorized_scope=scope,
                    native_allowed=bool(
                        request["allow_commands"]
                        and issued_harness_scope is not None
                    ),
                ) as workspace:
                    await self._checkpoint(claim, run_id, request, run)
                    from ..llm.tool_policy import set_current_agent_workspace_scope

                    edit_scope = _edit_scope_for_request(request) if request["mode"] == "mutate" else None
                    workspace_scope_token = set_current_agent_workspace_scope(
                        session_id=workspace.session_id,
                        agent_id=workspace.agent_id,
                        run_id=run_id,
                        resource_key=workspace.resource_key,
                        readonly=workspace.readonly,
                        native_allowed=workspace.native_allowed,
                        task_mode=request["mode"],
                        edit_scope=edit_scope,
                    )
                    with run_scope_context(scope):
                        if issued_harness_scope is None:
                            harness_context = None
                        else:
                            harness_context = harness_execution_scope_context(issued_harness_scope)
                            harness_context.__enter__()
                        try:
                            async with bind_chat_agent_execution(
                                execution,
                                user_id=request["owner_user_id"],
                                session_id=request["session_id"],
                                project_id=None,
                                config=self.config,
                                workspace=workspace,
                            ):
                                from ..llm.tool_policy import set_current_agent_tool_scope
                                from .agent_authority_guard import guard_agent_tool_call_sync

                                def revalidate_task_tool(name: str, args: Mapping[str, Any], definition: Any) -> bool:
                                    return guard_agent_tool_call_sync(
                                        name,
                                        args,
                                        tool_definition=definition,
                                        config=self.config,
                                    ).allowed

                                tool_scope_token = set_current_agent_tool_scope(
                                    request["required_capabilities"],
                                    allowed_subagent_ids=execution.binding.allowed_subagent_ids,
                                    revalidate=revalidate_task_tool,
                                )
                                task_build_options = (
                                    {"tool_generation_max_tokens": request["tool_generation_max_tokens"]}
                                    if request.get("tool_generation_max_tokens")
                                    else {}
                                )
                                if self.client_builder is not None:
                                    client = client_builder(source_client, self.config, execution, workspace=workspace, session_id=request["session_id"], project_id=None, run_id=run_id, **task_build_options)
                                    if inspect.isawaitable(client):
                                        build_task = asyncio.ensure_future(client)
                                        client = None
                                    else:
                                        build_task = None
                                else:
                                    build_task = asyncio.create_task(asyncio.to_thread(
                                        client_builder,
                                        source_client,
                                        self.config,
                                        execution,
                                        workspace=workspace,
                                        session_id=request["session_id"],
                                        project_id=None,
                                        run_id=run_id,
                                        **task_build_options,
                                    ))
                                if build_task is not None:
                                    build_cancelled = False
                                    try:
                                        while not build_task.done():
                                            try:
                                                await asyncio.shield(build_task)
                                            except asyncio.CancelledError:
                                                # A worker cannot be cancelled: retain the
                                                # result even after repeated cancellation.
                                                build_cancelled = True
                                        client = build_task.result()
                                    except Exception:
                                        # A builder failure after cancellation must not
                                        # replace the caller's cancellation classification.
                                        if build_cancelled:
                                            raise asyncio.CancelledError from None
                                        raise
                                owns_client = client is not source_client and (
                                    self.client_builder is None
                                    or _config_get(getattr(client, "config", None), "runtime.ephemeral_session_client", False) is True
                                )
                                if build_task is not None and build_cancelled:
                                    raise asyncio.CancelledError
                                capabilities = getattr(client, "capabilities", None)
                                if getattr(capabilities, "supports_tools", None) is not True:
                                    raise AgentTaskInvocationError("tool_calling_unsupported", "The target Agent client does not explicitly support tool calling", 409)
                                if getattr(client, "enable_tools", None) is False:
                                    raise AgentTaskInvocationError("tool_calling_disabled", "Tool calling is disabled for the target Agent client", 409)
                                _ensure_registered_tools_support(
                                    self.config,
                                    route["provider"],
                                    route["model"],
                                    client_supports_tools=getattr(capabilities, "supports_tools", None),
                                )
                                if getattr(client, "provider_label", route["provider"]) != route["provider"] or getattr(client, "model_name", route["model"]) != route["model"]:
                                    raise AgentTaskInvocationError("agent_route_mismatch", "The target client does not match the pinned route", 409)
                                _install_task_command_policy(client, request)
                                client._agent_run_id = run_id
                                client.generation_policy = task_generation_policy
                                client.current_session_id = request["session_id"]
                                client.session_user_id = request["owner_user_id"]
                                client.current_project_id = None
                                client.current_include_project_context = False
                                client.current_command_capabilities = tuple(claim.required_capabilities)
                                try:
                                    await self._prime_restart_history(client, request, run_id)
                                except Exception as exc:
                                    # The continuation note still carries the intent and settled outcomes.
                                    logger.warning("API Agent task restart history unavailable (%s)", type(exc).__name__)
                                self._active_provider_client = client
                                deadline_remaining = await self._checkpoint(claim, run_id, request, run)
                                # Every provider call below runs inside the Work's remaining
                                # hard-deadline budget, so a stuck transport and each
                                # corrective round are bounded by it and by cancellation.
                                budget = GenerationBudget(
                                    deadline_remaining,
                                    interruptible_transport=True,
                                    parent=current_generation_budget.get(),
                                )
                                budget_token = current_generation_budget.set(budget)
                                if request["mode"] == "mutate":
                                    from ..llm.read_stagnation import ReadStagnationPolicy, _ACTIVE as _read_stagnation

                                    read_stagnation_policy = ReadStagnationPolicy()
                                    read_stagnation_token = _read_stagnation.set(read_stagnation_policy)

                                # Bounded before/after workspace observations for command
                                # receipts of this attempt, keyed by tool_call_id.  This is
                                # in-memory only: an unmatched entry degrades to ``unknown``.
                                command_observations: dict[str, dict[str, Any]] = {}
                                # Exact owned files from the persisted edit scope; None keeps
                                # the conservative whole-workspace observation.
                                owned_scope_paths = _owned_file_scope_paths(request)
                                # job_id -> observation taken before the background launch.
                                background_windows: dict[str, Mapping[str, Any]] = {}

                                async def record_runtime_event(event_type: str, payload: Any) -> None:
                                    nonlocal event_provider_terminal
                                    from .agent_run_service import (
                                        AgentRunService,
                                        sanitize_tool_audit_payload,
                                    )

                                    raw = dict(payload) if isinstance(payload, Mapping) else {"_redacted": "invalid_event"}
                                    if "provider_terminal" in raw:
                                        terminal = sanitize_terminal_evidence(raw["provider_terminal"])
                                        raw.pop("provider_terminal")
                                        if terminal is not None:
                                            if event_type == "stream_end" and terminal["state"] == "continuation":
                                                terminal = protocol_error_terminal(
                                                    terminal, "unconsumed_continuation"
                                                )
                                            raw["provider_terminal"] = terminal
                                            if event_type == "stream_end":
                                                if (event_provider_terminal or {}).get("state") not in FAILED_TERMINAL_STATES:
                                                    event_provider_terminal = terminal
                                    safe = sanitize_tool_audit_payload(raw)
                                    if event_type == "tool_start" and request["mode"] == "mutate":
                                        # Durable before the tool runs: a restart settles the
                                        # edit by these digests, never by re-running it.
                                        safe.update(await asyncio.to_thread(
                                            _file_mutation_fingerprint,
                                            str(raw.get("tool") or "").rsplit(".", 1)[-1].casefold(),
                                            raw.get("tool_args") if isinstance(raw.get("tool_args"), Mapping) else {},
                                            request["workspace"],
                                        ))
                                        if str(raw.get("tool") or "").rsplit(".", 1)[-1].casefold() == "execute_command":
                                            # Bounded observation before the command runs. It is
                                            # held only for this attempt: a lost or unmatched start
                                            # leaves the receipt's evidence ``unknown`` (fail closed)
                                            # instead of guessing that the command wrote nothing.
                                            command_key = str(raw.get("operation_id") or raw.get("tool_call_id") or "").strip()
                                            while len(command_observations) >= 8:
                                                command_observations.pop(next(iter(command_observations)))
                                            command_observations[command_key] = {
                                                "arguments": dict(raw.get("tool_args"))
                                                if isinstance(raw.get("tool_args"), Mapping)
                                                else {},
                                                "observation": await asyncio.to_thread(
                                                    _workspace_content_observation,
                                                    request["workspace"],
                                                    request.get("required_outputs") or (),
                                                    scope_paths=owned_scope_paths,
                                                ),
                                            }
                                    run_service = AgentRunService(self.db, config=self.config)
                                    tool_result = raw.get("tool_result")
                                    if event_type == "tool_end" and isinstance(tool_result, Mapping):
                                        tool_name = str(
                                            tool_result.get("tool")
                                            or tool_result.get("tool_name")
                                            or raw.get("tool")
                                            or raw.get("tool_name")
                                            or ""
                                        ).strip()
                                        if tool_name and raw.get("tool_result_already_recorded") is not True:
                                            receipt_metadata = {
                                                key: value
                                                for key, value in tool_result.items()
                                                if key not in {"tool", "name", "arguments", "args", "output", "result"}
                                            }
                                            if not str(receipt_metadata.get("error_code") or "").strip():
                                                structured_code = _structured_error_code(
                                                    tool_result.get("output"),
                                                    tool_result.get("result"),
                                                )
                                                if structured_code:
                                                    receipt_metadata["error_code"] = structured_code
                                            if (
                                                request["mode"] == "mutate"
                                                and bool(tool_result.get("success"))
                                                and bool(tool_result.get("executed"))
                                                and tool_name.rsplit(".", 1)[-1].casefold()
                                                in _filesystem_mutation_receipt_names()
                                            ):
                                                receipt_metadata.update({
                                                    "task_mode": "mutate",
                                                    "task_authorized_capability": "workspace_write",
                                                })
                                            if (
                                                request["mode"] == "mutate"
                                                and tool_name.rsplit(".", 1)[-1].casefold() == "execute_command"
                                            ):
                                                command_key = str(
                                                    raw.get("operation_id")
                                                    or raw.get("tool_call_id")
                                                    or tool_result.get("tool_call_id")
                                                    or ""
                                                ).strip()
                                                tracked = command_observations.pop(command_key, None)
                                                raw_arguments = (
                                                    (tracked or {}).get("arguments")
                                                    or tool_result.get("arguments")
                                                    or raw.get("tool_args")
                                                )
                                                command_arguments = (
                                                    dict(raw_arguments)
                                                    if isinstance(raw_arguments, Mapping)
                                                    else {}
                                                )
                                                launch_before = (tracked or {}).get("observation")
                                                receipt_metadata.update(_command_workspace_evidence(
                                                    launch_before,
                                                    await asyncio.to_thread(
                                                        _workspace_content_observation,
                                                        request["workspace"],
                                                        request.get("required_outputs") or (),
                                                        scope_paths=owned_scope_paths,
                                                    ),
                                                    background=bool(command_arguments.get("run_in_background")),
                                                ))
                                                job_id = str(receipt_metadata.get("job_id") or "").strip()
                                                if job_id and launch_before:
                                                    while len(background_windows) >= 8:
                                                        background_windows.pop(next(iter(background_windows)))
                                                    background_windows[job_id] = launch_before
                                            elif (
                                                request["mode"] == "mutate"
                                                and tool_name.rsplit(".", 1)[-1].casefold() == "read_command_output"
                                                and str(receipt_metadata.get("status") or "").casefold() == "exited"
                                            ):
                                                # Launch-to-completion window: unchanged only if
                                                # the whole workspace is identical to the launch.
                                                window_before = background_windows.pop(
                                                    str(receipt_metadata.get("job_id") or "").strip(), None
                                                )
                                                receipt_metadata.update(_command_workspace_evidence(
                                                    window_before,
                                                    await asyncio.to_thread(
                                                        _workspace_content_observation,
                                                        request["workspace"],
                                                        request.get("required_outputs") or (),
                                                        scope_paths=owned_scope_paths,
                                                    ),
                                                ))
                                            receipt = await run_service.record_tool_call(
                                                run_id,
                                                tool_name=tool_name,
                                                arguments=(
                                                    tool_result.get("arguments")
                                                    or tool_result.get("args")
                                                    or raw.get("tool_args")
                                                    or {}
                                                ),
                                                result=(
                                                    tool_result.get("output")
                                                    or tool_result.get("result")
                                                    or tool_result
                                                ),
                                                success=bool(tool_result.get("success")),
                                                mutation_confirmed=bool(tool_result.get("mutation_confirmed")),
                                                tool_call_id=str(
                                                    raw.get("operation_id")
                                                    or raw.get("tool_call_id")
                                                    or tool_result.get("tool_call_id")
                                                    or ""
                                                ),
                                                metadata=receipt_metadata,
                                            )
                                            if receipt is None:
                                                raise AgentTaskInvocationError("audit_write_failed", "The tool result could not be durably recorded", 409)
                                    run_event = await run_service.record_event(
                                        run_id,
                                        event_type=f"api_agent_task.{event_type}"[:160],
                                        payload=(
                                            {**safe, **_llm_timing_run_usage(run_id, raw)}
                                            if event_type == "llm_timing"
                                            else safe
                                        ),
                                    )
                                    if run_event is None:
                                        raise AgentTaskInvocationError("audit_write_failed", "The AgentRun event could not be durably recorded", 409)
                                    work_event = await coordinator.record_event(
                                        claim=claim,
                                        event_type=f"api_agent_task.{event_type}"[:160],
                                        payload=safe,
                                    )
                                    if work_event is None:
                                        raise AgentTaskInvocationError("audit_write_failed", "The AgentWork event could not be durably recorded", 409)

                                if issued_harness_scope is not None:
                                    await record_runtime_event(
                                        "execution_backend",
                                        {
                                            "backend_kind": issued_harness_scope.backend_kind,
                                            "file_scoped": issued_harness_scope.file_scoped,
                                            "filesystem_isolation": issued_harness_scope.filesystem_isolation,
                                        },
                                    )
                                def output_cancelled() -> bool:
                                    return cancel_handle is not None and cancel_handle.cancel_requested.is_set()

                                await self._ensure_output_baseline(request, run_id, output_cancelled)
                                # Appended to this attempt's first input only: corrective
                                # turns continue the live history, which already holds it.
                                async def generate_within_budget(text: str) -> Any:
                                    nonlocal budget_timed_out, provider_terminal, event_provider_terminal
                                    # Each explicit turn owns its evidence. A previous request's
                                    # clean stop cannot settle an interrupted corrective turn.
                                    provider_terminal = event_provider_terminal = None
                                    if hasattr(client, "_last_provider_terminal_evidence"):
                                        client._last_provider_terminal_evidence = None
                                    if hasattr(client, "_last_provider_failure"):
                                        client._last_provider_failure = None
                                    try:
                                        value = await _generate_within_budget(
                                            budget,
                                            cancel_handle,
                                            lambda: client.generate_response_async(
                                                text, stream_callback=record_runtime_event
                                            ),
                                        )
                                        client_terminal = sanitize_terminal_evidence(
                                            getattr(client, "_last_provider_terminal_evidence", None)
                                        )
                                        # Neither projection may erase a proven interruption
                                        # just because the other still holds a clean stop.
                                        candidates = (client_terminal, event_provider_terminal)
                                        provider_terminal = next((
                                            item for item in candidates
                                            if item is not None and item["state"] in FAILED_TERMINAL_STATES
                                        ), client_terminal or event_provider_terminal)
                                        if provider_terminal is not None and provider_terminal["state"] == "continuation":
                                            # This call returned to task settlement without
                                            # consuming the provider's continuation request.
                                            provider_terminal = protocol_error_terminal(
                                                provider_terminal, "unconsumed_continuation"
                                            )
                                        return value
                                    except TimeoutError:
                                        budget_timed_out = True
                                        raise

                                result = await generate_within_budget(
                                    request["prompt"] + _runtime_capability_guidance(request, scope)
                                )
                                from ..llm.tool_policy import structured_task_policy_denial

                                task_policy_denial = structured_task_policy_denial()
                                # Only the explicit required_outputs contract can ask for a
                                # correction.  It continues the SAME live provider history
                                # (original tool results untouched, nothing rebuilt or
                                # re-persisted) with only the corrective note as input; with
                                # no usable history the task fails closed.  Progress is the
                                # required-output gap shrinking against the snapshot taken
                                # before the turn (a file-tool mutation is secondary), so
                                # reads cannot keep the loop alive.
                                gaps_before: list[str] = []
                                while (
                                    not task_policy_denial
                                    and (provider_terminal or {}).get("state") not in FAILED_TERMINAL_STATES
                                    and request.get("required_outputs")
                                    and not (read_stagnation_policy and read_stagnation_policy.stopped)
                                    and corrective_turns < _MAX_CORRECTIVE_TURNS
                                ):
                                    if output_cancelled():
                                        raise GenerationCancelled("task execution was cancelled")
                                    try:
                                        budget.checkpoint()
                                    except TimeoutError:
                                        budget_timed_out = True
                                        raise
                                    accepted, code, _ = await self._completion_evidence(run_id, request)
                                    if accepted or code not in _CORRECTABLE_EVIDENCE_CODES:
                                        break
                                    rows = await self._receipts_for_run(run_id)
                                    if _tool_failure_ledger(rows, request).unresolved:
                                        break
                                    gaps = await asyncio.to_thread(_required_output_gaps, request, rows, output_cancelled)
                                    if not gaps:
                                        break
                                    if corrective_turns and not _correction_made_progress(gaps_before, gaps, rows_before_correction, rows):
                                        break
                                    if not _native_history_usable(client):
                                        break
                                    gaps_before, rows_before_correction = gaps, rows
                                    corrective_turns += 1
                                    # Audit only (also feeds a later restart's reconstruction):
                                    # the final answer is not a streamed event.
                                    await record_runtime_event("assistant_text", {"text": str(result or "")[:8000]})
                                    await record_runtime_event(
                                        "corrective_turn",
                                        {"turn": corrective_turns, "missing": ", ".join(gaps)},
                                    )
                                    result = await generate_within_budget(_corrective_note(gaps))
                                    task_policy_denial = structured_task_policy_denial()
                                if issued_harness_scope is not None:
                                    _closed_jobs, interrupted_jobs = await asyncio.to_thread(
                                        _close_run_background_jobs,
                                        scope,
                                    )
                                    if interrupted_jobs:
                                        return ExecutionOutcome(
                                            classification="permanent",
                                            error_code="background_job_incomplete",
                                            error_message="A run-scoped background command was still active when the Agent task finished",
                                        )
                                await self._checkpoint(claim, run_id, request, run)
                        finally:
                            if harness_context is not None:
                                harness_context.__exit__(None, None, None)
            finally:
                self._active_provider_client = None
                cleanup_cancelled = False
                try:
                    if owns_client:
                        cleanup = getattr(client, "cleanup", None)
                        if callable(cleanup):
                            value = cleanup()
                            if inspect.isawaitable(value):
                                cleanup_task = asyncio.ensure_future(value)
                                while not cleanup_task.done():
                                    try:
                                        await asyncio.shield(cleanup_task)
                                    except asyncio.CancelledError:
                                        cleanup_cancelled = True
                                cleanup_task.result()
                except Exception:
                    # Do not leak provider errors or mask cancellation/settlement.
                    logger.warning("API Agent task client cleanup failed")
                finally:
                    if tool_scope_token is not None:
                        from ..llm.tool_policy import reset_current_agent_tool_scope

                        reset_current_agent_tool_scope(tool_scope_token)
                    if workspace_scope_token is not None:
                        from ..llm.tool_policy import reset_current_agent_workspace_scope

                        reset_current_agent_workspace_scope(workspace_scope_token)
                    if budget_token is not None:
                        current_generation_budget.reset(budget_token)
                    if read_stagnation_token is not None:
                        from ..llm.read_stagnation import _ACTIVE as _read_stagnation

                        _read_stagnation.reset(read_stagnation_token)
                    reset_current_generation_cancellation(cancel_token)
                    release_generation_cancellation(cancel_handle)
                    reset_current_agent_run_id(run_token)
                    reset_turn_context(context_token)
                    # A late cleanup exception must not turn an interrupted
                    # task into success after its cancellation was observed.
                    if cleanup_cancelled:
                        raise asyncio.CancelledError
            if cancel_handle is not None and cancel_handle.cancel_requested.is_set():
                raise GenerationCancelled("task execution was cancelled")
            from .agent_run_service import sanitize_assistant_display_text

            safe_result = sanitize_assistant_display_text(str(result or ""))
            if task_policy_denial:
                # A tool-policy denial is terminal: never settle it as success
                # or as a retryable missing-evidence failure. The Agent's last
                # answer is kept separately and never becomes a task result.
                return ExecutionOutcome(
                    classification="blocked",
                    error_code="tool_policy_denied",
                    error_message="A task tool call was denied by the tool policy",
                    result={"final_response": safe_result, "completion_evidence": "tool_policy_denied"},
                )
            if provider_terminal is not None and provider_terminal["state"] in FAILED_TERMINAL_STATES:
                # Tool receipts prove individual effects, not completion of the
                # provider's final answer. Preserve those effects without an
                # automatic retry after an uncertain provider/transport outcome.
                evidence_code = {
                    "incomplete": "provider_output_incomplete",
                    "interrupted": "provider_transport_interrupted",
                    "protocol_error": "provider_protocol_error",
                }[provider_terminal["state"]]
                # Only a structured failure re-validated here is kept; the
                # error code and failed classification stay unchanged.
                safe_failure = terminal_failure_event_fields(
                    {"provider_failure": getattr(client, "_last_provider_failure", None)}
                ).get("provider_failure")
                failure_extra = await self._provider_failure_evidence(
                    run_id, safe_failure, usage_records=getattr(client, "_last_usage_records", None)
                )
                return ExecutionOutcome(
                    # An exhausted temporary 429 is blocked, never transient: a
                    # transient outcome would re-run the whole task.
                    classification="blocked" if is_temporary_429_failure(safe_failure) else "permanent",
                    error_code=evidence_code,
                    error_message="The provider did not complete the task response",
                    result={
                        "final_response": safe_result,
                        "completion_evidence": evidence_code,
                        "provider_terminal": provider_terminal,
                        **({"provider_failure": safe_failure} if safe_failure is not None else {}),
                        **({"corrective_turns": corrective_turns} if corrective_turns else {}),
                    },
                    **failure_extra,
                )
            if read_stagnation_policy is not None and read_stagnation_policy.stopped:
                return ExecutionOutcome(
                    classification="permanent",
                    error_code="read_stagnation",
                    error_message="The task stopped at a checkpoint: reads stopped yielding new information without any change",
                    result={
                        "final_response": safe_result,
                        "completion_evidence": "read_stagnation",
                        "stop_signature": read_stagnation_policy.stop_signature,
                    },
                )
            evidence_ok, evidence_code, tool_failures = await self._completion_evidence(run_id, request)
            if not evidence_ok:
                missing_outputs = (
                    await asyncio.to_thread(_required_output_gaps, request, await self._receipts_for_run(run_id))
                    if evidence_code == "task_required_output_missing" else []
                )
                return ExecutionOutcome(
                    classification="permanent",
                    error_code=evidence_code,
                    error_message="The task goal is not verified by successful tool receipts",
                    result={
                        "final_response": safe_result,
                        "completion_evidence": evidence_code,
                        "tool_failures": tool_failures,
                        **({"provider_terminal": provider_terminal} if provider_terminal is not None else {}),
                        **({"missing_required_outputs": missing_outputs} if missing_outputs else {}),
                        **({"corrective_turns": corrective_turns} if corrective_turns else {}),
                    },
                )
            return ExecutionOutcome(
                classification="succeeded",
                result_summary="API Agent task completed with successful tool receipts",
                result={
                    "response": safe_result,
                    "successful_tool_receipts": True,
                    "completion_evidence": "verified_tool_evidence",
                    "tool_failures": tool_failures,
                    **({"provider_terminal": provider_terminal} if provider_terminal is not None else {}),
                    **(
                        {"required_output_provenance": await asyncio.to_thread(
                            _required_output_evidence, request, await self._receipts_for_run(run_id)
                        )}
                        if request.get("required_outputs") else {}
                    ),
                    **({"corrective_turns": corrective_turns} if corrective_turns else {}),
                },
                # The Run already holds this usage (summed live from the
                # provider timing events); Work only mirrors it.
                usage=(success_usage := await self._safe_prior_run_usage(run_id)),
                metadata={
                    "task_id": claim.source_id, "provider": route["provider"], "model": route["model"],
                    **({"usage_state": "known"} if success_usage else {}),
                },
            )
        except asyncio.CancelledError:
            if scope is not None and issued_harness_scope is not None:
                await asyncio.to_thread(_close_run_background_jobs, scope)
            raise
        except Exception as exc:
            if scope is not None and issued_harness_scope is not None:
                try:
                    await asyncio.to_thread(_close_run_background_jobs, scope)
                except Exception:
                    return ExecutionOutcome(
                        classification="permanent",
                        error_code="background_job_settlement_failed",
                        error_message="Run-scoped background commands could not be verified stopped",
                    )
            code = str(getattr(exc, "code", "") or "").strip()
            stop_requested = cancel_handle is not None and cancel_handle.cancel_requested.is_set()
            # Budget expiry is recognised before cancellation: expiring asks the
            # tools to stop (cancel_requested), which must not rename the outcome.
            timed_out = budget_timed_out or (
                not stop_requested
                and (isinstance(exc, TimeoutError) or (budget is not None and budget.expired()))
            )
            if timed_out:
                return ExecutionOutcome(
                    classification="permanent",
                    error_code="timeout",
                    error_message="The task deadline elapsed during execution",
                    result=await self._interrupted_evidence(run_id, request, corrective_turns),
                )
            if isinstance(exc, GenerationCancelled) or code in {"cancelled", "generation_cancelled"} or stop_requested:
                return ExecutionOutcome(
                    classification="cancelled",
                    error_code="cancelled",
                    error_message="Task cancellation completed",
                    result=await self._interrupted_evidence(run_id, request, corrective_turns),
                )
            if isinstance(exc, ModelOutputIncompleteError) and exc.request_type == "review":
                return ExecutionOutcome(
                    classification="permanent",
                    error_code="provider_review_output_incomplete",
                    error_message="The provider did not complete the task review",
                    result={
                        "completion_evidence": "provider_review_output_incomplete",
                        "provider_terminal": sanitize_terminal_evidence(
                            getattr(client, "_last_provider_terminal_evidence", None)
                        ),
                        "provider_review_terminal": sanitize_terminal_evidence(
                            getattr(exc, "provider_terminal", None)
                        ),
                    },
                )
            provider_failure = extract_provider_failure(exc)
            if provider_failure is not None:
                # A bare HTTP status is not a framework error code. Keep the
                # authoritative receipts and mark usage unknown, never zero.
                return await self._provider_failure_outcome(run_id, provider_failure)
            if code:
                return ExecutionOutcome(classification="blocked" if code in {"permission_denied", "workspace_not_allowed", "lease_lost", "agent_binding_mismatch", "agent_route_mismatch", "tool_capability_unknown", "tool_calling_unsupported"} else "permanent", error_code=code, error_message="API Agent task execution was denied or unavailable")
            logger.warning("API Agent task execution failed (%s)", type(exc).__name__)
            return ExecutionOutcome(classification="permanent", error_code="agent_task_execution_failed", error_message="API Agent task execution failed")
        finally:
            # Success, failure or cancellation: an empty scratch directory this
            # run created must not be left in the (possibly unconfined) workspace.
            if issued_harness_scope is not None:
                from .harness_execution_scope_service import release_created_scratch_roots

                try:
                    release_created_scratch_roots(issued_harness_scope)
                except Exception:
                    logger.debug("API Agent task scratch cleanup failed", exc_info=True)

    async def _provider_failure_evidence(self, run_id: str, provider_failure: Mapping[str, Any] | None, *, usage_records: Any = None) -> dict[str, Any]:
        """Known receipts and usage of the Run beside a provider failure.

        Unavailable receipts or usage stay explicitly ``unknown``, never zero.
        """

        receipts = await self._safe_receipts_for_run(run_id)
        persisted_usage = await self._safe_prior_run_usage(run_id)
        if await self._run_has_timing_usage(run_id):
            # Completed requests were persisted live from their timing events.
            usage_records = None
        current_usage = None
        if isinstance(usage_records, (list, tuple)):
            # Requests this attempt already answered but did not yet persist on
            # the Run: only valid counters, summed like the Run does.
            from .agent_run_service import _merge_agent_run_usage, _normalized_agent_run_usage

            totals: dict[str, Any] = {}
            for record in usage_records:
                for key, value in safe_usage_snapshot(record).items():
                    totals[key] = totals.get(key, 0) + value
            current_usage = _normalized_agent_run_usage(totals)
        if persisted_usage is not None and current_usage is not None:
            prior_usage = _merge_agent_run_usage(persisted_usage, current_usage)
        else:
            prior_usage = persisted_usage if persisted_usage is not None else current_usage
        receipt_known = receipts is not None
        return {
            "evidence": [
                {
                    "kind": "tool_receipt",
                    "tool_call_id": str(getattr(item, "tool_call_id", "") or "")[:96],
                    "tool_name": str(getattr(item, "tool_name", "") or "")[:96],
                    "success": bool(getattr(item, "success", False)),
                }
                for item in (receipts or ())[:16]
            ],
            "usage": prior_usage,
            "metadata": {
                **({"provider_failure": dict(provider_failure)} if provider_failure is not None else {}),
                **({"retryable": True} if is_temporary_429_failure(provider_failure) else {}),
                "usage_state": "known" if prior_usage else "unknown",
                # Only this attempt's unsaved usage is added to the Run; the
                # persisted part must not be merged twice.
                **({"unpersisted_usage": current_usage} if current_usage is not None else {}),
                "evidence_state": "known" if receipt_known else "unknown",
                "tool_receipt_count": len(receipts) if receipt_known else "unknown",
                "tool_receipt_success_count": (
                    sum(1 for item in receipts if bool(getattr(item, "success", False)))
                    if receipt_known else "unknown"
                ),
            },
        }

    async def _provider_failure_outcome(self, run_id: str, provider_failure: Mapping[str, Any]) -> ExecutionOutcome:
        # A bare HTTP status is not a framework error code. Keep the
        # authoritative receipts and mark usage unknown, never zero.
        extra = await self._provider_failure_evidence(run_id, provider_failure)
        return ExecutionOutcome(
            classification="blocked" if is_temporary_429_failure(provider_failure) else "permanent",
            error_code="provider_rate_limited" if provider_failure["http_status"] == 429 else "provider_http_error",
            error_message="The provider rejected the request",
            result={
                "provider_failure": dict(provider_failure),
                "in_flight_effects_unverified": True,
            },
            **extra,
        )

    async def _safe_receipts_for_run(self, run_id: str) -> Sequence[Any] | None:
        try:
            return await self._receipts_for_run(run_id)
        except Exception:
            logger.warning("API Agent task receipts unavailable for provider failure")
            return None

    async def _run_has_timing_usage(self, run_id: str) -> bool:
        from .agent_run_service import AgentRunService

        try:
            run = await AgentRunService(self.db, config=self.config).get_run(run_id)
        except Exception:
            return False
        metadata = run.get("metadata") if isinstance(run, Mapping) else None
        keys = metadata.get("_usage_event_keys") if isinstance(metadata, Mapping) else None
        return any(str(key).startswith("timing:") for key in keys or ())

    async def _safe_prior_run_usage(self, run_id: str) -> dict[str, Any] | None:
        """Authoritative usage already persisted on the Run, or None when unavailable."""

        from .agent_run_service import AgentRunService

        try:
            run = await AgentRunService(self.db, config=self.config).get_run(run_id)
        except Exception:
            logger.warning("API Agent task prior usage unavailable for provider failure")
            return None
        usage = run.get("usage") if isinstance(run, Mapping) else None
        return dict(usage) if isinstance(usage, Mapping) and usage else None

    async def _interrupted_evidence(self, run_id: str, request: Mapping[str, Any], corrective_turns: int) -> dict[str, Any] | None:
        """Required-output gaps and the failure ledger at a timeout/cancel, best effort.

        Evidence only: it never changes the interrupted outcome.  Generation
        and tool workers are cancelled cooperatively, so no claim is made that
        their side effects have stopped.
        """

        if not request.get("required_outputs"):
            return None
        try:
            _, _, tool_failures = await self._completion_evidence(run_id, request)
            missing = await asyncio.to_thread(_required_output_gaps, request, await self._receipts_for_run(run_id))
        except Exception:
            logger.warning("API Agent task interrupted-outcome evidence unavailable")
            return None
        return {
            "completion_evidence": "task_required_output_missing" if missing else "interrupted",
            "tool_failures": tool_failures,
            "in_flight_effects_unverified": True,
            **({"missing_required_outputs": missing} if missing else {}),
            **({"corrective_turns": corrective_turns} if corrective_turns else {}),
        }

    async def cancel(self, task_id: str, *, owner: Any) -> dict[str, Any]:
        owner_id = _owner_uuid(owner)
        row = await self._find_work(task_id, owner=owner_id)
        metadata = row.metadata_json if isinstance(row.metadata_json, Mapping) else {}
        # Queued, restart-requeued and restart-parked Runs have no live
        # adapter to settle them; close the Run with the Work.
        pending_run_id = (
            str(metadata.get("agent_run_id") or "")
            if str(row.state or "") in {"pending", "retry_wait", "awaiting_approval"}
            else ""
        )
        ok = await self.coordinator.cancel(
            str(row.id),
            actor={"id": owner_id, "actor_type": "human"},
            reason="owner_cancelled",
        )
        if not ok:
            raise AgentTaskInvocationError("not_found", "task not found or already terminal", 404)
        if pending_run_id:
            from .agent_run_service import AgentRunService

            try:
                await AgentRunService(self.db, config=self.config).cancel_run(
                    pending_run_id,
                    message="Task cancelled before execution started",
                )
            except Exception:
                logger.debug("Unable to cancel queued API Agent task Run", exc_info=True)
        return await self._task_projection(task_id, owner=owner_id)

    async def resume(self, task_id: str, *, owner: Any, confirmed_operation_ids: Sequence[str] = ()) -> dict[str, Any]:
        """Owner continuation of a task parked by restart reconciliation.

        The evidence is evaluated again first, so a digest that now proves an
        operation settles it. An operation that is still unknown continues
        only when the owner names its operation id, and the model is told the
        outcome is unknown; nothing is recorded as succeeded. Cancellation,
        a denied call or lost authority is never bypassed: the requeued claim
        re-runs source refresh, authority and the task fences.
        """

        from ..memory.models import AgentWorkItem
        from .agent_run_service import AgentRunService

        owner_id = _owner_uuid(owner)
        confirmed = frozenset(_string_list(list(confirmed_operation_ids), name="confirmed_operation_ids"))
        row = await self._find_work(task_id, owner=owner_id)
        if row.state != "awaiting_approval" or row.safe_error_code != "restart_reconciliation_required":
            raise AgentTaskInvocationError("not_awaiting_reconciliation", "The task is not waiting for restart reconciliation", 409)
        metadata = row.metadata_json if isinstance(row.metadata_json, Mapping) else {}
        await self._authorise_caps(str(row.assigned_agent_id), str(row.agent_revision_id), tuple(row.required_capabilities_json or ()))
        await self._evaluate_task_workspace(
            {"workspace": str(metadata.get("workspace") or "")},
            agent_id=str(row.assigned_agent_id),
            agent_revision_id=str(row.agent_revision_id),
            owner_user_id=owner_id,
            command=False,
        )
        session = await self._session()
        try:
            locked = (await session.execute(
                select(AgentWorkItem).where(AgentWorkItem.id == row.id).with_for_update()
            )).scalars().first()
            if locked is None or locked.state != "awaiting_approval" or locked.safe_error_code != "restart_reconciliation_required":
                raise AgentTaskInvocationError("not_awaiting_reconciliation", "The task is not waiting for restart reconciliation", 409)
            metadata = dict(locked.metadata_json) if isinstance(locked.metadata_json, Mapping) else {}
            run_id = locked.active_agent_run_id
            run_status, receipts, starts = await self._restart_evidence(session, locked.id, run_id)
            state, code, message, operations = _restart_recovery_plan(
                metadata, run_status=run_status, receipts=receipts, starts=starts, confirmed=confirmed
            )
            if state == "awaiting_approval":
                raise AgentTaskInvocationError(code, message, 409)
            if state == "retry_wait" and locked.deadline is not None and locked.deadline <= datetime.utcnow():
                # A confirmation cannot revive an elapsed hard deadline.
                raise AgentTaskInvocationError("deadline_expired", "The task hard deadline elapsed; it cannot be resumed", 409)
            locked.metadata_json = {**metadata, "restart_operations": operations}
            if state == "retry_wait":
                await _requeue_for_continuation(
                    session,
                    locked,
                    self.coordinator,
                    now=datetime.utcnow(),
                    run_id=run_id,
                    code="restart_reconciliation_confirmed",
                    payload={"confirmed_operation_ids": sorted(
                        str(item["operation_id"]) for item in operations if item.get("outcome") == "unknown_confirmed"
                    )},
                    actor_kind="human",
                    actor_id=owner_id,
                )
            else:
                await self.coordinator._transition_locked(session, locked, state, token=None, error_code=code, error_message=message)
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()
        if state != "retry_wait" and run_id:
            # The parked Run has no adapter left to settle it.
            await AgentRunService(self.db, config=self.config).fail_run(
                str(run_id), message, status="cancelled" if state == "cancelled" else "failed"
            )
        return await self._task_projection(task_id, owner=owner_id)

    async def tail(self, task_id: str, *, owner: Any, limit: int = 50, max_bytes: int = 16000) -> dict[str, Any]:
        from ..memory.models import AgentWorkEvent

        projection = await self._task_projection(task_id, owner=_owner_uuid(owner))
        row = await self._find_work(task_id, owner=_owner_uuid(owner))
        session = await self._session()
        try:
            all_rows = (await session.execute(
                select(AgentWorkEvent)
                .where(AgentWorkEvent.work_item_id == row.id)
                .order_by(desc(AgentWorkEvent.sequence))
                .limit(max(1, min(limit, 200)))
            )).scalars().all()
            events: list[dict[str, Any]] = []
            total = 0
            truncated = False
            for event in reversed(all_rows):
                value = event.to_safe_dict()
                encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if total + len(encoded) > max(1024, min(max_bytes, 64000)):
                    truncated = True
                    continue
                total += len(encoded)
                events.append(value)
            count = await session.scalar(select(func.count()).select_from(AgentWorkEvent).where(AgentWorkEvent.work_item_id == row.id))
            return {"task_id": task_id, "status": projection["status"], "events": events, "truncated": truncated, "event_count": int(count or 0)}
        finally:
            await session.close()


__all__ = [
    "AGENT_TASK_ADAPTER_KEY",
    "AGENT_TASK_SOURCE_TYPE",
    "AgentTaskExecutionAdapter",
    "AgentTaskInvocationError",
    "AgentTaskInvocationService",
    "AgentTaskWorkSource",
]
