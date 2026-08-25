"""Parent-owned construction and propagation of coding-run scope.

The filesystem path policy itself lives in :mod:`src.security.agent_run_scope`.
This module owns the *trust boundary* around that policy: only a parent
controller that supplies an explicit repository root may construct a scope and
attach it to a child Agent Team turn.  A string in a model/project payload is
never promoted to a mutation authority.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..security.agent_run_scope import AgentRunScope
from ..security.git_publication_gate import RunSnapshot


TRUSTED_PARENT_CONTEXT_KEY = "_trusted_agent_run_context"
TRUSTED_PARENT_CAPABILITY_KEY = "_trusted_agent_run_capability"
RUN_SCOPE_CONTEXT_KEY = "run_scope"
REQUIRE_RUN_SCOPE_KEY = "require_run_scope"


class AgentRunScopeServiceError(RuntimeError):
    """Base error for parent scope construction/propagation."""


class UntrustedParentScopeError(AgentRunScopeServiceError):
    """Raised when a caller tries to promote untrusted data into a scope."""


class ParentRunScopeMismatchError(UntrustedParentScopeError):
    """Raised when a scope is reused by another parent run or repository."""


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
    return TrustedParentRunContext(
        parent_run_id=clean_parent_id,
        scope=scope,
        snapshot=snapshot,
        capability=capability,
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


# Concise aliases for integrations that import a factory by a shorter name.
create_parent_run_scope = create_trusted_parent_run_context
create_agent_run_scope = create_trusted_parent_run_context
build_trusted_parent_run_context = create_trusted_parent_run_context
capture_parent_run_scope = create_trusted_parent_run_context
create_parent_scope_from_config = create_parent_run_context_from_config


__all__ = [
    "AgentRunScopeService",
    "AgentRunScopeServiceError",
    "ParentRunScopeCapability",
    "ParentRunScopeMismatchError",
    "RUN_SCOPE_CONTEXT_KEY",
    "REQUIRE_RUN_SCOPE_KEY",
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
    "resolve_explicit_repository_root",
    "resolve_trusted_parent_run_context",
]
