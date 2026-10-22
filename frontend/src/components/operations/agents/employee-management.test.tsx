// @vitest-environment jsdom

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EmployeeWorkspace, employeeHref } from "./employee-workspace";
import { AppDevelopmentPhasePreview, EmployeeRole } from "./employee-role";
import { EmployeePolicies } from "./employee-policies";
import { EmployeeAutomation } from "./employee-automation";
import { EmployeeAssignments } from "./employee-assignments";
import { EmployeeLiveInfo, type EmployeeObservability } from "./employee-observability";
import { EmployeeApiError, employeeRequest, type EmployeeCatalog, type EmployeeExecutionProfile, type EmployeeRevision } from "@/lib/agent-employees-api";

const mocks = vi.hoisted(() => ({ push: vi.fn(), query: new URLSearchParams(), enabled: true, manage: true, characters: vi.fn(), fetch: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: mocks.push }), useSearchParams: () => mocks.query }));
vi.mock("@/contexts/runtime-context", () => ({ useOptionalRuntimeContext: () => ({ runtimeFeatures: { application_features: { virtual_company: mocks.enabled, autonomous_agent_runtime: mocks.enabled } } }) }));
vi.mock("../operations-command-center", () => ({ OperationsCommandCenter: ({ agentId }: { agentId?: string }) => <p>読み取り投影 {agentId}</p> }));
vi.mock("./employee-integrations", () => ({ EmployeeIntegrations: () => <p>連携パネル</p>, EmployeePhone: () => <p>電話パネル</p> }));
vi.mock("@/lib/media-operations-api", () => ({ mediaOperationsApi: { listCharacters: mocks.characters, listPersonas: async () => [] } }));
vi.mock("@/lib/operations-api", () => ({ operationsApi: { listConnections: async () => [{ id: "conn", display_name: "備品接続", provider_key: "procurement" }, { id: "phone-conn", display_name: "受付接続", provider_key: "openai_realtime_sip" }, { id: "cw-conn", display_name: "CrowdWorks", provider_key: "crowdworks" }] } }));

