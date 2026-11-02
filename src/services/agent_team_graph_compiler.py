"""Compile an already-authorized graph revision and admission-pinned Agents.

The caller must obtain the envelope through AgentTeamGraphService.get and resolve
all referenced revisions under the same owner/project authority before calling.
No mutable model/provider settings are copied out of AgentRevision.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping
from uuid import UUID

from .agent_team_graph_schema import GraphDefinition


class GraphCompileError(ValueError):
    """The selected draft cannot safely be used for planning."""


@dataclass(frozen=True)
class PinnedAgent:
    agent_id: UUID
    revision_id: UUID


@dataclass(frozen=True)
class CompiledNode:
    key: str
    role: str
    agent_id: UUID
    revision_id: UUID
    explicit_revision: bool


@dataclass(frozen=True)
class CompiledEdge:
    key: str
    kind: str
    source: str
    target: str
    output_key: str | None


@dataclass(frozen=True)
class CompiledGroup:
    graph_id: UUID
    graph_revision_id: UUID
    graph_hash: str
    group_key: str
    purpose: str
    root: CompiledNode
    leader: CompiledNode
    roster: tuple[CompiledNode, ...]
    allowed_pairs: tuple[tuple[str, str], ...]
    edges: tuple[CompiledEdge, ...]
    external_handoffs: tuple[CompiledEdge, ...] = ()
    execution_kind: Literal["software_code"] | None = None


@dataclass(frozen=True)
class CompiledMediaGroup:
    execution_kind: Literal["media_image", "media_video"]
    graph: CompiledGroup


def compile_group(envelope: Mapping, group_key: str, pins: Mapping[str, PinnedAgent]) -> CompiledGroup:
    """Compile a legacy general delegation group only."""
    return _compile_group(envelope, group_key, pins, kind=None)


def compile_media_group(envelope: Mapping, group_key: str, pins: Mapping[str, PinnedAgent]) -> CompiledMediaGroup:
    """Compile explicit media topology; authorization remains the caller's responsibility."""
    compiled = _compile_group(envelope, group_key, pins, kind="media")
    definition = GraphDefinition.model_validate(envelope["definition"])
    kind = next(g.execution_kind for g in definition.groups if g.key == group_key)
    assert kind is not None  # checked by _compile_group
    return CompiledMediaGroup(kind, compiled)


def compile_software_group(envelope: Mapping, group_key: str, pins: Mapping[str, PinnedAgent]) -> CompiledGroup:
    """Compile the explicitly declared software topology."""
    return _compile_group(envelope, group_key, pins, kind="software_code")


def _compile_group(envelope: Mapping, group_key: str, pins: Mapping[str, PinnedAgent], *, kind: str | None) -> CompiledGroup:
    """Compile a group from a verified envelope and pins keyed by graph node key.

    Pins for unversioned nodes must be resolved exactly once by the caller;
    explicit revisions are checked against the graph. Authorization is not
    performed here. Cross-group handoffs are retained for the outer dispatcher.
    """
    definition = GraphDefinition.model_validate(envelope["definition"])
    group = next((g for g in definition.groups if g.key == group_key), None)
    if group is None or not group.leader_key or not definition.root_key:
        raise GraphCompileError("group, leader or root missing")
    if not ((kind is None and group.execution_kind is None) or
            (kind == "media" and group.execution_kind in {"media_image", "media_video"}) or
            (kind == "software_code" and group.execution_kind == kind)):
        raise GraphCompileError("execution kind does not match compiler")
    nodes = {n.key: n for n in definition.nodes}
    root, leader = nodes[definition.root_key], nodes[group.leader_key]
    if root.role != "coordinator" or leader.role != "coordinator" or root.agent_id == leader.agent_id:
        raise GraphCompileError("root and leader must be distinct coordinators")
    relevant = set(group.node_keys) | {root.key}
    compiled = {}
    for key in relevant:
        node, pin = nodes[key], pins.get(key)
        if pin is None or pin.agent_id != node.agent_id or (node.revision_id and pin.revision_id != node.revision_id):
            raise GraphCompileError(f"invalid agent revision pin: {key}")
        compiled[key] = CompiledNode(key, node.role, pin.agent_id, pin.revision_id, node.revision_id is not None)
    edges = []
    external_handoffs = []
    assignments, reviews = set(), set()
    members = set(group.node_keys)
    for edge in definition.edges:
        if edge.source not in members and edge.target not in members:
            continue
        if edge.source not in relevant or edge.target not in relevant:
            if edge.kind == "handoff":
                external_handoffs.append(CompiledEdge(edge.key, edge.kind, edge.source, edge.target, edge.output_key))
                continue
            raise GraphCompileError(f"cross-group edge: {edge.key}")
        source, target = nodes[edge.source], nodes[edge.target]
        if edge.kind == "assigns":
            if source.key != leader.key or target.key not in members or target.role != "worker":
                raise GraphCompileError(f"invalid assignment: {edge.key}")
            assignments.add(target.key)
        elif edge.kind == "reviews":
            if source.key not in members or source.role != "reviewer" or target.key not in members or target.role != "worker" or source.agent_id == target.agent_id:
                raise GraphCompileError(f"invalid review: {edge.key}")
            reviews.add((target.key, source.key))
        elif edge.kind == "handoff":
            if source.key not in members or target.key not in members or source.role != "worker" or target.role != "worker" or source.agent_id == target.agent_id or not edge.output_key:
                raise GraphCompileError(f"invalid handoff: {edge.key}")
        elif edge.kind == "supervises":
            if source.role != "coordinator" or target.key not in members:
                raise GraphCompileError(f"invalid supervision: {edge.key}")
        edges.append(CompiledEdge(edge.key, edge.kind, edge.source, edge.target, edge.output_key))
    pairs = tuple(sorted(pair for pair in reviews if pair[0] in assignments))
    if not pairs or any(worker not in assignments for worker, _ in reviews):
        raise GraphCompileError("assignment/review edges missing")
    if kind is not None and (len(pairs) != 1 or len(assignments) != 1 or len(reviews) != 1):
        raise GraphCompileError("group requires exactly one worker and reviewer pair")
    return CompiledGroup(UUID(str(envelope["id"])), UUID(str(envelope["revision_id"])), envelope["content_hash"],
        group.key, group.purpose, compiled[root.key], compiled[leader.key],
        tuple(compiled[key] for key in group.node_keys), pairs, tuple(edges), tuple(external_handoffs),
        "software_code" if kind == "software_code" else None)
