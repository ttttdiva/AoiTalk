"""Immutable, server-owned authorized scope for Muse scoped tool calls.

This module is a small generalisation of the fixed QA allowlist used by
``validate_muse_test_tool_call``.  It is intentionally *not* a permission
platform; the production adapter consumes only server-owned records.

Separation of inputs:

* Trusted input: a ``Mapping`` produced by a future server-owned, approved
  scope record (owner/agent/revision/profile identity, one exact workspace
  root, an exact allowlist of existing relative files, and a few exact command
  strings or argv).  It is passed to :func:`build_muse_authorized_scope` together
  with the caller's independently derived ``expected_identity``.
* Model input: only the :class:`UnifiedToolCall` handed to the hook returned by
  :func:`build_muse_authorized_call_validator`.  Model text, tool results and
  request bodies are never read here and can never widen the scope.

Selecting an execution profile id grants nothing by itself: the profile id is
only part of the identity that must match an explicitly approved record.
"""

from __future__ import annotations

import enum
import hashlib
import inspect
import json
import os
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .muse_scoped_tool_execution import MuseToolExecutionScope

SCOPE_SCHEMA = "muse-authorized-scope-v1"
AUTHORITY_SCHEMA = "muse-authorized-scope-authority-v1"

READ_FILE = "read_file"
EDIT_FILE = "edit_file"
EXECUTE_COMMAND = "execute_command"

MAX_READ_CHARS = 8000
DEFAULT_READ_CHARS = 5000
MAX_EDIT_CHARS = 16_000
MAX_FILES = 16
MAX_COMMANDS = 4
MAX_COMMAND_CHARS = 2000
MAX_RELATIVE_PATH_CHARS = 260
_MAX_ID_CHARS = 256

_TRUSTED_KEYS = frozenset(
    {
        "schema",
        "enabled",
        "scope_id",
        "owner_id",
        "agent_id",
        "agent_revision_id",
        "binding_snapshot_hash",
        # Read old server-owned scope records, but never write these aliases.
        "execution_profile_id",
        "execution_profile_revision",
        "workspace_root",
        "files",
        "commands",
        "readonly",
    }
)
_FILE_MODES = {"read": False, "read_edit": True}
_GLOB_CHARS = frozenset("*?[]{}")
# Shell control / expansion characters.  An approved command is one exact
# program invocation; chaining, redirection or variable expansion is refused.
_COMMAND_FORBIDDEN_CHARS = frozenset("&|;<>`$%^!\r\n\x00")
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)
_READ_KEYS = frozenset({"path", "start_line", "end_line", "offset", "max_chars"})
_EDIT_KEYS = frozenset({"path", "old_str", "new_str"})
_COMMAND_KEYS = frozenset({"command"})


class MuseAuthorizedScopeErrorCode(str, enum.Enum):
    INVALID_INPUT = "invalid_input"
    UNKNOWN_FIELD = "unknown_field"
    SCOPE_DISABLED = "scope_disabled"
    IDENTITY_MISMATCH = "identity_mismatch"
    INVALID_WORKSPACE_ROOT = "invalid_workspace_root"
    INVALID_PATH = "invalid_path"
    EMPTY_ALLOWLIST = "empty_allowlist"
    INVALID_COMMAND = "invalid_command"


class MuseAuthorizedScopeError(ValueError):
    """Fail-closed construction error.  Carries only a code, never raw input."""

    def __init__(self, code: MuseAuthorizedScopeErrorCode, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code.value}: {detail}" if detail else code.value)


def _fail(code: MuseAuthorizedScopeErrorCode, detail: str = "") -> MuseAuthorizedScopeError:
    return MuseAuthorizedScopeError(code, detail)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _identifier(name: str, value: Any) -> str:
    if type(value) is not str:
        raise _fail(MuseAuthorizedScopeErrorCode.INVALID_INPUT, f"{name} must be str")
    if not value or value != value.strip() or len(value) > _MAX_ID_CHARS:
        raise _fail(MuseAuthorizedScopeErrorCode.INVALID_INPUT, f"{name} must be a trimmed identifier")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise _fail(MuseAuthorizedScopeErrorCode.INVALID_INPUT, f"{name} has control characters")
    return value


