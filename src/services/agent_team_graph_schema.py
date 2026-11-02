"""Graph-definition DTOs for an external multi-Agent Team GUI (schema v1).

Pure data-transfer objects: no execution, auth, provider, model or
credential concepts live here. Models stay in AgentRevision; a node only
references an agent (and optionally a revision) by id. Drafts may be
incomplete/disconnected -- readiness is a later runtime-compile concern,
surfaced here only as non-raising diagnostics.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

KeyStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]
OptionalKeyStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
] | None

Role = Literal["coordinator", "worker", "reviewer"]
EdgeKind = Literal["supervises", "assigns", "reviews", "handoff"]

DIAGNOSTIC_MISSING_ROOT = "missing_root"
DIAGNOSTIC_NO_TEAM_LEADER = "no_team_leader"
DIAGNOSTIC_SUPERVISION_CYCLE = "supervision_cycle"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Node(_Strict):
    key: KeyStr
    agent_id: UUID
    revision_id: UUID | None = None
    role: Role
    x: float = Field(default=0.0, allow_inf_nan=False)
    y: float = Field(default=0.0, allow_inf_nan=False)
    title: Annotated[str, StringConstraints(max_length=160)] | None = None


class Edge(_Strict):
    key: KeyStr
    source: KeyStr
    target: KeyStr
    kind: EdgeKind
    output_key: OptionalKeyStr = None


class Group(_Strict):
    key: KeyStr
    title: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)
    ]
    purpose: Annotated[str, StringConstraints(max_length=1000)] = ""
    node_keys: list[KeyStr] = Field(default_factory=list)
    leader_key: OptionalKeyStr = None
    execution_kind: Literal["media_image", "media_video", "software_code"] | None = None

    @field_validator("node_keys")
    @classmethod
    def _deduplicate_members(cls, members: list[str]) -> list[str]:
        return list(dict.fromkeys(members))


def _has_supervision_cycle(edges: list[Edge]) -> bool:
    """Cycle check over `supervises` edges only (handoff cycles excluded)."""
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        if edge.kind == "supervises":
            adjacency.setdefault(edge.source, []).append(edge.target)
    state: dict[str, int] = {}  # 0=unvisited, 1=in stack, 2=done

    def visit(node: str) -> bool:
        state[node] = 1
        for nxt in adjacency.get(node, []):
            if state.get(nxt, 0) == 1:
                return True
            if state.get(nxt, 0) == 0 and visit(nxt):
                return True
        state[node] = 2
        return False

    return any(state.get(n, 0) == 0 and visit(n) for n in list(adjacency))


class GraphDefinition(_Strict):
    schema_version: Literal[1] = 1
    root_key: OptionalKeyStr = None
    nodes: list[Node] = Field(default_factory=list, max_length=200)
    edges: list[Edge] = Field(default_factory=list, max_length=1000)
    groups: list[Group] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _validate_references(self) -> Self:
        node_keys: set[str] = set()
        for node in self.nodes:
            if node.key in node_keys:
                raise ValueError(f"duplicate node key: {node.key}")
            node_keys.add(node.key)

        edge_keys: set[str] = set()
        for edge in self.edges:
            if edge.key in edge_keys:
                raise ValueError(f"duplicate edge key: {edge.key}")
            edge_keys.add(edge.key)
            for endpoint in (edge.source, edge.target):
                if endpoint not in node_keys:
                    raise ValueError(
                        f"edge {edge.key} references unknown node: {endpoint}"
                    )

        group_keys: set[str] = set()
        for group in self.groups:
            if group.key in group_keys:
                raise ValueError(f"duplicate group key: {group.key}")
            group_keys.add(group.key)
            for member in group.node_keys:
                if member not in node_keys:
                    raise ValueError(
                        f"group {group.key} references unknown node: {member}"
                    )
            if group.leader_key is not None:
                if group.leader_key not in node_keys:
                    raise ValueError(
                        f"group {group.key} leader is not a node: {group.leader_key}"
                    )
                if group.leader_key not in group.node_keys:
                    raise ValueError(
                        f"group {group.key} leader is not a member: {group.leader_key}"
                    )

        if self.root_key is not None and self.root_key not in node_keys:
            raise ValueError(f"root_key is not a node: {self.root_key}")
        return self

    def diagnostics(self) -> list[str]:
        """Runtime-readiness codes for a draft; never raises, never a gate."""
        codes: list[str] = []
        if self.root_key is None:
            codes.append(DIAGNOSTIC_MISSING_ROOT)
        if not any(group.leader_key is not None for group in self.groups):
            codes.append(DIAGNOSTIC_NO_TEAM_LEADER)
        if _has_supervision_cycle(self.edges):
            codes.append(DIAGNOSTIC_SUPERVISION_CYCLE)
        return codes


def normalize_graph(value: GraphDefinition | dict[str, Any]) -> dict[str, Any]:
    """Validate `value` and return its JSON form, preserving given order."""
    definition = (
        value
        if isinstance(value, GraphDefinition)
        else GraphDefinition.model_validate(value)
    )
    result = definition.model_dump(mode="json")
    # Keep legacy graph snapshots and their hashes byte-for-byte compatible.
    for group in result["groups"]:
        if group["execution_kind"] is None:
            del group["execution_kind"]
    return result


def graph_diagnostics(value: GraphDefinition | dict[str, Any]) -> list[str]:
    definition = (
        value
        if isinstance(value, GraphDefinition)
        else GraphDefinition.model_validate(value)
    )
    return definition.diagnostics()


__all__ = [
    "Edge",
    "GraphDefinition",
    "Group",
    "Node",
    "graph_diagnostics",
    "normalize_graph",
]
