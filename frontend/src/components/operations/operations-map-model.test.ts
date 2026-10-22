import { describe, expect, it } from "vitest";
import {
  OPERATIONS_MAP_NODE_HEIGHT,
  OPERATIONS_MAP_NODE_WIDTH,
  layoutOperationsMap,
  operationsMapInitialViewport,
  operationsMapViewportAfterLayout,
  operationsMapNeighborhood,
  operationsMapChildren,
  operationsMapScope,
  operationsMapSafeHref,
  operationsMapState,
  prepareOperationsMap,
  projectOperationsMap,
  operationsMapWorkGroups,
  operationsMapWorkOrder,
  type OperationsMapEdge,
  type OperationsMapNode,
} from "./operations-map-model";

const node = (id: string, kind: OperationsMapNode["kind"] = "work", label = id): OperationsMapNode => ({
  id, entityId: id.split(":").slice(1).join(":"), kind, label, state: "queued",
});
const edge = (source: string, target: string, relation: OperationsMapEdge["relation"] = "assignment"): OperationsMapEdge => ({
  id: `${relation}:${source}>${target}`, source, target, relation,
});

describe("Operations map layout camera correction", () => {
  const positions = (entries: Array<[string, number, number]>) => new Map(entries.map(([id, x, y]) => [id, { x, y }]));
  const screen = (point: { x: number; y: number }, viewport: { x: number; y: number; zoom: number }) => ({ x: point.x * viewport.zoom + viewport.x, y: point.y * viewport.zoom + viewport.y });

  it("prefers the visible selected record and preserves screen coordinates and zoom across large shifts", () => {
    const previous = positions([["project:chosen", 10, 20], ["agent:center", 500, 200]]);
    const next = positions([["project:chosen", 45000, -12000], ["agent:center", -4000, 8000]]);
    const viewport = { x: 20, y: 30, zoom: 0.75 };
    const result = operationsMapViewportAfterLayout(previous, next, viewport, 1000, 600, "project:chosen")!;
    expect(result.anchorId).toBe("project:chosen");
    expect(result.viewport.zoom).toBe(viewport.zoom);
    expect(screen(next.get(result.anchorId)!, result.viewport)).toEqual(screen(previous.get(result.anchorId)!, viewport));
  });

  it("uses a visible existing record when selection is outside the user's latest pan", () => {
    const previous = positions([["work:selected", 0, 20], ["agent:visible", 5000, 100]]);
    const next = positions([["work:selected", 80000, 20], ["agent:visible", 21000, 300]]);
    const viewport = { x: -4900, y: 40, zoom: 1 };
    const result = operationsMapViewportAfterLayout(previous, next, viewport, 1000, 600, "work:selected")!;
    expect(result.anchorId).toBe("agent:visible");
    expect(screen(next.get(result.anchorId)!, result.viewport)).toEqual(screen(previous.get(result.anchorId)!, viewport));
    expect(result.viewport.zoom).toBe(1);
  });

  it("chooses the nearest visible common node deterministically and ignores new or removed records", () => {
    const previous = positions([["removed", 400, 200], ["b", 378, 250], ["a", 378, 250], ["edge", -240, 20]]);
    const next = positions([["new", 400, 200], ["a", 5000, 250], ["b", 8000, 250], ["edge", 2000, 20]]);
    expect(operationsMapViewportAfterLayout(previous, next, { x: 0, y: 0, zoom: 1 }, 1000, 600, "removed")!.anchorId).toBe("a");
  });

  it("does not move an empty viewport, a disjoint scope, or unchanged anchor coordinates", () => {
    const previous = positions([["a", 0, 0]]);
    const next = positions([["a", 50000, 20000]]);
    const emptyPan = { x: -5000, y: -5000, zoom: 1.5 };
    expect(operationsMapViewportAfterLayout(previous, next, emptyPan, 1000, 600, "a")).toBeUndefined();
    expect(operationsMapViewportAfterLayout(previous, positions([["other-scope", 0, 0]]), { x: 0, y: 0, zoom: 1 }, 1000, 600)).toBeUndefined();
    expect(operationsMapViewportAfterLayout(previous, previous, { x: 0, y: 0, zoom: 1 }, 1000, 600)).toBeUndefined();
  });

  it.each(["work:a", "agent:a"])("anchors %s when later projects, manager evidence and wrapped Work change layout", (selectedId) => {
    const originalNodes = [node("project:a", "project"), node("agent:a", "agent"), node("work:a")];
    const originalEdges = [edge("project:a", "agent:a", "membership"), edge("agent:a", "work:a")];
    const previous = layoutOperationsMap(prepareOperationsMap(originalNodes, originalEdges)).positions;
    const newProjects = Array.from({ length: 80 }, (_, index) => node(`project:0-${index}`, "project"));
    const newWorks = Array.from({ length: 150 }, (_, index) => node(`work:0-${index}`));
    const next = layoutOperationsMap(prepareOperationsMap([...originalNodes, ...newProjects, ...newWorks, node("agent:manager", "agent")], [...originalEdges, ...newWorks.map(work => edge("agent:a", work.id)), edge("agent:manager", "agent:a", "supervision")])).positions;
    expect(next.get(selectedId)!.y - previous.get(selectedId)!.y).toBeGreaterThan(100);
    expect(next.has("project:a")).toBe(true);
    const point = previous.get(selectedId)!;
    const viewport = { x: 100 - point.x * 0.9, y: 50 - point.y * 0.9, zoom: 0.9 };
    const result = operationsMapViewportAfterLayout(previous, next, viewport, 1366, 500, selectedId)!;
    expect(result.anchorId).toBe(selectedId);
    expect(screen(next.get(selectedId)!, result.viewport).x).toBeCloseTo(screen(point, viewport).x, 8);
    expect(screen(next.get(selectedId)!, result.viewport).y).toBeCloseTo(screen(point, viewport).y, 8);
    expect(result.viewport.zoom).toBe(0.9);
  });
});

