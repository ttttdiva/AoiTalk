"""Closed Project Overview layout schema and deterministic Project Memory digest."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any


LAYOUT_SCHEMA_VERSION = 1
MAX_SECTIONS = 8
MAX_SECTION_MEMORY_IDS = 12
MAX_GRAPH_NODES = 24
MAX_GRAPH_EDGES = 48
MAX_GRAPH_IDENTIFIER_LENGTH = 120

SECTION_KINDS = frozenset({"highlight", "bullets", "cards", "timeline"})
SECTION_EMPHASIS = frozenset({"normal", "primary", "warning", "critical"})
SECTION_DENSITIES = frozenset({"compact", "normal"})
GRAPH_NODE_KINDS = frozenset(
    {"lead", "member", "stakeholder", "system", "other"}
)

# The generated payload is a declarative layout only. It must not smuggle
# executable/presentation-language content into the fixed frontend renderer.
_FORBIDDEN_TEXT_RE = re.compile(
    r"(?:"
    r"<\s*/?\s*[a-z][^>]*>|"
    r"\b(?:javascript|data)\s*:|"
    r"\b(?:mermaid|classname|style|css|html|script)\b|"
    r"(?:#[0-9a-f]{3,8}\b)|"
    r"(?:rgb|hsl)a?\s*\("
    r")",
    re.IGNORECASE,
)


class ProjectOverviewLayoutError(ValueError):
    """Generated layout violates the closed Project Overview schema."""


def _require_only_keys(
    value: Mapping[str, Any],
    *,
    allowed: frozenset[str],
    field: str,
) -> None:
    unknown = sorted(
        str(key)
        for key in value.keys()
        if key not in allowed
    )
    if unknown:
        raise ProjectOverviewLayoutError(
            f"{field} contains unknown field: {unknown[0]}"
        )


def empty_project_overview_layout() -> dict[str, Any]:
    return {
        "schema_version": LAYOUT_SCHEMA_VERSION,
        "sections": [],
        "graph": {
            "nodes": [],
            "edges": [],
        },
    }


def _plain_text(
    value: Any,
    *,
    field: str,
    max_length: int,
    required: bool = False,
) -> str:
    if value in (None, ""):
        if required:
            raise ProjectOverviewLayoutError(f"{field} is required")
        return ""
    if not isinstance(value, str):
        raise ProjectOverviewLayoutError(f"{field} must be a string")

    clean = " ".join(value.replace("\x00", "").split()).strip()
    if required and not clean:
        raise ProjectOverviewLayoutError(f"{field} is required")
    if len(clean) > max_length:
        raise ProjectOverviewLayoutError(
            f"{field} exceeds {max_length} characters"
        )
    if clean and _FORBIDDEN_TEXT_RE.search(clean):
        raise ProjectOverviewLayoutError(
            f"{field} contains forbidden presentation content"
        )
    return clean


def _graph_identifier(
    value: Any,
    *,
    field: str,
    required: bool = True,
) -> str:
    """Normalize an opaque stable graph identifier.

    Graph identifiers are deliberately not UUIDs. Values such as ``node-1``,
    ``team-lead`` or another stable producer-defined string are valid.
    """

    if value in (None, ""):
        if required:
            raise ProjectOverviewLayoutError(f"{field} is required")
        return ""
    if not isinstance(value, str):
        raise ProjectOverviewLayoutError(f"{field} must be a string")

    clean = value.replace("\x00", "").strip()
    if not clean:
        if required:
            raise ProjectOverviewLayoutError(f"{field} is required")
        return ""
    if len(clean) > MAX_GRAPH_IDENTIFIER_LENGTH:
        raise ProjectOverviewLayoutError(
            f"{field} exceeds {MAX_GRAPH_IDENTIFIER_LENGTH} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in clean):
        raise ProjectOverviewLayoutError(
            f"{field} contains control characters"
        )
    return clean


def _memory_uuid(value: Any, *, field: str) -> str:
    """Normalize a ContextMemory UUID.

    UUID validation intentionally applies only to Memory references, not graph
    node/edge identifiers.
    """

    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ProjectOverviewLayoutError(
            f"{field} must be a valid UUID"
        ) from exc


def _memory_value(
    memory: Mapping[str, Any] | Any,
    key: str,
) -> Any:
    if isinstance(memory, Mapping):
        return memory.get(key)
    return getattr(memory, key, None)


def _active_memory_map(
    memories: Iterable[Mapping[str, Any] | Any],
) -> dict[str, Mapping[str, Any] | Any]:
    result: dict[str, Mapping[str, Any] | Any] = {}
    for memory in memories:
        if str(_memory_value(memory, "status") or "") != "active":
            continue
        if str(_memory_value(memory, "scope_type") or "") != "project":
            continue
        memory_id = _memory_uuid(
            _memory_value(memory, "id"),
            field="memory.id",
        )
        result[memory_id] = memory
    return result


def _validate_memory_ids(
    raw_ids: Any,
    *,
    field: str,
    active_memories: Mapping[str, Any],
) -> list[str]:
    if raw_ids in (None, ""):
        return []
    if not isinstance(raw_ids, list):
        raise ProjectOverviewLayoutError(f"{field} must be an array")
    if len(raw_ids) > MAX_SECTION_MEMORY_IDS:
        raise ProjectOverviewLayoutError(
            f"{field} exceeds {MAX_SECTION_MEMORY_IDS} entries"
        )

    result: list[str] = []
    seen: set[str] = set()
    for index, raw_id in enumerate(raw_ids):
        memory_id = _memory_uuid(
            raw_id,
            field=f"{field}[{index}]",
        )
        if memory_id not in active_memories:
            raise ProjectOverviewLayoutError(
                f"{field}[{index}] references missing/inactive Project Memory"
            )
        if memory_id in seen:
            continue
        seen.add(memory_id)
        result.append(memory_id)
    return result


def validate_project_overview_layout(
    value: Any,
    *,
    active_memories: Iterable[Mapping[str, Any] | Any],
) -> dict[str, Any]:
    """Validate and normalize the closed Project Overview layout schema.

    Unknown object keys, unknown enums, invalid Memory UUIDs, missing/inactive
    Memory references, duplicate section/node IDs, duplicate explicit edge IDs,
    and missing edge endpoints are rejected.

    Node and edge IDs are opaque stable strings. Only ``memory_ids`` are UUIDs.
    Relationship edges require active referenced-memory evidence shared with
    both endpoints.
    """

    if not isinstance(value, Mapping):
        raise ProjectOverviewLayoutError("layout must be an object")

    _require_only_keys(
        value,
        allowed=frozenset({"schema_version", "sections", "graph"}),
        field="layout",
    )

    schema_version = value.get("schema_version")
    if schema_version != LAYOUT_SCHEMA_VERSION:
        raise ProjectOverviewLayoutError(
            f"schema_version must be {LAYOUT_SCHEMA_VERSION}"
        )

    active = _active_memory_map(active_memories)

    raw_sections = value.get("sections", [])
    if not isinstance(raw_sections, list):
        raise ProjectOverviewLayoutError("sections must be an array")
    if len(raw_sections) > MAX_SECTIONS:
        raise ProjectOverviewLayoutError(
            f"sections exceeds {MAX_SECTIONS} entries"
        )

    sections: list[dict[str, Any]] = []
    section_ids: set[str] = set()
    for index, raw in enumerate(raw_sections):
        if not isinstance(raw, Mapping):
            raise ProjectOverviewLayoutError(
                f"sections[{index}] must be an object"
            )

        _require_only_keys(
            raw,
            allowed=frozenset(
                {
                    "id",
                    "kind",
                    "title",
                    "emphasis",
                    "columns",
                    "density",
                    "memory_ids",
                }
            ),
            field=f"sections[{index}]",
        )

        section_id = _graph_identifier(
            raw.get("id"),
            field=f"sections[{index}].id",
            required=False,
        )
        if section_id:
            if section_id in section_ids:
                raise ProjectOverviewLayoutError(
                    f"duplicate section id: {section_id}"
                )
            section_ids.add(section_id)

        kind = str(raw.get("kind") or "").strip()
        emphasis = str(raw.get("emphasis") or "normal").strip()
        density = str(raw.get("density") or "normal").strip()
        columns = raw.get("columns", 1)

        if kind not in SECTION_KINDS:
            raise ProjectOverviewLayoutError(
                f"sections[{index}].kind is invalid"
            )
        if emphasis not in SECTION_EMPHASIS:
            raise ProjectOverviewLayoutError(
                f"sections[{index}].emphasis is invalid"
            )
        if density not in SECTION_DENSITIES:
            raise ProjectOverviewLayoutError(
                f"sections[{index}].density is invalid"
            )
        if isinstance(columns, bool) or columns not in (1, 2):
            raise ProjectOverviewLayoutError(
                f"sections[{index}].columns must be 1 or 2"
            )

        normalized_section: dict[str, Any] = {
                "kind": kind,
                "title": _plain_text(
                    raw.get("title"),
                    field=f"sections[{index}].title",
                    max_length=80,
                    required=True,
                ),
                "emphasis": emphasis,
                "columns": columns,
                "density": density,
                "memory_ids": _validate_memory_ids(
                    raw.get("memory_ids", []),
                    field=f"sections[{index}].memory_ids",
                    active_memories=active,
                ),
        }
        if section_id:
            normalized_section["id"] = section_id
        sections.append(normalized_section)

    raw_graph = value.get("graph", {})
    if raw_graph in (None, ""):
        raw_graph = {}
    if not isinstance(raw_graph, Mapping):
        raise ProjectOverviewLayoutError("graph must be an object")

    _require_only_keys(
        raw_graph,
        allowed=frozenset({"title", "nodes", "edges"}),
        field="graph",
    )
    graph_title = _plain_text(
        raw_graph.get("title"),
        field="graph.title",
        max_length=80,
    )

    raw_nodes = raw_graph.get("nodes", [])
    raw_edges = raw_graph.get("edges", [])
    if not isinstance(raw_nodes, list):
        raise ProjectOverviewLayoutError("graph.nodes must be an array")
    if not isinstance(raw_edges, list):
        raise ProjectOverviewLayoutError("graph.edges must be an array")
    if len(raw_nodes) > MAX_GRAPH_NODES:
        raise ProjectOverviewLayoutError(
            f"graph.nodes exceeds {MAX_GRAPH_NODES} entries"
        )
    if len(raw_edges) > MAX_GRAPH_EDGES:
        raise ProjectOverviewLayoutError(
            f"graph.edges exceeds {MAX_GRAPH_EDGES} entries"
        )

    nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    node_memory_ids: dict[str, set[str]] = {}

    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, Mapping):
            raise ProjectOverviewLayoutError(
                f"graph.nodes[{index}] must be an object"
            )

        _require_only_keys(
            raw,
            allowed=frozenset(
                {
                    "id",
                    "kind",
                    "label",
                    "subtitle",
                    "memory_ids",
                }
            ),
            field=f"graph.nodes[{index}]",
        )

        node_id = _graph_identifier(
            raw.get("id"),
            field=f"graph.nodes[{index}].id",
        )
        if node_id in node_ids:
            raise ProjectOverviewLayoutError(
                f"duplicate graph node id: {node_id}"
            )

        kind = str(raw.get("kind") or "").strip()
        if kind not in GRAPH_NODE_KINDS:
            raise ProjectOverviewLayoutError(
                f"graph.nodes[{index}].kind is invalid"
            )

        refs = _validate_memory_ids(
            raw.get("memory_ids", []),
            field=f"graph.nodes[{index}].memory_ids",
            active_memories=active,
        )
        if not refs:
            raise ProjectOverviewLayoutError(
                f"graph.nodes[{index}] requires active Project Memory evidence"
            )

        node_ids.add(node_id)
        node_memory_ids[node_id] = set(refs)
        nodes.append(
            {
                "id": node_id,
                "kind": kind,
                "label": _plain_text(
                    raw.get("label"),
                    field=f"graph.nodes[{index}].label",
                    max_length=80,
                    required=True,
                ),
                "subtitle": _plain_text(
                    raw.get("subtitle"),
                    field=f"graph.nodes[{index}].subtitle",
                    max_length=120,
                ),
                "memory_ids": refs,
            }
        )

    edges: list[dict[str, Any]] = []
    explicit_edge_ids: set[str] = set()

    for index, raw in enumerate(raw_edges):
        if not isinstance(raw, Mapping):
            raise ProjectOverviewLayoutError(
                f"graph.edges[{index}] must be an object"
            )

        _require_only_keys(
            raw,
            allowed=frozenset(
                {
                    "id",
                    "source",
                    "target",
                    "label",
                    "memory_ids",
                }
            ),
            field=f"graph.edges[{index}]",
        )

        source = _graph_identifier(
            raw.get("source"),
            field=f"graph.edges[{index}].source",
        )
        target = _graph_identifier(
            raw.get("target"),
            field=f"graph.edges[{index}].target",
        )
        if source not in node_ids or target not in node_ids:
            raise ProjectOverviewLayoutError(
                f"graph.edges[{index}] references a missing endpoint"
            )

        edge_id = _graph_identifier(
            raw.get("id"),
            field=f"graph.edges[{index}].id",
            required=False,
        )
        if edge_id:
            if edge_id in explicit_edge_ids:
                raise ProjectOverviewLayoutError(
                    f"duplicate graph edge id: {edge_id}"
                )
            explicit_edge_ids.add(edge_id)

        refs = _validate_memory_ids(
            raw.get("memory_ids", []),
            field=f"graph.edges[{index}].memory_ids",
            active_memories=active,
        )
        if not refs:
            raise ProjectOverviewLayoutError(
                f"graph.edges[{index}] requires active Project Memory evidence"
            )

        common_evidence = (
            set(refs)
            & node_memory_ids[source]
            & node_memory_ids[target]
        )
        if not common_evidence:
            raise ProjectOverviewLayoutError(
                f"graph.edges[{index}] lacks referenced-memory evidence "
                "shared by both endpoints"
            )

        normalized_edge: dict[str, Any] = {
            "source": source,
            "target": target,
            "label": _plain_text(
                raw.get("label"),
                field=f"graph.edges[{index}].label",
                max_length=80,
            ),
            "memory_ids": refs,
        }
        if edge_id:
            normalized_edge["id"] = edge_id
        edges.append(normalized_edge)

    normalized_graph: dict[str, Any] = {
        "nodes": nodes,
        "edges": edges,
    }
    if graph_title:
        normalized_graph["title"] = graph_title

    return {
        "schema_version": LAYOUT_SCHEMA_VERSION,
        "sections": sections,
        "graph": normalized_graph,
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def build_project_memory_snapshot(
    memories: Iterable[Mapping[str, Any] | Any],
) -> list[dict[str, Any]]:
    """Build deterministic metadata for active Project-scoped Memories.

    The snapshot never contains the raw Memory body. Content contributes only
    through SHA-256. The generator may separately receive current Memory data;
    this snapshot exists for race detection and source-digest comparison.
    """

    snapshot: list[dict[str, Any]] = []
    for memory in memories:
        if str(_memory_value(memory, "status") or "") != "active":
            continue
        if str(_memory_value(memory, "scope_type") or "") != "project":
            continue

        memory_id = _memory_uuid(
            _memory_value(memory, "id"),
            field="memory.id",
        )
        content = str(_memory_value(memory, "content") or "")

        snapshot.append(
            {
                "id": memory_id,
                "version": int(_memory_value(memory, "version") or 1),
                "updated_at": _iso(_memory_value(memory, "updated_at")),
                "title": str(_memory_value(memory, "title") or ""),
                "type": str(_memory_value(memory, "memory_type") or ""),
                "importance": int(
                    _memory_value(memory, "importance") or 0
                ),
                "confidence": float(
                    _memory_value(memory, "confidence") or 0.0
                ),
                "content_sha256": hashlib.sha256(
                    content.encode("utf-8")
                ).hexdigest(),
            }
        )

    snapshot.sort(
        key=lambda item: (
            item["id"],
            item["version"],
            item["updated_at"] or "",
            item["title"],
            item["type"],
            item["importance"],
            item["confidence"],
            item["content_sha256"],
        )
    )
    return snapshot


def build_project_memory_digest(
    memories: Iterable[Mapping[str, Any] | Any],
) -> str:
    """Return stable SHA-256 for active Project Memory overview inputs."""

    encoded = json.dumps(
        build_project_memory_snapshot(memories),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "GRAPH_NODE_KINDS",
    "LAYOUT_SCHEMA_VERSION",
    "MAX_GRAPH_EDGES",
    "MAX_GRAPH_IDENTIFIER_LENGTH",
    "MAX_GRAPH_NODES",
    "MAX_SECTION_MEMORY_IDS",
    "MAX_SECTIONS",
    "ProjectOverviewLayoutError",
    "SECTION_DENSITIES",
    "SECTION_EMPHASIS",
    "SECTION_KINDS",
    "build_project_memory_digest",
    "build_project_memory_snapshot",
    "empty_project_overview_layout",
    "validate_project_overview_layout",
]
