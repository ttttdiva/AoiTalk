"use client";

import Link from "next/link";
import { Dialog as DialogPrimitive } from "@base-ui/react/dialog";
import "@xyflow/react/dist/style.css";
import {
  Background,
  BackgroundVariant,
  Controls,
  ControlButton,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  ViewportPortal,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import { ArrowDownRight, ArrowLeft, ArrowUpRight, ChevronRight, Crosshair, ExternalLink, Network, X } from "lucide-react";
import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";
import { Dialog, DialogClose, DialogPortal, DialogTitle } from "@/components/ui/dialog";
import {
  OPERATIONS_MAP_NODE_HEIGHT,
  OPERATIONS_MAP_NODE_WIDTH,
  OPERATIONS_MAP_MORE_WORK,
  OPERATIONS_MAP_INITIAL_WORK,
  OPERATIONS_MAP_UNASSIGNED,
  layoutOperationsMap,
  operationsMapInitialViewport,
  operationsMapViewportAfterLayout,
  operationsMapKinds,
  operationsMapChildren,
  operationsMapScope,
  operationsMapNeighborhood,
  operationsMapRelations,
  operationsMapSafeHref,
  operationsMapState,
  prepareOperationsMap,
  projectOperationsMap,
  operationsMapWorkGroups,
  operationsMapWorkOrder,
  type OperationsMapEdge,
  type OperationsMapModel,
  type OperationsMapNode,
  type OperationsMapRelation,
  type OperationsMapPage,
  type OperationsMapWorkFilter,
} from "./operations-map-model";

export type OperationsMapCanvasProps = {
  nodes: OperationsMapNode[];
  edges: OperationsMapEdge[];
  loading?: boolean;
  selectedNodeId?: string | null;
  onSelectNode?: (id: string | null) => void;
  onSelectWork: (workId: string) => void;
  /** The execution overlay temporarily owns focus while graph selection remains. */
  executionOpen?: boolean;
  workPage?: OperationsMapPage;
  agentPage?: OperationsMapPage;
  workFilter?: OperationsMapWorkFilter;
  /** Fetched search/deep-link targets only; unknown IDs remain unknown. */
  revealNodeIds?: string[];
};

type CompactFlowNode = Node<{
  entity: OperationsMapNode;
  connectionCount: number;
  workCount: number;
  shownWork: number;
  supervisedCount: number;
  dimmed: boolean;
  highlighted: boolean;
}, "operation">;

const CompactNode = memo(function CompactNode({ data, selected }: NodeProps<CompactFlowNode>) {
  const kind = operationsMapKinds[data.entity.kind];
  const state = operationsMapState(data.entity.state);
  return <div
    className={`operations-map-node operations-map-node--${data.entity.kind}${selected ? " is-selected" : ""}${data.highlighted ? " is-highlighted" : ""}${data.dimmed ? " is-dimmed" : ""}`}
    data-entity-id={data.entity.id}
    data-testid="operations-map-node"
    title={`${data.entity.label} · ${state.label}`}
  >
    <Handle type="target" position={Position.Top} id="in" isConnectable={false} />
    <div className="operations-map-node-heading">
      <span style={{ color: kind.color }}>{kind.shortLabel}</span>
      <span className={`operations-map-state operations-map-state--${state.tone}`}>{state.label}</span>
    </div>
    <div className="operations-map-node-name">{data.entity.label}</div>
    {data.entity.detail && data.entity.note && data.entity.detail !== data.entity.note && <div className="operations-map-node-detail" title={data.entity.detail}>{data.entity.detail}</div>}
    <div className="operations-map-node-note">{data.entity.note || data.entity.detail || ""}</div>
    <div className="operations-map-node-footer">
      <code title={data.entity.entityId}>{data.entity.entityId.length > 16 ? `${data.entity.entityId.slice(0, 8)}…${data.entity.entityId.slice(-6)}` : data.entity.entityId}</code>
      <span>{data.entity.kind === "agent" ? `Work ${data.shownWork}/${data.workCount}${data.supervisedCount ? ` · 部下${data.supervisedCount}` : ""}` : `関連 ${data.connectionCount}件`}</span>
      <ChevronRight size={12} aria-hidden="true" />
    </div>
    <Handle type="source" position={Position.Bottom} id="out" isConnectable={false} />
  </div>;
});

const NODE_TYPES = { operation: CompactNode };
const RELATION_ORDER: OperationsMapRelation[] = ["supervision", "assignment", "parent", "approval", "membership", "execution", "dependency"];

type WorkGroup = ReturnType<typeof projectOperationsMap>["groups"][number];
const EMPTY_REVEAL_IDS: string[] = [];
const pageComplete = (page?: OperationsMapPage) => page?.hasMore === false && page.available === page.fetched;
function WorkGroupSummary({ group, label, page, onMore }: { group: WorkGroup; label: string; page?: OperationsMapPage; onMore: (id: string) => void }) {
  const displayedOutside = group.displayed - (group.matching.length - group.hidden);
  return <section className="operations-map-work-group" data-testid="operations-map-work-group" data-agent-id={group.id} aria-label={`${label}の担当Work表示`}>
    <h4>{label}</h4>
    <p>表示{group.displayed}件 · 取得済み{group.fetched}件 · 対象総数{pageComplete(page) ? `${group.fetched}件` : "不明"}</p>
    <p>未展開{group.hidden}件{group.filtered ? ` · 条件外${group.filtered}件` : ""}{!pageComplete(page) ? " · 未取得の担当Work数は不明" : ""}</p>
    {displayedOutside > 0 ? <p>選択で表示した条件外{displayedOutside}件は「表示」と「条件外」の両方に含まれます。</p> : null}
    <ul className="operations-map-work-states" aria-label="取得済み担当Workの状態別件数">{Object.entries(group.states).sort(([a], [b]) => a.localeCompare(b)).map(([state, count]) => <li key={state}>{operationsMapState(state).label} ({state}) {count}件</li>)}</ul>
    {group.hidden || group.fetched > OPERATIONS_MAP_INITIAL_WORK ? <button type="button" className="operations-map-primary-button" aria-disabled={!group.hidden} onClick={() => { if (group.hidden) onMore(group.id); }}>{group.hidden ? "さらに24件表示" : "取得済みの条件一致Workをすべて表示中"}</button> : null}
  </section>;
}

function MapInspector({ model, selected, onSelect, onSelectWork, onEnter, mobile, closeRef, workGroup, workPage, onMore }: {
  model: OperationsMapModel;
  selected: OperationsMapNode;
  onSelect: (id: string | null) => void;
  onSelectWork: (workId: string) => void;
  onEnter?: (id: string) => void;
  mobile?: boolean;
  closeRef?: RefObject<HTMLButtonElement | null>;
  workGroup?: WorkGroup;
  workPage?: OperationsMapPage;
  onMore: (id: string) => void;
}) {
  const incoming = model.incoming.get(selected.id) || [];
  const outgoing = model.outgoing.get(selected.id) || [];
  const href = operationsMapSafeHref(selected.href);
  const state = operationsMapState(selected.state);
  const children = operationsMapChildren(model, selected.id);
  const relations = (items: OperationsMapEdge[], direction: "incoming" | "outgoing") => <ul className="operations-map-relation-list">
    {items.map(edge => {
      const other = model.byId.get(direction === "incoming" ? edge.source : edge.target)!;
      const relation = operationsMapRelations[edge.relation];
      return <li key={edge.id}>
        <button type="button" onClick={() => onSelect(other.id)} className="operations-map-relation-button">
          <span className="operations-map-relation-type" style={{ color: relation.color }}>
            {direction === "incoming" ? <ArrowDownRight size={12} aria-hidden="true" /> : <ArrowUpRight size={12} aria-hidden="true" />}
            {edge.label || relation.label}
          </span>
          <span>{other.label}</span>
        </button>
      </li>;
    })}
  </ul>;

  return <>
      <div className="operations-map-inspector-topline">
        <span className="operations-map-kind-label">{operationsMapKinds[selected.kind].shortLabel}</span>
        <span className={`operations-map-state operations-map-state--${state.tone}`}>{state.label}</span>
        {mobile ? <DialogClose ref={closeRef} className="operations-map-icon-button" aria-label="選択を解除" data-testid="operations-map-inspector-dismiss"><X size={15} /></DialogClose>
          : <button type="button" className="operations-map-icon-button" onClick={() => onSelect(null)} aria-label="選択を解除"><X size={15} /></button>}
      </div>
      {mobile ? <DialogTitle className="operations-map-inspector-title">{selected.label}</DialogTitle> : <h3 className="operations-map-inspector-title">{selected.label}</h3>}
      <p className="operations-map-entity-id">{selected.entityId}</p>
      {selected.detail ? <p className="operations-map-detail">{selected.detail}</p> : null}
      {selected.note ? <p className="operations-map-evidence-note">{selected.note}</p> : null}
      {children.length && onEnter ? <button type="button" className="operations-map-primary-button operations-map-enter" onClick={() => onEnter(selected.id)}>関連する内訳を開く · {children.length}件 <ChevronRight size={14} aria-hidden="true" /></button> : null}
      {selected.kind === "agent" ? <WorkGroupSummary group={workGroup || { id: selected.id, works: [], matching: [], fetched: 0, displayed: 0, hidden: 0, filtered: 0, states: {} }} label={selected.label} page={workPage} onMore={onMore} /> : null}
      {(selected.workId || href) ? <div className="operations-map-inspector-actions">
        {selected.workId ? <button type="button" className="operations-map-primary-button" onClick={() => onSelectWork(selected.workId!)}>実行の内側を見る <ChevronRight size={14} aria-hidden="true" /></button> : null}
        {href ? <Link className="operations-map-source-link" href={href}>正本で確認 <ExternalLink size={12} aria-hidden="true" /></Link> : null}
      </div> : null}
      <section className="operations-map-inspector-section">
        <h4>入ってくる関係 <span>{incoming.length}</span></h4>
        {incoming.length ? relations(incoming, "incoming") : <p className="operations-map-muted">記録なし</p>}
      </section>
      <section className="operations-map-inspector-section">
        <h4>出ていく関係 <span>{outgoing.length}</span></h4>
        {outgoing.length ? relations(outgoing, "outgoing") : <p className="operations-map-muted">記録なし</p>}
      </section>
      {selected.metadata?.length ? <details className="operations-map-record-details">
        <summary>記録の詳細 <span>{selected.metadata.length}</span></summary>
        <dl className="operations-map-metadata">
          {selected.metadata.map((entry, index) => <div key={`${entry.label}:${index}`}><dt>{entry.label}</dt><dd>{entry.value || "未記録"}</dd></div>)}
        </dl>
      </details> : null}
      {!incoming.length && !outgoing.length ? <p className="operations-map-evidence-note">取得済みの範囲に、この記録の相手IDを持つ関係はありません。名前から担当や親子関係を推測していません。</p> : null}
  </>;
}

function MapContent({ nodes, edges, loading, selectedNodeId, onSelectNode, onSelectWork, executionOpen = false, workPage, agentPage, workFilter = "all", revealNodeIds = EMPTY_REVEAL_IDS }: OperationsMapCanvasProps) {
  const model = useMemo(() => prepareOperationsMap(nodes, edges), [nodes, edges]);
  const [scopeTrail, setScopeTrail] = useState<string[]>([]);
  const scopeId = scopeTrail.at(-1) || null;
  const scopeKey = scopeTrail.join("\u001f");
  const scoped = useMemo(() => operationsMapScope(model, scopeId), [model, scopeId]);
  const scopeSnapshots = useRef(new Map<string, { viewport: { x: number; y: number; zoom: number }; selectedId: string | null; scrollTop: number }>());
  const inspectorPanel = useRef<HTMLElement | null>(null);
  const inspectorScroll = useRef<Record<string, number>>({});
  const focusSelection = useRef<string | null>(null);
  const [localSelection, setLocalSelection] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const selectedId = selectedNodeId !== undefined ? selectedNodeId : localSelection;
  const selected = selectedId ? model.byId.get(selectedId) : undefined;
  const highlightedId = hoveredId && model.byId.has(hoveredId) ? hoveredId : selected?.id || null;
  const neighborhood = operationsMapNeighborhood(model, highlightedId);
  const canvasRef = useRef<HTMLElement>(null);
  const inspectorCloseRef = useRef<HTMLButtonElement>(null);
  const browserRef = useRef<HTMLDetailsElement>(null);
  const contextRef = useRef<HTMLDetailsElement>(null);
  const [search, setSearch] = useState("");
  const [limits, setLimits] = useState(new Map<string, number>());
  const [pinned, setPinned] = useState(new Set<string>());
  const [ordered, setOrdered] = useState(() => ({ model, order: operationsMapWorkOrder(new Map(), operationsMapWorkGroups(model)) }));
  let workOrder = ordered.order;
  if (ordered.model !== model) {
    workOrder = operationsMapWorkOrder(ordered.order, operationsMapWorkGroups(model));
    setOrdered({ model, order: workOrder });
  }
  const incomingReveals = [...revealNodeIds, ...(selectedId ? [selectedId] : [])];
  if (selected && ["work", "run"].includes(selected.kind)) for (const id of model.neighbors.get(selected.id) || []) if (["work", "run"].includes(model.byId.get(id)?.kind || "")) incomingReveals.push(id);
  const newReveals = incomingReveals.filter(id => model.byId.has(id) && !pinned.has(id));
  let requestedIds = pinned;
  if (newReveals.length) {
    requestedIds = new Set([...pinned, ...newReveals]);
    setPinned(requestedIds);
  }
  const projection = useMemo(() => projectOperationsMap(scoped.model, { limits, order: workOrder, revealIds: requestedIds, workFilter }), [scoped.model, limits, workOrder, requestedIds, workFilter]);
  const displayModel = useMemo(() => prepareOperationsMap(projection.nodes, model.edges), [projection.nodes, model.edges]);
  const [mobileInspector, setMobileInspector] = useState(false);
  useEffect(() => {
    const media = window.matchMedia("(max-width:700px)");
    const sync = () => setMobileInspector(media.matches);
    sync();
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);
  const flowContainer = useRef<HTMLDivElement>(null);
  const [viewportWidth, setViewportWidth] = useState(0);
  const [viewportHeight, setViewportHeight] = useState(0);
  useEffect(() => {
    const element = flowContainer.current;
    if (!element) return;
    const resize = new ResizeObserver(entries => {
      const width = entries[0]?.contentRect.width;
      if (width && width > 0) setViewportWidth(width);
      const height = entries[0]?.contentRect.height;
      if (height && height > 0) setViewportHeight(height);
    });
    resize.observe(element);
    return () => resize.disconnect();
  }, []);
  const layout = useMemo(() => layoutOperationsMap(displayModel, viewportWidth || 1000, model, scoped.outsideIds), [displayModel, viewportWidth, model, scoped.outsideIds]);
  const moreWork = (id: string) => {
    const shown = new Set(projection.nodes.map(node => node.id));
    const group = projection.groups.find(group => group.id === id);
    const eligible = new Set(group?.matching.map(work => work.id));
    const matchingOrder = (workOrder.get(id) || []).filter(workId => eligible.has(workId));
    const added = matchingOrder.filter(workId => !shown.has(workId)).slice(0, OPERATIONS_MAP_MORE_WORK);
    // Advance through the next 24 hidden records even when selected records
    // already occupy this prefix. Expansion still respects the Work filter.
    const lastAdded = added.at(-1);
    if (lastAdded) setLimits(previous => new Map(previous).set(id, matchingOrder.indexOf(lastAdded) + 1));
  };
  const { fitView, getViewport, setViewport, viewportInitialized } = useReactFlow<CompactFlowNode>();
  const select = (id: string | null) => {
    if (id && !model.byId.has(id)) return;
    if (selectedId && inspectorPanel.current) inspectorScroll.current[selectedId] = inspectorPanel.current.scrollTop;
    if (id) {
      setPinned(previous => previous.has(id) ? previous : new Set(previous).add(id));
      if (!scoped.model.byId.has(id)) {
        scopeSnapshots.current.set(scopeKey, { viewport: getViewport(), selectedId: selectedId || null, scrollTop: inspectorPanel.current?.scrollTop || 0 });
        setScopeTrail([]);
      }
    }
    if (selectedNodeId === undefined) setLocalSelection(id);
    onSelectNode?.(id);
  };
  const initiallyFitted = useRef(false);
  const explicitFit = useRef<symbol | null>(null);
  const moveScope = (next: string[]) => {
    if (selectedId && inspectorPanel.current) inspectorScroll.current[selectedId] = inspectorPanel.current.scrollTop;
    scopeSnapshots.current.set(scopeKey, { viewport: getViewport(), selectedId: selectedId || null, scrollTop: inspectorPanel.current?.scrollTop || 0 });
    const snapshot = scopeSnapshots.current.get(next.join("\u001f"));
    setHoveredId(null);
    setScopeTrail(next);
    const restored = snapshot?.selectedId;
    const valid = restored && model.byId.has(restored) ? restored : null;
    if (selectedNodeId === undefined) setLocalSelection(valid);
    onSelectNode?.(valid);
  };
  const enterScope = (id: string) => {
    if (!operationsMapChildren(model, id).length || id === scopeId) return;
    const ancestor = scopeTrail.indexOf(id);
    moveScope(ancestor >= 0 ? scopeTrail.slice(0, ancestor + 1) : scopeId && !scoped.insideIds.has(id) ? [id] : [...scopeTrail, id]);
  };
  const fitWholeMap = (duration: number) => {
    initiallyFitted.current = true;
    const request = Symbol("explicit-fit");
    explicitFit.current = request;
    void fitView({ padding: 0.06, duration, minZoom: 0.02, maxZoom: 1 }).finally(() => {
      if (explicitFit.current === request) explicitFit.current = null;
    });
  };
  // Fit only the first records in this mounted scope. Later layout changes
  // preserve a visible record at the current pan/zoom, including user moves
  // made while a page was loading. No visible anchor means no camera change.
  const previousLayout = useRef<typeof layout | null>(null);
  const previousScopeKey = useRef(scopeKey);
  useLayoutEffect(() => {
    if (previousScopeKey.current === scopeKey || !viewportWidth || !viewportHeight || !viewportInitialized) return;
    previousScopeKey.current = scopeKey;
    previousLayout.current = layout;
    initiallyFitted.current = true;
    explicitFit.current = null;
    focusSelection.current = scopeSnapshots.current.get(scopeKey)?.selectedId || null;
    void setViewport(scopeSnapshots.current.get(scopeKey)?.viewport || operationsMapInitialViewport(layout, viewportWidth, viewportHeight), { duration: 0 });
  }, [scopeKey, layout, viewportWidth, viewportHeight, viewportInitialized, setViewport]);
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const snapshot = scopeSnapshots.current.get(scopeKey);
      if (inspectorPanel.current) inspectorPanel.current.scrollTop = snapshot?.selectedId === selectedId ? snapshot?.scrollTop || 0 : selectedId ? inspectorScroll.current[selectedId] || 0 : 0;
    });
    return () => cancelAnimationFrame(frame);
  }, [scopeKey, selectedId, mobileInspector]);
  useLayoutEffect(() => {
    const previous = previousLayout.current;
    previousLayout.current = layout;
    // An explicit fit owns its animation. Advance the layout baseline without
    // interrupting it or applying a deferred correction after it completes.
    if (!initiallyFitted.current || !previous || previous === layout || explicitFit.current) return;
    const rect = flowContainer.current?.getBoundingClientRect();
    const correction = operationsMapViewportAfterLayout(previous.positions, layout.positions, getViewport(), rect?.width || viewportWidth, rect?.height || viewportHeight, selected?.id);
    if (correction) void setViewport(correction.viewport, { duration: 0 });
  }, [layout, getViewport, setViewport, viewportWidth, viewportHeight, selected?.id]);
  useEffect(() => {
    // The provisional desktop layout is never a camera baseline. Wait for
    // the actual canvas dimensions and the initialized ReactFlow viewport.
    if (initiallyFitted.current || !displayModel.nodes.length || !viewportWidth || !viewportHeight || !viewportInitialized) return;
    const frame = requestAnimationFrame(() => {
      if (initiallyFitted.current) return;
      initiallyFitted.current = true;
      void setViewport(operationsMapInitialViewport(layout, viewportWidth, viewportHeight), { duration: 0 });
    });
    return () => cancelAnimationFrame(frame);
  }, [displayModel.nodes.length, layout, setViewport, viewportWidth, viewportHeight, viewportInitialized]);

  useEffect(() => {
    if (!selected || executionOpen || focusSelection.current === selected.id) return;
    if (!viewportWidth || !viewportHeight || !viewportInitialized) return;
    const frame = requestAnimationFrame(() => {
      if (mobileInspector) {
        inspectorCloseRef.current?.focus();
      } else {
        const point = layout.positions.get(selected.id);
        const rect = flowContainer.current?.getBoundingClientRect();
        if (point && rect?.width && rect.height && !explicitFit.current) {
          const viewport = getViewport();
          const x = point.x * viewport.zoom + viewport.x;
          const y = point.y * viewport.zoom + viewport.y;
          if (x < 0 || y < 0 || x + OPERATIONS_MAP_NODE_WIDTH * viewport.zoom > rect.width || y + OPERATIONS_MAP_NODE_HEIGHT * viewport.zoom > rect.height) {
            void setViewport({ x: rect.width / 2 - (point.x + OPERATIONS_MAP_NODE_WIDTH / 2) * viewport.zoom, y: rect.height / 2 - (point.y + OPERATIONS_MAP_NODE_HEIGHT / 2) * viewport.zoom, zoom: viewport.zoom }, { duration: 0 });
          }
        }
        const target = Array.from(canvasRef.current?.querySelectorAll<HTMLElement>(".react-flow__node,[data-map-context-id]") || []).find(node => node.dataset.id === selected.id || node.dataset.mapContextId === selected.id);
        target?.focus({ preventScroll: true });
      }
      focusSelection.current = selected.id;
    });
    return () => cancelAnimationFrame(frame);
  }, [selected, layout, mobileInspector, executionOpen, getViewport, setViewport, viewportWidth, viewportHeight, viewportInitialized]);
  useEffect(() => { if (!selected) focusSelection.current = null; }, [selected]);

  const flowNodes: CompactFlowNode[] = useMemo(() => projection.nodes.map(node => ({
    id: node.id,
    type: "operation",
    position: layout.positions.get(node.id)!,
    width: OPERATIONS_MAP_NODE_WIDTH,
    height: OPERATIONS_MAP_NODE_HEIGHT,
    data: {
      entity: node,
      connectionCount: model.neighbors.get(node.id)?.size || 0,
      workCount: new Set((model.outgoing.get(node.id) || []).filter(edge => edge.relation === "assignment" && model.byId.get(edge.target)?.kind === "work").map(edge => edge.target)).size,
      shownWork: projection.groups.find(group => group.id === node.id)?.displayed || 0,
      supervisedCount: new Set((model.outgoing.get(node.id) || []).filter(edge => edge.relation === "supervision").map(edge => edge.target)).size,
      dimmed: neighborhood.size > 0 && !neighborhood.has(node.id),
      highlighted: neighborhood.has(node.id),
    },
    selected: selected?.id === node.id,
    draggable: false,
    ariaRole: "button",
    ariaLabel: `${operationsMapKinds[node.kind].label}、${node.label}、${operationsMapState(node.state).label}`,
    style: { width: OPERATIONS_MAP_NODE_WIDTH, height: OPERATIONS_MAP_NODE_HEIGHT },
  })), [model, projection, layout, neighborhood, selected]);
  const flowEdges: Edge[] = useMemo(() => projection.edges.map(edge => {
    const relation = operationsMapRelations[edge.relation];
    const highlighted = highlightedId === edge.source || highlightedId === edge.target;
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      sourceHandle: "out",
      targetHandle: "in",
      type: edge.relation === "supervision" || edge.relation === "assignment" ? "smoothstep" : "default",
      markerEnd: { type: MarkerType.ArrowClosed, color: relation.color, width: 14, height: 14 },
      style: { stroke: relation.color, strokeWidth: highlighted ? 2.5 : edge.relation === "supervision" ? 2 : 1.5, strokeDasharray: relation.dash, opacity: highlightedId ? highlighted ? 1 : 0.15 : 0.75 },
      zIndex: highlighted ? 2 : 0,
      ariaLabel: edge.label || relation.label,
      interactionWidth: 0,
    };
  }), [projection.edges, highlightedId]);
  const searchTerm = search.trim().toLocaleLowerCase();
  const searchResults = searchTerm ? model.nodes.filter(node => `${node.label} ${node.entityId} ${node.state}`.toLocaleLowerCase().includes(searchTerm)) : [];
  const unknownTargets = [...new Set([...revealNodeIds, ...(selectedId ? [selectedId] : [])])].filter(id => !model.byId.has(id));
  const restoreDesktopFocus = (originalNode?: HTMLElement | null) => {
    const target = originalNode || Array.from(canvasRef.current?.querySelectorAll<HTMLElement>(".react-flow__node,[data-map-context-id]") || []).find(node => node.dataset.id === selected?.id || node.dataset.mapContextId === selected?.id);
    // ReactFlow queues its own Escape blur before this bubbling handler.
    // Restore afterward, while preserving child controls' Escape handling.
    requestAnimationFrame(() => {
      if (target?.isConnected && !target.closest("[inert]")) target.focus({ preventScroll: true });
    });
  };

  return <section ref={canvasRef} tabIndex={-1} className="operations-map" data-testid="operations-map-canvas" aria-label="Operations 全体関係図" onKeyDown={event => {
    if (event.key !== "Escape" || event.defaultPrevented || executionOpen || (mobileInspector && selected)) return;
    const target = event.target instanceof HTMLElement ? event.target : null;
    const originalNode = target?.closest<HTMLElement>(".react-flow__node");
    if (originalNode || target?.closest(".operations-map-inspector")) restoreDesktopFocus(originalNode);
    event.stopPropagation(); setHoveredId(null);
    if (!selected && scopeTrail.length) moveScope(scopeTrail.slice(0, -1)); else select(null);
  }}>
    <style>{MAP_STYLES}</style>
    <div className="operations-map-main" data-testid="operations-map-main" inert={mobileInspector && Boolean(selected) && !executionOpen}>
      <header className="operations-map-header">
        <nav className="operations-map-breadcrumb" aria-label="関係図の階層">
          {scopeTrail.length ? <button type="button" onClick={() => moveScope(scopeTrail.slice(0, -1))} aria-label="関係図の前の階層へ戻る"><ArrowLeft size={14} /> 戻る</button> : null}
          <button type="button" aria-current={!scopeTrail.length ? "page" : undefined} onClick={() => moveScope([])}>全体</button>
          {scopeTrail.map((id, index) => <span key={id}><ChevronRight size={12} aria-hidden="true" /><button type="button" aria-current={index === scopeTrail.length - 1 ? "page" : undefined} onClick={() => moveScope(scopeTrail.slice(0, index + 1))}>{model.byId.get(id)?.label || "取得範囲外"}</button></span>)}
        </nav>
        <div className="operations-map-title-row">
          <h2 className="sr-only">全体関係図</h2><Network size={16} aria-hidden="true" />
          <span className="operations-map-summary">取得{model.nodes.length}{scopeId ? ` · この階層${scoped.insideIds.size}` : ""} · 描画{projection.nodes.length}</span>
          {loading ? <span className="operations-map-refreshing" role="status">更新中</span> : null}
          <details ref={browserRef} className="operations-map-browser"><summary>記録・表示</summary><div className="operations-map-browser-panel">
            <p>取得済み{model.nodes.length}件 · 描画{projection.nodes.length}件 · 未展開Work{projection.hiddenWork}件 · 補助{projection.auxiliaryNodes.length}件</p>
            <p>Work: 取得済み{workPage?.fetched ?? model.nodes.filter(node => node.kind === "work").length}件 / 対象総数{workPage?.available ?? "不明"}{!pageComplete(workPage) ? " · 未取得あり/不明" : ""}</p>
            <p>Agent: 取得済み{agentPage?.fetched ?? model.nodes.filter(node => node.kind === "agent").length}件 / 対象総数{agentPage?.available ?? "不明"}{!pageComplete(agentPage) ? " · 未取得あり/不明" : ""}</p>
            <label className="operations-map-search-label">取得済みの記録を検索<input type="search" value={search} onChange={event => setSearch(event.target.value)} aria-label="取得済みの記録を検索" placeholder="名前・ID・状態" /></label>
            {searchTerm ? <div data-testid="operations-map-search-results"><p>取得済みの一致{searchResults.length}件{searchResults.length > 20 ? " · 先頭20件。入力を絞ってください" : ""}</p>{searchResults.slice(0, 20).map(node => <button type="button" key={node.id} data-map-search-id={node.id} onClick={() => { select(node.id); setSearch(""); if (node.kind !== "project" && node.kind !== "space" && browserRef.current) browserRef.current.open = false; }}>{node.label} <span>{node.entityId}</span></button>)}{!searchResults.length ? <p>取得済みの範囲に一致はありません。未取得・取得範囲外の記録は検索対象に含まれません。</p> : null}</div> : null}
            <details className="operations-map-work-display"><summary>担当Workの表示</summary>{projection.groups.map(group => <WorkGroupSummary key={group.id} group={group} label={group.id === OPERATIONS_MAP_UNASSIGNED ? "担当Agent未記録・取得範囲外" : model.byId.get(group.id)!.label} page={workPage} onMore={moreWork} />)}</details>
            <details ref={contextRef} className="operations-map-context"><summary>Project / Space 補助一覧 {projection.auxiliaryNodes.length}件</summary><div data-testid="operations-map-context-list">{projection.auxiliaryNodes.map(node => <button type="button" key={node.id} data-map-context-id={node.id} aria-current={selectedId === node.id ? "true" : undefined} onClick={() => select(node.id)}>{operationsMapKinds[node.kind].shortLabel} · {node.label} <span>{node.entityId}</span></button>)}</div></details>
          </div></details>
          <details className="operations-map-help"><summary>概要・取得件数</summary><aside className="operations-map-help-panel" aria-label="関係図の概要" data-testid="operations-map-overview">
            <p className="operations-map-detail">記録された所属・担当・委任・実行関係を表示しています。ノードを選ぶと詳細を開きます。</p>
            <dl className="operations-map-metadata">{Object.entries(operationsMapKinds).map(([kind, value]) => <div key={kind}><dt>{value.label}</dt><dd>{scoped.model.nodes.filter(node => node.kind === kind).length}件取得</dd></div>)}</dl>
            <p className="operations-map-hint">取得済みの関係のみ。所属・アクセスや担当の記録は所有関係を意味しません。</p>
          </aside></details>
          <details className="operations-map-help"><summary>凡例・操作</summary><div className="operations-map-help-panel">
          <div className="operations-map-legend" aria-label="関係の種類">
          {RELATION_ORDER.map(relation => {
            const count = model.edges.filter(edge => edge.relation === relation).length;
            if (!count) return null;
            const descriptor = operationsMapRelations[relation];
            return <span className="operations-map-legend-item" key={relation} title={descriptor.description}>
              <svg width="20" height="10" viewBox="0 0 20 10" aria-hidden="true"><path d="M0 5H17M13 2L18 5L13 8" fill="none" stroke={descriptor.color} strokeWidth="1.5" strokeDasharray={descriptor.dash} /></svg>
              {descriptor.label}<span>{count}</span>
            </span>;
          })}
          {!model.edges.length ? <span className="operations-map-muted">この範囲に取得済みの関係はありません</span> : null}
        </div>
          <p className="operations-map-hint">ドラッグで移動 · スクロールで拡大・縮小 · 選択・ホバーで直接の関係を強調 · 空白または Esc で選択解除</p>
          <p className="operations-map-hint">全体操作で表示中のカードに合わせて移動・縮小できます。未展開Workは「記録・表示」で追加できます。選択で展開した記録は同じ範囲内で残ります。</p>
          </div></details>
          <button type="button" className="operations-map-fit-button" onClick={() => {
            // Read at activation so a changed OS preference applies immediately.
            const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
            fitWholeMap(reducedMotion ? 0 : 250);
          }} aria-label="関係図全体を表示"><Crosshair size={13} aria-hidden="true" />全体</button>
        </div>
      </header>
      <div ref={flowContainer} className="operations-map-flow" data-testid="operations-map-flow">
        {projection.nodes.length ? <ReactFlow<CompactFlowNode>
          nodes={flowNodes}
          edges={flowEdges}
          nodeTypes={NODE_TYPES}
          minZoom={0.02}
          maxZoom={2}
          colorMode="dark"
          nodesDraggable={false}
          nodesConnectable={false}
          nodesFocusable
          edgesFocusable={false}
          elementsSelectable
          deleteKeyCode={null}
          selectionOnDrag={false}
          selectionKeyCode={null}
          multiSelectionKeyCode={null}
          zoomOnDoubleClick={false}
          onNodeClick={(_, node) => select(node.id)}
          onNodeDoubleClick={(_, node) => enterScope(node.id)}
          onNodeMouseEnter={(_, node) => setHoveredId(node.id)}
          onNodeMouseLeave={() => setHoveredId(null)}
          onPaneClick={() => { setHoveredId(null); select(null); }}
          onNodesChange={changes => {
            const change = changes.find(item => item.type === "select" && item.selected);
            if (change?.type === "select") select(change.id);
          }}
          ariaLabelConfig={{ "controls.zoomIn.ariaLabel": "拡大", "controls.zoomOut.ariaLabel": "縮小", "controls.fitView.ariaLabel": "関係図全体を表示" }}
        >
          <ViewportPortal>{layout.bands.map(band => <div key={band.id} className={`operations-map-band operations-map-band--${band.id}`} style={{ left: band.x, top: band.y, width: band.width }}>{band.label}</div>)}</ViewportPortal>
          <Background variant={BackgroundVariant.Dots} gap={22} size={0.7} color="#273038" />
          <Controls showInteractive={false} showFitView={false}>
            <ControlButton className="react-flow__controls-fitview" onClick={() => fitWholeMap(0)} aria-label="関係図全体を表示" title="関係図全体を表示"><Crosshair size={16} aria-hidden="true" /></ControlButton>
          </Controls>
        </ReactFlow> : <div className="operations-map-empty" role="status"><Network size={30} aria-hidden="true" /><p>{loading ? "関係図を読み込んでいます" : projection.auxiliaryNodes.length ? "Project / Spaceは補助一覧で確認できます" : "この範囲の記録はありません"}</p><span>範囲や絞り込みを変更すると、取得できた関係が表示されます。</span></div>}
      </div>
      <footer className="operations-map-footer"><span>{unknownTargets.length ? `選択・検索対象${unknownTargets.length}件は未取得/取得範囲外/不明` : `未展開Work ${projection.hiddenWork}件 · 補助一覧 ${projection.auxiliaryNodes.length}件`}</span><span>取得済みの実記録関係のみ</span></footer>
    </div>
    {!mobileInspector && selected ? <aside ref={element => { inspectorPanel.current = element; }} className="operations-map-inspector" aria-label="関係図の詳細" data-testid="operations-map-inspector">
      <MapInspector model={model} selected={selected} onSelect={id => { if (!id) restoreDesktopFocus(); select(id); }} onSelectWork={onSelectWork} onEnter={selected.id === scopeId ? undefined : enterScope} workGroup={projection.groups.find(group => group.id === selected.id)} workPage={workPage} onMore={moreWork} />
    </aside> : null}
    <Dialog
      open={mobileInspector && Boolean(selected) && !executionOpen}
      modal={false}
      disablePointerDismissal
      onOpenChange={(open, details) => {
        if (open) return;
        if (details.reason === "escape-key" && (details.event.defaultPrevented || details.event.cancelBubble)) {
          details.cancel();
          return;
        }
        setHoveredId(null);
        select(null);
      }}
    >
      <DialogPortal container={canvasRef}>
        {mobileInspector && selected ? <DialogPrimitive.Popup
          ref={element => { inspectorPanel.current = element; }}
          className="operations-map-inspector"
          data-testid="operations-map-inspector"
          aria-modal={false}
          initialFocus={inspectorCloseRef}
          finalFocus={() => {
            // The Work execution overlay owns focus until it has closed.
            if (executionOpen || canvasRef.current?.closest("[inert]")) return false;
            const originalNode = Array.from(canvasRef.current?.querySelectorAll<HTMLElement>(".react-flow__node,[data-map-context-id]") || []).find(node => node.dataset.id === selected.id || node.dataset.mapContextId === selected.id);
            return originalNode || canvasRef.current || false;
          }}
        >
          <MapInspector model={model} selected={selected} onSelect={select} onSelectWork={onSelectWork} onEnter={selected.id === scopeId ? undefined : enterScope} mobile closeRef={inspectorCloseRef} workGroup={projection.groups.find(group => group.id === selected.id)} workPage={workPage} onMore={moreWork} />
        </DialogPrimitive.Popup> : null}
      </DialogPortal>
    </Dialog>
  </section>;
}