@dataclass(frozen=True)
class MuseAuthorizedScopeIdentity:
    """Who the approved scope belongs to.  All five fields must match exactly."""

    owner_id: str
    agent_id: str
    agent_revision_id: str
    binding_snapshot_hash: str

    def __post_init__(self) -> None:
        for name in (
            "owner_id",
            "agent_id",
            "agent_revision_id",
            "binding_snapshot_hash",
        ):
            _identifier(name, getattr(self, name))

    def as_dict(self) -> dict[str, str]:
        return {
            "owner_id": self.owner_id,
            "agent_id": self.agent_id,
            "agent_revision_id": self.agent_revision_id,
            "binding_snapshot_hash": self.binding_snapshot_hash,
        }


@dataclass(frozen=True)
class MuseAuthorizedFile:
    """One exact, existing, workspace-relative file (posix separators)."""

    path: str
    editable: bool


@dataclass(frozen=True)
class MuseAuthorizedScope:
    """Frozen snapshot of one approved scope.  Build it with the factory only."""

    identity: MuseAuthorizedScopeIdentity
    scope_id: str
    workspace_root: Path
    files: tuple[MuseAuthorizedFile, ...]
    commands: tuple[str, ...]
    digest: str
    # ``readonly`` is intentionally outside the historical snapshot material.
    # Existing scope digests and authority revisions therefore remain stable;
    # the normalized file/command allowlist still proves that a readonly grant
    # contains no mutation or command capability.
    readonly: bool = False

    @property
    def readable_paths(self) -> frozenset[str]:
        return frozenset(item.path for item in self.files)

    @property
    def editable_paths(self) -> frozenset[str]:
        return frozenset(item.path for item in self.files if item.editable)

    @property
    def allowed_tools(self) -> frozenset[str]:
        tools: set[str] = set()
        if self.files:
            tools.add(READ_FILE)
        if self.editable_paths:
            tools.add(EDIT_FILE)
        if self.commands:
            tools.add(EXECUTE_COMMAND)
        return frozenset(tools)

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe canonical material; ``digest`` is its sha256."""

        return _snapshot_material(
            identity=self.identity,
            scope_id=self.scope_id,
            workspace_root=self.workspace_root,
            files=self.files,
            commands=self.commands,
        )


def _snapshot_material(
    *,
    identity: MuseAuthorizedScopeIdentity,
    scope_id: str,
    workspace_root: Path,
    files: Sequence[MuseAuthorizedFile],
    commands: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema": SCOPE_SCHEMA,
        "identity": identity.as_dict(),
        "scope_id": scope_id,
        "workspace_root": os.path.normcase(str(workspace_root)),
        "files": [{"path": item.path, "editable": bool(item.editable)} for item in files],
        "commands": list(commands),
    }


def _is_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return bool(path.is_symlink() or (callable(is_junction) and is_junction()))


def _canonical_root(value: Any) -> Path:
    code = MuseAuthorizedScopeErrorCode.INVALID_WORKSPACE_ROOT
    if isinstance(value, Path):
        raw = str(value)
    elif type(value) is str:
        raw = value
    else:
        raise _fail(code, "workspace_root must be str or Path")
    if not raw or raw != raw.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise _fail(code, "workspace_root is not a clean path")
    if raw.startswith(("\\\\", "//")):
        raise _fail(code, "UNC workspace roots are not allowed")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise _fail(code, "workspace_root must be absolute")
    try:
        if _is_link(candidate):
            raise _fail(code, "workspace_root must not be a link")
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise _fail(code, "workspace_root must exist") from exc
    if os.path.normcase(str(resolved)) != os.path.normcase(str(candidate)):
        raise _fail(code, "workspace_root must be canonical")
    if not resolved.is_dir():
        raise _fail(code, "workspace_root must be a directory")
    return resolved


def _recheck_root(root: Path) -> Path:
    """Re-run the build-time root primitive and require the same canonical path."""

    current = _canonical_root(str(root))
    if os.path.normcase(str(current)) != os.path.normcase(str(root)):
        raise _fail(MuseAuthorizedScopeErrorCode.INVALID_WORKSPACE_ROOT, "workspace_root changed")
    return current


def _lexical_relative_path(value: Any) -> str:
    """Exact posix relative file path; no drive, UNC, ADS, glob or traversal."""

    code = MuseAuthorizedScopeErrorCode.INVALID_PATH
    if type(value) is not str:
        raise _fail(code, "path must be str")
    if not value or len(value) > MAX_RELATIVE_PATH_CHARS:
        raise _fail(code, "path length out of range")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise _fail(code, "path has control characters")
    if "\\" in value or ":" in value:
        # Backslashes, drive letters, UNC prefixes and NTFS alternate data
        # streams are all refused; the canonical form uses "/" only.
        raise _fail(code, "path must be posix relative without ':' or '\\'")
    if value.startswith("/"):
        raise _fail(code, "absolute paths are not allowed")
    if any(ch in _GLOB_CHARS for ch in value):
        raise _fail(code, "glob patterns are not allowed")
    parts = value.split("/")
    for part in parts:
        if part in {"", ".", ".."}:
            raise _fail(code, "empty, '.' or '..' segments are not allowed")
        if part != part.strip() or part.endswith("."):
            raise _fail(code, "segments must not end in space or dot")
        if part.split(".", 1)[0].lower() in _WINDOWS_RESERVED:
            raise _fail(code, "reserved device names are not allowed")
    if PurePosixPath(value).as_posix() != value:
        raise _fail(code, "path is not in canonical form")
    return value


def _checked_file(root: Path, relative_path: str) -> Path:
    """Re-check the filesystem *now*: existing regular file, no link, inside root."""

    code = MuseAuthorizedScopeErrorCode.INVALID_PATH
    _lexical_relative_path(relative_path)
    _recheck_root(root)
    try:
        cursor = root
        for part in relative_path.split("/"):
            cursor = cursor / part
            if _is_link(cursor):
                raise _fail(code, "symlink or junction component")
        target = root.joinpath(*relative_path.split("/"))
        resolved = target.resolve(strict=True)
        resolved.relative_to(root)
        if os.path.normcase(str(resolved)) != os.path.normcase(str(target)):
            raise _fail(code, "path does not resolve to itself")
        if not resolved.is_file():
            raise _fail(code, "path must be an existing regular file")
    except MuseAuthorizedScopeError:
        raise
    except (OSError, ValueError) as exc:
        raise _fail(code, "path is missing or outside the workspace") from exc
    return resolved


def _normalize_files(root: Path, value: Any) -> tuple[MuseAuthorizedFile, ...]:
    code = MuseAuthorizedScopeErrorCode.INVALID_PATH
    if not isinstance(value, Mapping):
        raise _fail(MuseAuthorizedScopeErrorCode.INVALID_INPUT, "files must be a mapping of path to mode")
    items = list(value.items())
    if len(items) > MAX_FILES:
        raise _fail(code, "too many files")
    seen: set[str] = set()
    normalized: list[MuseAuthorizedFile] = []
    for raw_path, raw_mode in items:
        path = _lexical_relative_path(raw_path)
        if type(raw_mode) is not str or raw_mode not in _FILE_MODES:
            raise _fail(MuseAuthorizedScopeErrorCode.INVALID_INPUT, "file mode must be 'read' or 'read_edit'")
        folded = os.path.normcase(path)
        if folded in seen:
            raise _fail(code, "duplicate file entry")
        seen.add(folded)
        _checked_file(root, path)
        normalized.append(MuseAuthorizedFile(path=path, editable=_FILE_MODES[raw_mode]))
    return tuple(sorted(normalized, key=lambda item: item.path))


_SHELL_PROGRAMS = frozenset(
    {
        "cmd",
        "command",
        "powershell",
        "powershell_ise",
        "pwsh",
        "bash",
        "sh",
        "zsh",
        "dash",
        "ksh",
        "csh",
        "tcsh",
        "fish",
        "busybox",
        "wsl",
        "env",
        "start",
    }
)


def _program_name(token: str) -> str:
    name = token.strip('"').replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".com", ".bat", ".cmd"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _string_executable(command: str) -> str:
    """argv[0] of a command string with Windows ``CommandLineToArgvW`` rules.

    argv[0] is either a double-quoted run (no escapes) or the text up to the
    first whitespace.  Anything ambiguous (unterminated quote, text glued to
    the closing quote, empty executable) is refused rather than guessed.
    """

    code = MuseAuthorizedScopeErrorCode.INVALID_COMMAND
    if command.count('"') % 2:
        raise _fail(code, "unbalanced quotes in command")
    if command.startswith('"'):
        end = command.find('"', 1)
        executable = command[1:end]
        rest = command[end + 1 :]
        if rest and not rest.startswith(" "):
            raise _fail(code, "ambiguous text after the quoted executable")
    else:
        executable = command.split(" ", 1)[0]
    if not executable or executable != executable.strip():
        raise _fail(code, "empty or padded executable")
    return executable


def _refuse_interpreters(tokens: Sequence[str]) -> None:
    for token in tokens:
        if _program_name(token) in _SHELL_PROGRAMS:
            raise _fail(MuseAuthorizedScopeErrorCode.INVALID_COMMAND, "shell interpreters are not allowed")


def _normalize_command(value: Any) -> str:
    code = MuseAuthorizedScopeErrorCode.INVALID_COMMAND
    if type(value) is str:
        command = value
    elif isinstance(value, (list, tuple)):
        argv = list(value)
        if not argv or any(type(item) is not str or not item for item in argv):
            raise _fail(code, "argv must be a non-empty list of non-empty str")
        if '"' in argv[0] or argv[0] != argv[0].strip():
            raise _fail(code, "argv[0] must be a bare executable path")
        command = subprocess.list2cmdline(argv)
    else:
        raise _fail(code, "command must be str or argv list")
    if not command or command != command.strip() or len(command) > MAX_COMMAND_CHARS:
        raise _fail(code, "command must be a trimmed, bounded string")
    if any(ch in _COMMAND_FORBIDDEN_CHARS for ch in command) or any(
        ord(ch) < 32 or ord(ch) == 127 for ch in command
    ):
        raise _fail(code, "shell control characters are not allowed")
    if any(ch in _GLOB_CHARS for ch in command):
        raise _fail(code, "glob patterns are not allowed in commands")
    executable = _string_executable(command)
    # The tokenized executable must not be an interpreter, and neither may any
    # whitespace-separated fragment: an unquoted executable path containing a
    # space (``C:/Program Files/.../pwsh.exe``) is ambiguous and is refused
    # whenever one of its fragments names an interpreter.
    _refuse_interpreters([executable, *command.split(" ")])
    return command


def _normalize_commands(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise _fail(MuseAuthorizedScopeErrorCode.INVALID_INPUT, "commands must be a list")
    if len(value) > MAX_COMMANDS:
        raise _fail(MuseAuthorizedScopeErrorCode.INVALID_COMMAND, "too many commands")
    commands: list[str] = []
    for entry in value:
        command = _normalize_command(entry)
        if command in commands:
            raise _fail(MuseAuthorizedScopeErrorCode.INVALID_COMMAND, "duplicate command")
        commands.append(command)
    return tuple(sorted(commands))


def build_muse_authorized_scope(
    trusted_record: Mapping[str, Any],
    *,
    expected_identity: MuseAuthorizedScopeIdentity,
) -> MuseAuthorizedScope:
    """Build a frozen snapshot from a server-owned approved scope record.

    ``trusted_record`` must come from a server-owned approval store, never from
    model text, tool results or a request body.  ``expected_identity`` is the
    caller's own current owner/agent/revision/binding identity, derived
    independently; any difference fails closed.  The record is copied, so
    later mutation of the caller's mapping has no effect on the snapshot.
    """

    if not isinstance(expected_identity, MuseAuthorizedScopeIdentity):
        raise _fail(MuseAuthorizedScopeErrorCode.INVALID_INPUT, "expected_identity is required")
    if not isinstance(trusted_record, Mapping):
        raise _fail(MuseAuthorizedScopeErrorCode.INVALID_INPUT, "trusted_record must be a mapping")
    record = dict(trusted_record)
    unknown = set(record) - _TRUSTED_KEYS
    if unknown:
        raise _fail(MuseAuthorizedScopeErrorCode.UNKNOWN_FIELD, "unexpected field in scope record")
    if record.get("schema") != SCOPE_SCHEMA:
        raise _fail(MuseAuthorizedScopeErrorCode.INVALID_INPUT, "unsupported scope schema")
    if record.get("enabled") is not True:
        raise _fail(MuseAuthorizedScopeErrorCode.SCOPE_DISABLED, "scope is missing or disabled")
    raw_readonly = record.get("readonly", False)
    if type(raw_readonly) is not bool:
        raise _fail(MuseAuthorizedScopeErrorCode.INVALID_INPUT, "readonly must be a bool")
    try:
        if "binding_snapshot_hash" in record:
            identity = MuseAuthorizedScopeIdentity(
                owner_id=record.get("owner_id"),
                agent_id=record.get("agent_id"),
                agent_revision_id=record.get("agent_revision_id"),
                binding_snapshot_hash=record.get("binding_snapshot_hash"),
            )
        else:
            # Existing approved scopes used the Agent revision UUID and
            # binding hash in fields named for the removed ExecutionProfile.
            # Accept that stored shape only when both values prove this exact
            # revision and immutable binding.
            legacy_profile_id = record.get("execution_profile_id")
            legacy_binding_hash = record.get("execution_profile_revision")
            if legacy_profile_id != record.get("agent_revision_id"):
                raise _fail(
                    MuseAuthorizedScopeErrorCode.IDENTITY_MISMATCH,
                    "legacy scope does not bind the Agent revision",
                )
            identity = MuseAuthorizedScopeIdentity(
                owner_id=record.get("owner_id"),
                agent_id=record.get("agent_id"),
                agent_revision_id=record.get("agent_revision_id"),
                binding_snapshot_hash=legacy_binding_hash,
            )
    except MuseAuthorizedScopeError as exc:
        raise _fail(MuseAuthorizedScopeErrorCode.IDENTITY_MISMATCH, "scope identity is incomplete") from exc
    if identity != expected_identity:
        raise _fail(MuseAuthorizedScopeErrorCode.IDENTITY_MISMATCH, "scope identity does not match")
    scope_id = _identifier("scope_id", record.get("scope_id"))
    root = _canonical_root(record.get("workspace_root"))
    files = _normalize_files(root, record.get("files"))
    commands = _normalize_commands(record.get("commands"))
    if not files and not commands:
        raise _fail(MuseAuthorizedScopeErrorCode.EMPTY_ALLOWLIST, "scope grants nothing")
    inferred_readonly = not commands and not any(item.editable for item in files)
    if "readonly" in record and raw_readonly is False and inferred_readonly:
        raise _fail(
            MuseAuthorizedScopeErrorCode.INVALID_INPUT,
            "readonly false is inconsistent with a command-free read-only allowlist",
        )
    readonly = bool(raw_readonly or inferred_readonly)
    if readonly and (commands or any(item.editable for item in files)):
        raise _fail(
            MuseAuthorizedScopeErrorCode.INVALID_INPUT,
            "readonly scope cannot grant edit or command tools",
        )
    digest = _sha256(
        _snapshot_material(
            identity=identity,
            scope_id=scope_id,
            workspace_root=root,
            files=files,
            commands=commands,
        )
    )
    return MuseAuthorizedScope(
        identity=identity,
        scope_id=scope_id,
        workspace_root=root,
        files=files,
        commands=commands,
        digest=digest,
        readonly=readonly,
    )


def _verified(authorized: Any) -> MuseAuthorizedScope:
    """Reject hand-made or ``dataclasses.replace``-d snapshots."""

    if not isinstance(authorized, MuseAuthorizedScope):
        raise _fail(MuseAuthorizedScopeErrorCode.INVALID_INPUT, "authorized scope required")
    if not isinstance(authorized.identity, MuseAuthorizedScopeIdentity):
        raise _fail(MuseAuthorizedScopeErrorCode.INVALID_INPUT, "authorized identity required")
    if type(authorized.readonly) is not bool:
        raise _fail(MuseAuthorizedScopeErrorCode.INVALID_INPUT, "authorized readonly flag is invalid")
    if not isinstance(authorized.files, tuple) or not isinstance(authorized.commands, tuple):
        raise _fail(MuseAuthorizedScopeErrorCode.INVALID_INPUT, "authorized scope must be frozen")
    if authorized.readonly and (authorized.commands or any(item.editable for item in authorized.files)):
        raise _fail(
            MuseAuthorizedScopeErrorCode.INVALID_INPUT,
            "readonly scope cannot grant edit or command tools",
        )
    if not authorized.files and not authorized.commands:
        raise _fail(MuseAuthorizedScopeErrorCode.EMPTY_ALLOWLIST, "scope grants nothing")
    if _sha256(authorized.snapshot()) != authorized.digest:
        raise _fail(MuseAuthorizedScopeErrorCode.INVALID_INPUT, "authorized scope digest mismatch")
    return authorized


def compose_authority_revision(base_authority_revision: str, authorized: MuseAuthorizedScope) -> str:
    """Bind the caller's existing authority revision to this scope digest."""

    base = _identifier("base_authority_revision", base_authority_revision)
    scope = _verified(authorized)
    return _sha256({"schema": AUTHORITY_SCHEMA, "base": base, "scope_digest": scope.digest})


