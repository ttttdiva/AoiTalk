import dagre from "@dagrejs/dagre";

/** Read-only map contract. IDs and relationships must come from recorded data. */
export type OperationsMapKind = "agent" | "work" | "action" | "space" | "project" | "run";

export type OperationsMapRelation =
  | "supervision"
  | "assignment"
  | "membership"
  | "parent"
  | "approval"
  | "execution"
  | "dependency";

export type OperationsMapNode = {
  /** Stable, kind-qualified graph ID; never a display name. */
  id: string;
  entityId: string;
  kind: OperationsMapKind;
  label: string;
  state: string;
  detail?: string;
  /** Explains missing/out-of-scope recorded relationships without placeholder nodes. */
  note?: string;
  metadata?: Array<{ label: string; value: string }>;
  href?: string;
  /** The recorded work ID to open in the existing execution viewer. */
  workId?: string;
  /** Canonical Work completion also accounts for unresolved attention. */
  finished?: boolean;
};

export type OperationsMapEdge = {
  id: string;
  source: string;
  target: string;
  relation: OperationsMapRelation;
  label?: string;
};

export const OPERATIONS_MAP_NODE_WIDTH = 244;
export const OPERATIONS_MAP_NODE_HEIGHT = 156;

export const operationsMapKinds: Record<OperationsMapKind, { label: string; shortLabel: string; color: string }> = {
  space: { label: "Space", shortLabel: "SPACE", color: "#94a3b8" },
  project: { label: "Project", shortLabel: "PROJECT", color: "#a1a1aa" },
  agent: { label: "Agent", shortLabel: "AGENT", color: "#6cb7eb" },
  work: { label: "仕事", shortLabel: "WORK", color: "#89cba7" },
  action: { label: "外部操作", shortLabel: "ACTION", color: "#d6a2df" },
  run: { label: "実行", shortLabel: "RUN", color: "#d5bb78" },
};

export const operationsMapRelations: Record<OperationsMapRelation, { label: string; description: string; color: string; dash?: string }> = {
  supervision: { label: "上司 → Agent", description: "取得済みのAgent IDで確認できる上司・部下関係", color: "#e2ba7c" },
  membership: { label: "所属・アクセス", description: "記録された所属やアクセス範囲", color: "#81919e" },
  assignment: { label: "担当", description: "仕事・操作に記録された担当", color: "#68a4db" },
  parent: { label: "仕事の委任", description: "Workに記録された親子・委任関係。上司・部下関係とは別です", color: "#b49cdc" },
  approval: { label: "提案・外部操作", description: "記録された提案元と外部操作の関連。承認済みを意味しません", color: "#cf9cdc", dash: "3 4" },
  execution: { label: "実行", description: "仕事と実行記録の関連", color: "#d4b563", dash: "8 4" },
  dependency: { label: "依存", description: "記録された依存関係。線だけでは待機中を示しません", color: "#6ac3b1", dash: "5 4" },
};

export function operationsMapState(state: string): { label: string; tone: "active" | "ready" | "attention" | "failed" | "neutral" } {
  switch (state.toLowerCase()) {
    case "working": case "running": case "in_progress": case "executing": case "claimed":
      return { label: "実行中", tone: "active" };
    case "ready": case "active": case "enabled": case "idle":
      return { label: state.toLowerCase() === "idle" ? "待機" : "有効", tone: "ready" };
    case "succeeded": case "completed": case "done":
      return { label: "完了", tone: "ready" };
    case "awaiting_approval": case "pending_approval": case "approval_required": case "proposed":
      return { label: state.toLowerCase() === "proposed" ? "提案" : "承認待ち", tone: "attention" };
    case "blocked": case "waiting": case "paused": case "stopped":
      return { label: state.toLowerCase() === "waiting" ? "待機" : "停止", tone: "attention" };
    case "uncertain":
      return { label: "結果不明", tone: "attention" };
    case "retry_wait":
      return { label: "再試行待ち", tone: "attention" };
    case "ready_for_review":
      return { label: "レビュー待ち", tone: "attention" };
    case "dead_letter":
      return { label: "要対応", tone: "failed" };
    case "verification_failed":
      return { label: "検証失敗", tone: "failed" };
    case "failed": case "error": case "stale": case "failed_stale":
      return { label: state.toLowerCase() === "stale" ? "更新停止" : "失敗", tone: "failed" };
    case "queued": case "pending": case "scheduled":
      return { label: state.toLowerCase() === "scheduled" ? "予定" : "未着手", tone: "neutral" };
    case "cancelled": case "canceled":
      return { label: "中止", tone: "neutral" };
    case "unknown": case "": case "—":
      return { label: "未記録", tone: "neutral" };
    default:
      return { label: state, tone: "neutral" };
  }
}