export function OperationsMapCanvas(props: OperationsMapCanvasProps) {
  return <ReactFlowProvider><MapContent {...props} /></ReactFlowProvider>;
}

const MAP_STYLES = `
.operations-map{--map-canvas:#101416;--map-panel:#171d21;--map-border:#30383e;--map-text:#e0e6e9;--map-muted:#8e999f;position:relative;display:flex;flex:1;min-height:0;min-width:0;overflow:hidden;background:var(--map-canvas);color:var(--map-text);color-scheme:dark;font-size:12px;text-align:left}
.operations-map *{box-sizing:border-box}
.operations-map button,.operations-map a{-webkit-tap-highlight-color:transparent}
.operations-map button:focus-visible,.operations-map a:focus-visible{outline:2px solid #71b8e5;outline-offset:3px}
.operations-map-main{display:flex;flex:1;min-width:0;min-height:0;flex-direction:column}
.operations-map-header{border-bottom:1px solid var(--map-border);padding:8px 12px}
.operations-map-title-row{display:flex;align-items:center;gap:10px;min-height:24px;flex-wrap:wrap}
.operations-map-title-row h2{display:flex;align-items:center;gap:7px;margin:0;font-size:14px;font-weight:650;letter-spacing:.01em}
.operations-map-summary{color:var(--map-muted);font-size:12px;font-variant-numeric:tabular-nums}
.operations-map-browser{position:relative}.operations-map-browser>summary{cursor:pointer;border:1px solid #364049;border-radius:5px;padding:4px 7px;font-size:11px;white-space:nowrap}.operations-map-browser-panel{position:absolute;left:0;top:calc(100% + 8px);z-index:20;width:min(370px,calc(100vw - 24px));max-height:min(440px,65vh);overflow:auto;padding:12px;border:1px solid var(--map-border);border-radius:6px;background:var(--map-panel);box-shadow:0 8px 24px #0004;font-size:11px;scrollbar-width:thin}.operations-map-browser-panel p{margin:4px 0;overflow-wrap:anywhere;line-height:1.6}.operations-map-browser-panel summary{cursor:pointer;padding:8px 0;color:#c1cbd1}.operations-map-search-label{display:block;margin:12px 0}.operations-map-search-label input{display:block;width:100%;margin-top:5px;padding:7px;border:1px solid #43515b;border-radius:5px;background:#10181d;color:#e0e6e9;font-size:12px}.operations-map-browser-panel [data-map-search-id],.operations-map-context button{display:block;width:100%;padding:7px;border:1px solid transparent;border-radius:5px;background:transparent;color:#c4d7e5;text-align:left;overflow-wrap:anywhere;cursor:pointer}.operations-map-browser-panel [data-map-search-id]:hover,.operations-map-context button:hover{border-color:#37434b;background:#1d272e}.operations-map-browser-panel button span{display:block;color:#8e999f;font-size:10px}.operations-map-work-group{margin:9px 0;padding:9px;border:1px solid #344048;border-radius:5px;font-size:11px}.operations-map-work-group h4{margin:0 0 6px;color:#c6d1d8;font-size:12px}.operations-map-work-group p{margin:4px 0;line-height:1.6;overflow-wrap:anywhere}.operations-map-work-states{display:flex;flex-wrap:wrap;gap:4px 9px;list-style:none;margin:7px 0;padding:0;color:#9eadb7;font-size:10px}.operations-map-work-group .operations-map-primary-button{margin-top:8px}
.operations-map-refreshing{color:#89cba7;font-size:11px}
.operations-map-fit-button{display:inline-flex;align-items:center;gap:5px;border:1px solid #364049;border-radius:5px;padding:4px 7px;background:#1c2429;color:#c5d4dd;font-size:11px;cursor:pointer}
.operations-map-fit-button:hover{background:#28343b}
.operations-map-help{position:relative;margin-left:auto}.operations-map-help summary{cursor:pointer;border:1px solid #364049;border-radius:5px;padding:4px 7px;font-size:11px;white-space:nowrap}.operations-map-help-panel{position:absolute;right:0;top:calc(100% + 8px);z-index:20;width:min(360px,calc(100vw - 40px));padding:12px;border:1px solid var(--map-border);border-radius:6px;background:var(--map-panel);box-shadow:0 8px 24px #0004}
.operations-map-legend{display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-top:6px;min-height:20px}
.operations-map-legend-item{display:inline-flex;align-items:center;gap:5px;border:1px solid #293238;border-radius:12px;padding:2px 7px;background:#171d21;color:#c1cbd1;font-size:11px;white-space:nowrap}
.operations-map-legend-item>span{color:#89969e;font-variant-numeric:tabular-nums}
.operations-map-hint{margin:7px 0 0;color:#7e8b93;font-size:11px;line-height:1.5}
.operations-map-flow{position:relative;flex:1;min-height:0;min-width:0;background:var(--map-canvas)}
.operations-map-flow .react-flow{--xy-background-color:#101416;--xy-node-background-color:#171d21;--xy-node-border:#374249;--xy-controls-button-background-color:#1b2328;--xy-controls-button-background-color-hover:#2a373f;--xy-controls-button-color:#c6d1d8;--xy-controls-button-border-color:#354048;--xy-attribution-background-color:#151c20}
.operations-map .react-flow__node-operation{border:0;border-radius:8px;background:transparent;cursor:pointer}
.operations-map .react-flow__node-operation:focus-visible{outline:2px solid #6cb7eb;outline-offset:4px}
.operations-map .react-flow__handle{width:3px;min-width:3px;height:3px;min-height:3px;border:0;background:transparent;pointer-events:none}
.operations-map .react-flow__edge-path{transition:opacity .15s,stroke-width .15s}
.operations-map .react-flow__controls{border:1px solid #354048;border-radius:6px;overflow:hidden;box-shadow:0 3px 12px #0003}
.operations-map .react-flow__attribution{font-size:8px;color:#82929c}
.operations-map-node{position:relative;width:244px;height:156px;border:1px solid #364048;border-radius:8px;padding:10px 12px;background:#181e22;box-shadow:3px 3px 0 #1c2429,4px 4px 0 #303a42;transition:opacity .15s,border-color .15s,box-shadow .15s}
.operations-map-node--action{border-color:#5d4166;background:#221c27;box-shadow:3px 3px 0 #28212e,4px 4px 0 #44344e}
.operations-map-node.is-dimmed{opacity:.23}
.operations-map-node.is-highlighted{border-color:#687984}
.operations-map-node.is-selected{border-color:#75b8e5;box-shadow:0 0 0 2px #75b8e52e,3px 3px 0 #1f2b33;opacity:1}
.operations-map-node-heading{display:flex;align-items:center;justify-content:space-between;gap:5px;min-width:0;font-size:12px;font-weight:650;line-height:17px;letter-spacing:.075em}
.operations-map-state{display:inline-block;max-width:98px;overflow:hidden;border-radius:5px;padding:0 5px;background:#2b3237;color:#abb5bd;font-size:12px;font-weight:600;line-height:17px;text-overflow:ellipsis;white-space:nowrap;letter-spacing:0}
.operations-map-state--active{background:#1c3541;color:#8bc9ee}
.operations-map-state--ready{background:#19362d;color:#76c69c}
.operations-map-state--attention{background:#403323;color:#dec185}
.operations-map-state--failed{background:#43292c;color:#e3999d}
.operations-map-node-name{overflow:hidden;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;min-height:46px;max-height:46px;margin-top:3px;color:#e5eaed;font-size:16px;font-weight:600;line-height:23px;overflow-wrap:anywhere;white-space:normal}
.operations-map-node-detail{overflow:hidden;color:#c4d1d9;font-size:12px;line-height:18px;text-overflow:ellipsis;white-space:nowrap}
.operations-map-node-note{overflow:hidden;margin-top:3px;color:#b0bcc4;font-size:12px;line-height:18px;text-overflow:ellipsis;white-space:nowrap}
.operations-map-node-footer{display:flex;align-items:center;justify-content:space-between;gap:6px;margin-top:3px;color:#9aa7af;font-size:12px;line-height:18px}.operations-map-node-footer code{flex-shrink:0;font:inherit;color:#c1cbd1}
.operations-map-node-footer svg{color:#70a8ca}
.operations-map-node-footer span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.operations-map-band{position:absolute;border-top:1px solid #33404a;padding:6px 0 0;color:#a9bac6;font-size:12px;line-height:18px;pointer-events:none}.operations-map-band--actions{border-top-style:dashed;border-color:#65516b;color:#c3a1cf}.operations-map-band--unassigned{color:#c1ab88}
.operations-map-footer{display:flex;justify-content:space-between;gap:8px;padding:8px 13px;border-top:1px solid #242e34;color:#7e8b93;font-size:9px}
.operations-map-inspector{position:relative;flex:0 0 300px;width:300px;min-width:0;min-height:0;border-left:1px solid var(--map-border);overflow:auto;padding:16px;background:var(--map-panel);scrollbar-width:thin;scrollbar-color:#435059 transparent}
.operations-map-breadcrumb{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:8px;font-size:12px;line-height:1.6}.operations-map-breadcrumb span{display:flex;min-width:0;align-items:center;gap:6px}.operations-map-breadcrumb button{display:flex;align-items:center;gap:5px;border:1px solid #354049;border-radius:5px;padding:4px 7px;background:#1a2329;color:#b8cedb;cursor:pointer;max-width:240px;overflow-wrap:anywhere;text-align:left}.operations-map-breadcrumb [aria-current=page]{border-color:transparent;background:transparent;color:#e1e8ec}.operations-map-enter{margin-top:14px}.operations-map-band--outside{border-top-style:dashed;color:#b1ac9b}.operations-map-band--context{border-top-style:dashed;color:#aab8c8}
.operations-map-inspector-topline{position:sticky;top:-16px;z-index:3;display:flex;align-items:center;gap:7px;min-height:36px;margin:-16px -16px 0;padding:8px 16px;background:var(--map-panel);border-bottom:1px solid var(--map-border)}
.operations-map-kind-label{border:1px solid #3c454c;border-radius:4px;padding:1px 5px;color:#a3afb6;font-size:9px;font-weight:650;letter-spacing:.08em}
.operations-map-icon-button{display:flex;align-items:center;justify-content:center;width:24px;height:24px;margin-left:auto;border:0;border-radius:5px;background:transparent;color:#98a7b0;cursor:pointer}
.operations-map-icon-button:hover{background:#2b353c;color:#e2eaef}
.operations-map-inspector-title{margin:9px 0 4px;overflow-wrap:anywhere;color:#e2e9ed;font-size:16px;font-weight:650;line-height:1.5}
.operations-map-entity-id{overflow-wrap:anywhere;margin:0 0 13px;color:#8e9ba3;font-family:ui-monospace,SFMono-Regular,monospace;font-size:10px;line-height:1.55}
.operations-map-detail{overflow-wrap:anywhere;margin:9px 0;color:#b9c4ca;font-size:12px;line-height:1.7;white-space:pre-wrap}
.operations-map-record-details{margin-top:22px;border-top:1px solid #303b43;padding-top:12px}.operations-map-record-details summary{color:#aab9c2;font-size:11px;cursor:pointer}.operations-map-record-details summary span{margin-left:5px;color:#748793;font-size:10px}
.operations-map-metadata{margin:18px 0 0}
.operations-map-metadata>div{margin:10px 0;overflow-wrap:anywhere}
.operations-map-metadata dt{color:#8d9da7;font-size:10px;line-height:1.5}
.operations-map-metadata dd{margin:3px 0 0;color:#d0dae0;font-size:11px;line-height:1.6;white-space:pre-wrap}
.operations-map-inspector-actions{display:flex;align-items:flex-start;gap:10px;flex-direction:column;margin:18px 0 22px}
.operations-map-primary-button{display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;border:1px solid #42667c;border-radius:6px;padding:8px 10px;background:#213b4a;color:#bfdef1;font-size:11px;font-weight:600;cursor:pointer}
.operations-map-primary-button:hover{background:#2b4e62}
.operations-map-source-link{display:inline-flex;align-items:center;gap:5px;color:#88bddc;font-size:11px;text-decoration:none}
.operations-map-source-link:hover{text-decoration:underline}
.operations-map-inspector-section{margin-top:22px}
.operations-map-inspector-section h4{display:flex;gap:7px;margin:0 0 9px;color:#9caab3;font-size:10px;font-weight:600;letter-spacing:.06em}
.operations-map-inspector-section h4 span{color:#70818c;font-variant-numeric:tabular-nums}
.operations-map-muted{margin:5px 0;color:#7f8e98;font-size:10px;line-height:1.6}
.operations-map-relation-list{list-style:none;margin:0;padding:0}
.operations-map-relation-list li+li{margin-top:5px}
.operations-map-relation-button{display:flex;flex-direction:column;gap:4px;width:100%;border:1px solid transparent;border-radius:5px;padding:6px 7px;background:transparent;text-align:left;font-size:11px;color:#c4d7e5;cursor:pointer;overflow-wrap:anywhere}
.operations-map-relation-button:hover{border-color:#37434b;background:#1d272e}
.operations-map-relation-type{display:flex;align-items:center;gap:4px;font-size:10px}
.operations-map-evidence-note{margin:22px 0 0;color:#7e8f9a;font-size:10px;line-height:1.7}
.operations-map-empty{display:flex;align-items:center;justify-content:center;flex-direction:column;height:100%;padding:30px;color:#7e919e;text-align:center}
.operations-map-empty p{margin:12px 0 6px;color:#c1d0da;font-size:13px}
.operations-map-empty span{font-size:11px;line-height:1.7}
@media(max-width:700px){.operations-map-header{padding:7px 10px}.operations-map-title-row{gap:7px}.operations-map-summary{font-size:11px}.operations-map-browser-panel{position:fixed;left:12px;top:auto}.operations-map-help-panel{right:-50px}.operations-map-legend{gap:3px}.operations-map-legend-item{font-size:10px;padding:1px 5px}.operations-map-footer{font-size:9px}.operations-map-inspector{position:absolute;top:8px;right:8px;bottom:8px;z-index:10;width:calc(100% - 16px);max-height:calc(100% - 16px);border:1px solid var(--map-border);border-radius:8px;box-shadow:0 8px 30px #0005}.operations-map-breadcrumb button{max-width:200px}}
@media(prefers-reduced-motion:reduce){.operations-map-node,.operations-map .react-flow__edge-path{transition:none}}
`;
