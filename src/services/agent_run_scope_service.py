"""Parent-owned construction and propagation of coding-run scope.

The filesystem path policy itself lives in :mod:`src.security.agent_run_scope`.
This module owns the *trust boundary* around that policy: only a parent
controller that supplies an explicit repository root may construct a scope and
attach it to a child Agent Team turn.  A string in a model/project payload is
never promoted to a mutation authority.
"""

from __future__ import annotations

import os
import hashlib
import inspect
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

from ..security.agent_run_scope import AgentRunScope
from ..security.git_publication_gate import RunSnapshot
from ..security.harness_execution_scope import (
    HarnessExecutionScope,
    NetworkCapability,
    ResourceLimits,
)


TRUSTED_PARENT_CONTEXT_KEY = "_trusted_agent_run_context"
TRUSTED_PARENT_CAPABILITY_KEY = "_trusted_agent_run_capability"
RUN_SCOPE_CONTEXT_KEY = "run_scope"
REQUIRE_RUN_SCOPE_KEY = "require_run_scope"
HARNESS_EXECUTION_SCOPE_CONTEXT_KEY = "_trusted_harness_execution_scope"


class AgentRunScopeServiceError(RuntimeError):
    """Base error for parent scope construction/propagation."""


class UntrustedParentScopeError(AgentRunScopeServiceError):
    """Raised when a caller tries to promote untrusted data into a scope."""


class ParentRunScopeMismatchError(UntrustedParentScopeError):
    """Raised when a scope is reused by another parent run or repository."""


class InteractiveAgentRunScopeDenied(AgentRunScopeServiceError):
    """Raised when a Chat Agent cannot obtain a fresh native-run proof."""


_INTERACTIVE_ACTIVE_RUN_STATES = frozenset({"queued", "running", "waiting", "active"})
_INTERACTIVE_WORK_STATE = "running"
_INTERACTIVE_WORK_SOURCE = "chat_turn"
_INTERACTIVE_WORK_ADAPTER = "interactive_chat"
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


