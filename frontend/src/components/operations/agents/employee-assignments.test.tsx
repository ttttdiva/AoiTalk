// @vitest-environment jsdom

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EmployeeAssignments } from "./employee-assignments";
import type { EmployeeAssignment } from "@/lib/agent-employees-api";

vi.mock("@/lib/media-operations-api", () => ({ mediaOperationsApi: { listPersonas: async () => [
  { id: "persona-a", current_revision: { display_name: "広報Persona" } },
  { id: "persona-b", current_revision: { display_name: "調査Persona" } },
] } }));

type Write = { path: string; method: string; body?: Record<string, unknown> };
let rows: EmployeeAssignment[];
let writes: Write[];
let failure: number;
const readLabel = "このPersonaの入力資料の読取を許可（workspace_read）";
const savedRows = () => screen.getByRole("list", { name: "保存済みの所属・権限" });
beforeEach(() => {
  rows = []; writes = []; failure = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const path = url.replace("/api/python-proxy", "/api");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const respond = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
    if (method !== "GET") {
      writes.push({ path, method, body });
      if (failure) return respond({ detail: "private-error-body" }, failure);
    }
    if (path.endsWith("organization-profile")) return respond({ organization_profile: {} });
    if (path === "/api/spaces") return respond({ spaces: [{ id: "space", name: "本社" }] });
    if (path === "/api/projects") return respond({ projects: [{ id: "project", name: "広報Project" }] });
    if (path.includes("persona-operator-assignments")) {
      if (method === "POST" && path.endsWith("/revoke")) rows = rows.map(r => ({ ...r, state: "revoked" }));
      else if (method === "POST") rows.push({ id: "assignment", state: "active", ...body });
      else if (method === "PATCH") rows = rows.map(r => ({ ...r, capability_ceiling: body.capability_ceiling }));
      return respond({ persona_operator_assignments: rows });
    }
    if (path.includes("space-assignments")) return respond({ space_assignments: [] });
    if (path.includes("project-grants")) return respond({ project_grants: [] });
    throw new Error(`Unexpected request ${path}`);
  }));
});
function mount(agentId = "agent", canManage = true) {
  return render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0, shouldRetryOnError: false }}><EmployeeAssignments agentId={agentId} canManage={canManage} /></SWRConfig>);
}
async function select(user: ReturnType<typeof userEvent.setup>, label: string, option: string) {
  await user.click(screen.getByRole("combobox", { name: label }));
  await user.click(await screen.findByRole("option", { name: option }));
}
async function openHelp(user: ReturnType<typeof userEvent.setup>, label: string) {
  await user.click(screen.getByRole("button", { name: `${label}の説明` }));
}
async function openPersona(user: ReturnType<typeof userEvent.setup>) {
  await select(user, "所属・権限の種類", "Persona 担当");
  await select(user, "割り当て先", "広報Persona");
}