describe("Operations map evidence model", () => {
  it("keeps unassigned work and proposed actions without fabricating a run, root, or edge", () => {
    const nodes = [node("agent:a", "agent"), node("work:a"), node("action:a", "action")];
    const model = prepareOperationsMap(nodes, []);
    expect(model.nodes).toEqual(nodes);
    expect(model.edges).toEqual([]);
    expect(model.nodes.some(item => item.kind === "run")).toBe(false);
    expect(model.neighbors.get("work:a")?.size).toBe(0);
  });

  it("uses typed IDs and never merges identical display names", () => {
    const model = prepareOperationsMap([node("agent:1", "agent", "same"), node("agent:2", "agent", "same"), node("work:1", "work", "same")], [edge("agent:2", "work:1")]);
    expect(model.byId.size).toBe(3);
    expect([...model.neighbors.get("agent:1")!]).toEqual([]);
    expect([...model.neighbors.get("agent:2")!]).toEqual(["work:1"]);
  });

  it("drops duplicate IDs and dangling endpoints instead of creating placeholders", () => {
    const recorded = edge("agent:1", "work:1");
    const model = prepareOperationsMap([node("agent:1", "agent"), node("agent:1", "agent"), node("work:1")], [recorded, recorded, edge("missing", "work:1"), edge("work:1", "missing")]);
    expect(model.nodes).toHaveLength(2);
    expect(model.edges).toEqual([recorded]);
  });

  it("preserves distinct relationship types between the same two records", () => {
    const model = prepareOperationsMap([node("agent:1", "agent"), node("work:1")], [edge("agent:1", "work:1"), edge("agent:1", "work:1", "parent")]);
    expect(model.edges).toHaveLength(2);
    expect(model.outgoing.get("agent:1")).toHaveLength(2);
    expect(model.incoming.get("work:1")).toHaveLength(2);
    expect(model.neighbors.get("agent:1")?.size).toBe(1);
  });

  it("highlights only one hop and returns no highlight for an invalid focus", () => {
    const model = prepareOperationsMap([node("work:a"), node("work:b"), node("work:c")], [edge("work:a", "work:b", "dependency"), edge("work:b", "work:c", "dependency")]);
    expect([...operationsMapNeighborhood(model, "work:a")]).toEqual(["work:a", "work:b"]);
    expect(operationsMapNeighborhood(model, "missing").size).toBe(0);
    expect(operationsMapNeighborhood(model, null).size).toBe(0);
  });

  it("does not count a recorded self edge as a second entity", () => {
    const model = prepareOperationsMap([node("work:a")], [edge("work:a", "work:a", "dependency")]);
    expect(model.edges).toHaveLength(1);
    expect(model.neighbors.get("work:a")?.size).toBe(0);
  });
});