const employee = { id: "agent", display_name: "備品担当", slug: "supplies", state: "draft" as const };
const catalog: EmployeeCatalog = { can_manage: true, teams: [
  { team_id: "employee", name: "Agentチーム", subagents: [{ subagent_id: "operator", name: "運用担当", capability_ids: ["external_action_propose"] }], execution_profiles: [{ profile_id: "manual", name: "手動モデル" }] },
  { team_id: "support", name: "支援チーム", subagents: [], execution_profiles: [{ profile_id: "support-profile", name: "支援モデル" }] },
], capabilities: [{ id: "external_action_propose", family: "action", access: "write", native: false }] };
const revision: EmployeeRevision = { id: "rev1", agent_id: "agent", version: 1, display_name: "備品担当", mission: "在庫を管理する", responsibility_summary: "備品補充", operational_instructions: "確認する", agent_team_id: "employee", execution_profile_id: "manual", allowed_subagent_ids: ["operator"], capability_ceiling: ["external_action_propose"], budget_policy: {}, concurrency_policy: { max_parallel_runs: 1 } };
const definition = { action_type: "procurement.place_order", display_name: "水を注文", status: "unavailable", category: "procurement", required_capabilities: ["external_action_propose"], connection_provider_keys: ["procurement"], constraints_schema: {} };
const procurement = { fixed_item_ref: "water-ref", fixed_ship_to_ref: "tokyo-office", min_quantity: 1, max_quantity: 2, default_quantity: 1, currency: "JPY", max_order_total_minor: 10000, require_quote_before_execute: true };
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
  if (path === "/api/integrations/actions") return response({ actions: [definition, { action_type: "telephony.transfer_call", display_name: "通話を転送", status: "unavailable", category: "telephony", required_capabilities: ["telephony_control"], connection_provider_keys: ["openai_realtime_sip"], constraints_schema: null }, { action_type: "engagement.submit_application", display_name: "CrowdWorksへ応募", status: "automatable", category: "engagement", required_capabilities: ["external_action_propose"], connection_provider_keys: ["crowdworks"], constraints_schema: {} }] });
  if (path === "/api/telephony/routes?agent_id=agent") return response({ routes: [{ id: "route", agent_id: "agent", display_name: "本社受付", provider_route_ref: "main", connection_id: "phone-conn", state: "draft" }] });
  if (path === "/api/telephony/catalog") return response({ route_keys: [{ key: "main", display_name: "登録済み本社回線", connection_id: "phone-conn", destination_keys: [{ key: "support", display_name: "サポート窓口" }] }] });
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
  it("preserves v1 and POSTs an immutable v2 after review; catalog changes reset incompatible profile", async () => {
    const user = userEvent.setup(); const saved = vi.fn(); mount(<EmployeeRole agent={employee} revisions={[revision]} catalog={catalog} onSaved={saved} />);
    // Models live in the Team's Execution Profiles; the role only references one.
    await openHelp(user, "職務・モデル");
    expect(await screen.findByText(/Execution TeamはSubagentとExecution Profile/)).toHaveTextContent("Agent組織とは別");
    expect(screen.getByText(/Execution TeamはSubagentとExecution Profile/)).toHaveTextContent("実行時の指示やtoolsがすべて共通になるわけではありません");
    expect(screen.queryByRole("combobox", { name: "Agent Team" })).not.toBeInTheDocument();
    expect(screen.getByTestId("employee-model-settings-hint")).toHaveTextContent("Execution Profile");
    expect(screen.getByRole("link", { name: /Execution Profiles/ })).toHaveAttribute("href", "/settings#llm-model");
    await select(user, "Execution Team", "支援チーム");
    expect(screen.getByRole("combobox", { name: "Execution Profile" })).not.toHaveTextContent("手動モデル");
    await select(user, "Execution Profile", "支援モデル");
    await user.clear(screen.getByLabelText("ミッション")); await user.type(screen.getByLabelText("ミッション"), "新しい職務");
    await user.click(screen.getByRole("checkbox", { name: "変更内容を確認しました" }));
    await user.click(screen.getByRole("button", { name: "新しい職務 revision v2 を保存" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    const request = requests.find(r => r.method === "POST")!;
    expect(request.body).toMatchObject({ version: 2, mission: "新しい職務", agent_team_id: "support", execution_profile_id: "support-profile", allowed_subagent_ids: [], capability_ceiling: [] });
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
    await user.click(screen.getByRole("checkbox", { name: "変更内容を確認しました" }));
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
    await user.click(screen.getByRole("checkbox", { name: "変更内容を確認しました" }));
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
    await user.click(screen.getByRole("checkbox", { name: "変更内容を確認しました" }));
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
    await user.click(screen.getByRole("checkbox", { name: "変更内容を確認しました" }));
    await user.click(screen.getByRole("button", { name: "新しい職務 revision v2 を保存" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    expect(requests.find(r => r.method === "POST")?.body).toMatchObject({ budget_policy: {}, concurrency_policy: { max_parallel_tasks: 2, queue_class: "low" } });
  });
  it("previews each Subagent's effective route read-only and keeps Team/Profile after save and reload", async () => {
    const routed: EmployeeCatalog = { ...catalog, teams: [...catalog.teams, { team_id: "app", name: "App Development", subagents: [
      { subagent_id: "architecture_planner", name: "設計", capability_ids: [] }, { subagent_id: "code_implementer", name: "実装", capability_ids: [] }],
      execution_profiles: [{ profile_id: "hybrid", name: "思考=高性能 / 作業=ローカル", routes: [
        { subagent_id: "architecture_planner", name: "設計", provider: "claude-cli", model: "opus", effort: "high", route_source: "profile_default", inherits_main: false },
        { subagent_id: "code_implementer", name: "実装", provider: "openai_compatible_local", model: "qwen3-coder", effort: null, route_source: "subagent_override", inherits_main: false }] }] }] };
    const user = userEvent.setup(); const saved = vi.fn();
    const view = mount(<EmployeeRole agent={employee} revisions={[revision]} catalog={routed} onSaved={saved} />);
    expect(screen.queryByRole("region", { name: "実効モデル" })).not.toBeInTheDocument();
    await select(user, "Execution Team", "App Development"); await select(user, "Execution Profile", "思考=高性能 / 作業=ローカル");
    const preview = screen.getByRole("region", { name: "実効モデル" });
    expect(preview).toHaveTextContent("設計（未許可）: claude-cli / opus · effort high · Default Subagent Route");
    expect(preview).toHaveTextContent("openai_compatible_local / qwen3-coder · Subagent Override");
    await openHelp(user, "実効モデル");
    expect(await screen.findByText(/設定 → 言語モデル → Execution Team → Execution Profile/u)).toBeVisible();
    expect(preview.querySelector("input, select")).toBeNull();
    const phases = screen.getByRole("region", { name: "App Development phase解決" });
    await openHelp(user, "App Development phase解決");
    expect(await screen.findByText(/自動reviewを強制する設定ではありません/u)).toBeVisible();
    expect(phases).toHaveTextContent("planner");
    await user.click(screen.getByRole("checkbox", { name: "設計" })); await user.click(screen.getByRole("checkbox", { name: "実装" }));
    expect(preview).not.toHaveTextContent("未許可");
    expect(phases).toHaveTextContent(/planner\s*→\s*設計/);
    expect(phases).toHaveTextContent(/worker\s*→\s*実装/);
    expect(phases).toHaveTextContent(/reviewer\s*→\s*設計/);
    await user.click(screen.getByRole("checkbox", { name: "変更内容を確認しました" }));
    await user.click(screen.getByRole("button", { name: "新しい職務 revision v2 を保存" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    const body = requests.find(r => r.method === "POST")!.body!;
    expect(body).toMatchObject({ agent_team_id: "app", execution_profile_id: "hybrid", allowed_subagent_ids: ["architecture_planner", "code_implementer"] });
    view.unmount();
    const v2 = { ...revision, ...body, id: "rev2", version: 2 } as EmployeeRevision;
    mount(<EmployeeRole agent={employee} revisions={[revision, v2]} catalog={routed} onSaved={saved} />);
    expect(screen.getByRole("heading", { name: /現在 v2/ })).toBeVisible();
    expect(screen.getByRole("region", { name: "実効モデル" })).toHaveTextContent("openai_compatible_local / qwen3-coder");
    expect(screen.getByRole("checkbox", { name: "実装" })).toBeChecked();
  });

  it("canonical preset aliasをAgentRoleのExecution Profile選択へ統合して保存する", async () => {
    const presetCatalog: EmployeeCatalog = {
      ...catalog,
      teams: [...catalog.teams, {
        team_id: "preset-team",
        name: "Preset Team",
        subagents: [],
        execution_profiles: [],
        execution_profile_presets: [{ preset_id: "official", name: "Official Preset", preset: true }],
      }],
    };
    const user = userEvent.setup(); const saved = vi.fn();
    mount(<EmployeeRole agent={employee} revisions={[revision]} catalog={presetCatalog} onSaved={saved} />);
    await select(user, "Execution Team", "Preset Team");
    await select(user, "Execution Profile", "Official Preset");
    await user.click(screen.getByRole("checkbox", { name: "変更内容を確認しました" }));
    await user.click(screen.getByRole("button", { name: "新しい職務 revision v2 を保存" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    expect(requests.find(r => r.method === "POST")?.body).toMatchObject({ agent_team_id: "preset-team", execution_profile_id: "official" });
  });

  it("candidate-specific phase errorは後続の許可候補へfallbackし、role-level errorだけ表示する", () => {
    const team = {
      team_id: "app_development",
      name: "App Development",
      subagents: [
        { subagent_id: "architecture_planner", name: "設計", capability_ids: [] },
        { subagent_id: "general_worker", name: "汎用作業", capability_ids: [] },
      ],
      execution_profiles: [],
    };
    const profile: EmployeeExecutionProfile = {
      profile_id: "phase",
      name: "Phase preview",
      routes: [
        { subagent_id: "architecture_planner", name: "設計", error_code: "agent_route_model_unavailable" },
        { subagent_id: "general_worker", name: "汎用作業", provider: "openai", model: "gpt-5.6", route_source: "profile_default" },
      ],
      phase_routes: [
        { phase: "planner", subagent_id: "architecture_planner", name: "設計", error_code: "agent_route_model_unavailable" },
        { phase: "reviewer", subagent_id: "", name: "", error_code: "agent_route_role_unavailable" },
      ],
    };
    mount(<AppDevelopmentPhasePreview team={team} profile={profile} allowed={["architecture_planner", "general_worker"]} />);
    const preview = screen.getByRole("region", { name: "App Development phase解決" });
    expect(preview).toHaveTextContent(/planner\s*→\s*汎用作業/);
    expect(preview).toHaveTextContent("reviewer");
    expect(preview).toHaveTextContent("agent_route_role_unavailable");
    expect(preview).not.toHaveTextContent("agent_route_model_unavailable");
  });

  it("Jev routeをAgentのprovider/model表示へ出さない", async () => {
    const jevCatalog: EmployeeCatalog = {
      ...catalog,
      teams: [{
        team_id: "jev-team",
        name: "Jev Team",
        subagents: [{ subagent_id: "worker", name: "作業", capability_ids: [] }],
        execution_profiles: [{ profile_id: "jev-profile", name: "Jev hidden", routes: [{ subagent_id: "worker", name: "作業", provider: "jev", model: "decision", route_source: "profile_default" }] }],
      }],
    };
    const user = userEvent.setup();
    mount(<EmployeeRole agent={employee} revisions={[revision]} catalog={jevCatalog} onSaved={vi.fn()} />);
    await select(user, "Execution Team", "Jev Team");
    await select(user, "Execution Profile", "Jev hidden");
    const preview = screen.getByRole("region", { name: "実効モデル" });
    expect(preview).not.toHaveTextContent(/Jev|decision/i);
    expect(preview).toHaveTextContent("内部選択ルート");
  });
  it("shows an explicit warning when an App Development revision lacks the operator", () => {
    const warnedRevision = {
      ...revision,
      agent_team_id: "app_development",
      execution_profile_id: "manual",
      allowed_subagent_ids: ["code_explorer", "architecture_planner", "code_implementer", "code_reviewer"],
      route_warnings: [{ code: "employee_operator_not_allowed", subagent_id: "employee_operator", message: "新しい職務Revisionでemployee_operatorを許可してください。" }],
    } as EmployeeRevision;
    const appCatalog: EmployeeCatalog = {
      ...catalog,
      teams: [{ team_id: "app_development", name: "App Development", subagents: [{ subagent_id: "employee_operator", name: "Agent業務", capability_ids: ["external_action_propose"] }], execution_profiles: [{ profile_id: "manual", name: "標準モデル" }] }],
    };
    mount(<EmployeeRole agent={employee} revisions={[warnedRevision]} catalog={appCatalog} onSaved={vi.fn()} />);
    expect(screen.getByRole("alert")).toHaveTextContent("外部操作operator: 未許可");
  });
  it("409 exposes reload with no echoed secret and no automatic overwrite retry", async () => {
    fail = r => r.method === "POST" ? 409 : undefined; const reload = vi.fn(); const user = userEvent.setup();
    mount(<EmployeeRole agent={employee} revisions={[revision]} catalog={catalog} onSaved={reload} />);
    await user.click(screen.getByRole("checkbox", { name: "変更内容を確認しました" })); await user.click(screen.getByRole("button", { name: "新しい職務 revision v2 を保存" }));
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
  it("saves structured bounded procurement as draft and never invokes an execution endpoint", async () => {
    const user = userEvent.setup(); mount(<EmployeePolicies agentId="agent" canManage />);
    await user.click(screen.getByRole("button", { name: "ポリシーを追加" }));
    await select(user, "登録済みアクション", "水を注文 — unavailable");
    await user.type(screen.getByLabelText("ポリシー名"), "水の補充"); await select(user, "登録済み接続先", "備品接続"); await select(user, "承認方式", "範囲内自動実行");
    await user.type(screen.getByLabelText("固定商品参照キー"), "water-ref"); await user.type(screen.getByLabelText("固定配送先参照キー"), "tokyo-office");
    await user.click(screen.getByRole("button", { name: "購買範囲の説明" }));
    expect(await screen.findByText(/商品・配送先は管理者が確認済みの参照キー/)).toBeVisible();
    for (const label of ["固定商品参照キー", "固定配送先参照キー"]) {
      const input = screen.getByLabelText(label) as HTMLInputElement;
      const pattern = new RegExp(`^(?:${input.pattern})$`, "v");
      expect(pattern.test(input.value)).toBe(true);
      expect(pattern.test("supplier:water-01")).toBe(true);
      expect(pattern.test("https://unregistered.invalid/order")).toBe(false);
      expect(input.maxLength).toBe(120);
    }
    fireEvent.change(screen.getByLabelText("注文総額上限（最小通貨単位・JPYは円）"), { target: { value: "10000" } });
    expect(screen.getByText("この範囲内の操作は毎回の人間承認なしで実行されます。")).toBeVisible();
    await user.click(screen.getByRole("checkbox", { name: "上記の許可範囲を確認しました" })); await user.click(screen.getByRole("button", { name: "ポリシー revision を保存" }));
    await waitFor(() => expect(requests.find(r => r.path.endsWith("policy/revisions"))?.body).toMatchObject({ expected_version: 1, authorization_mode: "bounded_auto", constraints: { fixed_item_ref: "water-ref", fixed_ship_to_ref: "tokyo-office", max_order_total_minor: 10000 }, dedupe_window_seconds: 86400, fallback_behavior: "block" }));
    expect(requests.filter(r => r.method !== "GET")).toHaveLength(2);
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
    expect(await screen.findByText(/標準モデル（manual）を含め/)).toHaveTextContent("旧provider・model・reasoning effort指定は使われません");
    expect(screen.getByText(/標準モデル（manual）を含め/)).toHaveTextContent("Execution Team / Execution Profile");
    await user.type(screen.getByLabelText("旧・評価LLM provider（非推奨）"), "codex-cli");
    await user.type(screen.getByLabelText("旧・評価LLM model（非推奨）"), "gpt-5.6-codex");
    await user.click(screen.getByRole("checkbox", { name: "上記の許可範囲を確認しました" }));
    await user.click(screen.getByRole("button", { name: "ポリシー revision を保存" }));
    await waitFor(() => expect(requests.find(r => r.path.endsWith("policy/revisions"))?.body).toMatchObject({
      action_type: "engagement.submit_application", connection_id: "cw-conn", authorization_mode: "bounded_auto",
      constraints: { search_urls: ["https://crowdworks.jp/public/jobs/?category=1", "https://crowdworks.jp/public/jobs/?category=2"], max_applications_per_scan: 3, min_expected_profit: 5000, min_profit_per_hour: 2500, currency: "JPY", llm_provider: "codex-cli", llm_model: "gpt-5.6-codex" },
    }));
    expect(requests.some(r => /execute|attempt|applications/.test(r.path))).toBe(false);
  });
  it("saves semantic rule with scoped human trigger and exact policy revision defaults", async () => {
    const user = userEvent.setup(); policyRows = [{ id: "policy", display_name: "水の補充", state: "draft", version: 2, current_revision: { id: "policyrev", version: 1, authorization_mode: "bounded_auto" } }];
    mount(<EmployeeAutomation agentId="agent" revisionId="rev1" canManage />);
    await user.click(screen.getByRole("button", { name: "自動化ルールを追加" }));
    await user.type(screen.getByLabelText("ルール名"), "水がない報告"); await select(user, "対象のチャット範囲", "備品 Project"); await select(user, "反応する条件", "意味で判定");
    expect(screen.getByLabelText("どんな報告に反応するか")).toHaveAttribute("placeholder", "人がウォーターサーバー用の交換水がない、空になった、在庫切れと報告したとき");
    await user.type(screen.getByLabelText("どんな報告に反応するか"), "交換水が空になった報告");
    await select(user, "追加する操作ポリシー", "水の補充 v1 — 範囲内自動"); await user.click(screen.getByRole("button", { name: "操作を紐付ける" }));
    await user.click(screen.getByRole("button", { name: "ルール revision を保存" }));
    await waitFor(() => expect(requests.find(r => r.path.endsWith("rule/revisions"))?.body).toMatchObject({ agent_revision_id: "rev1", expected_version: 1, trigger_config: { human_only: true, project_id: "project" }, condition_mode: "semantic", condition_config: { situation_description: "交換水が空になった報告" }, actions: [{ action_policy_revision_id: "policyrev", input_mapping: {}, on_noop: "continue" }] }));
  });
  it("uses evaluate-only manual test and never POSTs actions or work", async () => {
    ruleRows = [{ id: "rule", display_name: "補充", state: "draft", version: 2, current_revision: { id: "rulerev", version: 1, agent_revision_id: "rev1", event_type: "chat.message.created", trigger_config: { human_only: true, project_id: "project" }, condition_mode: "always", condition_config: {}, actions: [], max_attempts: 3, priority: 0 } }];
    const user = userEvent.setup(); mount(<EmployeeAutomation agentId="agent" revisionId="rev1" canManage />);
    await user.click(await screen.findByRole("button", { name: "ルールを編集・テスト" })); await user.type(await screen.findByLabelText("テストする報告"), "水がない"); await user.click(screen.getByRole("button", { name: "条件をテスト" }));
    expect(await screen.findByText(/条件に一致しました/)).toHaveTextContent("実行していません");
    expect(requests.filter(r => r.method !== "GET")).toEqual([{ path: "/api/agent-automation/rules/rule/test", method: "POST", body: { rule_revision_id: "rulerev", text: "水がない" } }]);
  });
  it("encodes URL identities and sends authenticated proxy requests without retaining failure bodies", async () => {
    expect(employeeHref("a&panel=phone", "role")).toBe("/operations?tab=agents&agent=a%26panel%3Dphone&panel=role");
    fail = () => 403;
    await expect(employeeRequest("/api/agents")).rejects.toEqual(new EmployeeApiError(403));
    expect(mocks.fetch).toHaveBeenCalledWith("/api/python-proxy/agents", expect.objectContaining({ credentials: "include", cache: "no-store", method: "GET" }));
    expect(() => JSON.stringify(new EmployeeApiError(403))).not.toThrow();
  });
  it("blocks semantic activation when runtime support is absent", async () => {
    ruleRows = [{ id: "rule", display_name: "意味判定", state: "draft", version: 2, current_revision: { id: "rulerev", version: 1, agent_revision_id: "rev1", condition_mode: "semantic", actions: [], semantic_readiness: { supported: false, error_code: "semantic_provider_unavailable" } } }];
    mount(<EmployeeAutomation agentId="agent" revisionId="rev1" canManage />);
    expect(await screen.findByRole("button", { name: "ルールを有効化" })).toBeDisabled();
    expect(screen.getByText(/意味判定の実行設定は利用できません/)).toBeVisible();
    expect(requests.some(r => r.method !== "GET")).toBe(false);
  });
  it("keeps hidden counters unknown and does not label stored credentials executable", () => {
    const info: EmployeeObservability = { management_visible: false, rule_count: null, active_rule_count: null, action_policy_count: null, integration_readiness: { status: "ready", executable: false }, last_automation_trigger: null, last_automation_result: null, uncertain_action_count: 2, phone: { route_count: null } };
    mount(<EmployeeLiveInfo projection={{ id: "agent", agent_team_id: "employee", execution_profile_id: "manual", employee_observability: info }} />);
    expect(screen.getByText(/有効な自動化/)).toHaveTextContent("非表示・未取得");
    expect(screen.getByText(/登録・認証確認済み/)).toHaveTextContent("実行時に再検証");
    expect(screen.getByText(/要確認 2件/)).toHaveTextContent("再実行禁止");
    expect(screen.getByText(/Execution Team: employee/)).toBeVisible();
  });
  it("reads the native Task GET array and assigns a task from the selected project", async () => {
    const user = userEvent.setup(); mount(<EmployeeAssignments agentId="agent" canManage />);
    await select(user, "所属・権限の種類", "Task 担当"); await select(user, "Task の Project", "備品 Project");
    await select(user, "割り当て先", "在庫確認"); await user.click(screen.getByRole("button", { name: "所属・権限を追加" }));
    await waitFor(() => expect(requests).toContainEqual({ path: "/api/agents/agent/task-assignments", method: "POST", body: { task_id: "task", assignment_role: "executor" } }));
  });
  it("preserves both policy effective-window boundaries in a new revision", async () => {
    policyRows = [{ id: "policy", display_name: "期間限定補充", state: "draft", version: 4, current_revision: { id: "p-rev", version: 3, action_type: "procurement.place_order", connection_id: "conn", authorization_mode: "human_approval", constraints: procurement, rate_limit: { window_seconds: 86400, max_actions: 4 }, dedupe_window_seconds: 86400, fallback_behavior: "block", active_from: "2026-10-01T00:00:00Z", active_until: "2026-10-31T00:00:00Z" } }];
    const user = userEvent.setup(); mount(<EmployeePolicies agentId="agent" canManage />);
    await user.click(await screen.findByRole("button", { name: "編集・新しい版" }));
    await user.click(screen.getByRole("button", { name: "ポリシー revision を保存" }));
    await waitFor(() => expect(requests.find(r => r.path.endsWith("policy/revisions"))?.body).toMatchObject({ expected_version: 4, active_from: "2026-10-01T00:00:00Z", active_until: "2026-10-31T00:00:00Z" }));
  });
  it("creates a registered transfer policy using only the selected route and catalog destinations", async () => {
    const user = userEvent.setup(); mount(<EmployeePolicies agentId="agent" canManage />);
    await user.click(screen.getByRole("button", { name: "ポリシーを追加" }));
    await select(user, "登録済みアクション", "通話を転送 — unavailable");
    await user.type(screen.getByLabelText("ポリシー名"), "受付の転送"); await select(user, "登録済み接続先", "受付接続");
    await select(user, "転送を許可する電話受付", "本社受付");
    await user.click(screen.getByRole("button", { name: "転送先の許可範囲の説明" }));
    expect(await screen.findByText(/電話受付の編集画面でポリシーの正確な revision/)).toBeVisible();
    await user.click(screen.getByRole("checkbox", { name: "サポート窓口" }));
    expect(screen.queryByLabelText("固定商品参照キー")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "ポリシー revision を保存" }));
    await waitFor(() => expect(requests.find(r => r.path.endsWith("policy/revisions"))?.body).toMatchObject({ action_type: "telephony.transfer_call", connection_id: "phone-conn", constraints: { route_id: "route", allowed_destination_keys: ["support"] } }));
    expect(requests.some(r => /\/calls|\/execute/.test(r.path))).toBe(false);
  });
  it("recovers a lost policy create response with its original intent before applying an edited name", async () => {
    let lost = false; fail = r => { if (!lost && r.path === "/api/agent-action-policies" && r.method === "POST") { lost = true; return 503; } };
    const user = userEvent.setup(); mount(<EmployeePolicies agentId="agent" canManage />);
    await user.click(screen.getByRole("button", { name: "ポリシーを追加" })); await select(user, "登録済みアクション", "水を注文 — unavailable");
    await user.type(screen.getByLabelText("ポリシー名"), "元の名前"); await select(user, "登録済み接続先", "備品接続");
    await user.type(screen.getByLabelText("固定商品参照キー"), "water-ref"); await user.type(screen.getByLabelText("固定配送先参照キー"), "tokyo-office"); fireEvent.change(screen.getByLabelText("注文総額上限（最小通貨単位・JPYは円）"), { target: { value: "10000" } });
    await user.click(screen.getByRole("button", { name: "ポリシー revision を保存" })); await screen.findByRole("alert");
    await user.clear(screen.getByLabelText("ポリシー名")); await user.type(screen.getByLabelText("ポリシー名"), "変更した名前");
    await user.click(screen.getByRole("button", { name: "ポリシー revision を保存" }));
    await waitFor(() => expect(requests.some(r => r.path.endsWith("policy/revisions"))).toBe(true));
    const creates = requests.filter(r => r.path === "/api/agent-action-policies" && r.method === "POST"); expect(creates).toHaveLength(2); expect(creates[1].body).toEqual(creates[0].body);
    expect(requests.find(r => r.path === "/api/agent-action-policies/policy" && r.method === "PATCH")?.body).toMatchObject({ display_name: "変更した名前", expected_version: 1 });
  });
  it("recovers a lost rule create response before applying edited fields", async () => {
    let lost = false; fail = r => { if (!lost && r.path === "/api/agent-automation/rules" && r.method === "POST") { lost = true; return 503; } };
    const user = userEvent.setup(); mount(<EmployeeAutomation agentId="agent" revisionId="rev1" canManage />);
    await user.click(screen.getByRole("button", { name: "自動化ルールを追加" })); await user.type(screen.getByLabelText("ルール名"), "旧ルール名"); await select(user, "対象のチャット範囲", "備品 Project"); await user.type(screen.getByLabelText("キーワード（1行に1つ）"), "水切れ");
    await user.click(screen.getByRole("button", { name: "ルール revision を保存" })); await screen.findByRole("alert");
    await user.clear(screen.getByLabelText("ルール名")); await user.type(screen.getByLabelText("ルール名"), "新ルール名"); await user.click(screen.getByRole("button", { name: "ルール revision を保存" }));
    await waitFor(() => expect(requests.some(r => r.path.endsWith("rule/revisions"))).toBe(true));
    const creates = requests.filter(r => r.path === "/api/agent-automation/rules" && r.method === "POST"); expect(creates).toHaveLength(2); expect(creates[1].body).toEqual(creates[0].body);
    expect(requests.find(r => r.path === "/api/agent-automation/rules/rule" && r.method === "PATCH")?.body).toMatchObject({ display_name: "新ルール名", expected_version: 1 });
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
