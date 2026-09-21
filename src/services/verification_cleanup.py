"""Bounded cleanup of explicitly-proven verification/test fixtures.

This module is intentionally *not* a general database garbage collector.  A
cleanup operation is driven by a frozen manifest produced by a verification
run.  Every target is identified by an exact UUID (or another exact opaque
identifier), the manifest is digest-bound, and rows which are not in the
manifest are never selected for deletion.  The coordinator delegates the
normal task/project lifecycle to the canonical repositories first and only
then removes the already snapshotted dependent graph.

The provenance service is released independently from this coordinator.  To
keep rolling deployments import-safe, provenance calls are resolved lazily
and a small compatibility adapter accepts the method spellings used by both
the initial and final provenance implementations.  When no provenance
service is installed the existing append-only ``ContentDeletionEvent`` audit
is used as a conservative fallback; it never contains prompt or document
contents.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import (
    AgentRun,
    AgentRunEdge,
    AgentRunEvent,
    AgentRunToolCall,
    App,
    AppGrant,
    AppJob,
    ConversationArchive,
    ConversationDispatchOutbox,
    ConversationHistory,
    ConversationMessage,
    ConversationParticipant,
    ConversationSession,
    ContentDeletionEvent,
    ContextMemory,
    ContextMemoryAudit,
    ClipIngestReceipt,
    DocsCandidate,
    DocsClipIngestJob,
    DocsLibrary,
    KnowledgeAiSuggestion,
    KnowledgeAttachment,
    KnowledgeEdge,
    KnowledgeEditEvent,
    KnowledgeField,
    KnowledgeFieldValue,
    KnowledgeImportItem,
    KnowledgeImportJob,
    KnowledgeNode,
    KnowledgeNodePlacement,
    KnowledgeNodeShare,
    KnowledgeNodeSupertag,
    KnowledgeRevision,
    KnowledgeSearchIndex,
    KnowledgeSource,
    KnowledgeSourcePermission,
    KnowledgeSupertag,
    KnowledgeSupertagField,
    LocalTask,
    NotificationDelivery,
    Project,
    ProjectApp,
    ProjectJoinRequest,
    ProjectKnowledgeRef,
    ProjectMember,
    ProjectNotificationSetting,
    ProjectOverview,
    ProjectOverviewRefreshJob,
    ProjectQaEntry,
    ProjectSchedulePhase,
    ProjectStorageOperation,
    RecordAttachment,
    RecordEvent,
    RecordField,
    RecordRow,
    RecordTable,
    RecordView,
    ScopedMemoryJob,
    SkillProposal,
    SkillProposalHistory,
    SkillUsageReceipt,
    Space,
    Task,
    TaskActivity,
    TaskAppLink,
    TaskAssignee,
    TaskAttachment,
    TaskComment,
    TaskDependency,
    TaskOccurrence,
    TaskRecurrenceRule,
    TaskRecurrenceScheduleSegment,
    TaskReference,
    TaskRelation,
    TaskSchedulePlacement,
    TaskTag,
    TaskEvent,
    TaskExecutionSession,
    TimeEntry,
    User,
    HeartbeatRunHistory,
    HeartbeatRunState,
)
from ..memory.project_repository import ProjectRepository
from ..memory.user_repository import UserRepository
from .task_management_service import TaskManagementService

try:  # ECC model is optional in lightweight migrations/tests.
    from ..models.ecc_models import TokenUsage
except Exception:  # pragma: no cover - import-safe during partial upgrades
    TokenUsage = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


MAX_MANIFEST_BYTES = 1_048_576
MANIFEST_SCHEMA_VERSION = 1
_ALLOWED_ENTITY_TYPES = frozenset({"project", "task", "user"})
_ALLOWED_CATEGORIES = frozenset({"A", "B", "C"})
# A project cleanup never owns its enclosing Space.  Keep the accepted
# disposition vocabulary intentionally narrow so an operator cannot submit a
# manifest that *claims* a Space will be deleted while this coordinator leaves
# it behind (or vice versa).  ``preserve`` is retained as a short alias for
# manifests produced by older verification runners.
_ALLOWED_SPACE_DISPOSITIONS = frozenset({"preserve_shared_space", "preserve"})
# Row assertions are deliberately finite.  Unknown assertion keys are
# rejected instead of silently ignored, otherwise a malformed manifest could
# appear evidence-backed while the field carrying the evidence had no effect.
_ALLOWED_ASSERTION_KEYS = frozenset(
    {
        "name",
        "title",
        "slug",
        "username",
        "role",
        "is_active",
        "owner_id",
        "space_id",
        "project_id",
        "knowledge_node_id",
        "docs_library_id",
        "library_id",
        "parent_id",
        "root_page_id",
        "deleted_at",
        "deletion_batch_id",
    }
)
# Legacy one-time cleanup is intentionally a finite registry, not a
# ``<user-supplied-key>.json`` file reader.  Add a new key only with a reviewed
# manifest and corresponding focused evidence; HTTP selectors cannot probe
# arbitrary files under the repository.
_LEGACY_MANIFEST_REGISTRY = frozenset({"wiqa_20260830_epic"})


class VerificationCleanupError(RuntimeError):
    """A fail-closed validation or cleanup error.

    ``status_code`` is suitable for the admin BFF (4xx for operator input,
    409 for a stale/mutated target, and 500 for an unexpected database
    failure).  ``details`` intentionally contains identifiers and counts only.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "verification_cleanup_failed",
        status_code: int = 409,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = dict(details or {})


def _string_uuid(value: Any, *, field_name: str) -> str:
    """Normalize an identifier while preserving opaque IDs for compatibility."""

    if isinstance(value, UUID):
        return str(value)
    if value is None:
        raise VerificationCleanupError(
            f"{field_name} is required",
            code="manifest_missing_identity",
            status_code=422,
        )
    text = str(value).strip()
    if not text or len(text) > 512:
        raise VerificationCleanupError(
            f"{field_name} is invalid",
            code="manifest_invalid_identity",
            status_code=422,
        )
    return text


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _as_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise VerificationCleanupError(
            f"{label} must be an object",
            code="manifest_invalid_shape",
            status_code=422,
        )
    return value


def _coerce_count_map(value: Any, *, label: str) -> dict[str, int]:
    if value is None:
        return {}
    mapping = _as_mapping(value, label=label)
    result: dict[str, int] = {}
    aliases = {
        "project": "projects",
        "task": "tasks",
        "user": "users",
        "knowledge_node": "knowledge_nodes",
        "session": "sessions",
        "message": "messages",
    }
    for key, raw in mapping.items():
        if not isinstance(key, str) or key not in _ALLOWED_ENTITY_TYPES and key not in {
            "projects",
            "tasks",
            "users",
            "knowledge_nodes",
            "sessions",
            "messages",
        }:
            raise VerificationCleanupError(
                f"{label} has an unknown key",
                code="manifest_invalid_counts",
                status_code=422,
                details={"key": str(key)},
            )
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise VerificationCleanupError(
                f"{label}.{key} must be a non-negative integer",
                code="manifest_invalid_counts",
                status_code=422,
            )
        normalized_key = aliases.get(key, key)
        if normalized_key in result and result[normalized_key] != raw:
            raise VerificationCleanupError(
                f"{label} has conflicting aliases",
                code="manifest_invalid_counts",
                status_code=422,
                details={"key": normalized_key},
            )
        result[normalized_key] = raw
    return result


@dataclass(frozen=True)
class ManifestEntry:
    """One exact, immutable cleanup target."""

    entity_type: str
    entity_id: str
    category: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    # Positive row-level evidence (for example an exact project slug or a
    # deletion batch id).  These assertions are *additional* fences; the
    # cleanup selector remains the opaque UUID and never falls back to names.
    assertions: Mapping[str, Any] = field(default_factory=dict)
    graph: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    counts: Mapping[str, int] = field(default_factory=dict)
    owner_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "category": self.category,
            "metadata": dict(self.metadata),
            **({"assertions": dict(self.assertions)} if self.assertions else {}),
            "graph": {key: list(value) for key, value in self.graph.items()},
            "counts": dict(self.counts),
            **({"owner_id": self.owner_id} if self.owner_id else {}),
        }


@dataclass(frozen=True)
class CleanupManifest:
    """Frozen, digestable manifest consumed by the coordinator."""

    run_id: str
    source: str
    disposable: bool
    schema_version: int
    created_at: str | None
    entries: tuple[ManifestEntry, ...]
    manifest_id: str | None = None
    counts: Mapping[str, int] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    anchors: Mapping[str, Any] = field(default_factory=dict)
    workspace: Mapping[str, str] = field(default_factory=dict)
    # Evidence for related rows (for example the exact task title, project
    # pointer, or prior deletion batch recorded in a legacy inventory).  This
    # is retained in the signed/digest-bound payload and checked by the
    # project graph snapshot; it is never used as a selector.
    entity_evidence: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    digest: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            **({"manifest_id": self.manifest_id} if self.manifest_id else {}),
            "run_id": self.run_id,
            "source": self.source,
            "disposable": self.disposable,
            "created_at": self.created_at,
            "entries": [entry.to_dict() for entry in self.entries],
            "counts": dict(self.counts),
            "metadata": dict(self.metadata),
            **({"anchors": dict(self.anchors)} if self.anchors else {}),
            **({"workspace": dict(self.workspace)} if self.workspace else {}),
            **({"entity_evidence": dict(self.entity_evidence)} if self.entity_evidence else {}),
        }

    def with_digest(self) -> "CleanupManifest":
        return CleanupManifest(**{**self.__dict__, "digest": _sha256(self.payload())})

    def to_dict(self) -> dict[str, Any]:
        payload = self.payload()
        payload["digest"] = self.digest or _sha256(payload)
        return payload


def _entry_graph(value: Any, *, label: str) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    mapping = _as_mapping(value, label=label)
    graph: dict[str, tuple[str, ...]] = {}
    for raw_key, raw_values in mapping.items():
        key = str(raw_key)
        if not isinstance(raw_values, Sequence) or isinstance(raw_values, (str, bytes)):
            raise VerificationCleanupError(
                f"{label}.{key} must be an array",
                code="manifest_invalid_graph",
                status_code=422,
            )
        values = tuple(sorted({_string_uuid(item, field_name=f"{label}.{key}") for item in raw_values}))
        graph[key] = values
    return graph


def _entry_assertions(value: Any, *, label: str) -> dict[str, Any]:
    """Validate exact row assertions carried by a manifest entry.

    Assertions are intentionally scalar and bounded.  They are never used to
    *find* a row; the UUID entry identity is always selected first and the
    assertions only decide whether that exact row still matches the frozen
    evidence.
    """

    if value is None:
        return {}
    mapping = _as_mapping(value, label=label)
    result: dict[str, Any] = {}
    for raw_key, raw_value in mapping.items():
        key = str(raw_key).strip().casefold()
        if key not in _ALLOWED_ASSERTION_KEYS:
            raise VerificationCleanupError(
                f"{label} contains an unsupported assertion",
                code="manifest_invalid_assertion",
                status_code=422,
                details={"key": key},
            )
        if isinstance(raw_value, (Mapping, Sequence)) and not isinstance(
            raw_value, (str, bytes, bytearray)
        ):
            raise VerificationCleanupError(
                f"{label}.{key} must be scalar",
                code="manifest_invalid_assertion",
                status_code=422,
            )
        if raw_value is None:
            # Null is useful as an explicit assertion for an optional pointer
            # (for example ``knowledge_node_id``), so retain it.
            result[key] = None
        elif isinstance(raw_value, (bool, int, float, str, UUID)):
            if isinstance(raw_value, str) and len(raw_value) > 1024:
                raise VerificationCleanupError(
                    f"{label}.{key} is too long",
                    code="manifest_invalid_assertion",
                    status_code=422,
                )
            result[key] = str(raw_value) if isinstance(raw_value, UUID) else raw_value
        else:
            raise VerificationCleanupError(
                f"{label}.{key} has an unsupported value",
                code="manifest_invalid_assertion",
                status_code=422,
            )
    return result


def _manifest_anchors(value: Any) -> dict[str, Any]:
    """Validate the optional exact project/knowledge anchor evidence."""

    if value is None:
        return {}
    anchors = _as_mapping(value, label="anchors")
    result: dict[str, Any] = {}
    allowed_sections = {"project", "knowledge_root", "space"}
    for raw_section, raw_values in anchors.items():
        section = str(raw_section).strip().casefold()
        if section not in allowed_sections:
            raise VerificationCleanupError(
                "anchors contains an unsupported section",
                code="manifest_invalid_anchor",
                status_code=422,
            )
        section_map = _as_mapping(raw_values, label=f"anchors.{section}")
        normalized: dict[str, Any] = {}
        for raw_key, raw_value in section_map.items():
            key = str(raw_key).strip().casefold()
            if key not in {
                "id",
                "metadata_key",
                "metadata_value",
                "library_id",
                "docs_library_id",
                "parent_id",
                "project_id",
                "space_id",
                "owner_id",
                "owner_user_id",
                "disposition",
            }:
                raise VerificationCleanupError(
                    "anchors contains an unsupported field",
                    code="manifest_invalid_anchor",
                    status_code=422,
                    details={"section": section, "key": key},
                )
            if isinstance(raw_value, (Mapping, Sequence)) and not isinstance(
                raw_value, (str, bytes, bytearray)
            ):
                raise VerificationCleanupError(
                    "anchor values must be scalar",
                    code="manifest_invalid_anchor",
                    status_code=422,
                )
            if raw_value is not None and not isinstance(raw_value, (str, bool, int, float, UUID)):
                raise VerificationCleanupError(
                    "anchor value has an unsupported type",
                    code="manifest_invalid_anchor",
                    status_code=422,
                )
            normalized[key] = str(raw_value) if isinstance(raw_value, UUID) else raw_value
        result[section] = normalized
    return result


def _entry_records(raw: Any, *, default_category: str | None = None) -> list[Mapping[str, Any]]:
    """Expand list and legacy ``*_ids`` manifest forms into records."""

    records: list[Mapping[str, Any]] = []
    if raw is None:
        return records
    if isinstance(raw, Mapping):
        # ``entities`` has historically been accepted as either a list or a
        # type->IDs map.  Preserve exact IDs while adding the type.
        for key, value in raw.items():
            if key in _ALLOWED_ENTITY_TYPES or key.rstrip("s") in _ALLOWED_ENTITY_TYPES:
                entity_type = key.rstrip("s")
                if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                    records.extend(
                        {"entity_type": entity_type, "entity_id": item, "category": default_category}
                        for item in value
                    )
                    continue
            if isinstance(value, Mapping) and ("entity_id" in value or "id" in value):
                records.append({"entity_type": key, **value})
                continue
            raise VerificationCleanupError(
                "entities has an invalid entry",
                code="manifest_invalid_entities",
                status_code=422,
            )
        return records
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise VerificationCleanupError(
            "entities must be an array or object",
            code="manifest_invalid_entities",
            status_code=422,
        )
    for item in raw:
        if not isinstance(item, Mapping):
            raise VerificationCleanupError(
                "every entity must be an object",
                code="manifest_invalid_entities",
                status_code=422,
            )
        records.append(item)
    return records


