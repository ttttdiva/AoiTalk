// @vitest-environment jsdom

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EmployeeWorkspace, employeeHref } from "./employee-workspace";
import { EmployeeRole } from "./employee-role";
import { EmployeePolicies } from "./employee-policies";
import { EmployeeAutomation } from "./employee-automation";
import { EmployeeAssignments } from "./employee-assignments";
import { EmployeeLiveInfo, type EmployeeObservability } from "./employee-observability";
import { EmployeeApiError, employeeRequest, type EmployeeCatalog, type EmployeeRevision } from "@/lib/agent-employees-api";

const mocks = vi.hoisted(() => ({ push: vi.fn(), query: new URLSearchParams(), enabled: true, manage: true, fetch: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: mocks.push }), useSearchParams: () => mocks.query }));
vi.mock("@/contexts/runtime-context", () => ({ useOptionalRuntimeContext: () => ({ runtimeFeatures: { application_features: { virtual_company: mocks.enabled, autonomous_agent_runtime: mocks.enabled } } }) }));
vi.mock("../operations-command-center", () => ({ OperationsCommandCenter: ({ agentId }: { agentId?: string }) => <p>読み取り投影 {agentId}</p> }));
vi.mock("./employee-integrations", () => ({ EmployeeIntegrations: () => <p>連携パネル</p>, EmployeePhone: () => <p>電話パネル</p> }));
vi.mock("@/lib/media-operations-api", () => ({ mediaOperationsApi: { listCharacters: async () => [], listPersonas: async () => [] } }));
vi.mock("@/lib/operations-api", () => ({ operationsApi: { listConnections: async () => [{ id: "conn", display_name: "備品接続", provider_key: "procurement" }, { id: "phone-conn", display_name: "受付接続", provider_key: "openai_realtime_sip" }] } }));