describe("Operations map recorded drilldown", () => {
  const records = () => prepareOperationsMap([
    node("space:s", "space"), node("project:p", "project"), node("agent:a", "agent"),
    node("agent:reviewer", "agent"), node("work:parent"), node("work:child"),
    node("work:dependency"), node("run:r", "run"), node("action:approval", "action"),
    node("project:unrelated", "project"),
  ], [
    edge("space:s", "project:p", "membership"), edge("project:p", "agent:a", "membership"),
    edge("agent:a", "work:parent"), edge("agent:a", "action:approval", "approval"),
    edge("work:parent", "work:child", "parent"), edge("work:child", "work:parent", "parent"),
    edge("work:child", "run:r", "execution"), edge("work:parent", "work:dependency", "dependency"),
    edge("agent:reviewer", "work:child"),
  ]);
  it("follows only recorded hierarchy types, keeps cycles finite and separates outside neighbors", () => {
    const model = records();
    expect(operationsMapChildren(model, "agent:a").map(item => item.id)).toEqual(["work:parent"]);
    expect(operationsMapChildren(model, "work:child").map(item => item.id)).toEqual(["run:r", "work:parent"]);
    const scope = operationsMapScope(model, "project:p");
    expect([...scope.insideIds].sort()).toEqual(["agent:a", "project:p", "run:r", "work:child", "work:parent"]);
    expect([...scope.outsideIds].sort()).toEqual(["action:approval", "agent:reviewer", "space:s", "work:dependency"]);
    expect(scope.model.byId.has("project:unrelated")).toBe(false);
    expect(scope.model.edges.every(item => model.edges.includes(item))).toBe(true);
    const projection = projectOperationsMap(scope.model);
    const layout = layoutOperationsMap(prepareOperationsMap(projection.nodes, scope.model.edges), 390, model, scope.outsideIds);
    const insideBottom = Math.max(...[...scope.insideIds].map(id => layout.positions.get(id)!.y + OPERATIONS_MAP_NODE_HEIGHT));
    for (const id of scope.outsideIds) expect(layout.positions.get(id)!.y).toBeGreaterThan(insideBottom);
    const points = [...layout.positions.values()];
    for (let i = 0; i < points.length; i++) for (const b of points.slice(i + 1)) {
      const a = points[i];
      expect(a.x < b.x + OPERATIONS_MAP_NODE_WIDTH && a.x + OPERATIONS_MAP_NODE_WIDTH > b.x && a.y < b.y + OPERATIONS_MAP_NODE_HEIGHT && a.y + OPERATIONS_MAP_NODE_HEIGHT > b.y).toBe(false);
    }
  });
  it("renders connected context and default edges while unrelated context stays searchable", () => {
    const model = records();
    const projection = projectOperationsMap(model);
    expect(projection.nodes.some(item => item.id === "space:s")).toBe(true);
    expect(projection.nodes.some(item => item.id === "project:p")).toBe(true);
    expect(projection.nodes.some(item => item.id === "project:unrelated")).toBe(false);
    expect(projection.auxiliaryNodes.some(item => item.id === "project:unrelated")).toBe(true);
    expect(projection.edges).toEqual(model.edges);
    expect(projectOperationsMap(model, { revealIds: new Set(["project:unrelated"]) }).nodes.some(item => item.id === "project:unrelated")).toBe(true);
  });
  it("never falls back to the entire graph when a scoped record is unavailable", () => {
    const model = records();
    expect(operationsMapScope(model, null).model).toBe(model);
    expect(operationsMapScope(model, "project:missing").model.nodes).toEqual([]);
    expect(operationsMapChildren(model, "project:missing")).toEqual([]);
  });
});

