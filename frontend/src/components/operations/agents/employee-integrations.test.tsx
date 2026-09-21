// @vitest-environment jsdom

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EmployeeIntegrations, EmployeePhone } from "./employee-integrations";
import { EmployeeApiError, employeeRequest } from "@/lib/agent-employees-api";
import { operationsApi } from "@/lib/operations-api";

vi.mock("@/lib/agent-employees-api", async importOriginal => ({
  ...await importOriginal<typeof import("@/lib/agent-employees-api")>(),
  employeeRequest: vi.fn(), employeeKey: () => "request-key",
}));
vi.mock("@/lib/operations-api", () => ({ operationsApi: { listConnections: vi.fn() } }));

const props = { agentId: "agent-1", revisionId: "revision-2", canManage: true };
const connection = { id: "connection-1", display_name: "受付用接続", provider_key: "openai_realtime_sip" };
const credentialPath = "/api/integrations/connections/connection-1/credential";
const routePath = "/api/telephony/routes/route-1";
const revisionsPath = "/api/agents/agent-1/revisions";
const phoneRevisionLabel = "v1 · 電話担当 · employee / realtime";
const revisionFixtures = [
  { id: "revision-1", agent_id: "agent-1", version: 1, display_name: "電話担当", agent_team_id: "employee", execution_profile_id: "realtime" },
  { id: "revision-2", agent_id: "agent-1", version: 2, display_name: "チャット担当", agent_team_id: "employee", execution_profile_id: "manual" },
];
const catalog = {
  provider: "openai_realtime_sip", provider_status: "unverified",
  route_keys: [{ key: "office-main", display_name: "本社受付", called_number_masked: "***1234", connection_id: "connection-1",
    destination_keys: [{ key: "support", display_name: "サポート窓口" }, { key: "sales", display_name: "営業窓口" }] }],
};
const routeFixture = {
  id: "route-1", display_name: "本社の電話", state: "draft", version: 4, provider: "openai_realtime_sip",
  connection_id: "connection-1", provider_route_ref: "office-main", called_number_masked: "***1234",
  agent_id: "agent-1", agent_revision_id: "revision-1", timezone: "Asia/Tokyo",
  business_hours_json: { days: [0, 1, 2, 3, 4], start: "09:00", end: "17:00" },
  greeting_override: "お電話ありがとうございます", transfer_policy_json: {}, fallback_mode: "reject",
};
const policy = {
  id: "policy-1", agent_id: "agent-1", display_name: "受付転送", state: "active", version: 9,
  current_revision: { id: "policy-revision-9", action_type: "telephony.transfer_call", connection_id: "connection-1",
    authorization_mode: "bounded_auto", constraints: { allowed_destination_keys: ["support"], route_id: "route-1" } },
};
type Credential = { id: string; revision: number; status: string; verified_at: string | null };
let credential: Credential | null;
let routes: typeof routeFixture[];
let policies: typeof policy[];
let currentCatalog: typeof catalog;
let ready: boolean;
let employeeRevisions: typeof revisionFixtures;
const request = vi.mocked(employeeRequest);
async function selectOption(user: ReturnType<typeof userEvent.setup>, control: HTMLElement, label: string) {
  await user.click(control);
  await user.click(await screen.findByRole("option", { name: label }));
}
function envelope() {
  return { credential, readiness: { ready: credential?.status === "verified", status: credential?.status ?? "not_configured", reason_code: "ready" } };
}
async function backend(path: string, method = "GET", body?: unknown): Promise<unknown> {
  if (path === revisionsPath) return { revisions: employeeRevisions };
  if (path === "/api/telephony/catalog") return currentCatalog;
  if (path === "/api/agent-action-policies?agent_id=agent-1") return { success: true, policies };
  if (path === "/api/telephony/routes?agent_id=agent-1") return { routes };
  if (path === credentialPath) {
    if (method !== "GET") credential = { id: "credential-1", revision: (credential?.revision ?? 0) + 1, status: "verification_pending", verified_at: null };
    return envelope();
  }
  if (path === `${credentialPath}/verify` || path === `${credentialPath}/disable`) {
    credential = { ...credential!, revision: credential!.revision + 1, status: path.endsWith("verify") ? "verified" : "disabled" };
    return envelope();
  }
  if (path === `${credentialPath}/audit`) return { items: [{ id: "audit-1", revision: 3, event_type: "verify", created_at: "2026-09-08T00:00:00Z", payload: "never-render-audit-secret" }] };
  if (path === routePath) {
    if (method === "PATCH") routes = [{ ...routes[0], ...body as Partial<typeof routeFixture>, version: routes[0].version + 1 }];
    return { route: routes[0] };
  }
  if (path === `${routePath}/readiness`) return { ready, reason_codes: ready ? [] : ["provider_unavailable"], provider_status: "unverified", external_setup: "unverified" };
  if (path === "/api/telephony/calls?route_id=route-1") return { calls: [{ id: "call-1", state: "uncertain", caller_masked: "***5678", called_masked: "***1234", received_at: "2026-09-08T00:00:00Z", caller: "+819012345678", webhook_secret: "never-render-call-secret" }] };
  if (path === "/api/telephony/routes" && method === "POST") return { route: { ...routeFixture, ...body as object } };
  if (path === `${routePath}/state`) {
    routes = [{ ...routes[0], state: (body as { state: string }).state, version: routes[0].version + 1 }];
    return { route: routes[0] };
  }
  throw new Error(`Unexpected test request: ${method} ${path}`);
}
beforeEach(() => {
  vi.clearAllMocks();
  credential = null; routes = []; policies = []; currentCatalog = structuredClone(catalog); ready = false;
  employeeRevisions = structuredClone(revisionFixtures);
  vi.mocked(operationsApi.listConnections).mockResolvedValue([connection]);
  request.mockImplementation(backend as typeof employeeRequest);
});