const employee = { id: "agent", display_name: "備品担当", slug: "supplies", state: "draft" as const };
const catalog: EmployeeCatalog = { can_manage: true, teams: [
  { team_id: "employee", name: "社員チーム", subagents: [{ subagent_id: "operator", name: "運用担当", capability_ids: ["external_action_propose"] }], execution_profiles: [{ profile_id: "manual", name: "手動モデル" }] },
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
let currentEmployee: { id: string; display_name: string; slug: string; state: string };
let fail: ((request: RequestRecord) => number | undefined) | null;
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
async function backend(url: string, init?: RequestInit) {
  const path = url.replace("/api/python-proxy", "/api"); const method = init?.method ?? "GET";
  const body = init?.body ? JSON.parse(String(init.body)) : undefined;
  const request = { path, method, body }; requests.push(request);
  const status = fail?.(request); if (status) return response({ detail: "echoed-secret-must-never-render" }, status);
  if (path === "/api/agents/catalog") return response({ ...catalog, can_manage: mocks.manage });
  if (path === "/api/operations/command-center") return response({ summary: { working: 0, awaiting_approval: 0, uncertain: 0 }, agents: [] });
  if (path === "/api/agents") return response(method === "POST" ? { agent: currentEmployee } : { agents: [currentEmployee] });
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
  if (path === "/api/integrations/actions") return response({ actions: [definition, { action_type: "telephony.transfer_call", display_name: "通話を転送", status: "unavailable", category: "telephony", required_capabilities: ["telephony_control"], connection_provider_keys: ["openai_realtime_sip"], constraints_schema: null }] });
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
beforeEach(() => {
  vi.restoreAllMocks(); mocks.push.mockReset(); mocks.query = new URLSearchParams("tab=agents"); mocks.enabled = true; mocks.manage = true;
  requests = []; policyRows = []; ruleRows = []; revisionRows = [revision]; assignmentRows = []; fail = null; currentEmployee = { ...employee };
  mocks.fetch.mockImplementation(backend); vi.stubGlobal("fetch", mocks.fetch);
});

describe("AI社員管理と API 境界", () => {
  it("requires both runtime flags, describes startup and Enterprise denial, and sends no management requests", () => {
    mocks.enabled = false; mount(<EmployeeWorkspace />);
    expect(screen.getByText("AI社員機能はこの実行環境で無効です")).toBeVisible();
    expect(screen.getByText(/FEATURE_VIRTUAL_COMPANY=true/)).toHaveTextContent("FEATURE_AUTONOMOUS_AGENT_RUNTIME=true");
    expect(screen.getByText(/Enterprise profile/)).toHaveTextContent("制限を解除できません");
    expect(screen.queryByRole("button", { name: /追加|有効化/ })).not.toBeInTheDocument(); expect(mocks.fetch).not.toHaveBeenCalled();
  });
  it("offers administrator create and resumes a persisted draft using URL selection", async () => {
    const user = userEvent.setup(); mount(<EmployeeWorkspace />);
    expect(await screen.findByRole("button", { name: "＋ AI社員を追加" })).toBeVisible();
    await user.click(await screen.findByRole("button", { name: /備品担当.*下書きの設定を再開/ }));
    expect(mocks.push).toHaveBeenCalledWith("/operations?tab=agents&agent=agent&panel=overview", { scroll: false });
  });
  it("hides administrator writes when the catalog cannot manage", async () => {
    mocks.manage = false; mount(<EmployeeWorkspace />); await screen.findByRole("button", { name: /備品担当/ });
    expect(screen.queryByRole("button", { name: "＋ AI社員を追加" })).not.toBeInTheDocument();
  });
  it("keeps identity on profile failure and retries the same draft without POSTing another identity", async () => {
    const user = userEvent.setup(); let failed = false;
    fail = r => { if (!failed && r.method === "PATCH" && r.path.endsWith("organization-profile")) { failed = true; return 503; } };
    mount(<EmployeeWorkspace />); await user.click(await screen.findByRole("button", { name: "＋ AI社員を追加" }));
    await user.type(screen.getByLabelText("社員名"), "備品担当"); await user.type(screen.getByLabelText("役職"), "調達");
    await user.click(screen.getByRole("button", { name: "下書きを保存して職務設定へ" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("利用できません");
    expect(screen.getByText(/下書き 備品担当 は保存済み/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "下書きを保存して職務設定へ" }));
    await waitFor(() => expect(mocks.push).toHaveBeenCalledWith(employeeHref("agent", "role"), { scroll: false }));
    expect(requests.filter(r => r.path === "/api/agents" && r.method === "POST")).toHaveLength(1);
  });
  it("opens the selected panel directly on reload and sends expected_state on activation", async () => {
    mocks.query = new URLSearchParams("tab=agents&agent=agent&panel=overview"); const user = userEvent.setup(); mount(<EmployeeWorkspace />);
    await user.click(await screen.findByRole("button", { name: "社員を有効化" }));
    await waitFor(() => expect(requests).toContainEqual({ path: "/api/agents/agent/state", method: "PATCH", body: { state: "active", expected_state: "draft" } }));
    await user.click(screen.getByRole("button", { name: "活動・要確認" }));
    expect(mocks.push).toHaveBeenCalledWith(employeeHref("agent", "activity"), { scroll: false });
  });
  it("preserves v1 and POSTs an immutable v2 after review; catalog changes reset incompatible profile", async () => {
    const user = userEvent.setup(); const saved = vi.fn(); mount(<EmployeeRole agent={employee} revisions={[revision]} catalog={catalog} onSaved={saved} />);
    await select(user, "Agent Team", "支援チーム");
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
  it("saves semantic rule with scoped human trigger and exact policy revision defaults", async () => {
    const user = userEvent.setup(); policyRows = [{ id: "policy", display_name: "水の補充", state: "draft", version: 2, current_revision: { id: "policyrev", version: 1, authorization_mode: "bounded_auto" } }];
    mount(<EmployeeAutomation agentId="agent" revisionId="rev1" canManage />);
    await user.click(screen.getByRole("button", { name: "自動化ルールを追加" }));
    await user.type(screen.getByLabelText("ルール名"), "水がない報告"); await select(user, "対象のチャット範囲", "備品 Project"); await select(user, "反応する条件", "意味で判定");
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
    expect(screen.getByText(/Team: employee/)).toBeVisible();
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
    const user = userEvent.setup(); mount(<EmployeeWorkspace />); await user.click(await screen.findByRole("button", { name: "＋ AI社員を追加" }));
    await user.type(screen.getByLabelText("社員名"), "備品担当"); await user.click(screen.getByRole("button", { name: "下書きを保存して職務設定へ" })); await screen.findByRole("alert");
    expect(screen.getByLabelText("社員名")).toBeDisabled(); expect(screen.getByLabelText("slug（任意）")).toBeDisabled();
    await user.type(screen.getByLabelText("社員名"), "変更できない");
    await user.click(screen.getByRole("button", { name: "下書きを保存して職務設定へ" })); await waitFor(() => expect(mocks.push).toHaveBeenCalled());
    const creates = requests.filter(r => r.path === "/api/agents" && r.method === "POST"); expect(creates).toHaveLength(2); expect(creates[1].body).toEqual(creates[0].body);
  });
  it("refreshes list, authority and projection together after lifecycle activation", async () => {
    mocks.query = new URLSearchParams("tab=agents&agent=agent&panel=overview"); const user = userEvent.setup(); mount(<EmployeeWorkspace />);
    await screen.findByText("agent_inactive"); const before = requests.length;
    await user.click(screen.getByRole("button", { name: "社員を有効化" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "社員を一時停止" })).toBeEnabled());
    expect(screen.queryByText("agent_inactive")).not.toBeInTheDocument();
    expect(screen.queryByText("下書きの設定を再開")).not.toBeInTheDocument();
    expect(requests.slice(before).some(r => r.path === "/api/agents")).toBe(true);
    expect(requests.slice(before).some(r => r.path === "/api/operations/command-center")).toBe(true);
    expect(requests.slice(before).some(r => r.path.includes("effective-authority"))).toBe(true);
  });
});