def build_muse_authorized_execution_scope(
    authorized: MuseAuthorizedScope,
    *,
    session_id: str,
    run_id: str,
    base_authority_revision: str,
) -> MuseToolExecutionScope:
    """The existing ``MuseToolExecutionScope`` pinned to this snapshot."""

    scope = _verified(authorized)
    return MuseToolExecutionScope(
        owner_id=scope.identity.owner_id,
        session_id=session_id,
        run_id=run_id,
        workspace_root=scope.workspace_root,
        allowed_tools=scope.allowed_tools,
        authority_revision=compose_authority_revision(base_authority_revision, scope),
    )


def resolve_muse_authorized_file(
    authorized: MuseAuthorizedScope,
    relative_path: str,
    *,
    require_editable: bool = False,
) -> Path:
    """Re-validate one allowlisted file at use time (for a future router wrapper)."""

    scope = _verified(authorized)
    path = _lexical_relative_path(relative_path)
    allowed = scope.editable_paths if require_editable else scope.readable_paths
    if path not in allowed:
        raise _fail(MuseAuthorizedScopeErrorCode.INVALID_PATH, "path is not allowlisted")
    return _checked_file(scope.workspace_root, path)


def _non_negative_int(value: Any, minimum: int) -> bool:
    return type(value) is int and value >= minimum


