"use client";

import "@xyflow/react/dist/style.css";

import { useMemo } from "react";
import dagre from "@dagrejs/dagre";
import {
  Background,
  Controls,
  MarkerType,
  ReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import { useTheme } from "@/contexts/theme-context";

import type {
  ProjectOverviewGraphEdge,
  ProjectOverviewGraphNode,
  ProjectOverviewLayout,
} from "./project-overview-model";

const NODE_WIDTH = 220;
const NODE_HEIGHT = 84;

type OverviewFlowNodeData = {
  label: string;
  memoryIds: string[];
  kind: ProjectOverviewGraphNode["kind"];
};

type OverviewFlowEdgeData = {
  memoryIds: string[];
};

export type OverviewFlowNode = Node<OverviewFlowNodeData>;
export type OverviewFlowEdge = Edge<OverviewFlowEdgeData>;

const NODE_CLASS_BY_KIND: Record<
  ProjectOverviewGraphNode["kind"],
  string
> = {
  lead: "border-primary/60 bg-primary/10 text-foreground",
  member: "border-border bg-card text-foreground",
  stakeholder: "border-border bg-surface-container text-foreground",
  system: "border-border bg-muted text-foreground",
  other: "border-border bg-card text-foreground",
};

function nodeLabel(node: ProjectOverviewGraphNode): string {
  return node.subtitle
    ? `${node.label}\n${node.subtitle}`
    : node.label;
}

export function buildProjectOverviewFlow(
  graphValue: ProjectOverviewLayout["graph"],
): {
  nodes: OverviewFlowNode[];
  edges: OverviewFlowEdge[];
} {
  const graph = new dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));
  graph.setGraph({
    rankdir: "LR",
    nodesep: 48,
    ranksep: 90,
    marginx: 24,
    marginy: 24,
  });

  for (const node of graphValue.nodes) {
    graph.setNode(node.id, {
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
    });
  }
  for (const edge of graphValue.edges) {
    graph.setEdge(edge.source, edge.target);
  }
  dagre.layout(graph);

  const nodes: OverviewFlowNode[] = graphValue.nodes.map((node) => {
    const position = graph.node(node.id);
    return {
      id: node.id,
      position: {
        x: Number(position?.x ?? 0) - NODE_WIDTH / 2,
        y: Number(position?.y ?? 0) - NODE_HEIGHT / 2,
      },
      data: {
        label: nodeLabel(node),
        memoryIds: [...node.memory_ids],
        kind: node.kind,
      },
      className: [
        "whitespace-pre-line rounded-lg border px-3 py-2 text-left text-xs shadow-sm",
        NODE_CLASS_BY_KIND[node.kind],
      ].join(" "),
      style: {
        width: NODE_WIDTH,
        minHeight: NODE_HEIGHT,
      },
      draggable: false,
      selectable: true,
    };
  });

  const edges: OverviewFlowEdge[] = graphValue.edges.map(
    (edge: ProjectOverviewGraphEdge, index) => ({
      id: edge.id || `overview-edge-${index}`,
      source: edge.source,
      target: edge.target,
      label: edge.label || undefined,
      type: "smoothstep",
      data: {
        memoryIds: [...edge.memory_ids],
      },
      markerEnd: {
        type: MarkerType.ArrowClosed,
      },
      style: {
        strokeWidth: 1.5,
      },
      labelStyle: {
        fontSize: 11,
      },
      labelBgPadding: [4, 2],
      labelBgBorderRadius: 4,
      selectable: true,
    }),
  );

  return { nodes, edges };
}

export function ProjectOverviewGraph({
  graph,
  onSelectMemoryIds,
}: {
  graph: ProjectOverviewLayout["graph"];
  onSelectMemoryIds: (memoryIds: string[]) => void;
}) {
  const { resolvedTheme } = useTheme();
  const flow = useMemo(
    () => buildProjectOverviewFlow(graph),
    [graph],
  );

  if (flow.nodes.length === 0) return null;

  return (
    <section
      aria-label="Project Overview 関係図"
      className="overflow-hidden rounded-lg border border-border bg-surface-container-lowest"
      data-testid="project-overview-graph"
    >
      <div className="h-[360px] min-h-[280px] w-full">
        <ReactFlow<OverviewFlowNode, OverviewFlowEdge>
          nodes={flow.nodes}
          edges={flow.edges}
          fitView
          fitViewOptions={{ padding: 0.24 }}
          minZoom={0.4}
          maxZoom={1.5}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable
          nodesFocusable
          edgesFocusable
          colorMode={resolvedTheme}
          proOptions={{ hideAttribution: true }}
          onNodeClick={(_event, node) =>
            onSelectMemoryIds(node.data.memoryIds)
          }
          onEdgeClick={(_event, edge) =>
            onSelectMemoryIds(edge.data?.memoryIds ?? [])
          }
        >
          <Background gap={22} size={1} />
          <Controls
            showInteractive={false}
            position="bottom-right"
          />
        </ReactFlow>
      </div>
    </section>
  );
}
