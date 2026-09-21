"""
LLM Function Tools for OS Operations

Provides function tools that can be called by the LLM:
- execute_command: Run shell commands
- read_file: Read one file (workspace-relative or absolute)
- create_file: Create new files
- delete_file: Delete files
- append_to_file: Append content to files
- edit_file: Edit files via string replacement
- insert_to_file: Insert content at specific line
- undo_edit: Undo last file edit
- list_directory: List directory contents (flat or bounded recursive)
- search_files: Search for files and folders by name or content
"""

import fnmatch
import functools
import logging
import ntpath
import os
import re
import stat
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import tool
from ..external_llm_permission import check_permission_sync
from ..output_truncation import format_command_output

from .background_jobs import BackgroundJobError, get_background_job_registry
from .command_executor import get_command_executor
from .file_editor import (
    FileEditorError,
    allowed_absolute_paths,
    get_file_editor,
    path_outside_allowed_error,
)
from .file_system import get_file_system, FileSystemError

try:
    from ...security.agent_run_scope import (
        AgentRunScope,
        RunScopeViolation,
        get_current_run_scope,
        run_scope_context,
    )
except ImportError:  # pragma: no cover - defensive for stripped builds
    AgentRunScope = None  # type: ignore[assignment,misc]
    RunScopeViolation = PermissionError  # type: ignore[assignment]

    def get_current_run_scope():  # type: ignore[no-redef]
        return None

    @contextmanager
    def run_scope_context(_scope):  # type: ignore[no-redef]
        yield _scope

try:
    from ...security.harness_execution_scope import (
        HarnessExecutionScope,
        get_current_harness_execution_scope,
        harness_execution_scope_context,
    )
except ImportError:  # pragma: no cover - defensive for stripped builds
    HarnessExecutionScope = None  # type: ignore[assignment,misc]

    def get_current_harness_execution_scope():  # type: ignore[no-redef]
        return None

    @contextmanager
    def harness_execution_scope_context(_scope):  # type: ignore[no-redef]
        yield _scope

logger = logging.getLogger(__name__)


def _enterprise_scope_error(exc: BaseException, *, operation: str) -> Dict[str, Any]:
    """Return a stable fail-closed result for one Enterprise operation."""

    logger.warning("Enterprise %s scope setup denied: %s", operation, exc)
    return {
        "success": False,
        "error": (
            "Enterpriseではこの操作に必要な信頼済み実行スコープを"
            f"構築できないため拒否しました: {exc}"
        ),
    }


def _enterprise_scope_config() -> Any:
    """Load the server-owned config used by the Enterprise scope factory."""

    from ...config import Config

    return Config()


def _validate_enterprise_command_backend() -> None:
    """Require the configured file-scoped backend before command calls."""

    from ...security.wsl_bwrap_backend import get_wsl_bwrap_backend

    backend = get_wsl_bwrap_backend()
    if not getattr(backend, "file_scoped", False):
        raise RuntimeError("configured command backend is not file-scoped")
    available = getattr(backend, "is_available", None)
    if not callable(available) or not available():
        raise RuntimeError("file-scoped WSL2/bubblewrap backend is unavailable")


def _enterprise_sensitive_filter_active() -> bool:
    """Whether the explicit sandbox/coding path should mask secret-like names.

    Enterprise deployment by itself is not a coding-agent execution scope.  In
    the ordinary employee lane, filename heuristics must not turn into a
    blanket authorization denial.  A bound upper or lower capability still
    opts the call into the conservative sandbox policy.
    """

    if not _enterprise_mode():
        return False
    try:
        return (
            get_current_harness_execution_scope() is not None
            or get_current_run_scope() is not None
        )
    except Exception:
        # Scope lookup is part of a security boundary.  If the trusted context
        # cannot be inspected, retain the conservative scoped policy rather
        # than exposing a secret-like path accidentally.
        logger.exception("Failed to inspect Enterprise execution scope")
        return True


@contextmanager
def _enterprise_execution_scope(*, require_backend: bool = False):
    """Validate and bind already-issued Enterprise execution capabilities.

    Enterprise deployment does not itself establish a coding/repository
    sandbox.  Ordinary employee calls therefore remain unbound.  A parent
    harness may explicitly bind a server-issued ``HarnessExecutionScope`` and
    its matching ``AgentRunScope``; only those capabilities are validated and
    (when needed) adapted for the decorated tool.  No scope is manufactured
    from the current turn or model/tool arguments here.
    """

    if not _enterprise_mode():
        yield None
        return

    upper_scope = get_current_harness_execution_scope()
    bound_scope = get_current_run_scope()

    # Ordinary Enterprise filesystem calls stay on the capability/ACL lane.
    # Command execution applies its shared-identity fail-closed check after
    # the existing permission confirmation contract; ``require_backend`` only
    # controls validation when an explicit coding scope is already active.
    if upper_scope is None and bound_scope is None:
        yield None
        return

    adapter_scope = bound_scope
    try:
        if upper_scope is not None and HarnessExecutionScope is not None and not isinstance(
            upper_scope, HarnessExecutionScope
        ):
            raise TypeError("invalid trusted HarnessExecutionScope")
        if bound_scope is not None and AgentRunScope is not None and not isinstance(
            bound_scope, AgentRunScope
        ):
            raise TypeError("invalid trusted AgentRunScope")

        if upper_scope is not None and bound_scope is None:
            # The upper capability is already server-issued and immutable.  Its
            # repository adapter is safe to bind for this call; no new
            # authority or roots are inferred.
            adapter_scope = upper_scope.to_agent_run_scope()
        elif upper_scope is None and bound_scope is not None:
            # A repository-only lower scope has no authenticated principal,
            # finite limits, secret policy, or forced COW publication contract.
            # It is sufficient for legacy Personal helpers, but Enterprise
            # execution still requires the matching server-issued upper scope.
            raise RuntimeError(
                "Enterprise AgentRunScope requires a matching server-issued "
                "HarnessExecutionScope"
            )

        # When both layers are already bound, they must describe exactly the
        # same immutable run.  A lower scope from another run must never be
        # accepted merely because an upper scope exists in the ambient context.
        if upper_scope is not None and bound_scope is not None:
            expected = upper_scope.to_agent_run_scope()
            try:
                from .background_jobs import compute_scope_fingerprint

                matching = (
                    str(getattr(bound_scope, "run_id", ""))
                    == str(getattr(expected, "run_id", ""))
                    and str(getattr(bound_scope, "repo_identity", ""))
                    == str(getattr(expected, "repo_identity", ""))
                    and compute_scope_fingerprint(bound_scope)
                    == compute_scope_fingerprint(expected)
                )
            except Exception:
                matching = False
            if not matching:
                raise RuntimeError("HarnessExecutionScope and AgentRunScope do not match")

        if require_backend:
            _validate_enterprise_command_backend()
    except Exception as exc:
        yield _enterprise_scope_error(exc, operation="execution")
        return

    # For newly built scopes bind both layers for the complete call so path
    # resolution, permission checks, and backend/registry calls all observe
    # one immutable capability fingerprint.  Enter the contexts before the
    # single yield so an enter failure is converted to a local denial, while
    # exceptions raised by the wrapped tool itself still propagate normally.
    stack = ExitStack()
    try:
        if upper_scope is not None and upper_scope is not get_current_harness_execution_scope():
            stack.enter_context(harness_execution_scope_context(upper_scope))
        if adapter_scope is not bound_scope:
            stack.enter_context(run_scope_context(adapter_scope))
    except Exception as exc:
        stack.close()
        yield _enterprise_scope_error(exc, operation="execution")
        return
    try:
        yield None
    finally:
        stack.close()


