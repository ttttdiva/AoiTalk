// @vitest-environment jsdom
import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { TeamGraphCanvas } from "./team-graph-canvas";
import { emptyGraph, type GraphDefinition } from "@/lib/agent-team-graph-api";

// Native select is a test-only stand-in for the application control.
// eslint-disable-next-line no-restricted-syntax
vi.mock("@/components/ui/app-select", () => ({ AppSelect: (props: React.SelectHTMLAttributes<HTMLSelectElement>) => <select {...props} /> }));
vi.mock("@xyflow/react", () => ({
  Background: () => null, Controls: () => null, Handle: () => null,
  ViewportPortal: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  MarkerType: { ArrowClosed: "arrowclosed" }, Position: { Top: "top", Bottom: "bottom" },
  ReactFlow: ({ nodes, edges, nodeTypes, onConnect, onReconnect, onEdgeClick, onNodesChange, children }: {
    nodes: { id: string; data: unknown; selected: boolean }[]; edges: { id: string }[]; nodeTypes: { agent: React.ComponentType<{ data: unknown; selected: boolean }> };
    onConnect: (connection: { source: string; target: string }) => void;
    onReconnect: (edge: { id: string }, connection: { source: string; target: string }) => void;
    onEdgeClick: (event: null, edge: { id: string }) => void;
    onNodesChange: (changes: ({ type: "position"; id: string; position: { x: number; y: number } } | { type: "dimensions"; id: string; dimensions: { width: number; height: number } } | { type: "select"; id: string; selected: boolean })[]) => void;
    children: React.ReactNode;
  }) => <div>
    <button onClick={() => onNodesChange([{ type: "select", id: nodes[0].id, selected: true }])}>select first</button>
    <button onClick={() => onNodesChange(nodes.map(node => ({ type: "select", id: node.id, selected: true })))}>select all</button>
    <button onClick={() => onConnect({ source: nodes[0].id, target: nodes[1].id })}>connect</button>
    <button onClick={() => onConnect({ source: nodes[0].id, target: nodes[0].id })}>self connect</button>
    <button onClick={() => onEdgeClick(null, edges[0])}>select edge</button>
    <button onClick={() => onReconnect(edges[0], { source: nodes[1].id, target: nodes[0].id })}>reconnect</button>
    <button onClick={() => onNodesChange([{ type: "dimensions", id: nodes[0].id, dimensions: { width: 250, height: 130 } }])}>measure</button>
    <button onClick={() => onNodesChange([{ type: "position", id: nodes[0].id, position: { x: 320, y: 240 } }])}>move</button>
    {nodes.map(node => <nodeTypes.agent key={node.id} data={node.data} selected={node.selected} />)}
    {children}
  </div>,
}));

const agents = [
  { id: "a", slug: "a", display_name: "統括Agent", state: "active" as const },
  { id: "b", slug: "b", display_name: "作業Agent", state: "active" as const },
];
function Harness({ initial = emptyGraph() }: { initial?: GraphDefinition }) {
  const [definition, setDefinition] = useState<GraphDefinition>(initial);
  return <><TeamGraphCanvas definition={definition} onChange={setDefinition} agents={agents} revisions={{}} onAgentSettings={vi.fn()} /><output data-testid="definition">{JSON.stringify(definition)}</output></>;
}
const snapshot = (): GraphDefinition => JSON.parse(screen.getByTestId("definition").textContent!);

