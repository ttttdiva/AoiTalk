"""Pure readiness projection for dependent groups (standard library only)."""
from __future__ import annotations

import heapq
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

STATES = frozenset({"unstarted", "running", "succeeded", "failed", "uncertain", "cancelled"})
TERMINAL_FAILURE = frozenset({"failed", "cancelled"})


@dataclass(frozen=True)
class GroupReadiness:
    ready: tuple[str, ...]
    running: tuple[str, ...]
    succeeded: tuple[str, ...]
    blocked: Mapping[str, tuple[str, ...]]
    uncertain: tuple[str, ...]
    complete: bool


def _topological(index: dict[str, int], preds: dict[str, list[str]], succ: dict[str, list[str]]) -> list[str]:
    keys = list(index)
    indegree = {key: len(preds[key]) for key in keys}
    heap = [index[key] for key in keys if indegree[key] == 0]
    heapq.heapify(heap)
    order: list[str] = []
    while heap:
        node = keys[heapq.heappop(heap)]
        order.append(node)
        for consumer in succ[node]:
            indegree[consumer] -= 1
            if indegree[consumer] == 0:
                heapq.heappush(heap, index[consumer])
    if len(order) != len(keys):
        stuck = tuple(key for key in keys if indegree[key] > 0)
        raise ValueError(f"dependency cycle among groups: {stuck!r}")
    return order


def evaluate_group_readiness(
    group_keys: Sequence[str],
    edges: Sequence[tuple[str, str]],
    states: Mapping[str, str],
) -> GroupReadiness:
    index: dict[str, int] = {}
    for key in group_keys:
        if not isinstance(key, str) or not key:
            raise ValueError(f"group key must be a non-empty string: {key!r}")
        if key in index:
            raise ValueError(f"duplicate group key: {key!r}")
        index[key] = len(index)
    if not isinstance(states, Mapping):
        raise ValueError("states must be a mapping")
    for key, state in states.items():
        if key not in index:
            raise ValueError(f"state given for unknown group: {key!r}")
        if not isinstance(state, str) or state not in STATES:
            raise ValueError(f"malformed state for {key!r}: {state!r}")

    rank = index.__getitem__
    preds: dict[str, list[str]] = {key: [] for key in index}
    succ: dict[str, list[str]] = {key: [] for key in index}
    seen: set[tuple[str, str]] = set()
    for edge in edges:
        if not isinstance(edge, tuple) or len(edge) != 2:
            raise ValueError(f"edge must be a (producer, consumer) tuple: {edge!r}")
        producer, consumer = edge
        for key in edge:
            if not isinstance(key, str) or key not in index:
                raise ValueError(f"edge {edge!r} references unknown group: {key!r}")
        if producer == consumer:
            raise ValueError(f"self-loop is not allowed: {producer!r}")
        if edge not in seen:
            seen.add(edge)
            preds[consumer].append(producer)
            succ[producer].append(consumer)
    for key in index:
        preds[key].sort(key=rank)
        succ[key].sort(key=rank)
    order = _topological(index, preds, succ)

    state = {key: states.get(key, "unstarted") for key in index}
    causes: dict[str, frozenset[str]] = {}
    held: dict[str, bool] = {}
    for key in order:  # predecessors are always resolved first
        if state[key] in TERMINAL_FAILURE:
            causes[key], held[key] = frozenset({key}), False
        elif state[key] == "uncertain":
            causes[key], held[key] = frozenset(), True
        elif state[key] == "unstarted":
            upstream = [p for p in preds[key] if state[p] in TERMINAL_FAILURE or state[p] == "unstarted"]
            causes[key] = frozenset().union(*(causes[p] for p in upstream))
            held[key] = any(state[p] == "uncertain" or (state[p] == "unstarted" and held[p]) for p in preds[key])
        else:  # running / succeeded stop propagation; source admission owns them
            causes[key], held[key] = frozenset(), False

    ready, running, succeeded, uncertain = [], [], [], []
    blocked: dict[str, tuple[str, ...]] = {}
    for key in index:
        current = state[key]
        if current == "running":
            running.append(key)
        elif current == "succeeded":
            succeeded.append(key)
        elif current == "uncertain":
            uncertain.append(key)
        elif causes[key]:
            blocked[key] = tuple(sorted(causes[key], key=rank))
        elif held[key]:
            uncertain.append(key)
        elif all(state[p] == "succeeded" for p in preds[key]):
            ready.append(key)
        # else: ordinary waiting on running/unstarted upstream, not a failure

    return GroupReadiness(
        ready=tuple(ready),
        running=tuple(running),
        succeeded=tuple(succeeded),
        blocked=MappingProxyType(blocked),
        uncertain=tuple(uncertain),
        complete=all(value == "succeeded" for value in state.values()),
    )

