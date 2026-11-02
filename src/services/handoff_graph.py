"""Pure handoff-graph analysis: deterministic topological order and cyclic SCCs."""
from __future__ import annotations

import heapq
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence


@dataclass(frozen=True)
class HandoffAnalysis:
    ordered_nodes: tuple[str, ...]
    predecessor_map: Mapping[str, tuple[str, ...]]
    cycles: tuple[tuple[str, ...], ...]


def _validate(node_keys: Sequence[str], edges: Sequence[tuple[str, str]]) -> tuple[dict[str, int], set[tuple[str, str]]]:
    index: dict[str, int] = {}
    for key in node_keys:
        if not isinstance(key, str) or not key:
            raise ValueError(f"node key must be a non-empty string: {key!r}")
        if key in index:
            raise ValueError(f"duplicate node key: {key!r}")
        index[key] = len(index)
    unique: set[tuple[str, str]] = set()
    for edge in edges:
        if not isinstance(edge, tuple) or len(edge) != 2:
            raise ValueError(f"edge must be a (producer, consumer) tuple: {edge!r}")
        producer, consumer = edge
        for key in (producer, consumer):
            if not isinstance(key, str) or key not in index:
                raise ValueError(f"edge {edge!r} references unknown node: {key!r}")
        if producer == consumer:
            raise ValueError(f"self-loop is not allowed: {producer!r}")
        unique.add((producer, consumer))
    return index, unique


def _cyclic_components(keys: list[str], succ: dict[str, list[str]]) -> list[tuple[str, ...]]:
    """Iterative Tarjan SCC; returns components with more than one member."""
    counter = 0
    low: dict[str, int] = {}
    order: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[tuple[str, ...]] = []
    for root in keys:
        if root in order:
            continue
        work = [(root, 0)]
        order[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, i = work[-1]
            if i < len(succ[node]):
                work[-1] = (node, i + 1)
                nxt = succ[node][i]
                if nxt not in order:
                    order[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, 0))
                elif nxt in on_stack:
                    low[node] = min(low[node], order[nxt])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == order[node]:
                members = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    members.append(member)
                    if member == node:
                        break
                if len(members) > 1:
                    found.append(tuple(members))
    return found


def analyze_handoffs(node_keys: Sequence[str], edges: Sequence[tuple[str, str]]) -> HandoffAnalysis:
    index, unique = _validate(node_keys, edges)
    keys = list(index)
    rank = index.__getitem__
    preds: dict[str, list[str]] = {key: [] for key in keys}
    succ: dict[str, list[str]] = {key: [] for key in keys}
    for producer, consumer in unique:
        preds[consumer].append(producer)
        succ[producer].append(consumer)
    for key in keys:
        preds[key].sort(key=rank)
        succ[key].sort(key=rank)

    indegree = {key: len(preds[key]) for key in keys}
    ready = [index[key] for key in keys if indegree[key] == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        node = keys[heapq.heappop(ready)]
        ordered.append(node)
        for consumer in succ[node]:
            indegree[consumer] -= 1
            if indegree[consumer] == 0:
                heapq.heappush(ready, index[consumer])

    cycles: tuple[tuple[str, ...], ...] = ()
    if len(ordered) != len(keys):
        components = [tuple(sorted(c, key=rank)) for c in _cyclic_components(keys, succ)]
        cycles = tuple(sorted(components, key=lambda c: index[c[0]]))

    return HandoffAnalysis(
        ordered_nodes=tuple(ordered),
        predecessor_map=MappingProxyType({key: tuple(preds[key]) for key in keys}),
        cycles=cycles,
    )