/** Remove dangling/duplicate edges rather than inventing missing entities. */
export function prepareOperationsMap(nodes: OperationsMapNode[], edges: OperationsMapEdge[]) {
  const byId = new Map<string, OperationsMapNode>();
  for (const node of nodes) if (node.id && !byId.has(node.id)) byId.set(node.id, node);
  const validEdges: OperationsMapEdge[] = [];
  const edgeIds = new Set<string>();
  for (const edge of edges) {
    if (!edge.id || edgeIds.has(edge.id) || !byId.has(edge.source) || !byId.has(edge.target)) continue;
    edgeIds.add(edge.id);
    validEdges.push(edge);
  }
  const incoming = new Map<string, OperationsMapEdge[]>();
  const outgoing = new Map<string, OperationsMapEdge[]>();
  const neighbors = new Map<string, Set<string>>();
  for (const id of byId.keys()) {
    incoming.set(id, []);
    outgoing.set(id, []);
    neighbors.set(id, new Set());
  }
  for (const edge of validEdges) {
    incoming.get(edge.target)!.push(edge);
    outgoing.get(edge.source)!.push(edge);
    if (edge.source !== edge.target) {
      neighbors.get(edge.source)!.add(edge.target);
      neighbors.get(edge.target)!.add(edge.source);
    }
  }
  return { nodes: [...byId.values()], edges: validEdges, byId, incoming, outgoing, neighbors };
}

export type OperationsMapModel = ReturnType<typeof prepareOperationsMap>;

/** Follow only typed, recorded hierarchy links. Access/assignment scope is not ownership. */
export function operationsMapChildren(model: OperationsMapModel, id: string) {
  const source = model.byId.get(id);
  if (!source) return [];
  const ids = new Set((model.outgoing.get(id) || []).filter(edge => {
    const target = model.byId.get(edge.target)!;
    if (source.kind === "space" || source.kind === "project") return edge.relation === "membership";
    if (source.kind === "agent") return (edge.relation === "supervision" && target.kind === "agent") || (edge.relation === "assignment" && target.kind === "work");
    if (source.kind === "work") return (edge.relation === "parent" && target.kind === "work") || (edge.relation === "execution" && target.kind === "run");
    return false;
  }).map(edge => edge.target).filter(target => target !== id));
  return model.nodes.filter(node => ids.has(node.id)).sort((a, b) => a.id.localeCompare(b.id));
}

/** Keep the recorded subtree and its outside neighbors; never synthesize missing children. */
export function operationsMapScope(model: OperationsMapModel, rootId: string | null) {
  const insideIds = new Set<string>();
  const outsideIds = new Set<string>();
  if (!rootId) return { model, insideIds, outsideIds };
  if (!model.byId.has(rootId)) return { model: prepareOperationsMap([], []), insideIds, outsideIds };
  const queue = [rootId];
  for (let index = 0; index < queue.length; index++) {
    const id = queue[index];
    if (insideIds.has(id)) continue;
    insideIds.add(id);
    for (const child of operationsMapChildren(model, id)) if (!insideIds.has(child.id)) queue.push(child.id);
  }
  for (const id of insideIds) for (const neighbor of model.neighbors.get(id) || []) if (!insideIds.has(neighbor)) outsideIds.add(neighbor);
  return { model: prepareOperationsMap(model.nodes.filter(node => insideIds.has(node.id) || outsideIds.has(node.id)), model.edges), insideIds, outsideIds };
}