def _enterprise_scoped(*, require_backend: bool = False):
    """Decorator binding Enterprise authority around one LLM-facing tool."""

    def decorate(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            with _enterprise_execution_scope(require_backend=require_backend) as denied:
                if denied is not None:
                    return denied
                return function(*args, **kwargs)

        return wrapped

    return decorate


def _active_harness_resource_limits() -> Any:
    """Return limits from the bound upper scope, if any."""

    scope = get_current_harness_execution_scope()
    return getattr(scope, "resource_limits", None) if scope is not None else None

# --- Path Protection Utilities ---

_protected_paths_cache: Optional[List[str]] = None
_allowed_workspace_dirs_cache: Optional[List[str]] = None
_command_config_cache: Optional[Dict[str, Any]] = None

# config が読めない場合のフォールバック既定値（config_defaults.py と揃える）
_COMMAND_CONFIG_DEFAULTS: Dict[str, Any] = {
    "shell": "auto",
    "timeout_seconds": 120,
    "max_output_bytes": 32768,
    "background_enabled": True,
    "max_background_jobs": 8,
    "background_buffer_bytes": 1048576,
}

# User context for permission checks (bound to one async execution context).
_DEFAULT_USER_CONTEXT: Dict[str, Any] = {
    "user_id": None,
    # Missing context must never become an implicit administrator.  Legacy
    # unauthenticated mode explicitly installs its compatibility context via
    # set_current_user_context() during request setup.
    "is_admin": False,
    "project_ids": [],
    "writable_project_ids": [],
    "deletable_project_ids": [],
}
_current_user_context: ContextVar[Dict[str, Any]] = ContextVar(
    "aoi_os_operations_user_context",
    default=_DEFAULT_USER_CONTEXT,
)


def set_current_user_context(
    user_id: Optional[str],
    is_admin: bool,
    project_ids: Optional[List[str]] = None,
    writable_project_ids: Optional[List[str]] = None,
    deletable_project_ids: Optional[List[str]] = None,
):
    """
    Set current user context for path permission checks.
    
    This should be called before agent execution to set the user context.
    
    Args:
        user_id: User UUID as string (None for anonymous/system)
        is_admin: Whether user is admin
        project_ids: List of readable project UUIDs
        writable_project_ids: List of project UUIDs with write permission.
            Omitted callers retain the historical project_ids behavior.
        deletable_project_ids: List of project UUIDs with delete permission.
            Omitted callers retain the historical project_ids behavior.
    """
    readable = list(project_ids or [])
    _current_user_context.set({
        "user_id": user_id,
        "is_admin": is_admin,
        "project_ids": readable,
        "writable_project_ids": (
            list(writable_project_ids)
            if writable_project_ids is not None
            else readable
        ),
        "deletable_project_ids": (
            list(deletable_project_ids)
            if deletable_project_ids is not None
            else readable
        ),
    })
    logger.debug(
        "Set user context: user_id=%s, is_admin=%s, projects=%d, writable=%d",
        user_id,
        is_admin,
        len(readable),
        len(
            writable_project_ids
            if writable_project_ids is not None
            else readable
        ),
    )


def get_current_user_context() -> Dict[str, Any]:
    """Get current user context for permission checks."""
    context = _current_user_context.get()
    return {
        "user_id": context.get("user_id"),
        "is_admin": bool(context.get("is_admin", False)),
        "project_ids": list(context.get("project_ids") or []),
        "writable_project_ids": list(context.get("writable_project_ids") or []),
        "deletable_project_ids": list(context.get("deletable_project_ids") or []),
    }


def clear_user_context():
    """Clear user context and restore the fail-closed anonymous default."""
    _current_user_context.set(dict(_DEFAULT_USER_CONTEXT))


def _confirm_tool_action(
    tool_name: str,
    tool_args: Dict[str, Any],
    denied_message: str,
) -> Optional[Dict[str, Any]]:
    """Ask the user before executing a guarded LLM tool action."""
    if check_permission_sync(tool_name, tool_args):
        return None
    return {"success": False, "error": denied_message}


def _get_protected_paths() -> List[str]:
    """Get protected paths from config. Cached for performance."""
    global _protected_paths_cache
    if _protected_paths_cache is not None:
        return _protected_paths_cache
    
    try:
        from ...config import Config
        config = Config()
        os_ops_config = config.get('os_operations', {})
        paths = os_ops_config.get('protected_paths', [])
        _protected_paths_cache = paths if paths else []
    except Exception as e:
        logger.warning(f"Failed to load protected paths from config: {e}")
        _protected_paths_cache = []
    
    return _protected_paths_cache


def _get_command_config() -> Dict[str, Any]:
    """コマンド実行設定（os_operations.command）を取得する。キャッシュ付き。

    config.yaml に該当セクションが無い場合でも動作するよう、
    _COMMAND_CONFIG_DEFAULTS で必ず埋める。
    """
    global _command_config_cache
    if _command_config_cache is not None:
        return _command_config_cache

    merged = dict(_COMMAND_CONFIG_DEFAULTS)
    try:
        from ...config import Config
        config = Config()
        os_ops_config = config.get('os_operations', {}) or {}
        command_config = os_ops_config.get('command', {}) or {}
        for key, default_value in _COMMAND_CONFIG_DEFAULTS.items():
            value = command_config.get(key, default_value)
            if value is None:
                value = default_value
            merged[key] = value
    except Exception as e:
        logger.warning(f"Failed to load os_operations.command config: {e}")

    _command_config_cache = merged
    return _command_config_cache


def _get_allowed_workspace_dirs() -> List[str]:
    """Get allowed workspace directory prefixes from config. Cached for performance."""
    global _allowed_workspace_dirs_cache
    if _allowed_workspace_dirs_cache is not None:
        return _allowed_workspace_dirs_cache
    
    try:
        from ...config import Config
        config = Config()
        os_ops_config = config.get('os_operations', {})
        dirs = os_ops_config.get('allowed_workspace_dirs', ['_users', '_projects'])
        _allowed_workspace_dirs_cache = dirs if dirs else []
    except Exception as e:
        logger.warning(f"Failed to load allowed workspace dirs from config: {e}")
        _allowed_workspace_dirs_cache = ['_users', '_projects']
    
    return _allowed_workspace_dirs_cache


def _get_user_files_root() -> Path:
    """Get the user_files root directory."""
    import os
    files_dir = os.environ.get("AOITALK_WORKSPACES_DIR", "./workspaces")
    return Path(files_dir).resolve()


def _enterprise_mode() -> bool:
    """Return the fail-closed Enterprise profile state for tool boundaries."""
    try:
        from ...features import Features

        return Features.is_enterprise()
    except Exception:
        # A feature-profile lookup failure must not silently re-enable a
        # generic writer/command path.  Deny until the profile can be read.
        logger.exception("Failed to resolve Enterprise profile; failing closed")
        return True


_ENTERPRISE_MANAGED_WORKSPACE_NAMESPACES = frozenset(
    {
        "_projects",
        "_docs",
        "_apps",
        "_app_artifacts",
        "_app_instances",
    }
)


def _normalise_windows_device_alias(path: str) -> tuple[str, bool]:
    """Return a filesystem path with Windows device aliases normalised.

    ``Path.resolve`` does not consistently collapse Windows extended/device
    prefixes.  Treat drive/UNC aliases as their ordinary path while refusing
    opaque device namespaces (``GLOBALROOT``, ``pipe``, volume GUIDs, etc.).
    The second return value records that a device alias was supplied.
    """

    raw = str(path or "")
    if not raw:
        return raw, False

    # Device paths are documented with backslashes, but accepting forward
    # slashes here prevents a mixed-separator alias from bypassing the check.
    windows_form = raw.replace("/", "\\")
    for prefix in ("\\\\?\\", "\\\\.\\"):
        if not windows_form.casefold().startswith(prefix):
            continue
        rest = windows_form[len(prefix):]
        folded = rest.casefold()
        if folded.startswith("unc\\"):
            tail = rest[4:]
            if not tail or tail.startswith("\\"):
                raise ValueError("invalid Windows UNC device alias")
            return "\\\\" + tail, True
        if (
            len(rest) >= 3
            and rest[0].isalpha()
            and rest[1] == ":"
            and rest[2] == "\\"
        ):
            return rest, True
        # Do not attempt to map arbitrary devices into a filesystem path.
        raise ValueError("unsupported Windows device namespace")
    return raw, False


def _path_key(path: Path) -> str:
    """Return a case/segment-normalised key suitable for containment checks."""

    return os.path.normcase(os.path.normpath(os.fspath(path)))


def _path_is_within(path: Path, root: Path) -> bool:
    """Containment check that does not confuse sibling prefixes (``foo2``)."""

    try:
        return os.path.commonpath((_path_key(path), _path_key(root))) == _path_key(root)
    except (OSError, ValueError):
        # Different drives/UNC shares and malformed paths are never inside one
        # another for this authorization boundary.
        return False


def _existing_path_components(
    path: Path,
) -> list[tuple[Path, Optional[bool]]]:
    """Inspect existing lexical components without following links.

    The optional flag is ``None`` when metadata could not be inspected.  A
    caller enforcing an authorization boundary must treat that state as a
    denial rather than guessing from a resolved path.
    """

    anchor = Path(path.anchor) if path.anchor else Path.cwd()
    try:
        parts = path.relative_to(anchor).parts if path.anchor else path.parts
    except ValueError:
        anchor = Path.cwd()
        parts = path.parts

    current = anchor
    result: list[tuple[Path, Optional[bool]]] = []
    for part in parts:
        if not part or part == ".":
            continue
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            # A missing final component is valid for create/write operations;
            # no later component can exist once its parent is absent.
            break
        except (OSError, PermissionError, NotADirectoryError):
            result.append((current, None))
            break
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        linked = stat.S_ISLNK(metadata.st_mode) or bool(
            attributes & reparse_flag
        )
        result.append((current, linked))
    return result


def _canonical_path_identity(
    path: str,
    *,
    base: Optional[Path] = None,
) -> tuple[Path, Path, list[tuple[Path, Optional[bool]]], bool]:
    """Canonicalise one path while retaining its lexical identity.

    Both the lexical path and the ``realpath`` are returned so callers can
    reject links that escape a trusted root in either direction.  ``base`` is
    used only for relative paths; ordinary tool callers normally pass an
    already-resolved absolute path.
    """

    normalised, device_alias = _normalise_windows_device_alias(path)
    if "\x00" in normalised:
        raise ValueError("path contains NUL")

    # ``ntpath`` catches Windows absolute/device input even when a stripped
    # build is exercised on a POSIX host.  Such a path cannot be safely mapped
    # to that host's filesystem and is therefore handled as invalid below.
    if not os.path.isabs(normalised):
        if ntpath.isabs(normalised) and os.name != "nt":
            raise ValueError("Windows absolute path on non-Windows host")
        normalised = os.path.join(os.fspath(base or Path.cwd()), normalised)

    lexical = Path(os.path.normpath(os.path.abspath(normalised)))
    try:
        resolved = Path(os.path.realpath(os.fspath(lexical)))
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("path cannot be canonicalized") from exc
    components = _existing_path_components(lexical)
    return lexical, resolved, components, device_alias


def _native_employee_capability() -> Any | None:
    """Return the currently bound, validated native employee capability.

    Native execution is an explicit alternate lane for ordinary Enterprise
    requests.  The capability is issued/bound by the request dispatcher and
    proves that this process is running for one authenticated endpoint/user;
    it is never reconstructed from model arguments.  Keep the imports lazy so
    tool discovery does not create a security-service <-> tools cycle.

    A missing/invalid capability is represented as ``None``.  Callers that
    need a user-facing error deliberately use the existing Enterprise
    fail-closed helpers instead of leaking validation details.
    """

    if not _enterprise_mode():
        return None
    try:
        from ...security.native_employee_execution import (
            get_current_native_employee_execution_capability,
            validate_native_employee_execution_capability,
        )
    except (ImportError, AttributeError):
        return None
    try:
        capability = get_current_native_employee_execution_capability()
    except Exception:
        logger.exception("Failed to inspect native employee execution capability")
        return None
    if capability is None:
        return None

    # If the native execution service exposes a live-dispatch validator, use
    # it in addition to the value-only security validator.  This is where
    # endpoint configuration, current process SID/token and request/session
    # binding are checked.  Keep this optional for Personal/stripped builds;
    # Enterprise with a present but uncheckable capability fails closed below.
    try:
        from ...services import native_employee_execution_service as service

        # Prefer the full service validator (config + endpoint/SID mapping).
        # The explicit ``current`` form requires a Config argument; test or
        # stripped adapters may expose the shorter capability-only form.
        live_validator = getattr(
            service,
            "validate_current_native_employee_execution_capability",
            None,
        )
        if callable(live_validator):
            try:
                live_validated = live_validator(capability, _enterprise_scope_config())
            except TypeError:
                live_validated = live_validator(capability)
            if live_validated is False:
                return None
            if live_validated is not None:
                capability = live_validated
    except (ImportError, AttributeError):
        pass
    except Exception:
        logger.warning(
            "Native employee live capability validation failed",
            exc_info=True,
        )
        return None

    # Revalidate the opaque handle and bind it to the authenticated request
    # context.  ``validate_*`` intentionally owns token/expiry checks; this
    # additional principal comparison prevents a capability for another user
    # from being used when a caller's ContextVar was switched or omitted.
    try:
        validated = validate_native_employee_execution_capability(capability)
    except Exception:
        logger.warning("Native employee execution capability validation failed", exc_info=True)
        return None
    if validated is False:
        return None
    capability = capability if validated is None else validated

    context = get_current_user_context()
    active_user = context.get("user_id")
    capability_user = getattr(capability, "authenticated_user_id", None)
    if capability_user is None:
        capability_user = getattr(capability, "principal_id", None)
    if capability_user is None:
        capability_user = getattr(capability, "user_id", None)
    if not active_user or capability_user is None:
        return None
    if str(capability_user).casefold() != str(active_user).casefold():
        logger.warning(
            "Native employee capability principal mismatch: capability=%s active=%s",
            capability_user,
            active_user,
        )
        return None
    return capability


def _enterprise_explicit_scope_active() -> bool:
    """Whether coding/harness authority is bound for the current call."""

    try:
        return bool(
            get_current_harness_execution_scope() is not None
            or get_current_run_scope() is not None
        )
    except Exception:
        # If scope inspection fails, callers must not select the less
        # restrictive native lane.
        return True


def _native_employee_capability_error(operation: str) -> Dict[str, Any]:
    """Stable denial returned when ordinary Enterprise lacks native authority."""

    return {
        "success": False,
        "error": (
            "Enterpriseの通常Agentはユーザーにバインドされた"
            f"ネイティブ実行能力がないため{operation}を拒否しました。"
            "共有サーバーIDによる外部パス操作は許可されません。"
        ),
    }


def _enterprise_native_mutation_error(
    operation: str,
    *,
    allow_native_employee: bool,
) -> Optional[Dict[str, Any]]:
    """Gate generic mutation operations in the native employee lane.

    ``create_file`` and ``edit_file`` are the deliberately narrow native
    filesystem opt-ins.  Delete/append/insert/undo remain fail-closed because
    they either destroy state or bypass the editor's journal/rollback contract.
    Coding/harness scopes are unaffected and continue through AgentRunScope.
    """

    if (
        not _enterprise_mode()
        or _enterprise_explicit_scope_active()
        or _native_employee_capability() is None
        or allow_native_employee
    ):
        return None
    return {
        "success": False,
        "error": (
            "Enterpriseのネイティブ従業員レーンでは"
            f"{operation}を許可していません。"
            "削除・追記・挿入・取り消しは管理された編集APIを使用してください。"
        ),
    }


def _aoi_workspace_identity() -> tuple[Path, Path] | None:
    """Return lexical/resolved AoiTalk workspace roots, or ``None`` on error."""

    try:
        workspace = Path(_get_user_files_root())
        lexical, resolved, _components, _alias = _canonical_path_identity(str(workspace))
        return lexical, resolved
    except Exception:
        logger.warning("AoiTalk workspace identity could not be canonicalized", exc_info=True)
        return None


def _path_is_aoi_workspace(path: str) -> bool:
    """Whether *path* lexically or canonically belongs to AoiTalk workspace."""

    roots = _aoi_workspace_identity()
    if roots is None:
        # An inability to establish the workspace boundary is not an excuse to
        # let a capability-backed file operation bypass the managed-data gate.
        return True
    workspace_lexical, workspace_resolved = roots
    try:
        lexical, resolved, _components, _alias = _canonical_path_identity(
            str(path), base=workspace_lexical
        )
    except Exception:
        return True
    return _path_is_within(lexical, workspace_lexical) or _path_is_within(
        resolved, workspace_resolved
    )


def _native_employee_external_path_allowed(path: str, operation: str) -> bool:
    """Authorize one external path for the native employee lane.

    The native capability is the OS-identity proof for paths outside AoiTalk's
    own workspace.  Canonicalization still runs first to reject malformed/device
    aliases and uninspectable link/reparse components.  A path that is inside
    the AoiTalk workspace remains subject to the existing ACL/managed guards.
    """

    if _native_employee_capability() is None:
        return False
    roots = _aoi_workspace_identity()
    if roots is None:
        return False
    workspace_lexical, workspace_resolved = roots
    try:
        lexical, resolved, components, _device_alias = _canonical_path_identity(
            str(path), base=workspace_lexical
        )
    except Exception:
        return False

    lexical_inside = _path_is_within(lexical, workspace_lexical)
    resolved_inside = _path_is_within(resolved, workspace_resolved)
    # A link/reparse escape originating inside the application workspace is
    # never an ordinary native path.  Keep the old canonical workspace guard
    # even when an endpoint capability is present.
    if lexical_inside and not resolved_inside:
        return False
    for _component, linked in components:
        if linked is None:
            return False
    if lexical_inside or resolved_inside:
        # Managed/personal roots are still checked by the normal AoiTalk ACL
        # and generic-writer protection below.
        return False
    return True


def _native_employee_mutation_resolution_error(
    path: str,
    operation: str,
    *,
    allow_native_employee: bool,
) -> Optional[Dict[str, Any]]:
    """Apply the explicit native opt-in while resolving mutation targets."""

    if (
        not allow_native_employee
        or not _enterprise_mode()
        or _enterprise_explicit_scope_active()
        or _path_is_aoi_workspace(path)
    ):
        return None
    allowed_error = _check_absolute_path_allowed(path)
    if allowed_error:
        return {"success": False, "error": allowed_error}
    if not _native_employee_external_path_allowed(path, operation):
        return _native_employee_capability_error(operation)
    return None


def _enterprise_unscoped_path_error(
    path: str,
    operation: str,
    *,
    allow_native_employee: bool = False,
) -> Optional[Dict[str, Any]]:
    """Reject unscoped Enterprise access outside AoiTalk's own workspace.

    The Enterprise process normally uses a shared service identity.  A
    server-issued native employee capability is the explicit alternate lane
    for ordinary external paths; without it (or without the caller's explicit
    opt-in), admin flags and model-provided absolute paths must not turn that
    identity into an employee-wide browser.
    Explicit Harness/Agent scopes are handled by their own canonical
    capability checks and intentionally bypass this compatibility gate.
    """

    if not _enterprise_mode():
        return None

    try:
        if (
            get_current_harness_execution_scope() is not None
            or get_current_run_scope() is not None
        ):
            return None
    except Exception:
        # A scope lookup failure cannot establish native identity authority.
        pass

    denial = _native_employee_capability_error(operation)
    try:
        workspace = Path(_get_user_files_root())
        workspace_lexical, workspace_resolved, _, _ = _canonical_path_identity(
            str(workspace)
        )
        lexical, resolved, components, _device_alias = _canonical_path_identity(
            str(path),
            base=workspace_lexical,
        )

        lexical_inside = _path_is_within(lexical, workspace_lexical)
        resolved_inside = _path_is_within(resolved, workspace_resolved)

        # An explicitly-issued native capability authorizes an external path,
        # but not an alias that starts inside AoiTalk and escapes its root.  A
        # canonical target inside AoiTalk stays on the existing ACL lane.
        if not lexical_inside and not resolved_inside:
            if allow_native_employee and _native_employee_external_path_allowed(
                path,
                operation,
            ):
                return None
            return denial
        if lexical_inside and not resolved_inside:
            return denial

        # A link/reparse component is allowed only when it remains wholly
        # within the workspace.  This preserves harmless user-local aliases
        # while preventing a transient escape followed by ``..`` from being
        # hidden by the final realpath.
        for component, linked in components:
            if linked is None:
                return denial
            if not linked:
                continue
            component_resolved = Path(
                os.path.realpath(os.fspath(component))
            )
            if not _path_is_within(component, workspace_lexical):
                return denial
            if not _path_is_within(component_resolved, workspace_resolved):
                return denial
    except Exception:
        # Canonical identity is an authorization prerequisite, not a best
        # effort hint.  Keep the boundary fail-closed on metadata failures.
        return denial
    return None


def _enterprise_project_write_error(path: str, operation: str) -> Optional[Dict[str, Any]]:
    """Reject generic file-editor writes into managed storage in Enterprise.

    Project/Docs API writers are the only paths that hold the required row
    locks, ACL context, and quota/journal accounting.  The generic LLM file
    editor has no transaction context, so allowing it to write managed
    namespaces would bypass those controls.
    """
    if not _enterprise_mode():
        return None
    try:
        root = Path(_get_user_files_root()).resolve(strict=False)
        candidate, resolved_candidate, components, _device_alias = (
            _canonical_path_identity(str(path or ""), base=root)
        )

        # A lexical path can escape through a symlink/reparse point in a
        # personal namespace (for example _users/.../link -> _projects/...).
        # Inspect every existing component before any editor or shutil call.
        for _component, linked in components:
            if linked is None or linked:
                return {
                    "success": False,
                    "error": (
                        "Enterpriseではシンボリックリンク/再解析ポイントを経由する"
                        f"{operation}を無効化しています。"
                        " (symlink/junction/reparse point)"
                    ),
                }

        managed_roots = {
            namespace: root / namespace
            for namespace in _ENTERPRISE_MANAGED_WORKSPACE_NAMESPACES
        }
        matched_namespace: Optional[str] = None
        for namespace, managed_root in managed_roots.items():
            managed_root_resolved = Path(
                os.path.realpath(os.fspath(managed_root))
            )
            if _path_is_within(candidate, managed_root_resolved) or _path_is_within(
                resolved_candidate, managed_root_resolved
            ):
                matched_namespace = namespace
                break
            if matched_namespace is not None:
                break
        if matched_namespace is None:
            return None
    except Exception:
        return {
            "success": False,
            "error": "Enterpriseでは管理対象保存領域のパスを安全に確認できません。",
        }
    namespace_labels = {
        "_projects": "プロジェクト",
        "_docs": "Docs",
        "_apps": "App",
        "_app_artifacts": "App artifact",
        "_app_instances": "App instance",
    }
    namespace_label = namespace_labels.get(matched_namespace or "", "管理対象")
    return {
        "success": False,
        "error": (
            f"Enterpriseでは{namespace_label}保存領域への汎用agent書き込みを"
            f"無効化しています。{namespace_label}のファイルAPIを使用してください。"
        ),
    }


def _enterprise_command_error() -> Optional[Dict[str, Any]]:
    """Compatibility shim; Enterprise commands are scoped per invocation."""
    return None


def _enterprise_unscoped_command_error(
) -> Optional[Dict[str, Any]]:
    """Reject host-process commands without an explicit coding scope.

    The Enterprise server process may run under one shared service identity.
    A model-facing command cannot treat that identity (or a native employee
    capability that describes it) as the authenticated employee, because a
    same-token shell has no AoiTalk Project/Docs mediation.  Native command
    execution is therefore a v1 non-goal: only an explicitly issued and
    validated HarnessExecutionScope/AgentRunScope may select the bounded WSL2
    backend.  Capability-backed employee access remains available through the
    mediated filesystem tools below.
    """

    if not _enterprise_mode():
        return None
    try:
        explicit_scope = (
            get_current_harness_execution_scope() is not None
            or get_current_run_scope() is not None
        )
    except Exception:
        explicit_scope = False
    if explicit_scope:
        return None
    return {
        "success": False,
        "error": (
            "Enterpriseの通常Agentコマンドは、ユーザーにバインドされた"
            "ネイティブ実行ID/実行能力がなく、AoiTalkの管理境界を"
            "保証できないため拒否しました。共有サーバーID/同一サービスIDの"
            "ネイティブshellは許可されません。"
            "明示的なAgentRunScope/HarnessExecutionScopeを使用してください。"
        ),
    }


def _enterprise_sensitive_path_error(path: str) -> Optional[Dict[str, Any]]:
    if not _enterprise_sensitive_filter_active():
        return None
    for part in Path(str(path or "")).parts:
        name = part.casefold()
        if (
            name in {
                "credentials", "credential", "secrets", "secret",
                ".ssh", "id_rsa", "id_ed25519",
            }
            or name == ".env"
            or name.startswith(".env.")
            or name.endswith(
                (".key", ".pem", ".p12", ".pfx", ".secret", ".secrets")
            )
        ):
            return {
                "success": False,
                "error": (
                    "Enterprise harnessではcredential/secret pathを直接操作できません。"
                    "明示的なsecret capability/brokerを使用してください。"
                ),
            }
    return None


def _resolve_path_for_user(path: str) -> str:
    """
    Resolve a file path based on user context.
    
    For relative paths:
    - If user context is set: Resolve to user's personal workspace
      (user_files/_users/user_{uuid}/)
    - If no user context: Resolve to user_files root (fallback)
    
    Absolute paths are returned as-is.
    
    Args:
        path: File path to resolve (relative or absolute)
        
    Returns:
        Resolved absolute path string
    """
    # If already absolute, return as-is
    if os.path.isabs(path):
        return path
    
    context = get_current_user_context()
    user_id = context.get("user_id")
    
    user_files_root = _get_user_files_root()
    
    if user_id:
        # User is logged in: resolve to their personal workspace
        # e.g., "日記フォルダ/2026-01-18.md" -> "user_files/_users/user_{id}/日記フォルダ/2026-01-18.md"
        user_workspace = user_files_root / "_users" / f"user_{user_id}"
        user_workspace.mkdir(parents=True, exist_ok=True)
        resolved = user_workspace / path
        logger.debug(f"Resolved path '{path}' to user workspace: {resolved}")
        return str(resolved)
    else:
        # No user context: resolve to user_files root (fallback for system/anonymous)
        resolved = user_files_root / path
        logger.debug(f"Resolved path '{path}' to user_files root: {resolved}")
        return str(resolved)


_EXPLICIT_WORKSPACE_NAMESPACES = frozenset(
    {
        "_projects",
        "_users",
        "_apps",
        # Canonical application release artifacts and managed Docs storage
        # also live directly below the workspace root. Their existing ACL and
        # protected-path checks remain authoritative after resolution.
        "_app_artifacts",
        "_docs",
    }
)


def _is_explicit_workspace_namespace(path: str) -> bool:
    """Whether a relative path explicitly names a canonical root namespace."""

    raw = str(path or "").replace("\\", "/").strip("/")
    first = raw.split("/", 1)[0].casefold() if raw else ""
    return first in _EXPLICIT_WORKSPACE_NAMESPACES


def _resolve_mutation_target(
    path: str,
    operation: str = "write",
    *,
    allow_native_employee: bool = False,
) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Resolve generic mutations without shadowing explicit workspace paths.

    Absolute paths preserve the legacy external-path contract. Explicit
    ``_projects``/``_users``/``_apps`` namespaces use the canonical workspace
    resolver (including traversal/symlink checks). Ordinary relative paths
    retain the historical current-user personal-workspace base.

    ``allow_native_employee`` is an explicit opt-in for the narrow
    create/edit tools.  It is intentionally false for delete/append/insert/
    undo so a native capability cannot widen those mutation lanes.
    """

    raw = str(path or "")

    # A bound AgentRunScope is the authoritative resolver for worker runs.
    # Do this before legacy user-workspace resolution so a relative path cannot
    # silently land in a different user's workspace, and so absolute paths are
    # checked canonically (including symlink/junction components).
    scope = get_current_run_scope()
    if scope is not None:
        try:
            if operation.lower() in {"delete", "remove", "rmdir"}:
                resolved = scope.assert_delete_allowed(raw)
            else:
                resolved = scope.assert_mutation_allowed(raw, operation)
            sensitive = _enterprise_sensitive_path_error(str(resolved))
            return (None, sensitive) if sensitive else (str(resolved), None)
        except RunScopeViolation as exc:
            return None, {"success": False, "error": str(exc)}

    if raw and os.path.isabs(raw):
        sensitive = _enterprise_sensitive_path_error(raw)
        if sensitive:
            return None, sensitive
        native_error = _native_employee_mutation_resolution_error(
            raw,
            operation,
            allow_native_employee=allow_native_employee,
        )
        return (None, native_error) if native_error else (raw, None)
    if _is_explicit_workspace_namespace(raw):
        target, valid = _workspace_service().resolve_workspace_path(raw)
        if not valid:
            return None, {
                "success": False,
                "error": f"ワークスペース外のパスは指定できません: {raw}",
            }
        sensitive = _enterprise_sensitive_path_error(str(target))
        return (None, sensitive) if sensitive else (str(target), None)
    resolved = _resolve_path_for_user(raw)
    sensitive = _enterprise_sensitive_path_error(resolved)
    if sensitive:
        return None, sensitive
    native_error = _native_employee_mutation_resolution_error(
        resolved,
        operation,
        allow_native_employee=allow_native_employee,
    )
    return (None, native_error) if native_error else (resolved, None)


def _workspace_service():
    """file_explorer サービスを遅延importする（循環import回避）。"""
    from ..file_explorer import file_explorer_service

    return file_explorer_service


def _allowed_absolute_paths() -> List[str]:
    """AOITALK_ALLOWED_PATHS による絶対パス制限（未設定なら制限なし）。"""
    return allowed_absolute_paths()


def _check_absolute_path_allowed(path: str) -> Optional[str]:
    """許可パス判定は file_editor の1系統へ委譲する。

    行範囲指定の有無で `read_file` の判定が食い違わないよう、FileEditor と
    同じ関数・同じタイミングで環境変数を読む。
    """
    return path_outside_allowed_error(path)


def _resolve_read_target(
    path: str,
    *,
    allow_native_employee: bool = False,
) -> tuple[Optional[str], bool, Optional[str]]:
    """read_file / list_directory / search_files 共通のパス解決。

    絶対パスは統合前の各ツールと同じく
    そのまま扱い、AOITALK_ALLOWED_PATHS の制限だけを適用する。
    相対パスは workspace ルート基準を優先し、そこに実体が無ければ
    ログイン中ユーザー専用workspace基準へ落とす。どちらもファイラーと
    同じ境界チェックの内側に収まる。

    最終的な解決先には、絶対パス指定か相対パス指定かに関わらず
    AOITALK_ALLOWED_PATHS の判定を適用する。FileEditor 経由（行範囲指定）でも
    同じ判定が走るため、行範囲の有無で結果が変わらない。

    ``allow_native_employee`` is an explicit opt-in used only by the
    ordinary read tools and RepoMap.  It is not a capability grant by itself;
    the currently bound native employee handle must still validate.

    Returns:
        (解決済み絶対パス, 絶対パス指定だったか, エラーメッセージ)
    """
    raw = str(path or "")

    # Reads in a worker run use the same canonical run root as mutations.  This
    # also prevents the legacy workspace resolver from following an external
    # symlink before the central reparse-point check gets a chance to run.
    scope = get_current_run_scope()
    if scope is not None:
        try:
            resolved = str(scope.assert_read_allowed(raw))
            sensitive = _enterprise_sensitive_path_error(resolved)
            if sensitive:
                return (
                    None,
                    bool(raw and os.path.isabs(raw)),
                    str(sensitive["error"]),
                )
            return resolved, bool(raw and os.path.isabs(raw)), None
        except RunScopeViolation as exc:
            return None, bool(raw and os.path.isabs(raw)), str(exc)

    if raw and os.path.isabs(raw):
        denied = _check_absolute_path_allowed(raw)
        if denied:
            return None, True, denied
        permission_error = _check_user_permission(
            raw,
            "読み取り",
            allow_native_employee=allow_native_employee,
        )
        if permission_error:
            return None, True, str(permission_error["error"])
        sensitive = _enterprise_sensitive_path_error(raw)
        if sensitive:
            return None, True, str(sensitive["error"])
        return raw, True, None

    service = _workspace_service()
    workspace_target, valid = service.resolve_workspace_path(raw)
    if not valid:
        return None, False, f"ワークスペース外のパスは指定できません: {raw}"

    context = get_current_user_context()
    user_target = Path(_resolve_path_for_user(raw))
    explicit_namespace = _is_explicit_workspace_namespace(raw)
    if (
        not context["is_admin"]
        and not explicit_namespace
        and user_target.exists()
    ):
        # 非管理者の相対パスは本人領域を優先する。これにより空pathの
        # list/searchもworkspace全体ではなく本人領域だけを対象にする。
        resolved = str(user_target)
    elif explicit_namespace or workspace_target.exists():
        resolved = str(workspace_target)
    else:
        resolved = str(user_target if user_target.exists() else workspace_target)

    denied = _check_absolute_path_allowed(resolved)
    if denied:
        return None, False, denied
    permission_error = _check_user_permission(
        resolved,
        "読み取り",
        allow_native_employee=allow_native_employee,
    )
    if permission_error:
        return None, False, str(permission_error["error"])
    sensitive = _enterprise_sensitive_path_error(resolved)
    if sensitive:
        return None, False, str(sensitive["error"])
    return resolved, False, None


def _resolve_working_directory(working_directory: Optional[str]) -> Optional[str]:
    """execute_command の作業ディレクトリを read_file / search_files と同じ基準で解決する。

    read_file・search_files・list_directory の相対パスは workspace ルート基準
    （例: ``_apps/app_xxx``）だが、execute_command はプロセスCWD基準
    （例: ``workspaces/_apps/app_xxx``）だった。同じ相対パスがツールごとに別物を
    指すため、モデルが基準の食い違いを埋めるだけでツールループを浪費していた。

    既存の CWD 基準指定を壊さないよう、実体があるものを優先して採用する。
    """
    raw = str(working_directory or "").strip()
    scope = get_current_run_scope()
    if scope is not None:
        try:
            return str(scope.assert_command_cwd_allowed(working_directory))
        except RunScopeViolation:
            # Keep the original value so CommandExecutor can return the
            # structured run-scope denial instead of silently selecting a
            # process-global cwd.
            return working_directory
    if not raw or os.path.isabs(raw):
        return working_directory

    cwd_candidate = Path(raw)
    if cwd_candidate.is_dir():
        return working_directory

    resolved, _is_absolute, error = _resolve_read_target(raw)
    if error or not resolved:
        return working_directory
    if Path(resolved).is_dir():
        return resolved
    return working_directory


def _normalize_extensions(extensions: Any) -> Optional[List[str]]:
    """拡張子フィルタを list へ正規化する。

    モデルは ``".py,.md"`` のような文字列で渡してくることがあり、そのまま
    渡すと突合が全て外れて「該当なし」になる。区切り文字で分解して吸収する。
    """
    if extensions is None:
        return None
    if isinstance(extensions, str):
        parts = [p.strip() for p in re.split(r"[,\s;]+", extensions) if p.strip()]
    elif isinstance(extensions, (list, tuple, set)):
        parts = [str(p).strip() for p in extensions if str(p).strip()]
    else:
        return None

    normalized: List[str] = []
    for part in parts:
        cleaned = part.strip("'\"[]")
        if not cleaned:
            continue
        if not cleaned.startswith("."):
            cleaned = f".{cleaned}"
        normalized.append(cleaned)
    return normalized or None


def _is_path_in_user_workspace(path: str, user_id: Optional[str], project_ids: List[str]) -> bool:
    """
    Check if a path is within the user's allowed workspace directories.
    
    Args:
        path: Path to check
        user_id: User UUID as string
        project_ids: List of project UUIDs the user participates in
        
    Returns:
        True if path is in user's personal directory or a participating project directory
    """
    if not user_id:
        return False
    
    try:
        target_path = Path(path).resolve()
        user_files_root = _get_user_files_root()
        
        # Check if path is under user_files at all
        try:
            target_path.relative_to(user_files_root)
        except ValueError:
            return False
        
        # Check if path is in user's personal directory
        user_dir = user_files_root / "_users" / f"user_{user_id}"
        try:
            target_path.relative_to(user_dir)
            return True
        except ValueError:
            pass
        
        # Check if path is in any participating project directory
        for project_id in project_ids:
            project_dir = user_files_root / "_projects" / f"project_{project_id}"
            try:
                target_path.relative_to(project_dir)
                return True
            except ValueError:
                continue
        
        return False
        
    except Exception as e:
        logger.warning(f"Error checking user workspace path: {e}")
        return False


def _check_user_permission(
    path: str,
    operation: str,
    *,
    allow_native_employee: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Check if current user has permission to perform operation on path.
    
    For admin users: Uses protected_paths check (current behavior)
    For non-admin users: Only allows access to own directory and participating projects
    
    Args:
        path: Path to check
        operation: Operation name for error message
        
    Returns:
        Error dict if not permitted, None if allowed
    """
    context = get_current_user_context()

    # A bound run scope is the worker's explicit repository authorization.  Do
    # not force it through the interactive user-files ACL (which may have no
    # user identity at all); the central scope has already canonicalised and
    # constrained this path for the run.
    if get_current_run_scope() is not None:
        return None

    # Enterprise's host process currently runs under a shared service
    # identity.  Before the administrator bypass, require the lexical and
    # canonical path to remain inside AoiTalk's workspace unless an explicit
    # Harness/Agent capability is already bound.  This keeps admin flags from
    # turning an absolute external path into a cross-employee data oracle.
    identity_error = _enterprise_unscoped_path_error(
        path,
        operation,
        allow_native_employee=allow_native_employee,
    )
    if identity_error:
        return identity_error
    
    # Admin users: use existing protected_paths logic
    if context["is_admin"]:
        return None  # Admins bypass this check, will use _check_path_protection
    
    # Non-admin users: must be in their workspace
    user_id = context["user_id"]
    if operation == "読み取り":
        project_ids = context["project_ids"]
    elif operation == "削除":
        project_ids = context["deletable_project_ids"]
    else:
        project_ids = context["writable_project_ids"]
    
    if not user_id:
        # Personal-mode legacy callers can still inspect the shared workspace
        # when no authenticated request context is installed.  Enterprise
        # keeps the fail-closed behavior: every generic agent read must carry
        # an explicit durable user/project context.
        if operation == "読み取り" and not _enterprise_mode():
            return None
        return {
            "success": False,
            "error": f"操作拒否: ユーザーコンテキストが設定されていません。"
        }
    
    if not _is_path_in_user_workspace(path, user_id, project_ids):
        # Outside AoiTalk's workspace, a valid native employee capability
        # delegates authorization to the actual Windows token/ACL.  Never use
        # this bypass for paths that resolve into AoiTalk-managed data: those
        # remain on the user/project ACL above and generic writers are blocked
        # separately by _enterprise_project_write_error.
        if (
            allow_native_employee
            and _enterprise_mode()
            and _native_employee_external_path_allowed(
                path,
                operation,
            )
        ):
            return None
        return {
            "success": False,
            "error": f"操作拒否: パス '{path}' へのアクセス権限がありません。\n"
                     f"自分のディレクトリまたは参加しているプロジェクトのディレクトリのみ{operation}できます。"
        }
    
    return None


def _is_path_protected(path: str) -> tuple[bool, Optional[str]]:
    """
    Check if a path is protected from modification.
    
    Args:
        path: Path to check
        
    Returns:
        Tuple of (is_protected, matched_protected_path)
    """
    protected_paths = _get_protected_paths()
    if not protected_paths:
        return False, None
    
    try:
        # Normalize the path
        target_path = Path(path).resolve()
        
        for protected in protected_paths:
            protected_path = Path(protected).resolve()
            # Check if target is equal to or under the protected path
            try:
                target_path.relative_to(protected_path)
                return True, str(protected_path)
            except ValueError:
                # Not under this protected path
                continue
    except Exception as e:
        logger.warning(f"Error checking path protection for {path}: {e}")
    
    return False, None


def _check_path_protection(path: str, operation: str) -> Optional[Dict[str, Any]]:
    """
    Check if path is protected and return error dict if so.
    
    For admins: Blocks access to protected paths except user_files workspace.
    For non-admins: Uses _check_user_permission instead (called separately).
    
    Args:
        path: Path to check
        operation: Operation name for error message
        
    Returns:
        Error dict if protected, None if allowed
    """
    # First check if path is within user_files (always allowed for all users)
    try:
        target_path = Path(path).resolve()
        user_files_root = _get_user_files_root()
        try:
            target_path.relative_to(user_files_root)
            # Path is within user_files - skip protected paths check
            # Individual user permission check is done separately
            return None
        except ValueError:
            pass  # Not in user_files, continue with protection check
    except Exception:
        pass
    
    is_protected, matched_path = _is_path_protected(path)
    if is_protected:
        return {
            "success": False,
            "error": f"操作拒否: パス '{path}' は保護されています。\n"
                     f"理由: '{matched_path}' 以下のファイルは {operation} できません。\n"
                     f"（config.yaml の os_operations.protected_paths で設定されています）"
        }
    return None




# Destructive command patterns that should be blocked on protected paths
_DESTRUCTIVE_COMMANDS = [
    'del', 'erase', 'rm', 'remove',  # File deletion
    'rmdir', 'rd',  # Directory deletion
    'move', 'mv', 'ren', 'rename',  # Move/rename
    'copy', 'cp',  # These can overwrite files
]

# PowerShell destructive cmdlets (case-insensitive)
_DESTRUCTIVE_POWERSHELL_CMDLETS = [
    'remove-item', 'ri', 'rm', 'rmdir', 'del', 'erase', 'rd',  # Deletion
    'move-item', 'mi', 'mv', 'move',  # Move
    'rename-item', 'rni', 'ren',  # Rename
    'copy-item', 'ci', 'cp', 'copy',  # Copy (can overwrite)
    'set-content', 'sc',  # Overwrite file content
    'out-file',  # Write to file
    'add-content', 'ac',  # Append to file
    'clear-content', 'clc',  # Clear file content
]


def _extract_paths_from_command(command: str) -> List[str]:
    """Extract potential file paths from a command string."""
    import re
    paths = []
    
    # Match paths in various formats:
    # - Quoted paths: "D:\path\to\file" or 'D:\path\to\file'
    # - Unquoted absolute paths: D:\path\to\file or /path/to/file
    
    # Quoted paths
    quoted_pattern = r'["\']([A-Za-z]:\\[^"\']+|/[^"\']+)["\']'
    paths.extend(re.findall(quoted_pattern, command))
    
    # Unquoted Windows absolute paths (D:\something)
    win_path_pattern = r'(?<!["\'])([A-Za-z]:\\[^\s"\'<>|]+)'
    paths.extend(re.findall(win_path_pattern, command))
    
    return paths


def _check_command_protection(command: str, working_directory: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Check if a command targets protected paths with destructive operations.
    
    Args:
        command: Command string to check
        working_directory: Working directory for the command
        
    Returns:
        Error dict if protected path is targeted, None if allowed
    """
    protected_paths = _get_protected_paths()
    if not protected_paths:
        return None
    
    command_lower = command.lower()
    
    # Parse the command to extract the base command
    parts = command.strip().split()
    if not parts:
        return None
    
    base_cmd = parts[0].lower().replace('.exe', '')
    
    # Check for PowerShell commands
    is_powershell = base_cmd in ['powershell', 'pwsh']
    
    if is_powershell:
        # Check for destructive PowerShell cmdlets in the command
        detected_cmdlet = None
        for cmdlet in _DESTRUCTIVE_POWERSHELL_CMDLETS:
            # Check for cmdlet (case-insensitive, word boundary)
            if cmdlet in command_lower:
                # Verify it's a word boundary match
                import re
                if re.search(r'\b' + re.escape(cmdlet) + r'\b', command_lower):
                    detected_cmdlet = cmdlet
                    break
        
        if detected_cmdlet:
            # Extract paths from the PowerShell command
            paths = _extract_paths_from_command(command)
            for check_path in paths:
                is_protected, matched_path = _is_path_protected(check_path)
                if is_protected:
                    return {
                        "success": False,
                        "error": f"コマンド拒否: 保護されたパス '{check_path}' への破壊的操作は禁止されています。\n"
                                 f"理由: '{matched_path}' 以下は保護されており、PowerShellの '{detected_cmdlet}' を実行できません。\n"
                                 f"（config.yaml の os_operations.protected_paths で設定されています）"
                    }
        return None
    
    # Check if it's a destructive command (non-PowerShell)
    is_destructive = any(base_cmd == dc for dc in _DESTRUCTIVE_COMMANDS)
    if not is_destructive:
        return None
    
    # Extract potential paths from the command arguments
    for arg in parts[1:]:
        # Skip flags
        if arg.startswith('-') or arg.startswith('/'):
            continue
        
        # Remove quotes
        arg = arg.strip('"').strip("'")
        
        # Try to resolve the path
        try:
            if os.path.isabs(arg):
                check_path = arg
            elif working_directory:
                check_path = os.path.join(working_directory, arg)
            else:
                check_path = arg
            
            # Check if this path is protected
            is_protected, matched_path = _is_path_protected(check_path)
            if is_protected:
                return {
                    "success": False,
                    "error": f"コマンド拒否: 保護されたパス '{check_path}' への破壊的操作は禁止されています。\n"
                             f"理由: '{matched_path}' 以下は保護されており、'{base_cmd}' コマンドを実行できません。\n"
                             f"（config.yaml の os_operations.protected_paths で設定されています）"
                }
        except Exception:
            continue
    
    return None


@tool
@_enterprise_scoped(require_backend=True)
def execute_command(
    command: str,
    working_directory: Optional[str] = None,
    timeout: Optional[int] = None,
    shell: Optional[str] = None,
    run_in_background: bool = False
) -> Dict[str, Any]:
    """シェルコマンドを実行する

    出力は長すぎる場合に中央を省略してトリムされる（先頭と末尾は必ず残る）ため、
    巨大な出力を吐くコマンドでも安全に実行できる。

    バックグラウンド実行（run_in_background=True）を使うべき場面:
        - サーバやウォッチャの起動（npm run dev、uvicorn、python -m http.server など）
        - 数分以上かかるビルド・インストール・学習処理
        - 対話的な入力が必要なコマンド（write_command_input で stdin へ送れる）
      バックグラウンド実行は即座に job_id を返すので、その後
      read_command_output で進捗を読み、必要なら stop_command で停止する。
      逆に、すぐ終わるコマンド（ls、git status、python --version など）は
      バックグラウンドにせず前景で実行すること。

    shell の選び方:
        - "auto"（既定）: Windows なら PowerShell、Unix 系ならログインシェル
        - "powershell": Windows で Get-ChildItem 等の cmdlet やパイプライン処理を使う場合
        - "cmd": Windows のバッチ構文（%VAR%、call、古い .bat）が必要な場合
        - "bash": WSL / Linux / macOS で POSIX シェル構文を使う場合

    Args:
        command: 実行するコマンド（例：「dir」「ls -la」「python script.py」）
        working_directory: コマンドを実行するディレクトリ。read_file / search_files と
            同じくworkspace相対パス（例: `_apps/app_xxx`）を指定できる
        timeout: タイムアウト秒数（省略時は設定値。前景実行のみ有効）
        shell: 使用するシェル（auto / cmd / powershell / bash）
        run_in_background: Trueで別プロセスとして起動し、即座に job_id を返す

    Returns:
        Dict[str, Any]: 前景実行は実行結果（success, output, stderr, return_code,
            duration_seconds, truncated, timed_out）、
            バックグラウンド実行は success と job_id

    Examples:
        >>> execute_command("dir")
        >>> execute_command("ls -la", "/home/user")
        >>> execute_command("pytest -q", timeout=600)
        >>> execute_command("npm run dev", run_in_background=True)
        >>> execute_command("Get-Process | Select-Object -First 5", shell="powershell")
    """
    print(f"[Tool] execute_command が呼び出されました: {command[:50]}...")

    working_directory = _resolve_working_directory(working_directory)
    command_config = _get_command_config()

    # Check for destructive commands on protected paths
    protection_error = _check_command_protection(command, working_directory)
    if protection_error:
        return protection_error

    permission_error = _confirm_tool_action(
        "execute_command",
        {
            "command": command,
            "working_directory": working_directory,
            "run_in_background": run_in_background,
        },
        "ユーザーによってコマンド実行がキャンセルされました。",
    )
    if permission_error:
        return permission_error

    # Keep the existing confirmation/permission contract first.  Ordinary
    # Enterprise commands are always fail-closed: a native employee capability
    # is intentionally not a shell authority because a same-token process
    # cannot guarantee AoiTalk Project/Docs mediation.  Explicit
    # AgentRunScope/HarnessExecutionScope calls continue through the bounded
    # WSL2/bubblewrap backend.  This also keeps background lifecycle APIs
    # denied before touching the process-global registry.
    enterprise_identity_error = _enterprise_unscoped_command_error()
    if enterprise_identity_error:
        return enterprise_identity_error

    if shell is None:
        shell = command_config.get("shell", "auto")

    # --- バックグラウンド実行 ---
    if run_in_background:
        if not command_config.get("background_enabled", True):
            return {
                "success": False,
                "error": "バックグラウンド実行は設定で無効化されています"
                         "（os_operations.command.background_enabled）。",
            }
        try:
            registry = get_background_job_registry()
            job_id = registry.start(command, cwd=working_directory, shell=shell)
        except BackgroundJobError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"Error starting background command: {e}", exc_info=True)
            return {"success": False, "error": f"予期しないエラー: {str(e)}"}

        return {
            "success": True,
            "job_id": job_id,
            "message": f"バックグラウンドで起動しました (job_id={job_id})。"
                       f"read_command_output で出力を確認し、stop_command で停止できます。",
        }

    # --- 前景実行 ---
    if timeout is None:
        timeout = command_config.get("timeout_seconds", 120)
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = int(_COMMAND_CONFIG_DEFAULTS["timeout_seconds"])

    max_output_bytes = int(command_config.get("max_output_bytes", 32768))
    # The model may request arbitrarily large foreground limits.  In an
    # Enterprise run the immutable upper scope is authoritative and the
    # request can only narrow (never widen) those limits.
    limits = _active_harness_resource_limits()
    if limits is not None:
        try:
            timeout = min(float(timeout), float(limits.timeout_seconds))
            if timeout <= 0:
                raise ValueError("timeout_seconds must be positive")
            max_output_bytes = max(
                1,
                min(int(max_output_bytes), int(limits.max_output_bytes)),
            )
        except (TypeError, ValueError, AttributeError):
            return _enterprise_scope_error(
                RuntimeError("invalid Enterprise resource limits"),
                operation="command",
            )

    executor = get_command_executor()
    result = executor.execute(
        command,
        cwd=working_directory,
        timeout=timeout,
        shell=shell,
    )

    formatted = format_command_output(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.return_code,
        duration_seconds=result.duration_seconds,
        timed_out=result.timed_out,
        timeout_seconds=timeout,
        max_output_bytes=max_output_bytes,
    )

    response: Dict[str, Any] = {
        "success": result.success,
        "output": formatted["output"],
        "stdout": formatted["stdout"],
        "stderr": formatted["stderr"] if formatted["stderr"] else None,
        "return_code": result.return_code,
        "duration_seconds": round(result.duration_seconds, 3),
        "truncated": formatted["truncated"],
        "timed_out": result.timed_out,
    }

    if not result.success:
        response["error"] = (
            result.error_message
            or formatted["stderr"]
            or f"コマンドが終了コード {result.return_code} で失敗しました"
        )

    return response


@tool
@_enterprise_scoped()
def read_command_output(
    job_id: str,
    max_output_bytes: int = 8192
) -> Dict[str, Any]:
    """バックグラウンドジョブの現在までの出力と状態を取得する

    execute_command(run_in_background=True) で起動したジョブの進捗確認に使う。
    出力は max_output_bytes を超える場合、中央を省略してトリムされる。

    Args:
        job_id: execute_command が返した job_id
        max_output_bytes: 取得する出力の最大バイト数（既定: 8192）

    Returns:
        Dict[str, Any]: status（running/exited/killed）、exit_code、stdout、stderr など

    Examples:
        >>> read_command_output("a1b2c3d4")
        >>> read_command_output("a1b2c3d4", max_output_bytes=32768)
    """
    print(f"[Tool] read_command_output が呼び出されました: {job_id}")

    # An unscoped Enterprise turn must not use the process-global registry:
    # ownerless jobs could expose another employee's command output.  Scoped
    # coding/harness turns retain the registry's existing owner filter.
    enterprise_identity_error = _enterprise_unscoped_command_error()
    if enterprise_identity_error:
        return enterprise_identity_error

    try:
        max_output_bytes = int(max_output_bytes)
    except (TypeError, ValueError):
        max_output_bytes = 8192
    limits = _active_harness_resource_limits()
    if limits is not None:
        try:
            max_output_bytes = max(
                1,
                min(max_output_bytes, int(limits.max_output_bytes)),
            )
        except (TypeError, ValueError, AttributeError):
            return _enterprise_scope_error(
                RuntimeError("invalid Enterprise resource limits"),
                operation="background output",
            )

    try:
        registry = get_background_job_registry()
        info = registry.read(job_id, max_output_bytes=max_output_bytes)
    except BackgroundJobError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Error in read_command_output: {e}", exc_info=True)
        return {"success": False, "error": f"予期しないエラー: {str(e)}"}

    info["success"] = True
    return info


@tool
@_enterprise_scoped()
def write_command_input(
    job_id: str,
    text: str
) -> Dict[str, Any]:
    """バックグラウンドジョブの標準入力へ文字列を書き込む

    対話的なコマンド（確認プロンプト、REPL など）へ入力を送るために使う。
    末尾に改行が無い場合は自動で付与される。

    Args:
        job_id: execute_command が返した job_id
        text: 送信する文字列（改行は自動付与）

    Returns:
        Dict[str, Any]: 書き込み結果

    Examples:
        >>> write_command_input("a1b2c3d4", "yes")
        >>> write_command_input("a1b2c3d4", "print(1 + 1)")
    """
    print(f"[Tool] write_command_input が呼び出されました: {job_id}")

    permission_error = _confirm_tool_action(
        "write_command_input",
        {"job_id": job_id, "text": text},
        "ユーザーによって標準入力の書き込みがキャンセルされました。",
    )
    if permission_error:
        return permission_error

    enterprise_identity_error = _enterprise_unscoped_command_error()
    if enterprise_identity_error:
        return enterprise_identity_error

    try:
        registry = get_background_job_registry()
        result = registry.write_stdin(job_id, text)
    except BackgroundJobError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Error in write_command_input: {e}", exc_info=True)
        return {"success": False, "error": f"予期しないエラー: {str(e)}"}

    result["success"] = True
    return result


@tool
@_enterprise_scoped()
def stop_command(job_id: str) -> Dict[str, Any]:
    """バックグラウンドジョブを停止する

    まず terminate を送り、猶予時間を過ぎても終了しない場合は kill する。

    Args:
        job_id: execute_command が返した job_id

    Returns:
        Dict[str, Any]: 停止結果（status, exit_code）

    Examples:
        >>> stop_command("a1b2c3d4")
    """
    print(f"[Tool] stop_command が呼び出されました: {job_id}")

    permission_error = _confirm_tool_action(
        "stop_command",
        {"job_id": job_id},
        "ユーザーによってジョブ停止がキャンセルされました。",
    )
    if permission_error:
        return permission_error

    # Keep stop's existing confirmation first; only a confirmed action then
    # reaches this Enterprise identity boundary and the registry.
    enterprise_identity_error = _enterprise_unscoped_command_error()
    if enterprise_identity_error:
        return enterprise_identity_error

    try:
        registry = get_background_job_registry()
        result = registry.stop(job_id)
    except BackgroundJobError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Error in stop_command: {e}", exc_info=True)
        return {"success": False, "error": f"予期しないエラー: {str(e)}"}

    result["success"] = True
    return result


@tool
@_enterprise_scoped()
def list_commands() -> Dict[str, Any]:
    """バックグラウンドジョブの一覧を取得する

    終了済みのジョブも履歴として残る。

    Returns:
        Dict[str, Any]: jobs（job_id, command, status, started_at, exit_code のリスト）

    Examples:
        >>> list_commands()
    """
    print("[Tool] list_commands が呼び出されました")

    enterprise_identity_error = _enterprise_unscoped_command_error()
    if enterprise_identity_error:
        return enterprise_identity_error

    try:
        registry = get_background_job_registry()
        jobs = registry.list_jobs()
    except Exception as e:
        logger.error(f"Error in list_commands: {e}", exc_info=True)
        return {"success": False, "error": f"予期しないエラー: {str(e)}"}

    return {
        "success": True,
        "jobs": jobs,
        "running_count": sum(1 for job in jobs if job["status"] == "running"),
    }


def close_scoped_jobs(
    scope: Any | None = None,
    *,
    owner_run_id: Optional[str] = None,
    repository_identity: Optional[str] = None,
    scope_fingerprint: Optional[str] = None,
    remove: bool = False,
) -> List[Dict[str, Any]]:
    """Parent lifecycle helper for terminating one run's background jobs.

    This is intentionally not an LLM-facing ``@tool``.  A parent controller
    owns the immutable ``AgentRunScope`` and calls this helper when its run
    ends; ordinary tool calls rely on the registry's current-scope filter.
    """

    registry = get_background_job_registry()
    return registry.close_scoped_jobs(
        scope,
        owner_run_id=owner_run_id,
        repository_identity=repository_identity,
        scope_fingerprint=scope_fingerprint,
        remove=remove,
    )


def preflight_scoped_jobs(scope: Any | None = None) -> Dict[str, Any]:
    """Return the publication preflight for active jobs owned by one scope."""

    return get_background_job_registry().preflight_scoped_jobs(scope)


def assert_no_active_scoped_jobs(scope: Any | None = None) -> Dict[str, Any]:
    """Raise when a parent is about to publish with an active scoped job."""

    return get_background_job_registry().assert_no_active_scoped_jobs(scope)



@tool
@_enterprise_scoped()
def read_file(
    path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    offset: int = 0,
    max_chars: int = 5000,
) -> Dict[str, Any]:
    """Read a single file, by workspace-relative path or by absolute path.

    A relative path is resolved inside the workspace (workspace root first,
    then the current user's own workspace folder); an absolute path is read
    directly. Text is returned as a character window controlled by `offset`
    and `max_chars`; pass `start_line`/`end_line` instead to get a
    line-numbered slice. Images return metadata only (binary image data is
    supplied through the chat media channel), xlsx/docx/pptx/pdf are
    converted to Markdown text, and eml/msg files are parsed into a
    structured mail transcript (including line-numbered slices).

    Args:
        path: Workspace-relative or absolute file path.
        start_line: First line to return (1-based). Enables line-numbered output.
        end_line: Last line to return (-1 or omitted means end of file).
        offset: Characters to skip before the returned text window (default 0).
        max_chars: Maximum characters of text to return (default 5000).

    Returns:
        Dict[str, Any]: File content and metadata.

    Examples:
        >>> read_file("日記フォルダ/2026-01-18.md")
        >>> read_file("C:/work/config.yaml", start_line=1, end_line=20)
        >>> read_file("報告書.xlsx")
    """
    print(f"[Tool] read_file が呼び出されました: {path}")

    resolved, is_absolute, error = _resolve_read_target(
        path,
        allow_native_employee=True,
    )
    if error or not resolved:
        return {"success": False, "error": error or "パスを解決できませんでした"}

    # Gemini API が float を渡すことがあるので int へ寄せる。
    if start_line is not None:
        start_line = int(start_line)
    if end_line is not None:
        end_line = int(end_line)
        if end_line == -1:
            end_line = None

    if start_line is not None or end_line is not None:
        converted_suffix = Path(resolved).suffix.casefold()
        if converted_suffix in {".eml", ".msg", ".docx", ".xlsx", ".pptx", ".pdf"}:
            try:
                service = _workspace_service()
                result = service.get_full_content(resolved, is_admin=True)
            except Exception as e:
                logger.error(f"Error in read_file: {e}", exc_info=True)
                return {"success": False, "error": f"予期しないエラー: {str(e)}"}
            if not result.get("success"):
                return result
            lines = str(result.get("content") or "").splitlines()
            first = max(0, (start_line or 1) - 1)
            last = len(lines) if end_line is None else max(first, end_line)
            selected = lines[first:last]
            return {
                "success": True,
                "type": "mail" if converted_suffix in {".eml", ".msg"} else "office",
                "path": path,
                "content": "\n".join(selected),
                "start_line": first + 1,
                "end_line": first + len(selected),
            }
        try:
            editor = get_file_editor()
            service = _workspace_service()
            content = editor.view(
                resolved,
                start_line=start_line,
                end_line=end_line,
                max_file_size=service.MAX_READ_FILE_SIZE,
            )
        except FileEditorError as e:
            result = {"success": False, "error": str(e)}
            if getattr(e, "error_code", ""):
                result["error_code"] = e.error_code
            return result
        except Exception as e:
            logger.error(f"Error in read_file: {e}", exc_info=True)
            return {"success": False, "error": f"予期しないエラー: {str(e)}"}
        return {
            "success": True,
            "type": "text",
            "path": path,
            "content": content,
            "start_line": start_line or 1,
            "end_line": end_line,
        }

    try:
        offset = max(0, int(offset or 0))
        max_chars = max(1, min(int(max_chars or 5000), 200000))
        service = _workspace_service()
        # 相対パスは解決時に境界チェック済み、絶対パスは許可パス確認済みなので
        # サービス側の絶対パス解決を使う。
        result = service.get_preview(
            resolved,
            max_chars=max_chars,
            offset=offset,
            is_admin=True,
        )
    except Exception as e:
        logger.error(f"Error in read_file: {e}", exc_info=True)
        return {"success": False, "error": f"予期しないエラー: {str(e)}"}

    if result.get("success") and result.get("type") in {"text", "office", "mail"}:
        result["offset"] = offset
        result["max_chars"] = max_chars
    if result.get("success") and result.get("type") in {
        "image",
        "audio",
        "video",
        "binary",
    }:
        # UI preview may return metadata/base64 for known media, but model-facing
        # file reads must never treat binary bytes as a successful text read.
        result.pop("data_url", None)
        result.pop("content", None)
        result.update(
            {
                "success": False,
                "error_code": "binary_file",
                "error": "バイナリファイルはread_fileでテキストとして読み取れません",
            }
        )
    if is_absolute:
        result["path"] = path
    return result


@tool
@_enterprise_scoped()
def create_file(
    path: str,
    content: str
) -> Dict[str, Any]:
    """新しいファイルを作成する
    
    Args:
        path: 作成するファイルのパス
        content: ファイルの内容
    
    Returns:
        Dict[str, Any]: 作成結果
    
    Examples:
        >>> create_file("hello.txt", "Hello, World!")
        >>> create_file("src/utils.py", "def helper():\\n    pass")
    """
    print(f"[Tool] create_file が呼び出されました: {path}")
    
    path, resolution_error = _resolve_mutation_target(
        path,
        "create",
        allow_native_employee=True,
    )
    if resolution_error or not path:
        return resolution_error or {"success": False, "error": "パスを解決できませんでした"}

    enterprise_error = _enterprise_project_write_error(path, "作成")
    if enterprise_error:
        return enterprise_error
    
    # Check user permission (for non-admin users)
    user_perm_error = _check_user_permission(
        path,
        "作成",
        allow_native_employee=True,
    )
    if user_perm_error:
        return user_perm_error
    
    # Check path protection (for admin users)
    protection_error = _check_path_protection(path, "作成")
    if protection_error:
        return protection_error

    permission_error = _confirm_tool_action(
        "create_file",
        {"path": path},
        "ユーザーによってファイル作成がキャンセルされました。",
    )
    if permission_error:
        return permission_error
    
    try:
        editor = get_file_editor()
        result = editor.create(path, content)
        return {
            "success": True,
            "message": result
        }
    except FileEditorError as e:
        return {
            "success": False,
            "error": str(e)
        }
    except Exception as e:
        logger.error(f"Error in create_file: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"予期しないエラー: {str(e)}"
        }


@tool
@_enterprise_scoped()
def delete_file(path: str) -> Dict[str, Any]:
    """ファイルまたはディレクトリを削除する
    
    ファイルの場合は単体削除、ディレクトリの場合は中身ごと再帰的に削除します。
    削除操作には注意が必要です。
    
    Args:
        path: 削除するファイルまたはディレクトリのパス
    
    Returns:
        Dict[str, Any]: 削除結果
    
    Examples:
        >>> delete_file("temp.txt")
        >>> delete_file("D:\\Download\\old_folder")
        >>> delete_file("/tmp/cache")
    """
    import os
    import shutil
    print(f"[Tool] delete_file が呼び出されました: {path}")
    
    path, resolution_error = _resolve_mutation_target(path, "delete")
    if resolution_error or not path:
        return resolution_error or {"success": False, "error": "パスを解決できませんでした"}

    enterprise_error = _enterprise_project_write_error(path, "削除")
    if enterprise_error:
        return enterprise_error
    
    # Check user permission (for non-admin users)
    user_perm_error = _check_user_permission(path, "削除")
    if user_perm_error:
        return user_perm_error
    
    # Check path protection (for admin users)
    protection_error = _check_path_protection(path, "削除")
    if protection_error:
        return protection_error

    permission_error = _confirm_tool_action(
        "delete_file",
        {"path": path},
        "ユーザーによってファイル削除がキャンセルされました。",
    )
    if permission_error:
        return permission_error

    native_lane_error = _enterprise_native_mutation_error(
        "削除",
        allow_native_employee=False,
    )
    if native_lane_error:
        return native_lane_error
    
    try:
        scope = get_current_run_scope()
        if scope is not None:
            try:
                path = str(scope.assert_delete_allowed(path))
            except RunScopeViolation as exc:
                return {"success": False, "error": str(exc)}
        if not os.path.exists(path):
            return {
                "success": False,
                "error": f"File or directory not found: {path}"
            }
        
        if os.path.isdir(path):
            # Delete directory recursively
            shutil.rmtree(path)
            return {
                "success": True,
                "message": f"Directory deleted successfully (including all contents): {path}"
            }
        else:
            # Delete single file
            os.remove(path)
            return {
                "success": True,
                "message": f"File deleted successfully: {path}"
            }
    except PermissionError:
        return {
            "success": False,
            "error": f"Permission denied: {path}"
        }
    except Exception as e:
        logger.error(f"Error in delete_file: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"予期しないエラー: {str(e)}"
        }


@tool
@_enterprise_scoped()
def append_to_file(path: str, content: str) -> Dict[str, Any]:
    """ファイルの末尾に内容を追記する
    
    Args:
        path: 追記するファイルのパス
        content: 追記する内容
    
    Returns:
        Dict[str, Any]: 追記結果
    
    Examples:
        >>> append_to_file("log.txt", "新しいログエントリ")
        >>> append_to_file("data.txt", "\\n追加データ")
    """
    import os
    print(f"[Tool] append_to_file が呼び出されました: {path}")
    
    path, resolution_error = _resolve_mutation_target(path, "append")
    if resolution_error or not path:
        return resolution_error or {"success": False, "error": "パスを解決できませんでした"}

    enterprise_error = _enterprise_project_write_error(path, "編集")
    if enterprise_error:
        return enterprise_error
    
    # Check user permission (for non-admin users)
    user_perm_error = _check_user_permission(path, "編集")
    if user_perm_error:
        return user_perm_error
    
    # Check path protection (for admin users)
    protection_error = _check_path_protection(path, "編集")
    if protection_error:
        return protection_error

    permission_error = _confirm_tool_action(
        "append_to_file",
        {"path": path},
        "ユーザーによってファイル追記がキャンセルされました。",
    )
    if permission_error:
        return permission_error

    native_lane_error = _enterprise_native_mutation_error(
        "追記",
        allow_native_employee=False,
    )
    if native_lane_error:
        return native_lane_error
    
    try:
        if not os.path.exists(path):
            return {
                "success": False,
                "error": f"File not found: {path}. Use create_file for new files."
            }
        
        with open(path, 'a', encoding='utf-8') as f:
            f.write(content)
        
        return {
            "success": True,
            "message": f"Content appended to file: {path}"
        }
    except PermissionError:
        return {
            "success": False,
            "error": f"Permission denied: {path}"
        }
    except Exception as e:
        logger.error(f"Error in append_to_file: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"予期しないエラー: {str(e)}"
        }


@tool
@_enterprise_scoped()
def edit_file(
    path: str,
    old_str: str,
    new_str: str
) -> Dict[str, Any]:
    """ファイル内の文字列を置換して編集する
    
    old_strはファイル内で一意である必要があります（複数箇所にある場合はエラー）。
    編集は取り消し可能です（undo_editを使用）。
    
    Args:
        path: 編集するファイルのパス
        old_str: 置換する元の文字列（ファイル内で一意である必要あり）
        new_str: 置換後の文字列
    
    Returns:
        Dict[str, Any]: 編集結果
    
    Examples:
        >>> edit_file("config.py", "DEBUG = False", "DEBUG = True")
        >>> edit_file("main.py", "def old_func():", "def new_func():")
    """
    print(f"[Tool] edit_file が呼び出されました: {path}")
    
    path, resolution_error = _resolve_mutation_target(
        path,
        "edit",
        allow_native_employee=True,
    )
    if resolution_error or not path:
        return resolution_error or {"success": False, "error": "パスを解決できませんでした"}

    enterprise_error = _enterprise_project_write_error(path, "編集")
    if enterprise_error:
        return enterprise_error
    
    # Check user permission (for non-admin users)
    user_perm_error = _check_user_permission(
        path,
        "編集",
        allow_native_employee=True,
    )
    if user_perm_error:
        return user_perm_error
    
    # Check path protection (for admin users)
    protection_error = _check_path_protection(path, "編集")
    if protection_error:
        return protection_error

    permission_error = _confirm_tool_action(
        "edit_file",
        {"path": path},
        "ユーザーによってファイル編集がキャンセルされました。",
    )
    if permission_error:
        return permission_error
    
    try:
        editor = get_file_editor()
        result = editor.str_replace(path, old_str, new_str)
        return {
            "success": True,
            "message": result
        }
    except FileEditorError as e:
        return {
            "success": False,
            "error": str(e)
        }
    except Exception as e:
        logger.error(f"Error in edit_file: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"予期しないエラー: {str(e)}"
        }


@tool
@_enterprise_scoped()
def insert_to_file(
    path: str,
    line_number: int,
    content: str
) -> Dict[str, Any]:
    """ファイルの指定行に内容を挿入する
    
    Args:
        path: 編集するファイルのパス
        line_number: 挿入する行番号（0=ファイルの先頭、n=n行目の後）
        content: 挿入する内容
    
    Returns:
        Dict[str, Any]: 挿入結果
    
    Examples:
        >>> insert_to_file("main.py", 0, "# -*- coding: utf-8 -*-")
        >>> insert_to_file("config.yaml", 10, "new_setting: value")
    """
    print(f"[Tool] insert_to_file が呼び出されました: {path} at line {line_number}")
    
    path, resolution_error = _resolve_mutation_target(path, "insert")
    if resolution_error or not path:
        return resolution_error or {"success": False, "error": "パスを解決できませんでした"}

    enterprise_error = _enterprise_project_write_error(path, "編集")
    if enterprise_error:
        return enterprise_error
    
    # Check user permission (for non-admin users)
    user_perm_error = _check_user_permission(path, "編集")
    if user_perm_error:
        return user_perm_error
    
    # Check path protection (for admin users)
    protection_error = _check_path_protection(path, "編集")
    if protection_error:
        return protection_error

    permission_error = _confirm_tool_action(
        "insert_to_file",
        {"path": path, "line_number": line_number},
        "ユーザーによってファイル挿入がキャンセルされました。",
    )
    if permission_error:
        return permission_error

    native_lane_error = _enterprise_native_mutation_error(
        "挿入",
        allow_native_employee=False,
    )
    if native_lane_error:
        return native_lane_error
    
    try:
        editor = get_file_editor()
        result = editor.insert(path, line_number, content)
        return {
            "success": True,
            "message": result
        }
    except FileEditorError as e:
        return {
            "success": False,
            "error": str(e)
        }
    except Exception as e:
        logger.error(f"Error in insert_to_file: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"予期しないエラー: {str(e)}"
        }


@tool
@_enterprise_scoped()
def undo_edit(path: str) -> Dict[str, Any]:
    """ファイルの直前の編集を取り消す
    
    edit_fileまたはinsert_to_fileで行った変更を元に戻します。
    
    Args:
        path: 取り消し対象のファイルパス
    
    Returns:
        Dict[str, Any]: 取り消し結果
    
    Examples:
        >>> undo_edit("config.py")
    """
    print(f"[Tool] undo_edit が呼び出されました: {path}")
    
    path, resolution_error = _resolve_mutation_target(path, "undo")
    if resolution_error or not path:
        return resolution_error or {"success": False, "error": "パスを解決できませんでした"}

    enterprise_error = _enterprise_project_write_error(path, "編集")
    if enterprise_error:
        return enterprise_error

    # Keep undo behind the same project/personal ACL and protected-path
    # boundary as the edit operation it reverses.  This matters now that an
    # explicit workspace namespace resolves canonically instead of being
    # shadowed below the current user's personal directory.
    user_perm_error = _check_user_permission(path, "編集")
    if user_perm_error:
        return user_perm_error

    protection_error = _check_path_protection(path, "編集")
    if protection_error:
        return protection_error

    permission_error = _confirm_tool_action(
        "undo_edit",
        {"path": path},
        "ユーザーによって編集取り消しがキャンセルされました。",
    )
    if permission_error:
        return permission_error

    native_lane_error = _enterprise_native_mutation_error(
        "取り消し",
        allow_native_employee=False,
    )
    if native_lane_error:
        return native_lane_error
    
    try:
        editor = get_file_editor()
        result = editor.undo(path)
        return {
            "success": True,
            "message": result
        }
    except FileEditorError as e:
        return {
            "success": False,
            "error": str(e)
        }
    except Exception as e:
        logger.error(f"Error in undo_edit: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"予期しないエラー: {str(e)}"
        }


@tool
@_enterprise_scoped()
def list_directory(
    path: str = "",
    max_depth: int = 1,
    pattern: Optional[str] = None,
    include_files: bool = True,
    max_entries: int = 300,
) -> Dict[str, Any]:
    """List folder contents, by workspace-relative path or by absolute path.

    An empty path lists the workspace root. `max_depth` 1 (default) lists only
    the direct children; raise it for a bounded recursive inventory, capped by
    `max_entries`. Each entry reports name, path, kind, size, and modified time.

    Args:
        path: Workspace-relative or absolute folder path ("" for workspace root).
        max_depth: Recursion depth, 1 for a flat listing (max 8).
        pattern: Optional glob filter on entry names (e.g. "*.py").
        include_files: False to list folders only.
        max_entries: Maximum entries to return (default 300, max 1000).

    Returns:
        Dict[str, Any]: Listed entries and truncation state.

    Examples:
        >>> list_directory()
        >>> list_directory("_projects/project-1", max_depth=3)
        >>> list_directory("src", pattern="*.py")
    """
    print(f"[Tool] list_directory が呼び出されました: {path}")

    resolved, _is_absolute, error = _resolve_read_target(
        path,
        allow_native_employee=True,
    )
    if error or resolved is None:
        return {"success": False, "error": error or "パスを解決できませんでした"}

    try:
        service = _workspace_service()
        result = service.walk_workspace_tree(
            resolved,
            max_depth=max_depth,
            include_files=include_files,
            max_entries=max_entries,
            is_admin=True,
        )
    except Exception as e:
        logger.error(f"Error in list_directory: {e}", exc_info=True)
        return {"success": False, "error": f"予期しないエラー: {str(e)}"}

    if result.get("success") and _enterprise_sensitive_filter_active():
        result["entries"] = [
            entry
            for entry in result.get("entries", [])
            if not _enterprise_sensitive_path_error(str(entry.get("path", "")))
        ]
        # Child counts from the generic explorer include entries that were
        # intentionally redacted.  Do not expose that metadata side channel.
        for entry in result["entries"]:
            if entry.get("kind") == "directory":
                entry["item_count"] = None
        result["total_returned"] = len(result["entries"])
    if result.get("success") and pattern:
        matcher = str(pattern).lower()
        result["entries"] = [
            entry
            for entry in result.get("entries", [])
            if fnmatch.fnmatch(str(entry.get("name", "")).lower(), matcher)
        ]
        result["total_returned"] = len(result["entries"])
        result["pattern"] = pattern
    return result


@tool
@_enterprise_scoped()
def search_files(
    query: str,
    path: str = "",
    extensions: Optional[List[str]] = None,
    search_content: bool = False,
    include_dirs: bool = True,
    include_files: bool = True,
    max_results: int = 50,
) -> Dict[str, Any]:
    """Search files and folders by name, or search inside file contents.

    By default this matches names under the search root, returning folders as
    well as files, so it is the way to locate a workspace item you only know by
    name. `query` is a glob when it contains `*` or `?`, otherwise a
    case-insensitive substring. Set `search_content=True` to grep file contents
    with a regular expression instead. The path is workspace-relative
    ("" for the workspace root) or absolute.

    Args:
        query: Name pattern, or a regular expression when search_content is True.
        path: Workspace-relative or absolute search root ("" for workspace root).
        extensions: Optional extension filter (e.g. [".py", ".md"]).
        search_content: True to search file contents instead of names.
        include_dirs: False to skip folders in name search.
        include_files: False to skip files in name search.
        max_results: Maximum results to return (default 50).

    Returns:
        Dict[str, Any]: Search results.

    Examples:
        >>> search_files("AIエージェント共有")
        >>> search_files("*.py", "src", max_results=100)
        >>> search_files("TODO", "src", search_content=True)
    """
    print(f"[Tool] search_files が呼び出されました: {query} in {path}")

    extensions = _normalize_extensions(extensions)
    resolved, _is_absolute, error = _resolve_read_target(
        path,
        allow_native_employee=True,
    )
    if error or resolved is None:
        return {"success": False, "error": error or "パスを解決できませんでした"}

    if search_content:
        try:
            fs = get_file_system()
            result = fs.search_files(
                query,
                resolved,
                extensions=extensions,
                search_content=True,
                max_results=int(max_results or 50),
                exclude_path=(
                    lambda candidate: bool(
                        _enterprise_sensitive_path_error(str(candidate))
                    )
                )
                if _enterprise_sensitive_filter_active()
                else None,
            )
        except FileSystemError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"Error in search_files: {e}", exc_info=True)
            return {"success": False, "error": f"予期しないエラー: {str(e)}"}
        return {"success": True, "query": query, "content": result}

    try:
        service = _workspace_service()
        result = service.search_workspace_entries(
            query=query,
            path=resolved,
            include_dirs=include_dirs,
            include_files=include_files,
            max_results=max_results,
            extensions=extensions,
            is_admin=True,
        )
        if result.get("success") and _enterprise_sensitive_filter_active():
            result["results"] = [
                entry
                for entry in result.get("results", [])
                if not _enterprise_sensitive_path_error(str(entry.get("path", "")))
            ]
            for entry in result["results"]:
                if entry.get("kind") == "directory":
                    entry["item_count"] = None
            result["total_returned"] = len(result["results"])
        return result
    except Exception as e:
        logger.error(f"Error in search_files: {e}", exc_info=True)
        return {"success": False, "error": f"予期しないエラー: {str(e)}"}
