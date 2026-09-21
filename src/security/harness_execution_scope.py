"""Trusted capability-oriented execution scope for AoiTalk harness runs.

``AgentRunScope`` is repository-specific.  This module adds the upper,
server-owned contract needed by a sandboxed harness: authenticated identity,
ACL identifiers, independent read/write/delete roots, external read-only
grants, network/environment capabilities and finite resource limits.

The scope cannot be constructed from a model/tool JSON object.  Only the
trusted service factory can issue the opaque authority token required by the
constructor.  Backends should receive an already-issued object and never
derive authority from command arguments or project prompt data.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

from .agent_run_scope import AgentRunScope, ScopeDecision

PathLike = str | os.PathLike[str]
AccessKind = Literal["read", "write", "delete", "mutation"]


class HarnessExecutionScopeError(ValueError):
    """Base error for malformed or denied harness execution scopes."""


class HarnessExecutionScopeConfigurationError(HarnessExecutionScopeError):
    """Raised when trusted inputs cannot produce a safe scope."""


class HarnessExecutionScopeViolation(PermissionError, HarnessExecutionScopeError):
    """Raised when a path/operation is outside a harness scope."""


# Compatibility aliases used by integrations that call this an execution
# policy rather than a scope.
ExecutionScopeError = HarnessExecutionScopeError
ExecutionScopeConfigurationError = HarnessExecutionScopeConfigurationError
ExecutionScopeViolation = HarnessExecutionScopeViolation


class NetworkCapability(str, Enum):
    """Network egress granted to a sandboxed command."""

    NONE = "none"
    ORGANIZATION = "organization"
    ALLOWLIST = "allowlist"
    BROAD = "broad"


NetworkCapabilityValue = Literal["none", "organization", "allowlist", "broad"]


def _clean_identifier(value: Any, *, label: str, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise HarnessExecutionScopeConfigurationError(f"{label} is required")
        return None
    text = str(value).strip()
    if not text:
        if required:
            raise HarnessExecutionScopeConfigurationError(f"{label} is required")
        return None
    if "\x00" in text or len(text) > 512:
        raise HarnessExecutionScopeConfigurationError(f"invalid {label}")
    return text


def _normalise_id_tuple(values: Iterable[Any] | None, *, label: str) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for item in values or ():
        value = _clean_identifier(item, label=label)
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return tuple(result)


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(path)))


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_path_key(path), _path_key(root))) == _path_key(root)
    except (OSError, ValueError):
        return False


def _canonicalise(path: PathLike, *, base: Path | None = None) -> Path:
    raw = os.fspath(path)
    if not os.path.isabs(raw):
        raw = os.path.join(os.fspath(base or Path.cwd()), raw)
    # strict=False behaviour permits a missing final component for create.
    return Path(os.path.realpath(os.path.abspath(raw)))


def _lexical_absolute(path: PathLike, *, base: Path) -> Path:
    raw = os.fspath(path)
    if not os.path.isabs(raw):
        raw = os.path.join(os.fspath(base), raw)
    return Path(os.path.normpath(os.path.abspath(raw)))


def _existing_components(path: Path) -> Iterable[Path]:
    anchor = Path(path.anchor) if path.anchor else Path.cwd()
    current = anchor
    try:
        parts = path.relative_to(anchor).parts if path.anchor else path.parts
    except ValueError:
        current = Path()
        parts = path.parts
    for part in parts:
        current = current / part
        try:
            os.lstat(current)
        except FileNotFoundError:
            break
        except OSError:
            yield current
            break
        yield current


def _component_link(path: Path) -> tuple[bool, bool]:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False, True
    except (OSError, PermissionError, NotADirectoryError):
        return False, False
    attrs = getattr(metadata, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attrs & flag), True


def _unsafe_reparse(path: Path, root: Path) -> bool:
    """Reject links/reparse components resolving outside *root*.

    A missing final component is allowed for create/write checks.  Every
    existing component is inspected with lstat before realpath is trusted.
    """
    for component in _existing_components(path):
        linked, inspectable = _component_link(component)
        if not inspectable:
            return True
        if not linked:
            continue
        try:
            resolved = _canonicalise(component)
        except (OSError, ValueError, RuntimeError):
            return True
        if not _is_within(resolved, root):
            return True
    return False


def _canonical_roots(
    values: Sequence[PathLike] | PathLike | None,
    *,
    base: Path,
    label: str,
    require_existing: bool = True,
) -> tuple[Path, ...]:
    if values is None:
        items: Sequence[PathLike] = ()
    elif isinstance(values, (str, os.PathLike)):
        items = (values,)
    else:
        items = values
    result: list[Path] = []
    seen: set[str] = set()
    for value in items:
        try:
            path = _canonicalise(value, base=base)
        except (OSError, ValueError, RuntimeError) as exc:
            raise HarnessExecutionScopeConfigurationError(f"invalid {label}") from exc
        if require_existing and (not path.exists() or not path.is_dir()):
            raise HarnessExecutionScopeConfigurationError(
                f"{label} must be an existing directory: {path}"
            )
        if _unsafe_reparse(path, path):
            raise HarnessExecutionScopeConfigurationError(
                f"{label} contains an unsafe symlink/reparse component: {path}"
            )
        key = _path_key(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return tuple(result)


def _root_for_candidate(candidate: Path, roots: Sequence[Path]) -> Path | None:
    matched = [root for root in roots if _is_within(candidate, root)]
    if not matched:
        return None
    return max(matched, key=lambda item: len(_path_key(item)))


def _safe_alias(value: Any) -> str:
    alias = str(value or "").strip()
    if not alias or len(alias) > 96 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", alias):
        raise HarnessExecutionScopeConfigurationError("invalid external read grant mount alias")
    return alias


@dataclass(frozen=True, slots=True)
class ExternalReadGrant:
    """One server-approved, read-only external input mount."""

    root: Path
    mount_alias: str
    grant_id: str | None = None
    organization_id: str | None = None
    principal_id: str | None = None

    @property
    def alias(self) -> str:
        return self.mount_alias

    @property
    def canonical_root(self) -> Path:
        return self.root

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "root": str(self.root),
            "mount_alias": self.mount_alias,
            "read_only": True,
        }
        if self.grant_id:
            result["grant_id"] = self.grant_id
        if self.organization_id:
            result["organization_id"] = self.organization_id
        if self.principal_id:
            result["principal_id"] = self.principal_id
        return result


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Finite limits enforced by a harness backend."""

    timeout_seconds: float = 120.0
    # Keep the default finite without starving Node/PowerShell runtimes, which
    # legitimately create several worker threads/processes during startup.
    max_processes: int = 64
    max_output_bytes: int = 32_768
    max_disk_bytes: int = 256 * 1024 * 1024
    max_cpu_seconds: float | None = None
    max_memory_bytes: int | None = None
    max_background_seconds: float | None = None

    def __post_init__(self) -> None:
        timeout = float(self.timeout_seconds)
        if not (0 < timeout <= 86_400):
            raise HarnessExecutionScopeConfigurationError(
                "timeout_seconds must be between 0 and 86400"
            )
        for name in ("max_processes", "max_output_bytes", "max_disk_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise HarnessExecutionScopeConfigurationError(
                    f"{name} must be a positive integer"
                )
        ceilings = {
            "max_processes": 1024,
            "max_output_bytes": 64 * 1024 * 1024,
            "max_disk_bytes": 8 * 1024 * 1024 * 1024,
        }
        for name, ceiling in ceilings.items():
            if getattr(self, name) > ceiling:
                raise HarnessExecutionScopeConfigurationError(
                    f"{name} exceeds the trusted hard ceiling ({ceiling})"
                )
        for name in ("max_cpu_seconds", "max_memory_bytes", "max_background_seconds"):
            value = getattr(self, name)
            if value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
            ):
                raise HarnessExecutionScopeConfigurationError(
                    f"{name} must be positive when configured"
                )
        if self.max_cpu_seconds is not None and float(self.max_cpu_seconds) > 86_400:
            raise HarnessExecutionScopeConfigurationError(
                "max_cpu_seconds exceeds the trusted hard ceiling (86400)"
            )
        if self.max_background_seconds is not None and float(
            self.max_background_seconds
        ) > 86_400:
            raise HarnessExecutionScopeConfigurationError(
                "max_background_seconds exceeds the trusted hard ceiling (86400)"
            )
        if self.max_memory_bytes is not None and float(
            self.max_memory_bytes
        ) > 1024 * 1024 * 1024 * 1024:
            raise HarnessExecutionScopeConfigurationError(
                "max_memory_bytes exceeds the trusted hard ceiling (1 TiB)"
            )
        object.__setattr__(self, "timeout_seconds", timeout)

    @classmethod
    def from_values(cls, value: Any = None, **overrides: Any) -> "ResourceLimits":
        raw = dict(value) if isinstance(value, Mapping) else {}
        raw.update(overrides)
        allowed = {
            "timeout_seconds", "max_processes", "max_output_bytes", "max_disk_bytes",
            "max_cpu_seconds", "max_memory_bytes", "max_background_seconds",
        }
        return cls(**{key: raw[key] for key in allowed if key in raw})

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_processes": self.max_processes,
            "max_output_bytes": self.max_output_bytes,
            "max_disk_bytes": self.max_disk_bytes,
            "max_cpu_seconds": self.max_cpu_seconds,
            "max_memory_bytes": self.max_memory_bytes,
            "max_background_seconds": self.max_background_seconds,
        }