export const OPERATIONS_MAP_INITIAL_WORK = 12;
export const OPERATIONS_MAP_MORE_WORK = 24;
export const OPERATIONS_MAP_UNASSIGNED = "unassigned";
export type OperationsMapWorkFilter = "all" | "open" | "completed";
export type OperationsMapPage = { fetched: number; available: number | null; hasMore: boolean };

export function operationsMapWorkMatches(node: OperationsMapNode, filter: OperationsMapWorkFilter) {
  const finished = node.finished ?? ["succeeded", "cancelled"].includes(node.state.toLowerCase());
  return filter === "all" || (filter === "completed" ? finished : !finished);
}

/** UI groups are not records or inferred assignment edges. */
export function operationsMapWorkGroups(model: OperationsMapModel) {
  const groups = new Map<string, OperationsMapNode[]>();
  const owned = new Set<string>();
  for (const agent of model.nodes.filter(node => node.kind === "agent")) {
    const works = new Map<string, OperationsMapNode>();
    for (const edge of model.outgoing.get(agent.id) || []) {
      const target = model.byId.get(edge.target);
      if (edge.relation === "assignment" && target?.kind === "work") {
        works.set(target.id, target);
        owned.add(target.id);
      }
    }
    if (works.size) groups.set(agent.id, [...works.values()].sort((a, b) => a.id.localeCompare(b.id)));
  }
  const unassigned = model.nodes.filter(node => node.kind === "work" && !owned.has(node.id)).sort((a, b) => a.id.localeCompare(b.id));
  if (unassigned.length) groups.set(OPERATIONS_MAP_UNASSIGNED, unassigned);
  return groups;
}

/** Keep previously fetched IDs in place when a page inserts earlier IDs. */
export function operationsMapWorkOrder(previous: ReadonlyMap<string, string[]>, groups: ReturnType<typeof operationsMapWorkGroups>) {
  return new Map([...groups].map(([id, works]) => {
    const valid = new Set(works.map(work => work.id));
    const kept = (previous.get(id) || []).filter(workId => valid.has(workId));
    const known = new Set(kept);
    return [id, [...kept, ...works.filter(work => !known.has(work.id)).map(work => work.id)]];
  }));
}

/** Retain the full evidence model; derive only its current canvas projection. */
export function projectOperationsMap(
  model: OperationsMapModel,
  options: {
    limits?: ReadonlyMap<string, number>;
    order?: ReadonlyMap<string, string[]>;
    revealIds?: ReadonlySet<string>;
    workFilter?: OperationsMapWorkFilter;
  } = {},
) {
  const filter = options.workFilter || "all";
  const revealed = options.revealIds || new Set<string>();
  const visibleWork = new Set<string>();
  const groups = [...operationsMapWorkGroups(model)].map(([id, works]) => {
    const matching = works.filter(work => operationsMapWorkMatches(work, filter));
    const matchingIds = new Set(matching.map(work => work.id));
    const order = options.order?.get(id) || works.map(work => work.id);
    for (const workId of order.filter(workId => matchingIds.has(workId)).slice(0, options.limits?.get(id) ?? OPERATIONS_MAP_INITIAL_WORK)) visibleWork.add(workId);
    for (const work of works) if (revealed.has(work.id)) visibleWork.add(work.id);
    return { id, works, matching, fetched: works.length, states: works.reduce<Record<string, number>>((counts, work) => {
      const state = work.state || "unknown";
      counts[state] = (counts[state] || 0) + 1;
      return counts;
    }, {}) };
  });
  const auxiliaryNodes = model.nodes.filter(node => node.kind === "project" || node.kind === "space").sort((a, b) => a.id.localeCompare(b.id));
  const nodes = model.nodes.filter(node => node.kind !== "project" && node.kind !== "space" && (node.kind !== "work" || visibleWork.has(node.id)));
  const ids = new Set(nodes.map(node => node.id));
  // Connected context is visible on the canvas. Unrelated fetched context remains searchable.
  let added = true;
  while (added) {
    added = false;
    for (const node of auxiliaryNodes) if (!ids.has(node.id) && (revealed.has(node.id) || model.edges.some(edge => (edge.source === node.id && ids.has(edge.target)) || (edge.target === node.id && ids.has(edge.source))))) {
      ids.add(node.id); nodes.push(node); added = true;
    }
  }
  const edges = model.edges.filter(edge => ids.has(edge.source) && ids.has(edge.target));
  return {
    nodes, edges, auxiliaryNodes,
    groups: groups.map(group => ({ ...group,
      displayed: group.works.filter(work => visibleWork.has(work.id)).length,
      hidden: group.matching.filter(work => !visibleWork.has(work.id)).length,
      filtered: group.fetched - group.matching.length,
    })),
    hiddenWork: model.nodes.filter(node => node.kind === "work" && operationsMapWorkMatches(node, filter) && !visibleWork.has(node.id)).length,
  };
}

