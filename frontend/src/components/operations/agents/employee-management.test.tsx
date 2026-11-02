// @vitest-environment jsdom

import { fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EmployeeWorkspace, employeeHref } from "./employee-workspace";
import { EmployeeRole } from "./employee-role";
import { EmployeePolicies } from "./employee-policies";
import { EmployeeAssignments } from "./employee-assignments";
import { EmployeeLiveInfo, type EmployeeObservability } from "./employee-observability";
import { EmployeeApiError, employeeRequest, type EmployeeCatalog, type EmployeeRevision } from "@/lib/agent-employees-api";

const mocks = vi.hoisted(() => ({ push: vi.fn(), query: new URLSearchParams(), enabled: true, manage: true, characters: vi.fn(), fetch: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: mocks.push }), useSearchParams: () => mocks.query }));
vi.mock("@/contexts/runtime-context", () => ({ useOptionalRuntimeContext: () => ({ runtimeFeatures: { application_features: { virtual_company: mocks.enabled, autonomous_agent_runtime: mocks.enabled } } }) }));
vi.mock("../operations-command-center", () => ({ OperationsCommandCenter: ({ agentId }: { agentId?: string }) => <p>読み取り投影 {agentId}</p> }));
vi.mock("@/lib/media-operations-api", () => ({ mediaOperationsApi: { listCharacters: mocks.characters, listPersonas: async () => [] } }));
vi.mock("@/lib/operations-api", () => ({ operationsApi: { listConnections: async () => [{ id: "conn", display_name: "備品接続", provider_key: "procurement" }, { id: "phone-conn", display_name: "受付接続", provider_key: "openai_realtime_sip" }, { id: "cw-conn", display_name: "CrowdWorks", provider_key: "crowdworks" }] } }));

const employee = { id: "agent", display_name: "備品担当", slug: "supplies", state: "draft" as const };
const catalog: EmployeeCatalog = { can_manage: true, capabilities: [{ id: "external_action_propose", family: "action", access: "write", native: false }] };
const revision: EmployeeRevision = { id: "rev1", agent_id: "agent", version: 1, display_name: "備品担当", mission: "在庫を管理する", responsibility_summary: "備品補充", operational_instructions: "確認する", legacy: { agent_team_id: "employee", execution_profile_id: "manual", allowed_subagent_ids: ["operator"] }, capability_ceiling: ["external_action_propose"], budget_policy: {}, concurrency_policy: { max_parallel_runs: 1 } };
const definition = { action_type: "procurement.place_order", display_name: "水を注文", status: "unavailable", category: "procurement", required_capabilities: ["external_action_propose"], connection_provider_keys: ["procurement"], constraints_schema: {} };
type RequestRecord = { path: string; method: string; body?: Record<string, unknown> };
let requests: RequestRecord[];
let policyRows: object[];
let ruleRows: object[];
let revisionRows: EmployeeRevision[];
let assignmentRows: object[];
let currentEmployee: { id: string; display_name: string; slug: string; state: string; character_id?: string | null };
const CHARACTER_ID = "7d0c2f3e-5b1a-4c8e-9a64-1f2e3d4c5b6a";
let characterRows: object[];
let fail: ((request: RequestRecord) => number | undefined) | null;
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
async function backend(url: string, init?: RequestInit) {
  const path = url.replace("/api/python-proxy", "/api"); const method = init?.method ?? "GET";
  const body = init?.body ? JSON.parse(String(init.body)) : undefined;
  const request = { path, method, body }; requests.push(request);
  const status = fail?.(request); if (status) return response({ detail: "echoed-secret-must-never-render" }, status);
  if (path === "/api/agents/catalog") return response({ ...catalog, can_manage: mocks.manage });
  if (path === "/api/llm/models") return response({ current: {}, providers: [{ id: "openai", label: "OpenAI", models: [{ id: "gpt-5.6-luna", label: "Luna" }, { id: "gpt-5.6-sol", label: "Sol" }] }, { id: "codex-cli", label: "Codex CLI", models: [{ id: "gpt-6.1-sol", label: "GPT-6.1 Sol", reasoning_effort_options: ["low", "medium", "high", "xhigh", "max", "ultra"] }, { id: "gpt-6-luna", label: "GPT-6 Luna", reasoning_effort_options: ["low", "medium", "high", "xhigh", "max"] }] }] });
  if (path === "/api/free-team/settings") return response({ profile: { pools: { coordinator: {} } } });
  if (path === "/api/agent-tools/catalog") return response({ tools: [
    { id: "external_action_propose", label: "外部操作の提案", group: "操作", requires_capabilities: ["external_action_propose"] },
    { id: "delegate", label: "Agent に委任", group: "委任", requires_capabilities: [] },
    { id: "delegate_worker:codex", label: "Codex に委任", group: "委任", requires_capabilities: ["remote_agent_delegate"] },
  ] });
  if (path === "/api/operations/command-center") return response({ summary: { working: 0, awaiting_approval: 0, uncertain: 0 }, agents: [] });
  if (path === "/api/characters/manage") return response({ success: true, characters: characterRows });
  if (path === "/api/agents" && method === "POST") { currentEmployee = { ...currentEmployee, ...(body?.character_id ? { character_id: String(body.character_id) } : {}) }; return response({ agent: currentEmployee }); }
  if (path === "/api/agents") return response({ agents: [currentEmployee] });
  if (path === "/api/agents/agent") return response({ agent: currentEmployee });
  if (path.endsWith("/organization-profile")) return response({ organization_profile: { job_title: "備品", employment_state: "active", autonomy_level: "supervised" } });
  if (path === "/api/agents/agent/revisions") return response(method === "POST" ? { revision: { ...revision, ...body } } : { revisions: revisionRows });
  if (path.includes("effective-authority")) return response({ authority: { allowed: currentEmployee.state === "active", reason_code: currentEmployee.state === "active" ? "allowed" : "agent_inactive" } });
  if (path === "/api/agents/agent/state") { currentEmployee = { ...currentEmployee, state: String(body?.state) }; return response({ agent: currentEmployee }); }
  if (path === "/api/spaces") return response({ spaces: [{ id: "space", name: "本社" }] });
  if (path === "/api/projects") return response({ projects: [{ id: "project", name: "備品 Project" }] });
  if (path === "/api/tasks?project_id=project") return response([{ id: "task", title: "在庫確認", project_id: "project" }]);
  if (path.includes("task-assignments")) return response({ task_assignments: assignmentRows });
  if (path.includes("space-assignments")) return response({ space_assignments: assignmentRows });
  if (path.includes("project-grants")) return response({ project_grants: assignmentRows });
  if (path.startsWith("/api/agent-action-policies?")) return response({ policies: policyRows });
  if (path === "/api/agent-action-policies") return response({ policy: { id: "policy", display_name: body?.display_name, version: 1, state: "draft", current_revision: null } });
  if (path === "/api/agent-action-policies/policy" && method === "PATCH") return response({ policy: { id: "policy", display_name: body?.display_name, version: 2, state: "draft", current_revision: null } });
  if (path === "/api/agent-action-policies/policy/revisions") return response({ policy: { id: "policy", version: 2 } });
  if (path === "/api/integrations/actions") return response({ actions: [definition, { action_type: "engagement.submit_application", display_name: "CrowdWorksへ応募", status: "automatable", category: "engagement", required_capabilities: ["external_action_propose"], connection_provider_keys: ["crowdworks"], constraints_schema: {} }] });
  if (path.startsWith("/api/agent-automation/rules?")) return response({ rules: ruleRows });
  if (path === "/api/agent-automation/rules") return response({ rule: { id: "rule", display_name: body?.display_name, version: 1 } });
  if (path === "/api/agent-automation/rules/rule/revisions") { ruleRows = [{ id: "rule", display_name: "水がない報告", state: "draft", version: 2, current_revision: { id: "rulerev", version: 1, ...body } }]; return response({ rule: ruleRows[0] }); }
  if (path === "/api/agent-automation/rules/rule/test") return response({ evaluation: { matched: true, dry_run: true } });
  if (path === "/api/agent-automation/rules/rule") return response({ rule: method === "PATCH" ? { id: "rule", display_name: body?.display_name, version: 2, state: "draft", current_revision: null } : ruleRows[0] });
  throw new Error(`Unexpected request ${method} ${path}`);
}
function mount(node: React.ReactNode) { return render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0, shouldRetryOnError: false }}>{node}</SWRConfig>); }
async function select(user: ReturnType<typeof userEvent.setup>, label: string, option: string) {
  await user.click(screen.getByRole("combobox", { name: label }));
  await user.click(await screen.findByRole("option", { name: option }));
}
async function openHelp(user: ReturnType<typeof userEvent.setup>, label: string) {
  await user.click(screen.getByRole("button", { name: `${label}の説明` }));
}
beforeEach(() => {
  vi.restoreAllMocks(); mocks.push.mockReset(); mocks.query = new URLSearchParams("tab=agents"); mocks.enabled = true; mocks.manage = true;
  mocks.characters.mockResolvedValue([]);
  characterRows = [{ id: CHARACTER_ID, name: "葵", slug: "aoi", is_enabled: true, system_prompt: "x" }, { id: "off-char", name: "休止中", is_enabled: false }];
  requests = []; policyRows = []; ruleRows = []; revisionRows = [revision]; assignmentRows = []; fail = null; currentEmployee = { ...employee };
  mocks.fetch.mockImplementation(backend); vi.stubGlobal("fetch", mocks.fetch);
});