describe("Operations map staged projection", () => {
  const stress = () => {
    const agents = [node("agent:late-manager", "agent"), node("agent:worker", "agent"), ...Array.from({ length: 138 }, (_, index) => node(`agent:agent-${String(index + 1).padStart(3, "0")}`, "agent"))];
    const works = Array.from({ length: 603 }, (_, index) => ({ ...node(`work:work-${String(index + 1).padStart(4, "0")}`), state: index < 346 ? "blocked" : index < 519 ? "dead_letter" : index < 523 ? "awaiting_approval" : "running" }));
    const context = [node("space:space", "space"), ...Array.from({ length: 130 }, (_, index) => node(`project:${index}`, "project"))];
    return prepareOperationsMap([...agents, ...works, ...context, node("action:a", "action")], [edge("agent:late-manager", "agent:worker", "supervision"), ...works.slice(0, 599).map(work => edge("agent:worker", work.id))]);
  };
  const overlaps = (positions: ReturnType<typeof layoutOperationsMap>["positions"]) => {
    const points = [...positions.values()];
    return points.flatMap((a, index) => points.slice(index + 1).filter(b => a.x < b.x + OPERATIONS_MAP_NODE_WIDTH && a.x + OPERATIONS_MAP_NODE_WIDTH > b.x && a.y < b.y + OPERATIONS_MAP_NODE_HEIGHT && a.y + OPERATIONS_MAP_NODE_HEIGHT > b.y));
  };

  it("keeps all 875 fetched records while rendering 12 assigned Work and four unassigned Work", () => {
    const model = stress();
    const projected = projectOperationsMap(model);
    expect(model.nodes).toHaveLength(875);
    expect(projected.nodes).toHaveLength(157);
    expect(projected.auxiliaryNodes).toHaveLength(131);
    expect(projected.hiddenWork).toBe(587);
    const worker = projected.groups.find(group => group.id === "agent:worker")!;
    expect(worker).toMatchObject({ fetched: 599, displayed: 12, hidden: 587, states: { blocked: 346, dead_letter: 173, awaiting_approval: 4, running: 76 } });
    expect(projected.edges).toHaveLength(13);
    expect(projected.edges).toContainEqual(edge("agent:late-manager", "agent:worker", "supervision"));
    expect(projected.edges.filter(item => item.relation === "assignment")).toHaveLength(12);
    expect(projected.edges.every(item => projected.nodes.some(node => node.id === item.target))).toBe(true);
    expect(model.outgoing.get("agent:worker")).toHaveLength(599);
    const expanded = projectOperationsMap(model, { limits: new Map([["agent:worker", 36]]) });
    expect(expanded.nodes).toHaveLength(181);
    expect(expanded.hiddenWork).toBe(563);
  });

  it.each([1301, 390])("wraps the staged 875-record graph at %spx without any card overlap or fake records", width => {
    const model = stress();
    const projection = projectOperationsMap(model);
    const layout = layoutOperationsMap(prepareOperationsMap(projection.nodes, model.edges), width, model);
    expect(layout.positions.size).toBe(157);
    expect(overlaps(layout.positions)).toEqual([]);
    expect(layout.columns).toBe(width === 390 ? 1 : 4);
    expect(layout.initialBounds.width).toBeLessThanOrEqual(width - 48);
    const viewport = operationsMapInitialViewport(layout, width, 600);
    expect(viewport.zoom).toBe(1);
    const supervisor = layout.positions.get("agent:late-manager")!;
    expect(supervisor.x + viewport.x).toBeGreaterThanOrEqual(0);
    expect(supervisor.x + viewport.x + OPERATIONS_MAP_NODE_WIDTH).toBeLessThanOrEqual(width);
    expect(supervisor.y + viewport.y).toBe(24);
    const points = [...layout.positions.values()];
    const bounds = { width: Math.max(...points.map(p => p.x + OPERATIONS_MAP_NODE_WIDTH)) - Math.min(...points.map(p => p.x)), height: Math.max(...points.map(p => p.y + OPERATIONS_MAP_NODE_HEIGHT)) - Math.min(...points.map(p => p.y)) };
    expect(bounds.width).toBe(layout.width);
    expect(bounds.height + Math.min(...points.map(point => point.y))).toBe(layout.height);
    expect(layout.positions.has("project:0")).toBe(false);
  });

  it("reveals fetched selected/search targets outside the limit and shows only their recorded edges", () => {
    const model = stress();
    const target = "work:work-0599";
    const projection = projectOperationsMap(model, { revealIds: new Set([target, "work:unfetched"]) });
    expect(projection.nodes).toHaveLength(158);
    expect(projection.nodes.some(node => node.id === target)).toBe(true);
    expect(projection.nodes.some(node => node.id === "work:unfetched")).toBe(false);
    expect(projection.edges).toHaveLength(14);
    expect(projection.edges).toContainEqual(edge("agent:worker", target));
    expect(projection.edges.every(item => model.edges.includes(item))).toBe(true);
    expect(projection.groups.find(group => group.id === "agent:worker")!.states.running).toBe(76);
  });

  it("keeps rendered IDs across reordered refresh and earlier arriving page IDs without expanding the page", () => {
    const original = stress();
    const order = operationsMapWorkOrder(new Map(), operationsMapWorkGroups(original));
    const before = projectOperationsMap(original, { order });
    const added = node("work:000-earlier");
    const next = prepareOperationsMap([added, ...[...original.nodes].reverse()], [edge("agent:worker", added.id), ...[...original.edges].reverse()]);
    const nextOrder = operationsMapWorkOrder(order, operationsMapWorkGroups(next));
    const after = projectOperationsMap(next, { order: nextOrder });
    expect(after.nodes.filter(node => node.kind === "work").map(node => node.id).sort()).toEqual(before.nodes.filter(node => node.kind === "work").map(node => node.id).sort());
    expect(after.groups.find(group => group.id === "agent:worker")).toMatchObject({ fetched: 600, displayed: 12, hidden: 588 });
  });

  it("filters only the projection, respects canonical unresolved completion, and reveals a completed deep link", () => {
    const items = [{ ...node("work:done"), state: "succeeded", finished: true }, { ...node("work:attention"), state: "succeeded", finished: false }, { ...node("work:unknown"), state: "custom" }];
    const model = prepareOperationsMap([node("agent:a", "agent"), ...items], items.map(item => edge("agent:a", item.id)));
    expect(projectOperationsMap(model, { workFilter: "open" }).nodes.filter(node => node.kind === "work").map(node => node.id)).toEqual(["work:attention", "work:unknown"]);
    const revealed = projectOperationsMap(model, { workFilter: "open", revealIds: new Set(["work:done"]) });
    expect(revealed.nodes).toHaveLength(4);
    expect(revealed.groups[0]).toMatchObject({ fetched: 3, displayed: 3, filtered: 1 });
    expect(model.edges).toHaveLength(3);
  });
});

