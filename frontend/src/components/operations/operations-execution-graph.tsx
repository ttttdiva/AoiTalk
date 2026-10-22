"use client";
import "@xyflow/react/dist/style.css";
import { useMemo } from "react";
import dagre from "@dagrejs/dagre";
import { Background, Controls, ReactFlow, type Node, type Edge } from "@xyflow/react";
import { executionDuration, executionTime } from "@/lib/operations-execution-api";
import type { ExecutionNode } from "./operations-execution-model";

type FlowNode = Node<{ label: string }>;
export function OperationsExecutionGraph({ nodes, edges, selected, onSelect }: {
  nodes: ExecutionNode[]; edges: Array<{ id: string; source: string; target: string; label: string }>;
  selected: string; onSelect: (id: string) => void;
}) {
  const flow = useMemo(() => {
    const graph = new dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));
    graph.setGraph({ rankdir: "LR", nodesep: 28, ranksep: 65 });
    nodes.forEach(node => graph.setNode(node.id, { width: 290, height: 160 }));
    edges.forEach(edge => graph.setEdge(edge.source, edge.target)); dagre.layout(graph);
    const related = new Set([selected, ...edges.filter(edge => edge.source === selected || edge.target === selected).flatMap(edge => [edge.source, edge.target])]);
    return { nodes: nodes.map(node => ({ id: node.id, position: { x: graph.node(node.id).x - 145, y: graph.node(node.id).y - 80 },
      data: { label: `${node.title}\n状態 ${node.state}\n工程 ${node.phase || "未記録"} · Tool ${node.tool || "未記録"}\n最終活動 ${executionTime(node.activity)}\n経過 ${executionDuration(node.elapsed)} · Attempt ${node.attempt ?? "未記録"}\n停止理由 ${node.reason || "未記録"}${node.warning ? `\n${node.warning}` : ""}` },
      selected: node.id === selected, ariaLabel: node.title, draggable: false,
      className: "whitespace-pre-line break-all rounded-lg border border-border bg-card p-3 text-left text-xs text-foreground",
      style: { width: 290, minHeight: 160, opacity: related.has(node.id) ? 1 : 0.45 },
    } satisfies FlowNode)), edges: edges.map(edge => ({ ...edge, type: "smoothstep", style: { opacity: edge.source === selected || edge.target === selected ? 1 : 0.3 }, labelStyle: { fontSize: 11 } } satisfies Edge)) };
  }, [nodes, edges, selected]);
  return <div className="hidden h-[520px] min-w-0 overflow-hidden rounded-lg border border-border md:block" aria-label="実行関係図" data-testid="operations-execution-graph">
    <ReactFlow<FlowNode> key={nodes[0]?.id} nodes={flow.nodes} edges={flow.edges} fitView minZoom={0.2} maxZoom={1.5}
      nodesDraggable={false} nodesConnectable={false} nodesFocusable edgesFocusable={false}
      onNodeClick={(_, node) => onSelect(node.id)} onNodesChange={changes => { const change = changes.find(change => change.type === "select" && change.selected); if (change && change.type === "select") onSelect(change.id); }} deleteKeyCode={null}>
      <Background /><Controls showInteractive={false} />
    </ReactFlow>
  </div>;
}
