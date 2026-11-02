// @vitest-environment jsdom
import { webcrypto } from "node:crypto";
import type { SelectHTMLAttributes } from "react";
// テストでは選択操作を簡潔に再現する。
// eslint-disable-next-line no-restricted-syntax
vi.mock("@/components/ui/app-select", () => ({ AppSelect: (props: SelectHTMLAttributes<HTMLSelectElement>) => <select {...props} /> }));
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { EmployeeOrganizationSetup } from "./employee-organization-setup";
import { createOrganizationPending, employeeOrganizationApi, legacyOrganizationPendingKey, organizationPendingKey, type OrganizationGraph } from "@/lib/employee-organization-setup-api";
import { mediaBrandPortfolioApi } from "@/lib/media-brand-portfolio-api";
import type { EmployeeCatalog } from "@/lib/agent-employees-api";

const catalog = { can_manage: true, capabilities: [] } as EmployeeCatalog;
const before: OrganizationGraph = { success: true, edges: [], nodes: [{
  agent: { id: "worker", slug: "worker", display_name: "担当", state: "active" },
  organization_profile: { agent_id: "worker", job_title: "旧役職", responsibility_summary: "既存責務", manager_agent_id: null, manager_user_id: null, primary_space_id: null, autonomy_level: "supervised", company_permission_ceiling: {}, employment_state: "active" },
  revision: { id: "rev", legacy: { execution_profile_id: "existing-profile" } },
}] };
const body = { employees: [{ key: "agent-worker", agent_id: "worker", display_name: "担当", job_title: "新役職" }] };
const saved: OrganizationGraph = { ...before, nodes: [{ ...before.nodes[0], organization_profile: { ...before.nodes[0].organization_profile, job_title: "新役職" } }] };
const onSaved = vi.fn(async () => undefined);
function mount() { return render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><EmployeeOrganizationSetup catalog={catalog} onSaved={onSaved} onCreate={() => {}} /></SWRConfig>); }
async function ready() { await screen.findByLabelText("編集するAgentを追加"); }

