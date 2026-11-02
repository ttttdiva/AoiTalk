"use client";

import "@xyflow/react/dist/style.css";
import { Background, Controls, Handle, MarkerType, Position, ReactFlow, ViewportPortal, type Connection, type Edge, type Node, type NodeProps, type NodeChange } from "@xyflow/react";
import { memo, useCallback, useMemo, useState } from "react";
import { AppSelect } from "@/components/ui/app-select";
import { Checkbox } from "@/components/ui/checkbox";
import { HelpPopover } from "@/components/ui/help-popover";
import type { Employee, EmployeeRevision } from "@/lib/agent-employees-api";
import { boundsForGroup, moveGroupMembers } from "./team-graph-geometry";
import type { GraphDefinition, GraphEdge, GraphGroup, GraphNode } from "@/lib/agent-team-graph-api";

const roles: Record<GraphNode["role"], string> = { coordinator: "統括", worker: "作業", reviewer: "レビュー" };
const kinds: Record<GraphEdge["kind"], { label: string; color: string; dash?: string }> = {
  supervises: { label: "統括", color: "#8b5cf6" }, assigns: { label: "作業を割り当て", color: "#2563eb", dash: "7 3" },
  reviews: { label: "レビュー", color: "#d97706", dash: "3 3" }, handoff: { label: "成果物を引き渡し", color: "#059669" },
};
const input = "w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground";
const button = "rounded-md border border-border bg-background px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50";
const key = () => crypto.randomUUID();
const NODE_WIDTH = 190;
// The card has three text rows and padding; measurements replace this fallback.
const NODE_HEIGHT = 100;
const NODE_GAP = 24;

type AgentFlowNode = Node<{ name: string; agentId: string; role: string; candidates: string; root: boolean }, "agent">;
const AgentNode = memo(function AgentNode({ data, selected }: NodeProps<AgentFlowNode>) {
  return <div className={`w-[190px] rounded-xl border bg-card p-3 text-card-foreground shadow-md ${selected ? "border-primary ring-2 ring-primary/30" : "border-border"}`} data-testid="team-graph-node">
    <Handle type="target" position={Position.Top} />
    <div className="flex items-center justify-between text-xs text-muted-foreground"><span>{data.role}</span><span className="shrink-0">[{data.agentId.slice(0, 8)}]</span>{data.root && <span className="font-semibold text-primary">ROOT</span>}</div>
    <div className="mt-1 truncate font-semibold" title={data.name}>{data.name}</div>
    <div className="mt-2 text-xs text-muted-foreground">{data.candidates}</div>
    <Handle type="source" position={Position.Bottom} />
  </div>;
});
const nodeTypes = { agent: AgentNode };

export type TeamGraphCanvasProps = {
  definition: GraphDefinition;
  onChange: (definition: GraphDefinition) => void;
  agents: Employee[];
  revisions: Record<string, EmployeeRevision | undefined>;
  onAgentSettings: (id: string, nodeKey: string) => void;
};

