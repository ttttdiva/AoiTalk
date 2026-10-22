import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";
import { mockPersonaWorkspace } from "./support/persona-workspace-fixture";
import type { EmployeeAssignment } from "../src/lib/agent-employees-api";

const agent = { id: "ceiling-employee", display_name: "広報社員", slug: "ceiling-employee", state: "draft" };
const endpoint = `/api/python-proxy/agents/${agent.id}/persona-operator-assignments`;
const readLabel = "このPersonaの入力資料の読取を許可（workspace_read）";
async function select(page: Page, label: string, value: string) {
  await page.getByRole("combobox", { name: label }).click();
  await page.getByRole("option", { name: value, exact: true }).click();
}

for (const width of [1440, 390]) {
  test(`Persona read ceiling requires explicit save and preserves existing grants at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await addAuthCookie(page);
    await mockAuthenticatedApis(page); await mockPersonaWorkspace(page);
    let rows: EmployeeAssignment[] = [{ id: "existing", persona_id: "persona-a", role: "researcher", state: "active", capability_ceiling: ["web_read"] }];
    const writes: { path: string; method: string; body: Record<string, unknown> | null }[] = [];
    const errors: string[] = [];
    let rejectNextEdit = false;
    page.on("pageerror", error => errors.push(error.message));
    // All API traffic is a browser fixture; no grant or account is written to a database.
    await page.route("**/api/**", async route => {
      const request = route.request(); const path = new URL(request.url()).pathname;
      const method = request.method(); const body = request.postDataJSON();
      if (method !== "GET") writes.push({ path, method, body });
      if (path === endpoint && method === "POST") {
        rows.push({ ...body, id: "created", state: "active" });
        return route.fulfill({ status: 201, json: { persona_operator_assignment: rows.at(-1) } });
      }
      if (path.startsWith(`${endpoint}/`) && method === "PATCH") {
        if (rejectNextEdit) { rejectNextEdit = false; return route.fulfill({ status: 422, json: { detail: "private-error-body" } }); }
        rows = rows.map(r => path.endsWith(`/${r.id}`) ? { ...r, capability_ceiling: body.capability_ceiling } : r);
        return route.fulfill({ json: { persona_operator_assignment: rows.find(r => path.endsWith(`/${r.id}`)) } });
      }
      if (path.startsWith(`${endpoint}/`) && path.endsWith("/revoke")) {
        rows = rows.map(r => path === `${endpoint}/${r.id}/revoke` ? { ...r, state: "revoked" } : r);
        return route.fulfill({ json: { success: true } });
      }
      if (path === endpoint) return route.fulfill({ json: { persona_operator_assignments: rows } });
      if (path === "/api/auth/status") return route.fulfill({ json: { authenticated: true, user: { id: "fixture-owner", role: "admin", password_reset_required: false } } });
      if (path === "/api/python-proxy/runtime/features") return route.fulfill({ json: { features: {}, application_features: { virtual_company: true, autonomous_agent_runtime: true } } });
      if (path === "/api/python-proxy/agents/catalog") return route.fulfill({ json: { can_manage: true, teams: [], capabilities: [] } });
      if (path === "/api/python-proxy/agents") return route.fulfill({ json: { agents: [agent] } });
      if (path === `/api/python-proxy/agents/${agent.id}`) return route.fulfill({ json: { agent } });
      if (path.endsWith("/organization-profile")) return route.fulfill({ json: { organization_profile: {} } });
      if (path.endsWith("/revisions")) return route.fulfill({ json: { revisions: [] } });
      if (path.endsWith("/space-assignments")) return route.fulfill({ json: { space_assignments: [] } });
      if (path.endsWith("/project-grants")) return route.fulfill({ json: { project_grants: [] } });
      if (path.endsWith("/command-center")) return route.fulfill({ json: { summary: { working: 0, awaiting_approval: 0, uncertain: 0 }, agents: [], current_work: [] } });
      if (path === "/api/projects") return route.fulfill({ json: { projects: [{ id: "project", name: "広報Project" }] } });
      if (path === "/api/spaces") return route.fulfill({ json: { spaces: [] } });
      if (path === "/api/python-proxy/operations/media/personas") return route.fulfill({ json: [
        { id: "persona-a", current_revision: { display_name: "広報Persona" } },
        { id: "persona-b", current_revision: { display_name: "調査Persona" } },
      ] });
      return route.fallback();
    });
    await page.goto(`/operations?tab=agents&agent=${agent.id}&panel=assignments`);
    await select(page, "所属・権限の種類", "Persona 担当");
    await expect(page.getByRole("checkbox", { name: readLabel })).not.toBeChecked();
    await select(page, "割り当て先", "調査Persona");
    await expect(page.getByText("対象Persona: 調査Persona（ID: persona-b）")).toBeVisible();
    await page.getByRole("checkbox", { name: readLabel }).check();
    expect(writes).toEqual([]);
    await page.getByRole("button", { name: "所属・権限を追加" }).click();
    await expect.poll(() => writes[0]?.body).toEqual({ persona_id: "persona-b", role: "operator", is_primary: false, capability_ceiling: ["workspace_read"] });
    await expect(page.getByRole("checkbox", { name: readLabel })).not.toBeChecked();
    const list = page.getByRole("list", { name: "保存済みの所属・権限" });
    const existing = list.getByRole("listitem").filter({ hasText: "広報Persona" });
    await existing.getByRole("button", { name: "読取上限を編集" }).click();
    await existing.getByRole("checkbox", { name: readLabel }).check();
    await existing.getByRole("button", { name: "取消" }).click();
    expect(writes).toHaveLength(1);
    await existing.getByRole("button", { name: "読取上限を編集" }).click();
    await expect(existing.getByRole("checkbox", { name: readLabel })).not.toBeChecked();
    await existing.getByRole("checkbox", { name: readLabel }).check();
    rejectNextEdit = true;
    await existing.getByRole("button", { name: "Persona担当の読取上限を保存" }).click();
    await expect(existing.getByRole("alert")).toBeVisible();
    await expect(existing.getByText("担当の権限上限: web_read", { exact: true })).toBeVisible();
    await existing.getByRole("button", { name: "Persona担当の読取上限を保存" }).click();
    await expect(existing.getByRole("button", { name: "取消" })).toHaveCount(0);
    expect(writes.at(-1)).toEqual({ path: `${endpoint}/existing`, method: "PATCH", body: { capability_ceiling: ["web_read", "workspace_read"], expected_capability_ceiling: ["web_read"] } });
    await page.reload(); await select(page, "所属・権限の種類", "Persona 担当");
    await expect(existing.getByText("担当の権限上限: web_read / workspace_read")).toBeVisible();
    await existing.getByRole("button", { name: "解除" }).click();
    await expect(existing.getByText("解除済み")).toBeVisible();
    await expect(existing.getByRole("button", { name: "読取上限を編集" })).toHaveCount(0);
    await select(page, "所属・権限の種類", "Project 権限"); await select(page, "割り当て先", "広報Project");
    await page.getByRole("button", { name: "所属・権限を追加" }).click();
    await expect.poll(() => writes.at(-1)?.body).toEqual({ project_id: "project", role: "viewer", permissions: { read: true, write: false, delete: false, manage_members: false, manage_settings: false } });
    expect(errors).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}
