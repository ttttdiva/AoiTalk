"""Trusted current-turn builder for :class:`HarnessExecutionScope`.

This service is the only supported bridge from request/runtime state to the
upper execution capability.  It reads the task-local ``TurnContext``, the
server-bound OS user context and deployment configuration; it never accepts
paths or grants from tool arguments.  Organization/DB grant storage is kept
behind the small ``ExternalReadGrantResolver`` protocol so deployments can
plug in their own authorization source without moving that policy into the
filesystem sandbox.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from ..security.harness_execution_scope import (
    ExternalReadGrant,
    HarnessExecutionScope,
    HarnessExecutionScopeConfigurationError,
    NetworkCapability,
    ResourceLimits,
    _canonicalise,
    _canonical_roots,
    _is_within,
    _path_key,
)
from .agent_run_service import get_current_agent_run_id
from .project_context import get_runtime_project_context
from .turn_context import TurnContext, get_turn_context

PathLike = str | os.PathLike[str]


class HarnessExecutionScopeServiceError(HarnessExecutionScopeConfigurationError):
    """Base service-level scope construction failure."""


class MissingTrustedExecutionContext(HarnessExecutionScopeServiceError):
    """Raised when current-turn/server identity is absent or inconsistent."""


class ExternalReadGrantResolver(Protocol):
    """Optional server-owned source for approved external read grants.

    Implementations may consult organization/admin/DB policy.  The resolver
    receives only authenticated identity and deployment config; raw model/tool
    paths are never passed.  Returned records are still validated by this
    service before becoming mounts.
    """

    def resolve_external_read_grants(
        self,
        *,
        user_id: str,
        organization_id: str | None,
        config: Any,
    ) -> Iterable[Any]: ...


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    getter = getattr(config, "get", None)
    if callable(getter):
        with suppress(Exception):
            result = getter(key, default)
            if result is not None:
                return result
    root = config.config if hasattr(config, "config") else config
    current: Any = root
    for part in key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return default
        current = current[part]
    return current


def _first_config(config: Any, keys: Sequence[str], default: Any = None) -> Any:
    for key in keys:
        value = _config_get(config, key, None)
        if value is not None:
            return value
    return default


def _clean_id(value: Any, label: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise MissingTrustedExecutionContext(f"{label} is unavailable")
        return None
    text = str(value).strip()
    if not text:
        if required:
            raise MissingTrustedExecutionContext(f"{label} is unavailable")
        return None
    if "\x00" in text or len(text) > 512:
        raise MissingTrustedExecutionContext(f"invalid {label}")
    return text


def _namespace_id(value: Any, label: str, *, required: bool = False) -> str | None:
    text = _clean_id(value, label, required=required)
    if text is None:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", text):
        raise MissingTrustedExecutionContext(f"invalid {label}")
    return text


def _normalise_ids(values: Any) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, os.PathLike)):
        values = (values,)
    if not isinstance(values, Iterable):
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        result.append(text)
    return tuple(result)


def _workspace_root(config: Any) -> Path:
    value = _first_config(
        config,
        (
            "harness_execution.workspace_root",
            "harness.execution.workspace_root",
            "agent_harness.execution.workspace_root",
            "workspaces_root",
            "workspace_root",
        ),
        os.environ.get("AOITALK_WORKSPACES_DIR", "./workspaces"),
    )
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise HarnessExecutionScopeServiceError("trusted workspace root is unavailable")
    lexical = Path(str(value)).expanduser()
    if not lexical.is_absolute():
        lexical = Path.cwd() / lexical
    lexical = Path(os.path.abspath(lexical))
    if _path_has_any_link_or_reparse(lexical):
        raise HarnessExecutionScopeServiceError(
            "trusted workspace root contains a symbolic link/reparse point"
        )
    try:
        root = _canonicalise(value)
    except (OSError, ValueError, RuntimeError) as exc:
        raise HarnessExecutionScopeServiceError("trusted workspace root cannot be canonicalized") from exc
    if not root.exists():
        with suppress(OSError):
            root.mkdir(parents=True, exist_ok=True)
    if not root.exists() or not root.is_dir():
        raise HarnessExecutionScopeServiceError("trusted workspace root is not a directory")
    # Never permit the deployment workspace itself to be an escaping link.
    if _path_contains_unsafe_link(root, root):
        raise HarnessExecutionScopeServiceError("trusted workspace root contains a reparse point")
    return root


def _path_contains_unsafe_link(path: Path, root: Path) -> bool:
    # Reuse the scope module's behaviour without exposing that helper as API.
    from ..security.harness_execution_scope import _existing_components, _component_link

    for component in _existing_components(path):
        linked, inspectable = _component_link(component)
        if not inspectable:
            return True
        if linked:
            try:
                if not _is_within(_canonicalise(component), root):
                    return True
            except (OSError, ValueError, RuntimeError):
                return True
    return False


def _path_has_any_link_or_reparse(path: Path) -> bool:
    """Reject lexical grant aliases before canonicalisation hides them."""

    from ..security.harness_execution_scope import _component_link, _existing_components

    for component in _existing_components(path):
        linked, inspectable = _component_link(component)
        if not inspectable or linked:
            return True
    return False


def _sensitive_name(name: str) -> bool:
    lowered = name.casefold()
    if lowered in {
        ".env", "credentials", "credential", "secrets", "secret",
        "id_rsa", "id_ed25519",
    }:
        return True
    return lowered.startswith(".env.") or lowered.endswith((".key", ".pem", ".p12", ".pfx", ".secret", ".secrets"))


def _sensitive_root(path: Path) -> bool:
    return any(_sensitive_name(part) for part in path.parts)


def _protected_roots(config: Any, workspace: Path) -> tuple[Path, ...]:
    configured = _first_config(
        config,
        (
            "harness_execution.protected_roots",
            "harness.execution.protected_roots",
            "agent_harness.execution.protected_roots",
            "enterprise.protected_roots",
        ),
        (),
    )
    defaults: list[PathLike] = []
    if os.name == "nt":
        for env_name in ("WINDIR", "ProgramFiles", "ProgramData"):
            if os.environ.get(env_name):
                defaults.append(os.environ[env_name])
    else:
        defaults.extend(("/etc", "/root", "/proc", "/sys", "/dev", "/usr"))
    roots: list[Path] = []
    values = configured if isinstance(configured, (list, tuple, set)) else ((configured,) if configured else ())
    for value in (*defaults, *values):
        if not isinstance(value, (str, os.PathLike)):
            continue
        with suppress(Exception):
            root = _canonicalise(value, base=workspace)
            if root.exists() and root.is_dir() and not _sensitive_root(root):
                roots.append(root)
    return tuple(dict.fromkeys(roots))


def _is_protected(path: Path, protected_roots: Sequence[Path]) -> bool:
    return _sensitive_root(path) or any(_is_within(path, root) for root in protected_roots)


def _grant_records(config: Any, resolver: Any, *, user_id: str, organization_id: str | None) -> Iterable[Any]:
    if resolver is not None:
        method = getattr(resolver, "resolve_external_read_grants", None)
        if callable(method):
            return method(user_id=user_id, organization_id=organization_id, config=config)
        if callable(resolver):
            return resolver(user_id=user_id, organization_id=organization_id, config=config)
        raise HarnessExecutionScopeServiceError("invalid external grant resolver")
    configured = _first_config(
        config,
        (
            "harness_execution.external_read_grants",
            "harness.execution.external_read_grants",
            "agent_harness.execution.external_read_grants",
            "enterprise.harness.external_read_grants",
            "enterprise.execution_scope.external_read_grants",
            "external_read_grants",
        ),
        (),
    )
    if configured is None:
        return ()
    if not isinstance(configured, (list, tuple)):
        raise HarnessExecutionScopeServiceError("external_read_grants must be a list")
    return configured


def _build_external_grants(
    config: Any,
    resolver: Any,
    *,
    user_id: str,
    organization_id: str | None,
    workspace: Path,
    protected_roots: Sequence[Path],
) -> tuple[ExternalReadGrant, ...]:
    records = _grant_records(config, resolver, user_id=user_id, organization_id=organization_id)
    grants: list[ExternalReadGrant] = []
    aliases: set[str] = set()
    roots: set[str] = set()
    for index, record in enumerate(records or ()):
        if not isinstance(record, Mapping):
            raise HarnessExecutionScopeServiceError("external grant records must be mappings")
        if record.get("enabled") is False or record.get("approved") is False:
            continue
        if record.get("read_only") is False or record.get("writable") is True:
            raise HarnessExecutionScopeServiceError("external grants must be read-only")
        grant_user = record.get("user_id", record.get("principal_id"))
        grant_org = record.get("organization_id", record.get("org_id"))
        if grant_user is not None and str(grant_user).strip() != user_id:
            continue
        if grant_org is not None and str(grant_org).strip() != str(organization_id or ""):
            continue
        raw_root = record.get("root", record.get("path"))
        if not isinstance(raw_root, (str, os.PathLike)) or not str(raw_root).strip():
            raise HarnessExecutionScopeServiceError("external grant root is required")
        lexical_root = Path(str(raw_root))
        if not lexical_root.is_absolute():
            lexical_root = workspace / lexical_root
        lexical_root = Path(os.path.abspath(lexical_root))
        if _path_has_any_link_or_reparse(lexical_root):
            raise HarnessExecutionScopeServiceError(
                "external grant root contains a symbolic link/reparse point"
            )
        try:
            root = _canonicalise(raw_root, base=workspace)
        except (OSError, ValueError, RuntimeError) as exc:
            raise HarnessExecutionScopeServiceError("external grant root cannot be canonicalized") from exc
        if not root.exists() or not root.is_dir() or _path_contains_unsafe_link(root, root):
            raise HarnessExecutionScopeServiceError("external grant root is unavailable or unsafe")
        if root == Path(root.anchor):
            raise HarnessExecutionScopeServiceError(
                "filesystem/share roots cannot be granted as external inputs"
            )
        if os.name == "nt" and os.environ.get("USERPROFILE"):
            current_profile = _canonicalise(os.environ["USERPROFILE"])
            profiles_root = current_profile.parent
            if _is_within(root, profiles_root):
                if not _is_within(root, current_profile):
                    raise HarnessExecutionScopeServiceError(
                        "external grant cannot expose another OS user's private profile"
                    )
                if record.get("allow_user_profile") is not True or (
                    grant_user is None
                    or str(grant_user).strip() != user_id
                ):
                    raise HarnessExecutionScopeServiceError(
                        "user-profile grants require explicit principal-bound approval"
                    )
        managed_private_namespaces = (
            "_users", "_projects", "_apps", "_app_instances",
            "_app_artifacts", "_docs",
        )
        managed_roots = (
            workspace,
            *(workspace / namespace for namespace in managed_private_namespaces),
        )
        if (
            _is_protected(root, protected_roots)
            or any(_is_within(protected, root) for protected in protected_roots)
            or any(
            _is_within(root, managed) or _is_within(managed, root)
            for managed in managed_roots
            )
        ):
            raise HarnessExecutionScopeServiceError("external grant points to a protected/private workspace")
        grant_id = record.get("grant_id", record.get("id"))
        alias = record.get("mount_alias", record.get("alias"))
        if alias is None:
            alias = re.sub(r"[^A-Za-z0-9._-]+", "-", str(grant_id or f"external-{index + 1}")).strip("-")
        alias = str(alias).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", alias or ""):
            raise HarnessExecutionScopeServiceError("invalid external grant mount alias")
        key = alias.casefold()
        root_key = _path_key(root)
        if key in aliases or root_key in roots:
            raise HarnessExecutionScopeServiceError("duplicate external grant alias/root")
        aliases.add(key)
        roots.add(root_key)
        grants.append(
            ExternalReadGrant(
                root=root,
                mount_alias=alias,
                grant_id=(str(grant_id).strip() if grant_id else None),
                organization_id=(str(grant_org).strip() if grant_org else None),
                principal_id=(str(grant_user).strip() if grant_user else None),
            )
        )
    return tuple(grants)


def _project_root(
    project_context: Any,
    *,
    project_id: str,
    workspace: Path,
    protected_roots: Sequence[Path],
) -> Path:
    if project_context is not None and not isinstance(project_context, Mapping):
        raise MissingTrustedExecutionContext("runtime project context is invalid")
    if isinstance(project_context, Mapping):
        context_id = str(project_context.get("id") or "").strip()
        if context_id and context_id != project_id:
            raise MissingTrustedExecutionContext("runtime Project identity does not match TurnContext")
    expected = workspace / "_projects" / f"project_{project_id}"
    if _path_has_any_link_or_reparse(Path(os.path.abspath(expected))):
        raise MissingTrustedExecutionContext(
            "runtime Project namespace contains a symbolic link/reparse point"
        )
    raw_storage = project_context.get("project_storage_path") if isinstance(project_context, Mapping) else None
    candidate = expected
    if isinstance(raw_storage, (str, os.PathLike)) and str(raw_storage).strip():
        text = str(raw_storage).replace("\\", "/").strip()
        if os.path.isabs(text):
            lexical_candidate = Path(os.path.abspath(str(raw_storage)))
            if _path_has_any_link_or_reparse(lexical_candidate):
                raise MissingTrustedExecutionContext(
                    "runtime Project storage path contains a link/reparse point"
                )
            candidate = _canonicalise(text)
            if _path_key(candidate) != _path_key(expected):
                raise MissingTrustedExecutionContext(
                    "runtime Project storage path mismatch"
                )
        elif text.casefold() == f"_projects/project_{project_id}".casefold():
            candidate = _canonicalise(text, base=workspace)
        else:
            raise MissingTrustedExecutionContext("runtime Project storage path mismatch")
    candidate = _canonicalise(candidate)
    if not candidate.exists() or not candidate.is_dir() or _is_protected(candidate, protected_roots):
        raise MissingTrustedExecutionContext("runtime Project root is unavailable or protected")
    if not _is_within(candidate, workspace / "_projects"):
        raise MissingTrustedExecutionContext("runtime Project root is outside managed workspace")
    return candidate


def _limits(config: Any) -> ResourceLimits:
    raw = _first_config(
        config,
        (
            "harness_execution.resource_limits",
            "harness.execution.resource_limits",
            "agent_harness.execution.resource_limits",
            "enterprise.execution_scope.resource_limits",
        ),
        {},
    )
    if not isinstance(raw, Mapping):
        raw = {}
    return ResourceLimits.from_values(raw)


def _network(config: Any) -> tuple[NetworkCapability, tuple[str, ...]]:
    value = _first_config(
        config,
        (
            "harness_execution.network_capability",
            "harness.execution.network_capability",
            "agent_harness.execution.network_capability",
            "enterprise.execution_scope.network_capability",
        ),
        "none",
    )
    try:
        capability = NetworkCapability(str(value).strip().lower())
    except ValueError as exc:
        raise HarnessExecutionScopeServiceError("invalid network capability") from exc
    raw_allowlist = _first_config(
        config,
        (
            "harness_execution.network_allowlist",
            "harness.execution.network_allowlist",
            "agent_harness.execution.network_allowlist",
            "enterprise.execution_scope.network_allowlist",
        ),
        (),
    )
    allowlist = _normalise_ids(raw_allowlist)
    if capability is NetworkCapability.ALLOWLIST and not allowlist:
        raise HarnessExecutionScopeServiceError("allowlist network capability requires destinations")
    return capability, allowlist


def _capability_ids(config: Any, key_names: Sequence[str], *, label: str) -> tuple[str, ...]:
    value = _first_config(config, key_names, ())
    if isinstance(value, Mapping):
        # Secret values are never accepted.  Only explicit capability IDs may
        # cross this boundary.
        if any(str(key).casefold() in {"value", "secret", "token", "credential", "api_key"} for key in value):
            raise HarnessExecutionScopeServiceError(f"{label} values cannot be supplied")
        value = value.get("ids", value.get("capability_ids", ()))
    result = _normalise_ids(value)
    for identifier in result:
        if any(marker in identifier.casefold() for marker in ("=", "://", "token:", "secret:")):
            raise HarnessExecutionScopeServiceError(f"invalid {label} identifier")
    return result


def _server_user_context(candidate: Any = None) -> dict[str, Any]:
    try:
        from ..tools.os_operations.tools import get_current_user_context

        current = get_current_user_context()
    except Exception as exc:
        raise MissingTrustedExecutionContext("server-bound OS user context is unavailable") from exc
    if not isinstance(current, Mapping):
        raise MissingTrustedExecutionContext("server-bound OS user context is invalid")
    if candidate is not None:
        if not isinstance(candidate, Mapping):
            raise MissingTrustedExecutionContext("untrusted OS user context payload")
        # Compare every authorization-bearing value to the task-local server
        # copy.  A caller cannot widen ACLs by supplying a scope-looking dict.
        for key in ("user_id", "is_admin", "project_ids", "writable_project_ids", "deletable_project_ids", "organization_id", "org_id"):
            if key in candidate and candidate.get(key) != current.get(key):
                raise MissingTrustedExecutionContext("OS user context identity/ACL mismatch")
    return dict(current)


def build_current_turn_enterprise_scope(
    *,
    config: Any = None,
    turn_context: TurnContext | None = None,
    os_user_context: Mapping[str, Any] | None = None,
    project_context: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    audit_id: str | None = None,
    external_grant_resolver: ExternalReadGrantResolver | Callable[..., Iterable[Any]] | None = None,
) -> HarnessExecutionScope:
    """Build the current Enterprise turn's trusted execution capability.

    The selected user's private workspace is the only mutable host root.  A
    selected Project is added read-only only when the server-bound OS context
    grants that Project.  External grants are read-only and must be approved
    by deployment config or the resolver boundary.
    """
    execution_enabled = _first_config(
        config,
        (
            "agent_harness.execution.enabled",
            "harness_execution.enabled",
            "harness.execution.enabled",
        ),
        True,
    )
    if execution_enabled is not True:
        raise HarnessExecutionScopeServiceError(
            "trusted harness execution is disabled by server policy"
        )
    backend_name = str(
        _first_config(
            config,
            (
                "agent_harness.execution.backend",
                "harness_execution.backend",
                "harness.execution.backend",
            ),
            "wsl_bwrap",
        )
        or ""
    ).strip().lower()
    if backend_name not in {"wsl_bwrap", "wsl2_bwrap"}:
        raise HarnessExecutionScopeServiceError(
            f"unsupported trusted harness backend: {backend_name or '(empty)'}"
        )
    backend_available = _first_config(
        config,
        (
            "harness_execution.backend_available",
            "harness.execution.backend_available",
            "agent_harness.execution.backend_available",
            "sandbox_backend_available",
        ),
        True,
    )
    if backend_available is False:
        raise HarnessExecutionScopeServiceError(
            "trusted harness sandbox backend prerequisites are unavailable"
        )
    turn = get_turn_context() if turn_context is None else turn_context
    if not isinstance(turn, TurnContext):
        raise MissingTrustedExecutionContext("turn_context must be a trusted TurnContext")
    server_context = _server_user_context(os_user_context)
    user_id = _namespace_id(turn.user_id, "TurnContext.user_id", required=True)
    bound_user = _namespace_id(
        server_context.get("user_id"),
        "server user_id",
        required=True,
    )
    if bound_user != user_id:
        raise MissingTrustedExecutionContext("TurnContext user does not match server user context")
    organization_id = _clean_id(
        server_context.get("organization_id", server_context.get("org_id")),
        "organization_id",
    )
    workspace = _workspace_root(config)
    protected_roots = _protected_roots(config, workspace)
    user_root = _canonicalise(workspace / "_users" / f"user_{user_id}")
    lexical_user_root = workspace / "_users" / f"user_{user_id}"
    if _path_has_any_link_or_reparse(Path(os.path.abspath(lexical_user_root))):
        raise HarnessExecutionScopeServiceError(
            "private user namespace contains a symbolic link/reparse point"
        )
    if not _is_within(user_root, workspace / "_users") or _is_protected(user_root, protected_roots):
        raise HarnessExecutionScopeServiceError("private user workspace is outside managed namespace")
    if _path_contains_unsafe_link(user_root, workspace / "_users"):
        raise HarnessExecutionScopeServiceError("private user workspace contains an unsafe reparse point")
    if not user_root.exists():
        with suppress(OSError):
            user_root.mkdir(parents=True, exist_ok=True)
    if not user_root.exists() or not user_root.is_dir():
        raise HarnessExecutionScopeServiceError("private user workspace is unavailable")
    if _path_has_any_link_or_reparse(Path(os.path.abspath(lexical_user_root))):
        raise HarnessExecutionScopeServiceError(
            "private user namespace changed to a link/reparse point"
        )

    scratch_values = _first_config(
        config,
        (
            "harness_execution.scratch_roots",
            "harness.execution.scratch_roots",
            "agent_harness.execution.scratch_roots",
        ),
        (),
    )
    if not scratch_values:
        scratch_values = (user_root / ".harness_scratch",)
    raw_scratch_values = (
        (scratch_values,)
        if isinstance(scratch_values, (str, os.PathLike))
        else tuple(scratch_values)
    )
    for raw_scratch in raw_scratch_values:
        lexical_scratch = Path(str(raw_scratch))
        if not lexical_scratch.is_absolute():
            lexical_scratch = user_root / lexical_scratch
        if _path_has_any_link_or_reparse(Path(os.path.abspath(lexical_scratch))):
            raise HarnessExecutionScopeServiceError(
                "scratch root contains a symbolic link/reparse point"
            )
    scratch = _canonical_roots(scratch_values, base=user_root, label="scratch_roots", require_existing=False)
    for root in scratch:
        if not _is_within(root, user_root) or _is_protected(root, protected_roots):
            raise HarnessExecutionScopeServiceError("scratch root must remain inside the private workspace")
        with suppress(OSError):
            root.mkdir(parents=True, exist_ok=True)
        if _path_has_any_link_or_reparse(Path(os.path.abspath(root))):
            raise HarnessExecutionScopeServiceError(
                "scratch root changed to a symbolic link/reparse point"
            )

    read_roots: list[Path] = [user_root, *scratch]
    project_ids = _normalise_ids(
        server_context.get("project_ids", server_context.get("readable_project_ids", ()))
    )
    app_ids = _normalise_ids(
        server_context.get("app_ids", server_context.get("readable_app_ids", ()))
    )
    selected_project = _namespace_id(turn.project_id, "TurnContext.project_id")
    runtime_project = get_runtime_project_context() if project_context is None else project_context
    project_grants: list[ExternalReadGrant] = []
    if isinstance(runtime_project, Mapping):
        runtime_user = str(runtime_project.get("user_id") or "").strip()
        if runtime_user and runtime_user != user_id:
            raise MissingTrustedExecutionContext("runtime Project principal mismatch")
    if selected_project:
        if selected_project.casefold() not in {item.casefold() for item in project_ids}:
            # Admin status does not silently grant a mutable Project root; an
            # explicit server project ID is still required for a read mount.
            raise MissingTrustedExecutionContext("selected Project has no read grant")
        selected_project_root = _project_root(
            runtime_project,
            project_id=selected_project,
            workspace=workspace,
            protected_roots=protected_roots,
        )
        read_roots.append(selected_project_root)
        project_alias = re.sub(
            r"[^A-Za-z0-9._-]+", "-", f"project-{selected_project}"
        ).strip("-")[:96]
        project_grants.append(
            ExternalReadGrant(
                root=selected_project_root,
                mount_alias=project_alias,
                grant_id=f"project:{selected_project}",
                principal_id=user_id,
            )
        )

    configured_grants = _build_external_grants(
        config,
        external_grant_resolver,
        user_id=user_id,
        organization_id=organization_id,
        workspace=workspace,
        protected_roots=protected_roots,
    )
    grants = (*project_grants, *configured_grants)
    read_roots.extend(grant.root for grant in configured_grants)
    network_capability, network_allowlist = _network(config)
    env_ids = _capability_ids(
        config,
        (
            "harness_execution.environment_capability_ids",
            "harness.execution.environment_capability_ids",
            "agent_harness.execution.environment_capability_ids",
        ),
        label="environment capability",
    )
    secret_ids = _capability_ids(
        config,
        (
            "harness_execution.secret_capability_ids",
            "harness.execution.secret_capability_ids",
            "agent_harness.execution.secret_capability_ids",
        ),
        label="secret capability",
    )
    selected_run_id = (
        _clean_id(run_id, "run_id")
        or _clean_id(get_current_agent_run_id(), "current_agent_run_id")
        or _clean_id(turn.message_id, "message_id")
        or _clean_id(turn.session_id, "session_id")
        or uuid.uuid4().hex
    )
    selected_audit_id = _clean_id(audit_id, "audit_id") or f"turn:{selected_run_id}"
    root_identity = "user:" + hashlib.sha256((user_id + "|" + _path_key(user_root)).encode("utf-8", "surrogatepass")).hexdigest()[:32]
    return HarnessExecutionScope._issue(
        user_id=user_id,
        organization_id=organization_id,
        project_ids=project_ids,
        app_ids=app_ids,
        read_roots=tuple(dict.fromkeys(read_roots)),
        write_roots=(user_root, *scratch),
        delete_roots=(user_root, *scratch),
        command_roots=(user_root, *scratch),
        scratch_roots=scratch,
        external_read_grants=grants,
        network_capability=network_capability,
        network_allowlist=network_allowlist,
        environment_capability_ids=env_ids,
        secret_capability_ids=secret_ids,
        resource_limits=_limits(config),
        run_id=selected_run_id,
        audit_id=selected_audit_id,
        root_identity=root_identity,
    )


def build_autonomous_agent_harness_scope(
    *,
    workspace_path: PathLike,
    harness_workspace_root: PathLike,
    resource_limits: ResourceLimits,
    run_id: str,
    audit_id: str,
    network_capability: NetworkCapability = NetworkCapability.NONE,
) -> HarnessExecutionScope:
    """Issue the least-privilege scope for one autonomous harness checkout.

    This factory is intentionally separate from the current-turn builder:
    background Agent Harness work has no authenticated chat principal and must
    not inherit ambient user/Project/App grants.  Both paths are trusted
    server factories, but this one authorizes exactly one WorkspaceManager
    checkout plus an internal scratch directory, with no external roots,
    secrets, environment capabilities, or network access.
    """

    if not isinstance(resource_limits, ResourceLimits):
        raise HarnessExecutionScopeServiceError(
            "autonomous harness resource_limits must be ResourceLimits"
        )
    if network_capability not in {NetworkCapability.NONE, NetworkCapability.BROAD}:
        raise HarnessExecutionScopeServiceError(
            "autonomous harness network capability requires an unavailable broker"
        )
    lexical_root = Path(os.path.abspath(Path(harness_workspace_root).expanduser()))
    lexical_workspace = Path(os.path.abspath(Path(workspace_path).expanduser()))
    if _path_has_any_link_or_reparse(lexical_root) or _path_has_any_link_or_reparse(
        lexical_workspace
    ):
        raise HarnessExecutionScopeServiceError(
            "autonomous harness workspace contains a symbolic link/reparse point"
        )
    try:
        root = _canonicalise(lexical_root)
        workspace = _canonicalise(lexical_workspace)
    except (OSError, ValueError, RuntimeError) as exc:
        raise HarnessExecutionScopeServiceError(
            "autonomous harness workspace cannot be canonicalized"
        ) from exc
    if not root.exists() or not root.is_dir():
        raise HarnessExecutionScopeServiceError(
            "autonomous harness workspace root is unavailable"
        )
    if not workspace.exists() or not workspace.is_dir() or not _is_within(workspace, root):
        raise HarnessExecutionScopeServiceError(
            "autonomous harness checkout is outside the configured workspace root"
        )
    if _path_key(workspace) == _path_key(root):
        raise HarnessExecutionScopeServiceError(
            "autonomous harness checkout cannot be the shared workspace root"
        )
    scratch = workspace / ".aoitalk-harness-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    if _path_has_any_link_or_reparse(Path(os.path.abspath(scratch))):
        raise HarnessExecutionScopeServiceError(
            "autonomous harness scratch contains a symbolic link/reparse point"
        )
    scratch = _canonicalise(scratch)
    if not _is_within(scratch, workspace):
        raise HarnessExecutionScopeServiceError(
            "autonomous harness scratch escaped the checkout"
        )
    selected_run_id = _namespace_id(run_id, "run_id", required=True)
    selected_audit_id = _clean_id(audit_id, "audit_id", required=True)
    root_identity = "agent-harness:" + hashlib.sha256(
        _path_key(workspace).encode("utf-8", "surrogatepass")
    ).hexdigest()[:32]
    return HarnessExecutionScope._issue(
        user_id="agent-harness-system",
        organization_id=None,
        project_ids=(),
        app_ids=(),
        read_roots=(workspace, scratch),
        write_roots=(workspace, scratch),
        delete_roots=(workspace, scratch),
        command_roots=(workspace, scratch),
        scratch_roots=(scratch,),
        external_read_grants=(),
        network_capability=network_capability,
        network_allowlist=(),
        environment_capability_ids=(),
        secret_capability_ids=(),
        resource_limits=resource_limits,
        run_id=selected_run_id,
        audit_id=selected_audit_id,
        root_identity=root_identity,
    )


# Short aliases used by route/runtime integrations.
build_enterprise_execution_scope = build_current_turn_enterprise_scope
build_current_turn_scope = build_current_turn_enterprise_scope
create_current_turn_enterprise_scope = build_current_turn_enterprise_scope


class HarnessExecutionScopeService:
    """Facade retaining a stable service API for runtime dependency wiring."""

    build_current_turn_enterprise_scope = staticmethod(build_current_turn_enterprise_scope)
    build_enterprise_execution_scope = staticmethod(build_current_turn_enterprise_scope)
    build_current_turn_scope = staticmethod(build_current_turn_enterprise_scope)
    create = staticmethod(build_current_turn_enterprise_scope)
    build_autonomous_agent_harness_scope = staticmethod(
        build_autonomous_agent_harness_scope
    )


__all__ = [
    "ExternalReadGrantResolver",
    "HarnessExecutionScopeService",
    "HarnessExecutionScopeServiceError",
    "MissingTrustedExecutionContext",
    "build_current_turn_enterprise_scope",
    "build_autonomous_agent_harness_scope",
    "build_current_turn_scope",
    "build_enterprise_execution_scope",
    "create_current_turn_enterprise_scope",
]