def validate_muse_authorized_tool_call(
    authorized: MuseAuthorizedScope,
    execution_scope: MuseToolExecutionScope,
    call: Any,
    *,
    base_authority_revision: str,
) -> bool:
    """Pure fail-closed check of one model tool call against the snapshot.

    Only ``call.tool`` and ``call.arguments`` are read from the model call.  The
    filesystem is re-checked on every invocation, so a file later swapped for a
    symlink/junction or moved out of the workspace is refused.
    """

    try:
        scope = _verified(authorized)
        if not isinstance(execution_scope, MuseToolExecutionScope):
            return False
        if (
            execution_scope.owner_id != scope.identity.owner_id
            or os.path.normcase(str(execution_scope.workspace_root)) != os.path.normcase(str(scope.workspace_root))
            or execution_scope.allowed_tools != scope.allowed_tools
            or execution_scope.authority_revision != compose_authority_revision(base_authority_revision, scope)
        ):
            return False
        tool = getattr(call, "tool", None)
        raw_arguments = getattr(call, "arguments", None)
        if type(tool) is not str or tool not in scope.allowed_tools or not isinstance(raw_arguments, Mapping):
            return False
        arguments = dict(raw_arguments)
        from .outbound_privacy_service import redact_secret_for_local_display

        if redact_secret_for_local_display(arguments) != arguments:
            return False
        # Every tool, including command dispatch (which uses the root as its
        # cwd), re-checks the canonical, existing, non-link workspace root now.
        _recheck_root(scope.workspace_root)
        if tool == EXECUTE_COMMAND:
            command = arguments.get("command")
            return set(arguments) == _COMMAND_KEYS and type(command) is str and command in scope.commands
        path = arguments.get("path")
        if type(path) is not str:
            return False
        if tool == READ_FILE:
            if set(arguments) - _READ_KEYS or path not in scope.readable_paths:
                return False
            for key, minimum in (("start_line", 1), ("offset", 0), ("max_chars", 1)):
                value = arguments.get(key)
                if value is not None and not _non_negative_int(value, minimum):
                    return False
            end_line = arguments.get("end_line")
            if end_line is not None and (type(end_line) is not int or (end_line < 1 and end_line != -1)):
                return False
            if int(arguments.get("max_chars") or DEFAULT_READ_CHARS) > MAX_READ_CHARS:
                return False
        elif tool == EDIT_FILE:
            old_str = arguments.get("old_str")
            new_str = arguments.get("new_str")
            if (
                set(arguments) != _EDIT_KEYS
                or path not in scope.editable_paths
                or type(old_str) is not str
                or not old_str
                or len(old_str) > MAX_EDIT_CHARS
                or type(new_str) is not str
                or len(new_str) > MAX_EDIT_CHARS
            ):
                return False
        else:
            return False
        _checked_file(scope.workspace_root, path)
        # Reuse the existing QA allowlist as a second, independent check for
        # the same principles (link walk, resolve, normcase, size limits).
        from .muse_tool_loop import validate_muse_test_tool_call

        return (
            validate_muse_test_tool_call(
                execution_scope,
                call,
                target_test_path=path,
                fixed_test_command="\x00muse-authorized-scope-no-command\x00",
            )
            is True
        )
    except Exception:
        return False