describe("EmployeeIntegrations", () => {
  it("clears both write-only secrets before the upload finishes and never renders an error body", async () => {
    const user = userEvent.setup();
    let rejectUpload!: (cause: unknown) => void;
    request.mockImplementation((async (path, method, body) => {
      if (path === credentialPath && method === "POST") return new Promise((_resolve, reject) => { rejectUpload = reject; });
      return backend(path, method, body);
    }) as typeof employeeRequest);
    const storage = vi.spyOn(Storage.prototype, "setItem");
    render(<EmployeeIntegrations {...props} />);
    const apiKey = await screen.findByLabelText("APIキー");
    const webhook = screen.getByLabelText("Webhookシークレット（任意）");
    expect(apiKey).toHaveAttribute("type", "password");
    await user.type(apiKey, "private-api-key");
    await user.type(webhook, "private-webhook-secret");
    await user.click(screen.getByRole("button", { name: "認証情報を登録" }));
    expect(apiKey).toHaveValue(""); expect(webhook).toHaveValue("");
    expect(request).toHaveBeenCalledWith(credentialPath, "POST", {
      credential_kind: "api_key", payload: { api_key: "private-api-key", webhook_secret: "private-webhook-secret" }, expected_revision: 0,
    });
    await act(async () => rejectUpload(Object.assign(new Error("private-api-key private-webhook-secret"), { status: 422, body: { api_key: "private-api-key" } })));
    expect(await screen.findByRole("alert")).toHaveTextContent("入力内容または設定");
    expect(document.body).not.toHaveTextContent("private-api-key");
    expect(document.body).not.toHaveTextContent("private-webhook-secret");
    expect(storage).not.toHaveBeenCalled();
    storage.mockRestore();
  });

  it("replaces then verifies and disables using the latest revision, and shows safe audit fields", async () => {
    credential = { id: "credential-1", revision: 2, status: "verification_pending", verified_at: null };
    const user = userEvent.setup();
    render(<EmployeeIntegrations {...props} />);
    await user.type(await screen.findByLabelText("APIキー"), "replacement-value");
    await user.click(screen.getByRole("button", { name: "認証情報を差し替え" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith(credentialPath, "PUT", { credential_kind: "api_key", payload: { api_key: "replacement-value" }, expected_revision: 2 }));
    expect(screen.getByLabelText("APIキー")).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "認証情報を検証" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith(`${credentialPath}/verify`, "POST", { expected_revision: 3 }));
    expect(await screen.findByText(/リビジョン: 4/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "認証情報を無効化" }));
    expect(request).toHaveBeenCalledWith(`${credentialPath}/disable`, "POST", { expected_revision: 4 });
    expect(await screen.findByText(/無効化済み/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "認証情報の操作履歴" }));
    expect(await screen.findByText(/検証 · v3/)).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("never-render-audit-secret");
  });

  it("requires reload after a revision conflict and uses the reloaded revision", async () => {
    credential = { id: "credential-1", revision: 2, status: "verification_pending", verified_at: null };
    let conflict = true;
    request.mockImplementation((async (path, method, body) => {
      if (path.endsWith("/verify") && conflict) { conflict = false; credential!.revision = 8; throw new EmployeeApiError(409); }
      return backend(path, method, body);
    }) as typeof employeeRequest);
    const user = userEvent.setup();
    render(<EmployeeIntegrations {...props} />);
    await user.click(await screen.findByRole("button", { name: "認証情報を検証" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("再読み込み");
    expect(screen.getByRole("button", { name: "認証情報を検証" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "認証情報の操作履歴" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "再読み込み" }));
    await user.click(await screen.findByRole("button", { name: "認証情報を検証" }));
    expect(request).toHaveBeenLastCalledWith(`${credentialPath}/verify`, "POST", { expected_revision: 8 });
  });

  it("does not expose credential inputs or mutations to read-only viewers", async () => {
    credential = { id: "credential-1", revision: 2, status: "verified", verified_at: null };
    render(<EmployeeIntegrations {...props} canManage={false} />);
    expect(await screen.findByText(/登録済み（非表示）/)).toBeInTheDocument();
    expect(screen.queryByLabelText("APIキー")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "認証情報を検証" })).not.toBeInTheDocument();
    expect(request.mock.calls.every(([, method]) => !method || method === "GET")).toBe(true);
  });

  it("drops entered secrets when switching connections", async () => {
    const user = userEvent.setup();
    vi.mocked(operationsApi.listConnections).mockResolvedValue([connection, { ...connection, id: "connection-2", display_name: "別の接続" }]);
    request.mockImplementation((async (path, method, body) => path.includes("/connection-2/") ? envelope() : backend(path, method, body)) as typeof employeeRequest);
    render(<EmployeeIntegrations {...props} />);
    await user.type(await screen.findByLabelText("APIキー"), "do-not-carry-over");
    await selectOption(user, screen.getByRole("combobox", { name: "接続" }), "別の接続（openai_realtime_sip）");
    expect(await screen.findByLabelText("APIキー")).toHaveValue("");
  });
});