describe("Agent管理と API 境界", () => {
  it("requires both runtime flags, describes startup and Enterprise denial, and sends no management requests", () => {
    mocks.enabled = false; mount(<EmployeeWorkspace />);
    expect(screen.getByText("Agent機能はこの実行環境で無効です")).toBeVisible();
    expect(screen.getByText(/FEATURE_VIRTUAL_COMPANY=true/)).toHaveTextContent("FEATURE_AUTONOMOUS_AGENT_RUNTIME=true");
    expect(screen.getByText(/Enterprise profile/)).toHaveTextContent("制限を解除できません");
    expect(screen.queryByRole("button", { name: /追加|有効化/ })).not.toBeInTheDocument(); expect(mocks.fetch).not.toHaveBeenCalled();
  });
  it("offers administrator create and resumes a persisted draft using URL selection", async () => {
    const user = userEvent.setup(); mount(<EmployeeWorkspace />);
    expect(await screen.findByRole("button", { name: "＋ Agentを追加" })).toBeVisible();
    await user.click(await screen.findByRole("button", { name: /備品担当.*下書きの設定を再開/ }));
    expect(mocks.push).toHaveBeenCalledWith("/operations?tab=agents&agent=agent&panel=overview", { scroll: false });
  });
  it("lists conversation Characters, POSTs the selected Character UUID (never a Media Persona) and reads it back", async () => {
    mocks.characters.mockResolvedValue([{ id: "media-persona-1", current_revision: { display_name: "発信用Persona" } }]);
    const user = userEvent.setup(); mount(<EmployeeWorkspace />);
    await user.click(await screen.findByRole("button", { name: "＋ Agentを追加" }));
    await user.type(screen.getByLabelText("Agent名"), "制作担当");
    await user.click(screen.getByRole("combobox", { name: "Character（会話人格・任意）" }));
    expect(await screen.findByRole("option", { name: "葵" })).toBeVisible();
    expect(screen.queryByRole("option", { name: "発信用Persona" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "休止中" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "葵" }));
    await select(user, "雇用状態", "業務委託");
    await user.click(screen.getByRole("button", { name: "下書きを保存して職務設定へ" }));
    await waitFor(() => expect(mocks.push).toHaveBeenCalledWith(employeeHref("agent", "role"), { scroll: false }));
    expect(requests.find(r => r.path === "/api/agents" && r.method === "POST")?.body).toMatchObject({ display_name: "制作担当", character_id: CHARACTER_ID });
    expect(requests.some(r => r.path === "/api/characters/manage")).toBe(true);
    expect(mocks.characters).not.toHaveBeenCalled();
    expect(requests.find(r => r.path.endsWith("organization-profile") && r.method === "PATCH")?.body).toMatchObject({ employment_state: "contractor" });
    expect(requests.some(r => r.path.endsWith("/state") && r.method === "PATCH")).toBe(false);
  });
  it("allows creating an Agent without a Character and omits character_id", async () => {
    const user = userEvent.setup(); mount(<EmployeeWorkspace />);
    await user.click(await screen.findByRole("button", { name: "＋ Agentを追加" }));
    await user.type(screen.getByLabelText("Agent名"), "未指定担当");
    await user.click(screen.getByRole("button", { name: "下書きを保存して職務設定へ" }));
    await waitFor(() => expect(mocks.push).toHaveBeenCalledWith(employeeHref("agent", "role"), { scroll: false }));
    expect(requests.find(r => r.path === "/api/agents" && r.method === "POST")?.body).not.toHaveProperty("character_id");
  });
  it.each([
    [CHARACTER_ID, "葵"], [null, "未選択"], ["media-persona-1", "未解決"],
  ])("shows the saved character_id %s as %s without rewriting it", async (id, text) => {
    currentEmployee = { ...employee, character_id: id }; mocks.query = new URLSearchParams("tab=agents&agent=agent&panel=overview");
    mount(<EmployeeWorkspace />);
    expect(await screen.findByTestId("employee-character")).toHaveTextContent(text);
    expect(requests.some(r => r.path.startsWith("/api/agents") && r.method !== "GET")).toBe(false);
  });
  it("hides administrator writes when the catalog cannot manage", async () => {
    mocks.manage = false; mount(<EmployeeWorkspace />); await screen.findByRole("button", { name: /備品担当/ });
    expect(screen.queryByRole("button", { name: "＋ Agentを追加" })).not.toBeInTheDocument();
  });
  it("keeps identity on profile failure and retries the same draft without POSTing another identity", async () => {
    const user = userEvent.setup(); let failed = false;
    fail = r => { if (!failed && r.method === "PATCH" && r.path.endsWith("organization-profile")) { failed = true; return 503; } };
    mount(<EmployeeWorkspace />); await user.click(await screen.findByRole("button", { name: "＋ Agentを追加" }));
    expect(screen.getByRole("combobox", { name: "Character（会話人格・任意）" })).toBeVisible();
    await openHelp(user, "Agentを追加");
    expect(await screen.findByText(/候補は会話用Character/)).toHaveTextContent("Personaとは別");
    expect(screen.getByText(/雇用状態は組織上の在籍・契約状態/)).toHaveTextContent("実行状態");
    await user.type(screen.getByLabelText("Agent名"), "備品担当"); await user.type(screen.getByLabelText("役職"), "調達");
    await user.click(screen.getByRole("button", { name: "下書きを保存して職務設定へ" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("利用できません");
    expect(screen.getByText(/下書き 備品担当 は保存済み/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "下書きを保存して職務設定へ" }));
    await waitFor(() => expect(mocks.push).toHaveBeenCalledWith(employeeHref("agent", "role"), { scroll: false }));
    expect(requests.filter(r => r.path === "/api/agents" && r.method === "POST")).toHaveLength(1);
  });
  it("opens the selected panel directly on reload and sends expected_state on activation", async () => {
    mocks.query = new URLSearchParams("tab=agents&agent=agent&panel=overview"); const user = userEvent.setup(); mount(<EmployeeWorkspace />);
    await user.click(await screen.findByRole("button", { name: "Agentを有効化" }));
    await waitFor(() => expect(requests).toContainEqual({ path: "/api/agents/agent/state", method: "PATCH", body: { state: "active", expected_state: "draft" } }));
    await user.click(screen.getByRole("button", { name: "活動・要確認" }));
    expect(mocks.push).toHaveBeenCalledWith(employeeHref("agent", "activity"), { scroll: false });
  });
  it("keeps a legacy revision's Team-derived models and tools on the server when only text changes", async () => {
    const user = userEvent.setup(); const saved = vi.fn(); mount(<EmployeeRole agent={employee} revisions={[revision]} catalog={catalog} onSaved={saved} />);
    await openHelp(user, "職務・モデル");
    expect(await screen.findByTestId("employee-model-settings-hint")).toHaveTextContent("全体の表");
    expect(screen.getByRole("link", { name: /設定 → 言語モデル → モデル/ })).toHaveAttribute("href", "/settings#llm-model");
    expect(screen.queryByRole("combobox", { name: "Execution Team" })).not.toBeInTheDocument();
    expect(screen.getByText(/旧設定の読み取り投影で動いています/)).toBeVisible();
    await user.clear(screen.getByLabelText("ミッション")); await user.type(screen.getByLabelText("ミッション"), "新しい職務");
    await user.click(screen.getByRole("button", { name: "新しい職務 revision v2 を保存" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    const body = requests.find(r => r.method === "POST")!.body!;
    expect(body).toMatchObject({ version: 2, mission: "新しい職務", capability_ceiling: ["external_action_propose"] });
    for (const field of ["model_overrides", "tool_ids", "delegate_policy", "agent_team_id", "execution_profile_id", "allowed_subagent_ids"]) expect(body).not.toHaveProperty(field);
    expect(revision.mission).toBe("在庫を管理する"); expect(requests.some(r => r.method === "PATCH")).toBe(false);
  });
  it.each([
    { budget_policy: {}, concurrency_policy: {} },
    { budget_policy: { currency: "USD", max_run_cost_micros: 400, max_daily_cost_micros: 2000 }, concurrency_policy: { max_parallel_runs: 4, max_parallel_tasks: 2, queue_class: "low" } },
    { budget_policy: { currency: "JPY", max_run_cost_micros: 0, max_daily_cost_micros: 0 }, concurrency_policy: { max_parallel_tasks: 3, queue_class: "normal" } },
  ])("preserves untouched budget/concurrency policies when only workspace_read changes: %j", async policies => {
    const original: EmployeeRevision = { ...revision, ...policies, capability_ceiling: [] };
    const readCatalog: EmployeeCatalog = { ...catalog, capabilities: [{ id: "workspace_read", family: "workspace", access: "read", native: true }] };
    const saved = vi.fn(); const user = userEvent.setup();
    mount(<EmployeeRole agent={employee} revisions={[original]} catalog={readCatalog} onSaved={saved} />);
    expect(screen.queryByText("予算通貨", { selector: "dt" })).not.toBeInTheDocument();
    expect(screen.queryByText("同時実行数", { selector: "dt" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "workspace_read (workspace / read)" }));
    await user.click(screen.getByRole("button", { name: "新しい職務 revision v2 を保存" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    expect(requests.find(r => r.method === "POST")?.body).toMatchObject({ capability_ceiling: ["workspace_read"], ...policies });
    expect(original.budget_policy).toEqual(policies.budget_policy);
    expect(original.concurrency_policy).toEqual(policies.concurrency_policy);
  });
  it("explicitly saves previously unset defaults and budgets, then restores them after reload", async () => {
    const original: EmployeeRevision = { ...revision, budget_policy: {}, concurrency_policy: {} };
    const saved = vi.fn(); const user = userEvent.setup();
    const view = mount(<EmployeeRole agent={employee} revisions={[original]} catalog={catalog} onSaved={saved} />);
    expect(screen.getByRole("combobox", { name: "予算通貨" })).toHaveTextContent("選択してください");
    expect(screen.getByLabelText("最大同時実行数")).toHaveValue(null);
    fireEvent.change(screen.getByLabelText("1実行の予算上限（百万分の1通貨単位）"), { target: { value: "0" } });
    fireEvent.change(screen.getByLabelText("1日の予算上限（百万分の1通貨単位）"), { target: { value: "2000" } });
    await select(user, "予算通貨", "JPY");
    fireEvent.change(screen.getByLabelText("最大同時実行数"), { target: { value: "1" } });
    await user.click(screen.getByRole("button", { name: "新しい職務 revision v2 を保存" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    const body = requests.find(r => r.method === "POST")!.body!;
    expect(body.budget_policy).toEqual({ currency: "JPY", max_run_cost_micros: 0, max_daily_cost_micros: 2000 });
    expect(body.concurrency_policy).toEqual({ max_parallel_runs: 1 });
    view.unmount();
    mount(<EmployeeRole agent={employee} revisions={[original, { ...original, ...body, id: "rev2", version: 2 } as EmployeeRevision]} catalog={catalog} onSaved={saved} />);
    expect(screen.getByRole("combobox", { name: "予算通貨" })).toHaveTextContent("JPY");
    expect(screen.getByLabelText("最大同時実行数")).toHaveValue(1);
    expect(screen.getByLabelText("1実行の予算上限（百万分の1通貨単位）")).toHaveValue(0);
    expect(screen.getByLabelText("1日の予算上限（百万分の1通貨単位）")).toHaveValue(2000);
    expect(original.budget_policy).toEqual({}); expect(original.concurrency_policy).toEqual({});
  });
  it("changes only the edited policy field and keeps stored concurrency fields", async () => {
    const original: EmployeeRevision = { ...revision, budget_policy: { currency: "USD", max_run_cost_micros: 500, max_daily_cost_micros: 2000 }, concurrency_policy: { max_parallel_runs: 4, max_parallel_tasks: 2, queue_class: "low" } };
    const saved = vi.fn(); const user = userEvent.setup();
    mount(<EmployeeRole agent={employee} revisions={[original]} catalog={catalog} onSaved={saved} />);
    fireEvent.change(screen.getByLabelText("1日の予算上限（百万分の1通貨単位）"), { target: { value: "3000" } });
    await user.click(screen.getByRole("button", { name: "新しい職務 revision v2 を保存" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    expect(requests.find(r => r.method === "POST")?.body).toMatchObject({ budget_policy: { ...original.budget_policy, max_daily_cost_micros: 3000 }, concurrency_policy: original.concurrency_policy });
  });
  it("can intentionally clear policy fields while preserving noneditable concurrency fields", async () => {
    const original: EmployeeRevision = { ...revision, budget_policy: { currency: "USD", max_run_cost_micros: 500, max_daily_cost_micros: 2000 }, concurrency_policy: { max_parallel_runs: 4, max_parallel_tasks: 2, queue_class: "low" } };
    const saved = vi.fn(); const user = userEvent.setup();
    mount(<EmployeeRole agent={employee} revisions={[original]} catalog={catalog} onSaved={saved} />);
    await select(user, "予算通貨", "選択してください");
    await user.clear(screen.getByLabelText("1実行の予算上限（百万分の1通貨単位）"));
    await user.clear(screen.getByLabelText("1日の予算上限（百万分の1通貨単位）"));
    await user.clear(screen.getByLabelText("最大同時実行数"));
    await user.click(screen.getByRole("button", { name: "新しい職務 revision v2 を保存" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    expect(requests.find(r => r.method === "POST")?.body).toMatchObject({ budget_policy: {}, concurrency_policy: { max_parallel_tasks: 2, queue_class: "low" } });
  });
  it("overrides one role's model, shows inherited routes, and saves tools and delegation with the revision", async () => {
    const current: EmployeeRevision = { ...revision, model_overrides: {}, tool_ids: ["external_action_propose"], delegate_policy: { agents: [], workers: [] },
      effective_model_routes: { chat: { route: { kind: "llm", provider: "openai", model: "gpt-5.6-luna" }, source: "global" }, code: { route: { kind: "llm", provider: "openai", model: "gpt-5.6-luna" }, source: "inherit_chat" } } };
    const user = userEvent.setup(); const saved = vi.fn(); const view = mount(<EmployeeRole agent={employee} revisions={[current]} catalog={catalog} onSaved={saved} />);
    const code = () => document.querySelector('[data-agent-role="code"]') as HTMLElement;
    expect(code()).toHaveTextContent("今は openai / gpt-5.6-luna（会話のモデルを継承）");
    await waitFor(() => expect(requests.some(r => r.path === "/api/llm/models")).toBe(true));
    await select(user, "コードに候補を追加", "OpenAI · Sol");
    await select(user, "コードの候補1の effort", "high");
    expect(code()).not.toHaveTextContent("今は");
    await user.click(await screen.findByRole("checkbox", { name: "Agent に委任" }));
    await user.click(screen.getByRole("checkbox", { name: "Codex に委任" }));
    await user.click(await screen.findByRole("checkbox", { name: "同じ所有者の Agent すべて" }));
    expect(screen.getByText(/権限上限に remote_agent_delegate が無いため使えません/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "新しい職務 revision v2 を保存" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    const body = requests.find(r => r.method === "POST")!.body!;
    expect(body).toMatchObject({
      model_overrides: { code: { routes: [{ kind: "llm", provider: "openai", model: "gpt-5.6-sol", effort: "high" }] } },
      tool_ids: ["external_action_propose", "delegate", "delegate_worker:codex"],
      delegate_policy: { agents: "any_owned", workers: ["codex"] },
    });
    expect(body).not.toHaveProperty("agent_team_id");
    view.unmount();
    mount(<EmployeeRole agent={employee} revisions={[current, { ...current, ...body, id: "rev2", version: 2 } as EmployeeRevision]} catalog={catalog} onSaved={saved} />);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "コードの候補1" })).toHaveTextContent("OpenAI · Sol"));
    await user.click(screen.getByRole("button", { name: "全体の設定を継承" }));
    expect(screen.queryByRole("combobox", { name: "コードの候補1の effort" })).not.toBeInTheDocument();
  });
  it("selects Codex Sol xhigh as primary and as a later candidate", async () => {
    const current: EmployeeRevision = { ...revision, model_overrides: {}, tool_ids: [], delegate_policy: { agents: [], workers: [] } };
    const user = userEvent.setup(); const saved = vi.fn();
    mount(<EmployeeRole agent={employee} revisions={[current]} catalog={catalog} onSaved={saved} />);
    await select(user, "コードに候補を追加", "Codex CLI · GPT-6.1 Sol");
    await select(user, "コードの候補1の effort", "xhigh");
    await select(user, "コードに候補を追加", "Codex CLI · GPT-6 Luna");
    await select(user, "コードに候補を追加", "Codex CLI · GPT-6.1 Sol");
    await select(user, "コードの候補3の effort", "xhigh");
    await user.click(screen.getByRole("button", { name: "新しい職務 revision v2 を保存" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    const routes = (requests.find(r => r.method === "POST")!.body!.model_overrides as { code: { routes: unknown[] } }).code.routes;
    expect(routes[0]).toEqual({ kind: "llm", provider: "codex-cli", model: "gpt-6.1-sol", effort: "xhigh" });
    expect(routes[2]).toEqual({ kind: "llm", provider: "codex-cli", model: "gpt-6.1-sol", effort: "xhigh" });
  });
  it("saves ordered candidates and preserves unrelated revision fields", async () => {
    const current: EmployeeRevision = { ...revision, model_overrides: { code: { routes: [{ kind: "llm", provider: "openai", model: "unavailable", effort: "low" }, { kind: "llm", provider: "openai", model: "gpt-5.6-luna" }], allow_paid_fallback: false }, review: { routes: [{ kind: "pool", pool_id: "old" }] } }, tool_ids: [], delegate_policy: { agents: [], workers: [] }, budget_policy: { currency: "USD" }, concurrency_policy: { max_parallel_runs: 2, queue_class: "low" } };
    const user = userEvent.setup(); const saved = vi.fn();
    mount(<EmployeeRole agent={employee} revisions={[current]} catalog={catalog} onSaved={saved} />);
    await waitFor(() => expect(requests.some(r => r.path === "/api/free-team/settings")).toBe(true));
    expect(screen.getByRole("combobox", { name: "コードの候補1" })).toHaveTextContent("unavailable");
    await select(user, "コードに候補を追加", "coordinator");
    await user.click(screen.getByRole("button", { name: "コードの候補3を上へ" }));
    await select(user, "コードの候補3の effort", "high");
    await select(user, "コードの従量課金 fallback", "従量課金 fallback: 許可");
    await user.click(screen.getByRole("button", { name: "新しい職務 revision v2 を保存" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    const body = requests.find(r => r.method === "POST")!.body!;
    expect(body).toMatchObject({ version: 2, budget_policy: current.budget_policy, concurrency_policy: current.concurrency_policy, model_overrides: { code: { routes: [{ kind: "llm", provider: "openai", model: "unavailable", effort: "low" }, { kind: "pool", pool_id: "coordinator" }, { kind: "llm", provider: "openai", model: "gpt-5.6-luna", effort: "high" }], allow_paid_fallback: true }, review: current.model_overrides!.review } });
    await user.click(screen.getByRole("button", { name: "新しい職務 revision v2 を保存" }));
    expect(requests.filter(r => r.method === "POST").map(r => r.body?.idempotency_key)).toEqual([body.idempotency_key, body.idempotency_key]);
  });
  it.each([{ conditions: null }, { conditions: [] }, { conditions: ["rate_limited"] }] as const)("saves and reads back fallback conditions %j", async ({ conditions }) => {
    const current: EmployeeRevision = { ...revision, model_overrides: { code: { routes: [{ kind: "llm", provider: "openai", model: "unknown" }, { kind: "pool", pool_id: "old" }], fallback_on: null }, review: { routes: [{ kind: "pool", pool_id: "other" }], fallback_on: [] } }, tool_ids: [], delegate_policy: { agents: [], workers: [] } };
    const user = userEvent.setup(); const saved = vi.fn();
    const view = mount(<EmployeeRole agent={employee} revisions={[current]} catalog={catalog} onSaved={saved} />);
    const code = document.querySelector('[data-agent-role="code"]') as HTMLElement;
    if (conditions !== null) {
      await user.click(within(code).getByRole("checkbox", { name: "既定の技術エラー条件を使う" }));
      for (const label of ["モデルが利用できない", "プロバイダーが利用できない", "レート制限に達した", "接続がタイムアウトした"]) {
        if (!(conditions.length && label === "レート制限に達した")) await user.click(within(code).getByRole("checkbox", { name: label }));
      }
    }
    await user.click(screen.getByRole("button", { name: "新しい職務 revision v2 を保存" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    const body = requests.find(r => r.method === "POST")!.body!;
    expect(body.model_overrides).toEqual({ code: { ...current.model_overrides!.code, fallback_on: conditions }, review: current.model_overrides!.review });
    view.unmount();
    mount(<EmployeeRole agent={employee} revisions={[{ ...current, ...body, id: "rev2", version: 2 } as EmployeeRevision]} catalog={catalog} onSaved={saved} />);
    const reread = document.querySelector('[data-agent-role="code"]') as HTMLElement;
    expect(within(reread).getByRole("checkbox", { name: "既定の技術エラー条件を使う" })).toHaveAttribute("aria-checked", String(conditions === null));
    expect(within(reread).getByRole("checkbox", { name: "レート制限に達した" })).toHaveAttribute("aria-checked", String(conditions === null || conditions.length > 0));
  });
  it.each([undefined, null, true, false] as const)("preserves paid fallback state %s", async (value) => {
    type CodeOverride = NonNullable<NonNullable<EmployeeRevision["model_overrides"]>["code"]>;
    const response = { ...revision, model_overrides: { code: { routes: [{ kind: "llm", provider: "openai", model: "gpt-5.6-sol" }], ...(value === undefined ? {} : { allow_paid_fallback: value }) } } } satisfies Omit<EmployeeRevision, "model_overrides"> & { model_overrides: { code: Omit<CodeOverride, "allow_paid_fallback"> & { allow_paid_fallback?: boolean | null } } };
    // Admit runtime JSON at the API boundary, including null inheritance.
    const current = JSON.parse(JSON.stringify(response)) as EmployeeRevision;
    const user = userEvent.setup(); const saved = vi.fn();
    mount(<EmployeeRole agent={employee} revisions={[current]} catalog={catalog} onSaved={saved} />);
    expect(screen.getByRole("combobox", { name: "コードの従量課金 fallback" })).toHaveTextContent(value == null ? "継承" : value ? "許可" : "許可しない");
    await user.click(screen.getByRole("button", { name: "新しい職務 revision v2 を保存" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    expect(requests.find(r => r.method === "POST")!.body!.model_overrides).toEqual(current.model_overrides);
  });
  it("removes the paid fallback field when switching from allow to inherit", async () => {
    const current: EmployeeRevision = { ...revision, model_overrides: { code: { routes: [{ kind: "llm", provider: "openai", model: "gpt-5.6-sol" }], allow_paid_fallback: true } } };
    const user = userEvent.setup(); const saved = vi.fn();
    mount(<EmployeeRole agent={employee} revisions={[current]} catalog={catalog} onSaved={saved} />);
    await select(user, "コードの従量課金 fallback", "従量課金 fallback: 継承");
    await user.click(screen.getByRole("button", { name: "新しい職務 revision v2 を保存" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    expect(requests.find(r => r.method === "POST")!.body!.model_overrides).toEqual({ code: { routes: current.model_overrides!.code!.routes } });
  });
  it("distinguishes catalog loading, failure, missing and known routes without replacing them", async () => {
    let resolveModels!: (value: Response) => void;
    mocks.fetch.mockImplementation((url: string, init?: RequestInit) => url.endsWith("/llm/models") ? new Promise<Response>(resolve => { resolveModels = resolve; }) : backend(url, init));
    const current: EmployeeRevision = { ...revision, model_overrides: { code: { routes: [{ kind: "llm", provider: "openai", model: "gpt-5.6-sol" }, { kind: "llm", provider: "openai", model: "missing" }] } } };
    const view = mount(<EmployeeRole agent={employee} revisions={[current]} catalog={catalog} onSaved={vi.fn()} />);
    expect(screen.getByRole("combobox", { name: "コードの候補1" })).toHaveTextContent("一覧を読み込み中");
    await waitFor(() => expect(resolveModels).toBeDefined());
    resolveModels(response({ providers: [{ id: "openai", label: "OpenAI", models: [{ id: "gpt-5.6-sol", label: "Sol" }] }] }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "コードの候補1" })).toHaveTextContent("OpenAI · Sol"));
    expect(screen.getByRole("combobox", { name: "コードの候補2" })).toHaveTextContent("一覧にありません");
    view.unmount();
    fail = r => r.path === "/api/llm/models" ? 503 : undefined; mocks.fetch.mockImplementation(backend);
    mount(<EmployeeRole agent={employee} revisions={[current]} catalog={catalog} onSaved={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "コードの候補1" })).toHaveTextContent("一覧を取得できません"), { timeout: 6000 });
    expect(screen.getByRole("combobox", { name: "コードの候補2" })).toHaveTextContent("missing");
  });
  it("clears only the selected role when inheriting", async () => {
    const current: EmployeeRevision = { ...revision, model_overrides: { code: { routes: [{ kind: "llm", provider: "openai", model: "missing" }], allow_paid_fallback: true }, review: { routes: [{ kind: "pool", pool_id: "old" }] } }, tool_ids: [], delegate_policy: { agents: [], workers: [] } };
    const user = userEvent.setup(); const saved = vi.fn();
    mount(<EmployeeRole agent={employee} revisions={[current]} catalog={catalog} onSaved={saved} />);
    const code = document.querySelector('[data-agent-role="code"]') as HTMLElement;
    await user.click(code.querySelector('button')!);
    await user.click(screen.getByRole("button", { name: "新しい職務 revision v2 を保存" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    expect(requests.find(r => r.method === "POST")!.body!.model_overrides).toEqual({ review: current.model_overrides!.review });
  });
  it("marks the default Chat Agent", () => {
    mount(<EmployeeRole agent={{ ...employee, is_default_chat_agent: true }} revisions={[revision]} catalog={catalog} onSaved={vi.fn()} />);
    expect(screen.getByText(/普通の Chat を受け持つ既定の Agent/)).toBeVisible();
  });
  it("409 exposes reload with no echoed secret and no automatic overwrite retry", async () => {
    fail = r => r.method === "POST" ? 409 : undefined; const reload = vi.fn(); const user = userEvent.setup();
    mount(<EmployeeRole agent={employee} revisions={[revision]} catalog={catalog} onSaved={reload} />);
    await user.click(screen.getByRole("button", { name: "新しい職務 revision v2 を保存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("競合"); expect(screen.queryByText("echoed-secret-must-never-render")).not.toBeInTheDocument();
    expect(requests.filter(r => r.method === "POST")).toHaveLength(1); await user.click(screen.getByRole("button", { name: "再読み込み" })); expect(reload).toHaveBeenCalledOnce();
  });
  it("adds a scoped project grant with explicit read/write and retains revoked history", async () => {
    const user = userEvent.setup(); assignmentRows = [{ id: "grant", project_id: "project", role: "viewer", state: "revoked" }];
    mount(<EmployeeAssignments agentId="agent" canManage />);
    await select(user, "所属・権限の種類", "Project 権限"); await select(user, "割り当て先", "備品 Project");
    await user.click(screen.getByRole("checkbox", { name: "書込を許可" })); await user.click(screen.getByRole("button", { name: "所属・権限を追加" }));
    await waitFor(() => expect(requests).toContainEqual({ path: "/api/agents/agent/project-grants", method: "POST", body: { project_id: "project", role: "viewer", permissions: { read: true, write: true, delete: false, manage_members: false, manage_settings: false } } }));
    expect(screen.getByText("解除済み")).toBeVisible(); expect(screen.queryByRole("button", { name: "解除" })).not.toBeInTheDocument();
  });
  it("saves CrowdWorks scan and profitability policy without submitting during setup", async () => {
    const user = userEvent.setup(); mount(<EmployeePolicies agentId="agent" canManage />);
    await user.click(screen.getByRole("button", { name: "ポリシーを追加" }));
    await select(user, "登録済みアクション", "CrowdWorksへ応募 — automatable");
    await user.type(screen.getByLabelText("ポリシー名"), "CrowdWorks自動応募");
    await select(user, "登録済み接続先", "CrowdWorks");
    await select(user, "承認方式", "範囲内自動実行");
    fireEvent.change(screen.getByLabelText("検索URL（1行1件）"), { target: { value: "https://crowdworks.jp/public/jobs/?category=1\nhttps://crowdworks.jp/public/jobs/?category=2" } });
    fireEvent.change(screen.getByLabelText("1回の最大応募案件数"), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText("最低期待利益"), { target: { value: "5000" } });
    fireEvent.change(screen.getByLabelText("最低期待利益/時間"), { target: { value: "2500" } });
    await openHelp(user, "CrowdWorks応募ポリシー");
    expect(await screen.findByText(/評価・応募文作成のモデル/)).toHaveTextContent("役割ごとのモデル");
    expect(screen.queryByLabelText(/旧・評価LLM/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "上記の許可範囲を確認しました" }));
    await user.click(screen.getByRole("button", { name: "ポリシー revision を保存" }));
    await waitFor(() => expect(requests.find(r => r.path.endsWith("policy/revisions"))?.body).toMatchObject({
      action_type: "engagement.submit_application", connection_id: "cw-conn", authorization_mode: "bounded_auto",
      constraints: { search_urls: ["https://crowdworks.jp/public/jobs/?category=1", "https://crowdworks.jp/public/jobs/?category=2"], max_applications_per_scan: 3, min_expected_profit: 5000, min_profit_per_hour: 2500, currency: "JPY" },
    }));
    expect(requests.some(r => /execute|attempt|applications/.test(r.path))).toBe(false);
  });
  it("encodes URL identities and sends authenticated proxy requests without retaining failure bodies", async () => {
    expect(employeeHref("a&panel=phone", "role")).toBe("/operations?tab=agents&agent=a%26panel%3Dphone&panel=role");
    fail = () => 403;
    await expect(employeeRequest("/api/agents")).rejects.toEqual(new EmployeeApiError(403));
    expect(mocks.fetch).toHaveBeenCalledWith("/api/python-proxy/agents", expect.objectContaining({ credentials: "include", cache: "no-store", method: "GET" }));
    expect(() => JSON.stringify(new EmployeeApiError(403))).not.toThrow();
  });
  it("shows uncertain actions without counters for removed automation, integration or phone features", () => {
    const info: EmployeeObservability = { management_visible: false, rule_count: null, active_rule_count: null, action_policy_count: null, integration_readiness: { status: "ready", executable: false }, last_automation_trigger: null, last_automation_result: null, uncertain_action_count: 2 };
    mount(<EmployeeLiveInfo projection={{ id: "agent", legacy: { agent_team_id: "employee", execution_profile_id: "manual" }, employee_observability: info }} />);
    expect(screen.queryByText(/有効な自動化|電話受付|連携:/)).not.toBeInTheDocument();
    expect(screen.getByText(/要確認 2件/)).toHaveTextContent("再実行禁止");
    expect(screen.getByText(/旧設定: employee/)).toBeVisible();
  });
  it("reads the native Task GET array and assigns a task from the selected project", async () => {
    const user = userEvent.setup(); mount(<EmployeeAssignments agentId="agent" canManage />);
    await select(user, "所属・権限の種類", "Task 担当"); await select(user, "Task の Project", "備品 Project");
    await select(user, "割り当て先", "在庫確認"); await user.click(screen.getByRole("button", { name: "所属・権限を追加" }));
    await waitFor(() => expect(requests).toContainEqual({ path: "/api/agents/agent/task-assignments", method: "POST", body: { task_id: "task", assignment_role: "executor" } }));
  });
  it("preserves both policy effective-window boundaries in a new revision", async () => {
    policyRows = [{ id: "policy", display_name: "期間限定補充", state: "draft", version: 4, current_revision: { id: "p-rev", version: 3, action_type: "engagement.submit_application", connection_id: "cw-conn", authorization_mode: "human_approval", constraints: { search_urls: ["https://crowdworks.jp/public/jobs/"], scan_interval_minutes: 30, max_pages_per_scan: 3, max_opportunities_per_scan: 100, max_applications_per_scan: 5, min_expected_profit: 0, min_profit_per_hour: 0, currency: "JPY" }, rate_limit: { window_seconds: 86400, max_actions: 4 }, dedupe_window_seconds: 86400, fallback_behavior: "block", active_from: "2026-10-01T00:00:00Z", active_until: "2026-10-31T00:00:00Z" } }];
    const user = userEvent.setup(); mount(<EmployeePolicies agentId="agent" canManage />);
    await user.click(await screen.findByRole("button", { name: "編集・新しい版" }));
    await user.click(screen.getByRole("button", { name: "ポリシー revision を保存" }));
    await waitFor(() => expect(requests.find(r => r.path.endsWith("policy/revisions"))?.body).toMatchObject({ expected_version: 4, active_from: "2026-10-01T00:00:00Z", active_until: "2026-10-31T00:00:00Z" }));
  });
  it("recovers a lost policy create response with its original intent before applying an edited name", async () => {
    let lost = false; fail = r => { if (!lost && r.path === "/api/agent-action-policies" && r.method === "POST") { lost = true; return 503; } };
    const user = userEvent.setup(); mount(<EmployeePolicies agentId="agent" canManage />);
    await user.click(screen.getByRole("button", { name: "ポリシーを追加" })); await select(user, "登録済みアクション", "CrowdWorksへ応募 — automatable");
    await user.type(screen.getByLabelText("ポリシー名"), "元の名前"); await select(user, "登録済み接続先", "CrowdWorks");
    await user.click(screen.getByRole("button", { name: "ポリシー revision を保存" })); await screen.findByRole("alert");
    await user.clear(screen.getByLabelText("ポリシー名")); await user.type(screen.getByLabelText("ポリシー名"), "変更した名前");
    await user.click(screen.getByRole("button", { name: "ポリシー revision を保存" }));
    await waitFor(() => expect(requests.some(r => r.path.endsWith("policy/revisions"))).toBe(true));
    const creates = requests.filter(r => r.path === "/api/agent-action-policies" && r.method === "POST"); expect(creates).toHaveLength(2); expect(creates[1].body).toEqual(creates[0].body);
    expect(requests.find(r => r.path === "/api/agent-action-policies/policy" && r.method === "PATCH")?.body).toMatchObject({ display_name: "変更した名前", expected_version: 1 });
  });
  it("locks uncertain identity inputs and replays the original create intent without a new key", async () => {
    let lost = false; fail = r => { if (!lost && r.path === "/api/agents" && r.method === "POST") { lost = true; return 503; } };
    const user = userEvent.setup(); mount(<EmployeeWorkspace />); await user.click(await screen.findByRole("button", { name: "＋ Agentを追加" }));
    await user.type(screen.getByLabelText("Agent名"), "備品担当"); await user.click(screen.getByRole("button", { name: "下書きを保存して職務設定へ" })); await screen.findByRole("alert");
    expect(screen.getByLabelText("Agent名")).toBeDisabled(); expect(screen.getByLabelText("slug（任意）")).toBeDisabled();
    await user.type(screen.getByLabelText("Agent名"), "変更できない");
    await user.click(screen.getByRole("button", { name: "下書きを保存して職務設定へ" })); await waitFor(() => expect(mocks.push).toHaveBeenCalled());
    const creates = requests.filter(r => r.path === "/api/agents" && r.method === "POST"); expect(creates).toHaveLength(2); expect(creates[1].body).toEqual(creates[0].body);
  });
  it("refreshes list, authority and projection together after lifecycle activation", async () => {
    mocks.query = new URLSearchParams("tab=agents&agent=agent&panel=overview"); const user = userEvent.setup(); mount(<EmployeeWorkspace />);
    await waitFor(() => expect(requests.some(r => r.path.includes("effective-authority"))).toBe(true));
    expect(screen.queryByText("agent_inactive")).not.toBeInTheDocument();
    expect(screen.queryByText(/実効権限:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Agent ID:/)).not.toBeInTheDocument();
    expect(screen.queryByText("読み取り投影 agent")).not.toBeInTheDocument();
    const before = requests.length;
    await user.click(screen.getByRole("button", { name: "Agentを有効化" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Agentを一時停止" })).toBeEnabled());
    expect(screen.queryByText("agent_inactive")).not.toBeInTheDocument();
    expect(screen.queryByText("下書きの設定を再開")).not.toBeInTheDocument();
    expect(requests.slice(before).some(r => r.path === "/api/agents")).toBe(true);
    expect(requests.slice(before).some(r => r.path === "/api/operations/command-center")).toBe(true);
    expect(requests.slice(before).some(r => r.path.includes("effective-authority"))).toBe(true);
  });
});