CurrentAuthorizedScope = Callable[[], "MuseAuthorizedScope | None | Awaitable[MuseAuthorizedScope | None]"]


def build_muse_authorized_call_validator(
    authorized: MuseAuthorizedScope,
    execution_scope: MuseToolExecutionScope,
    *,
    base_authority_revision: str,
    current_authorized_scope: CurrentAuthorizedScope,
) -> Callable[[MuseToolExecutionScope, Any], Awaitable[bool]]:
    """Return ``validate_call(scope, call)`` for ``MuseScopedToolExecutor``.

    ``current_authorized_scope`` is a trusted server-side re-read (for example
    re-running :func:`build_muse_authorized_scope` on the stored approval with
    the current identity).  It runs on every check, so the executor's admission
    and pre-dispatch calls both see revocation, disablement or any change.
    """

    scope = _verified(authorized)
    if not callable(current_authorized_scope):
        raise _fail(MuseAuthorizedScopeErrorCode.INVALID_INPUT, "current_authorized_scope must be callable")
    if (
        not isinstance(execution_scope, MuseToolExecutionScope)
        or execution_scope.owner_id != scope.identity.owner_id
        or execution_scope.workspace_root != scope.workspace_root
        or execution_scope.allowed_tools != scope.allowed_tools
        or execution_scope.authority_revision != compose_authority_revision(base_authority_revision, scope)
    ):
        raise _fail(MuseAuthorizedScopeErrorCode.IDENTITY_MISMATCH, "execution scope is not pinned to this grant")

    async def validate_call(candidate: MuseToolExecutionScope, call: Any) -> bool:
        try:
            if candidate != execution_scope:
                return False
            current = current_authorized_scope()
            if inspect.isawaitable(current):
                current = await current
            if not isinstance(current, MuseAuthorizedScope):
                return False
            if _verified(current) != scope:
                return False
            return validate_muse_authorized_tool_call(
                scope,
                candidate,
                call,
                base_authority_revision=base_authority_revision,
            )
        except Exception:
            return False

    return validate_call


__all__ = [
    "AUTHORITY_SCHEMA",
    "SCOPE_SCHEMA",
    "MuseAuthorizedFile",
    "MuseAuthorizedScope",
    "MuseAuthorizedScopeError",
    "MuseAuthorizedScopeErrorCode",
    "MuseAuthorizedScopeIdentity",
    "build_muse_authorized_call_validator",
    "build_muse_authorized_execution_scope",
    "build_muse_authorized_scope",
    "compose_authority_revision",
    "resolve_muse_authorized_file",
    "validate_muse_authorized_tool_call",
]