/** Only the focused node and its direct recorded neighbors are highlighted. */
export function operationsMapNeighborhood(model: OperationsMapModel, id: string | null): Set<string> {
  return id && model.byId.has(id) ? new Set([id, ...(model.neighbors.get(id) || [])]) : new Set();
}

/** Links are local canonical views or explicit web URLs, never executable URLs. */
export function operationsMapSafeHref(href?: string): string | undefined {
  if (!href || /[\u0000-\u001f\u007f]/u.test(href)) return undefined;
  if (href.startsWith("/") && !href.startsWith("//") && !href.includes("\\")) return href;
  try {
    const url = new URL(href);
    return !url.username && !url.password && (url.protocol === "https:" || url.protocol === "http:") ? url.href : undefined;
  } catch {
    return undefined;
  }
}


/** Longest depth on the condensed relationship graph. Mutually reachable
 * records share a layer, so malformed cycles cannot increase ranks forever. */
function relationshipRanks(items: OperationsMapNode[], edges: OperationsMapEdge[]) {
  const ids = items.map(node => node.id);
  const adjacent = new Map(ids.map(id => [id, new Set<string>()]));
  for (const edge of edges) if (adjacent.has(edge.source) && adjacent.has(edge.target)) adjacent.get(edge.source)!.add(edge.target);
  const reachable = new Map<string, Set<string>>();
  for (const id of ids) {
    const visited = new Set<string>();
    const pending = [id];
    while (pending.length) {
      const next = pending.pop()!;
      if (visited.has(next)) continue;
      visited.add(next);
      pending.push(...adjacent.get(next)!);
    }
    reachable.set(id, visited);
  }
  const component = new Map<string, string>();
  for (const id of ids) {
    if (component.has(id)) continue;
    for (const other of reachable.get(id)!) if (reachable.get(other)!.has(id)) component.set(other, id);
  }
  const components = [...new Set(component.values())];
  const outgoing = new Map(components.map(id => [id, new Set<string>()]));
  const incomingCount = new Map(components.map(id => [id, 0]));
  const depths = new Map(components.map(id => [id, 0]));
  for (const [from, neighbors] of adjacent) {
    for (const to of neighbors) {
      const source = component.get(from)!;
      const target = component.get(to)!;
      if (source === target || outgoing.get(source)!.has(target)) continue;
      outgoing.get(source)!.add(target);
      incomingCount.set(target, incomingCount.get(target)! + 1);
    }
  }
  const ready = components.filter(id => incomingCount.get(id) === 0);
  while (ready.length) {
    const source = ready.shift()!;
    for (const target of outgoing.get(source)!) {
      depths.set(target, Math.max(depths.get(target)!, depths.get(source)! + 1));
      incomingCount.set(target, incomingCount.get(target)! - 1);
      if (!incomingCount.get(target)) ready.push(target);
    }
  }
  return new Map(ids.map(id => [id, depths.get(component.get(id)!)!]));
}

/** Layout only displayed records, including their recorded context.
 * Layout slots never create relationship records. */