async def resolve_case_readiness(session, *, service, owner_user_id, root_session_id, case_key, graph) -> dict:
    """Project one authorized graph case from persisted dialogue and return proof."""
    import hashlib
    import json

    from sqlalchemy import select

    from ..memory.models import AgentDialogue, AgentDialogueDelivery
    from ..memory.models.conversations import ConversationMessage
    from .agent_delegation_dialogue_return import validate_delegation_return_message
    from .agent_delegation_service import DelegationError
    from .agent_team_graph_schema import GraphDefinition

    definition = GraphDefinition.model_validate(graph["definition"])
    keys = [group.key for group in definition.groups]
    memberships = {}
    for group in definition.groups:
        for node in group.node_keys:
            memberships.setdefault(node, []).append(group.key)
    states, refs, outputs, diagnostics = {}, {}, {}, []
    for node, groups in memberships.items():
        if len(groups) > 1:
            diagnostics.append({"code": "ambiguous_node_groups", "node_key": node, "groups": groups})
            states.update({key: "uncertain" for key in groups})
    edges = []
    for edge in definition.edges:
        if edge.kind != "handoff":
            continue
        source, target = memberships.get(edge.source, []), memberships.get(edge.target, [])
        if len(source) != 1 or len(target) != 1:
            affected = list(dict.fromkeys(source + target))
            diagnostics.append({"code": "ambiguous_handoff", "edge_key": edge.key, "groups": affected})
            states.update({key: "uncertain" for key in affected})
        elif source[0] != target[0]:
            edges.append((source[0], target[0]))

    rows = (await session.scalars(select(AgentDialogue).where(
        AgentDialogue.owner_user_id == owner_user_id,
        AgentDialogue.root_session_id == root_session_id,
    ))).all()
    matched = {}
    for dialogue in rows:
        auth = dialogue.authorization_json
        team = auth.get("team_graph") if isinstance(auth, dict) else None
        ref = team.get("ref") if isinstance(team, dict) else None
        if not isinstance(ref, dict) or (ref.get("graph_id"), ref.get("revision_id"),
                team.get("case_key")) != (str(graph["id"]), str(graph["revision_id"]), case_key):
            continue
        key = ref.get("group_key")
        if key not in keys:
            diagnostics.append({"code": "unknown_group", "dialogue_id": str(dialogue.id)})
            continue
        matched.setdefault(key, []).append(dialogue)
    for key, dialogues in matched.items():
        refs[key] = [str(row.id) for row in dialogues]
        if len(dialogues) != 1:
            states[key] = "uncertain"
            diagnostics.append({"code": "duplicate_dialogues", "group_key": key})
            continue
        dialogue = dialogues[0]
        digest = hashlib.sha256(json.dumps(dialogue.authorization_json, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if digest != dialogue.authorization_hash:
            states[key] = "uncertain"
            diagnostics.append({"code": "invalid_authorization_hash", "group_key": key})
            continue
        if dialogue.authorization_json["team_graph"].get("graph_hash") != graph["content_hash"]:
            states[key] = "uncertain"
            diagnostics.append({"code": "graph_hash_mismatch", "group_key": key})
            continue
        if states.get(key) == "uncertain":
            continue
        if dialogue.state in {"queued", "running", "waiting"}:
            states[key] = "running"
        elif dialogue.state in {"failed", "cancelled"}:
            states[key] = dialogue.state
        elif dialogue.state == "succeeded":
            deliveries = (await session.scalars(select(AgentDialogueDelivery).where(
                AgentDialogueDelivery.dialogue_id == dialogue.id,
                AgentDialogueDelivery.state == "acked",
                AgentDialogueDelivery.kind.in_(("response", "event")),
            ).order_by(AgentDialogueDelivery.sequence))).all()
            accepted, final, invalid = [], False, False
            for delivery in deliveries:
                message = await session.get(ConversationMessage, delivery.source_message_id) if delivery.source_message_id else None
                metadata = message.message_metadata if message and isinstance(message.message_metadata, dict) else {}
                if "agent_delegation_return" not in metadata:
                    continue
                try:
                    proof = await validate_delegation_return_message(session, service, dialogue, delivery, message)
                except (DelegationError, ValueError, TypeError, KeyError):
                    invalid = True
                    continue
                accepted.append(proof.as_dict())
                final |= delivery.kind == "response"
            if invalid or not final:
                states[key] = "uncertain"
                diagnostics.append({"code": "invalid_or_missing_return_proof", "group_key": key})
            else:
                states[key] = "succeeded"
                outputs[key] = accepted
        else:
            states[key] = "uncertain"
            diagnostics.append({"code": "unknown_dialogue_state", "group_key": key})
    projection = evaluate_group_readiness(keys, edges, states)
    return {"ready": list(projection.ready), "running": list(projection.running),
        "succeeded": list(projection.succeeded), "blocked": {k: list(v) for k, v in projection.blocked.items()},
        "uncertain": list(projection.uncertain), "complete": projection.complete,
        "group_dialogue_refs": refs, "accepted_outputs": outputs, "diagnostics": diagnostics}