def _manifest_from_mapping(raw: Mapping[str, Any]) -> CleanupManifest:
    forbidden = {"where", "query", "regex", "name_pattern", "title_pattern", "older_than", "inactive", "status", "all"}
    if forbidden.intersection(str(key) for key in raw):
        raise VerificationCleanupError(
            "name/status/age/broad selectors are not allowed",
            code="manifest_broad_selector",
            status_code=403,
        )
    # ``VerificationProvenanceService.preview_run`` returns a read-only
    # projection (``run`` + ``artifacts``), not the operator manifest shape.
    # Normalize that projection here so the same digest/identity fences apply
    # to both legacy files and server-created verification runs.
    if isinstance(raw.get("run"), Mapping) and isinstance(raw.get("artifacts"), Sequence):
        run_projection = raw["run"]
        raw = {
            "schema_version": run_projection.get("schema_version", MANIFEST_SCHEMA_VERSION),
            "run_id": run_projection.get("run_id"),
            "source": run_projection.get("source") or run_projection.get("harness"),
            "disposable": run_projection.get("disposable"),
            "created_at": run_projection.get("created_at"),
            "classification": "A",
            "entities": [
                {
                    "entity_type": artifact.get("entity_type"),
                    "entity_id": artifact.get("entity_id"),
                    "category": "A",
                    "metadata": artifact.get("metadata", {}),
                }
                for artifact in raw.get("artifacts", ())
                if isinstance(artifact, Mapping)
            ],
            "counts": raw.get("counts", {}),
            "metadata": run_projection.get("metadata", {}),
        }
    schema_version = raw.get("schema_version", raw.get("version", MANIFEST_SCHEMA_VERSION))
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != MANIFEST_SCHEMA_VERSION:
        raise VerificationCleanupError(
            "unsupported verification manifest schema",
            code="manifest_schema_unsupported",
            status_code=422,
        )
    run_id = _string_uuid(raw.get("run_id", raw.get("verification_run_id")), field_name="run_id")
    source = raw.get("source", raw.get("producer", raw.get("source_path")))
    if not isinstance(source, str) or not source.strip() or len(source.strip()) > 255:
        raise VerificationCleanupError(
            "source is required",
            code="manifest_missing_source",
            status_code=422,
        )
    disposable = raw.get("disposable", raw.get("disposable_fixture"))
    if disposable is not True:
        raise VerificationCleanupError(
            "only disposable verification manifests may be cleaned",
            code="manifest_not_disposable",
            status_code=403,
        )
    created_at = raw.get("created_at")
    if created_at is not None and (not isinstance(created_at, str) or len(created_at) > 80):
        raise VerificationCleanupError(
            "created_at is invalid",
            code="manifest_invalid_created_at",
            status_code=422,
        )

    top_category = raw.get("category", raw.get("classification"))
    if top_category is not None and top_category not in _ALLOWED_CATEGORIES:
        raise VerificationCleanupError(
            "manifest category is invalid",
            code="manifest_invalid_category",
            status_code=422,
        )

    records: list[Mapping[str, Any]] = []
    entities = raw.get("entities", raw.get("entries", raw.get("artifacts", raw.get("targets"))))
    records.extend(_entry_records(entities, default_category=top_category))
    # One-time operator manifests commonly use exact UUID arrays.  These are
    # expanded only when the array is present; no name/status/age inference is
    # ever performed.
    for entity_type in _ALLOWED_ENTITY_TYPES:
        key = f"{entity_type}_ids"
        if key in raw:
            values = raw[key]
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                raise VerificationCleanupError(
                    f"{key} must be an array",
                    code="manifest_invalid_entities",
                    status_code=422,
                )
            records.extend(
                {"entity_type": entity_type, "entity_id": value, "category": top_category}
                for value in values
            )
    if not records:
        raise VerificationCleanupError(
            "manifest has no exact cleanup targets",
            code="manifest_empty",
            status_code=422,
        )

    # Legacy one-time manifests keep their frozen selector under
    # ``selection`` while carrying one project entity record with metadata
    # assertions.  Promote those exact task/user IDs to the project's graph
    # fence; they are not independent targets and therefore do not undergo a
    # second provenance lookup.
    selection = raw.get("selection")
    if selection is not None:
        selection = _as_mapping(selection, label="selection")
        selected_graph: dict[str, tuple[str, ...]] = {}
        for key, graph_key in (("projects", "projects"), ("tasks", "tasks"), ("users", "users")):
            values = selection.get(key)
            if values is None:
                raise VerificationCleanupError(
                    f"selection.{key} is required",
                    code="manifest_invalid_selection",
                    status_code=422,
                )
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                raise VerificationCleanupError(
                    f"selection.{key} must be an array",
                    code="manifest_invalid_selection",
                    status_code=422,
                )
            selected_graph[graph_key] = tuple(sorted({_string_uuid(item, field_name=f"selection.{key}") for item in values}))
        for idx, record in enumerate(records):
            if str(record.get("entity_type", record.get("type", ""))).rstrip("s") != "project":
                continue
            if record.get("graph") is None:
                records[idx] = {**record, "graph": {"tasks": list(selected_graph["tasks"])}}

    top_metadata = raw.get("metadata", raw.get("provenance", {}))
    if top_metadata is None:
        top_metadata = {}
    top_metadata = _as_mapping(top_metadata, label="metadata")

    anchors = _manifest_anchors(raw.get("anchors"))

    # Workspace paths are optional forensic evidence for one-time legacy
    # manifests.  They are retained in the frozen digest but are only acted
    # upon after strict path/owner/project validation in the coordinator.
    raw_workspace = raw.get("workspace", {})
    if raw_workspace is None:
        raw_workspace = {}
    if not isinstance(raw_workspace, Mapping):
        raise VerificationCleanupError(
            "workspace must be an object",
            code="manifest_invalid_workspace",
            status_code=422,
        )
    workspace: dict[str, str] = {}
    for workspace_key in ("project_root", "legacy_file"):
        value = raw_workspace.get(workspace_key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > 1024:
            raise VerificationCleanupError(
                "workspace path is invalid",
                code="manifest_invalid_workspace",
                status_code=422,
            )
        normalized = value.strip().replace("\\", "/")
        path_parts = [part for part in normalized.split("/") if part]
        if normalized.startswith("/") or any(":" in part for part in path_parts) or ".." in path_parts or "\x00" in normalized:
            raise VerificationCleanupError(
                "workspace path must be a safe relative path",
                code="manifest_invalid_workspace",
                status_code=422,
            )
        workspace[workspace_key] = "/".join(path_parts)
    raw_counts = raw.get("expected_counts")
    if isinstance(raw_counts, Mapping):
        # ``counts`` may be a compact summary while expected_counts is the
        # frozen forensic fence.  Merge compact keys without overriding the
        # authoritative expected values.
        compact_counts = _coerce_count_map(raw.get("counts"), label="counts")
        top_counts = {**compact_counts, **_coerce_count_map(raw_counts, label="expected_counts")}
    else:
        top_counts = _coerce_count_map(raw.get("counts"), label="counts")
    graph_by_id = raw.get("graph", raw.get("expected_graph", {}))
    if graph_by_id is None:
        graph_by_id = {}
    if not isinstance(graph_by_id, Mapping):
        raise VerificationCleanupError(
            "graph must be an object",
            code="manifest_invalid_graph",
            status_code=422,
        )

    evidence_by_identity: dict[tuple[str, str], dict[str, Mapping[str, Any]]] = {}
    entity_evidence = raw.get("entity_evidence")
    if isinstance(entity_evidence, Mapping):
        for raw_type, raw_items in entity_evidence.items():
            entity_type = str(raw_type).rstrip("s")
            if entity_type not in _ALLOWED_ENTITY_TYPES or not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
                continue
            for item in raw_items:
                if not isinstance(item, Mapping):
                    continue
                raw_id = item.get("id", item.get("entity_id"))
                if raw_id is None:
                    continue
                metadata_assertions = item.get("metadata_assertions", item.get("metadata", {}))
                if not isinstance(metadata_assertions, Mapping):
                    metadata_assertions = {}
                # Keep row assertions separate from JSON metadata assertions.
                # The former are checked against ORM columns below; treating
                # e.g. ``name`` as project metadata would make the evidence
                # silently ineffective.
                row_assertions = {
                    str(key): value
                    for key, value in item.items()
                    if str(key) not in {
                        "id",
                        "entity_id",
                        "metadata",
                        "metadata_assertions",
                        "assertions",
                        "deletion_assertions",
                        "category",
                        "graph",
                        "counts",
                    }
                }
                deletion_assertions = item.get("deletion_assertions")
                if isinstance(deletion_assertions, Mapping):
                    row_assertions.update(dict(deletion_assertions))
                evidence_by_identity[(entity_type, str(raw_id))] = {
                    "metadata": dict(metadata_assertions),
                    "assertions": row_assertions,
                }

    # Preserve the evidence map separately from cleanup entries.  The legacy
    # WIQA manifest intentionally has one project selector plus a list of
    # exact task IDs; retaining the task assertions here lets the snapshot
    # validate every selected row (including a prior deletion batch) without
    # turning those related rows into independent deletion targets.
    frozen_entity_evidence: dict[str, Mapping[str, Any]] = {
        f"{entity_type}:{entity_id}": {
            "metadata": dict(value.get("metadata", {})),
            "assertions": dict(value.get("assertions", {})),
        }
        for (entity_type, entity_id), value in sorted(evidence_by_identity.items())
    }

    entries: list[ManifestEntry] = []
    seen: dict[tuple[str, str], ManifestEntry] = {}
    for index, record in enumerate(records):
        entity_type = str(record.get("entity_type", record.get("type", ""))).rstrip("s")
        if entity_type not in _ALLOWED_ENTITY_TYPES:
            raise VerificationCleanupError(
                "manifest contains an unknown entity type",
                code="manifest_unknown_entity_type",
                status_code=422,
                details={"index": index, "entity_type": entity_type},
            )
        entity_id = _string_uuid(record.get("entity_id", record.get("id")), field_name=f"entities[{index}].id")
        category = record.get("category", top_category)
        # A missing category is deliberately not guessed.  A one-time cleanup
        # must carry positive A evidence, not rely on row age or display name.
        if category not in _ALLOWED_CATEGORIES:
            raise VerificationCleanupError(
                "every cleanup target requires an explicit A/B/C category",
                code="manifest_missing_category",
                status_code=422,
                details={"entity_type": entity_type, "entity_id": entity_id},
            )
        if category != "A":
            raise VerificationCleanupError(
                "only category A targets may be cleaned",
                code="manifest_protected_target",
                status_code=403,
                details={"entity_type": entity_type, "entity_id": entity_id, "category": category},
            )
        metadata = record.get("metadata", record.get("provenance", top_metadata))
        metadata = _as_mapping(metadata or {}, label=f"entities[{index}].metadata")
        evidence = evidence_by_identity.get((entity_type, entity_id), {})
        evidence_metadata = evidence.get("metadata", {})
        if evidence_metadata:
            metadata = {**dict(metadata), **dict(evidence_metadata)}
        assertions = _entry_assertions(
            {
                **(
                    dict(record.get("assertions", {}))
                    if isinstance(record.get("assertions", {}), Mapping)
                    else {}
                ),
                **(
                    dict(record.get("deletion_assertions", {}))
                    if isinstance(record.get("deletion_assertions", {}), Mapping)
                    else {}
                ),
                **dict(evidence.get("assertions", {})),
            },
            label=f"entities[{index}].assertions",
        )
        entry_graph = record.get("graph")
        if entry_graph is None:
            entry_graph = graph_by_id.get(entity_id, {})
        entry_counts = _coerce_count_map(record.get("counts", {}), label=f"entities[{index}].counts")
        entry = ManifestEntry(
            entity_type=entity_type,
            entity_id=entity_id,
            category=category,
            metadata=dict(metadata),
            assertions=assertions,
            graph=_entry_graph(entry_graph, label=f"entities[{index}].graph"),
            counts=entry_counts,
            owner_id=(str(record["owner_id"]) if record.get("owner_id") is not None else None),
        )
        identity = (entity_type, entity_id)
        prior = seen.get(identity)
        if prior is not None and prior != entry:
            raise VerificationCleanupError(
                "manifest contains conflicting duplicate targets",
                code="manifest_duplicate_target",
                status_code=422,
                details={"entity_type": entity_type, "entity_id": entity_id},
            )
        seen[identity] = entry
    entries = sorted(seen.values(), key=lambda item: (item.entity_type, item.entity_id))
    if selection is not None:
        selected_projects = set(selected_graph.get("projects", ()))
        manifest_projects = {entry.entity_id for entry in entries if entry.entity_type == "project"}
        if selected_projects != manifest_projects:
            raise VerificationCleanupError(
                "selection.projects must exactly match project entities",
                code="manifest_selection_mismatch",
                status_code=422,
            )
    manifest = CleanupManifest(
        run_id=run_id,
        manifest_id=(str(raw.get("manifest_id")).strip() if raw.get("manifest_id") is not None else None),
        source=source.strip(),
        disposable=True,
        schema_version=schema_version,
        created_at=created_at,
        entries=tuple(entries),
        counts=top_counts,
        metadata=dict(top_metadata),
        anchors=anchors,
        workspace=workspace,
        entity_evidence=frozen_entity_evidence,
    ).with_digest()
    supplied_digest = raw.get("digest", raw.get("manifest_digest"))
    if supplied_digest is not None and supplied_digest != manifest.digest:
        raise VerificationCleanupError(
            "manifest digest does not match its contents",
            code="manifest_digest_mismatch",
            status_code=409,
            details={"expected_digest": manifest.digest},
        )
    return manifest


def load_cleanup_manifest(
    source: CleanupManifest | Mapping[str, Any] | str | Path,
    *,
    max_bytes: int = MAX_MANIFEST_BYTES,
) -> CleanupManifest:
    """Load and freeze a manifest with a hard byte bound.

    A mapping is serialized before parsing as well, so callers cannot bypass
    the bound with a giant in-memory object.  Paths are read once and never
    followed recursively.
    """

    if max_bytes <= 0 or max_bytes > 16 * 1024 * 1024:
        raise ValueError("max_bytes must be in (0, 16MiB]")
    if isinstance(source, CleanupManifest):
        if not source.digest:
            return source.with_digest()
        # Dataclasses prevent attribute reassignment but their nested mapping
        # fields may still be mutated by an adapter.  Recompute before every
        # use so a stale digest can never authorize a changed target graph.
        expected_digest = _sha256(source.payload())
        if source.digest != expected_digest:
            raise VerificationCleanupError(
                "manifest digest does not match its frozen contents",
                code="manifest_digest_mismatch",
                status_code=409,
                details={"expected_digest": expected_digest},
            )
        return source
    if isinstance(source, Mapping):
        try:
            encoded = _canonical_json(source).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise VerificationCleanupError(
                "manifest is not JSON serializable",
                code="manifest_invalid_json",
                status_code=422,
            ) from exc
        if len(encoded) > max_bytes:
            raise VerificationCleanupError(
                "manifest exceeds size limit",
                code="manifest_too_large",
                status_code=413,
            )
        raw = source
    else:
        # A string is an operator/API manifest key, not a path.  Keeping path
        # loading restricted to ``Path`` objects prevents an HTTP selector
        # from probing or reading an arbitrary file in the server's cwd.
        if isinstance(source, str):
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", source) is None:
                raise VerificationCleanupError(
                    "manifest key is invalid",
                    code="manifest_unreadable",
                    status_code=422,
                )
            if source not in _LEGACY_MANIFEST_REGISTRY:
                raise VerificationCleanupError(
                    "manifest key is not allow-listed",
                    code="manifest_unreadable",
                    status_code=404,
                )
            path = (
                Path(__file__).resolve().parents[2]
                / "scripts"
                / "verification"
                / "manifests"
                / f"{source}.json"
            )
        else:
            path = Path(source)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise VerificationCleanupError(
                "manifest cannot be read",
                code="manifest_unreadable",
                status_code=422,
            ) from exc
        if size > max_bytes:
            raise VerificationCleanupError(
                "manifest exceeds size limit",
                code="manifest_too_large",
                status_code=413,
            )
        try:
            encoded = path.read_bytes()
            raw = json.loads(encoded.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VerificationCleanupError(
                "manifest is not valid UTF-8 JSON",
                code="manifest_invalid_json",
                status_code=422,
            ) from exc
        if not isinstance(raw, Mapping):
            raise VerificationCleanupError(
                "manifest root must be an object",
                code="manifest_invalid_shape",
                status_code=422,
            )
    return _manifest_from_mapping(raw)


def _uuid_or_none(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _ids(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(sorted({str(value) for value in values if value is not None}))


# Explicit order for the graph whose FKs are not uniformly ON DELETE CASCADE.
# Classes are looked up by object rather than issuing table-name SQL.
_TASK_CHILDREN: tuple[tuple[Any, str, str], ...] = (
    (NotificationDelivery, "task_id", "task"),
    (TimeEntry, "task_id", "task"),
    (TaskOccurrence, "task_id", "task"),
    (TaskDependency, "task_id", "task"),
    (TaskRelation, "task_a_id", "task"),
    (TaskRelation, "task_b_id", "task"),
    (TaskTag, "task_id", "task"),
    (TaskAppLink, "task_id", "task"),
    (TaskAssignee, "task_id", "task"),
    (TaskAttachment, "task_id", "task"),
    (TaskComment, "task_id", "task"),
    (TaskActivity, "task_id", "task"),
    (TaskReference, "task_id", "task"),
    (TaskSchedulePlacement, "task_id", "task"),
    (TaskRecurrenceScheduleSegment, "task_id", "task"),
    (TaskRecurrenceRule, "task_id", "task"),
)

_PROJECT_AUXILIARY: tuple[tuple[Any, str], ...] = (
    (ProjectApp, "project_id"),
    (AppGrant, "project_id"),
    (AppJob, "project_id"),
    (ProjectStorageOperation, "project_id"),
    (ProjectNotificationSetting, "project_id"),
    (ProjectJoinRequest, "project_id"),
    (ProjectMember, "project_id"),
    (ProjectQaEntry, "project_id"),
    (DocsCandidate, "project_id"),
    (DocsClipIngestJob, "project_id"),
    (KnowledgeImportJob, "project_id"),
    (KnowledgeSourcePermission, "project_id"),
    (ProjectKnowledgeRef, "project_id"),
    (ProjectOverviewRefreshJob, "project_id"),
    (ProjectOverview, "project_id"),
    (ProjectSchedulePhase, "project_id"),
    (ContextMemory, "project_id"),
    (ScopedMemoryJob, "project_id"),
    (SkillProposal, "project_id"),
    (SkillUsageReceipt, "project_id"),
    (HeartbeatRunState, "project_id"),
    (HeartbeatRunHistory, "project_id"),
    # Notifications may be retained independently by the canonical project
    # repository; include the exact project-scoped rows in the frozen graph so
    # rolling deployments cannot leave verification-only deliveries behind.
    (NotificationDelivery, "project_id"),
    (RecordAttachment, "project_id"),
    (RecordEvent, "project_id"),
    (RecordField, "project_id"),
    (RecordRow, "project_id"),
    (RecordTable, "project_id"),
    (RecordView, "project_id"),
    (KnowledgeSearchIndex, "project_id"),
    (AgentRun, "project_id"),
    (ConversationSession, "project_id"),
)
if TokenUsage is not None:
    _PROJECT_AUXILIARY += ((TokenUsage, "project_id"),)


def _model_id_column(model: Any) -> Any | None:
    return getattr(model, "id", None)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _column_values(model: Any, column_name: str, values: Iterable[Any]) -> tuple[Any, ...]:
    """Normalize frozen string IDs to the mapped UUID Python type.

    PostgreSQL's ``UUID(as_uuid=True)`` bind processor expects ``uuid.UUID``
    objects on asyncpg, while manifests intentionally serialize IDs as text.
    Convert only columns whose mapped Python type is UUID; plain String IDs
    (legacy session/archive keys, user IDs, etc.) remain untouched.
    """

    raw_values = tuple({value for value in values if value is not None})
    column = getattr(model, column_name, None)
    try:
        mapped_type = column.property.columns[0].type.python_type
    except Exception:
        mapped_type = None
    if mapped_type is UUID:
        converted: list[Any] = []
        for value in raw_values:
            converted.append(value if isinstance(value, UUID) else (_uuid_or_none(value) or value))
        return tuple(converted)
    return raw_values


async def _delete_where(session: AsyncSession, model: Any, column_name: str, values: Iterable[Any]) -> int:
    values = _column_values(model, column_name, values)
    if not values:
        return 0
    column = getattr(model, column_name, None)
    if column is None:
        return 0
    result = await session.execute(delete(model).where(column.in_(values)))
    return int(getattr(result, "rowcount", 0) or 0)


async def _select_ids(session: AsyncSession, model: Any, column_name: str, values: Iterable[Any]) -> tuple[Any, ...]:
    values = _column_values(model, column_name, values)
    id_column = _model_id_column(model)
    column = getattr(model, column_name, None)
    if not values or id_column is None or column is None:
        return ()
    result = await session.execute(select(id_column).where(column.in_(values)))
    return tuple(result.scalars().all())


async def _select_models(session: AsyncSession, model: Any, column_name: str, values: Iterable[Any]) -> list[Any]:
    values = _column_values(model, column_name, values)
    column = getattr(model, column_name, None)
    if not values or column is None:
        return []
    result = await session.execute(select(model).where(column.in_(values)))
    return list(result.scalars().all())


async def _select_context_audit_project_ids(
    session: AsyncSession,
    project_id: UUID,
    *,
    allowed_memory_ids: Iterable[Any] = (),
) -> tuple[Any, ...]:
    """Read exact audit IDs carrying a direct JSON project marker.

    ContextMemoryAudit predates a relational ``project_id`` column; newer
    writers retain the scope in ``turn_context`` JSON.  This is an ORM JSON
    predicate (not a raw SQL sweep) and is best-effort on rolling SQLite/test
    schemas that do not support the PostgreSQL JSON comparator.
    """

    allowed = {str(value) for value in allowed_memory_ids if value is not None}
    try:
        result = await session.execute(
            select(ContextMemoryAudit).where(
                ContextMemoryAudit.turn_context["project_id"].as_string() == str(project_id)
            )
        )
    except Exception as exc:
        raise VerificationCleanupError(
            "context audit scope could not be verified",
            code="context_audit_unknown",
            status_code=409,
            details={"project_id": str(project_id)},
        ) from exc
    rows = list(result.scalars().all())
    for row in rows:
        memory_id = getattr(row, "memory_id", None)
        if memory_id is not None and str(memory_id) not in allowed:
            raise VerificationCleanupError(
                "context audit references a protected memory",
                code="shared_context_audit",
                status_code=409,
                details={"project_id": str(project_id)},
            )
    return tuple(getattr(row, "id", None) for row in rows if getattr(row, "id", None) is not None)


async def _validate_space_anchor(
    session: AsyncSession,
    *,
    project_row: Any,
    project_id: UUID,
    space_anchor: Mapping[str, Any],
) -> Space:
    """Validate a manifest's enclosing Space without treating it as a target.

    Spaces are shared containers and are intentionally never deleted by the
    project graph purge.  A project pointer alone is therefore insufficient
    evidence: the anchor must name a real Space, carry a supported
    preservation disposition, and (when supplied) match the frozen owner.
    The owner row is also checked so a malformed/partially migrated database
    cannot turn an arbitrary UUID into a trusted shared-space anchor.
    """

    raw_space_id = space_anchor.get("id") or space_anchor.get("space_id")
    space_id = _uuid_or_none(raw_space_id)
    if space_id is None:
        raise VerificationCleanupError(
            "space anchor requires a valid UUID",
            code="shared_space_anchor",
            status_code=409,
            details={"project_id": str(project_id)},
        )

    actual_project_space_id = getattr(project_row, "space_id", None)
    if actual_project_space_id is None or str(actual_project_space_id).casefold() != str(space_id).casefold():
        raise VerificationCleanupError(
            "space anchor does not match target project",
            code="project_anchor_mismatch",
            status_code=409,
            details={"project_id": str(project_id)},
        )

    disposition = space_anchor.get("disposition")
    normalized_disposition = (
        str(disposition).strip().casefold() if isinstance(disposition, str) else ""
    )
    if normalized_disposition not in _ALLOWED_SPACE_DISPOSITIONS:
        raise VerificationCleanupError(
            "space anchor disposition is not a supported preservation policy",
            code="shared_space_disposition_invalid",
            status_code=409,
            details={"project_id": str(project_id)},
        )

    space_row = await session.get(Space, space_id)
    if space_row is None:
        raise VerificationCleanupError(
            "space anchor row is missing",
            code="shared_space_anchor",
            status_code=409,
            details={"project_id": str(project_id), "space_id": str(space_id)},
        )

    expected_owner_raw = space_anchor.get("owner_id") or space_anchor.get("owner_user_id")
    expected_owner = _uuid_or_none(expected_owner_raw) if expected_owner_raw is not None else None
    if expected_owner_raw is not None and expected_owner is None:
        raise VerificationCleanupError(
            "space anchor owner is invalid",
            code="shared_space_anchor",
            status_code=409,
            details={"project_id": str(project_id), "space_id": str(space_id)},
        )

    actual_owner = getattr(space_row, "owner_id", None)
    actual_owner_uuid = _uuid_or_none(actual_owner)
    if actual_owner_uuid is None:
        raise VerificationCleanupError(
            "space anchor owner is missing",
            code="shared_space_anchor",
            status_code=409,
            details={"project_id": str(project_id), "space_id": str(space_id)},
        )
    if expected_owner is not None and actual_owner_uuid != expected_owner:
        raise VerificationCleanupError(
            "space anchor owner changed",
            code="project_anchor_mismatch",
            status_code=409,
            details={"project_id": str(project_id), "space_id": str(space_id)},
        )

    # The Space FK is NO ACTION in some deployed schemas.  Verify the owner
    # row explicitly as an integrity fence rather than relying on the FK
    # implementation in a lightweight/test database.
    if await session.get(User, actual_owner_uuid) is None:
        raise VerificationCleanupError(
            "space anchor owner row is missing",
            code="shared_space_anchor",
            status_code=409,
            details={"project_id": str(project_id), "space_id": str(space_id)},
        )

    anchor_project_id = space_anchor.get("project_id")
    if anchor_project_id is not None and str(anchor_project_id).casefold() != str(project_id).casefold():
        raise VerificationCleanupError(
            "space anchor project does not match target",
            code="project_anchor_mismatch",
            status_code=409,
            details={"project_id": str(project_id), "space_id": str(space_id)},
        )
    return space_row


async def _validate_node_projection_ownership(
    session: AsyncSession,
    *,
    project_id: UUID,
    node_ids: Iterable[Any],
) -> None:
    """Fence import/clip rows whose node FKs lack a project_id column.

    ``KnowledgeImportItem`` and ``ClipIngestReceipt`` are the two mapped
    projections where a node FK alone is not ownership proof.  A node can be
    shared by multiple workflows/libraries, and deleting an A node would
    otherwise mutate a B/C import job or receipt via ``SET NULL``/``CASCADE``.
    Require the owning job/topic node to carry the exact project identity and
    reject missing/mismatched owners before any generic FK sweep runs.
    """

    frozen_node_ids = tuple(node_ids)
    if not frozen_node_ids:
        return

    node_rows = await _select_models(session, KnowledgeNode, "id", frozen_node_ids)
    node_by_id = {str(getattr(row, "id", "")): row for row in node_rows}

    import_rows = await _select_models(session, KnowledgeImportItem, "node_id", frozen_node_ids)
    if import_rows:
        job_ids = {
            getattr(row, "job_id", None)
            for row in import_rows
            if getattr(row, "job_id", None) is not None
        }
        jobs = await _select_models(session, KnowledgeImportJob, "id", job_ids)
        jobs_by_id = {str(getattr(job, "id", "")): job for job in jobs}
        for item in import_rows:
            node = node_by_id.get(str(getattr(item, "node_id", "")))
            job = jobs_by_id.get(str(getattr(item, "job_id", "")))
            if (
                node is None
                or job is None
                or str(getattr(job, "project_id", "")).casefold() != str(project_id).casefold()
            ):
                raise VerificationCleanupError(
                    "knowledge import item crosses a protected boundary",
                    code="shared_knowledge_reference",
                    status_code=409,
                    details={"project_id": str(project_id), "table": "knowledge_import_items"},
                )
            if str(getattr(job, "docs_library_id", "")).casefold() != str(
                getattr(node, "docs_library_id", "")
            ).casefold():
                raise VerificationCleanupError(
                    "knowledge import item library does not match its node",
                    code="shared_knowledge_reference",
                    status_code=409,
                    details={"project_id": str(project_id), "table": "knowledge_import_items"},
                )

    receipt_rows_by_id: dict[str, Any] = {}
    for column_name in ("topic_node_id", "target_node_id"):
        for row in await _select_models(session, ClipIngestReceipt, column_name, frozen_node_ids):
            receipt_rows_by_id[str(getattr(row, "id", ""))] = row
    for receipt in receipt_rows_by_id.values():
        topic_id = getattr(receipt, "topic_node_id", None)
        topic = node_by_id.get(str(topic_id))
        if topic is None and topic_id is not None:
            topic = await session.get(KnowledgeNode, topic_id)
        if topic is None or str(getattr(topic, "project_id", "")).casefold() != str(project_id).casefold():
            raise VerificationCleanupError(
                "clip receipt topic crosses a protected boundary",
                code="shared_knowledge_reference",
                status_code=409,
                details={"project_id": str(project_id), "table": "docs_clip_ingest_receipts"},
            )
        target_id = getattr(receipt, "target_node_id", None)
        if target_id is not None:
            target = node_by_id.get(str(target_id))
            if target is None:
                target = await session.get(KnowledgeNode, target_id)
            if target is None or str(getattr(target, "project_id", "")).casefold() != str(project_id).casefold():
                raise VerificationCleanupError(
                    "clip receipt target crosses a protected boundary",
                    code="shared_knowledge_reference",
                    status_code=409,
                    details={"project_id": str(project_id), "table": "docs_clip_ingest_receipts"},
                )
        if str(getattr(receipt, "docs_library_id", "")).casefold() != str(
            getattr(topic, "docs_library_id", "")
        ).casefold():
            raise VerificationCleanupError(
                "clip receipt library does not match its topic",
                code="shared_knowledge_reference",
                status_code=409,
                details={"project_id": str(project_id), "table": "docs_clip_ingest_receipts"},
            )


def _safe_manifest_workspace_path(
    relative_path: str,
    *,
    workspace_root: Path,
) -> Path:
    """Resolve one manifest path while enforcing the workspace boundary."""

    normalized = str(relative_path or "").strip().replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(part in {".", ".."} or ":" in part for part in parts):
        raise VerificationCleanupError(
            "workspace path is outside the configured root",
            code="workspace_path_invalid",
            status_code=409,
        )
    # Checked-in manifests record paths relative to the repository root
    # (``workspaces/...``), while the runtime helper receives the workspaces
    # directory itself.  Strip that redundant leading component only when it
    # exactly matches the configured root name.
    if parts and parts[0].casefold() == workspace_root.name.casefold():
        parts = parts[1:]
    candidate = (workspace_root.joinpath(*parts)).resolve()
    try:
        candidate.relative_to(workspace_root)
    except ValueError as exc:
        raise VerificationCleanupError(
            "workspace path escaped the configured root",
            code="workspace_path_invalid",
            status_code=409,
        ) from exc
    return candidate


class VerificationCleanupCoordinator:
    """Preview and execute one exact verification cleanup manifest."""

    def __init__(
        self,
        provenance: Any | None = None,
        task_service: Any | None = None,
        project_repository: Any = ProjectRepository,
        *,
        max_manifest_bytes: int = MAX_MANIFEST_BYTES,
    ) -> None:
        self.provenance = provenance
        self.task_service = task_service
        self.project_repository = project_repository
        self.max_manifest_bytes = max_manifest_bytes
        self._cleanup_run_ids: dict[str, UUID] = {}

    def _manifest(self, value: CleanupManifest | Mapping[str, Any] | str | Path) -> CleanupManifest:
        return load_cleanup_manifest(value, max_bytes=self.max_manifest_bytes)

    async def _call_provenance(self, names: Sequence[str], session: AsyncSession, **kwargs: Any) -> Any:
        service = self.provenance
        if service is None:
            try:
                from .verification_provenance import VerificationProvenanceService

                service = VerificationProvenanceService()
                self.provenance = service
            except Exception:
                service = None
        if service is None:
            return None
        for name in names:
            method = getattr(service, name, None)
            if not callable(method):
                continue
            try:
                result = method(session, **kwargs)
            except TypeError:
                # Accommodate classmethods/legacy adapters which do not take
                # the session as their first positional argument.
                try:
                    result = method(**kwargs)
                except TypeError:
                    continue
            return await _maybe_await(result)
        return None

    async def _existing_ledger(self, session: AsyncSession, manifest: CleanupManifest) -> Mapping[str, Any] | None:
        # The legacy WIQA manifest uses an opaque, allow-listed selector key
        # rather than a UUID-backed VerificationRun.  Never pass that key to
        # the provenance ORM/service (which correctly rejects non-UUID IDs);
        # use the append-only compatibility ledger below instead.
        found = None
        if _uuid_or_none(manifest.run_id) is not None:
            found = await self._call_provenance(
                ("get_cleanup", "find_cleanup", "get_cleanup_run", "lookup_cleanup"),
                session,
                run_id=manifest.run_id,
            )
        if isinstance(found, Mapping):
            return found
        # The provenance implementation stores cleanup attempts in a
        # separate row keyed by the UUID verification run.  Read it directly
        # as a compatibility bridge when the service intentionally exposes
        # only create/finish methods.
        run_uuid = _uuid_or_none(manifest.run_id)
        if run_uuid is not None:
            try:
                from ..memory.models.verification import VerificationCleanupRun

                result = await session.execute(
                    select(VerificationCleanupRun)
                    .where(VerificationCleanupRun.run_id == run_uuid)
                    .order_by(VerificationCleanupRun.created_at.desc())
                    .limit(1)
                )
                cleanup = result.scalar_one_or_none()
            except Exception:
                cleanup = None
            if cleanup is not None:
                cleanup_id = _uuid_or_none(getattr(cleanup, "id", None))
                if cleanup_id is not None:
                    self._cleanup_run_ids[manifest.run_id] = cleanup_id
                summary = getattr(cleanup, "summary_json", None) or getattr(cleanup, "summary", {}) or {}
                metadata = getattr(cleanup, "metadata_json", None) or getattr(cleanup, "metadata", {}) or {}
                return {
                    "run_id": manifest.run_id,
                    "digest": getattr(cleanup, "confirmation_sha256", None),
                    "preview_digest": metadata.get("preview_digest") if isinstance(metadata, Mapping) else None,
                    "status": getattr(cleanup, "status", None),
                    "counts": summary if isinstance(summary, Mapping) else {},
                    "cleanup_id": str(cleanup_id) if cleanup_id else None,
                }
        # Fallback audit lookup.  ContentDeletionEvent is append-only and has
        # no FK to the run, which keeps it queryable after purge.
        try:
            result = await session.execute(
                select(ContentDeletionEvent)
                .where(
                    ContentDeletionEvent.entity_type == "verification_run",
                    ContentDeletionEvent.entity_id == manifest.run_id,
                )
                .order_by(ContentDeletionEvent.event_at.desc())
                .limit(1)
            )
            event = result.scalar_one_or_none()
        except Exception:
            return None
        if event is None:
            return None
        return {
            "run_id": manifest.run_id,
            "digest": (event.event_metadata or {}).get("manifest_digest"),
            "preview_digest": (event.event_metadata or {}).get("preview_digest"),
            "status": (event.event_metadata or {}).get("status"),
            "counts": (event.event_metadata or {}).get("counts", {}),
        }

    async def _record_ledger(
        self,
        session: AsyncSession,
        manifest: CleanupManifest,
        *,
        status: str,
        actor_user_id: Any = None,
        details: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> None:
        payload = {
            "run_id": manifest.run_id,
            "manifest_digest": manifest.digest,
            "status": status,
            "source": manifest.source,
            "counts": dict((details or {}).get("counts", {})),
            "entity_counts": dict((details or {}).get("entity_counts", {})),
        }
        error_code = (details or {}).get("error_code")
        if error_code:
            payload["error_code"] = str(error_code)[:128]
        preview_digest = (details or {}).get("preview_digest") or (details or {}).get("digest")
        if preview_digest:
            payload["preview_digest"] = str(preview_digest)[:128]
        graph = (details or {}).get("graph")
        if isinstance(graph, Mapping) and graph.get("digest"):
            payload["graph_digest"] = str(graph["digest"])[:128]
        # Keep the operator-facing inventory bounded and machine-readable.  Do
        # not persist prompt/document bodies or arbitrary manifest metadata in
        # the durable ledger.
        reason = manifest.metadata.get("reason") if isinstance(manifest.metadata, Mapping) else None
        inventory: dict[str, Any] = {
            "classification": {"A": len(manifest.entries), "B": 0, "C": 0},
            "counts": dict(manifest.counts),
            "targets": [
                {
                    "entity_type": entry.entity_type,
                    "entity_id": entry.entity_id,
                    "category": entry.category,
                }
                for entry in manifest.entries[:200]
            ],
        }
        if isinstance(reason, str) and reason:
            inventory["reason"] = reason[:1024]
        payload["inventory"] = inventory
        # VerificationProvenanceService uses a two-phase cleanup row: begin
        # (running) then finish (succeeded/failed).  Keep the row id in this
        # coordinator and recover it from the database on retries.
        service = self.provenance
        if service is None:
            try:
                from .verification_provenance import VerificationProvenanceService

                service = VerificationProvenanceService()
                self.provenance = service
            except Exception:
                service = None
        if service is not None and _uuid_or_none(manifest.run_id) is not None:
            try:
                handled = False
                if status == "running":
                    existing = await self._existing_ledger(session, manifest)
                    # A previous failed/partial attempt must not be reused as
                    # the active two-phase row.  Only an actually running row
                    # is safe to resume; terminal success is handled by the
                    # idempotence check in execute().
                    cleanup_id = (
                        _uuid_or_none((existing or {}).get("cleanup_id"))
                        if isinstance(existing, Mapping)
                        and str(existing.get("status") or "").lower() == "running"
                        else None
                    )
                    if cleanup_id is None:
                        begin = getattr(service, "begin_cleanup", None) or getattr(service, "create_cleanup_run", None)
                        if callable(begin):
                            value = begin(
                                session,
                                run_id=manifest.run_id,
                                actor_user_id=actor_user_id,
                                confirmation_sha256=manifest.digest,
                                metadata=payload,
                                # The caller persists the bounded running
                                # inventory immediately before mutation.
                                commit=False,
                            )
                            value = await _maybe_await(value)
                            cleanup_id = _uuid_or_none(getattr(value, "id", None) if value is not None else None)
                            if cleanup_id is not None:
                                self._cleanup_run_ids[manifest.run_id] = cleanup_id
                                handled = True
                else:
                    cleanup_id = self._cleanup_run_ids.get(manifest.run_id)
                    if cleanup_id is None:
                        existing = await self._existing_ledger(session, manifest)
                        cleanup_id = _uuid_or_none((existing or {}).get("cleanup_id")) if isinstance(existing, Mapping) else None
                    finish = getattr(service, "finish_cleanup", None)
                    if cleanup_id is not None and callable(finish):
                        terminal = (
                            "succeeded"
                            if status in {"completed", "idempotent"}
                            else "partial"
                            if status == "partial"
                            else "failed"
                        )
                        value = finish(
                            session,
                            cleanup_id,
                            status=terminal,
                            counts=payload["counts"],
                            error=(payload.get("error_code") if terminal == "failed" else None),
                            commit=commit,
                        )
                        await _maybe_await(value)
                        if terminal == "succeeded":
                            list_artifacts = getattr(service, "list_artifacts", None)
                            mark_artifact = getattr(service, "mark_artifact_cleaned", None)
                            if callable(list_artifacts) and callable(mark_artifact):
                                allowed_artifacts = {
                                    (entry.entity_type, entry.entity_id)
                                    for entry in manifest.entries
                                }
                                artifacts = await _maybe_await(list_artifacts(session, manifest.run_id))
                                for artifact in artifacts or ():
                                    artifact_id = getattr(artifact, "id", None)
                                    if artifact_id is None and isinstance(artifact, Mapping):
                                        artifact_id = artifact.get("id")
                                    artifact_type = getattr(artifact, "entity_type", None)
                                    artifact_entity_id = getattr(artifact, "entity_id", None)
                                    if isinstance(artifact, Mapping):
                                        artifact_type = artifact_type or artifact.get("entity_type")
                                        artifact_entity_id = artifact_entity_id or artifact.get("entity_id")
                                    if (str(artifact_type or ""), str(artifact_entity_id or "")) not in allowed_artifacts:
                                        # Unsupported docs/files/runs remain
                                        # pending for their own cleanup owner;
                                        # this coordinator must not claim them.
                                        continue
                                    if artifact_id is not None:
                                        await _maybe_await(mark_artifact(session, artifact_id, status="deleted", commit=False))
                                await session.flush()
                        handled = True
                if handled:
                    return
            except Exception:
                # A provenance write failure must not silently widen the
                # cleanup selector.  Keep the append-only audit fallback and
                # let the caller's domain operation decide whether to fail.
                logger.exception("verification provenance ledger write failed for %s", manifest.run_id)
        # ``ContentDeletionEvent`` has a strict action vocabulary.  Use
        # ``deleted`` for running/completed and keep the status in metadata.
        # The row is intentionally small and never stores content bodies.
        event = ContentDeletionEvent(
            entity_type="verification_run",
            entity_id=manifest.run_id,
            root_entity_id=manifest.run_id,
            batch_id=_uuid_or_none(manifest.run_id) or uuid4(),
            actor_user_id=_uuid_or_none(actor_user_id),
            action="deleted" if status in {"running", "completed", "idempotent"} else "purged",
            source="verification_cleanup",
            event_at=datetime.now(timezone.utc).replace(tzinfo=None),
            event_metadata=payload,
        )
        session.add(event)
        await session.flush()

    @staticmethod
    def _metadata_for(row: Any) -> Mapping[str, Any]:
        for attr in ("project_metadata", "task_metadata", "user_settings", "metadata", "run_metadata"):
            value = getattr(row, attr, None)
            if isinstance(value, Mapping):
                return value
        return {}

    @staticmethod
    def _metadata_matches(actual: Mapping[str, Any], expected: Mapping[str, Any], *, run_id: str) -> bool:
        def lookup(mapping: Mapping[str, Any], key: str) -> tuple[bool, Any]:
            if key in mapping:
                return True, mapping[key]
            # Manifest evidence often names the persisted JSON field to make
            # the assertion auditable (e.g. ``project_metadata.qa_fixture``).
            # Resolve that prefix against the row's metadata mapping.
            short = key.rsplit(".", 1)[-1]
            if short in mapping:
                return True, mapping[short]
            if key.startswith(("project_metadata.", "task_metadata.", "user_settings.", "run_metadata.")):
                key = key.split(".", 1)[1]
            current: Any = mapping
            for part in key.split("."):
                if not isinstance(current, Mapping) or part not in current:
                    return False, None
                current = current[part]
            return True, current

        if expected:
            for key, value in expected.items():
                found, actual_value = lookup(actual, str(key))
                if not found or actual_value != value:
                    return False
        # A malformed run marker is never silently accepted.  ``qa_fixture``
        # is allowed for legacy manifests when the expected fixture is bound
        # in the manifest; run_id markers, when present, must match exactly.
        for key in ("verification_run_id", "run_id"):
            if key in actual and str(actual[key]) != run_id:
                return False
        return bool(expected) or any(key in actual for key in ("verification_run_id", "run_id", "qa_fixture", "fixture_id", "disposable"))

    @staticmethod
    def _assertion_value(row: Any, key: str) -> Any:
        """Read one allow-listed ORM identity/evidence field."""

        aliases = {
            "id": "id",
            "name": "name",
            "title": "title",
            "slug": "slug",
            "username": "username",
            "role": "role",
            "is_active": "is_active",
            "owner_id": "owner_id",
            "space_id": "space_id",
            "project_id": "project_id",
            "knowledge_node_id": "knowledge_node_id",
            "docs_library_id": "docs_library_id",
            "library_id": "docs_library_id",
            "parent_id": "parent_id",
            "root_page_id": "root_page_id",
            "deleted_at": "deleted_at",
            "deletion_batch_id": "deletion_batch_id",
        }
        attribute = aliases.get(key)
        return getattr(row, attribute, None) if attribute else None

    @staticmethod
    def _assertion_equal(actual: Any, expected: Any, *, key: str) -> bool:
        """Compare scalar assertions without lossy string heuristics."""

        if expected is None:
            return actual is None
        if key in {
            "owner_id",
            "space_id",
            "project_id",
            "knowledge_node_id",
            "docs_library_id",
            "library_id",
            "parent_id",
            "root_page_id",
            "deletion_batch_id",
        }:
            return str(actual or "").casefold() == str(expected).strip().casefold()
        if key in {"deleted_at"}:
            # Datetimes from the ORM may carry microseconds while a frozen
            # forensic manifest commonly records whole-second ISO precision.
            # Normalize both values to UTC and compare at the precision the
            # manifest supplies; this still rejects a different deletion.
            try:
                expected_text = str(expected).strip().replace("Z", "+00:00")
                expected_dt = datetime.fromisoformat(expected_text)
                actual_dt = actual if isinstance(actual, datetime) else datetime.fromisoformat(str(actual).replace("Z", "+00:00"))
                if expected_dt.tzinfo is not None:
                    expected_dt = expected_dt.astimezone(timezone.utc).replace(tzinfo=None)
                if actual_dt.tzinfo is not None:
                    actual_dt = actual_dt.astimezone(timezone.utc).replace(tzinfo=None)
                precision = 0 if expected_dt.microsecond == 0 else 6
                if precision == 0:
                    return actual_dt.replace(microsecond=0) == expected_dt.replace(microsecond=0)
                return actual_dt == expected_dt
            except (TypeError, ValueError, AttributeError):
                return False
        if isinstance(actual, UUID):
            return str(actual).casefold() == str(expected).strip().casefold()
        return actual == expected or str(actual if actual is not None else "") == str(expected)

    def _assertions_match(self, row: Any, assertions: Mapping[str, Any]) -> bool:
        for key, expected in assertions.items():
            actual = self._assertion_value(row, str(key))
            if not self._assertion_equal(actual, expected, key=str(key)):
                return False
        return True

    async def _load_target_rows(self, session: AsyncSession, manifest: CleanupManifest) -> dict[tuple[str, str], Any]:
        model_by_type = {"project": Project, "task": Task, "user": User}
        rows: dict[tuple[str, str], Any] = {}
        for entry in manifest.entries:
            model = model_by_type[entry.entity_type]
            identity = _uuid_or_none(entry.entity_id)
            if identity is None:
                raise VerificationCleanupError(
                    "cleanup targets must use UUID identities",
                    code="manifest_invalid_identity",
                    status_code=422,
                    details={"entity_type": entry.entity_type, "entity_id": entry.entity_id},
                )
            row = await session.get(model, identity)
            if row is None:
                # A prior completed run is handled by ledger idempotence.  A
                # missing row without that ledger is ambiguous and fails
                # closed rather than treating it as a successful purge.
                raise VerificationCleanupError(
                    "manifest target no longer exists",
                    code="target_missing",
                    status_code=409,
                    details={"entity_type": entry.entity_type, "entity_id": entry.entity_id},
                )
            if not self._metadata_matches(self._metadata_for(row), entry.metadata, run_id=manifest.run_id):
                raise VerificationCleanupError(
                    "target provenance does not match manifest",
                    code="target_provenance_mismatch",
                    status_code=409,
                    details={"entity_type": entry.entity_type, "entity_id": entry.entity_id},
                )
            if entry.owner_id is not None:
                actual_owner = getattr(row, "owner_id", None)
                if actual_owner is None:
                    actual_owner = getattr(row, "user_id", None)
                if str(actual_owner or "") != str(entry.owner_id):
                    raise VerificationCleanupError(
                        "target owner does not match manifest",
                        code="target_owner_mismatch",
                        status_code=409,
                        details={"entity_type": entry.entity_type, "entity_id": entry.entity_id},
                    )
            if not self._assertions_match(row, entry.assertions):
                raise VerificationCleanupError(
                    "target row does not match manifest assertions",
                    code="target_assertion_mismatch",
                    status_code=409,
                    details={"entity_type": entry.entity_type, "entity_id": entry.entity_id},
                )
            rows[(entry.entity_type, entry.entity_id)] = row
        return rows

    async def _snapshot_project(
        self,
        session: AsyncSession,
        project_id: UUID,
        entry: ManifestEntry,
        anchors: Mapping[str, Any] | None = None,
        entity_evidence: Mapping[str, Mapping[str, Any]] | None = None,
        verification_run_id: str | None = None,
    ) -> dict[str, Any]:
        project_row = await session.get(Project, project_id)
        if project_row is None:
            raise VerificationCleanupError(
                "manifest target no longer exists",
                code="target_missing",
                status_code=409,
                details={"entity_type": "project", "entity_id": str(project_id)},
            )
        if entry.owner_id is not None and str(getattr(project_row, "owner_id", "")) != str(entry.owner_id):
            raise VerificationCleanupError(
                "project owner does not match the frozen manifest",
                code="target_provenance_mismatch",
                status_code=409,
                details={"entity_type": "project", "entity_id": str(project_id)},
            )
        graph: dict[str, tuple[str, ...]] = {}
        task_rows = await _select_models(session, Task, "project_id", [project_id])
        task_ids = _ids(row.id for row in task_rows)
        # A task parent pointer is a self-referential NO ACTION edge in some
        # deployed schemas.  A parent outside this exact project graph would
        # either block the delete or tempt a broad tree sweep, so reject the
        # manifest before mutation instead.
        parent_ids = {
            row.parent_task_id
            for row in task_rows
            if getattr(row, "parent_task_id", None) is not None
        }
        if parent_ids:
            parent_rows = await _select_models(session, Task, "id", parent_ids)
            parent_map = {getattr(row, "id", None): row for row in parent_rows}
            if len(parent_map) != len(parent_ids) or any(
                getattr(parent_map.get(parent_id), "project_id", None) != project_id
                for parent_id in parent_ids
            ):
                raise VerificationCleanupError(
                    "task graph crosses a protected project boundary",
                    code="shared_task_tree",
                    status_code=409,
                    details={"project_id": str(project_id)},
                )
        # ``TaskManagementService.delete_task`` walks descendants by
        # ``parent_task_id``.  A legacy/corrupt row from another project could
        # therefore be tombstoned by the canonical call even though it was not
        # selected by the project graph.  Fence every direct child of the
        # frozen A task set before any mutation; same-project children are
        # already present in ``task_rows`` and remain part of the exact graph.
        if task_ids:
            foreign_children = await session.execute(
                select(Task.id, Task.project_id).where(
                    Task.parent_task_id.in_(tuple(task_ids)),
                    Task.project_id != project_id,
                )
            )
            if foreign_children.first() is not None:
                raise VerificationCleanupError(
                    "task graph crosses a protected project boundary",
                    code="shared_task_tree",
                    status_code=409,
                    details={"project_id": str(project_id)},
                )
        legacy_local_ids = {
            row.legacy_local_task_id
            for row in task_rows
            if getattr(row, "legacy_local_task_id", None) is not None
        }
        if legacy_local_ids:
            legacy_rows = await _select_models(session, LocalTask, "id", legacy_local_ids)
            legacy_map = {getattr(row, "id", None): row for row in legacy_rows}
            if len(legacy_map) != len(legacy_local_ids) or any(
                getattr(legacy_map.get(local_id), "project_id", None) != project_id
                for local_id in legacy_local_ids
            ):
                raise VerificationCleanupError(
                    "task legacy graph crosses a protected project boundary",
                    code="shared_task_tree",
                    status_code=409,
                    details={"project_id": str(project_id)},
                )
        # Validate related task evidence carried by the frozen manifest.  The
        # exact project/task UUIDs remain the only selectors; human-readable
        # fields and deletion timestamps are additional assertions that must
        # still match immediately before mutation.
        task_evidence = entity_evidence or {}
        task_rows_by_id = {str(row.id): row for row in task_rows}
        for evidence_key, evidence in task_evidence.items():
            if not evidence_key.startswith("task:") or not isinstance(evidence, Mapping):
                continue
            related_task_id = evidence_key.split(":", 1)[1]
            related_row = task_rows_by_id.get(related_task_id)
            if related_row is None:
                raise VerificationCleanupError(
                    "manifest task evidence is outside the frozen project graph",
                    code="shared_task_tree",
                    status_code=409,
                    details={"project_id": str(project_id), "task_id": related_task_id},
                )
            related_metadata = evidence.get("metadata", {})
            if isinstance(related_metadata, Mapping) and related_metadata:
                if not self._metadata_matches(
                    self._metadata_for(related_row),
                    related_metadata,
                    run_id=str(verification_run_id or ""),
                ):
                    raise VerificationCleanupError(
                        "related task provenance does not match manifest",
                        code="target_provenance_mismatch",
                        status_code=409,
                        details={"project_id": str(project_id), "task_id": related_task_id},
                    )
            related_assertions = evidence.get("assertions", {})
            if isinstance(related_assertions, Mapping) and related_assertions:
                if not self._assertions_match(related_row, related_assertions):
                    raise VerificationCleanupError(
                        "related task row does not match manifest assertions",
                        code="target_assertion_mismatch",
                        status_code=409,
                        details={"project_id": str(project_id), "task_id": related_task_id},
                    )
        graph["tasks"] = task_ids
        graph["task_batches"] = {
            str(row.id): str(row.deletion_batch_id)
            for row in task_rows
            if getattr(row, "deletion_batch_id", None) is not None
        }
        local_rows = await _select_models(session, LocalTask, "project_id", [project_id])
        graph["local_tasks"] = _ids(row.id for row in local_rows)
        # Materialized Docs nodes may point at a project or be descendants of
        # its canonical root.  Walk descendants by exact IDs, never by title.
        node_rows = await _select_models(session, KnowledgeNode, "project_id", [project_id])
        node_ids = {row.id for row in node_rows}
        # The Project.knowledge_node_id foreign key is RESTRICT in the
        # production schema.  A malformed manifest must never clear a pointer
        # into the shared Personal Docs library or another project's tree.
        project_row = await session.get(Project, project_id)
        if project_row is None:
            raise VerificationCleanupError(
                "project target no longer exists",
                code="target_missing",
                status_code=409,
                details={"entity_type": "project", "entity_id": str(project_id)},
            )
        if getattr(project_row, "deleted_at", None) is not None:
            raise VerificationCleanupError(
                "project target is already deleted",
                code="project_already_deleted",
                status_code=409,
                details={"project_id": str(project_id)},
            )

        # Optional manifest anchors provide an additional identity fence for
        # one-time forensic cleanup.  They are never used as selectors, but a
        # changed space or Docs root must fail closed rather than allowing an
        # otherwise matching metadata marker to sweep a shared graph.
        anchor_map = anchors or {}
        project_anchor = anchor_map.get("project")
        if isinstance(project_anchor, Mapping):
            anchor_id = project_anchor.get("id")
            if anchor_id is not None and str(anchor_id).casefold() != str(project_id).casefold():
                raise VerificationCleanupError(
                    "project anchor does not match target",
                    code="project_anchor_mismatch",
                    status_code=409,
                    details={"project_id": str(project_id)},
                )
            metadata_key = project_anchor.get("metadata_key")
            metadata_value = project_anchor.get("metadata_value")
            if metadata_key is not None:
                if metadata_value is None or not self._metadata_matches(
                    self._metadata_for(project_row),
                    {str(metadata_key): metadata_value},
                    run_id="",
                ):
                    raise VerificationCleanupError(
                        "project anchor metadata does not match target",
                        code="project_anchor_mismatch",
                        status_code=409,
                        details={"project_id": str(project_id)},
                    )
            anchor_space_id = project_anchor.get("space_id")
            if anchor_space_id is not None and str(getattr(project_row, "space_id", "")).casefold() != str(anchor_space_id).casefold():
                raise VerificationCleanupError(
                    "project anchor space does not match target",
                    code="project_anchor_mismatch",
                    status_code=409,
                    details={"project_id": str(project_id)},
                )
        space_anchor = anchor_map.get("space")
        if isinstance(space_anchor, Mapping):
            await _validate_space_anchor(
                session,
                project_row=project_row,
                project_id=project_id,
                space_anchor=space_anchor,
            )
        root_id = getattr(project_row, "knowledge_node_id", None)
        knowledge_anchor = anchor_map.get("knowledge_root")
        if isinstance(knowledge_anchor, Mapping):
            anchor_root_id = knowledge_anchor.get("id")
            if anchor_root_id is None or str(root_id or "").casefold() != str(anchor_root_id).casefold():
                raise VerificationCleanupError(
                    "knowledge root anchor does not match project pointer",
                    code="shared_knowledge_root",
                    status_code=409,
                    details={"project_id": str(project_id)},
                )
            anchor_project_id = knowledge_anchor.get("project_id")
            anchor_library_id = knowledge_anchor.get("library_id") or knowledge_anchor.get("docs_library_id")
            anchor_parent_id = knowledge_anchor.get("parent_id")
            root_row = await session.get(KnowledgeNode, root_id) if root_id is not None else None
            if root_row is None:
                raise VerificationCleanupError(
                    "knowledge root anchor row is missing",
                    code="shared_knowledge_root",
                    status_code=409,
                    details={"project_id": str(project_id)},
                )
            for expected, actual in (
                (anchor_project_id, getattr(root_row, "project_id", None)),
                (anchor_library_id, getattr(root_row, "docs_library_id", None)),
                (anchor_parent_id, getattr(root_row, "parent_id", None)),
            ):
                if expected is not None and str(actual or "").casefold() != str(expected).casefold():
                    raise VerificationCleanupError(
                        "knowledge root anchor row changed",
                        code="shared_knowledge_root",
                        status_code=409,
                        details={"project_id": str(project_id)},
                    )
        if root_id is not None and root_id not in node_ids:
            root_row = await session.get(KnowledgeNode, root_id)
            if root_row is None or root_row.project_id != project_id:
                raise VerificationCleanupError(
                    "project knowledge root is outside the disposable graph",
                    code="shared_knowledge_root",
                    status_code=409,
                    details={"project_id": str(project_id)},
                )
            node_ids.add(root_id)
        graph["knowledge_root_id"] = str(root_id) if root_id is not None else None
        if node_ids:
            node_id_text_set = {str(value) for value in node_ids}
            foreign_project_roots = await session.execute(
                select(Project.id).where(
                    Project.knowledge_node_id.in_(tuple(node_ids)),
                    Project.id != project_id,
                )
            )
            if foreign_project_roots.first() is not None:
                raise VerificationCleanupError(
                    "knowledge root is referenced by another project",
                    code="shared_knowledge_root",
                    status_code=409,
                    details={"project_id": str(project_id)},
                )
        queue = list(node_ids)
        while queue:
            children = await _select_models(session, KnowledgeNode, "parent_id", queue)
            # A parent pointer alone is not sufficient ownership proof: a
            # malformed/shared tree may point a node from another Project at
            # this subtree.  Only capture descendants explicitly carrying
            # this project's identity.  Such rows are the contract emitted by
            # the verification harness and keep cross-project B/C nodes safe.
            foreign_children = [row for row in children if row.project_id != project_id]
            if foreign_children:
                raise VerificationCleanupError(
                    "knowledge graph crosses a protected project boundary",
                    code="shared_knowledge_node",
                    status_code=409,
                    details={"project_id": str(project_id)},
                )
            fresh = [row.id for row in children if row.id not in node_ids]
            node_ids.update(fresh)
            queue = fresh
        # Re-check after descendant expansion: references/edges on a child
        # node are just as capable of crossing into protected B/C content as
        # references on the root itself.
        if node_ids:
            # Import items and ClipIngest receipts do not carry a project_id;
            # their node FK alone is not sufficient ownership proof.  Validate
            # their owning job/topic before the generic FK projection sweep so
            # deleting an A node cannot mutate B/C provenance rows.
            try:
                await _validate_node_projection_ownership(
                    session,
                    project_id=project_id,
                    node_ids=node_ids,
                )
            except VerificationCleanupError:
                raise
            except Exception as exc:
                raise VerificationCleanupError(
                    "knowledge import/clip dependency graph could not be verified",
                    code="shared_knowledge_reference",
                    status_code=409,
                    details={"project_id": str(project_id)},
                ) from exc
            external_refs = await _select_models(
                session,
                ProjectKnowledgeRef,
                "knowledge_node_id",
                node_ids,
            )
            if any(getattr(ref, "project_id", None) != project_id for ref in external_refs):
                raise VerificationCleanupError(
                    "knowledge graph is referenced by another protected project",
                    code="shared_knowledge_reference",
                    status_code=409,
                    details={"project_id": str(project_id)},
                )
            edge_rows = [
                *await _select_models(session, KnowledgeEdge, "source_node_id", node_ids),
                *await _select_models(session, KnowledgeEdge, "target_node_id", node_ids),
            ]
            node_id_set = set(node_ids)
            if any(
                getattr(edge, "source_node_id", None) not in node_id_set
                or getattr(edge, "target_node_id", None) not in node_id_set
                for edge in edge_rows
            ):
                raise VerificationCleanupError(
                    "knowledge graph edge crosses a protected boundary",
                    code="shared_knowledge_edge",
                    status_code=409,
                    details={"project_id": str(project_id)},
                )
            node_id_set = set(node_ids)
            # Nullable target pointers on otherwise protected rows must not be
            # changed as a side effect of deleting the A subtree.  A row owned
            # by this same project is part of the exact auxiliary graph and is
            # removed there; every other owner is a fail-closed boundary.
            for model, column_name, owner_column in (
                (Task, "knowledge_node_id", "project_id"),
                (DocsCandidate, "target_node_id", "project_id"),
                (DocsClipIngestJob, "target_node_id", "project_id"),
                (ProjectQaEntry, "knowledge_node_id", "project_id"),
            ):
                referenced_rows = await _select_models(session, model, column_name, node_ids)
                if any(getattr(row, owner_column, None) != project_id for row in referenced_rows):
                    raise VerificationCleanupError(
                        "knowledge node pointer crosses a protected boundary",
                        code="shared_knowledge_reference",
                        status_code=409,
                        details={"project_id": str(project_id), "table": getattr(model, "__tablename__", model.__name__)},
                    )
            # A typed field value can point at an A node while the field row
            # itself belongs to a protected/B/C node.  The purge path must
            # never silently detach that cross-project reference as a side
            # effect.  Keep the boundary explicit and fail closed; same-node
            # field values are deleted with the exact subtree below.
            external_field_values = await session.execute(
                select(KnowledgeFieldValue.node_id, KnowledgeFieldValue.target_node_id).where(
                    KnowledgeFieldValue.target_node_id.in_(tuple(node_ids)),
                    ~KnowledgeFieldValue.node_id.in_(tuple(node_ids)),
                )
            )
            if external_field_values.first() is not None:
                raise VerificationCleanupError(
                    "knowledge field reference crosses a protected boundary",
                    code="shared_knowledge_reference",
                    status_code=409,
                    details={"project_id": str(project_id)},
                )
            # Keep this check extensible for new knowledge projections.  Any
            # mapped FK into the frozen node set must either belong to the
            # same project (or be one of the exact node-owned derivatives) or
            # the cleanup is refused.  This covers receipts/import items and
            # future rows that are not yet listed in the explicit graph.
            try:
                from ..memory.models.base import Base

                for mapper in Base.registry.mappers:
                    model = mapper.class_
                    if model in {KnowledgeImportItem, ClipIngestReceipt}:
                        # These node-only projections were validated against
                        # their owning job/topic above.  The generic fallback
                        # cannot infer ownership from a missing project_id and
                        # must not reclassify a valid A row as external.
                        continue
                    for column in model.__table__.columns:
                        if not any(
                            foreign_key.column.table.name == "knowledge_nodes"
                            for foreign_key in column.foreign_keys
                        ):
                            continue
                        referenced_rows = await _select_models(session, model, column.name, node_ids)
                        for referenced in referenced_rows:
                            referenced_id = str(getattr(referenced, "id", ""))
                            if model is KnowledgeNode and referenced_id in node_id_text_set:
                                continue
                            if model is Project and referenced_id == str(project_id):
                                continue
                            row_project_id = getattr(referenced, "project_id", None)
                            if row_project_id is not None and str(row_project_id) == str(project_id):
                                continue
                            row_node_id = getattr(referenced, "node_id", None)
                            if row_node_id is not None and str(row_node_id) in node_id_text_set:
                                continue
                            if model is KnowledgeEdge:
                                source_id = str(getattr(referenced, "source_node_id", ""))
                                target_id = str(getattr(referenced, "target_node_id", ""))
                                if source_id in node_id_text_set and target_id in node_id_text_set:
                                    continue
                            raise VerificationCleanupError(
                                "knowledge projection crosses a protected boundary",
                                code="shared_knowledge_reference",
                                status_code=409,
                                details={"project_id": str(project_id), "table": model.__table__.name},
                            )
            except VerificationCleanupError:
                raise
            except Exception as exc:
                raise VerificationCleanupError(
                    "knowledge dependency graph could not be verified",
                    code="shared_knowledge_reference",
                    status_code=409,
                    details={"project_id": str(project_id)},
                ) from exc
            external_root_rows = await _select_models(session, KnowledgeNode, "root_page_id", node_ids)
            if any(getattr(row, "id", None) not in node_id_set for row in external_root_rows):
                raise VerificationCleanupError(
                    "knowledge root pointer crosses a protected boundary",
                    code="shared_knowledge_root",
                    status_code=409,
                    details={"project_id": str(project_id)},
                )
            external_placements = await _select_models(session, KnowledgeNodePlacement, "parent_node_id", node_ids)
            if any(getattr(row, "node_id", None) not in node_id_set for row in external_placements):
                raise VerificationCleanupError(
                    "knowledge placement crosses a protected boundary",
                    code="shared_knowledge_reference",
                    status_code=409,
                    details={"project_id": str(project_id)},
                )
            # Apps are independent resources; a readme pointer into an A node
            # is not sufficient proof that the App itself is disposable.  Do
            # not null a B/C App pointer implicitly—require an isolated run to
            # register and purge that App as a separate artifact instead.
            external_apps = await _select_models(session, App, "readme_node_id", node_ids)
            if external_apps:
                raise VerificationCleanupError(
                    "knowledge node is referenced by an independent App",
                    code="shared_knowledge_reference",
                    status_code=409,
                    details={"project_id": str(project_id)},
                )
        reverse_projects = await _select_models(session, Project, "knowledge_node_id", node_ids)
        foreign_pointer_projects = [
            row for row in reverse_projects if getattr(row, "id", None) != project_id
        ]
        if foreign_pointer_projects:
            raise VerificationCleanupError(
                "knowledge graph is referenced by another protected project",
                code="shared_knowledge_node",
                status_code=409,
                details={"project_id": str(project_id)},
            )
        graph["knowledge_nodes"] = _ids(node_ids)
        session_rows = await _select_models(session, ConversationSession, "project_id", [project_id])
        graph["sessions"] = _ids(row.id for row in session_rows)
        if graph["sessions"]:
            graph["messages"] = _ids(await _select_ids(session, ConversationMessage, "session_id", graph["sessions"]))
            graph["session_participants"] = _ids(await _select_ids(session, ConversationParticipant, "session_id", graph["sessions"]))

        # Deleting a session/message can trigger ON DELETE SET NULL on rows
        # owned by another project (forks, memory jobs, agent/story records,
        # and other future models).  Such a side effect would mutate
        # protected B/C data even though the row itself is not selected.  Walk
        # the mapped FK graph and reject every external dependent; rows in the
        # exact A session/message graph, or rows explicitly scoped to this
        # project, are safe to remove with the graph.
        session_ids = tuple(graph.get("sessions", ()))
        message_ids = tuple(graph.get("messages", ()))
        protected_session_ids = {str(value) for value in session_ids}
        protected_message_ids = {str(value) for value in message_ids}
        if session_ids or message_ids:
            try:
                from ..memory.models.base import Base

                for mapper in Base.registry.mappers:
                    model = mapper.class_
                    for column in model.__table__.columns:
                        target_names = {
                            foreign_key.column.table.name
                            for foreign_key in column.foreign_keys
                            if foreign_key.column.table.name
                            in {"conversation_sessions", "conversation_messages"}
                        }
                        if not target_names:
                            continue
                        values: tuple[str, ...] = tuple(
                            sorted(
                                (protected_session_ids if "conversation_sessions" in target_names else set())
                                | (protected_message_ids if "conversation_messages" in target_names else set())
                            )
                        )
                        if not values:
                            continue
                        dependents = await _select_models(session, model, column.name, values)
                        for dependent in dependents:
                            dependent_id = str(getattr(dependent, "id", ""))
                            if model is ConversationSession and dependent_id in protected_session_ids:
                                continue
                            if model is ConversationMessage and dependent_id in protected_message_ids:
                                continue
                            dependent_session_id = getattr(dependent, "session_id", None)
                            if dependent_session_id is not None and str(dependent_session_id) in protected_session_ids:
                                continue
                            dependent_message_id = getattr(dependent, "message_id", None)
                            if dependent_message_id is not None and str(dependent_message_id) in protected_message_ids:
                                continue
                            row_project_id = getattr(dependent, "project_id", None)
                            if row_project_id is not None and str(row_project_id) == str(project_id):
                                continue
                            raise VerificationCleanupError(
                                "conversation graph crosses a protected boundary",
                                code="shared_conversation_graph",
                                status_code=409,
                                details={"project_id": str(project_id), "table": model.__table__.name},
                            )
            except VerificationCleanupError:
                raise
            except Exception as exc:
                raise VerificationCleanupError(
                    "conversation dependency graph could not be verified",
                    code="shared_conversation_graph",
                    status_code=409,
                    details={"project_id": str(project_id)},
                ) from exc
        run_rows = await _select_models(session, AgentRun, "project_id", [project_id])
        run_id_set = {str(row.id) for row in run_rows}
        # Some legacy execution rows predate the project_id column but retain
        # an exact session/message pointer.  Attribute those rows to this
        # project graph; otherwise they would survive a verified purge as
        # orphaned telemetry.  A null/unrelated pointer is not inferred.
        if session_ids:
            run_rows_by_session = await _select_models(session, AgentRun, "session_id", session_ids)
            for row in run_rows_by_session:
                row_project_id = getattr(row, "project_id", None)
                if row_project_id not in (None, project_id):
                    raise VerificationCleanupError(
                        "agent-run session reference crosses a protected boundary",
                        code="shared_agent_run",
                        status_code=409,
                        details={"project_id": str(project_id)},
                    )
                run_id_set.add(str(row.id))
        if message_ids:
            run_rows_by_message = await _select_models(session, AgentRun, "trigger_message_id", message_ids)
            for row in run_rows_by_message:
                row_project_id = getattr(row, "project_id", None)
                if row_project_id not in (None, project_id):
                    raise VerificationCleanupError(
                        "agent-run message reference crosses a protected boundary",
                        code="shared_agent_run",
                        status_code=409,
                        details={"project_id": str(project_id)},
                    )
                run_id_set.add(str(row.id))
        # AgentRun children may carry only ``parent_run_id``/``root_run_id``
        # and therefore not be discovered by the project_id query.  Walk the
        # exact self-referential graph now, refusing a child from another
        # project before the canonical project delete can reach it.
        frontier = list(run_id_set)
        while frontier:
            parent_rows = await _select_models(session, AgentRun, "parent_run_id", frontier)
            root_rows = await _select_models(session, AgentRun, "root_run_id", frontier)
            discovered: list[str] = []
            for row in (*parent_rows, *root_rows):
                row_project_id = getattr(row, "project_id", None)
                row_session_id = getattr(row, "session_id", None)
                row_trigger_message_id = getattr(row, "trigger_message_id", None)
                if row_project_id != project_id and not (
                    row_project_id is None
                    and (
                        str(row_session_id) in protected_session_ids
                        or str(row_trigger_message_id) in protected_message_ids
                    )
                ):
                    raise VerificationCleanupError(
                        "agent-run graph crosses a protected project boundary",
                        code="shared_agent_run",
                        status_code=409,
                        details={"project_id": str(project_id)},
                    )
                row_id = str(getattr(row, "id", ""))
                if row_id and row_id not in run_id_set:
                    run_id_set.add(row_id)
                    discovered.append(row_id)
            frontier = discovered
        graph["agent_runs"] = tuple(sorted(run_id_set))
        if graph["agent_runs"]:
            edge_rows = [
                *await _select_models(session, AgentRunEdge, "parent_run_id", graph["agent_runs"]),
                *await _select_models(session, AgentRunEdge, "child_run_id", graph["agent_runs"]),
            ]
            if edge_rows:
                endpoint_ids = {
                    *(_ids(getattr(edge, "parent_run_id", None) for edge in edge_rows)),
                    *(_ids(getattr(edge, "child_run_id", None) for edge in edge_rows)),
                }
                endpoint_rows = await _select_models(session, AgentRun, "id", endpoint_ids)
                endpoint_map = {str(getattr(row, "id", "")): row for row in endpoint_rows}
                if len(endpoint_map) != len(endpoint_ids) or any(
                    endpoint_id not in run_id_set
                    and getattr(endpoint_map.get(endpoint_id), "project_id", None) != project_id
                    for endpoint_id in endpoint_ids
                ):
                    raise VerificationCleanupError(
                        "agent-run edge crosses a protected boundary",
                        code="shared_agent_run",
                        status_code=409,
                        details={"project_id": str(project_id)},
                    )
            # Other telemetry/provenance tables may reference an AgentRun via
            # nullable SET NULL FKs.  Rows with an explicit project owner, or
            # the exact run-scoped event/outbox/edge tables that are purged
            # below, are part of this A graph; an unowned external row is a
            # protected-boundary violation rather than an implicit detach.
            try:
                from ..memory.models.base import Base

                run_scoped_tables = {
                    AgentRunEvent,
                    AgentRunToolCall,
                    ConversationDispatchOutbox,
                    AgentRunEdge,
                }
                for mapper in Base.registry.mappers:
                    model = mapper.class_
                    for column in model.__table__.columns:
                        if not any(
                            foreign_key.column.table.name == "agent_runs"
                            for foreign_key in column.foreign_keys
                        ):
                            continue
                        referenced_rows = await _select_models(session, model, column.name, graph["agent_runs"])
                        for referenced in referenced_rows:
                            referenced_id = str(getattr(referenced, "id", ""))
                            if model is AgentRun and referenced_id in run_id_set:
                                continue
                            row_project_id = getattr(referenced, "project_id", None)
                            if row_project_id is not None and str(row_project_id) == str(project_id):
                                continue
                            if model in run_scoped_tables:
                                continue
                            raise VerificationCleanupError(
                                "agent-run dependent crosses a protected boundary",
                                code="shared_agent_run",
                                status_code=409,
                                details={"project_id": str(project_id), "table": model.__table__.name},
                            )
            except VerificationCleanupError:
                raise
            except Exception as exc:
                raise VerificationCleanupError(
                    "agent-run dependency graph could not be verified",
                    code="shared_agent_run",
                    status_code=409,
                    details={"project_id": str(project_id)},
                ) from exc
        # Include every known project-scoped table in the forensic snapshot.
        # This is still bounded to one exact project UUID and excludes the
        # audit/provenance tables by design.
        for model, column_name in _PROJECT_AUXILIARY:
            name = getattr(model, "__tablename__", model.__name__).lower()
            if name in {"projects", "conversation_sessions", "agent_runs"}:
                continue
            ids = await _select_ids(session, model, column_name, [project_id])
            if ids:
                graph[name] = _ids(ids)
        memory_ids = graph.get("context_memories", ())
        if memory_ids:
            audit_ids = await _select_ids(session, ContextMemoryAudit, "memory_id", memory_ids)
            if audit_ids:
                graph["context_memory_audits"] = _ids(audit_ids)
        json_audit_ids = await _select_context_audit_project_ids(
            session,
            project_id,
            allowed_memory_ids=memory_ids,
        )
        if json_audit_ids:
            graph["context_memory_audits"] = _ids(
                (*graph.get("context_memory_audits", ()), *json_audit_ids)
            )

        expected_graph = entry.graph
        if expected_graph:
            for key, expected in expected_graph.items():
                actual = graph.get(key, ())
                if tuple(sorted(expected)) != tuple(sorted(actual)):
                    raise VerificationCleanupError(
                        "project graph changed since the manifest was frozen",
                        code="graph_mismatch",
                        status_code=409,
                        details={"project_id": str(project_id), "graph_key": key},
                    )
        expected_counts = dict(entry.counts)
        for key, expected in expected_counts.items():
            actual_key = key
            if key == "projects":
                actual = 1
            else:
                actual = len(graph.get(key, ()))
            if actual != expected:
                raise VerificationCleanupError(
                    "project graph count changed since the manifest was frozen",
                    code="graph_count_mismatch",
                    status_code=409,
                    details={"project_id": str(project_id), "key": key, "expected": expected, "actual": actual},
                )
        return graph

    async def _snapshot_task(self, session: AsyncSession, task_id: UUID, entry: ManifestEntry) -> dict[str, Any]:
        """Freeze one standalone task's complete descendant tree.

        ``TaskManagementService.delete_task`` tombstones a task tree, and the
        scoped purge refuses a partial tree.  Capture descendants before the
        canonical call so a standalone manifest entry cannot accidentally
        leave child rows behind or sweep a child from another project.
        """

        root = await session.get(Task, task_id)
        if root is None:
            raise VerificationCleanupError(
                "manifest target no longer exists",
                code="target_missing",
                status_code=409,
                details={"entity_type": "task", "entity_id": str(task_id)},
            )
        task_ids: set[Any] = {root.id}
        queue: list[Any] = [root.id]
        while queue:
            children = await _select_models(session, Task, "parent_task_id", queue)
            foreign_children = [row for row in children if getattr(row, "project_id", None) != getattr(root, "project_id", None)]
            if foreign_children:
                raise VerificationCleanupError(
                    "task graph crosses a protected project boundary",
                    code="shared_task_tree",
                    status_code=409,
                    details={"task_id": str(task_id)},
                )
            fresh = [row.id for row in children if row.id not in task_ids]
            task_ids.update(fresh)
            queue = fresh
        legacy_local_ids = {
            getattr(row, "legacy_local_task_id", None)
            for row in await _select_models(session, Task, "id", task_ids)
            if getattr(row, "legacy_local_task_id", None) is not None
        }
        if legacy_local_ids:
            legacy_rows = await _select_models(session, LocalTask, "id", legacy_local_ids)
            legacy_map = {getattr(row, "id", None): row for row in legacy_rows}
            if len(legacy_map) != len(legacy_local_ids) or any(
                getattr(legacy_map.get(local_id), "project_id", None)
                != getattr(root, "project_id", None)
                for local_id in legacy_local_ids
            ):
                raise VerificationCleanupError(
                    "task legacy graph crosses a protected project boundary",
                    code="shared_task_tree",
                    status_code=409,
                    details={"task_id": str(task_id)},
                )
        graph: dict[str, Any] = {
            "root_task_id": str(task_id),
            "tasks": _ids(task_ids),
            "task_batches": {
                str(row.id): str(row.deletion_batch_id)
                for row in await _select_models(session, Task, "id", task_ids)
                if getattr(row, "deletion_batch_id", None) is not None
            },
            "project_id": str(root.project_id) if getattr(root, "project_id", None) is not None else None,
        }
        if entry.graph:
            for key, expected in entry.graph.items():
                actual = graph.get(key, ())
                if tuple(sorted(expected)) != tuple(sorted(actual)):
                    raise VerificationCleanupError(
                        "task graph changed since the manifest was frozen",
                        code="graph_mismatch",
                        status_code=409,
                        details={"task_id": str(task_id), "graph_key": key},
                    )
        for key, expected in entry.counts.items():
            actual = len(graph.get(key, ()))
            if key == "tasks":
                actual = len(graph["tasks"])
            if actual != expected:
                raise VerificationCleanupError(
                    "task graph count changed since the manifest was frozen",
                    code="graph_count_mismatch",
                    status_code=409,
                    details={"task_id": str(task_id), "key": key, "expected": expected, "actual": actual},
                )
        return graph

    async def _snapshot(self, session: AsyncSession, manifest: CleanupManifest, rows: Mapping[tuple[str, str], Any]) -> dict[str, Any]:
        graph: dict[str, Any] = {
            "projects": {},
            "tasks": [],
            "task_graphs": {},
            "users": [],
        }
        for entry in manifest.entries:
            if entry.entity_type == "project":
                graph["projects"][entry.entity_id] = await self._snapshot_project(
                    session,
                    _uuid_or_none(entry.entity_id),  # type: ignore[arg-type]
                    entry,
                    manifest.anchors,
                    manifest.entity_evidence,
                    manifest.run_id,
                )
            elif entry.entity_type == "task":
                graph["task_graphs"][entry.entity_id] = await self._snapshot_task(
                    session,
                    _uuid_or_none(entry.entity_id),  # type: ignore[arg-type]
                    entry,
                )
                graph["tasks"].extend(graph["task_graphs"][entry.entity_id].get("tasks", ()))
            else:
                graph["users"].append(entry.entity_id)
        # A top-level expected count is a hard fence.  Missing keys are not
        # inferred, but present keys must match exactly.
        project_graphs = list(graph["projects"].values())
        task_ids = set(graph["tasks"])
        node_ids = set()
        session_ids = set()
        message_ids = set()
        for item in project_graphs:
            task_ids.update(item.get("tasks", ()))
            node_ids.update(item.get("knowledge_nodes", ()))
            session_ids.update(item.get("sessions", ()))
            message_ids.update(item.get("messages", ()))
        actual_counts = {
            "projects": len(graph["projects"]),
            "tasks": len(task_ids),
            "users": len(graph["users"]),
            "knowledge_nodes": len(node_ids),
            "sessions": len(session_ids),
            "messages": len(message_ids),
        }
        graph["tasks"] = sorted(task_ids)
        graph["knowledge_nodes"] = sorted(node_ids)
        graph["sessions"] = sorted(session_ids)
        graph["messages"] = sorted(message_ids)
        for key, expected in manifest.counts.items():
            actual = actual_counts.get(key, sum(len(item.get(key, ())) for item in graph["projects"].values()))
            if actual != expected:
                raise VerificationCleanupError(
                    "manifest entity count does not match the database",
                    code="manifest_count_mismatch",
                    status_code=409,
                    details={"key": key, "expected": expected, "actual": actual},
                )
        graph["counts"] = actual_counts
        graph["digest"] = _sha256(graph)
        return graph

    async def preview(
        self,
        session: AsyncSession,
        *,
        run_id: UUID | str,
        manifest: CleanupManifest | Mapping[str, Any] | str | Path,
        actor_user_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        frozen = self._manifest(manifest)
        if str(run_id) != frozen.run_id:
            raise VerificationCleanupError(
                "run_id does not match manifest",
                code="run_id_mismatch",
                status_code=409,
            )
        rows = await _maybe_await(self._load_target_rows(session, frozen))
        graph = await _maybe_await(self._snapshot(session, frozen, rows))
        preview_digest = _sha256({"manifest": frozen.digest, "graph": graph})
        return {
            "run_id": frozen.run_id,
            "source": frozen.source,
            "manifest_digest": frozen.digest,
            "digest": preview_digest,
            "preview_digest": preview_digest,
            "status": "preview",
            "eligible": True,
            "category_a": [entry.to_dict() for entry in frozen.entries],
            "protected": [],
            "counts": graph["counts"],
            "graph": graph,
        }

    async def list_eligible(
        self,
        session: AsyncSession,
        *,
        run_id: UUID | str | None = None,
        manifest: CleanupManifest | Mapping[str, Any] | str | Path | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """List provenance-approved runs without inferring targets.

        A manifest is required to produce a target preview.  Without one the
        coordinator delegates to the provenance service's bounded listing
        API; if that service is not installed, an empty list is returned
        rather than scanning the normal database for name/age heuristics.
        """

        if limit <= 0 or limit > 500:
            raise VerificationCleanupError(
                "limit must be between 1 and 500",
                code="invalid_limit",
                status_code=422,
            )
        if manifest is not None:
            if run_id is None:
                frozen = self._manifest(manifest)
                run_id = frozen.run_id
            return await self.preview(session, run_id=run_id, manifest=manifest)
        result = await self._call_provenance(
            ("list_eligible", "list_cleanup_runs", "list_verification_runs", "list_runs"),
            session,
            limit=limit,
        )
        if isinstance(result, Mapping):
            payload = dict(result)
        elif isinstance(result, Sequence) and not isinstance(result, (str, bytes)):
            payload = {"runs": [dict(item) if isinstance(item, Mapping) else {"run_id": str(item)} for item in result]}
        else:
            payload = {"runs": []}
        runs = payload.get("runs", [])
        if not isinstance(runs, list):
            runs = []
            payload["runs"] = runs
        normalized_runs: list[Any] = []
        for run in runs:
            if isinstance(run, Mapping):
                normalized_runs.append(dict(run))
                continue
            to_dict = getattr(run, "to_dict", None)
            if callable(to_dict):
                try:
                    value = to_dict()
                except Exception:
                    value = None
                if isinstance(value, Mapping):
                    normalized_runs.append(dict(value))
                    continue
            normalized_runs.append({"run_id": str(getattr(run, "run_id", run)), "classification": "A"})
        runs = normalized_runs
        payload["runs"] = runs
        selectors = payload.get("selectors")
        if not isinstance(selectors, list):
            selectors = []
            for run in runs:
                if not isinstance(run, Mapping):
                    continue
                raw_id = run.get("run_id") or run.get("id")
                if not raw_id:
                    continue
                selector = {
                    "type": "verification_run",
                    "id": str(raw_id),
                    "classification": str(run.get("classification", run.get("category", "A"))).upper(),
                }
                selectors.append(selector)
        # Normalize all selector projections (including those supplied by a
        # provenance adapter) to the route/UI contract without upgrading a
        # protected run.  ``disposable=true`` is the only default-A signal.
        normalized_selectors: list[dict[str, Any]] = []
        for selector in selectors:
            if not isinstance(selector, Mapping):
                continue
            value = dict(selector)
            marker = value.get("classification", value.get("category", value.get("disposition")))
            if not isinstance(marker, str) or marker.strip().upper() not in _ALLOWED_CATEGORIES:
                marker = "A" if value.get("disposable") is True else "C"
            marker = marker.strip().upper()
            value["classification"] = marker
            value.setdefault("category", marker)
            value.setdefault("disposition", marker)
            normalized_selectors.append(value)
        selectors = normalized_selectors
        payload["selectors"] = selectors
        payload.setdefault("status", "preview")
        payload.setdefault("preview_digest", _sha256(selectors))
        return payload

    async def _canonical_delete_task(self, session: AsyncSession, task_id: UUID, actor_user_id: UUID) -> Mapping[str, Any]:
        service = self.task_service or TaskManagementService()
        method = getattr(service, "delete_task", None)
        if not callable(method):
            raise VerificationCleanupError(
                "canonical task deletion is unavailable",
                code="canonical_delete_unavailable",
                status_code=500,
            )
        # Newer task services may expose a transaction-aware ``commit``
        # switch.  Use it when advertised so the coordinator can keep the
        # graph atomic; the current production service predates the switch and
        # is called without speculative kwargs.
        kwargs: dict[str, Any] = {"user_id": actor_user_id, "task_id": task_id}
        try:
            parameters = inspect.signature(method).parameters
            if "commit" in parameters:
                kwargs["commit"] = False
            elif any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
                kwargs["commit"] = False
        except (TypeError, ValueError):
            pass
        result = method(session, **kwargs)
        result = await _maybe_await(result)
        if isinstance(result, Mapping):
            return result
        return {"task_id": str(task_id), "result": result}

    async def _canonical_delete_project(self, session: AsyncSession, project_id: UUID) -> bool:
        method = getattr(self.project_repository, "delete_project", None)
        if not callable(method):
            raise VerificationCleanupError(
                "canonical project deletion is unavailable",
                code="canonical_delete_unavailable",
                status_code=500,
            )
        kwargs: dict[str, Any] = {"delete_workspace": True}
        try:
            parameters = inspect.signature(method).parameters
            if "commit" in parameters:
                kwargs["commit"] = False
            elif any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
                kwargs["commit"] = False
            if "delete_workspace" not in parameters and not any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
            ):
                kwargs.pop("delete_workspace", None)
        except (TypeError, ValueError):
            pass
        # Do not retry after invocation-time TypeError: an exception raised by
        # the canonical implementation itself must roll back the transaction,
        # not be mistaken for a legacy signature mismatch and run twice.
        result = method(session, project_id, **kwargs)
        return bool(await _maybe_await(result))

    async def _purge_task_graph(
        self,
        session: AsyncSession,
        task_ids: Sequence[str],
        *,
        expected_batches: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        uuids = [value for value in (_uuid_or_none(item) for item in task_ids) if value is not None]
        counts: dict[str, int] = {}
        if not uuids:
            return counts
        # Prefer the canonical scoped purge added to TaskManagementService.
        # It validates every tombstone/batch and records a ``purged`` audit
        # event, unlike a generic retention scan.  A legacy mixed-version
        # service falls back to the exact-ID ORM path below.
        service = self.task_service or TaskManagementService()
        scoped_purge = getattr(service, "purge_deleted_task_ids", None)
        if callable(scoped_purge):
            expected = {
                str(task_id): batch_id
                for task_id, batch_id in (expected_batches or {}).items()
                if batch_id
            }
            if len(expected) != len({str(item) for item in task_ids}):
                raise VerificationCleanupError(
                    "task purge requires a deletion batch for every frozen task",
                    code="task_batch_missing",
                    status_code=409,
                )
            result = scoped_purge(
                session,
                task_ids=uuids,
                expected_batch_ids=expected,
                commit=False,
            )
            result = await _maybe_await(result)
            if isinstance(result, Mapping):
                normalized = dict(result)
                missing = tuple(normalized.get("missing_task_ids", ()) or ())
                if missing:
                    # A missing row is only idempotent when the enclosing
                    # verification cleanup ledger already proves completion.
                    # During an active run, silently purging the remainder
                    # would turn a TOCTOU race/partial prior attempt into an
                    # unbounded deletion, so fail closed.
                    raise VerificationCleanupError(
                        "a frozen task target disappeared before purge",
                        code="task_target_missing",
                        status_code=409,
                        details={"task_ids": [str(value) for value in missing]},
                    )
                if "tasks" not in normalized and "purged_tasks" in normalized:
                    normalized["tasks"] = normalized["purged_tasks"]
                return normalized
            return {"result": result}
        child_ids = await _select_ids(session, Task, "parent_task_id", uuids)
        foreign_children = [child_id for child_id in child_ids if child_id not in uuids]
        if foreign_children:
            raise VerificationCleanupError(
                "task purge requires the complete deleted task tree",
                code="shared_task_tree",
                status_code=409,
            )
        # Detach self-references before deleting rows.  Dependency/relation
        # edges touching an A task are disposable edges; the referenced B/C
        # task itself is never selected.
        await session.execute(update(Task).where(Task.id.in_(uuids)).values(parent_task_id=None))
        for model, column_name, label in _TASK_CHILDREN:
            if model is TaskDependency and column_name == "task_id":
                count = await _delete_where(session, model, "task_id", uuids)
                count += await _delete_where(session, model, "depends_on_task_id", uuids)
            elif model is TaskRelation:
                count = await _delete_where(session, model, column_name, uuids)
            elif model is TimeEntry:
                count = await _delete_where(session, model, column_name, uuids)
            elif model is TaskOccurrence:
                occurrence_ids = await _select_ids(session, model, column_name, uuids)
                # NotificationDelivery may reference an occurrence without
                # carrying the parent task_id (for example a materialized
                # reminder).  Delete those exact rows before removing the
                # occurrence; otherwise the NO ACTION FK blocks the purge and
                # a retry could be tempted to broaden the selector.
                occurrence_delivery_count = await _delete_where(
                    session,
                    NotificationDelivery,
                    "occurrence_id",
                    occurrence_ids,
                )
                counts["notification_deliveries"] = counts.get(
                    "notification_deliveries", 0
                ) + occurrence_delivery_count
                count = await _delete_where(session, TimeEntry, "occurrence_id", occurrence_ids)
                count += await _delete_where(session, model, "id", occurrence_ids)
            else:
                count = await _delete_where(session, model, column_name, uuids)
            counts[getattr(model, "__tablename__", model.__name__)] = counts.get(getattr(model, "__tablename__", model.__name__), 0) + count
        counts["tasks"] = await _delete_where(session, Task, "id", uuids)
        return counts

    async def _purge_project_graph(self, session: AsyncSession, project_id: UUID, graph: Mapping[str, Any], *, delete_project_row: bool = True) -> dict[str, int]:
        counts: dict[str, int] = {}
        task_ids = tuple(graph.get("tasks", ()))
        counts.update(
            await self._purge_task_graph(
                session,
                task_ids,
                expected_batches=graph.get("task_batches", {}),
            )
        )
        local_task_ids = tuple(graph.get("local_tasks", ()))
        if local_task_ids:
            counts["task_events"] = await _delete_where(session, TaskEvent, "task_id", local_task_ids)
            counts["task_execution_sessions"] = await _delete_where(session, TaskExecutionSession, "task_id", local_task_ids)
            counts["local_tasks"] = await _delete_where(session, LocalTask, "id", local_task_ids)

        session_ids = tuple(graph.get("sessions", ()))
        if session_ids:
            # Message parent pointers are self-referential and must be
            # detached before the canonical session child rows are removed.
            message_ids = await _select_ids(session, ConversationMessage, "session_id", session_ids)
            if message_ids:
                # Boundary checks in ``_snapshot_project`` reject external
                # dependents.  Keep the defensive updates scoped to the A
                # session/message IDs as well, so a concurrent protected row
                # can never be modified by this purge.
                await session.execute(
                    update(ConversationMessage)
                    .where(
                        ConversationMessage.session_id.in_(session_ids),
                        ConversationMessage.parent_message_id.in_(message_ids),
                    )
                    .values(parent_message_id=None)
                )
                await session.execute(
                    update(ConversationSession)
                    .where(
                        ConversationSession.id.in_(session_ids),
                        ConversationSession.forked_from_message_id.in_(message_ids),
                    )
                    .values(forked_from_message_id=None)
                )
            counts["conversation_participants"] = await _delete_where(session, ConversationParticipant, "session_id", session_ids)
            counts["conversation_messages"] = await _delete_where(session, ConversationMessage, "session_id", session_ids)
            counts["conversation_history"] = await _delete_where(session, ConversationHistory, "session_id", session_ids)
            # Archives use a string session identity and have no FK.
            counts["conversation_archives"] = await _delete_where(session, ConversationArchive, "original_session_id", [str(value) for value in session_ids])
            counts["conversation_sessions"] = await _delete_where(session, ConversationSession, "id", session_ids)

        run_ids = tuple(graph.get("agent_runs", ()))
        if run_ids:
            # Include child runs discovered before purge; the parent graph is
            # exact and bounded to these IDs.  A parent_run_id alone is not
            # ownership proof: a malformed/cross-project child must never be
            # swept as part of an A project.  Fail closed on that boundary.
            # Walk both self-referential pointers to a fixed point.  A
            # one-level lookup would leave grandchildren (or a run carrying
            # only root_run_id) pointing at a deleted A row and could either
            # block the purge or cascade into protected data.
            seen_run_ids = {str(value) for value in run_ids}
            frontier = list(run_ids)
            graph_session_ids = {str(value) for value in graph.get("sessions", ())}
            graph_message_ids = {str(value) for value in graph.get("messages", ())}
            while frontier:
                parent_rows = await _select_models(session, AgentRun, "parent_run_id", frontier)
                root_rows = await _select_models(session, AgentRun, "root_run_id", frontier)
                discovered = []
                for row in (*parent_rows, *root_rows):
                    row_project_id = getattr(row, "project_id", None)
                    row_session_id = getattr(row, "session_id", None)
                    row_trigger_message_id = getattr(row, "trigger_message_id", None)
                    if row_project_id != project_id and not (
                        row_project_id is None
                        and (
                            str(row_session_id) in graph_session_ids
                            or str(row_trigger_message_id) in graph_message_ids
                        )
                    ):
                        raise VerificationCleanupError(
                            "agent-run graph crosses a protected project boundary",
                            code="shared_agent_run",
                            status_code=409,
                            details={"project_id": str(project_id)},
                        )
                    row_id = str(getattr(row, "id", ""))
                    if row_id and row_id not in seen_run_ids:
                        seen_run_ids.add(row_id)
                        discovered.append(row_id)
                frontier = discovered
            all_run_ids = tuple(dict.fromkeys((*run_ids, *sorted(seen_run_ids - {str(value) for value in run_ids}))))
            counts["conversation_dispatch_outbox"] = await _delete_where(session, ConversationDispatchOutbox, "run_id", all_run_ids)
            counts["agent_run_events"] = await _delete_where(session, AgentRunEvent, "run_id", all_run_ids)
            counts["agent_run_tool_calls"] = await _delete_where(session, AgentRunToolCall, "run_id", all_run_ids)
            counts["agent_run_edges"] = await _delete_where(session, AgentRunEdge, "parent_run_id", all_run_ids)
            counts["agent_run_edges"] += await _delete_where(session, AgentRunEdge, "child_run_id", all_run_ids)
            await session.execute(update(AgentRun).where(AgentRun.id.in_(all_run_ids)).values(root_run_id=None, parent_run_id=None))
            counts["agent_runs"] = await _delete_where(session, AgentRun, "id", all_run_ids)

        # Re-read and lock the canonical pointer immediately before clearing
        # it.  A concurrent project update must never let this cleanup null a
        # newly-shared/B/C knowledge root that was not in the frozen graph.
        current_project = await session.scalar(
            select(Project).where(Project.id == project_id).with_for_update()
        )
        if current_project is None:
            raise VerificationCleanupError(
                "project disappeared before graph purge",
                code="target_missing",
                status_code=409,
                details={"project_id": str(project_id)},
            )
        expected_root = graph.get("knowledge_root_id")
        actual_root = getattr(current_project, "knowledge_node_id", None)
        if str(actual_root or "") != str(expected_root or ""):
            raise VerificationCleanupError(
                "project knowledge root changed before graph purge",
                code="shared_knowledge_root",
                status_code=409,
                details={"project_id": str(project_id)},
            )
        # Clear the RESTRICT canonical pointer before deleting its exact node
        # subtree and final Project row.
        await session.execute(
            update(Project)
            .where(Project.id == project_id)
            .values(knowledge_node_id=None)
        )
        node_ids = tuple(graph.get("knowledge_nodes", ()))
        if node_ids:
            node_uuid_ids = [value for value in (_uuid_or_none(item) for item in node_ids) if value is not None]
            # Clear project canonical pointer and node self refs before
            # deleting the captured subtree.  Never select nodes by title or
            # project after the snapshot.
            await session.execute(update(Project).where(Project.id == project_id, Project.knowledge_node_id.in_(node_uuid_ids)).values(knowledge_node_id=None))
            await session.execute(update(KnowledgeNode).where(KnowledgeNode.id.in_(node_uuid_ids)).values(parent_id=None, root_page_id=None))
            for model, column in (
                (KnowledgeNodeShare, "node_id"),
                (KnowledgeNodePlacement, "node_id"),
                (KnowledgeEdge, "source_node_id"),
                (KnowledgeEdge, "target_node_id"),
                (KnowledgeFieldValue, "node_id"),
                (KnowledgeNodeSupertag, "node_id"),
                (KnowledgeRevision, "node_id"),
                (KnowledgeAttachment, "node_id"),
                (KnowledgeAiSuggestion, "node_id"),
                (KnowledgeEditEvent, "node_id"),
                (KnowledgeImportItem, "node_id"),
                (KnowledgeSearchIndex, "node_id"),
            ):
                counts[getattr(model, "__tablename__", model.__name__)] = counts.get(getattr(model, "__tablename__", model.__name__), 0) + await _delete_where(session, model, column, node_uuid_ids)
            # Re-check the nullable cross-node target immediately before the
            # exact node delete.  External/B/C field rows are not in this
            # graph and must never be implicitly detached or otherwise
            # mutated by cleanup; fail closed if one appeared after preview.
            external_field_values = await session.execute(
                select(KnowledgeFieldValue.node_id, KnowledgeFieldValue.target_node_id)
                .where(
                    KnowledgeFieldValue.target_node_id.in_(node_uuid_ids),
                    ~KnowledgeFieldValue.node_id.in_(node_uuid_ids),
                )
                .with_for_update()
            )
            if external_field_values.first() is not None:
                raise VerificationCleanupError(
                    "knowledge field reference crosses a protected boundary",
                    code="shared_knowledge_reference",
                    status_code=409,
                    details={"project_id": str(project_id)},
                )
            # Placement parent edges and field targets can point into the same
            # exact subtree from the other direction.
            counts["knowledge_node_placements"] += await _delete_where(session, KnowledgeNodePlacement, "parent_node_id", node_uuid_ids)
            counts["knowledge_nodes"] = await _delete_where(session, KnowledgeNode, "id", node_uuid_ids)

        memory_ids = tuple(graph.get("context_memories", ()))
        if memory_ids:
            counts["context_memory_audits"] = await _delete_where(
                session, ContextMemoryAudit, "memory_id", memory_ids
            )
        audit_ids = tuple(graph.get("context_memory_audits", ()))
        if audit_ids:
            counts["context_memory_audits"] = counts.get("context_memory_audits", 0) + await _delete_where(
                session, ContextMemoryAudit, "id", audit_ids
            )

        proposal_ids = tuple(graph.get("skill_proposals", ()))
        if proposal_ids:
            # Keep this explicit even though the FK is currently CASCADE; it
            # makes rolling-schema cleanup deterministic and avoids retaining
            # append-only proposal history for a deleted verification project.
            counts["skill_proposal_history"] = await _delete_where(
                session, SkillProposalHistory, "proposal_id", proposal_ids
            )

        # ProjectRepository already removed most of these rows.  Repeating an
        # exact-ID delete is idempotent and closes the remaining FK graph.
        for model, column_name in _PROJECT_AUXILIARY:
            name = getattr(model, "__tablename__", model.__name__)
            if name in {"conversation_sessions", "agent_runs"}:
                continue
            count = await _delete_where(session, model, column_name, [project_id])
            if count:
                counts[name] = counts.get(name, 0) + count
        if delete_project_row:
            counts["projects"] = await _delete_where(session, Project, "id", [project_id])
        await session.flush()
        return counts

    async def _prepare_project_canonical_delete(
        self,
        session: AsyncSession,
        graph: Mapping[str, Any],
    ) -> dict[str, int]:
        """Remove only LocalTask children that block ProjectRepository.

        ``ProjectRepository.delete_project`` is the lifecycle authority, but
        older databases have NO ACTION FKs from ``task_events`` and
        ``task_execution_sessions`` to ``local_tasks``.  Deleting these exact
        snapshotted child IDs before invoking the repository keeps the
        canonical project operation dependency-safe without broad SQL.
        """

        local_task_ids = tuple(graph.get("local_tasks", ()))
        if not local_task_ids:
            return {}
        counts = {
            "task_events": await _delete_where(session, TaskEvent, "task_id", local_task_ids),
            "task_execution_sessions": await _delete_where(session, TaskExecutionSession, "task_id", local_task_ids),
        }
        await session.flush()
        return counts

    async def _validate_user_purge(
        self,
        session: AsyncSession,
        user_id: UUID,
        *,
        allowed_project_ids: Iterable[str] = (),
    ) -> None:
        """Preflight ownership/FK invariants before any canonical mutation.

        UserRepository performs the same checks while deleting, but the
        coordinator invokes this read-only pass before task/project commits so
        a mixed manifest cannot leave a partial purge when a user is blocked.
        """

        allowed_projects = {str(value) for value in allowed_project_ids}
        blockers: list[str] = []
        owned_projects = await _select_ids(session, Project, "owner_id", [user_id])
        if any(str(value) not in allowed_projects for value in owned_projects):
            blockers.append("projects")
        for model, column_name, label in (
            (Space, "owner_id", "spaces"),
            (DocsLibrary, "owner_user_id", "docs_libraries"),
            (App, "owner_user_id", "apps"),
        ):
            if await _select_ids(session, model, column_name, [user_id]):
                blockers.append(label)
        if blockers:
            raise VerificationCleanupError(
                "user purge is blocked by owned resources",
                code="user_ownership_blocked",
                status_code=409,
                details={"user_id": str(user_id), "resources": blockers},
            )
        # Refuse rather than partially mutate when an unmapped NO ACTION FK
        # still points at the user (Docs creators/owners are common examples).
        # CASCADE/SET NULL references are safe for the canonical repository;
        # this loop only reads and reports the exact target UUID.
        try:
            from ..memory.models.base import Base

            for mapper in Base.registry.mappers:
                model = mapper.class_
                for column in model.__table__.columns:
                    for foreign_key in column.foreign_keys:
                        if foreign_key.column.table.name != "users":
                            continue
                        ondelete = str(foreign_key.ondelete or "").upper()
                        if ondelete in {"SET NULL", "CASCADE"}:
                            continue
                        dependent_rows = await _select_models(session, model, column.name, [user_id])
                        for dependent in dependent_rows:
                            # A project-owned row is safe when that exact
                            # project is also frozen for deletion earlier in
                            # this manifest.  Keep every unrelated/B/C row a
                            # hard blocker rather than nulling it implicitly.
                            if model is Project and str(getattr(dependent, "id", "")) in allowed_projects:
                                continue
                            row_project_id = getattr(dependent, "project_id", None)
                            if row_project_id is not None and str(row_project_id) in allowed_projects:
                                continue
                            raise VerificationCleanupError(
                                "user purge is blocked by dependent history",
                                code="user_dependency_blocked",
                                status_code=409,
                                details={"user_id": str(user_id), "table": model.__table__.name, "column": column.name},
                            )
        except VerificationCleanupError:
            raise
        except Exception:
            # A mapper inspection failure is safer as a closed operation than
            # as an unverified hard delete.
            raise VerificationCleanupError(
                "user dependency graph could not be verified",
                code="user_dependency_unknown",
                status_code=409,
                details={"user_id": str(user_id)},
            )

    async def _purge_user(
        self,
        session: AsyncSession,
        user_id: UUID,
        *,
        actor_user_id: UUID | None = None,
        allowed_project_ids: Iterable[str] = (),
    ) -> bool:
        await self._validate_user_purge(session, user_id, allowed_project_ids=allowed_project_ids)
        result = await UserRepository.soft_delete_user(
            session,
            user_id,
            deleted_by=actor_user_id or user_id,
            commit=False,
        )
        if result is None:
            return False
        return bool(await UserRepository.delete_user(session, user_id, commit=False, require_deleted=True))

    def _cleanup_project_filesystem(
        self,
        project_id: UUID,
        entry: ManifestEntry,
        workspace: Mapping[str, str] | None = None,
    ) -> dict[str, int]:
        """Remove only the exact workspace paths proven by the manifest.

        The canonical Project repository removes the normal ``_projects``
        workspace when it commits.  Legacy verification manifests may also
        carry a user-namespaced project directory/file; those paths are
        validated against the frozen owner/project IDs and removed only when
        empty or explicitly named by the manifest.
        """

        from ..services.app_storage import get_workspaces_root, remove_app_instance
        from ..services.project_workspace_cleanup import remove_project_workspace

        root = Path(get_workspaces_root()).resolve()
        counts: dict[str, int] = {}
        workspace_paths = dict(workspace or {})
        # Validate every operator-supplied path and directory boundary before
        # touching the canonical workspace.  A malformed legacy path must not
        # leave a committed DB deletion followed by a filesystem error.
        self._validate_project_filesystem_manifest(
            project_id,
            entry,
            workspace_paths,
            workspace_root=root,
        )
        if remove_project_workspace(project_id, workspace_root=root):
            counts["project_workspace"] = 1
        # ProjectRepository defers app-instance removal when commit=False;
        # perform that exact UUID-scoped cleanup after the enclosing DB commit.
        remove_app_instance(project_id, workspace_root=root)

        if not workspace_paths:
            return counts
        owner_id = _uuid_or_none(entry.owner_id)
        if owner_id is None:
            raise VerificationCleanupError(
                "workspace cleanup requires an exact project owner",
                code="workspace_owner_missing",
                status_code=409,
            )
        nested_root = (root / "_users" / f"user_{owner_id}" / "_projects" / f"project_{project_id}").resolve()
        canonical_root = (root / "_projects" / f"project_{project_id}").resolve()
        # Remove an explicitly named nested legacy file before attempting to
        # rmdir its project directory; manifests commonly carry both fields
        # and the directory is intentionally non-empty until the file is gone.
        workspace_items = sorted(
            workspace_paths.items(),
            key=lambda item: 0 if item[0] == "legacy_file" else 1,
        )
        for key, raw_path in workspace_items:
            candidate = _safe_manifest_workspace_path(raw_path, workspace_root=root)
            if key == "project_root":
                if candidate not in {nested_root, canonical_root}:
                    raise VerificationCleanupError(
                        "manifest project workspace does not match its owner/project",
                        code="workspace_path_mismatch",
                        status_code=409,
                    )
                # The canonical helper already removed canonical_root.  A
                # legacy nested root is removed only when empty so unlisted
                # files cannot be swept accidentally.
                if candidate == nested_root and candidate.exists():
                    try:
                        candidate.rmdir()
                        counts["legacy_project_workspace"] = counts.get("legacy_project_workspace", 0) + 1
                    except OSError as exc:
                        raise VerificationCleanupError(
                            "legacy project workspace is not empty",
                            code="workspace_not_empty",
                            status_code=409,
                        ) from exc
            elif key == "legacy_file":
                if candidate.parent not in {nested_root, canonical_root}:
                    raise VerificationCleanupError(
                        "manifest legacy file is outside its project workspace",
                        code="workspace_path_mismatch",
                        status_code=409,
                    )
                if candidate.exists() or candidate.is_symlink():
                    if not candidate.is_file() and not candidate.is_symlink():
                        raise VerificationCleanupError(
                            "manifest legacy workspace target is not a file",
                            code="workspace_target_invalid",
                            status_code=409,
                        )
                    candidate.unlink()
                    counts["legacy_workspace_file"] = counts.get("legacy_workspace_file", 0) + 1
                if candidate.parent == nested_root and nested_root.exists():
                    try:
                        nested_root.rmdir()
                        counts["legacy_project_workspace"] = counts.get("legacy_project_workspace", 0) + 1
                    except OSError:
                        # Other explicitly retained files keep the directory;
                        # the proven legacy file itself is already gone.
                        pass
            else:
                raise VerificationCleanupError(
                    "unknown manifest workspace field",
                    code="workspace_field_invalid",
                    status_code=422,
                )
        return counts

    def _validate_project_filesystem_manifest(
        self,
        project_id: UUID,
        entry: ManifestEntry,
        workspace_paths: Mapping[str, str],
        *,
        workspace_root: Path,
    ) -> None:
        """Perform filesystem validation without mutating anything."""

        if not workspace_paths:
            return
        owner_id = _uuid_or_none(entry.owner_id)
        if owner_id is None:
            raise VerificationCleanupError(
                "workspace cleanup requires an exact project owner",
                code="workspace_owner_missing",
                status_code=409,
            )
        nested_root = (workspace_root / "_users" / f"user_{owner_id}" / "_projects" / f"project_{project_id}").resolve()
        canonical_root = (workspace_root / "_projects" / f"project_{project_id}").resolve()
        for candidate_root in (nested_root, canonical_root):
            try:
                candidate_root.relative_to(workspace_root)
            except ValueError as exc:
                raise VerificationCleanupError(
                    "manifest project workspace escaped the configured root",
                    code="workspace_path_invalid",
                    status_code=409,
                ) from exc
        candidates: dict[str, Path] = {}
        for key, raw_path in workspace_paths.items():
            candidate = _safe_manifest_workspace_path(raw_path, workspace_root=workspace_root)
            candidates[key] = candidate
            if key == "project_root":
                if candidate not in {nested_root, canonical_root}:
                    raise VerificationCleanupError(
                        "manifest project workspace does not match its owner/project",
                        code="workspace_path_mismatch",
                        status_code=409,
                    )
            elif key == "legacy_file":
                if candidate.parent not in {nested_root, canonical_root}:
                    raise VerificationCleanupError(
                        "manifest legacy file is outside its project workspace",
                        code="workspace_path_mismatch",
                        status_code=409,
                    )
                if candidate.exists() or candidate.is_symlink():
                    if not candidate.is_file() and not candidate.is_symlink():
                        raise VerificationCleanupError(
                            "manifest legacy workspace target is not a file",
                            code="workspace_target_invalid",
                            status_code=409,
                        )
            else:
                raise VerificationCleanupError(
                    "unknown manifest workspace field",
                    code="workspace_field_invalid",
                    status_code=422,
                )

        # If a legacy nested project directory contains anything other than
        # the explicitly proven file, rmdir would be partial and unsafe.  Fail
        # before the database transaction rather than deleting the DB row and
        # leaving an orphaned workspace behind.
        legacy_file = candidates.get("legacy_file")
        if nested_root.exists() and nested_root.is_dir():
            allowed = {legacy_file.resolve()} if legacy_file is not None else set()
            try:
                unexpected = [
                    child.resolve()
                    for child in nested_root.iterdir()
                    if child.resolve() not in allowed
                ]
            except OSError as exc:
                raise VerificationCleanupError(
                    "legacy project workspace cannot be inspected",
                    code="workspace_unreadable",
                    status_code=409,
                ) from exc
            if unexpected:
                raise VerificationCleanupError(
                    "legacy project workspace is not empty",
                    code="workspace_not_empty",
                    status_code=409,
                )

    def _prevalidate_filesystem(self, manifest: CleanupManifest) -> None:
        """Validate all manifest paths before any database mutation.

        Filesystem cleanup is deliberately performed after the database commit,
        but a malformed or concurrently changed legacy workspace must still
        abort the operation *before* that commit.  This pass is read-only and
        bounded to the exact project IDs present in the frozen manifest.
        """

        if not manifest.workspace:
            return
        from ..services.app_storage import get_workspaces_root

        root = Path(get_workspaces_root()).resolve()
        for entry in manifest.entries:
            if entry.entity_type != "project":
                continue
            project_id = _uuid_or_none(entry.entity_id)
            if project_id is None:
                raise VerificationCleanupError(
                    "project workspace requires a UUID project identity",
                    code="workspace_project_invalid",
                    status_code=422,
                )
            self._validate_project_filesystem_manifest(
                project_id,
                entry,
                manifest.workspace,
                workspace_root=root,
            )

    async def execute(
        self,
        session: AsyncSession,
        *,
        run_id: UUID | str,
        manifest: CleanupManifest | Mapping[str, Any] | str | Path,
        actor_user_id: UUID | str,
        confirmation_digest: str | None = None,
        dry_run: bool = False,
        purge: bool = True,
    ) -> dict[str, Any]:
        frozen = self._manifest(manifest)
        if str(run_id) != frozen.run_id:
            raise VerificationCleanupError("run_id does not match manifest", code="run_id_mismatch", status_code=409)
        # Serialize cleanup attempts for a server-created UUID run.  The
        # cleanup ledger intentionally supports idempotent retries, but two
        # concurrent admins must not both pass preview and mint independent
        # running rows before mutating the same graph.
        run_uuid = _uuid_or_none(frozen.run_id)
        if run_uuid is not None:
            try:
                from ..memory.models.verification import VerificationRun

                await session.execute(
                    select(VerificationRun)
                    .where(VerificationRun.run_id == run_uuid)
                    .with_for_update()
                )
            except VerificationCleanupError:
                raise
            except Exception as exc:
                raise VerificationCleanupError(
                    "verification run could not be locked",
                    code="cleanup_concurrency_unknown",
                    status_code=409,
                    details={"run_id": frozen.run_id},
                ) from exc
        existing = await _maybe_await(self._existing_ledger(session, frozen))
        if existing is not None:
            existing_digest = existing.get("digest") or existing.get("manifest_digest")
            if existing_digest and existing_digest != frozen.digest:
                raise VerificationCleanupError("cleanup run is bound to a different manifest", code="cleanup_digest_mismatch", status_code=409)
            if existing.get("status") in {"completed", "idempotent", "purged", "succeeded", "already_clean"}:
                # A completed purge intentionally removes the target rows, so
                # running preview again would report ``target_missing``.  The
                # durable ledger is the idempotence proof; return its bounded
                # projection without re-selecting deleted entities.
                idempotent_digest = existing.get("preview_digest") or existing.get("digest") or frozen.digest
                return {
                    "run_id": frozen.run_id,
                    "source": frozen.source,
                    "manifest_digest": frozen.digest,
                    "digest": idempotent_digest,
                    "preview_digest": idempotent_digest,
                    "status": "idempotent",
                    "eligible": True,
                    "category_a": [entry.to_dict() for entry in frozen.entries],
                    "protected": [],
                    "counts": existing.get("counts", dict(frozen.counts)),
                    "graph": existing.get("graph", {}),
                    "deleted": existing.get("deleted", {}),
                    "failures": [],
                }
        preview = await self.preview(session, run_id=frozen.run_id, manifest=frozen, actor_user_id=actor_user_id)
        if dry_run:
            preview["status"] = "dry_run"
            return preview
        if not isinstance(confirmation_digest, str) or confirmation_digest != preview["digest"]:
            raise VerificationCleanupError(
                "confirmation digest does not match preview",
                code="confirmation_required",
                status_code=409,
                details={"expected_digest": preview["digest"]},
            )
        actor = _uuid_or_none(actor_user_id)
        if actor is None:
            raise VerificationCleanupError("actor_user_id must be a UUID", code="invalid_actor", status_code=422)
        deleted: dict[str, Any] = {"tasks": [], "projects": [], "users": [], "purged": {}}
        domain_committed = False
        try:
            # Re-read all identities immediately before mutation.  The second
            # provenance/graph check is the TOCTOU fence for the admin BFF.
            rows = await _maybe_await(self._load_target_rows(session, frozen))
            graph = await _maybe_await(self._snapshot(session, frozen, rows))
            # Validate user ownership/foreign-key blockers before any task or
            # project canonical method gets a chance to commit.  Projects in
            # this same frozen manifest are explicitly allowed because their
            # rows are removed first; every other owned/dependent row remains
            # a fail-closed blocker.
            selected_project_ids = tuple(graph.get("projects", {}).keys())
            for entry in frozen.entries:
                if entry.entity_type == "user":
                    await self._validate_user_purge(
                        session,
                        _uuid_or_none(entry.entity_id),  # type: ignore[arg-type]
                        allowed_project_ids=selected_project_ids,
                    )
            # Record the attempt only after both provenance and graph fences
            # have passed.  This avoids leaving a durable ``running`` row when
            # a stale manifest is rejected before any mutation begins.
            self._prevalidate_filesystem(frozen)
            await _maybe_await(self._record_ledger(session, frozen, status="running", actor_user_id=actor, details=preview))
            # Persist the bounded dry-run inventory and its digest before the
            # first canonical mutation.  A later failure can therefore be
            # audited/retried without relying on transient process logs.
            await session.commit()
            # Task tombstones must be created before ProjectRepository removes
            # Project memberships.  For a project target, every task in the
            # frozen graph is an exact canonical delete candidate; no title or
            # status inference is used.
            task_batches: dict[str, str] = {}
            for project_id, project_graph in graph["projects"].items():
                for task_id in project_graph.get("tasks", ()):
                    task_result = await self._canonical_delete_task(session, _uuid_or_none(task_id), actor)  # type: ignore[arg-type]
                    deleted["tasks"].append(task_result)
                    batch_id = task_result.get("deletion_batch_id") if isinstance(task_result, Mapping) else None
                    if batch_id:
                        task_batches[str(task_id)] = str(batch_id)

            # Standalone task selectors still use canonical soft-delete first;
            # they are physically purged below through the scoped task API.
            for entry in frozen.entries:
                if entry.entity_type != "task":
                    continue
                task_result = await self._canonical_delete_task(session, _uuid_or_none(entry.entity_id), actor)  # type: ignore[arg-type]
                deleted["tasks"].append(task_result)
                batch_id = task_result.get("deletion_batch_id") if isinstance(task_result, Mapping) else None
                result_task_ids = (
                    task_result.get("task_ids", ())
                    if isinstance(task_result, Mapping)
                    else ()
                )
                # ``task_ids`` is the canonical service's complete tree.  A
                # lightweight adapter may return only the selected identity;
                # the frozen task graph supplies the same exact fallback.
                if not result_task_ids:
                    result_task_ids = graph.get("task_graphs", {}).get(entry.entity_id, {}).get("tasks", (entry.entity_id,))
                for selected_task_id in result_task_ids:
                    if batch_id:
                        task_batches[str(selected_task_id)] = str(batch_id)

            # Remove only blocking LocalTask children, then invoke the
            # repository's canonical project lifecycle.
            for project_id, project_graph in graph["projects"].items():
                prep = await self._prepare_project_canonical_delete(session, project_graph)
                if prep:
                    deleted.setdefault("prepared", {})[project_id] = prep
                project_deleted = await self._canonical_delete_project(session, _uuid_or_none(project_id))  # type: ignore[arg-type]
                if not project_deleted:
                    raise VerificationCleanupError(
                        "canonical project deletion did not remove the target",
                        code="canonical_delete_failed",
                        status_code=409,
                        details={"project_id": project_id},
                    )
                deleted["projects"].append({"project_id": project_id, "deleted": True})
            if purge:
                for project_id, project_graph in graph["projects"].items():
                    project_batches = dict(project_graph.get("task_batches", {}))
                    project_batches.update({key: value for key, value in task_batches.items() if key in set(project_graph.get("tasks", ()))})
                    project_graph = {**project_graph, "task_batches": project_batches}
                    deleted["purged"][project_id] = await self._purge_project_graph(session, _uuid_or_none(project_id), project_graph)  # type: ignore[arg-type]
                project_task_ids = {
                    task_id
                    for item in graph["projects"].values()
                    for task_id in item.get("tasks", ())
                }
                standalone_ids: list[str] = []
                for entry in frozen.entries:
                    if entry.entity_type != "task":
                        continue
                    task_graph = graph.get("task_graphs", {}).get(entry.entity_id, {})
                    for selected_task_id in task_graph.get("tasks", (entry.entity_id,)):
                        if selected_task_id not in project_task_ids and selected_task_id not in standalone_ids:
                            standalone_ids.append(selected_task_id)
                if standalone_ids:
                    deleted["purged"]["tasks"] = await self._purge_task_graph(session, standalone_ids, expected_batches={key: task_batches[key] for key in standalone_ids if key in task_batches})
            for entry in frozen.entries:
                if entry.entity_type == "user":
                    deleted["users"].append({"user_id": entry.entity_id, "deleted": await self._purge_user(session, _uuid_or_none(entry.entity_id), actor_user_id=actor, allowed_project_ids=selected_project_ids)})  # type: ignore[arg-type]
            await session.commit()
            domain_committed = True
            # Filesystem cleanup is intentionally after the durable DB commit:
            # a failed transaction must not destroy a workspace still owned by
            # a live row.  Paths are restricted to the exact UUID-scoped
            # project roots and optional legacy manifest file above.
            filesystem: dict[str, Any] = {}
            if purge:
                try:
                    for project_entry in frozen.entries:
                        if project_entry.entity_type != "project":
                            continue
                        filesystem[project_entry.entity_id] = self._cleanup_project_filesystem(
                            _uuid_or_none(project_entry.entity_id),  # type: ignore[arg-type]
                            project_entry,
                            frozen.workspace,
                        )
                    if filesystem:
                        deleted["filesystem"] = filesystem
                except Exception as filesystem_error:
                    # The database purge is already durable at this point;
                    # never report it as a rollback-able failure.  Preserve a
                    # typed, retry-visible partial result and audit status.
                    deleted["filesystem_error"] = type(filesystem_error).__name__
                    result = {**preview, "status": "partial", "deleted": deleted, "failures": ["filesystem_cleanup"]}
                    try:
                        await _maybe_await(
                            self._record_ledger(
                                session,
                                frozen,
                                status="partial",
                                actor_user_id=actor,
                                details=result,
                            )
                        )
                        await session.commit()
                    except Exception:
                        logger.exception("Could not record partial filesystem cleanup for %s", frozen.run_id)
                    return result
            deleted["counts"] = preview["counts"]
            result = {**preview, "status": "completed", "deleted": deleted, "failures": []}
            try:
                await _maybe_await(self._record_ledger(session, frozen, status="completed", actor_user_id=actor, details=result))
                await session.commit()
            except Exception:
                # Domain deletion and filesystem cleanup are already durable;
                # a ledger outage must not turn a successful purge into a
                # misleading failure response.  The next operator read can
                # reconcile the append-only audit once storage is healthy.
                logger.exception("Could not finalize verification cleanup ledger for %s", frozen.run_id)
            return result
        except VerificationCleanupError:
            await session.rollback()
            # The running ledger is committed before domain mutation so an
            # interrupted operator request is observable.  Mark a typed
            # validation/TOCTOU failure terminal as well; otherwise retries
            # would inherit an indefinitely-running cleanup attempt.
            try:
                if domain_committed:
                    logger.error("Verification cleanup failed after domain commit for %s", frozen.run_id)
                    return {"run_id": frozen.run_id, "status": "partial", "deleted": deleted, "failures": ["post_commit"]}
                await _maybe_await(
                    self._record_ledger(
                        session,
                        frozen,
                        status="failed",
                        actor_user_id=actor,
                        details={"error_code": "verification_cleanup_error"},
                    )
                )
                await session.commit()
            except Exception:
                logger.exception("Could not record failed verification cleanup run %s", frozen.run_id)
            raise
        except Exception as exc:
            logger.exception("Verification cleanup failed for run %s", frozen.run_id)
            try:
                await session.rollback()
                await _maybe_await(self._record_ledger(session, frozen, status="failed", actor_user_id=actor, details={"error_code": type(exc).__name__}))
                await session.commit()
            except Exception:
                logger.exception("Could not record failed verification cleanup run %s", frozen.run_id)
            raise VerificationCleanupError(
                "verification cleanup failed",
                code="cleanup_failed",
                status_code=500,
            ) from exc

    cleanup = execute


async def preview_verification_cleanup(
    session: AsyncSession,
    *,
    run_id: UUID | str,
    manifest: CleanupManifest | Mapping[str, Any] | str | Path,
    actor_user_id: UUID | str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Module-level adapter used by routes and maintenance scripts."""

    return await VerificationCleanupCoordinator(**kwargs).preview(
        session,
        run_id=run_id,
        manifest=manifest,
        actor_user_id=actor_user_id,
    )


async def execute_verification_cleanup(
    session: AsyncSession,
    *,
    run_id: UUID | str,
    manifest: CleanupManifest | Mapping[str, Any] | str | Path,
    actor_user_id: UUID | str,
    confirmation_digest: str | None = None,
    dry_run: bool = False,
    purge: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    return await VerificationCleanupCoordinator(**kwargs).execute(
        session,
        run_id=run_id,
        manifest=manifest,
        actor_user_id=actor_user_id,
        confirmation_digest=confirmation_digest,
        dry_run=dry_run,
        purge=purge,
    )


async def list_eligible(
    session: AsyncSession,
    *,
    actor_user_id: UUID | str | None = None,
    limit: int = 100,
    **kwargs: Any,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Module-level eligible-run adapter used by the admin route."""

    return await VerificationCleanupCoordinator(**kwargs).list_eligible(
        session,
        actor_user_id=actor_user_id,
        limit=limit,
    )


__all__ = [
    "CleanupManifest",
    "ManifestEntry",
    "MAX_MANIFEST_BYTES",
    "VerificationCleanupCoordinator",
    "VerificationCleanupError",
    "VerificationCleanupService",
    "execute_verification_cleanup",
    "load_cleanup_manifest",
    "list_eligible",
    "preview_verification_cleanup",
]

# Compatibility spelling used by early route prototypes and maintenance
# scripts.  Keep one implementation so no caller can accidentally bypass the
# manifest/digest fences.
VerificationCleanupService = VerificationCleanupCoordinator
