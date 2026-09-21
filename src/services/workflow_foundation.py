"""Ephemeral privacy foundation shared by system-owned workflows.

The document and App/Macro workflows deliberately remain separate services,
but they share a small security primitive: :class:`ProtectedWorkflowContext`.
It binds raw values to opaque, request-local node/object references and creates
an advisory projection through the existing one-way privacy boundary.  The
context is never persisted and its raw bindings are never part of a cloud
payload, a projection ``repr`` or a provenance record.

This module is intentionally *not* a Cloud Advisor/router.  It does not choose
providers, models, reasoning effort or review policy.  ``PrivacyMaskingBoundary``
and, underneath it, ``OutboundPrivacyGateway.materialize_one_way`` remain the
only masking implementation used here; the Cloud Advisor remains the sole
external consultation authority.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import contextvars
import copy
import hashlib
import inspect
import json
import math
import re
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol, Sequence

from .privacy_masking_boundary import PrivacyMaskingBoundary, PrivacyMaskingError
from .outbound_privacy_service import get_privacy_policy_context


WORKFLOW_PROJECTION_SCHEMA = "aoitalk.workflow.projection.v1"
DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_NODES = 256
DEFAULT_MAX_ITEMS = 128
DEFAULT_MAX_STRING_CHARS = 8_192
DEFAULT_MAX_PAYLOAD_BYTES = 131_072

_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_ID_RE = re.compile(r"^wf_(?:ctx|node|obj)_[a-f0-9]{32}$")
_SENSITIVE_LITERAL_MIN_CHARS = 4
_LITERAL_TOKEN_RE = re.compile(r"[^\s,;|{}\[\]()<>\"']{4,}")

# Reference fields are deliberately narrow.  In particular, arbitrary model
# output such as ``{"lookup": "..."}`` never gets interpreted as a local
# reference.  This is the allowlist that makes cloud responses advisory only.
REFERENCE_KEYS = frozenset(
    {
        "node_id",
        "target_node_id",
        "target_node",
        "source_node_id",
        "object_id",
        "target_object_id",
        "source_object_id",
        "ref",
        "alias",
        "node_alias",
        "node_ref",
        "binding_id",
    }
)
ALLOWED_OPERATIONS = frozenset(
    {
        "read",
        "reference",
        "use",
        "rebind",
        "update",
        "replace",
        "append",
        "insert",
        "delete",
        "create",
        "validate",
        "execute",
        "test",
        "preserve",
        "copy",
    }
)


class WorkflowContextError(RuntimeError):
    """Base error for an invalid or unsafe workflow-context operation."""


class StaleWorkflowContext(WorkflowContextError):
    """Raised when a reference belongs to another context/generation."""


class WorkflowContextExpired(StaleWorkflowContext):
    """Raised after a request-scoped context has been closed."""


class UnknownWorkflowReference(WorkflowContextError):
    """Raised for an unknown node/object identifier or alias."""


class AmbiguousWorkflowReference(WorkflowContextError):
    """Raised when one object identifier maps to multiple nodes."""


class WorkflowProjectionError(WorkflowContextError):
    """Raised when a projection cannot be bounded and made safe."""


class WorkflowBindingError(WorkflowContextError):
    """Raised when a local binding or provenance record is malformed."""


class WorkflowMaskingError(WorkflowProjectionError):
    """Raised when the canonical one-way masking boundary fails."""


class WorkflowMaskingBoundary(Protocol):
    """Minimal boundary seam used by tests and embedding workflow adapters."""

    def materialize_sync(self, value: Any, *, source_kind: str = "masking") -> Any:
        ...

    async def materialize(self, value: Any, *, source_kind: str = "masking") -> Any:
        ...


@dataclass(frozen=True, slots=True)
class WorkflowProvenance:
    """Safe provenance metadata retained for one local workflow only.

    ``source_ref_hash`` is a digest, never the source path/URL/database ID.
    ``allowed_operations`` is the local rebinding allowlist.  No raw source or
    user-provided metadata is represented by this object.
    """

    source_kind: str = "unknown"
    source_ref_hash: str = ""
    version: str = ""
    operation: str = "read"
    allowed_operations: tuple[str, ...] = ("read", "reference")

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_ref_hash": self.source_ref_hash,
            "version": self.version,
            "operation": self.operation,
            "allowed_operations": list(self.allowed_operations),
        }

    to_dict = as_dict


@dataclass(frozen=True, slots=True)
class WorkflowReference:
    """Opaque stable reference to one locally bound workflow node."""

    context_id: str
    node_id: str
    object_id: str
    kind: str = "value"
    role: str = "value"
    generation: str = field(repr=False, compare=False, default="")

    @property
    def id(self) -> str:
        return self.node_id

    @property
    def alias(self) -> str:
        """Cloud-safe alias; it is intentionally the opaque node ID."""

        return self.node_id

    def as_dict(self) -> dict[str, str]:
        return {
            "context_id": self.context_id,
            "node_id": self.node_id,
            "object_id": self.object_id,
            "kind": self.kind,
            "role": self.role,
        }

    to_dict = as_dict


@dataclass(frozen=True, slots=True)
class ProtectedBinding:
    """Local binding record.

    The raw value is intentionally ``repr=False`` and excluded from equality;
    callers should use :meth:`ProtectedWorkflowContext.resolve` rather than
    reaching into this field.  Keeping it in memory is necessary for local
    rebinding, while excluding it from representations prevents accidental
    log/audit leakage.
    """

    reference: WorkflowReference
    raw_value: Any = field(repr=False, compare=False)
    sensitive: bool = field(default=True, repr=False, compare=False)
    provenance: WorkflowProvenance = field(default_factory=WorkflowProvenance)

    @property
    def node_id(self) -> str:
        return self.reference.node_id

    @property
    def object_id(self) -> str:
        return self.reference.object_id

    @property
    def alias(self) -> str:
        return self.reference.alias


@dataclass(frozen=True, slots=True)
class LocalRebinding:
    """Result of an allowlisted cloud reference resolution.

    ``value`` is local-only and omitted from ``repr``/comparison.  A workflow
    can use this object when it needs both the advisory operation and the raw
    value without ever putting that value back into cloud-facing metadata.
    """

    reference: WorkflowReference
    operation: str = "read"
    value: Any = field(repr=False, compare=False, default=None)
    provenance: WorkflowProvenance = field(default_factory=WorkflowProvenance)

    @property
    def raw_value(self) -> Any:
        return self.value


@dataclass(frozen=True, slots=True)
class CloudProjection:
    """Bounded safe payload intended for Cloud Advisor consultation.

    ``payload`` contains only masked/structural values and opaque IDs.  The
    class has no field for a raw mapping.  Its repr excludes the payload to
    keep logs concise; :meth:`to_dict` returns a defensive safe copy.
    """

    context_id: str
    workflow_kind: str
    node_ids: tuple[str, ...]
    payload: Mapping[str, Any] = field(repr=False, compare=False)
    schema_version: str = WORKFLOW_PROJECTION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise TypeError("cloud projection payload must be a mapping")
        # Detach from a mutable boundary result.  The copy contains no local
        # raw mapping by construction and is checked before this object is
        # returned.
        object.__setattr__(self, "payload", copy.deepcopy(dict(self.payload)))

    @property
    def safe_payload(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.payload))

    @property
    def cloud_payload(self) -> dict[str, Any]:
        return self.safe_payload

    @property
    def refs(self) -> tuple[str, ...]:
        return self.node_ids

    @property
    def masked_text(self) -> str:
        nodes = self.payload.get("nodes") if isinstance(self.payload, Mapping) else None
        if isinstance(nodes, list) and len(nodes) == 1 and isinstance(nodes[0], Mapping):
            candidate = nodes[0].get("value")
            if isinstance(candidate, str):
                return candidate
        return str(self)

    @property
    def value(self) -> Any:
        return self.masked_text

    def to_dict(self) -> dict[str, Any]:
        return self.safe_payload

    as_dict = to_dict

    def __str__(self) -> str:
        return json.dumps(self.safe_payload, ensure_ascii=False, sort_keys=True)

    def __await__(self):
        """Permit ``await ctx.project(...)`` in async workflow adapters.

        ``project`` is synchronous so it can be used by deterministic file
        adapters.  Making the result awaitable is a small compatibility seam
        for callers that naturally await all workflow operations.
        """

        async def ready() -> "CloudProjection":
            return self

        return ready().__await__()


# Domain-language aliases used by Document/App adapters and older embedding
# code.  They all point at the same implementation; there is no second
# context lifecycle or policy boundary.
ProtectedNode = WorkflowReference
ProtectedObject = WorkflowReference
ProtectedWorkflowNode = WorkflowReference
ProtectedWorkflowObject = WorkflowReference
WorkflowNodeRef = WorkflowReference
WorkflowObjectRef = WorkflowReference
RawBinding = ProtectedBinding
ReboundValue = LocalRebinding
SafeWorkflowProjection = CloudProjection
WorkflowProjection = CloudProjection
WorkflowContextStale = StaleWorkflowContext
UnknownReference = UnknownWorkflowReference
ProjectionError = WorkflowProjectionError


_current_workflow_context: contextvars.ContextVar[
    "ProtectedWorkflowContext | None"
] = contextvars.ContextVar("aoitalk_protected_workflow_context", default=None)


def get_current_workflow_context() -> "ProtectedWorkflowContext | None":
    return _current_workflow_context.get()


def set_current_workflow_context(
    context: "ProtectedWorkflowContext | None",
) -> contextvars.Token:
    return _current_workflow_context.set(context)


def reset_current_workflow_context(token: contextvars.Token) -> None:
    _current_workflow_context.reset(token)


@contextlib.contextmanager
def workflow_context_scope(
    context: "ProtectedWorkflowContext | None" = None,
    **kwargs: Any,
):
    """Bind one context to the current request/task and close owned state."""

    owned = context is None
    active = context or ProtectedWorkflowContext(**kwargs)
    token = set_current_workflow_context(active)
    try:
        yield active
    finally:
        reset_current_workflow_context(token)
        if owned:
            active.close()


class ProtectedWorkflowContext:
    """Request-scoped ephemeral raw/safe workflow context.

    A context owns one fresh :class:`PrivacyMaskingBoundary`, one alias scope,
    and one local binding table.  It is safe to use from concurrent tasks when
    each task has its own context (the normal request lifecycle); a re-entrant
    lock additionally protects accidental same-context callbacks.
    """

    def __init__(
        self,
        workflow_kind: str = "workflow",
        config: Any | None = None,
        *,
        session_context: Mapping[str, Any] | None = None,
        project_metadata: Mapping[str, Any] | None = None,
        semantic_redactor: Callable[..., Any] | None = None,
        masking_boundary: WorkflowMaskingBoundary | None = None,
        boundary_factory: Callable[..., WorkflowMaskingBoundary] | None = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_nodes: int = DEFAULT_MAX_NODES,
        max_items: int = DEFAULT_MAX_ITEMS,
        max_string_chars: int = DEFAULT_MAX_STRING_CHARS,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
        max_payload_size: int | None = None,
        max_payload_chars: int | None = None,
    ) -> None:
        self.workflow_kind = _safe_token(workflow_kind, fallback="workflow")
        self.config = config
        self.context_id = _new_id("ctx")
        self._generation = secrets.token_hex(16)
        self._lock = threading.RLock()
        self._closed = False
        self.max_depth = _positive_limit(max_depth, DEFAULT_MAX_DEPTH)
        self.max_nodes = _positive_limit(max_nodes, DEFAULT_MAX_NODES)
        self.max_items = _positive_limit(max_items, DEFAULT_MAX_ITEMS)
        self.max_string_chars = _positive_limit(
            max_string_chars, DEFAULT_MAX_STRING_CHARS
        )
        payload_limit = max_payload_bytes
        if max_payload_size is not None:
            payload_limit = max_payload_size
        if max_payload_chars is not None:
            # UTF-8 bytes are the provider-bound limit; a character limit is
            # conservatively converted to the same number of bytes.
            payload_limit = max_payload_chars
        self.max_payload_bytes = _positive_limit(payload_limit, DEFAULT_MAX_PAYLOAD_BYTES)
        self._bindings: dict[str, ProtectedBinding] = {}
        self._object_nodes: dict[str, list[str]] = {}
        self._stable_keys: dict[tuple[str, str], str] = {}
        self._object_fingerprints: dict[tuple[str, str], str] = {}
        self._provenance: dict[str, WorkflowProvenance] = {}

        if masking_boundary is not None:
            self._masking_boundary: WorkflowMaskingBoundary | None = masking_boundary
        else:
            factory = boundary_factory or PrivacyMaskingBoundary
            try:
                self._masking_boundary = factory(
                    config,
                    semantic_redactor=semantic_redactor,
                )
            except TypeError:
                # Keep the compatibility seam narrow for tiny embedding
                # factories; constructor failures otherwise remain visible.
                self._masking_boundary = factory(config)

            # PrivacyMaskingBoundary intentionally starts with an empty policy
            # scope for /masking.  Workflow contexts, unlike the one-way user
            # command, inherit the current request's policy metadata.
            boundary_gateway = getattr(self._masking_boundary, "gateway", None)
            update_policy = getattr(boundary_gateway, "update_policy_context", None)
            if callable(update_policy):
                inherited = get_privacy_policy_context()
                active_session = (
                    session_context
                    if session_context is not None
                    else inherited.session_context
                )
                active_project = (
                    project_metadata
                    if project_metadata is not None
                    else inherited.project_metadata
                )
                try:
                    update_policy(
                        session_context=active_session,
                        project_metadata=active_project,
                    )
                except TypeError:
                    # A minimal embedding gateway may not expose policy
                    # context refresh; its canonical boundary still forces the
                    # protected materialization path.
                    pass

    def __repr__(self) -> str:
        with self._lock:
            status = "closed" if self._closed else "active"
            return (
                f"ProtectedWorkflowContext(workflow_kind={self.workflow_kind!r}, "
                f"context_id={self.context_id!r}, status={status!r}, "
                f"bindings={len(self._bindings)})"
            )

    def __enter__(self) -> "ProtectedWorkflowContext":
        self._assert_active()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    async def __aenter__(self) -> "ProtectedWorkflowContext":
        self._assert_active()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def is_active(self) -> bool:
        return not self.closed

    @property
    def generation(self) -> str:
        return self._generation

    @property
    def id(self) -> str:
        """Opaque context identity (safe to include in advisory envelopes)."""

        return self.context_id

    @property
    def workflow_id(self) -> str:
        return self.context_id

    @property
    def aliases(self) -> dict[str, str]:
        """Return safe object-to-node aliases, never object-to-raw values."""

        with self._lock:
            self._assert_active()
            return {
                binding.object_id: binding.node_id
                for binding in self._bindings.values()
            }

    @property
    def references(self) -> tuple[WorkflowReference, ...]:
        with self._lock:
            self._assert_active()
            return tuple(binding.reference for binding in self._bindings.values())

    @property
    def bindings(self) -> tuple[ProtectedBinding, ...]:
        """Return local records with raw fields excluded from ``repr``."""

        with self._lock:
            self._assert_active()
            return tuple(self._bindings.values())

    def close(self) -> None:
        """Destroy request-local mappings and invalidate all references."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._bindings.clear()
            self._object_nodes.clear()
            self._stable_keys.clear()
            self._object_fingerprints.clear()
            self._provenance.clear()
            self._masking_boundary = None

    def _assert_active(self) -> None:
        if self._closed:
            raise WorkflowContextExpired("workflow protected context is closed")

    def _validate_reference_context(self, reference: WorkflowReference) -> None:
        if reference.context_id != self.context_id or (
            reference.generation and reference.generation != self._generation
        ):
            raise StaleWorkflowContext("workflow reference belongs to another context")

    def _snapshot_local(self, value: Any) -> Any:
        counters = [0]
        return self._bounded_copy(value, depth=0, counters=counters, ancestors=set())

    def _bounded_copy(
        self,
        value: Any,
        *,
        depth: int,
        counters: list[int],
        ancestors: set[int] | None = None,
    ) -> Any:
        active_ancestors = ancestors if ancestors is not None else set()
        if depth > self.max_depth:
            raise WorkflowProjectionError("workflow value exceeded depth limit")
        counters[0] += 1
        if counters[0] > self.max_nodes:
            raise WorkflowProjectionError("workflow value exceeded node limit")
        if value is None or type(value) is bool:
            return value
        if type(value) is int:
            return value
        if type(value) is float:
            if not math.isfinite(value):
                raise WorkflowProjectionError("workflow value contained non-finite number")
            return value
        if isinstance(value, str):
            if len(value) > self.max_string_chars:
                raise WorkflowProjectionError("workflow value exceeded string limit")
            return value
        if isinstance(value, (bytes, bytearray, memoryview)):
            raise WorkflowProjectionError("raw binary workflow values are unsupported")
        if isinstance(value, Mapping):
            if len(value) > self.max_items:
                raise WorkflowProjectionError("workflow mapping exceeded item limit")
            identity = id(value)
            if identity in active_ancestors:
                raise WorkflowProjectionError("workflow value contained a cycle")
            active_ancestors.add(identity)
            local_mapping: dict[Any, Any] = {}
            try:
                for key, item in value.items():
                    # Mapping keys are user data too.  Store them as values so
                    # the canonical masking boundary can protect confidential
                    # names.
                    if isinstance(key, (bytes, bytearray, memoryview)):
                        raise WorkflowProjectionError("binary workflow mapping key is unsupported")
                    if not isinstance(key, (str, int, float, bool)):
                        raise WorkflowProjectionError("unsupported workflow mapping key type")
                    if isinstance(key, float) and not math.isfinite(key):
                        raise WorkflowProjectionError("workflow mapping key was non-finite")
                    key_text = key if isinstance(key, str) else str(key)
                    if len(key_text) > self.max_string_chars:
                        raise WorkflowProjectionError("workflow mapping key exceeded string limit")
                    local_mapping[key] = self._bounded_copy(
                        item,
                        depth=depth + 1,
                        counters=counters,
                        ancestors=active_ancestors,
                    )
                return local_mapping
            finally:
                active_ancestors.discard(identity)
        if isinstance(value, (list, tuple)):
            if len(value) > self.max_items:
                raise WorkflowProjectionError("workflow sequence exceeded item limit")
            identity = id(value)
            if identity in active_ancestors:
                raise WorkflowProjectionError("workflow value contained a cycle")
            active_ancestors.add(identity)
            try:
                items = [
                    self._bounded_copy(
                        item,
                        depth=depth + 1,
                        counters=counters,
                        ancestors=active_ancestors,
                    )
                    for item in value
                ]
                return tuple(items) if isinstance(value, tuple) else items
            finally:
                active_ancestors.discard(identity)
        # Do not call repr(value): arbitrary objects commonly encode source
        # paths, tokens, or document contents in their repr.
        raise WorkflowProjectionError("unsupported workflow value type")

    def _projection_value(self, value: Any) -> Any:
        """Convert a local bounded value to a key-safe cloud representation."""

        if isinstance(value, Mapping):
            entries: list[dict[str, Any]] = []
            for key, item in value.items():
                key_text = key if isinstance(key, str) else str(key)
                entries.append(
                    {
                        "name": key_text,
                        "value": self._projection_value(item),
                    }
                )
            return {"entries": entries}
        if isinstance(value, tuple):
            return [self._projection_value(item) for item in value]
        if isinstance(value, list):
            return [self._projection_value(item) for item in value]
        return value

    def _fingerprint(self, value: Any) -> str:
        try:
            encoded = json.dumps(
                _canonical_for_fingerprint(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except Exception as exc:  # pragma: no cover - bounded copy is JSON-safe
            raise WorkflowBindingError("workflow value could not be fingerprinted") from exc
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def register(
        self,
        value: Any,
        *,
        kind: str = "value",
        role: str | None = None,
        node_id: str | None = None,
        object_id: str | None = None,
        stable_key: str | None = None,
        sensitive: bool = True,
        source_kind: str = "workflow",
        source_ref: Any | None = None,
        version: Any | None = None,
        operation: str = "read",
        allowed_operations: Iterable[str] | None = None,
        provenance: WorkflowProvenance | Mapping[str, Any] | None = None,
    ) -> WorkflowReference:
        """Bind one bounded raw value and return its opaque node reference."""

        with self._lock:
            self._assert_active()
            if type(sensitive) is not bool:
                raise WorkflowBindingError("binding sensitivity must be boolean")
            bounded = self._snapshot_local(value)
            normalized_kind = _safe_token(kind, fallback="value")
            normalized_role = _safe_token(role or normalized_kind, fallback=normalized_kind)
            fingerprint = self._fingerprint(bounded)

            if provenance is not None:
                if isinstance(provenance, WorkflowProvenance):
                    source_kind = provenance.source_kind
                    version = provenance.version or version
                    operation = provenance.operation
                    if allowed_operations is None:
                        allowed_operations = provenance.allowed_operations
                elif isinstance(provenance, Mapping):
                    source_kind = provenance.get("source_kind", source_kind)
                    source_ref = provenance.get("source_ref", source_ref)
                    if source_ref is None:
                        source_ref = provenance.get("source_ref_hash", source_ref)
                    version = provenance.get("version", version)
                    operation = provenance.get("operation", operation)
                    if allowed_operations is None:
                        candidate_allowed = provenance.get("allowed_operations")
                        if candidate_allowed is not None:
                            allowed_operations = candidate_allowed
                else:
                    raise WorkflowBindingError("provenance is malformed")

            stable_digest = ""
            if stable_key is not None:
                if not isinstance(stable_key, str) or not stable_key.strip():
                    raise WorkflowBindingError("stable key must be a non-empty string")
                if len(stable_key) > self.max_string_chars:
                    raise WorkflowBindingError("stable key exceeded string limit")
                stable_digest = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()
                existing_node = self._stable_keys.get((normalized_kind, stable_digest))
                if existing_node is not None:
                    existing = self._bindings[existing_node]
                    if self._fingerprint(existing.raw_value) != fingerprint:
                        raise WorkflowBindingError("stable key is already bound to another value")
                    return existing.reference

            if node_id is not None:
                _validate_id(node_id, "node")
                if node_id in self._bindings:
                    existing = self._bindings[node_id]
                    if self._fingerprint(existing.raw_value) != fingerprint:
                        raise WorkflowBindingError("node ID is already bound to another value")
                    return existing.reference
            else:
                node_id = _new_id("node")

            if object_id is not None:
                _validate_id(object_id, "obj")
            else:
                object_key = (normalized_kind, fingerprint)
                object_id = self._object_fingerprints.get(object_key)
                if object_id is None:
                    object_id = _new_id("obj")
                    self._object_fingerprints[object_key] = object_id

            allowed = _normalize_operations(allowed_operations)
            normalized_operation = _normalize_operation(operation)
            if normalized_operation not in allowed:
                allowed = tuple(dict.fromkeys((normalized_operation, *allowed)))
            provenance = _build_provenance(
                source_kind=source_kind,
                source_ref=source_ref,
                version=version,
                operation=normalized_operation,
                allowed_operations=allowed,
            )
            reference = WorkflowReference(
                context_id=self.context_id,
                node_id=node_id,
                object_id=object_id,
                kind=normalized_kind,
                role=normalized_role,
                generation=self._generation,
            )
            binding = ProtectedBinding(
                reference=reference,
                raw_value=bounded,
                sensitive=sensitive,
                provenance=provenance,
            )
            self._bindings[node_id] = binding
            self._object_nodes.setdefault(object_id, []).append(node_id)
            if stable_digest:
                self._stable_keys[(normalized_kind, stable_digest)] = node_id
            self._provenance[node_id] = provenance
            return reference

    # Explicit aliases make the foundation convenient for both workflow
    # adapters without creating a second implementation.
    bind = register
    add_value = register
    register_node = register
    bind_node = register
    register_value = register
    bind_raw = register
    bind_local = register

    def alias_for(self, value: Any, **kwargs: Any) -> str:
        return self.register(value, **kwargs).node_id

    def _resolve_id(self, identifier: str) -> ProtectedBinding:
        if not isinstance(identifier, str) or not identifier:
            raise UnknownWorkflowReference("workflow reference is unknown")
        binding = self._bindings.get(identifier)
        if binding is not None:
            return binding
        node_ids = self._object_nodes.get(identifier)
        if node_ids:
            if len(node_ids) != 1:
                raise AmbiguousWorkflowReference("object reference maps to multiple nodes")
            return self._bindings[node_ids[0]]
        raise UnknownWorkflowReference("workflow reference is unknown")

    def _resolve_binding(
        self,
        reference: WorkflowReference | ProtectedBinding | str,
        *,
        operation: str = "read",
    ) -> ProtectedBinding:
        if isinstance(reference, ProtectedBinding):
            reference = reference.reference
        if isinstance(reference, WorkflowReference):
            self._validate_reference_context(reference)
            identifier = reference.node_id
        elif isinstance(reference, str):
            identifier = reference
        else:
            raise UnknownWorkflowReference("workflow reference is unknown")
        binding = self._resolve_id(identifier)
        normalized_operation = _normalize_operation(operation)
        # Bindings may intentionally expose only a non-read operation (for
        # example an update target).  Local projection/rebinding still needs
        # to retrieve that value, so a default ``read`` request can use the
        # binding's declared operation without broadening its allowlist.
        if (
            normalized_operation == "read"
            and normalized_operation not in binding.provenance.allowed_operations
            and binding.provenance.operation in binding.provenance.allowed_operations
        ):
            normalized_operation = binding.provenance.operation
        if normalized_operation not in binding.provenance.allowed_operations:
            raise WorkflowBindingError("workflow operation is not allowlisted")
        return binding

    def resolve(
        self,
        reference: WorkflowReference | ProtectedBinding | str,
        *,
        operation: str = "read",
    ) -> Any:
        """Resolve a local reference, rejecting stale/unknown IDs."""

        with self._lock:
            self._assert_active()
            binding = self._resolve_binding(reference, operation=operation)
            return copy.deepcopy(binding.raw_value)

    resolve_node = resolve
    resolve_alias = resolve
    resolve_raw = resolve
    get_raw = resolve

    def record_provenance(
        self,
        reference: WorkflowReference | ProtectedBinding | str,
        *,
        source_kind: str,
        source_ref: Any | None = None,
        version: Any | None = None,
        operation: str = "read",
        allowed_operations: Iterable[str] | None = None,
    ) -> WorkflowProvenance:
        with self._lock:
            self._assert_active()
            binding = self._resolve_binding(reference, operation="read")
            existing_allowed = binding.provenance.allowed_operations
            allowed = _normalize_operations(
                allowed_operations if allowed_operations is not None else existing_allowed
            )
            normalized_operation = _normalize_operation(operation)
            if normalized_operation not in allowed:
                raise WorkflowBindingError("provenance operation is not allowlisted")
            provenance = _build_provenance(
                source_kind=source_kind,
                source_ref=source_ref,
                version=version,
                operation=normalized_operation,
                allowed_operations=allowed,
            )
            self._provenance[binding.node_id] = provenance
            # Keep the binding's local safe metadata coherent.  The raw value
            # is preserved and never copied into the provenance object.
            self._bindings[binding.node_id] = ProtectedBinding(
                reference=binding.reference,
                raw_value=binding.raw_value,
                sensitive=binding.sensitive,
                provenance=provenance,
            )
            return provenance

    def provenance(
        self,
        reference: WorkflowReference | ProtectedBinding | str | None = None,
    ) -> WorkflowProvenance | Mapping[str, Any]:
        with self._lock:
            self._assert_active()
            if reference is None:
                # Summary form is intentionally keyed by opaque node IDs and
                # contains only safe provenance dictionaries.  A raw source
                # path/value is never retained in this return shape.
                return {
                    node_id: provenance.as_dict()
                    for node_id, provenance in self._provenance.items()
                }
            binding = self._resolve_binding(reference, operation="read")
            return self._provenance[binding.node_id]

    get_provenance = provenance
    provenance_summary = provenance
    safe_provenance = provenance

    def provenance_records(self) -> tuple[WorkflowProvenance, ...]:
        with self._lock:
            self._assert_active()
            return tuple(self._provenance[node_id] for node_id in self._bindings)

    def _projection_bindings(
        self,
        value: Any | None,
        refs: Iterable[WorkflowReference | ProtectedBinding | str] | None,
    ) -> list[ProtectedBinding]:
        if refs is None:
            if isinstance(value, (WorkflowReference, ProtectedBinding, str)):
                refs = [value] if value is not None and (
                    isinstance(value, (WorkflowReference, ProtectedBinding))
                    or value in self._bindings
                    or value in self._object_nodes
                ) else None
            elif value is None:
                refs = [binding.reference for binding in self._bindings.values()]
        if refs is not None:
            result: list[ProtectedBinding] = []
            seen: set[str] = set()
            for ref in refs:
                binding = self._resolve_binding(ref, operation="read")
                if binding.node_id not in seen:
                    seen.add(binding.node_id)
                    result.append(binding)
            return result
        if value is None:
            return []
        # Treat an unregistered value as one local root node.  This keeps the
        # adapter API small while retaining a stable node ID for cloud plans.
        return [self._bindings[self.register(value, kind="payload").node_id]]

    def _safe_metadata(self, metadata: Mapping[str, Any] | None) -> dict[str, Any]:
        if not isinstance(metadata, Mapping):
            return {}
        allowed_keys = {"purpose", "format", "operation", "revision", "schema"}
        allowed_values = {
            "purpose": {"create", "update", "template", "analysis", "design", "debug", "document", "app", "macro"},
            "format": {"xlsx", "xlsm", "xltx", "xltm", "json", "text", "config", "log"},
            "operation": set(ALLOWED_OPERATIONS),
            "schema": {"v1", "v2", "1", "2"},
        }
        safe: dict[str, Any] = {}
        for key, value in metadata.items():
            key_text = _safe_token(key, fallback="")
            if key_text not in allowed_keys:
                continue
            if isinstance(value, str):
                token = _safe_token(value, fallback="")
                if token and (
                    token in allowed_values.get(key_text, set())
                    or (key_text == "revision" and re.fullmatch(r"(?:v)?\d{1,8}(?:\.\d{1,8}){0,3}", token))
                ):
                    safe[key_text] = token
                elif value.strip():
                    # Keep a bounded opaque marker for unknown metadata, not
                    # the caller's literal (which may be a project/customer
                    # identifier).  The digest is advisory only.
                    safe[key_text] = f"hash:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"
            elif type(value) is int and value >= 0:
                safe[key_text] = value
            elif type(value) is bool:
                safe[key_text] = value
        return safe

    def _build_envelope(
        self,
        bindings: Sequence[ProtectedBinding],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        raw_literals: list[str] = []
        for binding in bindings:
            local_value = self._projection_value(copy.deepcopy(binding.raw_value))
            nodes.append(
                {
                    "node_id": binding.node_id,
                    "object_id": binding.object_id,
                    "kind": binding.reference.kind,
                    "role": binding.reference.role,
                    "provenance": binding.provenance.as_dict(),
                    "value": local_value,
                }
            )
            if binding.sensitive:
                # Collect literals from the original bounded local value so
                # static projection keys (``entries``/``name``/``value``)
                # are not mistaken for user data during the final scrub.
                _collect_string_literals(binding.raw_value, raw_literals)
        envelope: dict[str, Any] = {
            "schema_version": WORKFLOW_PROJECTION_SCHEMA,
            "workflow": self.workflow_kind,
            "context_id": self.context_id,
            "nodes": nodes,
            "metadata": self._safe_metadata(metadata),
        }
        # Keep literals local as an implementation detail of the final scrub;
        # callers only ever receive the masked envelope below.
        envelope["__local_literals"] = raw_literals
        return envelope

    def _extract_masked_payload(self, result: Any) -> tuple[Any, tuple[str, ...]]:
        payload = (
            result.get("final_payload")
            if isinstance(result, Mapping)
            else getattr(result, "final_payload", None)
        )
        if payload is None:
            payload = (
                result.get("payload")
                if isinstance(result, Mapping)
                else getattr(result, "payload", None)
            )
        if payload is None:
            raise WorkflowMaskingError("privacy boundary returned no projection")
        findings_value = (
            result.get("findings", ())
            if isinstance(result, Mapping)
            else getattr(result, "findings", ())
        )
        categories: list[str] = []
        for finding in findings_value or ():
            category = (
                finding.get("category")
                if isinstance(finding, Mapping)
                else getattr(finding, "category", None)
            )
            token = _safe_token(category, fallback="")
            if token and token not in categories:
                categories.append(token)
        return payload, tuple(categories)

    def _scrub_literals(self, payload: Any, literals: Sequence[str], node_ids: Sequence[str]) -> Any:
        alias_seed = self.context_id + ":" + ",".join(node_ids)
        alias = f"[WF_VALUE_{hashlib.sha256(alias_seed.encode()).hexdigest()[:12].upper()}]"
        unique_literals = tuple(
            literal
            for literal in dict.fromkeys(literals)
            if isinstance(literal, str) and len(literal) >= _SENSITIVE_LITERAL_MIN_CHARS
        )

        def scrub(value: Any) -> Any:
            if isinstance(value, str):
                output = value
                for literal in unique_literals:
                    if literal in output:
                        output = output.replace(literal, alias)
                return output
            if isinstance(value, Mapping):
                return {key: scrub(item) for key, item in value.items()}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            if isinstance(value, tuple):
                return [scrub(item) for item in value]
            return value

        return scrub(payload)

    def _validate_safe_payload(self, payload: Any) -> Any:
        counters = [0]

        def walk(value: Any, depth: int = 0) -> Any:
            if depth > self.max_depth:
                raise WorkflowProjectionError("cloud projection exceeded depth limit")
            counters[0] += 1
            if counters[0] > self.max_nodes:
                raise WorkflowProjectionError("cloud projection exceeded node limit")
            if value is None or type(value) is bool or type(value) is int:
                return value
            if type(value) is float:
                if not math.isfinite(value):
                    raise WorkflowProjectionError("cloud projection contained non-finite number")
                return value
            if isinstance(value, str):
                if len(value) > self.max_string_chars:
                    raise WorkflowProjectionError("cloud projection exceeded string limit")
                return value
            if isinstance(value, Mapping):
                if len(value) > self.max_items:
                    raise WorkflowProjectionError("cloud projection exceeded item limit")
                checked: dict[str, Any] = {}
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise WorkflowProjectionError("cloud projection contained non-text key")
                    checked[key] = walk(item, depth + 1)
                return checked
            if isinstance(value, (list, tuple)):
                if len(value) > self.max_items:
                    raise WorkflowProjectionError("cloud projection exceeded item limit")
                return [walk(item, depth + 1) for item in value]
            raise WorkflowProjectionError("cloud projection contained unsupported value")

        checked = walk(payload)
        try:
            encoded = json.dumps(
                checked,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except Exception as exc:  # pragma: no cover - walk is JSON-safe
            raise WorkflowProjectionError("cloud projection could not be serialized") from exc
        if len(encoded) > self.max_payload_bytes:
            raise WorkflowProjectionError("cloud projection exceeded payload limit")
        return checked

    def _finalize_projection(
        self,
        bindings: Sequence[ProtectedBinding],
        envelope: Mapping[str, Any],
        result: Any,
    ) -> CloudProjection:
        payload, finding_categories = self._extract_masked_payload(result)
        local_literals = envelope.get("__local_literals", ())
        if not isinstance(local_literals, (list, tuple)):
            local_literals = ()
        node_ids = tuple(binding.node_id for binding in bindings)
        payload = self._scrub_literals(payload, local_literals, node_ids)
        if not isinstance(payload, Mapping):
            # The canonical boundary returns the envelope mapping.  A tiny
            # embedding seam may return one masked scalar; retain it safely
            # under a static field rather than allowing a raw/non-JSON object
            # to become a projection payload.
            payload = {"value": payload}
        if isinstance(payload, Mapping):
            payload = dict(payload)
            payload.pop("__local_literals", None)
            payload["masking"] = "forced_protected"
            if finding_categories:
                payload["redaction_categories"] = list(finding_categories)
        safe = self._validate_safe_payload(payload)
        # Defense in depth: a sensitive literal that survived both the
        # canonical boundary and local scrub causes a fail-closed projection,
        # never a raw fallback.
        # Only node values are compared here.  Static schema/provenance labels
        # (for example a redaction category named ``email``) are intentionally
        # not treated as user literals; all user mapping keys are represented
        # under the node ``value`` tree and are therefore still covered.
        value_projection = (
            [node.get("value") for node in safe.get("nodes", ()) if isinstance(node, Mapping)]
            if isinstance(safe, Mapping) and isinstance(safe.get("nodes"), list)
            else safe
        )
        serialized = json.dumps(value_projection, ensure_ascii=False, sort_keys=True)
        for literal in local_literals:
            if isinstance(literal, str) and len(literal) >= _SENSITIVE_LITERAL_MIN_CHARS:
                if literal in serialized:
                    raise WorkflowProjectionError("sensitive literal survived cloud projection")
        with self._lock:
            self._assert_active()
            return CloudProjection(
                context_id=self.context_id,
                workflow_kind=self.workflow_kind,
                node_ids=node_ids,
                payload=safe,
            )

    def _materialize_sync(self, envelope: Mapping[str, Any]) -> Any:
        boundary = self._masking_boundary
        if boundary is None:
            raise WorkflowContextExpired("workflow protected context is closed")
        method = getattr(boundary, "materialize_one_way_sync", None)
        if not callable(method):
            method = getattr(boundary, "materialize_sync", None)
        local_literals: list[str] = []
        _collect_string_literals(envelope, local_literals)
        semantic_exempt_values = tuple(dict.fromkeys(local_literals))
        try:
            if callable(method):
                try:
                    result = method(
                        envelope,
                        source_kind=f"workflow_{self.workflow_kind}",
                        semantic_exempt_values=semantic_exempt_values,
                    )
                except TypeError:
                    result = method(envelope)
                return _run_awaitable_sync(result) if inspect_is_awaitable(result) else result
            async_method = getattr(boundary, "materialize_one_way", None)
            if not callable(async_method):
                async_method = getattr(boundary, "materialize", None)
            if not callable(async_method):
                raise WorkflowMaskingError("privacy boundary lacks materialization")
            result = async_method(
                envelope,
                source_kind=f"workflow_{self.workflow_kind}",
                semantic_exempt_values=semantic_exempt_values,
            )
            if inspect_is_awaitable(result):
                return _run_awaitable_sync(result)
            return result
        except WorkflowContextError:
            raise
        except PrivacyMaskingError as exc:
            raise WorkflowMaskingError("privacy masking failed") from exc
        except Exception as exc:
            raise WorkflowMaskingError("privacy masking failed") from exc

    async def _materialize_async(self, envelope: Mapping[str, Any]) -> Any:
        boundary = self._masking_boundary
        if boundary is None:
            raise WorkflowContextExpired("workflow protected context is closed")
        method = getattr(boundary, "materialize_one_way", None)
        if not callable(method):
            method = getattr(boundary, "materialize", None)
        local_literals: list[str] = []
        _collect_string_literals(envelope, local_literals)
        semantic_exempt_values = tuple(dict.fromkeys(local_literals))
        try:
            if callable(method):
                try:
                    return await _maybe_await(
                        method(
                            envelope,
                            source_kind=f"workflow_{self.workflow_kind}",
                            semantic_exempt_values=semantic_exempt_values,
                        )
                    )
                except TypeError:
                    return await _maybe_await(method(envelope))
            sync_method = getattr(boundary, "materialize_one_way_sync", None)
            if not callable(sync_method):
                sync_method = getattr(boundary, "materialize_sync", None)
            if callable(sync_method):
                return sync_method(
                    envelope,
                    source_kind=f"workflow_{self.workflow_kind}",
                    semantic_exempt_values=semantic_exempt_values,
                )
            raise WorkflowMaskingError("privacy boundary lacks materialization")
        except WorkflowContextError:
            raise
        except PrivacyMaskingError as exc:
            raise WorkflowMaskingError("privacy masking failed") from exc
        except Exception as exc:
            raise WorkflowMaskingError("privacy masking failed") from exc

    def project(
        self,
        value: Any | None = None,
        *,
        refs: Iterable[WorkflowReference | ProtectedBinding | str] | None = None,
        nodes: Iterable[WorkflowReference | ProtectedBinding | str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        purpose: str | None = None,
        operation: str | None = None,
    ) -> CloudProjection:
        """Create one bounded, masked Cloud Advisor projection.

        ``refs``/``nodes`` select already-registered values.  Passing a raw
        value registers one local root node.  No caller-controlled metadata or
        raw mapping is copied into the returned projection.
        """

        with self._lock:
            self._assert_active()
            if refs is not None and nodes is not None:
                raise WorkflowProjectionError("projection refs and nodes are mutually exclusive")
            bindings = self._projection_bindings(value, refs if refs is not None else nodes)
            merged_metadata = dict(metadata or {}) if isinstance(metadata, Mapping) else {}
            if purpose is not None:
                merged_metadata.setdefault("purpose", purpose)
            if operation is not None:
                merged_metadata.setdefault("operation", operation)
            envelope = self._build_envelope(bindings, metadata=merged_metadata)
        result = self._materialize_sync(envelope)
        return self._finalize_projection(bindings, envelope, result)

    safe_projection = project
    project_cloud = project
    project_payload = project
    projection = project
    materialize = project

    async def project_async(
        self,
        value: Any | None = None,
        *,
        refs: Iterable[WorkflowReference | ProtectedBinding | str] | None = None,
        nodes: Iterable[WorkflowReference | ProtectedBinding | str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        purpose: str | None = None,
        operation: str | None = None,
    ) -> CloudProjection:
        with self._lock:
            self._assert_active()
            if refs is not None and nodes is not None:
                raise WorkflowProjectionError("projection refs and nodes are mutually exclusive")
            bindings = self._projection_bindings(value, refs if refs is not None else nodes)
            merged_metadata = dict(metadata or {}) if isinstance(metadata, Mapping) else {}
            if purpose is not None:
                merged_metadata.setdefault("purpose", purpose)
            if operation is not None:
                merged_metadata.setdefault("operation", operation)
            envelope = self._build_envelope(bindings, metadata=merged_metadata)
        result = await self._materialize_async(envelope)
        return self._finalize_projection(bindings, envelope, result)

    safe_projection_async = project_async
    materialize_async = project_async

    def mask_text(self, text: str, **kwargs: Any) -> str:
        """Return only the masked value for small adapter/UI previews."""

        if not isinstance(text, str):
            raise WorkflowProjectionError("workflow text must be a string")
        projection = self.project(text, **kwargs)
        nodes = projection.payload.get("nodes")
        if isinstance(nodes, list) and nodes:
            first = nodes[0]
            if isinstance(first, Mapping):
                value = first.get("value")
                if isinstance(value, str):
                    return value
        return str(projection)

    def project_text(self, text: str, **kwargs: Any) -> CloudProjection:
        if not isinstance(text, str):
            raise WorkflowProjectionError("workflow text must be a string")
        return self.project(text, **kwargs)

    def _extract_reference_from_mapping(self, value: Mapping[str, Any]) -> tuple[str, str] | None:
        context_value = value.get("context_id")
        if context_value is not None:
            if not isinstance(context_value, str) or context_value != self.context_id:
                raise StaleWorkflowContext("cloud reference belongs to another context")
        found: list[tuple[str, str]] = []
        for key in REFERENCE_KEYS:
            candidate = value.get(key)
            if candidate is not None:
                if not isinstance(candidate, str):
                    raise UnknownWorkflowReference("workflow reference is unknown")
                found.append((key, candidate))
        if not found:
            return None
        if len(found) > 1:
            # Multiple equivalent fields are accepted only when they identify
            # the same local binding; disagreement is ambiguous and denied.
            identifiers = {candidate for _key, candidate in found}
            if len(identifiers) != 1:
                raise AmbiguousWorkflowReference("cloud response contained conflicting references")
        return found[0]

    def rebind(
        self,
        value: Any,
        *,
        operation: str | None = None,
    ) -> Any:
        """Rebind only allowlisted cloud references to local raw values.

        A direct reference resolves to a defensive raw copy.  A structured
        advisory operation returns :class:`LocalRebinding`, retaining the
        operation/provenance while keeping the raw value local.  Unknown IDs,
        stale context IDs and disallowed operations fail closed.
        """

        with self._lock:
            self._assert_active()
            if isinstance(value, LocalRebinding):
                self._validate_reference_context(value.reference)
                return copy.deepcopy(value.value)
            if isinstance(value, WorkflowReference):
                return self.resolve(value, operation=operation or "read")
            if isinstance(value, ProtectedBinding):
                return self.resolve(value.reference, operation=operation or "read")
            if isinstance(value, str):
                # Strings that look like workflow IDs are never treated as
                # ordinary text when rebinding; this catches forged/stale IDs.
                if value.startswith("wf_"):
                    return self.resolve(value, operation=operation or "read")
                return value
            if isinstance(value, Mapping):
                extracted = self._extract_reference_from_mapping(value)
                if extracted is not None:
                    _key, identifier = extracted
                    op_value = operation or value.get("operation") or value.get("action") or "read"
                    normalized_operation = _normalize_operation(op_value)
                    binding = self._resolve_binding(identifier, operation=normalized_operation)
                    # A bare reference envelope resolves directly.  Extra
                    # advisory fields are represented as a local operation so
                    # they cannot accidentally become executable instructions.
                    non_reference_keys = set(value) - REFERENCE_KEYS - {
                        "context_id",
                        "operation",
                        "action",
                        "provenance",
                    }
                    if not non_reference_keys:
                        return copy.deepcopy(binding.raw_value)
                    return LocalRebinding(
                        reference=binding.reference,
                        operation=normalized_operation,
                        value=copy.deepcopy(binding.raw_value),
                        provenance=binding.provenance,
                    )
                # No allowlisted reference: preserve advisory structure but
                # still bound-check it and recursively reject forged IDs.
                rebound_mapping: dict[Any, Any] = {}
                for key, item in value.items():
                    if key == "context_id":
                        rebound_mapping[key] = item
                        continue
                    if isinstance(item, str) and item.startswith("wf_"):
                        # ``resolve`` returns a local value for a known ID and
                        # raises for an unknown/forged one.  The value is not
                        # retained here; recursion below preserves ordinary
                        # advisory text and only references in allowlisted
                        # fields are rebound.
                        self._resolve_binding(item, operation=operation or "read")
                        rebound_mapping[key] = item
                    else:
                        rebound_mapping[key] = self.rebind(item, operation=operation)
                return rebound_mapping
            if isinstance(value, list):
                return [self.rebind(item, operation=operation) for item in value]
            if isinstance(value, tuple):
                return tuple(self.rebind(item, operation=operation) for item in value)
            # Cloud output is untrusted.  Never call repr or silently coerce an
            # arbitrary object into a local value.
            raise UnknownWorkflowReference("cloud response value is unsupported")

    rebind_local = rebind
    rebind_response = rebind
    restore = rebind
    restore_text = rebind


WorkflowProtectedContext = ProtectedWorkflowContext
ProtectedContext = ProtectedWorkflowContext
WorkflowContext = ProtectedWorkflowContext


def new_protected_workflow_context(
    workflow_kind: str = "workflow",
    config: Any | None = None,
    **kwargs: Any,
) -> ProtectedWorkflowContext:
    """Construct one request-scoped context (convenience factory)."""

    return ProtectedWorkflowContext(workflow_kind, config, **kwargs)


new_workflow_context = new_protected_workflow_context
create_workflow_context = new_protected_workflow_context


def _new_id(prefix: str) -> str:
    return f"wf_{prefix}_{secrets.token_hex(16)}"


def _validate_id(identifier: str, expected_prefix: str) -> None:
    if not isinstance(identifier, str) or not _ID_RE.fullmatch(identifier):
        raise WorkflowBindingError("workflow ID is malformed")
    if not identifier.startswith(f"wf_{expected_prefix}_"):
        raise WorkflowBindingError("workflow ID has an invalid type")


def _safe_token(value: Any, *, fallback: str = "unknown") -> str:
    if not isinstance(value, str):
        return fallback
    token = value.strip().casefold()
    if _TOKEN_RE.fullmatch(token):
        return token
    return fallback


def _positive_limit(value: Any, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = fallback
    return max(1, number)


def _normalize_operation(value: Any) -> str:
    token = _safe_token(value, fallback="")
    if token not in ALLOWED_OPERATIONS:
        raise WorkflowBindingError("workflow operation is not allowlisted")
    return token


def _normalize_operations(values: Iterable[str] | None) -> tuple[str, ...]:
    if values is None:
        return ("read", "reference")
    if isinstance(values, (str, bytes, bytearray, memoryview)):
        raise WorkflowBindingError("workflow operation allowlist is malformed")
    result: list[str] = []
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise WorkflowBindingError("workflow operation allowlist is malformed") from exc
    for value in iterator:
        token = _normalize_operation(value)
        if token not in result:
            result.append(token)
    if not result:
        raise WorkflowBindingError("workflow operation allowlist is empty")
    return tuple(result)


def _build_provenance(
    *,
    source_kind: Any,
    source_ref: Any | None,
    version: Any | None,
    operation: str,
    allowed_operations: Iterable[str],
) -> WorkflowProvenance:
    normalized_source = _safe_token(source_kind, fallback="unknown")
    source_digest = ""
    if source_ref is not None:
        if isinstance(source_ref, (bytes, bytearray, memoryview)):
            raise WorkflowBindingError("binary provenance reference is unsupported")
        try:
            source_text = source_ref if isinstance(source_ref, str) else json.dumps(source_ref, ensure_ascii=False, sort_keys=True, default="")
        except Exception as exc:
            raise WorkflowBindingError("provenance reference is malformed") from exc
        if not isinstance(source_text, str) or len(source_text) > 16_384:
            raise WorkflowBindingError("provenance reference exceeded limit")
        source_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    if version is None:
        normalized_version = ""
    elif isinstance(version, (str, int)) and not isinstance(version, bool):
        version_text = str(version).strip()
        if _TOKEN_RE.fullmatch(version_text.casefold()):
            normalized_version = version_text[:128]
        elif version_text:
            normalized_version = hashlib.sha256(version_text.encode("utf-8")).hexdigest()
        else:
            normalized_version = ""
    else:
        normalized_version = ""
    normalized_operation = _normalize_operation(operation)
    allowed = _normalize_operations(allowed_operations)
    if normalized_operation not in allowed:
        raise WorkflowBindingError("provenance operation is not allowlisted")
    return WorkflowProvenance(
        source_kind=normalized_source,
        source_ref_hash=source_digest,
        version=normalized_version,
        operation=normalized_operation,
        allowed_operations=allowed,
    )


def _collect_string_literals(value: Any, output: list[str]) -> None:
    if isinstance(value, str):
        output.append(value)
        # A bounded JSON/text blob may contain sensitive values inside an
        # escaped string, so collecting only the outer blob would not scrub
        # those values from the foundation projection.  Keep token fragments
        # local and use them solely for the final one-way scrub.
        output.extend(match.group(0) for match in _LITERAL_TOKEN_RE.finditer(value))
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = None
            if parsed is not None:
                _collect_string_literals(parsed, output)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                output.append(key)
            _collect_string_literals(item, output)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_string_literals(item, output)


def _canonical_for_fingerprint(value: Any) -> Any:
    """Return a deterministic JSON shape without retaining raw key objects."""

    if isinstance(value, Mapping):
        canonical: dict[str, Any] = {}
        for key, item in value.items():
            key_text = key if isinstance(key, str) else str(key)
            if key_text in canonical:
                # The bounded local copy already rejects unsupported keys;
                # this guard prevents mixed ``1``/``"1"`` keys from silently
                # collapsing the object fingerprint.
                raise WorkflowBindingError("workflow mapping keys are ambiguous")
            canonical[key_text] = _canonical_for_fingerprint(item)
        return canonical
    if isinstance(value, tuple):
        return [_canonical_for_fingerprint(item) for item in value]
    if isinstance(value, list):
        return [_canonical_for_fingerprint(item) for item in value]
    return value


def inspect_is_awaitable(value: Any) -> bool:
    return inspect.isawaitable(value)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect_is_awaitable(value) else value


def _run_awaitable_sync(awaitable: Awaitable[Any]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    future: concurrent.futures.Future[Any] = concurrent.futures.Future()

    def runner() -> None:
        try:
            future.set_result(asyncio.run(awaitable))
        except BaseException as exc:  # noqa: BLE001
            future.set_exception(exc)

    threading.Thread(
        target=runner,
        name="aoitalk-workflow-projection",
        daemon=True,
    ).start()
    try:
        return future.result(timeout=30.0)
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise WorkflowMaskingError("workflow masking timed out") from exc


__all__ = [
    "ALLOWED_OPERATIONS",
    "AmbiguousWorkflowReference",
    "CloudProjection",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_ITEMS",
    "DEFAULT_MAX_NODES",
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "DEFAULT_MAX_STRING_CHARS",
    "LocalRebinding",
    "ProtectedBinding",
    "ProtectedContext",
    "ProtectedNode",
    "ProtectedObject",
    "ProtectedWorkflowNode",
    "ProtectedWorkflowObject",
    "ProtectedWorkflowContext",
    "RawBinding",
    "ReboundValue",
    "REFERENCE_KEYS",
    "SafeWorkflowProjection",
    "WorkflowProjection",
    "StaleWorkflowContext",
    "WorkflowContextStale",
    "UnknownWorkflowReference",
    "UnknownReference",
    "WorkflowBindingError",
    "WorkflowContextError",
    "WorkflowContextExpired",
    "WorkflowContext",
    "WorkflowMaskingBoundary",
    "WorkflowMaskingError",
    "WorkflowNodeRef",
    "WorkflowObjectRef",
    "WorkflowProjectionError",
    "ProjectionError",
    "WorkflowProtectedContext",
    "WorkflowProvenance",
    "WorkflowReference",
    "get_current_workflow_context",
    "create_workflow_context",
    "new_protected_workflow_context",
    "new_workflow_context",
    "reset_current_workflow_context",
    "set_current_workflow_context",
    "workflow_context_scope",
]
