"""Pure binder from caller-verified outputs to a target group's handoff inputs.

Records are assumed already validated against the database by the caller; this
helper only re-checks shape and pins, selects one record per incoming edge and
grants nothing. Standard library only, no I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Mapping, Sequence
from uuid import UUID

OUTPUT_KEY = re.compile(r"[a-z0-9][a-z0-9_.:-]*\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class HandoffBindingError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class Edge:
    key: str
    source_group: str
    source_node: str
    target_group: str
    output_key: str


@dataclass(frozen=True)
class VerifiedOutput:
    group_key: str
    node_key: str
    artifact_id: UUID
    sha256: str
    work_id: UUID
    run_id: UUID
    producer_agent_id: UUID
    producer_revision_id: UUID
    review_artifact_id: UUID
    reviewer_agent_id: UUID
    case_key: str
    graph_revision_id: UUID
    owner_id: UUID
    project_id: UUID
    provenance: Mapping | None = None


_UUID_FIELDS = tuple(f.name for f in fields(VerifiedOutput) if f.type in ("UUID", UUID))
_TEXT_FIELDS = ("group_key", "node_key", "case_key")


def _text(value: object, code: str, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise HandoffBindingError(code, f"{name} must be a non-empty string")
    return value


def _uuid(value: object, name: str) -> UUID:
    if not isinstance(value, UUID):
        raise HandoffBindingError("handoff_uuid_invalid", name)
    return value


def _check_output(output: object, position: int) -> VerifiedOutput:
    if not isinstance(output, VerifiedOutput):
        raise HandoffBindingError("handoff_output_invalid", f"outputs[{position}] is not a VerifiedOutput")
    for name in _TEXT_FIELDS:
        _text(getattr(output, name), "handoff_output_invalid", f"outputs[{position}].{name}")
    for name in _UUID_FIELDS:
        _uuid(getattr(output, name), f"outputs[{position}].{name}")
    if not isinstance(output.sha256, str) or not SHA256.match(output.sha256):
        raise HandoffBindingError("handoff_sha256_invalid", f"outputs[{position}].sha256")
    return output


def _incoming(edges: Sequence[Edge], target_group: str) -> list[Edge]:
    seen_keys: set[str] = set()
    aliases: set[str] = set()
    incoming: list[Edge] = []
    for position, edge in enumerate(edges):
        if not isinstance(edge, Edge):
            raise HandoffBindingError("handoff_edge_invalid", f"edges[{position}] is not an Edge")
        if edge.target_group != target_group:
            continue
        for name in ("key", "source_group", "source_node", "target_group"):
            _text(getattr(edge, name), "handoff_edge_invalid", f"edges[{position}].{name}")
        if len(edge.key) > 80 or not OUTPUT_KEY.fullmatch(edge.key):
            raise HandoffBindingError("handoff_edge_key_invalid", repr(edge.key))
        if edge.key in seen_keys:
            raise HandoffBindingError("handoff_edge_key_duplicate", edge.key)
        seen_keys.add(edge.key)
        key = edge.output_key
        if not isinstance(key, str) or not key.isascii() or len(key) > 80 or not OUTPUT_KEY.match(key):
            raise HandoffBindingError("handoff_output_key_invalid", f"{edge.key}: {key!r}")
        if key in aliases:
            raise HandoffBindingError("handoff_output_alias_duplicate", key)
        aliases.add(key)
        incoming.append(edge)
    return incoming


def bind_verified_handoffs(
    *,
    target_group: str,
    case_key: str,
    graph_revision_id: UUID,
    owner_id: UUID,
    project_id: UUID,
    edges: Sequence[Edge],
    outputs: Sequence[VerifiedOutput],
    selection: Mapping[str, UUID] | None = None,
) -> Mapping[str, VerifiedOutput]:
    _text(target_group, "handoff_target_invalid", "target_group")
    _text(case_key, "handoff_case_invalid", "case_key")
    pins = (_uuid(graph_revision_id, "graph_revision_id"), _uuid(owner_id, "owner_id"), _uuid(project_id, "project_id"))
    incoming = _incoming(edges, target_group)

    chosen: Mapping[str, UUID] = {} if selection is None else selection
    if not isinstance(chosen, Mapping):
        raise HandoffBindingError("handoff_selection_invalid", "selection must be a mapping")
    incoming_keys = {edge.key for edge in incoming}
    for edge_key, artifact_id in chosen.items():
        if edge_key not in incoming_keys:
            raise HandoffBindingError("handoff_selection_unknown_edge", repr(edge_key))
        _uuid(artifact_id, f"selection[{edge_key!r}]")

    bound: dict[str, VerifiedOutput] = {}
    for edge in incoming:
        candidates = [
            _check_output(output, position)
            for position, output in enumerate(outputs)
            if isinstance(output, VerifiedOutput)
            and output.case_key == case_key
            and (output.graph_revision_id, output.owner_id, output.project_id) == pins
            and output.group_key == edge.source_group
            and output.node_key == edge.source_node
        ]
        for output in candidates:
            if output.producer_agent_id == output.reviewer_agent_id:
                raise HandoffBindingError("handoff_self_review", f"{edge.key}: artifact {output.artifact_id}")
        if not candidates:
            raise HandoffBindingError("handoff_no_candidate", edge.key)
        if edge.key in chosen:
            matches = [output for output in candidates if output.artifact_id == chosen[edge.key]]
            if not matches:
                raise HandoffBindingError("handoff_selection_mismatch", f"{edge.key}: {chosen[edge.key]}")
            if len(matches) > 1:
                raise HandoffBindingError("handoff_selection_ambiguous", f"{edge.key}: {chosen[edge.key]}")
            bound[edge.output_key] = matches[0]
        elif len(candidates) == 1:
            bound[edge.output_key] = candidates[0]
        else:
            raise HandoffBindingError("handoff_selection_required", f"{edge.key}: {len(candidates)} candidates")
    return MappingProxyType(bound)