describe("EmployeePhone", () => {
  it("shows setup required for an empty catalog without arbitrary number or URI fields", async () => {
    currentCatalog.route_keys = [];
    render(<EmployeePhone {...props} />);
    expect(await screen.findByText(/登録済みの電話回線がありません/)).toBeInTheDocument();
    expect(screen.getByText(/PSTN.*未検証/)).toBeInTheDocument();
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/電話番号|URI/)).not.toBeInTheDocument();
  });

  it("creates a draft using only catalog references, structured hours, and an exact transfer policy revision", async () => {
    policies = [policy, { ...policy, id: "wrong", display_name: "別接続のポリシー", current_revision: { ...policy.current_revision, id: "wrong-revision", connection_id: "connection-other" } }];
    const user = userEvent.setup();
    render(<EmployeePhone {...props} />);
    const form = within(await screen.findByRole("form", { name: "電話受付を設定" }));
    await user.type(form.getByLabelText("受付の表示名"), "新しい受付");
    await selectOption(user, form.getByRole("combobox", { name: "登録済み回線" }), "本社受付（***1234）");
    expect(form.getByRole("button", { name: "下書きとして保存" })).toBeDisabled();
    expect(form.getByRole("combobox", { name: "電話で使用する役割リビジョン" })).toHaveTextContent("役割リビジョンを選択");
    await selectOption(user, form.getByRole("combobox", { name: "電話で使用する役割リビジョン" }), phoneRevisionLabel);
    await user.click(form.getByRole("checkbox", { name: "終日受付" }));
    await selectOption(user, form.getByRole("combobox", { name: "転送ポリシー" }), "受付転送");
    expect(form.getByRole("button", { name: "下書きとして保存" })).toBeDisabled();
    expect(form.queryByRole("option", { name: "別接続のポリシー" })).not.toBeInTheDocument();
    expect(form.queryByRole("checkbox", { name: "営業窓口" })).not.toBeInTheDocument();
    await user.click(form.getByRole("checkbox", { name: "サポート窓口" }));
    await user.type(form.getByLabelText("受付の挨拶（任意）"), "こんにちは");
    await user.click(form.getByRole("button", { name: "下書きとして保存" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("/api/telephony/routes", "POST", {
      display_name: "新しい受付", connection_id: "connection-1", provider_route_ref: "office-main",
      agent_id: "agent-1", agent_revision_id: "revision-1", timezone: "Asia/Tokyo",
      business_hours_json: { days: [0, 1, 2, 3, 4], start: "09:00", end: "17:00" }, greeting_override: "こんにちは",
      transfer_policy_json: { action_policy_revision_id: "policy-revision-9", destination_keys: ["support"] },
      fallback_mode: "reject", idempotency_key: "request-key",
    }));
    expect(request.mock.calls.some(([path]) => path.endsWith("/state"))).toBe(false);
  });

  it("validates business hours and requires an employee revision", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<EmployeePhone {...props} />);
    await user.type(await screen.findByLabelText("受付の表示名"), "受付");
    await selectOption(user, screen.getByRole("combobox", { name: "登録済み回線" }), "本社受付（***1234）");
    await selectOption(user, screen.getByRole("combobox", { name: "電話で使用する役割リビジョン" }), phoneRevisionLabel);
    await user.click(screen.getByRole("checkbox", { name: "終日受付" }));
    fireEvent.change(screen.getByLabelText("終了時刻"), { target: { value: "08:00" } });
    await user.click(screen.getByRole("button", { name: "下書きとして保存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("終了時刻を開始時刻より後");
    expect(request.mock.calls.some(([, method]) => method === "POST")).toBe(false);
    rerender(<EmployeePhone {...props} revisionId={null} />);
    expect(await screen.findByRole("button", { name: "下書きとして保存" })).toBeDisabled();
  });

  it("updates route configuration with expected_version and shows masked calls with honest readiness", async () => {
    routes = [structuredClone(routeFixture)]; policies = [policy];
    const user = userEvent.setup();
    render(<EmployeePhone {...props} />);
    const card = within(await screen.findByRole("article", { name: "本社の電話" }));
    expect(await card.findByText(/\*\*\*5678 → \*\*\*1234 · 要確認/)).toBeInTheDocument();
    expect(card.getByRole("button", { name: "受付を有効化" })).toBeDisabled();
    expect(document.body).not.toHaveTextContent("+819012345678");
    expect(document.body).not.toHaveTextContent("never-render-call-secret");
    await user.click(card.getByRole("button", { name: "電話受付を編集" }));
    const form = within(card.getByRole("form", { name: "電話受付を編集" }));
    await waitFor(() => expect(form.getByRole("combobox", { name: "電話で使用する役割リビジョン" })).toHaveTextContent(phoneRevisionLabel));
    await user.clear(form.getByLabelText("受付の表示名"));
    await user.type(form.getByLabelText("受付の表示名"), "更新した受付");
    await user.clear(form.getByLabelText("受付の挨拶（任意）"));
    await user.type(form.getByLabelText("受付の挨拶（任意）"), "新しいご挨拶");
    await user.click(form.getByRole("button", { name: "電話受付を更新" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith(routePath, "PATCH", expect.objectContaining({ display_name: "更新した受付", greeting_override: "新しいご挨拶", expected_version: 4, agent_revision_id: "revision-1" })));
    const update = request.mock.calls.find(([path, method]) => path === routePath && method === "PATCH")![2] as object;
    expect(update).not.toHaveProperty("idempotency_key");
  });

  it("activates, pauses, and retires through state commands using the latest route version", async () => {
    routes = [structuredClone(routeFixture)]; ready = true;
    const user = userEvent.setup();
    render(<EmployeePhone {...props} />);
    await user.click(await screen.findByRole("button", { name: "受付を有効化" }));
    expect(request).toHaveBeenCalledWith(`${routePath}/state`, "POST", { state: "active", expected_version: 4 });
    await user.click(await screen.findByRole("button", { name: "受付を一時停止" }));
    expect(request).toHaveBeenCalledWith(`${routePath}/state`, "POST", { state: "paused", expected_version: 5 });
    await user.click(await screen.findByRole("button", { name: "受付を終了" }));
    expect(request).toHaveBeenCalledWith(`${routePath}/state`, "POST", { state: "retired", expected_version: 6 });
    await screen.findByText(/終了 · v7/);
    expect(screen.queryByRole("button", { name: "電話受付を編集" })).not.toBeInTheDocument();
  });

  it("does not silently overwrite a conflicting route edit", async () => {
    routes = [structuredClone(routeFixture)]; ready = true;
    request.mockImplementation((async (path, method, body) => {
      if (path === routePath && method === "PATCH") throw new EmployeeApiError(409);
      return backend(path, method, body);
    }) as typeof employeeRequest);
    const user = userEvent.setup();
    render(<EmployeePhone {...props} />);
    await user.click(await screen.findByRole("button", { name: "電話受付を編集" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "電話受付を更新" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "電話受付を更新" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("再読み込み");
    expect(screen.getByRole("button", { name: "電話受付を更新" })).toBeDisabled();
    expect(request.mock.calls.filter(([, method]) => method === "PATCH")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "再読み込み" }));
    await screen.findByRole("button", { name: "電話受付を編集" });
    expect(screen.queryByRole("button", { name: "電話受付を更新" })).not.toBeInTheDocument();
  });

  it("keeps read-only phone panels free of mutation controls", async () => {
    routes = [structuredClone(routeFixture)]; ready = true;
    render(<EmployeePhone {...props} canManage={false} />);
    await screen.findByText(/\*\*\*5678 →/);
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "受付を有効化" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "受付を終了" })).not.toBeInTheDocument();
  });

  it("allows activation when inactivity is the only readiness reason", async () => {
    routes = [structuredClone(routeFixture)];
    request.mockImplementation((async (path, method, body) => path === `${routePath}/readiness`
      ? { ready: false, reason_codes: ["telephony_route_inactive"], provider_status: "unverified", external_setup: "unverified" }
      : backend(path, method, body)) as typeof employeeRequest);
    const user = userEvent.setup();
    render(<EmployeePhone {...props} />);
    await user.click(await screen.findByRole("button", { name: "受付を有効化" }));
    expect(request).toHaveBeenCalledWith(`${routePath}/state`, "POST", { state: "active", expected_version: 4 });
  });

  it("does not keep stale activation controls after readiness refresh fails", async () => {
    routes = [structuredClone(routeFixture)]; ready = true;
    let failReadiness = false;
    request.mockImplementation((async (path, method, body) => {
      if (path === `${routePath}/readiness` && failReadiness) throw new EmployeeApiError(503);
      return backend(path, method, body);
    }) as typeof employeeRequest);
    const user = userEvent.setup();
    render(<EmployeePhone {...props} />);
    expect(await screen.findByRole("button", { name: "受付を有効化" })).toBeEnabled();
    failReadiness = true;
    await user.click(screen.getByRole("button", { name: "受付状態・通話履歴を更新" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("現在利用できません");
    expect(screen.queryByRole("button", { name: "受付を有効化" })).not.toBeInTheDocument();
  });

  it("changes an existing route revision only after explicit selection with profile labels", async () => {
    routes = [structuredClone(routeFixture)];
    employeeRevisions.push({ ...revisionFixtures[0], id: "revision-3", version: 3, display_name: "新しい電話担当" });
    const user = userEvent.setup();
    render(<EmployeePhone {...props} />);
    await user.click(await screen.findByRole("button", { name: "電話受付を編集" }));
    const form = within(screen.getByRole("form", { name: "電話受付を編集" }));
    await waitFor(() => expect(form.getByRole("button", { name: "電話受付を更新" })).toBeEnabled());
    await user.click(form.getByRole("combobox", { name: "電話で使用する役割リビジョン" }));
    expect(await screen.findByRole("option", { name: "v2 · チャット担当 · employee / manual" })).toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "v3 · 新しい電話担当 · employee / realtime" }));
    await user.click(form.getByRole("button", { name: "電話受付を更新" }));
    expect(request).toHaveBeenCalledWith(routePath, "PATCH", expect.objectContaining({ agent_revision_id: "revision-3", expected_version: 4 }));
  });

  it("does not replace a missing bound revision with the latest revision", async () => {
    routes = [structuredClone(routeFixture)]; employeeRevisions = [revisionFixtures[1]];
    const user = userEvent.setup();
    render(<EmployeePhone {...props} />);
    await user.click(await screen.findByRole("button", { name: "電話受付を編集" }));
    const form = within(screen.getByRole("form", { name: "電話受付を編集" }));
    await waitFor(() => expect(form.getByRole("combobox", { name: "電話で使用する役割リビジョン" })).toBeEnabled());
    expect(form.getByRole("combobox", { name: "電話で使用する役割リビジョン" })).toHaveTextContent("設定済みのリビジョン（revision-1）");
    expect(form.getByRole("button", { name: "電話受付を更新" })).toBeDisabled();
    expect(request.mock.calls.some(([, method]) => method === "PATCH")).toBe(false);
  });

  it("blocks saving on revision load failure and allows scoped retry", async () => {
    currentCatalog.route_keys = [];
    routes = [structuredClone(routeFixture)];
    let fail = true;
    request.mockImplementation((async (path, method, body) => {
      if (path === revisionsPath && fail) throw new EmployeeApiError(503);
      return backend(path, method, body);
    }) as typeof employeeRequest);
    const user = userEvent.setup();
    render(<EmployeePhone {...props} />);
    await user.click(await screen.findByRole("button", { name: "電話受付を編集" }));
    const form = within(screen.getByRole("form", { name: "電話受付を編集" }));
    expect(await form.findByRole("alert")).toHaveTextContent("現在利用できません");
    expect(form.getByRole("button", { name: "電話受付を更新" })).toBeDisabled();
    fail = false;
    await user.click(form.getByRole("button", { name: "再読み込み" }));
    await waitFor(() => expect(form.getByRole("combobox", { name: "電話で使用する役割リビジョン" })).toHaveTextContent(phoneRevisionLabel));
    expect(form.queryByRole("alert")).not.toBeInTheDocument();
  });
});