describe("Operations map state labels", () => {
  it.each([
    ["claimed", "実行中", "active"], ["running", "実行中", "active"],
    ["uncertain", "結果不明", "attention"], ["retry_wait", "再試行待ち", "attention"],
    ["READY_FOR_REVIEW", "レビュー待ち", "attention"], ["awaiting_approval", "承認待ち", "attention"],
    ["dead_letter", "要対応", "failed"], ["verification_failed", "検証失敗", "failed"],
    ["stale", "更新停止", "failed"], ["completed", "完了", "ready"],
    ["proposed", "提案", "attention"], ["unknown", "未記録", "neutral"],
  ])("renders %s without inferring another state", (state, label, tone) => {
    expect(operationsMapState(state)).toEqual({ label, tone });
  });
  it("retains unfamiliar recorded states instead of treating them as successful", () => {
    expect(operationsMapState("custom-state")).toEqual({ label: "custom-state", tone: "neutral" });
  });
});

describe("Operations map canonical links", () => {
  it.each(["javascript:alert(1)", "data:text/html,test", "//example.org", "/\\example.org", "/\n/example.org", "https://user:secret@example.org", "not a URL", ""])("rejects unsafe link %s", href => {
    expect(operationsMapSafeHref(href)).toBeUndefined();
  });
  it("retains local canonical queries and ordinary web links", () => {
    expect(operationsMapSafeHref("/operations?tab=work&scope=space&space_id=1")).toBe("/operations?tab=work&scope=space&space_id=1");
    expect(operationsMapSafeHref("https://example.org/work/1")).toBe("https://example.org/work/1");
  });
});