describe("controlled Agent Team canvas", () => {
  it("distinguishes same-named agents while retaining full IDs in callbacks and saved nodes", () => {
    const duplicates = [
      { id: "12345678-full-agent-one", slug: "one", display_name: "research", state: "active" as const },
      { id: "87654321-full-agent-two", slug: "two", display_name: "research", state: "active" as const },
      { id: "abcdef12-full-agent-long", slug: "long", display_name: "very long agent name ".repeat(20), state: "active" as const },
    ];
    const onChange = vi.fn();
    const onAgentSettings = vi.fn();
    const definition: GraphDefinition = { ...emptyGraph(), root_key: "root-node", nodes: [
      { key: "root-node", agent_id: duplicates[0].id, revision_id: "revision-full", role: "coordinator", x: 0, y: 0, title: "A very long custom name" },
      { key: "other-node", agent_id: duplicates[1].id, revision_id: null, role: "worker", x: 200, y: 0, title: null },
    ] };
    render(<TeamGraphCanvas definition={definition} onChange={onChange} agents={duplicates} revisions={{}} onAgentSettings={onAgentSettings} />);
    const cards = screen.getAllByTestId("team-graph-node");
    expect(within(cards[0]).getByText("[12345678]")).toBeVisible();
    expect(within(cards[0]).getByText("ROOT")).toBeVisible();
    expect(within(cards[0]).getByText("A very long custom name")).toHaveClass("truncate");
    expect(within(cards[1]).getByText("[87654321]")).toBeVisible();
    expect(within(cards[1]).getByText("research")).toBeVisible();
    const selector = screen.getByLabelText("追加する Agent");
    expect(within(selector).getByRole("option", { name: "[12345678] research · one" })).toHaveValue(duplicates[0].id);
    expect(within(selector).getByRole("option", { name: "[87654321] research · two" })).toHaveValue(duplicates[1].id);
    expect(within(selector).getByRole("option", { name: `[abcdef12] ${duplicates[2].display_name.trim()} · long` })).toHaveValue(duplicates[2].id);
    fireEvent.click(screen.getByRole("button", { name: "select first" }));
    expect(screen.getByText("Agent: research [12345678]")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Agent・モデル設定を開く" }));
    expect(onAgentSettings).toHaveBeenCalledWith(duplicates[0].id, "root-node");
    fireEvent.change(selector, { target: { value: duplicates[1].id } });
    fireEvent.click(screen.getByRole("button", { name: "ノードを追加" }));
    expect(onChange.mock.lastCall![0].nodes.at(-1).agent_id).toBe(duplicates[1].id);
    expect(onChange.mock.lastCall![0].nodes[0]).toEqual(definition.nodes[0]);
  });
  it("round-trips nodes, connection and group edits through the definition", () => {
    render(<Harness />);
    fireEvent.change(screen.getByLabelText("追加する Agent"), { target: { value: "a" } });
    fireEvent.click(screen.getByRole("button", { name: "ノードを追加" }));
    fireEvent.change(screen.getByLabelText("表示名"), { target: { value: "リーダー" } });
    fireEvent.click(screen.getByRole("button", { name: "Root に設定" }));
    expect(snapshot().nodes[0].role).toBe("coordinator");
    fireEvent.click(screen.getByRole("button", { name: "self connect" }));
    expect(snapshot().edges).toHaveLength(0);
    fireEvent.change(screen.getByLabelText("追加する Agent"), { target: { value: "b" } });
    fireEvent.click(screen.getByRole("button", { name: "ノードを追加" }));
    fireEvent.click(screen.getByRole("button", { name: "move" }));
    fireEvent.click(screen.getByRole("button", { name: "connect" }));
    fireEvent.click(screen.getByRole("button", { name: "connect" }));
    expect(snapshot().edges).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "reconnect" }));
    expect(snapshot().edges[0]).toMatchObject({ source: snapshot().nodes[1].key, target: snapshot().nodes[0].key });
    fireEvent.click(screen.getByRole("button", { name: "select edge" }));
    fireEvent.change(screen.getByLabelText("関係"), { target: { value: "handoff" } });
    fireEvent.change(screen.getByLabelText("成果物キー（任意）"), { target: { value: "artifact" } });
    fireEvent.click(screen.getByRole("button", { name: "select all" }));
    fireEvent.click(screen.getByRole("button", { name: "グループ化 (2)" }));
    fireEvent.change(screen.getByLabelText("実行種別"), { target: { value: "media_image" } });
    fireEvent.change(screen.getByLabelText("名前"), { target: { value: "アプリ開発用" } });
    fireEvent.click(screen.getByRole("button", { name: "measure" }));
    const enclosure = screen.getByText("アプリ開発用").parentElement!;
    expect(enclosure.style.width).toBe("328px");
    expect(enclosure.style.height).toBe("338px");
    fireEvent.click(screen.getByRole("button", { name: "グループを左へ移動" }));
    fireEvent.change(screen.getByLabelText("目的"), { target: { value: "アプリを作る" } });
    const { nodes } = snapshot();
    fireEvent.change(screen.getByLabelText("リーダー", { selector: "select" }), { target: { value: nodes[0].key } });
    const result = snapshot();
    expect(result.root_key).toBe(nodes[0].key);
    expect(result.nodes[0]).toMatchObject({ agent_id: "a", role: "coordinator", title: "リーダー", x: 280, y: 240 });
    expect(result.nodes[1]).toMatchObject({ agent_id: "b", role: "worker", x: 254, y: 100 });
    expect(result.edges[0]).toMatchObject({ source: nodes[1].key, target: nodes[0].key, kind: "handoff", output_key: "artifact" });
    expect(result.groups[0]).toMatchObject({ title: "アプリ開発用", purpose: "アプリを作る", node_keys: nodes.map(node => node.key), leader_key: nodes[0].key });
    expect(screen.getByText("アプリ開発用")).toBeVisible();
    fireEvent.click(screen.getByRole("checkbox", { name: "リーダー" }));
    expect(snapshot().groups[0].leader_key).toBeNull();
    expect(snapshot().groups[0].execution_kind).toBe("media_image");
    fireEvent.click(screen.getByRole("button", { name: "select all" }));
    fireEvent.click(screen.getByRole("button", { name: "グループ化 (2)" }));
    expect(screen.getByLabelText("実行種別")).toHaveValue("");
    expect(snapshot().groups[1]).not.toHaveProperty("execution_kind");
  });
  it("places new cards apart and preserves custom coordinates, pins and connectors on reload", () => {
    const existing: GraphDefinition = { ...emptyGraph(), root_key: "old", nodes: [
      { key: "old", agent_id: "a", revision_id: "rev-1", role: "coordinator", x: -120, y: -80, title: "Existing" },
      { key: "custom", agent_id: "b", revision_id: "rev-2", role: "worker", x: 130, y: 100, title: null },
    ], edges: [{ key: "edge", source: "old", target: "custom", kind: "handoff", output_key: "result" }] };
    const view = render(<Harness initial={existing} />);
    fireEvent.change(screen.getByLabelText("追加する Agent"), { target: { value: "a" } });
    for (let i = 0; i < 3; i++) fireEvent.click(screen.getByRole("button", { name: "ノードを追加" }));
    const saved = snapshot();
    expect(saved.nodes.slice(0, 2)).toEqual(existing.nodes);
    expect(saved.edges).toEqual(existing.edges);
    expect(saved.root_key).toBe(existing.root_key);
    for (const [index, node] of saved.nodes.entries()) for (const other of saved.nodes.slice(index + 1)) {
      expect(node.x + 190 <= other.x || other.x + 190 <= node.x || node.y + 100 <= other.y || other.y + 100 <= node.y).toBe(true);
    }
    view.unmount();
    render(<Harness initial={JSON.parse(JSON.stringify(saved))} />);
    expect(snapshot()).toEqual(saved);
  });
  it("allows another relation on the same endpoints while rejecting an exact duplicate", () => {
    const node = (key: string) => ({ key, agent_id: key, revision_id: null, role: "worker" as const, x: 0, y: 0, title: null });
    render(<Harness initial={{ ...emptyGraph(), nodes: [node("a"), node("b")], edges: [{ key: "existing", source: "a", target: "b", kind: "reviews", output_key: null }] }} />);
    fireEvent.click(screen.getByRole("button", { name: "connect" }));
    expect(snapshot().edges).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "connect" }));
    expect(snapshot().edges).toHaveLength(2);
  });
  it("keeps omitted and null kinds on unrelated edits and does not infer from purpose", () => {
    const base = { key: "one", title: "One", purpose: "画像制作と動画制作", node_keys: [], leader_key: null };
    render(<Harness initial={{ ...emptyGraph(), groups: [base, { ...base, key: "two", title: "Two", execution_kind: null }] }} />);
    fireEvent.click(screen.getByRole("button", { name: "One · 0人" }));
    expect(screen.getByLabelText("実行種別")).toHaveValue("");
    fireEvent.change(screen.getByLabelText("名前"), { target: { value: "Edited" } });
    expect(snapshot().groups[0]).not.toHaveProperty("execution_kind");
    fireEvent.click(screen.getByRole("button", { name: "Two · 0人" }));
    fireEvent.change(screen.getByLabelText("目的"), { target: { value: "別の目的" } });
    expect(snapshot().groups[1]).toHaveProperty("execution_kind", null);
  });
  it("selects image and video independently and removes the field for generic", () => {
    const base = { key: "one", title: "One", purpose: "", node_keys: [], leader_key: null };
    render(<Harness initial={{ ...emptyGraph(), groups: [base, { ...base, key: "two", title: "Two" }] }} />);
    fireEvent.click(screen.getByRole("button", { name: "One · 0人" }));
    fireEvent.change(screen.getByLabelText("実行種別"), { target: { value: "media_image" } });
    fireEvent.change(screen.getByLabelText("名前"), { target: { value: "Image" } });
    expect(snapshot().groups[0].execution_kind).toBe("media_image");
    fireEvent.click(screen.getByRole("button", { name: "Two · 0人" }));
    expect(screen.getByLabelText("実行種別")).toHaveValue("");
    fireEvent.change(screen.getByLabelText("実行種別"), { target: { value: "media_video" } });
    expect(snapshot().groups[1].execution_kind).toBe("media_video");
    fireEvent.click(screen.getByRole("button", { name: "Image · 0人" }));
    expect(screen.getByLabelText("実行種別")).toHaveValue("media_image");
    fireEvent.change(screen.getByLabelText("実行種別"), { target: { value: "" } });
    expect(snapshot().groups[0]).not.toHaveProperty("execution_kind");
  });

});