def _interactive_id(value: Any, *, label: str, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise InteractiveAgentRunScopeDenied(f"{label} is required")
    if len(text) > 255 or "\x00" in text:
        raise InteractiveAgentRunScopeDenied(f"{label} is invalid")
    return text


def _interactive_id_equal(left: Any, right: Any) -> bool:
    return str(left or "").strip().casefold() == str(right or "").strip().casefold()


def _interactive_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _interactive_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _interactive_uuid_or_text(value: Any) -> Any:
    """Use UUIDs for SQLAlchemy while keeping symbolic test doubles usable."""

    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return str(value)


def _interactive_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _interactive_now() -> datetime:
    return datetime.now(timezone.utc)


def _interactive_safe_workspace_root(
    workspace_root: str | os.PathLike[str] | None,
    *,
    agent_id: str,
    session_id: str,
) -> tuple[Path, Path]:
    """Derive and create one deterministic server-owned session workspace."""

    configured = workspace_root or os.environ.get("AOITALK_WORKSPACES_DIR")
    base = Path(configured or (Path.cwd() / "workspaces")).expanduser()
    if not base.is_absolute():
        base = Path.cwd() / base
    base = base.absolute()
    base.mkdir(parents=True, exist_ok=True)
    if base.is_symlink():
        raise InteractiveAgentRunScopeDenied("workspace root cannot be a symlink")
    agent_key = hashlib.sha256(agent_id.encode("utf-8")).hexdigest()[:32]
    session_key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    selected = base / "_agent_dialogues" / f"agent_{agent_key}" / f"session_{session_key}"
    if selected.exists() and selected.is_symlink():
        raise InteractiveAgentRunScopeDenied("derived Agent session workspace is a symlink")
    selected.mkdir(parents=True, exist_ok=True)
    selected = selected.resolve()
    try:
        selected.relative_to(base.resolve())
    except ValueError as exc:
        raise InteractiveAgentRunScopeDenied(
            "derived Agent session workspace escaped the server workspace root"
        ) from exc
    if selected.is_symlink():
        raise InteractiveAgentRunScopeDenied("derived Agent session workspace is a symlink")
    provider_cache = selected / ".aoitalk-provider-cache"
    provider_cache.mkdir(parents=True, exist_ok=True)
    if provider_cache.is_symlink():
        raise InteractiveAgentRunScopeDenied("provider cache root cannot be a symlink")
    return selected, provider_cache.resolve()


def _interactive_limit(
    policies: tuple[Mapping[str, Any], ...],
    names: tuple[str, ...],
    *,
    default: int | float,
    maximum: int | float,
    integer: bool = False,
) -> int | float:
    candidate: Any = None
    for policy in policies:
        for name in names:
            if name in policy and policy.get(name) is not None:
                candidate = policy.get(name)
                break
        if candidate is not None:
            break
    try:
        parsed = int(candidate) if integer else float(candidate)
    except (TypeError, ValueError, OverflowError):
        parsed = int(default) if integer else float(default)
    if integer:
        return max(1, min(int(parsed), int(maximum)))
    if not math.isfinite(float(parsed)) or float(parsed) <= 0:
        return float(default)
    return min(float(parsed), float(maximum))


def _interactive_resource_limits(
    *,
    revision: Any,
    run: Any,
    config: Any | None,
) -> ResourceLimits:
    """Derive finite runtime limits from server-owned revision/run policy."""

    revision_policy = _interactive_mapping(
        getattr(revision, "budget_policy_json", None)
    )
    run_metadata = _interactive_mapping(getattr(run, "run_metadata", None))
    dialogue = _interactive_mapping(run_metadata.get("agent_dialogue"))
    persisted_budget = _interactive_mapping(dialogue.get("budget"))
    configured = _interactive_mapping(
        _config_value(config, "agent_dialogue.resource_limits")
    )
    policies = (persisted_budget, revision_policy, configured)
    values = {
        "timeout_seconds": _interactive_limit(
            policies, ("timeout_seconds", "max_seconds", "max_runtime_seconds"),
            default=120, maximum=900,
        ),
        "max_processes": _interactive_limit(
            # ``RLIMIT_NPROC`` counts native runtime threads on Linux. Bun
            # needs more than a handful during HTTP client startup; keep the
            # finite trusted ceiling aligned with ResourceLimits' standard
            # Node/PowerShell-safe default instead of issuing ``ulimit -u 8``.
            policies, ("max_processes",), default=64, maximum=64, integer=True,
        ),
        "max_output_bytes": _interactive_limit(
            policies, ("max_output_bytes",), default=128 * 1024,
            maximum=4 * 1024 * 1024, integer=True,
        ),
        "max_disk_bytes": _interactive_limit(
            policies, ("max_disk_bytes",), default=256 * 1024 * 1024,
            maximum=2 * 1024 * 1024 * 1024, integer=True,
        ),
        "max_cpu_seconds": _interactive_limit(
            policies, ("max_cpu_seconds",), default=120, maximum=900,
        ),
        "max_memory_bytes": _interactive_limit(
            policies, ("max_memory_bytes",), default=2 * 1024**3,
            maximum=16 * 1024**3, integer=True,
        ),
        "max_background_seconds": _interactive_limit(
            policies, ("max_background_seconds",), default=120, maximum=900,
        ),
    }
    return ResourceLimits.from_values(values)


@dataclass(frozen=True, slots=True)
class InteractiveAgentRunScope:
    """Server-issued proof for one interactive Agent provider turn.

    The private lease token is retained only for same-process renewal/fencing;
    ``to_metadata`` deliberately omits it.  Provider code receives the
    immutable scopes and roots, never client supplied path or capability data.
    """

    scope: AgentRunScope
    execution_scope: HarnessExecutionScope
    run_id: str
    work_item_id: str
    session_id: str
    owner_id: str
    agent_id: str
    agent_revision_id: str
    binding_id: str
    binding_snapshot_hash: str
    execution_manifest_hash: str
    capability_manifest_digest: str
    authority_digest: str
    lease_owner: str
    lease_expires_at: datetime
    resource_limits: ResourceLimits
    workspace_root: Path
    provider_cache_root: Path
    readonly: bool
    native_allowed: bool
    _lease_token: str = field(repr=False, compare=False)

    @property
    def run_scope(self) -> AgentRunScope:
        return self.scope

    @property
    def harness_scope(self) -> HarnessExecutionScope:
        return self.execution_scope

    @property
    def lease_token(self) -> str:
        """Internal renewal credential; callers must not serialize it."""

        return self._lease_token

    def assert_run(self, run_id: Any, *, session_id: Any | None = None) -> None:
        if not _interactive_id_equal(run_id, self.run_id):
            raise InteractiveAgentRunScopeDenied("interactive AgentRun scope mismatch")
        if session_id is not None and not _interactive_id_equal(session_id, self.session_id):
            raise InteractiveAgentRunScopeDenied("interactive session scope mismatch")

    def assert_lease(self, *, now: datetime | None = None) -> None:
        if not self.lease_owner or not self._lease_token:
            raise InteractiveAgentRunScopeDenied("interactive Work lease proof is incomplete")
        current = (now or _interactive_now()).astimezone(timezone.utc)
        if self.lease_expires_at.astimezone(timezone.utc) <= current:
            raise InteractiveAgentRunScopeDenied("interactive Work lease has expired")

    def to_metadata(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "work_item_id": self.work_item_id,
            "session_id": self.session_id,
            "owner_id": self.owner_id,
            "agent_id": self.agent_id,
            "agent_revision_id": self.agent_revision_id,
            "binding_id": self.binding_id,
            "binding_snapshot_hash": self.binding_snapshot_hash,
            "execution_manifest_hash": self.execution_manifest_hash,
            "capability_manifest_digest": self.capability_manifest_digest,
            "authority_digest": self.authority_digest,
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at.isoformat(),
            "readonly": self.readonly,
            "native_allowed": self.native_allowed,
            "workspace_root": str(self.workspace_root),
            "provider_cache_root": str(self.provider_cache_root),
            "resource_limits": self.resource_limits.to_dict(),
            "scope": self.scope.to_dict(),
            "execution_scope": self.execution_scope.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ParentRunScopeCapability:
    """Opaque capability proving that a parent created the run context.

    ``_token`` is intentionally private and is only populated by the factory
    below.  The object is carried through the trusted server-side project
    context; model text can contain a path or a dict with the same fields but
    cannot manufacture this capability.
    """

    parent_run_id: str
    repository_identity: str
    canonical_root: Path
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        parent_run_id = str(self.parent_run_id or "").strip()
        identity = str(self.repository_identity or "").strip()
        if not parent_run_id or not identity:
            raise UntrustedParentScopeError(
                "parent_run_id and repository_identity are required"
            )
        object.__setattr__(self, "parent_run_id", parent_run_id)
        object.__setattr__(self, "repository_identity", identity)
        object.__setattr__(
            self,
            "canonical_root",
            Path(self.canonical_root).resolve(),
        )


_CAPABILITY_TOKEN = object()


@dataclass(frozen=True, slots=True)
class TrustedParentRunContext:
    """Immutable repository scope and Git baseline owned by one parent run."""

    parent_run_id: str
    scope: AgentRunScope
    snapshot: RunSnapshot
    capability: ParentRunScopeCapability
    execution_scope: HarnessExecutionScope | None = None
    # A Director parent may temporarily bind its immutable context while an
    # Operator/Agent-Team child run is active.  Child IDs are issued by that
    # parent (never by model/project text) and are therefore the only
    # additional run IDs accepted at the marker boundary.  The public parent
    # identity and the scope/snapshot run ID remain the root parent ID.
    _bound_child_run_ids: frozenset[str] = field(
        default_factory=frozenset,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        parent_run_id = str(self.parent_run_id or "").strip()
        if not parent_run_id:
            raise UntrustedParentScopeError("parent_run_id is required")
        if not isinstance(self.scope, AgentRunScope):
            raise TypeError("scope must be an AgentRunScope")
        if not isinstance(self.snapshot, RunSnapshot):
            raise TypeError("snapshot must be a RunSnapshot")
        if not isinstance(self.capability, ParentRunScopeCapability):
            raise TypeError("capability must be a ParentRunScopeCapability")
        if self.execution_scope is not None:
            if not isinstance(self.execution_scope, HarnessExecutionScope):
                raise TypeError("execution_scope must be a HarnessExecutionScope")
            expected = self.execution_scope.to_agent_run_scope()
            roots_match = all(
                {
                    os.path.normcase(str(Path(item)))
                    for item in getattr(expected, name, ()) or ()
                }
                == {
                    os.path.normcase(str(Path(item)))
                    for item in getattr(self.scope, name, ()) or ()
                }
                for name in (
                    "read_roots",
                    "write_roots",
                    "delete_roots",
                    "command_roots",
                    "scratch_roots",
                )
            )
            if (
                expected.run_id != self.scope.run_id
                or expected.repo_identity != self.scope.repo_identity
                or expected.canonical_root != self.scope.canonical_root
                or expected.workspace_access_level != self.scope.workspace_access_level
                or not roots_match
            ):
                raise ParentRunScopeMismatchError(
                    "HarnessExecutionScope does not match the parent AgentRunScope"
                )
        if self.capability._token is not _CAPABILITY_TOKEN:
            raise UntrustedParentScopeError("invalid parent scope capability")
        if self.scope.run_id != parent_run_id:
            raise ParentRunScopeMismatchError(
                "AgentRunScope run_id does not match the parent AgentRun"
            )
        if self.snapshot.run_id != parent_run_id:
            raise ParentRunScopeMismatchError(
                "RunSnapshot run_id does not match the parent AgentRun"
            )
        if self.scope.repo_identity != self.snapshot.repo_identity:
            raise ParentRunScopeMismatchError(
                "scope and snapshot repository identities differ"
            )
        if self.scope.canonical_root != self.snapshot.canonical_root:
            raise ParentRunScopeMismatchError(
                "scope and snapshot repository roots differ"
            )
        if self.capability.parent_run_id != parent_run_id:
            raise ParentRunScopeMismatchError(
                "capability parent_run_id does not match the context"
            )
        if self.capability.repository_identity != self.scope.repo_identity:
            raise ParentRunScopeMismatchError(
                "capability repository identity does not match the scope"
            )
        if self.capability.canonical_root != self.scope.canonical_root:
            raise ParentRunScopeMismatchError(
                "capability repository root does not match the scope"
            )
        object.__setattr__(self, "parent_run_id", parent_run_id)
        object.__setattr__(
            self,
            "_bound_child_run_ids",
            frozenset(
                str(run_id).strip()
                for run_id in self._bound_child_run_ids
                if str(run_id).strip() and str(run_id).strip() != parent_run_id
            ),
        )

    @property
    def run_scope(self) -> AgentRunScope:
        """Compatibility alias used by runtime/tool integrations."""

        return self.scope

    @property
    def run_snapshot(self) -> RunSnapshot:
        """Compatibility alias for the immutable Git baseline."""

        return self.snapshot

    def assert_matches_parent(self, parent_run_id: str | None) -> None:
        """Fail closed unless the ID is this parent or its issued child."""

        candidate = str(parent_run_id or "").strip()
        if not candidate or (
            candidate != self.parent_run_id
            and candidate not in self._bound_child_run_ids
        ):
            raise ParentRunScopeMismatchError(
                "trusted run scope belongs to a different parent AgentRun"
            )

    def with_child_run(self, child_run_id: str | None) -> "TrustedParentRunContext":
        """Return this context bound to one parent-issued child run ID.

        The returned context keeps the same immutable scope, Git baseline, and
        opaque capability.  Only the marker's run-lineage allow-list changes;
        callers cannot supply a child ID to the factory or derive one from
        model/project payloads.
        """

        clean_child_id = str(child_run_id or "").strip()
        if not clean_child_id:
            raise UntrustedParentScopeError("child_run_id is required")
        if clean_child_id == self.parent_run_id:
            return self
        if clean_child_id in self._bound_child_run_ids:
            return self
        return TrustedParentRunContext(
            parent_run_id=self.parent_run_id,
            scope=self.scope,
            snapshot=self.snapshot,
            capability=self.capability,
            execution_scope=self.execution_scope,
            _bound_child_run_ids=self._bound_child_run_ids | {clean_child_id},
        )

    bind_child_run = with_child_run

    def child_metadata(self, *, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Return JSON-safe baseline/scope metadata for a child AgentRun."""

        metadata: dict[str, Any] = {
            "trusted_parent_scope": True,
            "require_run_scope": True,
            "parent_run_id": self.parent_run_id,
            "repository_identity": self.scope.repo_identity,
            "canonical_repository_root": str(self.scope.canonical_root),
            "run_scope": self.scope.to_dict(),
            "agent_run_scope": self.scope.to_dict(),
            "run_snapshot": self.snapshot.to_dict(),
            "baseline_git_state": self.snapshot.baseline_git_state,
        }
        if extra:
            metadata.update(dict(extra))
        return metadata

    metadata_for_child = child_metadata
    as_child_metadata = child_metadata

    def inject_into_project_context(
        self,
        project_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Attach the immutable scope to trusted child runtime context.

        The context object itself is kept under a private key so model-facing
        prompt formatting cannot treat a path as an instruction.  The
        ``run_scope`` value is the already-constructed immutable object that
        ``SpecialistDelegationRunner`` consumes.
        """

        context = dict(project_context or {})
        context[TRUSTED_PARENT_CONTEXT_KEY] = self
        context[TRUSTED_PARENT_CAPABILITY_KEY] = self.capability
        context[RUN_SCOPE_CONTEXT_KEY] = self.scope
        if self.execution_scope is not None:
            context[HARNESS_EXECUTION_SCOPE_CONTEXT_KEY] = self.execution_scope
        context[REQUIRE_RUN_SCOPE_KEY] = True
        metadata = context.get("metadata")
        safe_metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        for key in (
            RUN_SCOPE_CONTEXT_KEY,
            REQUIRE_RUN_SCOPE_KEY,
            "agent_run_scope",
            "trusted_parent_scope",
            "parent_run_id",
            "canonical_repository_root",
            "repository_identity",
        ):
            safe_metadata.pop(key, None)
        safe_metadata.update(
            {
                "require_run_scope": True,
                "trusted_parent_scope": True,
                "parent_run_id": self.parent_run_id,
                "repository_identity": self.scope.repo_identity,
                "canonical_repository_root": str(self.scope.canonical_root),
            }
        )
        context["metadata"] = safe_metadata
        return context

    # Naming aliases make the boundary explicit for callers that describe the
    # operation as binding rather than injecting a project context.
    to_project_context = inject_into_project_context
    bind_project_context = inject_into_project_context


def create_trusted_parent_run_context(
    canonical_repository_root: str | os.PathLike[str],
    *,
    parent_run_id: str,
    repository_identity: str | None = None,
    workspace_access_level: str = "write",
    read_roots: Any = None,
    write_roots: Any = None,
    delete_roots: Any = None,
    command_roots: Any = None,
    scratch_roots: Any = (),
) -> TrustedParentRunContext:
    """Capture a parent run's canonical Git baseline and immutable scope.

    ``canonical_repository_root`` is an explicit parent-controller argument;
    this function intentionally has no model/task-text fallback.  The Git
    snapshot canonicalises the selected checkout and captures pre-existing
    modified/staged/untracked paths before any child is started.
    """

    clean_parent_id = str(parent_run_id or "").strip()
    if not clean_parent_id:
        raise UntrustedParentScopeError("parent_run_id is required")
    if not canonical_repository_root:
        raise UntrustedParentScopeError("canonical_repository_root is required")
    supplied_root = Path(os.fspath(canonical_repository_root))
    if supplied_root.exists() and not supplied_root.is_dir():
        raise UntrustedParentScopeError(
            "canonical_repository_root must point to a repository directory"
        )

    snapshot = RunSnapshot.capture(
        canonical_repository_root,
        repository_identity=repository_identity,
        run_id=clean_parent_id,
    )
    scope = AgentRunScope.for_repository(
        snapshot.canonical_root,
        repository_identity=snapshot.repo_identity,
        run_id=clean_parent_id,
        baseline_revision=snapshot.baseline_revision,
        baseline_git_state=snapshot.baseline_git_state,
        workspace_access_level=workspace_access_level,  # type: ignore[arg-type]
        read_roots=read_roots,
        write_roots=write_roots,
        delete_roots=delete_roots,
        command_roots=command_roots,
        scratch_roots=scratch_roots,
    )
    capability = ParentRunScopeCapability(
        parent_run_id=clean_parent_id,
        repository_identity=scope.repo_identity,
        canonical_root=scope.canonical_root,
        _token=_CAPABILITY_TOKEN,
    )
    execution_scope = None
    try:
        from ..features import Features

        enterprise = bool(Features.is_enterprise())
    except Exception:
        enterprise = True
    if enterprise:
        # This is an autonomous repository controller, not a chat principal.
        # Preserve the parent-issued lower scope exactly while adding the
        # finite Enterprise authority layer required by every process lane.
        execution_scope = HarnessExecutionScope._issue(
            user_id="agent-parent-system",
            organization_id=None,
            project_ids=(),
            app_ids=(),
            read_roots=scope.read_roots,
            write_roots=scope.write_roots,
            delete_roots=scope.delete_roots,
            command_roots=scope.command_roots,
            scratch_roots=scope.scratch_roots,
            external_read_grants=(),
            network_capability=NetworkCapability.NONE,
            network_allowlist=(),
            environment_capability_ids=(),
            secret_capability_ids=(),
            resource_limits=ResourceLimits(),
            run_id=clean_parent_id,
            audit_id=f"agent-parent:{clean_parent_id}",
            root_identity=scope.repo_identity,
        )
    return TrustedParentRunContext(
        parent_run_id=clean_parent_id,
        scope=scope,
        snapshot=snapshot,
        capability=capability,
        execution_scope=execution_scope,
    )


_EXPLICIT_REPOSITORY_ROOT_CONFIG_KEYS = (
    "agent_operator.repository_root",
    "agent_operator.canonical_repository_root",
)
_EXPLICIT_REPOSITORY_ROOT_ENV_KEY = "AOITALK_OPERATOR_REPOSITORY_ROOT"


def _config_value(config: Any, key: str) -> Any:
    """Read one explicit config key without consulting project/model data."""

    if config is None:
        return None
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, None)
        except TypeError:
            value = getter(key)
        if value is not None:
            return value
    raw = config.get("config") if isinstance(config, Mapping) else getattr(config, "config", None)
    if raw is not None and raw is not config:
        value = _config_value(raw, key)
        if value is not None:
            return value
    if not isinstance(config, Mapping):
        return None
    current: Any = config
    for part in key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def resolve_explicit_repository_root(config: Any = None) -> str | os.PathLike[str] | None:
    """Resolve the parent-only repository root setting.

    Only the dedicated ``agent_operator.repository_root`` (or its canonical
    spelling) and ``AOITALK_OPERATOR_REPOSITORY_ROOT`` are accepted.  In
    particular, Project Context ``workspace_root`` and model/task text are not
    consulted here.
    """

    for key in _EXPLICIT_REPOSITORY_ROOT_CONFIG_KEYS:
        value = _config_value(config, key)
        if isinstance(value, (str, os.PathLike)) and str(value).strip():
            return value
    env_value = os.environ.get(_EXPLICIT_REPOSITORY_ROOT_ENV_KEY)
    if env_value and env_value.strip():
        return env_value.strip()
    return None


async def issue_interactive_agent_run_scope(
    *,
    run_id: str,
    work_item_id: str,
    session_id: str,
    agent_id: str,
    agent_revision_id: str,
    work_claim: Any | None = None,
    binding_id: str | None = None,
    binding_snapshot_hash: str | None = None,
    execution_manifest_hash: str | None = None,
    owner_id: str | None = None,
    project_id: str | None = None,
    provider_id: str | None = None,
    model_id: str | None = None,
    workspace_root: str | os.PathLike[str] | None = None,
    workspace_path: str | os.PathLike[str] | None = None,
    db_manager: Any | None = None,
    db_session: Any | None = None,
    config: Any | None = None,
    authority_resolver: Any | None = None,
) -> InteractiveAgentRunScope:
    """Issue a fresh, narrow native proof after interactive Work admission.

    Every identity and lease value is read again from the database.  The
    caller may supply a WorkClaim only as a consistency check; its token and
    the filesystem path are never promoted to authority.  A provider can use
    the returned scope only for this exact run and session.
    """

    selected_run_id = _interactive_id(run_id, label="run_id")
    selected_work_id = _interactive_id(work_item_id, label="work_item_id")
    selected_session_id = _interactive_id(session_id, label="session_id")
    selected_agent_id = _interactive_id(agent_id, label="agent_id")
    selected_revision_id = _interactive_id(agent_revision_id, label="agent_revision_id")

    own_session = db_session is None
    db = db_session
    if db is None:
        try:
            from ..memory.database import get_database_manager

            manager = db_manager or get_database_manager()
            value = manager.get_session()
            db = await value if inspect.isawaitable(value) else value
        except Exception as exc:
            raise InteractiveAgentRunScopeDenied(
                "interactive Agent scope database is unavailable"
            ) from exc

    async def _get(model: Any, value: Any) -> Any:
        key = _interactive_uuid_or_text(value)
        getter = getattr(db, "get", None)
        if not callable(getter):
            raise InteractiveAgentRunScopeDenied("interactive Agent scope database cannot read rows")
        result = getter(model, key)
        return await result if inspect.isawaitable(result) else result

    try:
        from ..memory.models import (
            Agent,
            AgentExecutionBinding,
            AgentRevision,
            AgentRun,
            AgentWorkItem,
            ConversationSession,
        )

        run = await _get(AgentRun, selected_run_id)
        work = await _get(AgentWorkItem, selected_work_id)
        conversation = await _get(ConversationSession, selected_session_id)
        agent = await _get(Agent, selected_agent_id)
        revision = await _get(AgentRevision, selected_revision_id)
        if run is None or work is None or conversation is None or agent is None or revision is None:
            raise InteractiveAgentRunScopeDenied("interactive Agent authority row is unavailable")

        if not _interactive_id_equal(getattr(run, "id", None), selected_run_id):
            raise InteractiveAgentRunScopeDenied("AgentRun identity mismatch")
        if str(getattr(run, "run_type", "") or "") != "chat_turn":
            raise InteractiveAgentRunScopeDenied("interactive scope requires a Chat AgentRun")
        if str(getattr(run, "status", "") or "").casefold() not in _INTERACTIVE_ACTIVE_RUN_STATES:
            raise InteractiveAgentRunScopeDenied("AgentRun is no longer active")
        for label, observed, expected in (
            ("run.session_id", getattr(run, "session_id", None), selected_session_id),
            ("run.agent_id", getattr(run, "agent_id", None), selected_agent_id),
            ("run.agent_revision_id", getattr(run, "agent_revision_id", None), selected_revision_id),
            ("run.work_item_id", getattr(run, "work_item_id", None), selected_work_id),
        ):
            if not _interactive_id_equal(observed, expected):
                raise InteractiveAgentRunScopeDenied(f"{label} binding mismatch")

        if str(getattr(work, "state", "") or "").casefold() != _INTERACTIVE_WORK_STATE:
            raise InteractiveAgentRunScopeDenied("interactive Work lease is not running")
        for label, observed, expected in (
            ("work.id", getattr(work, "id", None), selected_work_id),
            ("work.active_agent_run_id", getattr(work, "active_agent_run_id", None), selected_run_id),
            ("work.conversation_session_id", getattr(work, "conversation_session_id", None), selected_session_id),
            ("work.assigned_agent_id", getattr(work, "assigned_agent_id", None), selected_agent_id),
            ("work.agent_revision_id", getattr(work, "agent_revision_id", None), selected_revision_id),
        ):
            if not _interactive_id_equal(observed, expected):
                raise InteractiveAgentRunScopeDenied(f"{label} binding mismatch")
        if str(getattr(work, "source_type", "") or "") != _INTERACTIVE_WORK_SOURCE:
            raise InteractiveAgentRunScopeDenied("interactive Work source is not Chat")
        if str(getattr(work, "execution_adapter", "") or "") != _INTERACTIVE_WORK_ADAPTER:
            raise InteractiveAgentRunScopeDenied("interactive Work adapter is not approved")

        lease_owner = _interactive_id(getattr(work, "lease_owner", None), label="lease_owner")
        lease_token = _interactive_id(getattr(work, "lease_token", None), label="lease_token")
        lease_expires_at = _interactive_utc(getattr(work, "lease_expires_at", None))
        if lease_expires_at is None or lease_expires_at <= _interactive_now():
            raise InteractiveAgentRunScopeDenied("interactive Work lease is expired")

        def _claim_value(name: str) -> Any:
            if isinstance(work_claim, Mapping):
                return work_claim.get(name)
            return getattr(work_claim, name, None) if work_claim is not None else None

        claim_work_id = _claim_value("work_item_id")
        claim_owner = _claim_value("lease_owner")
        claim_token = _claim_value("lease_token")
        if claim_work_id and not _interactive_id_equal(claim_work_id, selected_work_id):
            raise InteractiveAgentRunScopeDenied("WorkClaim identity mismatch")
        if claim_owner and str(claim_owner) != lease_owner:
            raise InteractiveAgentRunScopeDenied("WorkClaim lease owner changed")
        if claim_token and str(claim_token) != lease_token:
            raise InteractiveAgentRunScopeDenied("WorkClaim lease token changed")

        if getattr(conversation, "deleted_at", None) is not None:
            raise InteractiveAgentRunScopeDenied("conversation session is deleted")
        if not _interactive_id_equal(getattr(conversation, "id", None), selected_session_id):
            raise InteractiveAgentRunScopeDenied("conversation session identity mismatch")
        conversation_agent_id = getattr(conversation, "agent_id", None)
        if not _interactive_id_equal(conversation_agent_id, selected_agent_id):
            raise InteractiveAgentRunScopeDenied("conversation Agent binding changed")
        persisted_owner = _interactive_id(getattr(conversation, "user_id", None), label="conversation owner")
        run_owner = _interactive_id(getattr(run, "user_id", None), label="run owner")
        if not _interactive_id_equal(persisted_owner, run_owner):
            raise InteractiveAgentRunScopeDenied("run and conversation owner differ")
        selected_owner = _interactive_id(owner_id or run_owner, label="owner_id")
        if not _interactive_id_equal(selected_owner, persisted_owner):
            raise InteractiveAgentRunScopeDenied("interactive owner mismatch")

        if str(getattr(agent, "state", "") or "").casefold() != "active":
            raise InteractiveAgentRunScopeDenied("Agent is no longer active")
        if not _interactive_id_equal(getattr(agent, "id", None), selected_agent_id):
            raise InteractiveAgentRunScopeDenied("Agent identity mismatch")
        if not _interactive_id_equal(getattr(revision, "id", None), selected_revision_id):
            raise InteractiveAgentRunScopeDenied("AgentRevision identity mismatch")
        if not _interactive_id_equal(getattr(revision, "agent_id", None), selected_agent_id):
            raise InteractiveAgentRunScopeDenied("AgentRevision is not bound to Agent")

        run_metadata = _interactive_mapping(getattr(run, "run_metadata", None))
        binding_metadata = _interactive_mapping(run_metadata.get("agent_binding"))
        persisted_binding_id = _interactive_id(
            binding_metadata.get("binding_id"), label="binding_id"
        )
        persisted_binding_hash = _interactive_id(
            binding_metadata.get("snapshot_hash"),
            label="binding_snapshot_hash",
        )
        if binding_id not in (None, "") and not _interactive_id_equal(
            binding_id, persisted_binding_id
        ):
            raise InteractiveAgentRunScopeDenied("requested binding_id is not the persisted run binding")
        if binding_snapshot_hash not in (None, "") and str(binding_snapshot_hash) != persisted_binding_hash:
            raise InteractiveAgentRunScopeDenied(
                "requested binding snapshot is not the persisted run binding"
            )
        selected_binding_id = persisted_binding_id
        selected_binding_hash = persisted_binding_hash
        binding = await _get(AgentExecutionBinding, selected_binding_id)
        if binding is None:
            raise InteractiveAgentRunScopeDenied("Agent execution binding is unavailable")
        for label, observed, expected in (
            ("binding.session_id", getattr(binding, "session_id", None), selected_session_id),
            ("binding.agent_id", getattr(binding, "agent_id", None), selected_agent_id),
            ("binding.agent_revision_id", getattr(binding, "agent_revision_id", None), selected_revision_id),
        ):
            if not _interactive_id_equal(observed, expected):
                raise InteractiveAgentRunScopeDenied(f"{label} mismatch")
        actual_binding_hash = _interactive_id(getattr(binding, "snapshot_hash", None), label="binding.snapshot_hash")
        if actual_binding_hash != selected_binding_hash:
            raise InteractiveAgentRunScopeDenied("Agent execution binding snapshot changed")

        manifest = getattr(run, "resolved_execution_manifest", None)
        persisted_model = str(
            getattr(run, "model", None)
            or getattr(run, "model_id", None)
            or (
                manifest.get("model")
                if isinstance(manifest, Mapping)
                else ""
            )
            or (
                manifest.get("model_id")
                if isinstance(manifest, Mapping)
                else ""
            )
            or ""
        ).strip()
        requested_model = str(model_id or "").strip()
        if (
            persisted_model
            and requested_model
            and persisted_model.casefold() != requested_model.casefold()
        ):
            raise InteractiveAgentRunScopeDenied(
                "requested native model does not match the persisted AgentRun model"
            )
        persisted_manifest_hash = _interactive_id(
            getattr(run, "execution_manifest_hash", None),
            label="execution_manifest_hash",
        )
        if execution_manifest_hash not in (None, "") and str(execution_manifest_hash).casefold() != persisted_manifest_hash.casefold():
            raise InteractiveAgentRunScopeDenied(
                "requested execution manifest is not the persisted run manifest"
            )
        stored_manifest_hash = persisted_manifest_hash
        if not _HEX64_RE.fullmatch(stored_manifest_hash):
            raise InteractiveAgentRunScopeDenied("execution manifest digest is invalid")
        if isinstance(manifest, Mapping):
            computed_manifest_hash = _interactive_hash(manifest)
            if computed_manifest_hash.casefold() != stored_manifest_hash.casefold():
                raise InteractiveAgentRunScopeDenied("execution manifest digest changed")
            for key, expected in (
                ("agent_id", selected_agent_id),
                ("agent_revision_id", selected_revision_id),
            ):
                if manifest.get(key) not in (None, "") and not _interactive_id_equal(manifest.get(key), expected):
                    raise InteractiveAgentRunScopeDenied(f"execution manifest {key} mismatch")

        run_project_id = str(getattr(run, "project_id", None) or "").strip() or None
        work_project_id = str(getattr(work, "project_id", None) or "").strip() or None
        if run_project_id and work_project_id and not _interactive_id_equal(
            run_project_id, work_project_id
        ):
            raise InteractiveAgentRunScopeDenied("run and Work project scope differ")
        selected_project_id = project_id or run_project_id or work_project_id
        selected_project_id = str(selected_project_id).strip() if selected_project_id else None
        for project_value in (run_project_id, work_project_id):
            if project_value and selected_project_id and not _interactive_id_equal(
                project_value, selected_project_id
            ):
                raise InteractiveAgentRunScopeDenied("interactive project scope mismatch")
        declared_capabilities = tuple(
            sorted(
                {
                    str(item).strip()
                    for item in (getattr(binding, "capability_ceiling_json", None) or [])
                    if str(item).strip()
                }
            )
        )
        if "workspace_read" not in declared_capabilities:
            raise InteractiveAgentRunScopeDenied("AgentRevision has no workspace_read capability")

        if authority_resolver is None:
            try:
                from .agent_authority import AgentAuthorityResolver

                authority_resolver = AgentAuthorityResolver(
                    db_manager=db_manager,
                    config=config,
                )
            except Exception as exc:
                raise InteractiveAgentRunScopeDenied("Agent authority resolver is unavailable") from exc

        resolve = getattr(authority_resolver, "resolve", None)
        if not callable(resolve):
            raise InteractiveAgentRunScopeDenied("Agent authority resolver is invalid")
        read_decision = resolve(
            agent_id=selected_agent_id,
            revision_id=selected_revision_id,
            project_id=selected_project_id,
            required_capability="workspace_read",
        )
        read_decision = await read_decision if inspect.isawaitable(read_decision) else read_decision
        if read_decision is None or not bool(getattr(read_decision, "allowed", False)):
            raise InteractiveAgentRunScopeDenied("current Agent authority denies workspace_read")
        write_decision = resolve(
            agent_id=selected_agent_id,
            revision_id=selected_revision_id,
            project_id=selected_project_id,
            required_capability="workspace_write",
        )
        write_decision = await write_decision if inspect.isawaitable(write_decision) else write_decision
        writable = (
            "workspace_write" in declared_capabilities
            and write_decision is not None
            and bool(getattr(write_decision, "allowed", False))
        )
        effective_capabilities = tuple(
            sorted(
                str(item)
                for item in (
                    getattr(read_decision, "capabilities", frozenset()) or frozenset()
                )
            )
        )
        authority_digest = _interactive_hash(
            {
                "agent_id": selected_agent_id,
                "agent_revision_id": selected_revision_id,
                "project_id": selected_project_id,
                "capabilities": effective_capabilities,
                "workspace_write": writable,
            }
        )
        capability_manifest_digest = _interactive_hash(
            {
                "execution_manifest_hash": stored_manifest_hash.lower(),
                "binding_snapshot_hash": actual_binding_hash,
                "declared_capabilities": declared_capabilities,
                "authority_digest": authority_digest,
            }
        )

        selected_workspace, provider_cache = _interactive_safe_workspace_root(
            workspace_root,
            agent_id=selected_agent_id,
            session_id=selected_session_id,
        )
        if workspace_path is not None:
            supplied_workspace = Path(os.fspath(workspace_path)).expanduser().absolute().resolve()
            if supplied_workspace != selected_workspace:
                raise InteractiveAgentRunScopeDenied(
                    "workspace_path is not the server-derived Agent session root"
                )
        resource_limits = _interactive_resource_limits(
            revision=revision,
            run=run,
            config=config,
        )
        scope_access = "write" if writable else "read"
        root_identity = "agent-session:" + hashlib.sha256(
            str(selected_workspace).encode("utf-8", "surrogatepass")
        ).hexdigest()[:32]
        execution_scope = HarnessExecutionScope._issue(
            user_id=f"agent:{selected_agent_id}",
            organization_id=None,
            project_ids=(),
            app_ids=(),
            read_roots=(selected_workspace, provider_cache),
            write_roots=(selected_workspace, provider_cache) if writable else (),
            delete_roots=(selected_workspace, provider_cache) if writable else (),
            command_roots=(selected_workspace, provider_cache),
            scratch_roots=(provider_cache,),
            external_read_grants=(),
            network_capability=NetworkCapability.NONE,
            network_allowlist=(),
            environment_capability_ids=(),
            secret_capability_ids=(),
            resource_limits=resource_limits,
            run_id=selected_run_id,
            audit_id=f"agent-dialogue:{selected_run_id}",
            root_identity=root_identity,
        )
        scope = execution_scope.to_agent_run_scope()
        if scope.workspace_access_level != scope_access:
            raise InteractiveAgentRunScopeDenied("issued scope access level is inconsistent")
        def _canonical_provider(value: Any) -> str:
            normalized = str(value or "").strip().casefold()
            return {
                "claude": "claude-cli",
                "claude-code": "claude-cli",
                "codex": "codex-cli",
                "codex-code": "codex-cli",
            }.get(normalized, normalized)

        persisted_provider = _canonical_provider(getattr(run, "provider", ""))
        requested_provider = _canonical_provider(provider_id)
        if requested_provider and requested_provider != persisted_provider:
            raise InteractiveAgentRunScopeDenied(
                "requested native provider does not match the persisted AgentRun provider"
            )
        provider = persisted_provider
        native_allowed = provider.endswith("-cli") or provider in {
            "claude-cli",
            "codex-cli",
        }
        result = InteractiveAgentRunScope(
            scope=scope,
            execution_scope=execution_scope,
            run_id=selected_run_id,
            work_item_id=selected_work_id,
            session_id=selected_session_id,
            owner_id=selected_owner,
            agent_id=selected_agent_id,
            agent_revision_id=selected_revision_id,
            binding_id=selected_binding_id,
            binding_snapshot_hash=actual_binding_hash,
            execution_manifest_hash=stored_manifest_hash.lower(),
            capability_manifest_digest=capability_manifest_digest,
            authority_digest=authority_digest,
            lease_owner=lease_owner,
            lease_expires_at=lease_expires_at,
            resource_limits=resource_limits,
            workspace_root=selected_workspace,
            provider_cache_root=provider_cache,
            readonly=not writable,
            native_allowed=native_allowed,
            _lease_token=lease_token,
        )
        result.assert_lease()
        return result
    finally:
        if own_session and db is not None:
            close = getattr(db, "close", None)
            if callable(close):
                value = close()
                if inspect.isawaitable(value):
                    await value


async def revalidate_interactive_agent_run_scope(
    proof: InteractiveAgentRunScope,
    *,
    provider_id: str,
    model_id: str,
    workspace_path: str | os.PathLike[str],
    project_id: str | None = None,
    workspace_root: str | os.PathLike[str] | None = None,
    db_manager: Any | None = None,
    db_session: Any | None = None,
    config: Any | None = None,
    authority_resolver: Any | None = None,
) -> InteractiveAgentRunScope:
    """Re-read durable Agent authority immediately before a native launch."""

    if not isinstance(proof, InteractiveAgentRunScope):
        raise InteractiveAgentRunScopeDenied("native launch proof is invalid")
    proof.assert_lease()
    fresh = await issue_interactive_agent_run_scope(
        run_id=proof.run_id,
        work_item_id=proof.work_item_id,
        session_id=proof.session_id,
        agent_id=proof.agent_id,
        agent_revision_id=proof.agent_revision_id,
        work_claim={
            "work_item_id": proof.work_item_id,
            "lease_owner": proof.lease_owner,
            "lease_token": proof.lease_token,
        },
        binding_id=proof.binding_id,
        binding_snapshot_hash=proof.binding_snapshot_hash,
        execution_manifest_hash=proof.execution_manifest_hash,
        owner_id=proof.owner_id,
        project_id=project_id,
        provider_id=provider_id,
        model_id=model_id,
        workspace_root=workspace_root,
        workspace_path=workspace_path,
        db_manager=db_manager,
        db_session=db_session,
        config=config,
        authority_resolver=authority_resolver,
    )
    for label, before, after in (
        ("run_id", proof.run_id, fresh.run_id),
        ("work_item_id", proof.work_item_id, fresh.work_item_id),
        ("session_id", proof.session_id, fresh.session_id),
        ("agent_id", proof.agent_id, fresh.agent_id),
        ("agent_revision_id", proof.agent_revision_id, fresh.agent_revision_id),
        ("binding_id", proof.binding_id, fresh.binding_id),
        ("binding_snapshot_hash", proof.binding_snapshot_hash, fresh.binding_snapshot_hash),
        ("execution_manifest_hash", proof.execution_manifest_hash, fresh.execution_manifest_hash),
        ("capability_manifest_digest", proof.capability_manifest_digest, fresh.capability_manifest_digest),
        ("authority_digest", proof.authority_digest, fresh.authority_digest),
        ("lease_owner", proof.lease_owner, fresh.lease_owner),
    ):
        if not _interactive_id_equal(before, after):
            raise InteractiveAgentRunScopeDenied(f"native launch proof changed: {label}")
    if (
        fresh.scope.canonical_root != proof.scope.canonical_root
        or fresh.provider_cache_root != proof.provider_cache_root
        or fresh.readonly != proof.readonly
        or not fresh.native_allowed
    ):
        raise InteractiveAgentRunScopeDenied("native launch workspace proof changed")
    return fresh


def create_parent_run_context_from_config(
    config: Any,
    *,
    parent_run_id: str | None,
    **kwargs: Any,
) -> TrustedParentRunContext | None:
    """Create one trusted parent context from an explicit runtime setting.

    ``None`` means the runtime has no configured repository authority; callers
    should keep ordinary Director reads available while write-capable workers
    fail closed.  When a root is configured, ``create_trusted_parent_run_context``
    canonicalises and validates that it is a Git checkout and captures the
    immutable baseline exactly once for this parent run.
    """

    clean_parent_id = str(parent_run_id or "").strip()
    root = resolve_explicit_repository_root(config)
    if not clean_parent_id or root is None:
        return None
    return create_trusted_parent_run_context(
        root,
        parent_run_id=clean_parent_id,
        **kwargs,
    )


def resolve_trusted_parent_run_context(
    candidate: Any,
    *,
    parent_run_id: str | None,
) -> TrustedParentRunContext | None:
    """Resolve a trusted marker; reject raw model/project path payloads.

    Returning ``None`` is intentional for ordinary chat and legacy callers.
    A coding worker that requires a scope must treat that absence as a hard
    denial instead of constructing a scope from ``workspace_root`` or task
    text.
    """

    if isinstance(candidate, Mapping):
        candidate = candidate.get(TRUSTED_PARENT_CONTEXT_KEY)
    if not isinstance(candidate, TrustedParentRunContext):
        return None
    try:
        candidate.assert_matches_parent(parent_run_id)
    except ParentRunScopeMismatchError:
        return None
    return candidate


def inject_trusted_parent_scope(
    project_context: Mapping[str, Any] | None,
    trusted_context: TrustedParentRunContext | None,
    *,
    parent_run_id: str | None = None,
) -> dict[str, Any]:
    """Inject only a validated trusted context, never a raw path/dict."""

    if trusted_context is None:
        return dict(project_context or {})
    # Direct parent callers may already hold the opaque capability and omit
    # the duplicate run id; runtime tool wiring always supplies the active
    # AgentRun id so cross-run reuse is still rejected at that boundary.
    trusted_context.assert_matches_parent(
        trusted_context.parent_run_id if parent_run_id is None else parent_run_id
    )
    return trusted_context.inject_into_project_context(project_context)


class AgentRunScopeService:
    """Small service facade used by parent coordinators and tests."""

    create_parent_run_context = staticmethod(create_trusted_parent_run_context)
    create_parent_scope = staticmethod(create_trusted_parent_run_context)
    capture = staticmethod(create_trusted_parent_run_context)
    build = staticmethod(create_trusted_parent_run_context)
    create_parent_run_context_from_config = staticmethod(
        create_parent_run_context_from_config
    )
    resolve_explicit_repository_root = staticmethod(resolve_explicit_repository_root)
    resolve_parent_run_context = staticmethod(resolve_trusted_parent_run_context)
    inject_parent_scope = staticmethod(inject_trusted_parent_scope)
    issue_interactive_agent_run_scope = staticmethod(issue_interactive_agent_run_scope)
    issue_interactive_scope = staticmethod(issue_interactive_agent_run_scope)


# Concise aliases for integrations that import a factory by a shorter name.
create_parent_run_scope = create_trusted_parent_run_context
create_agent_run_scope = create_trusted_parent_run_context
build_trusted_parent_run_context = create_trusted_parent_run_context
capture_parent_run_scope = create_trusted_parent_run_context
create_parent_scope_from_config = create_parent_run_context_from_config
issue_agent_chat_run_scope = issue_interactive_agent_run_scope
issue_interactive_scope = issue_interactive_agent_run_scope
issue_agent_native_run_scope = issue_interactive_agent_run_scope
AgentNativeRunScope = InteractiveAgentRunScope


__all__ = [
    "AgentRunScopeService",
    "AgentRunScopeServiceError",
    "AgentNativeRunScope",
    "InteractiveAgentRunScope",
    "InteractiveAgentRunScopeDenied",
    "ParentRunScopeCapability",
    "ParentRunScopeMismatchError",
    "RUN_SCOPE_CONTEXT_KEY",
    "REQUIRE_RUN_SCOPE_KEY",
    "HARNESS_EXECUTION_SCOPE_CONTEXT_KEY",
    "TRUSTED_PARENT_CAPABILITY_KEY",
    "TRUSTED_PARENT_CONTEXT_KEY",
    "TrustedParentRunContext",
    "UntrustedParentScopeError",
    "create_agent_run_scope",
    "create_parent_run_context_from_config",
    "create_parent_scope_from_config",
    "build_trusted_parent_run_context",
    "capture_parent_run_scope",
    "create_parent_run_scope",
    "create_trusted_parent_run_context",
    "inject_trusted_parent_scope",
    "issue_agent_chat_run_scope",
    "issue_interactive_agent_run_scope",
    "issue_interactive_scope",
    "issue_agent_native_run_scope",
    "resolve_explicit_repository_root",
    "resolve_trusted_parent_run_context",
]