describe("Operations map recorded hierarchy layout", () => {
  it.each([390, 1280, 1440])("starts an oversized %spx scope with readable first-lane Work instead of shrinking all labels", width => {
    const agents = [node("agent:a", "agent"), node("agent:b", "agent")];
    const works = Array.from({ length: 48 }, (_, index) => node(`work:${String(index).padStart(2, "0")}`));
    const model = prepareOperationsMap([...agents, ...works], works.map((work, index) => edge(agents[index < 24 ? 0 : 1].id, work.id)));
    const layout = layoutOperationsMap(model, width);
    const viewport = operationsMapInitialViewport(layout, width, 600);
    expect(layout.initialBounds.width).toBeGreaterThan(width - 48);
    expect(viewport.zoom).toBe(1);
    const first = layout.positions.get("work:00")!;
    expect(first.x + viewport.x).toBe(24);
    expect(first.x + viewport.x + OPERATIONS_MAP_NODE_WIDTH).toBeLessThan(width);
    expect(first.y + viewport.y + OPERATIONS_MAP_NODE_HEIGHT).toBeLessThan(600);
    expect(model.nodes).toHaveLength(50);
    expect(model.edges).toHaveLength(48);
  });
  it("places supervisors above workers and every assigned Work beneath its worker", () => {
    const nodes = [node("agent:supervisor", "agent"), node("agent:worker-a", "agent"), node("agent:worker-b", "agent"), node("work:a"), node("work:b"), node("work:c")];
    const edges = [edge("agent:supervisor", "agent:worker-a", "supervision"), edge("agent:supervisor", "agent:worker-b", "supervision"), edge("agent:worker-a", "work:a"), edge("agent:worker-a", "work:b"), edge("agent:worker-b", "work:c")];
    const model = prepareOperationsMap(nodes, edges);
    const layout = layoutOperationsMap(model);
    const point = (id: string) => layout.positions.get(id)!;
    expect(point("agent:worker-a").y).toBeGreaterThan(point("agent:supervisor").y + OPERATIONS_MAP_NODE_HEIGHT);
    expect(point("agent:worker-b").y).toBe(point("agent:worker-a").y);
    expect(point("work:a").y).toBeGreaterThan(point("agent:worker-a").y + OPERATIONS_MAP_NODE_HEIGHT);
    expect(point("work:b").y).toBe(point("work:a").y);
    expect(Math.abs(point("work:c").x - point("agent:worker-b").x)).toBeLessThan(OPERATIONS_MAP_NODE_WIDTH);
    expect((point("work:a").x + point("work:b").x) / 2).toBe(point("agent:worker-a").x);
    expect(model.edges).toEqual(edges);
  });

  it("keeps direct sibling Agents on one layer despite cross-worker Work delegation", () => {
    const nodes = [node("agent:supervisor", "agent"), node("agent:developer", "agent"), node("agent:reviewer", "agent"), node("work:develop"), node("work:review")];
    const edges = [edge("agent:supervisor", "agent:developer", "supervision"), edge("agent:supervisor", "agent:reviewer", "supervision"), edge("agent:developer", "work:develop"), edge("agent:reviewer", "work:review"), edge("work:develop", "work:review", "parent")];
    const model = prepareOperationsMap(nodes, edges);
    const layout = layoutOperationsMap(model);
    expect(layout.positions.get("agent:developer")!.y).toBe(layout.positions.get("agent:reviewer")!.y);
    expect(layout.positions.get("work:develop")!.y).toBeGreaterThan(layout.positions.get("agent:reviewer")!.y + OPERATIONS_MAP_NODE_HEIGHT);
    expect(layout.positions.get("work:review")!.y).toBeGreaterThan(layout.positions.get("work:develop")!.y + OPERATIONS_MAP_NODE_HEIGHT);
    expect(model.edges).toEqual(edges);
  });

  it("keeps an unassigned parent Work above its assigned child without inventing an owner", () => {
    const nodes = [node("agent:a", "agent"), node("agent:unrelated", "agent"), node("work:parent"), node("work:child"), node("work:sibling")];
    const edges = [edge("agent:a", "work:child"), edge("agent:a", "work:sibling"), edge("work:parent", "work:child", "parent")];
    const model = prepareOperationsMap(nodes, edges);
    const layout = layoutOperationsMap(model);
    expect(layout.positions.get("work:parent")!.y + OPERATIONS_MAP_NODE_HEIGHT).toBeLessThan(layout.positions.get("work:child")!.y);
    const positions = [...layout.positions.values()];
    for (let index = 0; index < positions.length; index++) for (const other of positions.slice(index + 1)) {
      const point = positions[index];
      expect(point.x < other.x + OPERATIONS_MAP_NODE_WIDTH && point.x + OPERATIONS_MAP_NODE_WIDTH > other.x && point.y < other.y + OPERATIONS_MAP_NODE_HEIGHT && point.y + OPERATIONS_MAP_NODE_HEIGHT > other.y).toBe(false);
    }
    expect(model.edges).toEqual(edges);
    expect(model.incoming.get("work:parent")).toEqual([]);
  });

  it("keeps unassigned Work, context and external operations in explicit visual lanes without new edges", () => {
    const nodes = [node("space:a", "space"), node("project:a", "project"), node("agent:a", "agent"), node("work:owned"), node("work:unassigned"), ...Array.from({ length: 8 }, (_, i) => node(`action:${i}`, "action"))];
    const edges = [edge("space:a", "project:a", "membership"), edge("project:a", "agent:a", "membership"), edge("agent:a", "work:owned"), edge("agent:a", "action:0", "approval")];
    const model = prepareOperationsMap(nodes, edges);
    const layout = layoutOperationsMap(model, 1366);
    expect(layout.positions.size).toBe(nodes.length);
    expect(layout.bands.map(band => band.id)).toEqual(["hierarchy", "unassigned", "actions", "context"]);
    expect(projectOperationsMap(model).auxiliaryNodes.map(node => node.id)).toEqual(["project:a", "space:a"]);
    expect(layout.positions.get("work:unassigned")!.y).toBeGreaterThan(layout.positions.get("work:owned")!.y + OPERATIONS_MAP_NODE_HEIGHT);
    const primaryBottom = Math.max(...nodes.filter(item => item.kind === "agent" || item.kind === "work").map(item => layout.positions.get(item.id)!.y + OPERATIONS_MAP_NODE_HEIGHT));
    expect(layout.actionBand!.y).toBeGreaterThan(primaryBottom);
    expect(new Set([...layout.positions.values()].map(point => `${point.x}:${point.y}`)).size).toBe(nodes.length);
    expect(model.edges).toEqual(edges);
  });

  it("is stable when refresh reorders records or changes only status and labels", () => {
    const nodes = [node("agent:a", "agent"), node("work:a"), node("work:b"), node("action:a", "action")];
    const edges = [edge("agent:a", "work:a"), edge("agent:a", "work:b"), edge("work:a", "action:a", "approval")];
    const first = layoutOperationsMap(prepareOperationsMap(nodes, edges));
    const refreshed = layoutOperationsMap(prepareOperationsMap([...nodes].reverse().map(item => ({ ...item, label: "updated", state: "running" })), [...edges].reverse()));
    expect(refreshed.positions).toEqual(first.positions);
  });

  it("keeps the supervisor fully visible in the initial viewport despite distant context and action records", () => {
    const model = prepareOperationsMap([node("space:a", "space"), node("agent:a", "agent"), node("agent:b", "agent"), node("work:a"), ...Array.from({ length: 12 }, (_, i) => node(`action:${i}`, "action"))], [edge("agent:a", "agent:b", "supervision"), edge("agent:b", "work:a")]);
    const layout = layoutOperationsMap(model, 1366);
    const viewport = operationsMapInitialViewport(layout, 1366, 500);
    const supervisor = layout.positions.get("agent:a")!;
    expect(viewport.zoom).toBeGreaterThanOrEqual(0.7);
    expect(supervisor.y * viewport.zoom + viewport.y).toBe(24);
    expect(supervisor.x * viewport.zoom + viewport.x).toBeGreaterThanOrEqual(0);
    expect((supervisor.x + OPERATIONS_MAP_NODE_WIDTH) * viewport.zoom + viewport.x).toBeLessThan(1366);
  });

  it("wraps external operations at narrow widths while retaining readable card dimensions", () => {
    const model = prepareOperationsMap(Array.from({ length: 8 }, (_, i) => node(`action:${i}`, "action")), []);
    const narrow = layoutOperationsMap(model, 390);
    const wide = layoutOperationsMap(model, 1440);
    expect(narrow.columns).toBe(1);
    expect(narrow.width).toBe(OPERATIONS_MAP_NODE_WIDTH);
    expect(narrow.height).toBeGreaterThan(wide.height);
    expect(model.edges).toEqual([]);
    expect(OPERATIONS_MAP_NODE_WIDTH).toBeGreaterThanOrEqual(240);
  });

  it("centers sparse external actions in the mobile initial viewport without inventing ownership", () => {
    const model = prepareOperationsMap([...Array.from({ length: 4 }, (_, i) => node(`agent:${i}`, "agent")), ...Array.from({ length: 8 }, (_, i) => node(`action:${i}`, "action"))], []);
    const layout = layoutOperationsMap(model, 390);
    const viewport = operationsMapInitialViewport(layout, 390, 423);
    expect(viewport.zoom).toBe(1);
    const action = layout.positions.get("action:0")!;
    expect(action.x * viewport.zoom + viewport.x).toBeGreaterThanOrEqual(0);
    expect((action.x + OPERATIONS_MAP_NODE_WIDTH) * viewport.zoom + viewport.x).toBeLessThan(390);
    expect(action.y * viewport.zoom + viewport.y).toBeLessThan(423);
    expect(model.edges).toEqual([]);
  });

  it("lays out supervision and Work delegation cycles without dropping recorded edges", () => {
    const nodes = [node("agent:a", "agent"), node("agent:b", "agent"), node("work:a"), node("work:b")];
    const edges = [edge("agent:a", "agent:b", "supervision"), edge("agent:b", "agent:a", "supervision"), edge("agent:a", "work:a"), edge("work:a", "work:b", "parent"), edge("work:b", "work:a", "parent")];
    const model = prepareOperationsMap(nodes, edges);
    const layout = layoutOperationsMap(model);
    expect(layout.positions.size).toBe(4);
    expect([...layout.positions.values()].every(point => Number.isFinite(point.x) && Number.isFinite(point.y))).toBe(true);
    expect(model.edges).toEqual(edges);
    expect(new Set([...layout.positions.values()].map(point => `${point.x}:${point.y}`)).size).toBe(4);
  });
});