describe("Agent組織の保存と再照合", () => {
  beforeEach(() => {
    localStorage.clear(); onSaved.mockClear();
    Object.defineProperty(globalThis, "crypto", { configurable: true, value: webcrypto });
    Object.defineProperty(navigator, "locks", { configurable: true, value: { request: async (_key: string, callback: () => Promise<void>) => callback() } });
    vi.spyOn(employeeOrganizationApi, "owner").mockResolvedValue("owner");
    vi.spyOn(employeeOrganizationApi, "graph").mockResolvedValue(before);
    vi.spyOn(employeeOrganizationApi, "setup").mockImplementation(() => new Promise(() => {}));
    vi.spyOn(mediaBrandPortfolioApi, "get").mockResolvedValue(undefined as never);
  });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear(); });

  it.each(["none", "v2", "legacy"])("再読込と親の再描画は下書きと%s記録を保持する", async kind => {
    const key = kind === "legacy" ? legacyOrganizationPendingKey : organizationPendingKey("owner");
    const record = kind === "v2" ? JSON.stringify(await createOrganizationPending(body, before, "owner")) : kind === "legacy" ? "legacy-record" : null;
    if (record) localStorage.setItem(key, record);
    const view = mount(); await ready();
    if (record) await screen.findByRole("button", { name: "保存結果を確認" });
    fireEvent.change(screen.getByLabelText("編集するAgentを追加"), { target: { value: "worker" } });
    fireEvent.change(screen.getByLabelText("役職"), { target: { value: "未保存役職" } });
    fireEvent.change(screen.getByLabelText("直属上司"), { target: { value: "none" } });
    const raw = '{"employees":[], "draft":true}';
    fireEvent.change(screen.getByLabelText("設定内容（JSON）"), { target: { value: raw } });
    const calls = vi.mocked(employeeOrganizationApi.graph).mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "組織を再読込" }));
    await waitFor(() => expect(employeeOrganizationApi.graph).toHaveBeenCalledTimes(calls + 1));
    view.rerender(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><EmployeeOrganizationSetup catalog={catalog} onSaved={onSaved} onCreate={() => {}} /></SWRConfig>);
    expect(screen.getByLabelText("役職")).toHaveValue("未保存役職");
    expect(screen.getByLabelText("直属上司")).toHaveValue("none");
    expect(screen.getByLabelText("設定内容（JSON）")).toHaveValue(raw);
    expect(localStorage.getItem(key) === record).toBe(true);
    expect(employeeOrganizationApi.setup).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
    view.unmount(); mount(); await ready();
    expect(localStorage.getItem(key) === record).toBe(true);
    expect(screen.queryByLabelText("役職")).not.toBeInTheDocument();
  });

  it("再読込は確認済みpreviewも保持する", async () => {
    mount(); await ready();
    fireEvent.change(screen.getByLabelText("編集するAgentを追加"), { target: { value: "worker" } });
    fireEvent.change(screen.getByLabelText("役職"), { target: { value: "未保存役職" } });
    fireEvent.click(screen.getByRole("button", { name: "保存前の内容を確認" }));
    fireEvent.click(await screen.findByRole("checkbox", { name: "保存内容を確認しました" }));
    const calls = vi.mocked(employeeOrganizationApi.graph).mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "組織を再読込" }));
    await waitFor(() => expect(employeeOrganizationApi.graph).toHaveBeenCalledTimes(calls + 1));
    expect(screen.getByRole("checkbox", { name: "保存内容を確認しました" })).toBeChecked();
    expect(screen.getByLabelText("保存予定のAgent組織")).toHaveTextContent("未保存役職");
    expect(employeeOrganizationApi.setup).not.toHaveBeenCalled(); expect(onSaved).not.toHaveBeenCalled();
  });

  it("正規GETの再検証でも未保存行を保持し書き込みしない", async () => {
    vi.mocked(employeeOrganizationApi.graph).mockRestore();
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify(before)));
    mount(); await ready();
    fireEvent.change(screen.getByLabelText("編集するAgentを追加"), { target: { value: "worker" } });
    fireEvent.change(screen.getByLabelText("役職"), { target: { value: "未保存役職" } });
    const calls = fetchMock.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "組織を再読込" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(calls + 1));
    await waitFor(() => expect(screen.getByLabelText("役職")).toHaveValue("未保存役職"));
    expect(fetchMock.mock.calls.every(([, options]) => options?.method === "GET")).toBe(true);
    expect(employeeOrganizationApi.setup).not.toHaveBeenCalled(); expect(onSaved).not.toHaveBeenCalled();
  });

  it("構造化フォームの正規本文を送信前に保存し、再マウント後に同じ本文を読取照合する", async () => {
    vi.mocked(employeeOrganizationApi.setup).mockImplementation(() => {
      expect(JSON.parse(localStorage.getItem(organizationPendingKey("owner"))!).body).toEqual(body);
      return new Promise(() => {});
    });
    const view = mount(); await ready();
    fireEvent.change(screen.getByLabelText("編集するAgentを追加"), { target: { value: "worker" } });
    fireEvent.change(screen.getByLabelText("役職"), { target: { value: "新役職" } });
    fireEvent.click(screen.getByRole("button", { name: "保存前の内容を確認" }));
    fireEvent.click(await screen.findByRole("checkbox", { name: "保存内容を確認しました" }));
    fireEvent.click(screen.getByRole("button", { name: "組織を保存" }));
    await waitFor(() => expect(employeeOrganizationApi.setup).toHaveBeenCalledWith(body));
    const record = localStorage.getItem(organizationPendingKey("owner"));
    expect(record).toBeTruthy(); expect(JSON.parse(record!).body).toEqual(body);
    expect(JSON.parse(record!).manifestHash).toMatch(/^[a-f0-9]{64}$/);
    view.unmount();
    vi.mocked(employeeOrganizationApi.graph).mockResolvedValue(saved);
    mount(); await ready();
    fireEvent.click(await screen.findByRole("button", { name: "保存結果を確認" }));
    await waitFor(() => expect(localStorage.getItem(organizationPendingKey("owner"))).toBeNull());
    expect(employeeOrganizationApi.setup).toHaveBeenCalledTimes(1);
    expect(onSaved).toHaveBeenCalledTimes(1);
  });

  it("本文のない旧記録は結果不明のまま残し、再送しない", async () => {
    const proof = await createOrganizationPending(body, before, "owner");
    delete proof.body;
    const raw = JSON.stringify(proof); localStorage.setItem(organizationPendingKey("owner"), raw);
    mount(); await ready();
    fireEvent.click(await screen.findByRole("button", { name: "保存結果を確認" }));
    await screen.findByRole("alert");
    expect(localStorage.getItem(organizationPendingKey("owner"))).toBe(raw);
    expect(employeeOrganizationApi.setup).not.toHaveBeenCalled(); expect(onSaved).not.toHaveBeenCalled();
  });

  it("別ownerの記録を読まず変更しない", async () => {
    const raw = JSON.stringify(await createOrganizationPending(body, before, "owner"));
    localStorage.setItem(organizationPendingKey("owner"), raw);
    vi.mocked(employeeOrganizationApi.owner).mockResolvedValue("other");
    mount(); await ready();
    await waitFor(() => expect(screen.getByRole("button", { name: "保存前の内容を確認" })).toBeEnabled());
    expect(screen.queryByRole("button", { name: "保存結果を確認" })).not.toBeInTheDocument();
    expect(localStorage.getItem(organizationPendingKey("owner"))).toBe(raw);
    expect(employeeOrganizationApi.setup).not.toHaveBeenCalled();
  });
});