describe("Persona担当の明示的な読取上限", () => {
  it("keeps a new assignment empty until the target-specific read checkbox is selected and saved", async () => {
    const user = userEvent.setup(); mount();
    await select(user, "所属・権限の種類", "Persona 担当");
    expect(screen.getByRole("checkbox", { name: readLabel })).toHaveAttribute("aria-disabled", "true");
    await select(user, "割り当て先", "広報Persona");
    expect(screen.getByRole("checkbox", { name: readLabel })).not.toBeChecked();
    expect(screen.getByText("対象Persona: 広報Persona（ID: persona-a）")).toBeVisible();
    await openHelp(user, "Persona読取上限");
    expect(await screen.findByText(/実際の読取にはAgent職務の上限・Project権限/)).toBeVisible();
    expect(writes).toEqual([]);
    await user.click(screen.getByRole("button", { name: "所属・権限を追加" }));
    await waitFor(() => expect(writes).toEqual([{ path: "/api/agents/agent/persona-operator-assignments", method: "POST", body: { persona_id: "persona-a", role: "operator", is_primary: false, capability_ceiling: [] } }]));
    expect(within(savedRows()).getByText("担当の権限上限: 未設定")).toBeVisible();
  });
  it("saves only workspace_read and resets the checkbox after saving", async () => {
    const user = userEvent.setup(); mount(); await openPersona(user);
    await user.click(screen.getByRole("checkbox", { name: readLabel }));
    expect(writes).toEqual([]);
    await user.click(screen.getByRole("button", { name: "所属・権限を追加" }));
    await waitFor(() => expect(writes[0]?.body).toEqual({ persona_id: "persona-a", role: "operator", is_primary: false, capability_ceiling: ["workspace_read"] }));
    expect(await within(savedRows()).findByText("担当の権限上限: workspace_read")).toBeVisible();
    expect(screen.getByRole("checkbox", { name: readLabel })).not.toBeChecked();
  });
  it("does not carry a checked permission to another Persona or assignment kind", async () => {
    const user = userEvent.setup(); mount(); await openPersona(user);
    await user.click(screen.getByRole("checkbox", { name: readLabel }));
    await select(user, "割り当て先", "調査Persona");
    expect(screen.getByRole("checkbox", { name: readLabel })).not.toBeChecked();
    await user.click(screen.getByRole("checkbox", { name: readLabel }));
    await select(user, "所属・権限の種類", "Project 権限");
    await select(user, "所属・権限の種類", "Persona 担当");
    expect(screen.getByRole("checkbox", { name: readLabel })).not.toBeChecked();
    expect(writes).toEqual([]);
  });
  it("edits the existing assignment, keeps other capabilities, and restores the saved value on remount", async () => {
    rows = [{ id: "assignment", persona_id: "persona-a", role: "researcher", state: "active", capability_ceiling: ["web_read"] }];
    const user = userEvent.setup(); const view = mount(); await openPersona(user);
    await user.click(await screen.findByRole("button", { name: "読取上限を編集" }));
    const editor = within(savedRows());
    expect(editor.getByRole("checkbox", { name: readLabel })).not.toBeChecked();
    await user.click(editor.getByRole("checkbox", { name: readLabel }));
    await user.click(editor.getByRole("button", { name: "Persona担当の読取上限を保存" }));
    await waitFor(() => expect(writes).toEqual([{ path: "/api/agents/agent/persona-operator-assignments/assignment", method: "PATCH", body: { capability_ceiling: ["web_read", "workspace_read"], expected_capability_ceiling: ["web_read"] } }]));
    await waitFor(() => expect(editor.queryByRole("button", { name: "取消" })).not.toBeInTheDocument());
    view.unmount(); mount(); await openPersona(user);
    expect(await within(savedRows()).findByText("担当の権限上限: web_read / workspace_read")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "読取上限を編集" }));
    expect(within(savedRows()).getByRole("checkbox", { name: readLabel })).toBeChecked();
  });
  it("cancels an edit without a request and can explicitly save an empty read ceiling", async () => {
    rows = [{ id: "assignment", persona_id: "persona-a", role: "operator", state: "active", capability_ceiling: ["workspace_read"] }];
    const user = userEvent.setup(); mount(); await openPersona(user);
    await user.click(screen.getByRole("button", { name: "読取上限を編集" }));
    await user.click(within(savedRows()).getByRole("checkbox", { name: readLabel }));
    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(writes).toEqual([]);
    expect(screen.getByText("担当の権限上限: workspace_read")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "読取上限を編集" }));
    expect(within(savedRows()).getByRole("checkbox", { name: readLabel })).toBeChecked();
    await user.click(within(savedRows()).getByRole("checkbox", { name: readLabel }));
    await user.click(screen.getByRole("button", { name: "Persona担当の読取上限を保存" }));
    await waitFor(() => expect(writes[0]?.body).toEqual({ capability_ceiling: [], expected_capability_ceiling: ["workspace_read"] }));
    expect(await screen.findByText("担当の権限上限: 未設定")).toBeVisible();
  });
  it.each([403, 409, 422, 503])("retains the saved assignment and lets a failed edit be cancelled (%s)", async status => {
    rows = [{ id: "assignment", persona_id: "persona-a", role: "operator", state: "active", capability_ceiling: [] }];
    failure = status; const user = userEvent.setup(); mount(); await openPersona(user);
    await user.click(screen.getByRole("button", { name: "読取上限を編集" }));
    await user.click(within(savedRows()).getByRole("checkbox", { name: readLabel }));
    await user.click(screen.getByRole("button", { name: "Persona担当の読取上限を保存" }));
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.queryByText("private-error-body")).not.toBeInTheDocument();
    expect(screen.getByText("担当の権限上限: 未設定")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(writes).toHaveLength(1);
  });
  it("revokes without changing the ceiling and prevents editing the inactive history", async () => {
    rows = [{ id: "assignment", persona_id: "persona-a", role: "operator", state: "active", capability_ceiling: ["workspace_read"] }];
    const user = userEvent.setup(); mount(); await openPersona(user);
    await user.click(screen.getByRole("button", { name: "解除" }));
    expect(await screen.findByText("解除済み")).toBeVisible();
    expect(screen.queryByRole("button", { name: "読取上限を編集" })).not.toBeInTheDocument();
    expect(writes).toEqual([{ path: "/api/agents/agent/persona-operator-assignments/assignment/revoke", method: "POST", body: undefined }]);
  });
  it("disables saving and hides editing for a viewer", async () => {
    rows = [{ id: "assignment", persona_id: "persona-a", state: "active", capability_ceiling: [] }];
    const user = userEvent.setup(); mount("agent", false);
    await select(user, "所属・権限の種類", "Persona 担当");
    expect(screen.getByRole("button", { name: "所属・権限を追加" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: readLabel })).toHaveAttribute("aria-disabled", "true");
    expect(screen.queryByRole("button", { name: "読取上限を編集" })).not.toBeInTheDocument();
    expect(writes).toEqual([]);
  });
  it("keeps the neighboring Project grant permissions unchanged", async () => {
    const user = userEvent.setup(); mount();
    await select(user, "所属・権限の種類", "Project 権限");
    await select(user, "割り当て先", "広報Project");
    await user.click(screen.getByRole("button", { name: "所属・権限を追加" }));
    await waitFor(() => expect(writes[0]?.body).toEqual({ project_id: "project", role: "viewer", permissions: { read: true, write: false, delete: false, manage_members: false, manage_settings: false } }));
  });
});