# The token is intentionally module-private.  Dataclass construction remains
# possible for trusted factories while a scope-looking JSON object cannot be
# promoted to authority by a caller.
_SCOPE_TOKEN = object()


@dataclass(frozen=True, slots=True)
class HarnessExecutionScope:
    """Immutable server-authorized capability set for one harness run."""

    user_id: str
    organization_id: str | None
    project_ids: tuple[str, ...]
    app_ids: tuple[str, ...]
    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]
    delete_roots: tuple[Path, ...]
    command_roots: tuple[Path, ...]
    scratch_roots: tuple[Path, ...]
    external_read_grants: tuple[ExternalReadGrant, ...]
    network_capability: NetworkCapability
    network_allowlist: tuple[str, ...]
    environment_capability_ids: tuple[str, ...]
    secret_capability_ids: tuple[str, ...]
    resource_limits: ResourceLimits
    run_id: str
    audit_id: str
    root_identity: str
    _authority_token: object = field(default=None, repr=False, compare=False)
    _capability_fingerprint: str = field(default="", init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._authority_token is not _SCOPE_TOKEN:
            raise HarnessExecutionScopeConfigurationError(
                "HarnessExecutionScope can only be issued by a trusted server factory"
            )
        user_id = _clean_identifier(self.user_id, label="user_id", required=True)
        organization_id = _clean_identifier(self.organization_id, label="organization_id")
        run_id = _clean_identifier(self.run_id, label="run_id", required=True)
        audit_id = _clean_identifier(self.audit_id, label="audit_id", required=True)
        root_identity = _clean_identifier(self.root_identity, label="root_identity", required=True)
        if not isinstance(self.resource_limits, ResourceLimits):
            raise HarnessExecutionScopeConfigurationError("resource_limits must be ResourceLimits")
        try:
            network = self.network_capability
            if not isinstance(network, NetworkCapability):
                network = NetworkCapability(str(network).strip().lower())
        except ValueError as exc:
            raise HarnessExecutionScopeConfigurationError("invalid network_capability") from exc
        read_roots = tuple(Path(item) for item in self.read_roots)
        write_roots = tuple(Path(item) for item in self.write_roots)
        delete_roots = tuple(Path(item) for item in self.delete_roots)
        command_roots = tuple(Path(item) for item in self.command_roots)
        scratch_roots = tuple(Path(item) for item in self.scratch_roots)
        for label, roots in (
            ("read_roots", read_roots), ("write_roots", write_roots),
            ("delete_roots", delete_roots), ("command_roots", command_roots),
            ("scratch_roots", scratch_roots),
        ):
            if any(not path.is_absolute() for path in roots):
                raise HarnessExecutionScopeConfigurationError(
                    f"{label} must be canonical absolute paths"
                )
            if len({_path_key(path) for path in roots}) != len(roots):
                raise HarnessExecutionScopeConfigurationError(f"{label} contains duplicate roots")
        object.__setattr__(self, "user_id", str(user_id))
        object.__setattr__(self, "organization_id", organization_id)
        object.__setattr__(self, "run_id", str(run_id))
        object.__setattr__(self, "audit_id", str(audit_id))
        object.__setattr__(self, "root_identity", str(root_identity))
        object.__setattr__(self, "network_capability", network)
        object.__setattr__(self, "project_ids", _normalise_id_tuple(self.project_ids, label="project_ids"))
        object.__setattr__(self, "app_ids", _normalise_id_tuple(self.app_ids, label="app_ids"))
        object.__setattr__(self, "network_allowlist", _normalise_id_tuple(self.network_allowlist, label="network_allowlist"))
        object.__setattr__(self, "environment_capability_ids", _normalise_id_tuple(self.environment_capability_ids, label="environment capability identifiers"))
        object.__setattr__(self, "secret_capability_ids", _normalise_id_tuple(self.secret_capability_ids, label="secret capability identifiers"))
        object.__setattr__(self, "read_roots", read_roots)
        object.__setattr__(self, "write_roots", write_roots)
        object.__setattr__(self, "delete_roots", delete_roots)
        object.__setattr__(self, "command_roots", command_roots)
        object.__setattr__(self, "scratch_roots", scratch_roots)
        grants = tuple(self.external_read_grants)
        if any(not isinstance(grant, ExternalReadGrant) for grant in grants):
            raise HarnessExecutionScopeConfigurationError(
                "external_read_grants must contain ExternalReadGrant values"
            )
        if len({grant.mount_alias.casefold() for grant in grants}) != len(grants):
            raise HarnessExecutionScopeConfigurationError("external read grant aliases must be unique")
        object.__setattr__(self, "external_read_grants", grants)
        object.__setattr__(self, "_capability_fingerprint", self._compute_fingerprint())

    @classmethod
    def _issue(cls, **kwargs: Any) -> "HarnessExecutionScope":
        """Issue a scope from trusted code in this module/service."""
        return cls(_authority_token=_SCOPE_TOKEN, **kwargs)

    @property
    def authenticated_user_id(self) -> str:
        return self.user_id

    @property
    def principal_id(self) -> str:
        return self.user_id

    @property
    def principal_user_id(self) -> str:
        return self.user_id

    @property
    def org_id(self) -> str | None:
        return self.organization_id

    @property
    def acl_project_ids(self) -> tuple[str, ...]:
        return self.project_ids

    @property
    def acl_app_ids(self) -> tuple[str, ...]:
        return self.app_ids

    @property
    def canonical_read_roots(self) -> tuple[Path, ...]:
        return self.read_roots

    @property
    def canonical_write_roots(self) -> tuple[Path, ...]:
        return self.write_roots

    @property
    def canonical_delete_roots(self) -> tuple[Path, ...]:
        return self.delete_roots

    @property
    def canonical_command_roots(self) -> tuple[Path, ...]:
        return self.command_roots

    @property
    def canonical_scratch_roots(self) -> tuple[Path, ...]:
        return self.scratch_roots

    @property
    def external_read_only_grants(self) -> tuple[ExternalReadGrant, ...]:
        return self.external_read_grants

    @property
    def external_read_roots(self) -> tuple[Path, ...]:
        return tuple(grant.root for grant in self.external_read_grants)

    @property
    def external_mount_aliases(self) -> Mapping[str, Path]:
        return {grant.mount_alias: grant.root for grant in self.external_read_grants}

    @property
    def env_capability_ids(self) -> tuple[str, ...]:
        return self.environment_capability_ids

    @property
    def secret_capabilities(self) -> tuple[str, ...]:
        return self.secret_capability_ids

    @property
    def limits(self) -> ResourceLimits:
        return self.resource_limits

    @property
    def capability_fingerprint(self) -> str:
        return self._capability_fingerprint

    @property
    def fingerprint(self) -> str:
        return self.capability_fingerprint

    @property
    def canonical_root(self) -> Path:
        """Primary mutable root (the first write root, if present)."""
        if self.write_roots:
            return self.write_roots[0]
        if self.read_roots:
            return self.read_roots[0]
        raise HarnessExecutionScopeConfigurationError("scope has no canonical root")

    @property
    def workspace_root(self) -> Path:
        return self.canonical_root

    @property
    def command_mounts(self) -> tuple[dict[str, Any], ...]:
        """Stable command-visible mount projection for sandbox backends."""
        mounts: list[dict[str, Any]] = []
        seen: set[str] = set()
        grant_keys = {_path_key(grant.root) for grant in self.external_read_grants}
        for root in self.command_roots:
            seen.add(_path_key(root))
            mounts.append({"path": str(root), "mode": "rw" if self.is_write_allowed(root) else "ro"})
        # Project/read-only roots may not be valid command cwd roots, but they
        # still need a read-only mount in a sandbox command's namespace.
        for root in self.read_roots:
            # External grants are emitted below with their stable aliases;
            # avoid first emitting an unlabelled duplicate read mount.
            if _path_key(root) in seen or _path_key(root) in grant_keys:
                continue
            seen.add(_path_key(root))
            mounts.append({"path": str(root), "mode": "ro"})
        for grant in self.external_read_grants:
            if _path_key(grant.root) in seen:
                continue
            seen.add(_path_key(grant.root))
            mounts.append({"path": str(grant.root), "alias": grant.mount_alias, "mode": "ro"})
        return tuple(mounts)

    def _compute_fingerprint(self) -> str:
        payload = {
            "user_id": self.user_id,
            "organization_id": self.organization_id,
            "project_ids": sorted(self.project_ids, key=str.casefold),
            "app_ids": sorted(self.app_ids, key=str.casefold),
            "read_roots": sorted(_path_key(path) for path in self.read_roots),
            "write_roots": sorted(_path_key(path) for path in self.write_roots),
            "delete_roots": sorted(_path_key(path) for path in self.delete_roots),
            "command_roots": sorted(_path_key(path) for path in self.command_roots),
            "scratch_roots": sorted(_path_key(path) for path in self.scratch_roots),
            "external_read_grants": [grant.to_dict() for grant in sorted(self.external_read_grants, key=lambda item: item.mount_alias.casefold())],
            "network_capability": self.network_capability.value,
            "network_allowlist": sorted(self.network_allowlist, key=str.casefold),
            "environment_capability_ids": sorted(self.environment_capability_ids, key=str.casefold),
            "secret_capability_ids": sorted(self.secret_capability_ids, key=str.casefold),
            "resource_limits": self.resource_limits.to_dict(),
            "root_identity": self.root_identity,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "surrogatepass")
        return hashlib.sha256(encoded).hexdigest()

    def _decision(self, path: PathLike, roots: Sequence[Path], scope: str) -> ScopeDecision:
        base = self.canonical_root
        lexical = _lexical_absolute(path, base=base)
        candidate = _canonicalise(path, base=base)
        root = _root_for_candidate(candidate, roots)
        if root is None:
            return ScopeDecision(False, candidate, scope, "canonical path is outside the configured scope")
        if _unsafe_reparse(lexical, root) or _unsafe_reparse(candidate, root):
            return ScopeDecision(False, candidate, scope, "path traverses a symlink/junction/reparse point outside the scope")
        return ScopeDecision(True, candidate, scope)

    @staticmethod
    def _raise(decision: ScopeDecision, operation: str) -> Path:
        if not decision.allowed:
            raise HarnessExecutionScopeViolation(
                f"harness scope denied {operation}: {decision.path} ({decision.reason})"
            )
        return decision.path

    def check_read(self, path: PathLike) -> ScopeDecision:
        return self._decision(path, self.read_roots, "read")

    def assert_read_allowed(self, path: PathLike) -> Path:
        return self._raise(self.check_read(path), "read")

    def is_read_allowed(self, path: PathLike) -> bool:
        return self.check_read(path).allowed

    def check_write(self, path: PathLike) -> ScopeDecision:
        return self._decision(path, self.write_roots, "write")

    def assert_write_allowed(self, path: PathLike) -> Path:
        return self._raise(self.check_write(path), "write")

    def is_write_allowed(self, path: PathLike) -> bool:
        return self.check_write(path).allowed

    def check_delete(self, path: PathLike) -> ScopeDecision:
        decision = self._decision(path, self.delete_roots, "delete")
        if decision.allowed and _path_key(decision.path) == _path_key(self.canonical_root):
            return ScopeDecision(False, decision.path, "delete", "canonical root cannot be deleted")
        return decision

    def assert_delete_allowed(self, path: PathLike) -> Path:
        return self._raise(self.check_delete(path), "delete")

    def is_delete_allowed(self, path: PathLike) -> bool:
        return self.check_delete(path).allowed

    def check_mutation(self, path: PathLike, operation: str = "write") -> ScopeDecision:
        if str(operation).strip().lower() in {"delete", "remove", "rmdir"}:
            return self.check_delete(path)
        return self.check_write(path)

    def assert_mutation_allowed(self, path: PathLike, operation: str = "write") -> Path:
        return self._raise(self.check_mutation(path, operation), operation)

    def is_mutation_allowed(self, path: PathLike, operation: str = "write") -> bool:
        return self.check_mutation(path, operation).allowed

    def check_command_cwd(self, cwd: PathLike | None = None) -> ScopeDecision:
        target = self.canonical_root if cwd is None else cwd
        decision = self._decision(target, self.command_roots, "command")
        if decision.allowed and decision.path.exists() and not decision.path.is_dir():
            return ScopeDecision(False, decision.path, "command", "command cwd is not a directory")
        return decision

    def assert_command_cwd_allowed(self, cwd: PathLike | None = None) -> Path:
        return self._raise(self.check_command_cwd(cwd), "command cwd")

    def is_command_cwd_allowed(self, cwd: PathLike | None = None) -> bool:
        return self.check_command_cwd(cwd).allowed

    def check_path(self, path: PathLike, access: AccessKind = "read") -> ScopeDecision:
        if access == "read":
            return self.check_read(path)
        if access == "delete":
            return self.check_delete(path)
        if access in {"write", "mutation"}:
            return self.check_write(path)
        raise HarnessExecutionScopeConfigurationError(f"unsupported path access: {access!r}")

    def assert_path_allowed(self, path: PathLike, access: AccessKind = "read") -> Path:
        return self._raise(self.check_path(path, access), access)

    def assert_move_allowed(self, source: PathLike, destination: PathLike) -> tuple[Path, Path]:
        return self.assert_delete_allowed(source), self.assert_write_allowed(destination)

    def assert_copy_allowed(self, source: PathLike, destination: PathLike) -> tuple[Path, Path]:
        return self.assert_read_allowed(source), self.assert_write_allowed(destination)

    def to_agent_run_scope(self) -> AgentRunScope:
        """Adapt this capability set to repository-specific integrations.

        External grants remain read-only.  ``AgentRunScope`` receives them as
        read roots but its mutation-root invariant filters any accidental
        external write/delete/command entries.
        """
        target = self.canonical_root
        scratch = tuple(self.scratch_roots)
        allowed_mutation_roots = (*scratch, target)
        write_roots = tuple(root for root in self.write_roots if any(_is_within(root, allowed) for allowed in allowed_mutation_roots))
        delete_roots = tuple(root for root in self.delete_roots if any(_is_within(root, allowed) for allowed in allowed_mutation_roots))
        command_roots = tuple(root for root in self.command_roots if any(_is_within(root, allowed) for allowed in allowed_mutation_roots))
        level: Literal["none", "read", "write"] = "write" if write_roots else ("read" if self.read_roots else "none")
        return AgentRunScope.for_repository(
            target,
            repository_identity=self.root_identity,
            run_id=self.run_id,
            workspace_access_level=level,
            read_roots=self.read_roots,
            write_roots=write_roots,
            delete_roots=delete_roots,
            command_roots=command_roots,
            scratch_roots=scratch,
        )

    as_agent_run_scope = to_agent_run_scope
    to_repository_scope = to_agent_run_scope

    @property
    def repository_scope(self) -> AgentRunScope:
        return self.to_agent_run_scope()

    def to_dict(self) -> dict[str, Any]:
        """Return audit metadata without secret values or authority tokens."""
        return {
            "user_id": self.user_id,
            "organization_id": self.organization_id,
            "project_ids": list(self.project_ids),
            "app_ids": list(self.app_ids),
            "read_roots": [str(path) for path in self.read_roots],
            "write_roots": [str(path) for path in self.write_roots],
            "delete_roots": [str(path) for path in self.delete_roots],
            "command_roots": [str(path) for path in self.command_roots],
            "scratch_roots": [str(path) for path in self.scratch_roots],
            "external_read_grants": [grant.to_dict() for grant in self.external_read_grants],
            "network_capability": self.network_capability.value,
            "network_allowlist": list(self.network_allowlist),
            "environment_capability_ids": list(self.environment_capability_ids),
            "secret_capability_ids": list(self.secret_capability_ids),
            "resource_limits": self.resource_limits.to_dict(),
            "run_id": self.run_id,
            "audit_id": self.audit_id,
            "root_identity": self.root_identity,
            "capability_fingerprint": self.capability_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "HarnessExecutionScope":
        raise HarnessExecutionScopeConfigurationError(
            "execution scope must be issued by the server; dictionaries are untrusted"
        )


def scope_from_untrusted(value: Any) -> HarnessExecutionScope:
    """Never promote model/tool/project payloads into a harness scope."""
    if isinstance(value, HarnessExecutionScope):
        return value
    raise HarnessExecutionScopeConfigurationError(
        "untrusted execution-scope payload cannot grant authority"
    )


# A backend can bind the issued upper scope for the lifetime of a command.
# Context-local storage prevents one concurrent turn from inheriting another
# turn's filesystem/network authority; no ambient default grants access.
_current_harness_scope: ContextVar[HarnessExecutionScope | None] = ContextVar(
    "aoi_harness_execution_scope",
    default=None,
)


def get_current_harness_execution_scope() -> HarnessExecutionScope | None:
    return _current_harness_scope.get()


def require_current_harness_execution_scope() -> HarnessExecutionScope:
    scope = _current_harness_scope.get()
    if scope is None:
        raise HarnessExecutionScopeViolation(
            "no HarnessExecutionScope is bound to the current execution context"
        )
    return scope


def bind_harness_execution_scope(
    scope: HarnessExecutionScope | None,
) -> Token[HarnessExecutionScope | None]:
    if scope is not None and not isinstance(scope, HarnessExecutionScope):
        raise TypeError("scope must be a HarnessExecutionScope or None")
    return _current_harness_scope.set(scope)


def reset_harness_execution_scope(token: Token[HarnessExecutionScope | None]) -> None:
    _current_harness_scope.reset(token)


@contextmanager
def harness_execution_scope_context(scope: HarnessExecutionScope):
    token = bind_harness_execution_scope(scope)
    try:
        yield scope
    finally:
        reset_harness_execution_scope(token)


# Common aliases for callers using shorter names.
ExecutionScope = HarnessExecutionScope
ExternalReadOnlyGrant = ExternalReadGrant
ExecutionResourceLimits = ResourceLimits


__all__ = [
    "AccessKind", "ExecutionResourceLimits", "ExecutionScope",
    "ExecutionScopeConfigurationError", "ExecutionScopeError", "ExecutionScopeViolation",
    "ExternalReadGrant", "ExternalReadOnlyGrant", "HarnessExecutionScope",
    "HarnessExecutionScopeConfigurationError", "HarnessExecutionScopeError",
    "HarnessExecutionScopeViolation", "NetworkCapability", "NetworkCapabilityValue",
    "ResourceLimits", "scope_from_untrusted", "get_current_harness_execution_scope",
    "require_current_harness_execution_scope", "bind_harness_execution_scope",
    "reset_harness_execution_scope", "harness_execution_scope_context",
]