export type OperationsMapLayout = {
  positions: Map<string, { x: number; y: number }>;
  bands: Array<{ id: string; label: string; x: number; y: number; width: number }>;
  actionBand?: { x: number; y: number; width: number };
  width: number;
  height: number;
  initialBounds: { x: number; y: number; width: number; height: number };
  columns: number;
};
export function layoutOperationsMap(model: OperationsMapModel, viewportWidth = 1000, evidenceModel = model, outsideIds: ReadonlySet<string> = new Set()): OperationsMapLayout {
  if (outsideIds.size) {
    const inside = prepareOperationsMap(model.nodes.filter(node => !outsideIds.has(node.id)), model.edges);
    const layout = layoutOperationsMap(inside, viewportWidth, evidenceModel);
    const outside = model.nodes.filter(node => outsideIds.has(node.id)).sort((a, b) => a.id.localeCompare(b.id));
    if (!outside.length) return layout;
    const y = layout.height + 48;
    outside.forEach((node, index) => layout.positions.set(node.id, { x: index % layout.columns * (OPERATIONS_MAP_NODE_WIDTH + 24), y: y + 30 + Math.floor(index / layout.columns) * (OPERATIONS_MAP_NODE_HEIGHT + 24) }));
    layout.bands.push({ id: "outside", label: "外側の関連記録", x: 0, y, width: Math.min(layout.columns, outside.length) * (OPERATIONS_MAP_NODE_WIDTH + 24) - 24 });
    return { ...layout, width: Math.max(layout.width, Math.min(layout.columns, outside.length) * (OPERATIONS_MAP_NODE_WIDTH + 24) - 24), height: Math.max(layout.height, y + 30 + Math.ceil(outside.length / layout.columns) * (OPERATIONS_MAP_NODE_HEIGHT + 24) - 24) };
  }
  const positions = new Map<string, { x: number; y: number }>();
  const gap = 24;
  const step = OPERATIONS_MAP_NODE_WIDTH + gap;
  const rowStep = OPERATIONS_MAP_NODE_HEIGHT + gap;
  const rankStep = OPERATIONS_MAP_NODE_HEIGHT + 48;
  const columns = Math.max(1, Math.min(6, Math.floor((Math.max(viewportWidth, OPERATIONS_MAP_NODE_WIDTH) - 24 + gap) / step)));
  const compare = (a: OperationsMapNode, b: OperationsMapNode) => a.id.localeCompare(b.id);
  const agents = model.nodes.filter(node => node.kind === "agent").sort(compare);
  const works = model.nodes.filter(node => node.kind === "work" || node.kind === "run").sort(compare);
  const actions = model.nodes.filter(node => node.kind === "action").sort(compare);
  const supervision = model.edges.filter(edge => edge.relation === "supervision" && model.byId.get(edge.source)?.kind === "agent" && model.byId.get(edge.target)?.kind === "agent");
  const owner = new Map<string, string>();
  for (const edge of [...model.edges].sort((a, b) => a.source.localeCompare(b.source))) {
    if (edge.relation === "assignment" && model.byId.get(edge.source)?.kind === "agent" && model.byId.get(edge.target)?.kind === "work" && !owner.has(edge.target)) owner.set(edge.target, edge.source);
  }
  // Recorded Work -> Run links provide layout context, not new assignments.
  for (let pass = 0; pass < works.length; pass++) {
    let changed = false;
    for (const edge of model.edges) if (edge.relation === "execution" && owner.has(edge.source) && !owner.has(edge.target) && model.byId.get(edge.target)?.kind === "run") {
      owner.set(edge.target, owner.get(edge.source)!); changed = true;
    }
    if (!changed) break;
  }
  const recordedOwners = evidenceModel.edges.filter(edge => edge.relation === "assignment" && evidenceModel.byId.get(edge.source)?.kind === "agent" && evidenceModel.byId.get(edge.target)?.kind === "work").map(edge => edge.source);
  const activeIds = new Set([...owner.values(), ...recordedOwners, ...supervision.flatMap(edge => [edge.source, edge.target])]);
  const active = agents.filter(agent => activeIds.has(agent.id));
  const secondary = agents.filter(agent => !activeIds.has(agent.id));
  const assigned = works.filter(work => owner.has(work.id));
  const detached = works.filter(work => !owner.has(work.id));
  const detachedSidebar = model.edges.some(edge => (edge.relation === "parent" || edge.relation === "execution") && works.some(work => work.id === edge.source) && works.some(work => work.id === edge.target) && owner.has(edge.source) !== owner.has(edge.target));
  const ranks = relationshipRanks(active, supervision);
  const workRanks = relationshipRanks(works, model.edges.filter(edge => edge.relation === "parent" || edge.relation === "execution"));
  const groupWidth = (id: string) => {
    const counts = new Map<number, number>();
    for (const work of assigned.filter(work => owner.get(work.id) === id)) counts.set(workRanks.get(work.id)!, (counts.get(workRanks.get(work.id)!) || 0) + 1);
    return Math.max(OPERATIONS_MAP_NODE_WIDTH, Math.min(columns, Math.max(0, ...counts.values())) * step - gap);
  };
  const slots = new Map<string, { x: number; width: number }>();
  if (active.length) {
    const graph = new dagre.graphlib.Graph({ multigraph: true });
    graph.setGraph({ rankdir: "TB", ranksep: 48, nodesep: gap });
    graph.setDefaultEdgeLabel(() => ({}));
    for (const agent of active) graph.setNode(agent.id, { width: groupWidth(agent.id), height: OPERATIONS_MAP_NODE_HEIGHT });
    for (const edge of supervision) if (edge.source !== edge.target) graph.setEdge(edge.source, edge.target, {}, edge.id);
    dagre.layout(graph);
    for (const agent of active) slots.set(agent.id, { x: graph.node(agent.id).x - groupWidth(agent.id) / 2, width: groupWidth(agent.id) });
    // Condensed-cycle ranks can differ from dagre's acyclic layout ranks.
    for (const rank of new Set(ranks.values())) {
      let right = 0;
      for (const agent of active.filter(agent => ranks.get(agent.id) === rank).sort((a, b) => slots.get(a.id)!.x - slots.get(b.id)!.x || compare(a, b))) {
        const slot = slots.get(agent.id)!; slot.x = Math.max(right, slot.x); right = slot.x + slot.width + gap;
      }
    }
    // Work-owning Agents on different supervisory levels still need distinct
    // Work slots below all Agent levels.
    let right = 0;
    for (const agent of active.filter(agent => assigned.some(work => owner.get(work.id) === agent.id)).sort((a, b) => slots.get(a.id)!.x - slots.get(b.id)!.x || compare(a, b))) {
      const slot = slots.get(agent.id)!; slot.x = Math.max(right, slot.x); right = slot.x + slot.width + gap;
    }
    // Give non-owning supervisory cards room after Work slots were separated.
    for (const rank of new Set(ranks.values())) {
      const occupied = active.filter(agent => ranks.get(agent.id) === rank && assigned.some(work => owner.get(work.id) === agent.id)).map(agent => {
        const slot = slots.get(agent.id)!; const x = slot.x + (slot.width - OPERATIONS_MAP_NODE_WIDTH) / 2;
        return { x, right: x + OPERATIONS_MAP_NODE_WIDTH };
      });
      for (const agent of active.filter(agent => ranks.get(agent.id) === rank && !assigned.some(work => owner.get(work.id) === agent.id)).sort(compare)) {
        const slot = slots.get(agent.id)!;
        let x = slot.x + (slot.width - OPERATIONS_MAP_NODE_WIDTH) / 2;
        for (;;) {
          const collision = occupied.find(other => x < other.right + gap && x + OPERATIONS_MAP_NODE_WIDTH + gap > other.x);
          if (!collision) break;
          x = collision.right + gap;
        }
        slot.x = x; slot.width = OPERATIONS_MAP_NODE_WIDTH;
        occupied.push({ x, right: x + OPERATIONS_MAP_NODE_WIDTH });
      }
    }
    for (const agent of active) {
      const slot = slots.get(agent.id)!;
      positions.set(agent.id, { x: slot.x + (slot.width - OPERATIONS_MAP_NODE_WIDTH) / 2, y: 30 + ranks.get(agent.id)! * rankStep });
    }
  }
  const workBase = active.length ? 30 + (Math.max(...ranks.values()) + 1) * rankStep : 30;
  const workY = new Map<number, number>();
  let nextY = workBase;
  for (const rank of [...new Set(workRanks.values())].sort((a, b) => a - b)) {
    workY.set(rank, nextY);
    const rows = Math.max(detachedSidebar ? Math.ceil(detached.filter(work => workRanks.get(work.id) === rank).length / columns) : 0, ...active.map(agent => Math.ceil(assigned.filter(work => owner.get(work.id) === agent.id && workRanks.get(work.id) === rank).length / columns)));
    if (rows) nextY += rows * rowStep + 24;
  }
  for (const agent of active) for (const rank of workY.keys()) {
    assigned.filter(work => owner.get(work.id) === agent.id && workRanks.get(work.id) === rank).forEach((work, index) => positions.set(work.id, { x: slots.get(agent.id)!.x + index % columns * step, y: workY.get(rank)! + Math.floor(index / columns) * rowStep }));
  }
  const coreWidth = Math.max(0, ...[...positions.values()].map(point => point.x + OPERATIONS_MAP_NODE_WIDTH));
  const coreBottom = Math.max(0, ...[...positions.values()].map(point => point.y + OPERATIONS_MAP_NODE_HEIGHT));
  const bands: Array<{ id: string; label: string; x: number; y: number; width: number }> = [];
  if (active.length) bands.push({ id: "hierarchy", label: "Agent → 担当する仕事", x: 0, y: 0, width: coreWidth });
  if (secondary.length) {
    const detachedWidth = Math.min(columns, detached.length) * step - gap;
    const widestPrimary = Math.max(detachedSidebar ? coreWidth + 48 + detachedWidth : coreWidth, detachedWidth, Math.min(columns, actions.length) * step - gap);
    const x = active.length ? widestPrimary + 48 : 0;
    secondary.forEach((agent, index) => positions.set(agent.id, { x: x + index % columns * step, y: 30 + Math.floor(index / columns) * rowStep }));
    bands.push({ id: "agents", label: "取得済みのAgent · この範囲に監督・担当関係なし", x, y: 0, width: Math.min(columns, secondary.length) * step - gap });
  }
  let primaryWidth = coreWidth || (secondary.length ? Math.min(columns, secondary.length) * step - gap : 0);
  let primaryBottom = coreBottom || (secondary.length ? 30 + Math.ceil(secondary.length / columns) * rowStep - gap : 0);
  if (detached.length) {
    const width = Math.min(columns, detached.length) * step - gap;
    primaryWidth = detachedSidebar ? coreWidth + 48 + width : Math.max(primaryWidth, width);
    const y = detachedSidebar ? workBase : primaryBottom ? primaryBottom + 48 : 30;
    const x = detachedSidebar ? coreWidth + 48 : (primaryWidth - width) / 2;
    // Parent/Run rank stays below its recorded predecessor, even without an Agent.
    let bottom = y;
    for (const rank of [...new Set(detached.map(work => workRanks.get(work.id)!))].sort((a, b) => a - b)) {
      const row = detached.filter(work => workRanks.get(work.id) === rank);
      row.forEach((work, index) => positions.set(work.id, { x: x + index % columns * step, y: (detachedSidebar ? workY.get(rank)! : bottom) + Math.floor(index / columns) * rowStep }));
      bottom += Math.ceil(row.length / columns) * rowStep + 24;
    }
    bands.push({ id: "unassigned", label: "担当Agentとの関係が未記録・取得範囲外", x, y: y - 30, width });
    primaryBottom = Math.max(primaryBottom, ...detached.map(work => positions.get(work.id)!.y + OPERATIONS_MAP_NODE_HEIGHT));
  }
  const actionWidth = Math.min(columns, actions.length) * step - gap;
  const actionBand = actions.length ? { x: primaryWidth > 0 ? (primaryWidth - actionWidth) / 2 : 0, y: primaryBottom ? primaryBottom + 48 : 0, width: actionWidth } : undefined;
  if (actionBand) {
    actions.forEach((action, index) => positions.set(action.id, { x: actionBand.x + index % columns * step, y: actionBand.y + 34 + Math.floor(index / columns) * rowStep }));
    bands.push({ id: "actions", label: `外部操作 · ${actions.length}件`, ...actionBand });
    if (!active.length) primaryWidth = Math.max(primaryWidth, actionWidth);
  }
  const context = model.nodes.filter(node => node.kind === "space" || node.kind === "project").sort(compare);
  if (context.length) {
    const y = Math.max(0, ...[...positions.values()].map(point => point.y + OPERATIONS_MAP_NODE_HEIGHT)) + 48;
    context.forEach((node, index) => positions.set(node.id, { x: index % columns * step, y: y + 30 + Math.floor(index / columns) * rowStep }));
    bands.push({ id: "context", label: "Space / Project · 所属とアクセス", x: 0, y, width: Math.min(columns, context.length) * step - gap });
  }
  const left = Math.min(0, ...[...positions.values()].map(point => point.x));
  const width = Math.max(0, ...[...positions.values()].map(point => point.x + OPERATIONS_MAP_NODE_WIDTH)) - left;
  const height = Math.max(0, ...[...positions.values()].map(point => point.y + OPERATIONS_MAP_NODE_HEIGHT));
  const initialBounds = active.length
    ? { x: 0, y: 30, width: coreWidth, height: coreBottom - 30 }
    : actionBand
      ? { x: actionBand.x, y: actionBand.y, width: actionBand.width, height: height - actionBand.y }
      : { x: left, y: 0, width: primaryWidth || width, height };
  return { positions, bands, actionBand, width, height, initialBounds, columns };
}


