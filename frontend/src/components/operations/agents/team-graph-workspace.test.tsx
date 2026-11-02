// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TeamGraphWorkspace } from "./team-graph-workspace";
import { AoiDotsApiError } from "@/lib/aoidots-api";
import { employeeRequest, EmployeeApiError } from "@/lib/agent-employees-api";
import { emptyGraph, type GraphDefinition } from "@/lib/agent-team-graph-api";

const mocks = vi.hoisted(() => ({ getDefault: vi.fn(), setDefault: vi.fn(), create: vi.fn(), get: vi.fn(), list: vi.fn(), update: vi.fn(), push: vi.fn(), bootstrap: vi.fn(), dispatch: vi.fn(), uncertain: vi.fn(), open: vi.fn(), messages: vi.fn(), revisions: vi.fn(), agent: vi.fn(), catalog: vi.fn(), revision: vi.fn(), tools: vi.fn(), realCanvas: false, project: "project" as string | null, user: "user" as string | null }));
vi.mock("@/lib/agent-employees-api", async importOriginal => ({ ...await importOriginal<typeof import("@/lib/agent-employees-api")>(), employeeRequest: vi.fn().mockResolvedValue({ profile: { pools: {} } }), employeeApi: { revisions: mocks.revisions, get: mocks.agent, catalog: mocks.catalog, revision: mocks.revision, toolCatalog: mocks.tools } }));
vi.mock("@/lib/chat-api", () => ({ chatApi: { getMessages: mocks.messages }, getLlmModelCatalog: vi.fn().mockResolvedValue({ providers: [{ id: "synthetic", label: "Synthetic", models: [{ id: "model", label: "Model" }] }] }) }));
vi.mock("@/contexts/project-context", () => ({ useOptionalProject: () => ({ selectedProjectId: mocks.project }) }));
vi.mock("@/components/providers/swr-global-provider", () => ({ useCurrentUserId: () => mocks.user }));
vi.mock("@/components/layout/shell-context", () => ({ useShellChrome: () => ({ setAoiDotsOpen: mocks.open }) }));
vi.mock("@/lib/aoidots-api", () => ({ AoiDotsApiError: class extends Error { constructor(message: string, public status: number, public code?: string) { super(message); } }, aoiDotsErrorCode: (error: { code?: string }) => error.code ?? null, bootstrapAoiDots: mocks.bootstrap, dispatchAoiDotsMessage: mocks.dispatch, listAoiDotsUncertainInteractiveWork: mocks.uncertain }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: mocks.push }) }));
vi.mock("@/lib/agent-team-graph-api", async importOriginal => ({ ...await importOriginal<typeof import("@/lib/agent-team-graph-api")>(), teamGraphApi: mocks }));
// Native select makes the API stub tests independent of the select portal.
// eslint-disable-next-line no-restricted-syntax
vi.mock("@/components/ui/app-select", () => ({ AppSelect: ({ onValueChange, onChange, ...props }: React.SelectHTMLAttributes<HTMLSelectElement> & { onValueChange?: (value: string) => void }) => <select {...props} onChange={event => { onChange?.(event); onValueChange?.(event.target.value); }} /> }));
vi.mock("./employee-workspace", () => ({ employeeHref: (id: string, panel: string) => `/operations?tab=agents&agent=${id}&panel=${panel}` }));
vi.mock("@xyflow/react", () => ({ Background: () => null, Controls: () => null, Handle: () => null, ViewportPortal: ({ children }: { children: React.ReactNode }) => <>{children}</>, MarkerType: { ArrowClosed: "arrowclosed" }, Position: { Top: "top", Bottom: "bottom" }, ReactFlow: ({ nodes, onNodesChange, children }: { nodes: { id: string }[]; onNodesChange: (changes: unknown[]) => void; children: React.ReactNode }) => <div><output data-testid="flow-nodes">{JSON.stringify(nodes)}</output><button onClick={() => onNodesChange(nodes.map(node => ({ type: "select", id: node.id, selected: true })))}>全ノード選択</button>{children}</div> }));
vi.mock("./team-graph-canvas", async importOriginal => { const actual = await importOriginal<typeof import("./team-graph-canvas")>(); return { TeamGraphCanvas: ({ onAgentSettings, definition, onChange, revisions }: { revisions: Record<string, unknown>; onAgentSettings: (id: string) => void; definition: GraphDefinition; onChange: (value: GraphDefinition) => void }) => mocks.realCanvas ? <actual.TeamGraphCanvas definition={definition} onChange={onChange} revisions={revisions as never} agents={[{ id: "agent-a", slug: "a", display_name: "Agent A", state: "active" }]} onAgentSettings={onAgentSettings} /> : <><button onClick={() => onAgentSettings("agent-a")}>Agent設定</button><button onClick={() => onChange({ ...definition, groups: definition.groups.map(group => ({ ...group, execution_kind: "media_image" })) })}>画像に変更</button><button onClick={() => onChange({ ...definition, root_key: "n", nodes: [{ key: "n", agent_id: "agent-a", revision_id: "rev-1", role: "coordinator", x: 10, y: 20, title: null }], groups: [{ key: "team", title: "Team", purpose: "Build", node_keys: ["n"], leader_key: "n" }] })}>ピンとグループを編集</button><output data-testid="canvas-revisions">{JSON.stringify(revisions)}</output><output data-testid="canvas-definition">{JSON.stringify(definition)}</output></> }; });
const graph = { id: "g", title: "新しい Agent Team", project_id: null, revision_id: "r", version: 1, content_hash: "hash", definition: emptyGraph(), diagnostics: [] };
const mount = () => render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><TeamGraphWorkspace agents={[]} onBack={vi.fn()} /></SWRConfig>);
beforeEach(() => { mocks.realCanvas = false; mocks.project = "project"; mocks.user = "user"; sessionStorage.clear(); vi.clearAllMocks(); mocks.getDefault.mockReset().mockResolvedValue({ available: false, configuration: null }); mocks.setDefault.mockReset(); mocks.bootstrap.mockResolvedValue({ agent_id: "root", session_id: "original" }); mocks.uncertain.mockResolvedValue([]); mocks.dispatch.mockResolvedValue({ success: true }); mocks.revisions.mockResolvedValue({ revisions: [] }); mocks.agent.mockResolvedValue({ agent: { id: "agent-a", display_name: "AoiDots", slug: "synthetic", state: "active" } }); mocks.catalog.mockResolvedValue({ can_manage: true, capabilities: [] }); mocks.tools.mockResolvedValue({ tools: [{ id: "synthetic-tool", label: "Synthetic tool", group: "web", requires_capabilities: [] }] }); mocks.list.mockResolvedValue({ graphs: [graph, { ...graph, id: "other", title: "Other" }] }); mocks.get.mockResolvedValue({ graph }); });
describe("Agent Team workspace", () => {
  it("sets a saved revision with null CAS and verifies it by reading again", async () => {
    const ref = { graph_id: graph.id, revision_id: graph.revision_id, content_hash: graph.content_hash };
    mocks.setDefault.mockResolvedValue({ available: true, configuration: ref });
    mount(); await screen.findByRole("option", { name: "Other" });
    expect(screen.getByRole("button", { name: "AoiDotsの既定にする" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } }); await screen.findByText("保存済み v1");
    mocks.getDefault.mockResolvedValueOnce({ available: true, configuration: ref });
    await waitFor(() => expect(screen.getByRole("button", { name: "AoiDotsの既定にする" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "AoiDotsの既定にする" }));
    await screen.findByText("AoiDotsの既定を確認しました。");
    expect(mocks.setDefault).toHaveBeenCalledWith({ graph_id: "g", revision_id: "r", expected: null });
    expect(mocks.getDefault).toHaveBeenCalledTimes(2);
    expect(mocks.update).not.toHaveBeenCalled(); expect(mocks.dispatch).not.toHaveBeenCalled();
    expect(JSON.parse(screen.getByTestId("canvas-definition").textContent!)).toEqual(graph.definition);
  });
  it("requires explicit refresh after a conflict and preserves edits before retry", async () => {
    const old = { graph_id: "old", revision_id: "old-rev", content_hash: "old-hash" };
    const latest = { graph_id: "new", revision_id: "new-rev", content_hash: "new-hash" };
    const target = { graph_id: "g", revision_id: "r", content_hash: "hash" };
    mocks.getDefault.mockResolvedValue({ available: true, configuration: old });
    mocks.setDefault.mockRejectedValueOnce(new EmployeeApiError(409)).mockResolvedValue({ available: true, configuration: target });
    mount(); await screen.findByRole("option", { name: "Other" }); fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } }); await screen.findByText("保存済み v1");
    await waitFor(() => expect(screen.getByRole("button", { name: "AoiDotsの既定にする" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "AoiDotsの既定にする" }));
    await screen.findByText(/既定の変更結果を確認できません/);
    expect(mocks.setDefault).toHaveBeenCalledWith({ graph_id: "g", revision_id: "r", expected: old });
    fireEvent.change(screen.getByLabelText("チーム名"), { target: { value: "編集中" } });
    mocks.getDefault.mockResolvedValue({ available: true, configuration: latest });
    fireEvent.click(screen.getByRole("button", { name: "現在の既定を再読込" }));
    await waitFor(() => expect(screen.getByText(/new-rev/)).toBeVisible());
    expect(screen.getByLabelText("チーム名")).toHaveValue("編集中");
    expect(screen.getByRole("button", { name: "AoiDotsの既定にする" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("チーム名"), { target: { value: graph.title } });
    mocks.getDefault.mockResolvedValue({ available: true, configuration: target });
    fireEvent.click(screen.getByRole("button", { name: "AoiDotsの既定にする" }));
    await screen.findByText("AoiDotsの既定を確認しました。");
    expect(mocks.setDefault).toHaveBeenCalledTimes(2); expect(mocks.setDefault.mock.calls[1][0].expected).toEqual(latest);
    expect(mocks.update).not.toHaveBeenCalled(); expect(mocks.dispatch).not.toHaveBeenCalled();
  });
  it("allows replacing an unavailable default with null CAS and verifies the result", async () => {
    const target = { graph_id: "g", revision_id: "r", content_hash: "hash" };
    mocks.getDefault.mockResolvedValueOnce({ available: false, configuration: null }).mockResolvedValue({ available: true, configuration: target });
    mocks.setDefault.mockResolvedValue({ available: true, configuration: target });
    mount(); await screen.findByRole("option", { name: "Other" }); fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } }); await screen.findByText("保存済み v1");
    await screen.findByText(/現在有効な既定はありません/);
    await waitFor(() => expect(screen.getByRole("button", { name: "AoiDotsの既定にする" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "AoiDotsの既定にする" }));
    await screen.findByText("AoiDotsの既定を確認しました。");
    expect(mocks.setDefault).toHaveBeenCalledWith({ graph_id: "g", revision_id: "r", expected: null });
    expect(mocks.getDefault).toHaveBeenCalledTimes(2);
  });
  it("blocks PUT when a contradictory default GET is rejected", async () => {
    const actual = await vi.importActual<typeof import("@/lib/agent-team-graph-api")>("@/lib/agent-team-graph-api");
    vi.mocked(employeeRequest).mockResolvedValueOnce({ available: true, configuration: null });
    mocks.getDefault.mockImplementation(actual.teamGraphApi.getDefault);
    mount(); await screen.findByRole("option", { name: "Other" }); fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } }); await screen.findByText("保存済み v1");
    await screen.findByText(/既定の取得に失敗/);
    expect(screen.getByRole("button", { name: "AoiDotsの既定にする" })).toBeDisabled(); expect(mocks.setDefault).not.toHaveBeenCalled();
  });
  it.each(["owner", "navigation"])("allows new default operations after %s while protecting their lock from an old PUT", async mode => {
    let resolveOld!: (value: unknown) => void;
    let resolveNew!: (value: unknown) => void;
    const target = { graph_id: "g", revision_id: "r", content_hash: "hash" };
    mocks.setDefault.mockReturnValueOnce(new Promise(r => { resolveOld = r; })).mockReturnValueOnce(new Promise(r => { resolveNew = r; }));
    const view = mount(); await screen.findByRole("option", { name: "Other" }); fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } }); await screen.findByText("保存済み v1");
    await waitFor(() => expect(screen.getByRole("button", { name: "AoiDotsの既定にする" })).toBeEnabled()); fireEvent.click(screen.getByRole("button", { name: "AoiDotsの既定にする" }));
    if (mode === "owner") {
      mocks.user = "other-user"; view.rerender(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><TeamGraphWorkspace agents={[]} onBack={vi.fn()} /></SWRConfig>);
      await waitFor(() => expect(screen.getByLabelText("保存済みチーム")).toHaveValue(""));
    } else fireEvent(window, new PopStateEvent("popstate"));
    mocks.get.mockResolvedValue({ graph: { ...graph, id: "other", title: "Other" } });
    fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "other" } });
    await waitFor(() => expect(screen.getByLabelText("チーム名")).toHaveValue("Other"));
    fireEvent.click(screen.getByRole("button", { name: "現在の既定を再読込" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "AoiDotsの既定にする" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "AoiDotsの既定にする" }));
    expect(mocks.setDefault).toHaveBeenCalledTimes(2);
    await act(async () => { resolveOld({ available: true, configuration: null }); });
    expect(screen.getByRole("button", { name: "現在の既定を再読込" })).toBeDisabled();
    expect(screen.queryByText("AoiDotsの既定を確認しました。")).not.toBeInTheDocument();
    const other = { ...target, graph_id: "other" }; mocks.getDefault.mockResolvedValue({ available: true, configuration: other });
    resolveNew({ available: true, configuration: other });
    await screen.findByText("AoiDotsの既定を確認しました。");
    expect(mocks.setDefault.mock.calls[1][0]).toEqual({ graph_id: "other", revision_id: "r", expected: null });
    expect(mocks.dispatch).not.toHaveBeenCalled();
  });
  it("distinguishes same-name agents in every template selector without changing their values", async () => {
    const agents = [
      { id: "11111111-1111-4111-8111-111111111111", display_name: "AoiDots", slug: "first", state: "active" as const },
      { id: "22222222-2222-4222-8222-222222222222", display_name: "AoiDots", slug: null, state: "active" as const },
      { id: "33333333-3333-4333-8333-333333333333", display_name: "AoiDots", slug: "", state: "active" as const },
    ];
    render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><TeamGraphWorkspace agents={agents as unknown as Parameters<typeof TeamGraphWorkspace>[0]["agents"]} onBack={vi.fn()} /></SWRConfig>);
    await waitFor(() => expect(screen.getByRole("button", { name: "複数チーム構造テンプレート" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "複数チーム構造テンプレート" }));
    const root = screen.getByLabelText("統括（AoiDots など）") as HTMLSelectElement;
    expect(Array.from(root.options).slice(1).map(option => [option.text, option.value])).toEqual([
      ["AoiDots [11111111] · first", agents[0].id],
      ["AoiDots [22222222]", agents[1].id],
      ["AoiDots [33333333]", agents[2].id],
    ]);
    for (const agent of agents) { fireEvent.change(root, { target: { value: agent.id } }); expect(root).toHaveValue(agent.id); }
    const leader = screen.getByLabelText("チーム1 リーダー") as HTMLSelectElement;
    expect(Array.from(leader.options).slice(1).map(option => option.text)).toEqual(Array.from(root.options).slice(1).map(option => option.text));
  });
  it("builds an explicitly selected two-team template and preserves its pins on save and reload", async () => {
    const ids = Array.from({ length: 7 }, (_, i) => `00000000-0000-4000-8000-00000000000${i + 1}`);
    const agents = ids.map((id, i) => ({ id, slug: `synthetic-${i}`, display_name: i ? `Synthetic ${i}` : "AoiDots", state: "active" as const }));
    mocks.revisions.mockImplementation(async (id: string) => ({ revisions: [{ id: `revision-${id}`, agent_id: id, version: 1 }] }));
    mocks.create.mockImplementationOnce(async (body: { definition: GraphDefinition; title: string }) => { const saved = { ...graph, title: body.title, definition: body.definition }; mocks.get.mockResolvedValue({ graph: saved }); return { graph: saved }; });
    render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><TeamGraphWorkspace agents={agents} onBack={vi.fn()} mapMode /></SWRConfig>);
    await waitFor(() => expect(screen.getByRole("button", { name: "複数チーム構造テンプレート" })).toBeEnabled());
    await waitFor(() => expect(screen.getByTestId("canvas-revisions")).toHaveTextContent(`revision-${ids[0]}`));
    fireEvent.click(screen.getByRole("button", { name: "複数チーム構造テンプレート" }));
    expect(screen.getByRole("button", { name: "下書きに作成" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("統括（AoiDots など）"), { target: { value: ids[0] } });
    fireEvent.click(screen.getByRole("button", { name: "キャンセル" }));
    expect(JSON.parse(screen.getByTestId("canvas-definition").textContent!).nodes).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "複数チーム構造テンプレート" }));
    expect(screen.getByLabelText("統括（AoiDots など）")).toHaveValue("");
    ["統括（AoiDots など）", "チーム1 リーダー", "チーム1 作業者", "チーム1 レビュアー", "チーム2 リーダー", "チーム2 作業者", "チーム2 レビュアー"].forEach((label, index) => fireEvent.change(screen.getByLabelText(label), { target: { value: ids[index] } }));
    fireEvent.click(screen.getByRole("button", { name: "下書きに作成" }));
    const draft = JSON.parse(screen.getByTestId("canvas-definition").textContent!) as GraphDefinition;
    expect(draft.nodes.map(node => [node.agent_id, node.role, node.revision_id])).toEqual(ids.map((id, index) => [id, ["coordinator", "coordinator", "worker", "reviewer", "coordinator", "worker", "reviewer"][index], `revision-${id}`]));
    expect(draft.root_key).toBe(draft.nodes[0].key);
    expect(draft.groups.map(group => group.leader_key)).toEqual([draft.nodes[1].key, draft.nodes[4].key]);
    expect(draft.edges.map(edge => edge.kind)).toEqual(["supervises", "assigns", "reviews", "supervises", "assigns", "reviews"]);
    expect(screen.getByRole("button", { name: "複数チーム構造テンプレート" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByText("保存済み v1");
    expect(mocks.create.mock.calls[0][0].definition).toEqual(draft);
    expect(screen.getByRole("button", { name: "複数チーム構造テンプレート" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "再読込" }));
    await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
    expect(JSON.parse(screen.getByTestId("canvas-definition").textContent!)).toEqual(draft);
  });
  it("creates only selected agents and refuses invalid root or reviewer selections", async () => {
    const agents = [1, 2, 3].map(i => ({ id: `00000000-0000-4000-8000-00000000000${i}`, slug: `s${i}`, display_name: `Synthetic ${i}`, state: "active" as const }));
    render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><TeamGraphWorkspace agents={agents} onBack={vi.fn()} /></SWRConfig>);
    await waitFor(() => expect(screen.getByRole("button", { name: "複数チーム構造テンプレート" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "複数チーム構造テンプレート" }));
    for (const label of ["統括（AoiDots など）", "チーム1 リーダー", "チーム2 リーダー"]) fireEvent.change(screen.getByLabelText(label), { target: { value: agents[0].id } });
    expect(screen.getByRole("button", { name: "下書きに作成" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("チーム1 リーダー"), { target: { value: agents[1].id } });
    fireEvent.change(screen.getByLabelText("チーム2 リーダー"), { target: { value: agents[2].id } });
    fireEvent.change(screen.getByLabelText("チーム1 作業者"), { target: { value: agents[1].id } });
    fireEvent.change(screen.getByLabelText("チーム1 レビュアー"), { target: { value: agents[1].id } });
    expect(screen.getByRole("button", { name: "下書きに作成" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("チーム1 作業者"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("チーム1 レビュアー"), { target: { value: "" } });
    expect(screen.getByText(/未選択のメンバーは作成されません/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "下書きに作成" }));
    const draft = JSON.parse(screen.getByTestId("canvas-definition").textContent!) as GraphDefinition;
    expect(draft.nodes.map(node => node.agent_id)).toEqual(agents.map(agent => agent.id));
    expect(draft.nodes.every(node => node.revision_id === null)).toBe(true);
    expect(draft.edges.map(edge => edge.kind)).toEqual(["supervises", "supervises"]);
    expect(mocks.create).not.toHaveBeenCalled();
  });
  it("does not offer the template for an existing saved graph or change its old pin", async () => {
    const definition: GraphDefinition = { ...emptyGraph(), root_key: "root", nodes: [{ key: "root", agent_id: "00000000-0000-4000-8000-000000000001", revision_id: "00000000-0000-4000-8000-000000000002", role: "coordinator", title: null, x: 0, y: 0 }] };
    mocks.get.mockResolvedValue({ graph: { ...graph, definition } });
    mount();
    await screen.findByRole("option", { name: "Other" });
    fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } });
    await screen.findByText("保存済み v1");
    expect(screen.getByRole("button", { name: "複数チーム構造テンプレート" })).toBeDisabled();
    expect(JSON.parse(screen.getByTestId("canvas-definition").textContent!)).toEqual(definition);
    expect(mocks.update).not.toHaveBeenCalled();
  });
  it("restores an owner-scoped edited graph after navigation and remount", async () => {
    const view = mount();
    fireEvent.change(screen.getByLabelText("チーム名"), { target: { value: "Draft team" } });
    fireEvent.click(screen.getByRole("button", { name: "ピンとグループを編集" }));
    const draft = screen.getByTestId("canvas-definition").textContent;
    await waitFor(() => expect(sessionStorage.getItem("aoitalk:graph-root-request:user")).toContain("Draft team"));
    fireEvent(window, new PopStateEvent("popstate"));
    view.unmount();
    mocks.user = "B";
    const other = mount();
    await waitFor(() => expect(screen.getByLabelText("チーム名")).toHaveValue("新しい Agent Team"));
    expect(screen.getByTestId("canvas-definition").textContent).not.toBe(draft);
    other.unmount();
    mocks.user = "user";
    mount();
    await waitFor(() => expect(screen.getByLabelText("チーム名")).toHaveValue("Draft team"));
    expect(screen.getByTestId("canvas-definition").textContent).toBe(draft);
    expect(JSON.parse(draft!).nodes[0].revision_id).toBe("rev-1");
    expect(JSON.parse(draft!).groups[0].node_keys).toEqual(["n"]);
  });
  it("keeps a dispatched create intent across navigation and ignores its late completion", async () => {
    let resolveCreate!: (value: { graph: typeof graph }) => void;
    mocks.create.mockReturnValueOnce(new Promise(resolve => { resolveCreate = resolve; }));
    const view = mount();
    fireEvent.change(screen.getByLabelText("チーム名"), { target: { value: "Pending create" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
    const sent = mocks.create.mock.calls[0][0];
    expect(JSON.parse(sessionStorage.getItem("aoitalk:graph-root-request:user")!).intent).toEqual(sent);
    fireEvent(window, new PopStateEvent("popstate"));
    view.unmount();
    resolveCreate({ graph });
    mount();
    await screen.findByRole("button", { name: "保存状態を確認" });
    expect(screen.getByRole("button", { name: "新規" })).toBeDisabled();
    expect(screen.getByLabelText("チーム名")).toHaveValue("Pending create");
    expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
    mocks.create.mockResolvedValueOnce({ graph });
    fireEvent.click(screen.getByRole("button", { name: "保存状態を確認" }));
    await screen.findByText("保存済み v1");
    expect(mocks.create).toHaveBeenCalledTimes(2);
    expect(mocks.create.mock.calls[1][0]).toEqual(sent);
    expect(JSON.parse(sessionStorage.getItem("aoitalk:graph-root-request:user")!).intent).toBeNull();
  });
  it("refreshes agent revisions on focus without replacing a pinned revision", async () => {
    const agent = { id: "agent-a" } as Parameters<typeof TeamGraphWorkspace>[0]["agents"][number];
    mocks.revisions.mockResolvedValueOnce({ revisions: [{ id: "rev-1", version: 1 }] }).mockResolvedValue({ revisions: [{ id: "rev-2", version: 2 }, { id: "rev-1", version: 1 }] });
    render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><TeamGraphWorkspace agents={[agent]} onBack={vi.fn()} mapMode /></SWRConfig>);
    fireEvent.click(screen.getByRole("button", { name: "ピンとグループを編集" }));
    await waitFor(() => expect(screen.getByTestId("canvas-revisions")).toHaveTextContent("rev-1"));
    fireEvent(window, new Event("focus"));
    await waitFor(() => expect(screen.getByTestId("canvas-revisions")).toHaveTextContent("rev-2"));
    expect(JSON.parse(screen.getByTestId("canvas-definition").textContent!).nodes[0].revision_id).toBe("rev-1");
  });
  it.each([null, "bound"]) ("launches with the correct target (%s)", async bound => {
    const saved = { ...graph, project_id: bound, definition: { ...emptyGraph(), root_key: "n", nodes: [{ key: "n", agent_id: "root", revision_id: null, role: "coordinator", x: 0, y: 0, title: null }], groups: [{ key: "team", title: "Team", purpose: "", node_keys: ["n"], leader_key: "n" }] } };
    mocks.get.mockResolvedValue({ graph: saved }); mount(); await screen.findByRole("option", { name: "Other" });
    fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } }); await screen.findByText("保存済み v1");
    fireEvent.change(screen.getByLabelText("依頼内容"), { target: { value: "目的" } }); fireEvent.change(screen.getByLabelText("依頼するグループ"), { target: { value: "team" } });
    if (!bound) expect(screen.getByText(/再利用チーム · 対象Project: project/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Rootに依頼" })); await waitFor(() => expect(mocks.dispatch).toHaveBeenCalledTimes(1));
    expect(mocks.bootstrap).toHaveBeenCalledWith(bound ?? "project");
    expect(mocks.dispatch.mock.calls[0][1]).toEqual(expect.objectContaining({ project_id: null, context_project_hint_id: bound ?? "project", include_project_context: true }));
  });
  it("requires a target for a reusable team", async () => {
    mocks.project = null; const saved = { ...graph, definition: { ...emptyGraph(), groups: [{ key: "team", title: "Team", purpose: "", node_keys: [], leader_key: null }] } };
    mocks.get.mockResolvedValue({ graph: saved }); mount(); await screen.findByRole("option", { name: "Other" });
    fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } }); await screen.findByText("保存済み v1");
    fireEvent.change(screen.getByLabelText("依頼内容"), { target: { value: "目的" } }); fireEvent.change(screen.getByLabelText("依頼するグループ"), { target: { value: "team" } });
    expect(screen.getByText(/対象Projectを選択/)).toBeVisible(); expect(screen.getByRole("button", { name: "Rootに依頼" })).toBeDisabled(); expect(mocks.bootstrap).not.toHaveBeenCalled(); expect(mocks.dispatch).not.toHaveBeenCalled();
  });
  it("isolates owner drafts and restores after logout", async () => {
    sessionStorage.setItem("aoitalk:graph-root-request:user", JSON.stringify({ graph, title: "A", definition: emptyGraph(), baseline: "baseline", goal: "A secret", groupKey: "team", clientMessageId: "A-key", requestSession: "A-session", requestUncertain: true }));
    const view = mount(); await waitFor(() => expect(screen.getByLabelText("依頼内容")).toHaveValue("A secret"));
    const change = (user: string | null) => { mocks.user = user; view.rerender(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><TeamGraphWorkspace agents={[]} onBack={vi.fn()} /></SWRConfig>); };
    change("B"); await waitFor(() => expect(screen.getByLabelText("依頼内容")).toHaveValue(""));
    expect(screen.getByLabelText("チーム名")).toHaveValue("新しい Agent Team"); expect(screen.queryByRole("button", { name: "送信状態を確認" })).toBeNull();
    await waitFor(() => expect(sessionStorage.getItem("aoitalk:graph-root-request:B")).not.toBeNull());
    expect(sessionStorage.getItem("aoitalk:graph-root-request:B")).not.toContain("A secret"); expect(sessionStorage.getItem("aoitalk:graph-root-request:B")).not.toContain("A-key");
    change(null); change("user"); await waitFor(() => expect(screen.getByLabelText("依頼内容")).toHaveValue("A secret"));
  });
  it("restores B and ignores stale A read", async () => {
    let resolveRead!: (value: { graph: typeof graph }) => void;
    mocks.get.mockReturnValueOnce(new Promise(resolve => { resolveRead = resolve; }));
    sessionStorage.setItem("aoitalk:graph-root-request:B", JSON.stringify({ graph: null, title: "B draft", definition: emptyGraph(), baseline: "baseline", goal: "B goal" }));
    const view = mount(); await screen.findByRole("option", { name: "Other" }); fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } });
    mocks.user = "B"; view.rerender(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><TeamGraphWorkspace agents={[]} onBack={vi.fn()} /></SWRConfig>);
    await waitFor(() => expect(screen.getByLabelText("依頼内容")).toHaveValue("B goal")); resolveRead({ graph: { ...graph, title: "A late" } });
    await waitFor(() => expect(screen.getByLabelText("チーム名")).toHaveValue("B draft"));
  });
  it("sends the saved revision once through the original root session", async () => {
    const saved = { ...graph, project_id: "project", definition: { ...emptyGraph(), root_key: "n", nodes: [{ key: "n", agent_id: "root", revision_id: null, role: "coordinator", x: 0, y: 0, title: null }], groups: [{ key: "team", title: "Team", purpose: "", node_keys: ["n"], leader_key: "n" }] } };
    mocks.get.mockResolvedValue({ graph: saved }); mount(); await screen.findByRole("option", { name: "Other" });
    fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } }); await screen.findByText("保存済み v1");
    fireEvent.change(screen.getByLabelText("依頼内容"), { target: { value: "目的" } }); fireEvent.change(screen.getByLabelText("依頼するグループ"), { target: { value: "team" } });
    fireEvent.change(screen.getByLabelText("チーム名"), { target: { value: "draft" } }); expect(screen.getByRole("button", { name: "Rootに依頼" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("チーム名"), { target: { value: graph.title } });
    fireEvent.click(screen.getByRole("button", { name: "Rootに依頼" })); fireEvent.click(screen.getByRole("button", { name: "Rootに依頼" }));
    await waitFor(() => expect(mocks.open).toHaveBeenCalledWith(true));
    expect(mocks.dispatch).toHaveBeenCalledTimes(1); expect(mocks.bootstrap).toHaveBeenCalledWith("project");
    expect(mocks.dispatch).toHaveBeenCalledWith("original", expect.objectContaining({ context_project_hint_id: "project", project_id: null, expected_agent_id: "root", message: expect.stringContaining('"revision_id":"r"') }));
  });
  it("blocks a mismatched root and keeps the goal", async () => {
    const saved = { ...graph, project_id: "project", definition: { ...emptyGraph(), root_key: "n", nodes: [{ key: "n", agent_id: "different", revision_id: null, role: "coordinator", x: 0, y: 0, title: null }], groups: [{ key: "team", title: "Team", purpose: "", node_keys: ["n"], leader_key: "n" }] } };
    mocks.get.mockResolvedValue({ graph: saved }); mount(); await screen.findByRole("option", { name: "Other" }); fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } }); await screen.findByText("保存済み v1");
    fireEvent.change(screen.getByLabelText("依頼内容"), { target: { value: "目的" } }); fireEvent.change(screen.getByLabelText("依頼するグループ"), { target: { value: "team" } });
    mocks.bootstrap.mockRejectedValueOnce(new Error("offline")); fireEvent.click(screen.getByRole("button", { name: "Rootに依頼" })); await screen.findByText("offline");
    fireEvent.click(screen.getByRole("button", { name: "Rootに依頼" })); await screen.findByText(/一致しません/); expect(mocks.dispatch).not.toHaveBeenCalled(); expect(screen.getByLabelText("依頼内容")).toHaveValue("目的");
  });
  it("does not resend after a lost dispatch response", async () => {
    const saved = { ...graph, project_id: null, definition: { ...emptyGraph(), root_key: "n", nodes: [{ key: "n", agent_id: "root", revision_id: null, role: "coordinator", x: 0, y: 0, title: null }], groups: [{ key: "team", title: "Team", purpose: "", node_keys: ["n"], leader_key: "n" }] } };
    mocks.get.mockResolvedValue({ graph: saved }); mocks.dispatch.mockRejectedValueOnce(new Error("lost")); const view = mount(); await screen.findByRole("option", { name: "Other" }); fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } }); await screen.findByText("保存済み v1");
    fireEvent.change(screen.getByLabelText("依頼内容"), { target: { value: "目的" } }); fireEvent.change(screen.getByLabelText("依頼するグループ"), { target: { value: "team" } }); fireEvent.click(screen.getByRole("button", { name: "Rootに依頼" }));
    await screen.findByText("lost"); expect(screen.queryByRole("button", { name: "未受付の依頼を編集" })).toBeNull(); expect(screen.getByRole("button", { name: "Rootに依頼" })).toBeDisabled(); expect(mocks.dispatch).toHaveBeenCalledTimes(1); expect(screen.getByLabelText("依頼内容")).toHaveValue("目的");
    mocks.project = "another"; view.rerender(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><TeamGraphWorkspace agents={[]} onBack={vi.fn()} /></SWRConfig>);
    expect(screen.getByRole("button", { name: "Rootに依頼" })).toBeDisabled(); expect(screen.getByText(/対象Project: project/)).toBeVisible();
    const sent = mocks.dispatch.mock.calls[0][1].client_message_id;
    view.unmount(); mount(); await screen.findByText(/送信結果を確認できません。再送せず状態/);
    mocks.messages.mockResolvedValue({ messages: [{ client_message_id: sent }] });
    fireEvent.click(screen.getByRole("button", { name: "送信状態を確認" }));
    await waitFor(() => expect(mocks.open).toHaveBeenCalledWith(true));
    expect(mocks.dispatch).toHaveBeenCalledTimes(1);
  });
  it("retries a rejected busy request with the same snapshot and key", async () => {
    const saved = { ...graph, project_id: null, definition: { ...emptyGraph(), root_key: "n", nodes: [{ key: "n", agent_id: "root", revision_id: null, role: "coordinator", x: 0, y: 0, title: null }], groups: [{ key: "team", title: "Team", purpose: "", node_keys: ["n"], leader_key: "n" }] } };
    mocks.get.mockResolvedValue({ graph: saved }); mocks.dispatch.mockRejectedValueOnce(new AoiDotsApiError("busy", 409, "interactive_session_busy"));
    const view = mount(); await screen.findByRole("option", { name: "Other" }); fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } }); await screen.findByText("保存済み v1");
    fireEvent.change(screen.getByLabelText("依頼内容"), { target: { value: "目的" } }); fireEvent.change(screen.getByLabelText("依頼するグループ"), { target: { value: "team" } });
    fireEvent.click(screen.getByRole("button", { name: "Rootに依頼" })); await screen.findByText(/受け付けられていません/);
    mocks.project = "another"; view.rerender(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><TeamGraphWorkspace agents={[]} onBack={vi.fn()} /></SWRConfig>);
    expect(screen.getByText(/対象Project: project/)).toBeVisible();
    expect(screen.getByLabelText("依頼内容")).toHaveValue("目的");
    fireEvent.click(screen.getByRole("button", { name: "Rootに依頼" }));
    await waitFor(() => expect(mocks.open).toHaveBeenCalledTimes(1));
    expect(mocks.dispatch).toHaveBeenCalledTimes(2); expect(mocks.dispatch.mock.calls[1]).toEqual(mocks.dispatch.mock.calls[0]); expect(mocks.bootstrap).toHaveBeenLastCalledWith("project");
  });
  it.each([false, true])("edits a rejected request and sends a new key (switch graph: %s)", async switchGraph => {
    const saved = { ...graph, project_id: "project", definition: { ...emptyGraph(), root_key: "n", nodes: [{ key: "n", agent_id: "root", revision_id: null, role: "coordinator", x: 0, y: 0, title: null }], groups: [{ key: "team", title: "Team", purpose: "", node_keys: ["n"], leader_key: "n" }] } };
    mocks.get.mockResolvedValue({ graph: saved }); mocks.dispatch.mockRejectedValueOnce(new AoiDotsApiError("busy", 409, "interactive_session_busy"));
    mount(); await screen.findByRole("option", { name: "Other" }); fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } }); await screen.findByText("保存済み v1");
    fireEvent.change(screen.getByLabelText("依頼内容"), { target: { value: "目的" } }); fireEvent.change(screen.getByLabelText("依頼するグループ"), { target: { value: "team" } });
    fireEvent.click(screen.getByRole("button", { name: "Rootに依頼" })); await screen.findByText(/受け付けられていません/);
    if (switchGraph) {
      vi.spyOn(window, "confirm").mockReturnValueOnce(true);
      mocks.get.mockResolvedValueOnce({ graph: { ...saved, id: "other", title: "Other", revision_id: "other-revision" } });
      fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "other" } }); await waitFor(() => expect(screen.getByLabelText("チーム名")).toHaveValue("Other"));
      expect(screen.getByRole("button", { name: "Rootに依頼" })).toBeDisabled();
    }
    fireEvent.click(screen.getByRole("button", { name: "未受付の依頼を編集" }));
    expect(screen.getByLabelText("依頼内容")).toHaveValue("目的");
    fireEvent.change(screen.getByLabelText("依頼内容"), { target: { value: "変更した目的" } });
    fireEvent.click(screen.getByRole("button", { name: "Rootに依頼" })); await waitFor(() => expect(mocks.dispatch).toHaveBeenCalledTimes(2));
    expect(mocks.dispatch.mock.calls[1][1].client_message_id).not.toBe(mocks.dispatch.mock.calls[0][1].client_message_id);
    expect(mocks.dispatch.mock.calls[1][1].message).toContain("変更した目的");
    expect(mocks.dispatch.mock.calls[1][1].message).toContain(switchGraph ? '"revision_id":"other-revision"' : '"revision_id":"r"');
    expect(mocks.uncertain).toHaveBeenCalledTimes(2); expect(mocks.messages).not.toHaveBeenCalled();
  });
  it("keeps an unknown outcome when history has no matching message", async () => {
    const saved = { ...graph, project_id: "project", definition: { ...emptyGraph(), root_key: "n", nodes: [{ key: "n", agent_id: "root", revision_id: null, role: "coordinator", x: 0, y: 0, title: null }], groups: [{ key: "team", title: "Team", purpose: "", node_keys: ["n"], leader_key: "n" }] } };
    mocks.get.mockResolvedValue({ graph: saved }); mocks.dispatch.mockRejectedValueOnce(new Error("lost")); mocks.messages.mockResolvedValue({ messages: [] });
    mount(); await screen.findByRole("option", { name: "Other" }); fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } }); await screen.findByText("保存済み v1");
    fireEvent.change(screen.getByLabelText("依頼内容"), { target: { value: "目的" } }); fireEvent.change(screen.getByLabelText("依頼するグループ"), { target: { value: "team" } }); fireEvent.click(screen.getByRole("button", { name: "Rootに依頼" })); await screen.findByText("lost");
    fireEvent.click(screen.getByRole("button", { name: "送信状態を確認" })); await screen.findByText(/まだ確認できません/);
    expect(mocks.dispatch).toHaveBeenCalledTimes(1); expect(mocks.open).not.toHaveBeenCalled(); expect(screen.getByLabelText("依頼内容")).toHaveValue("目的");
  });
  it("does not navigate or erase the request draft when an old send finishes after back navigation", async () => {
    const saved = { ...graph, project_id: "project", definition: { ...emptyGraph(), root_key: "n", nodes: [{ key: "n", agent_id: "root", revision_id: null, role: "coordinator", x: 0, y: 0, title: null }], groups: [{ key: "team", title: "Team", purpose: "", node_keys: ["n"], leader_key: "n" }] } };
    let resolveSend!: (result: { success: boolean }) => void;
    mocks.get.mockResolvedValue({ graph: saved }); mocks.dispatch.mockReturnValueOnce(new Promise(resolve => { resolveSend = resolve; }));
    mount(); await screen.findByRole("option", { name: "Other" }); fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } }); await screen.findByText("保存済み v1");
    fireEvent.change(screen.getByLabelText("依頼内容"), { target: { value: "目的" } }); fireEvent.change(screen.getByLabelText("依頼するグループ"), { target: { value: "team" } }); fireEvent.click(screen.getByRole("button", { name: "Rootに依頼" }));
    await waitFor(() => expect(mocks.dispatch).toHaveBeenCalledTimes(1)); fireEvent(window, new PopStateEvent("popstate")); resolveSend({ success: true });
    await waitFor(() => expect(screen.getByLabelText("依頼内容")).toHaveValue("目的")); expect(mocks.open).not.toHaveBeenCalled(); expect(screen.getByRole("button", { name: "送信状態を確認" })).toBeVisible();
  });
  it("saves and reloads new positions alongside existing pins and connectors", async () => {
    mocks.realCanvas = true;
    const definition: GraphDefinition = { ...emptyGraph(), root_key: "old", nodes: [
      { key: "old", agent_id: "agent-a", revision_id: "rev-1", role: "coordinator", x: -120, y: -80, title: null },
      { key: "custom", agent_id: "agent-a", revision_id: "rev-2", role: "worker", x: 130, y: 100, title: null },
    ], edges: [{ key: "edge", source: "old", target: "custom", kind: "handoff", output_key: "result" }] };
    mocks.get.mockResolvedValue({ graph: { ...graph, definition } });
    mocks.update.mockImplementationOnce(async (_id: string, body: { definition: GraphDefinition }) => {
      const saved = { ...graph, version: 2, definition: body.definition };
      mocks.get.mockResolvedValue({ graph: saved });
      return { graph: saved };
    });
    mount();
    await screen.findByRole("option", { name: "Other" });
    fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } });
    await screen.findByText("保存済み v1");
    fireEvent.change(screen.getByLabelText("追加する Agent"), { target: { value: "agent-a" } });
    for (let i = 0; i < 3; i++) fireEvent.click(screen.getByRole("button", { name: "ノードを追加" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByText("保存済み v2");
    const saved = mocks.update.mock.calls[0][1].definition as GraphDefinition;
    expect(saved.nodes.slice(0, 2)).toEqual(definition.nodes);
    expect(saved.edges).toEqual(definition.edges);
    expect(saved.root_key).toBe(definition.root_key);
    fireEvent.click(screen.getByRole("button", { name: "再読込" }));
    await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(JSON.parse(screen.getByTestId("flow-nodes").textContent!).map((node: { position: unknown }) => node.position)).toEqual(saved.nodes.map(({ x, y }) => ({ x, y }))));
  });
  it("creates, reloads and recovers a software group through the canvas", async () => {
    mocks.realCanvas = true;
    mocks.create.mockImplementationOnce(async (body: { definition: GraphDefinition }) => {
      const saved = { ...graph, definition: body.definition };
      mocks.get.mockResolvedValue({ graph: saved });
      return { graph: saved };
    });
    mount();
    fireEvent.change(screen.getByLabelText("追加する Agent"), { target: { value: "agent-a" } });
    fireEvent.click(screen.getByRole("button", { name: "ノードを追加" }));
    fireEvent.click(screen.getByRole("button", { name: "全ノード選択" }));
    fireEvent.click(screen.getByRole("button", { name: "グループ化 (1)" }));
    expect(screen.getByRole("option", { name: "画像制作" })).toHaveValue("media_image");
    expect(screen.getByRole("option", { name: "動画制作" })).toHaveValue("media_video");
    fireEvent.change(screen.getByLabelText("実行種別"), { target: { value: "software_code" } });
    fireEvent.change(screen.getByLabelText("名前"), { target: { value: "Code team" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByText("保存済み v1");
    const definition = mocks.create.mock.calls[0][0].definition as GraphDefinition;
    expect(definition.groups[0]).toMatchObject({ title: "Code team", execution_kind: "software_code" });
    const saved = { ...graph, definition };
    expect(screen.getByLabelText("実行種別")).toHaveValue("software_code");
    fireEvent.click(screen.getByRole("button", { name: "再読込" }));
    await waitFor(() => expect(mocks.get).toHaveBeenCalledWith("g"));
    fireEvent.click(screen.getByRole("button", { name: "Code team · 1人" }));
    expect(screen.getByLabelText("実行種別")).toHaveValue("software_code");
    fireEvent.change(screen.getByLabelText("目的"), { target: { value: "実装" } });
    const updated = { ...saved, version: 2, revision_id: "r2", definition: { ...definition, groups: [{ ...definition.groups[0], purpose: "実装" }] } };
    mocks.update.mockRejectedValueOnce(new Error("lost"));
    mocks.get.mockResolvedValueOnce({ graph: updated });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByText("保存済み v2");
    expect(mocks.update).toHaveBeenCalledWith("g", expect.objectContaining({ expected_revision: 1, definition: updated.definition }));
    expect(screen.getByLabelText("実行種別")).toHaveValue("software_code");
    fireEvent.change(screen.getByLabelText("目的"), { target: { value: "再編集" } });
    mocks.update.mockRejectedValueOnce(new Error("unsupported"));
    mocks.get.mockResolvedValueOnce({ graph: updated });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("保存結果を確認できません");
    expect(screen.getByText("未保存")).toBeVisible();
    expect(screen.getByLabelText("実行種別")).toHaveValue("software_code");
  });
  it("saves the selected kind with the revision CAS and reloads it", async () => {
    const group = { key: "team", title: "Team", purpose: "", node_keys: [], leader_key: null };
    const saved = { ...graph, definition: { ...emptyGraph(), groups: [group] } };
    const updated = { ...saved, version: 2, revision_id: "r2", definition: { ...emptyGraph(), groups: [{ ...group, execution_kind: "media_image" as const }] } };
    mocks.get.mockResolvedValue({ graph: saved }); mocks.update.mockResolvedValue({ graph: updated });
    mount(); await screen.findByRole("option", { name: "Other" });
    fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } }); await screen.findByText("保存済み v1");
    fireEvent.click(screen.getByRole("button", { name: "画像に変更" })); fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByText("保存済み v2");
    expect(mocks.update).toHaveBeenCalledWith("g", expect.objectContaining({ expected_revision: 1, definition: updated.definition }));
    mocks.get.mockResolvedValue({ graph: updated });
    fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } });
    await waitFor(() => expect(JSON.parse(screen.getByTestId("canvas-definition").textContent!).groups[0].execution_kind).toBe("media_image"));
  });
  it("locks repeated saves until creation and readback finish", async () => {
    let resolveCreate!: (result: { graph: typeof graph }) => void;
    mocks.create.mockReturnValue(new Promise(resolve => { resolveCreate = resolve; }));
    mount();
    const save = screen.getByRole("button", { name: "保存" });
    fireEvent.click(save); fireEvent.click(save);
    expect(mocks.create).toHaveBeenCalledTimes(1);
    resolveCreate({ graph });
    await waitFor(() => expect(screen.getByText("保存済み v1")).toBeVisible());
    expect(mocks.get).toHaveBeenCalledWith("g");
  });
  it("identifies the agent in the map settings heading", async () => {
    render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><TeamGraphWorkspace agents={[]} onBack={vi.fn()} mapMode /></SWRConfig>);
    fireEvent.click(screen.getByRole("button", { name: "Agent設定" }));
    expect(await screen.findByRole("heading", { name: "Agent・モデル設定 [agent-a]" })).toBeVisible();
  });
  it("opens the canonical agent settings", () => {
    mount(); fireEvent.click(screen.getByRole("button", { name: "Agent設定" }));
    expect(mocks.push).toHaveBeenCalledWith("/operations?tab=agents&agent=agent-a&panel=role");
  });
  it("edits an agent in the map, explicitly pins the new revision, and saves and reloads it", async () => {
    mocks.realCanvas = true;
    const agentId = "11111111-1111-4111-8111-111111111111";
    const oldId = "22222222-2222-4222-8222-222222222222";
    const newId = "33333333-3333-4333-8333-333333333333";
    const base = { id: oldId, agent_id: agentId, version: 1, display_name: "AoiDots", mission: "", responsibility_summary: "", operational_instructions: "", capability_ceiling: [], budget_policy: {}, concurrency_policy: {}, model_overrides: {}, tool_ids: [], delegate_policy: { agents: [], workers: [] } };
    const definition: GraphDefinition = { ...emptyGraph(), root_key: "n", nodes: [{ key: "n", agent_id: agentId, revision_id: oldId, role: "coordinator", x: 1, y: 2, title: null }] };
    mocks.revisions.mockResolvedValue({ revisions: [base] });
    mocks.agent.mockResolvedValue({ agent: { id: agentId, display_name: "AoiDots", slug: "synthetic", state: "active" } });
    mocks.get.mockResolvedValue({ graph: { ...graph, definition } });
    mocks.revision.mockImplementation(async (_id: string, body: typeof base) => ({ revision: { ...body, id: newId, agent_id: agentId } }));
    render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><TeamGraphWorkspace agents={[{ id: agentId, display_name: "AoiDots", slug: "synthetic", state: "active" }]} onBack={vi.fn()} mapMode /></SWRConfig>);
    await screen.findByRole("option", { name: "Other" });
    fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } });
    await screen.findByText("保存済み v1");
    fireEvent.change(screen.getByLabelText("チーム名"), { target: { value: "Draft" } });
    fireEvent.click(screen.getByRole("button", { name: "全ノード選択" }));
    fireEvent.click(screen.getByRole("button", { name: "Agent・モデル設定を開く" }));
    await screen.findByLabelText("業務指示");
    expect(screen.getByRole("heading", { name: "Agent・モデル設定 [11111111]" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.getByLabelText("チーム名")).toHaveValue("Draft");
    fireEvent.click(screen.getByRole("button", { name: "Agent・モデル設定を開く" }));
    fireEvent.change(await screen.findByLabelText("業務指示"), { target: { value: "synthetic prompt" } });
    fireEvent.change(await screen.findByLabelText("会話に候補を追加"), { target: { value: "llm:synthetic/model" } });
    fireEvent.click(await screen.findByRole("checkbox", { name: "Synthetic tool" }));
    mocks.revision.mockRejectedValueOnce(new EmployeeApiError(409));
    fireEvent.click(screen.getByRole("button", { name: /新しい職務 revision v2/ }));
    await screen.findByRole("alert");
    expect(screen.getByLabelText("業務指示")).toHaveValue("synthetic prompt");
    fireEvent.click(screen.getByRole("button", { name: /新しい職務 revision v2/ }));
    await screen.findByRole("button", { name: "このノードに v2 を固定" });
    expect(mocks.revision.mock.calls[1][1]).toMatchObject({ operational_instructions: "synthetic prompt", tool_ids: ["synthetic-tool"], model_overrides: { chat: { routes: [{ kind: "llm", provider: "synthetic", model: "model" }] } } });
    expect(screen.getByLabelText("チーム名")).toHaveValue("Draft");
    fireEvent.click(screen.getByRole("button", { name: "このノードに v2 を固定" }));
    expect(screen.getByLabelText("Agentの版")).toHaveValue(newId);
    mocks.update.mockImplementationOnce(async (_id: string, body: { definition: GraphDefinition; title: string }) => { const saved = { ...graph, title: body.title, definition: body.definition, version: 2 }; mocks.get.mockResolvedValue({ graph: saved }); return { graph: saved }; });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByText("保存済み v2");
    expect(mocks.update.mock.calls[0][1].definition.nodes[0]).toMatchObject({ agent_id: agentId, role: "coordinator", revision_id: newId });
    fireEvent.click(screen.getByRole("button", { name: "再読込" }));
    await waitFor(() => expect(screen.getByLabelText("Agentの版")).toHaveValue(newId));
  });
  async function editExisting() {
    mount();
    await screen.findByRole("option", { name: "Other" });
    fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } });
    await screen.findByText("保存済み v1");
    fireEvent.change(screen.getByLabelText("チーム名"), { target: { value: "変更後" } });
  }
  it("accepts intended server state after a lost update response", async () => {
    await editExisting();
    mocks.update.mockRejectedValueOnce(new Error("lost"));
    mocks.get.mockResolvedValueOnce({ graph: { ...graph, title: "変更後", version: 2 } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByText("保存済み v2");
    expect(mocks.update).toHaveBeenCalledTimes(1);
  });
  it("preserves the draft when readback is unchanged", async () => {
    await editExisting(); mocks.update.mockRejectedValueOnce(new Error("lost"));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("保存結果を確認できません");
    expect(screen.getByLabelText("チーム名")).toHaveValue("変更後");
    expect(screen.getByText("未保存")).toBeVisible();
  });
  it("preserves the draft on advanced conflict", async () => {
    await editExisting(); mocks.update.mockRejectedValueOnce(new Error("lost"));
    mocks.get.mockResolvedValueOnce({ graph: { ...graph, version: 2, title: "別の変更" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("競合");
    expect(screen.getByLabelText("チーム名")).toHaveValue("変更後");
  });
  it("preserves the draft when readback fails", async () => {
    await editExisting(); mocks.update.mockRejectedValueOnce(new Error("lost")); mocks.get.mockRejectedValueOnce(new Error("offline"));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("保存結果を確認できません");
    expect(screen.getByText("未保存")).toBeVisible();
  });
  it("ignores an old response after history navigation", async () => {
    await editExisting(); let rejectUpdate!: (error: Error) => void;
    mocks.update.mockReturnValueOnce(new Promise((_, reject) => { rejectUpdate = reject; }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    fireEvent(window, new PopStateEvent("popstate"));
    rejectUpdate(new Error("lost"));
    mocks.get.mockResolvedValueOnce({ graph: { ...graph, title: "変更後", version: 2 } });
    await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText("チーム名")).toHaveValue("変更後");
    expect(screen.getByText("未保存")).toBeVisible();
  });
  it("recovers an uncertain create using the exact same key and body", async () => {
    mocks.create.mockRejectedValueOnce(new Error("lost"));
    mount(); fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByRole("button", { name: "保存状態を確認" });
    const sent = mocks.create.mock.calls[0][0];
    mocks.create.mockResolvedValueOnce({ graph });
    fireEvent.click(screen.getByRole("button", { name: "保存状態を確認" }));
    await screen.findByText("保存済み v1");
    expect(mocks.create).toHaveBeenCalledTimes(2);
    expect(mocks.create.mock.calls[1][0]).toEqual(sent);
    expect(JSON.parse(sessionStorage.getItem("aoitalk:graph-root-request:user")!).intent).toBeNull();
  });
  it("checks an interrupted update by readback after remount", async () => {
    let resolveUpdate!: (result: { graph: typeof graph }) => void;
    mocks.update.mockReturnValueOnce(new Promise(resolve => { resolveUpdate = resolve; }));
    const view = mount(); await screen.findByRole("option", { name: "Other" });
    fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "g" } }); await screen.findByText("保存済み v1");
    fireEvent.change(screen.getByLabelText("チーム名"), { target: { value: "変更後" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(mocks.update).toHaveBeenCalledTimes(1));
    const request = mocks.update.mock.calls[0][1];
    expect(JSON.parse(sessionStorage.getItem("aoitalk:graph-root-request:user")!).updateIntent.request).toEqual(request);
    fireEvent(window, new PopStateEvent("popstate")); view.unmount();
    resolveUpdate({ graph: { ...graph, title: "変更後", version: 2 } });
    mocks.get.mockResolvedValue({ graph: { ...graph, title: "変更後", version: 2 } });
    mount(); await screen.findByRole("button", { name: "保存状態を確認" });
    expect(screen.getByLabelText("チーム名")).toHaveValue("変更後");
    fireEvent.click(screen.getByRole("button", { name: "保存状態を確認" }));
    await screen.findByText("保存済み v2");
    expect(mocks.update).toHaveBeenCalledTimes(1);
  });

  it("shows conflict when a 409 readback fails", async () => {
    await editExisting(); mocks.update.mockRejectedValueOnce(new EmployeeApiError(409)); mocks.get.mockRejectedValueOnce(new Error("offline"));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("競合");
    expect(screen.getByText("未保存")).toBeVisible();
  });
  it("does not replace a newer selection with an old save response", async () => {
    await editExisting(); let rejectUpdate!: (error: Error) => void;
    mocks.update.mockReturnValueOnce(new Promise((_, reject) => { rejectUpdate = reject; }));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    fireEvent(window, new PopStateEvent("popstate"));
    mocks.get.mockResolvedValueOnce({ graph: { ...graph, id: "other", title: "Other" } });
    fireEvent.change(screen.getByLabelText("保存済みチーム"), { target: { value: "other" } });
    await waitFor(() => expect(screen.getByLabelText("チーム名")).toHaveValue("Other"));
    mocks.get.mockResolvedValueOnce({ graph: { ...graph, title: "変更後", version: 2 } });
    rejectUpdate(new Error("lost"));
    await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(3));
    expect(screen.getByLabelText("チーム名")).toHaveValue("Other");
    vi.restoreAllMocks();
  });

  it("locks repeated update clicks", async () => {
    await editExisting(); let resolveUpdate!: (result: { graph: typeof graph }) => void;
    mocks.update.mockReturnValueOnce(new Promise(resolve => { resolveUpdate = resolve; }));
    const save = screen.getByRole("button", { name: "保存" });
    fireEvent.click(save); fireEvent.click(save);
    expect(mocks.update).toHaveBeenCalledTimes(1);
    resolveUpdate({ graph: { ...graph, title: "変更後", version: 2 } });
    await screen.findByText("保存済み v2");
  });

});