export function TeamGraphCanvas({ definition, onChange, agents, revisions, onAgentSettings }: TeamGraphCanvasProps) {
  const [selectedNodes, setSelectedNodes] = useState<string[]>([]);
  const [selectedEdge, setSelectedEdge] = useState<string | null>(null);
  const [selectedGroup, setSelectedGroup] = useState<string | null>(null);
  const [agentToAdd, setAgentToAdd] = useState("");
  const [dimensions, setDimensions] = useState<Record<string, { width: number; height: number }>>({});
  const selectedNode = selectedNodes.length === 1 ? definition.nodes.find(node => node.key === selectedNodes[0]) : undefined;
  const edge = definition.edges.find(item => item.key === selectedEdge);
  const group = definition.groups.find(item => item.key === selectedGroup);
  const agentName = (id: string) => agents.find(agent => agent.id === id)?.display_name ?? id;
  const nodeName = (node: GraphNode) => node.title || agentName(node.agent_id);
  const updateNode = (id: string, patch: Partial<GraphNode>) => onChange({ ...definition, nodes: definition.nodes.map(node => node.key === id ? { ...node, ...patch } : node) });
  const updateEdge = (id: string, patch: Partial<GraphEdge>) => onChange({ ...definition, edges: definition.edges.map(item => item.key === id ? { ...item, ...patch } : item) });
  const updateGroup = (id: string, patch: Partial<GraphGroup>) => onChange({ ...definition, groups: definition.groups.map(item => item.key === id ? { ...item, ...patch } : item) });
  const setExecutionKind = (id: string, value: "" | "media_image" | "media_video" | "software_code") => onChange({ ...definition, groups: definition.groups.map(item => {
    if (item.key !== id) return item;
    if (value) return { ...item, execution_kind: value };
    const generic = { ...item };
    delete generic.execution_kind;
    return generic;
  }) });
  const flowNodes = useMemo<AgentFlowNode[]>(() => definition.nodes.map(node => {
    const revision = node.revision_id ? revisions[node.revision_id] : revisions[node.agent_id];
    const known = revision && (!node.revision_id || node.revision_id === revision.id);
    const count = known ? Object.values(revision.model_overrides ?? {}).reduce((sum, route) => sum + (route?.routes.length ?? 0), 0) : null;
    return { id: node.key, type: "agent", position: { x: node.x, y: node.y }, selected: selectedNodes.includes(node.key), data: { agentId: node.agent_id, name: node.title || agents.find(agent => agent.id === node.agent_id)?.display_name || node.agent_id, role: roles[node.role], root: definition.root_key === node.key, candidates: count === null ? "モデル候補: 版を確認中" : `明示モデル候補 ${count}件${count === 0 ? "（継承あり）" : ""}` } };
  }), [definition.nodes, definition.root_key, agents, revisions, selectedNodes]);
  const flowEdges = useMemo<Edge[]>(() => definition.edges.map(item => ({ id: item.key, source: item.source, target: item.target, selected: selectedEdge === item.key, label: kinds[item.kind].label, markerEnd: { type: MarkerType.ArrowClosed, color: kinds[item.kind].color }, style: { stroke: kinds[item.kind].color, strokeWidth: 2, strokeDasharray: kinds[item.kind].dash }, labelStyle: { fill: kinds[item.kind].color, fontWeight: 600, fontSize: 11 }, labelBgStyle: { fill: "var(--card)" } })), [definition.edges, selectedEdge]);
  const nodeBoxes = useMemo(() => definition.nodes.map(node => ({ key: node.key, x: node.x, y: node.y, width: dimensions[node.key]?.width ?? NODE_WIDTH, height: dimensions[node.key]?.height ?? 73 })), [definition.nodes, dimensions]);
  const boxes = useMemo(() => definition.groups.flatMap(item => {
    const bounds = boundsForGroup(item.node_keys, nodeBoxes, 26, 16);
    return bounds ? [{ group: item, ...bounds }] : [];
  }), [definition.groups, nodeBoxes]);
  const moveGroup = (item: GraphGroup, dx: number, dy: number) => {
    const moved = moveGroupMembers(item.node_keys, nodeBoxes, dx, dy);
    const positions = new Map(moved.map(node => [node.key, node]));
    const nodes = definition.nodes.map(node => {
      const position = positions.get(node.key);
      return position && (position.x !== node.x || position.y !== node.y) ? { ...node, x: position.x, y: position.y } : node;
    });
    onChange({ ...definition, nodes });
  };
  const handleNodesChange = useCallback((changes: NodeChange<AgentFlowNode>[]) => {
    const measurements = changes.filter(change => change.type === "dimensions" && change.dimensions);
    if (measurements.length) setDimensions(previous => {
      let next = previous;
      for (const change of measurements) if (change.type === "dimensions" && change.dimensions) {
        const { width, height } = change.dimensions;
        if (width > 0 && height > 0 && (next[change.id]?.width !== width || next[change.id]?.height !== height)) {
          if (next === previous) next = { ...previous };
          next[change.id] = { width, height };
        }
      }
      return next;
    });
    const selections = changes.filter(change => change.type === "select");
    if (selections.length) setSelectedNodes(previous => {
      const next = new Set(previous);
      for (const change of selections) if (change.type === "select") {
        if (change.selected) next.add(change.id); else next.delete(change.id);
      }
      return next.size === previous.length && previous.every(id => next.has(id)) ? previous : [...next];
    });
    const positions = changes.filter(change => change.type === "position" && change.position);
    if (!positions.length) return;
    let changed = false;
    const nodes = definition.nodes.map(node => {
      const change = positions.find(item => item.type === "position" && item.id === node.key);
      if (!change || change.type !== "position" || !change.position || (node.x === change.position.x && node.y === change.position.y)) return node;
      changed = true;
      return { ...node, x: change.position.x, y: change.position.y };
    });
    if (changed) onChange({ ...definition, nodes });
  }, [definition, onChange]);
  const addNode = () => {
    if (!agentToAdd) return;
    const revision = revisions[agentToAdd];
    const width = Math.max(NODE_WIDTH, ...nodeBoxes.map(box => box.width));
    const height = Math.max(NODE_HEIGHT, ...nodeBoxes.map(box => box.height));
    let x = 80;
    const y = 100;
    // Advance past every intersecting card; this also handles arbitrary dragged positions.
    while (true) {
      const overlaps = nodeBoxes.filter(box => x < box.x + box.width + NODE_GAP && x + width + NODE_GAP > box.x && y < box.y + Math.max(box.height, NODE_HEIGHT) + NODE_GAP && y + height + NODE_GAP > box.y);
      if (!overlaps.length) break;
      x = Math.max(...overlaps.map(box => box.x + box.width + NODE_GAP));
    }
    const node: GraphNode = { key: key(), agent_id: agentToAdd, revision_id: revision?.id ?? null, role: "worker", x, y, title: null };
    onChange({ ...definition, nodes: [...definition.nodes, node] }); setSelectedNodes([node.key]); setSelectedEdge(null); setSelectedGroup(null);
  };
  const removeNode = (id: string) => {
    onChange({ ...definition, root_key: definition.root_key === id ? null : definition.root_key, nodes: definition.nodes.filter(node => node.key !== id), edges: definition.edges.filter(item => item.source !== id && item.target !== id), groups: definition.groups.map(item => ({ ...item, node_keys: item.node_keys.filter(member => member !== id), leader_key: item.leader_key === id ? null : item.leader_key })) });
    setSelectedNodes([]);
  };
  const validConnection = (source: string | null, target: string | null, except?: string) => {
    const candidate = definition.edges.find(item => item.key === except);
    const kind = candidate?.kind ?? "assigns";
    const outputKey = candidate?.output_key ?? null;
    return Boolean(source && target && source !== target && definition.nodes.some(node => node.key === source) && definition.nodes.some(node => node.key === target) && !definition.edges.some(item => item.key !== except && item.source === source && item.target === target && item.kind === kind && item.output_key === outputKey));
  };
  const connect = (connection: Connection) => {
    if (!validConnection(connection.source, connection.target)) return;
    onChange({ ...definition, edges: [...definition.edges, { key: key(), source: connection.source!, target: connection.target!, kind: "assigns", output_key: null }] });
  };
  const reconnect = (old: Edge, connection: Connection) => {
    if (validConnection(connection.source, connection.target, old.id)) updateEdge(old.id, { source: connection.source!, target: connection.target! });
  };
  const createGroup = () => {
    if (!selectedNodes.length) return;
    const next: GraphGroup = { key: key(), title: "新しいグループ", purpose: "", node_keys: selectedNodes, leader_key: null };
    onChange({ ...definition, groups: [...definition.groups, next] }); setSelectedGroup(next.key); setSelectedEdge(null);
  };
  return <div className="space-y-3" data-testid="team-graph-editor">
    <div className="flex flex-wrap items-center gap-2">
      <label className="sr-only" htmlFor="team-graph-add-agent">追加する Agent</label>
      <AppSelect id="team-graph-add-agent" className={`${input} max-w-64`} value={agentToAdd} onChange={event => setAgentToAdd(event.target.value)}><option value="">Agent を選択</option>{agents.map(agent => <option key={agent.id} value={agent.id}>[{agent.id.slice(0, 8)}] {agent.display_name}{agent.slug?.trim() ? ` · ${agent.slug.trim()}` : ""}</option>)}</AppSelect>
      <button className={button} type="button" disabled={!agentToAdd} onClick={addNode}>ノードを追加</button>
      <button className={button} type="button" disabled={!selectedNodes.length} onClick={createGroup}>グループ化 ({selectedNodes.length})</button>
      <span className="text-xs text-muted-foreground">Shift キーで複数選択。端点からドラッグして接続できます。</span>
    </div>
    <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_280px]">
      <div className="h-[560px] min-w-0 overflow-hidden rounded-xl border border-border bg-muted/30" aria-label="Agent Team キャンバス">
        <ReactFlow nodes={flowNodes} edges={flowEdges} nodeTypes={nodeTypes} onConnect={connect} onReconnect={reconnect} onNodesChange={handleNodesChange} onNodeClick={() => { setSelectedEdge(null); setSelectedGroup(null); }} onEdgeClick={(_, item) => { setSelectedEdge(item.id); setSelectedGroup(null); setSelectedNodes([]); }} onPaneClick={() => { setSelectedNodes([]); setSelectedEdge(null); setSelectedGroup(null); }} fitView minZoom={0.15} multiSelectionKeyCode="Shift" deleteKeyCode={null}>
          <Background /><Controls />
          <ViewportPortal>{boxes.map(({ group: item, x, y, width, height }) => <div key={item.key} style={{ position: "absolute", transform: `translate(${x}px, ${y}px)`, width, height, pointerEvents: "none", zIndex: 0 }} className="rounded-2xl border-2 border-dashed border-primary/40 bg-primary/5"><span className="absolute left-3 top-2 text-xs font-semibold text-primary">{item.title}</span></div>)}</ViewportPortal>
        </ReactFlow>
      </div>
      <aside className="space-y-4 rounded-xl border border-border bg-card p-4 text-sm text-card-foreground" aria-label="キャンバスの設定">
        {selectedNode && !group && !edge ? <section className="space-y-3"><h3 className="font-semibold">ノード設定</h3><p className="text-muted-foreground">Agent: {agentName(selectedNode.agent_id)} [{selectedNode.agent_id.slice(0, 8)}]</p>
          <label className="block space-y-1">表示名<input className={input} value={selectedNode.title ?? ""} placeholder="Agent 名を使用" onChange={event => updateNode(selectedNode.key, { title: event.target.value || null })} /></label>
          <label className="block space-y-1">役割<AppSelect className={input} value={selectedNode.role} disabled={definition.root_key === selectedNode.key} onChange={event => updateNode(selectedNode.key, { role: event.target.value as GraphNode["role"] })}>{Object.entries(roles).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</AppSelect></label>
          <button className={button} type="button" onClick={() => onChange({ ...definition, root_key: selectedNode.key, nodes: definition.nodes.map(node => node.key === selectedNode.key ? { ...node, role: "coordinator" } : node) })}>{definition.root_key === selectedNode.key ? "Root に設定済み" : "Root に設定"}</button>
          <p className="text-xs text-muted-foreground">モデルは実行時に1候補を使用します。順序付き代替候補は Agent の版で管理します。</p>
          <label className="block space-y-1">Agentの版<AppSelect className={input} value={selectedNode.revision_id ?? ""} onChange={event => updateNode(selectedNode.key, { revision_id: event.target.value || null })}><option value="">常に最新版</option>{Object.values(revisions).filter((revision, index, all) => revision?.agent_id === selectedNode.agent_id && all.findIndex(item => item?.id === revision.id) === index).map(revision => revision && <option key={revision.id} value={revision.id}>v{revision.version} に固定</option>)}</AppSelect></label>
          <button className={button} type="button" onClick={() => onAgentSettings(selectedNode.agent_id, selectedNode.key)}>Agent・モデル設定を開く</button>
          <button className={`${button} text-destructive`} type="button" onClick={() => removeNode(selectedNode.key)}>ノードを削除</button>
        </section> : null}
        {edge ? <section className="space-y-3"><h3 className="font-semibold">接続設定</h3><p className="text-xs text-muted-foreground">{definition.nodes.find(node => node.key === edge.source) ? nodeName(definition.nodes.find(node => node.key === edge.source)!) : edge.source} → {definition.nodes.find(node => node.key === edge.target) ? nodeName(definition.nodes.find(node => node.key === edge.target)!) : edge.target}</p>
          <label className="block space-y-1">接続元<AppSelect className={input} value={edge.source} onChange={event => { if (validConnection(event.target.value, edge.target, edge.key)) updateEdge(edge.key, { source: event.target.value }); }}>{definition.nodes.map(node => <option key={node.key} value={node.key}>{nodeName(node)}</option>)}</AppSelect></label>
          <label className="block space-y-1">接続先<AppSelect className={input} value={edge.target} onChange={event => { if (validConnection(edge.source, event.target.value, edge.key)) updateEdge(edge.key, { target: event.target.value }); }}>{definition.nodes.map(node => <option key={node.key} value={node.key}>{nodeName(node)}</option>)}</AppSelect></label>
          <label className="block space-y-1">関係<AppSelect className={input} value={edge.kind} onChange={event => updateEdge(edge.key, { kind: event.target.value as GraphEdge["kind"] })}>{Object.entries(kinds).map(([value, item]) => <option key={value} value={value}>{item.label}</option>)}</AppSelect></label>
          <p className="text-xs text-muted-foreground">レビューの矢印はレビュアーから作業者へ向きます。成果物は作業者からレビュアーへ流れます。</p>
          <label className="block space-y-1">成果物キー（任意）<input className={input} value={edge.output_key ?? ""} onChange={event => updateEdge(edge.key, { output_key: event.target.value || null })} /></label>
          <button className={`${button} text-destructive`} type="button" onClick={() => { onChange({ ...definition, edges: definition.edges.filter(item => item.key !== edge.key) }); setSelectedEdge(null); }}>接続を削除</button>
        </section> : null}
        {group ? <section className="space-y-3"><h3 className="font-semibold">グループ設定</h3>
          <div className="flex flex-wrap gap-1" aria-label="グループを移動">{[["上", 0, -40], ["左", -40, 0], ["下", 0, 40], ["右", 40, 0]].map(([label, dx, dy]) => <button key={label} className={button} type="button" aria-label={`グループを${label}へ移動`} onClick={() => moveGroup(group, Number(dx), Number(dy))}>{label}</button>)}</div>
          <label className="block space-y-1">名前<input className={input} value={group.title} onChange={event => updateGroup(group.key, { title: event.target.value })} /></label>
          <label className="block space-y-1">目的<textarea className={input} value={group.purpose} onChange={event => updateGroup(group.key, { purpose: event.target.value })} /></label>
          <div className="space-y-1"><div className="flex items-center gap-1"><label htmlFor="group-execution-kind">実行種別</label><HelpPopover label="実行種別">実行種別を明示します。画像・動画の実行可否はサーバーの診断を確認してください。動画の実行は現在未対応です。</HelpPopover></div><AppSelect id="group-execution-kind" className={input} value={group.execution_kind ?? ""} onChange={event => setExecutionKind(group.key, event.target.value as "" | "media_image" | "media_video" | "software_code")}><option value="">通常テキスト</option><option value="media_image">画像制作</option><option value="media_video">動画制作</option><option value="software_code">ソフトウェア開発（Codex）</option></AppSelect></div>
          <fieldset className="space-y-1"><legend>メンバー</legend>{definition.nodes.map(node => <label key={node.key} className="flex items-center gap-2"><Checkbox checked={group.node_keys.includes(node.key)} onCheckedChange={checked => updateGroup(group.key, { node_keys: checked ? [...group.node_keys, node.key] : group.node_keys.filter(id => id !== node.key), ...(!checked && group.leader_key === node.key ? { leader_key: null } : {}) })} />{nodeName(node)}</label>)}</fieldset>
          <label className="block space-y-1">リーダー<AppSelect className={input} value={group.leader_key ?? ""} onChange={event => updateGroup(group.key, { leader_key: event.target.value || null })}><option value="">未設定</option>{definition.nodes.filter(node => group.node_keys.includes(node.key)).map(node => <option key={node.key} value={node.key}>{nodeName(node)}</option>)}</AppSelect></label>
          <button className={`${button} text-destructive`} type="button" onClick={() => { onChange({ ...definition, groups: definition.groups.filter(item => item.key !== group.key) }); setSelectedGroup(null); }}>グループを削除</button>
        </section> : null}
        {!selectedNode && !edge && !group ? <p className="text-muted-foreground">ノードや接続を選択して編集します。</p> : null}
        <section className="space-y-2 border-t border-border pt-3"><h3 className="font-semibold">グループ</h3>{definition.groups.length ? definition.groups.map(item => <button key={item.key} type="button" className={`${button} block w-full text-left`} onClick={() => { setSelectedGroup(item.key); setSelectedEdge(null); }}>{item.title} · {item.node_keys.length}人</button>) : <p className="text-xs text-muted-foreground">ノードを選択してグループ化できます。同じノードを複数グループに追加できます。</p>}</section>
      </aside>
    </div>
    <p className="text-xs text-muted-foreground">接続やグループを編集しても Agent の所属や権限は変更されません。</p>
  </div>;
}