/** Start at readable card size. Wide scopes start at the first hierarchy lane;
 * additional records remain reachable by panning, search or explicit full-map fit. */
export function operationsMapInitialViewport(layout: ReturnType<typeof layoutOperationsMap>, width: number, height: number) {
  const bounds = layout.initialBounds;
  const inset = Math.min(24, Math.max(0, width / 2), Math.max(0, height / 2));
  const x = bounds.width > width - 2 * inset
    ? inset - bounds.x
    : width / 2 - bounds.x - bounds.width / 2;
  return { x, y: inset - bounds.y, zoom: 1 };
}

/** Preserve a visible record's screen position as retrieved records change
 * layout. An empty view is deliberate user navigation: never fit it here. */
export function operationsMapViewportAfterLayout(
  previous: ReadonlyMap<string, { x: number; y: number }>,
  next: ReadonlyMap<string, { x: number; y: number }>,
  viewport: { x: number; y: number; zoom: number },
  width: number,
  height: number,
  selectedId?: string | null,
) {
  if (width <= 0 || height <= 0 || !Number.isFinite(viewport.zoom) || viewport.zoom <= 0) return;
  const visible = (id: string) => {
    const point = previous.get(id);
    if (!point || !next.has(id)) return false;
    const x = viewport.x + point.x * viewport.zoom;
    const y = viewport.y + point.y * viewport.zoom;
    return x < width && y < height && x + OPERATIONS_MAP_NODE_WIDTH * viewport.zoom > 0 && y + OPERATIONS_MAP_NODE_HEIGHT * viewport.zoom > 0;
  };
  let anchorId = selectedId && visible(selectedId) ? selectedId : undefined;
  if (!anchorId) {
    let nearest = Infinity;
    for (const [id, point] of previous) {
      if (!visible(id)) continue;
      const x = viewport.x + (point.x + OPERATIONS_MAP_NODE_WIDTH / 2) * viewport.zoom - width / 2;
      const y = viewport.y + (point.y + OPERATIONS_MAP_NODE_HEIGHT / 2) * viewport.zoom - height / 2;
      const distance = x * x + y * y;
      if (distance < nearest || (distance === nearest && (!anchorId || id.localeCompare(anchorId) < 0))) {
        anchorId = id;
        nearest = distance;
      }
    }
  }
  if (!anchorId) return;
  const oldPoint = previous.get(anchorId)!;
  const newPoint = next.get(anchorId)!;
  if (oldPoint.x === newPoint.x && oldPoint.y === newPoint.y) return;
  return {
    anchorId,
    viewport: {
      x: viewport.x + (oldPoint.x - newPoint.x) * viewport.zoom,
      y: viewport.y + (oldPoint.y - newPoint.y) * viewport.zoom,
      zoom: viewport.zoom,
    },
  };
}
