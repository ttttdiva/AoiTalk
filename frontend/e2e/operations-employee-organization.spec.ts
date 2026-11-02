import { expect, test } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";
import { mockPersonaWorkspace } from "./support/persona-workspace-fixture";
import { createOrganizationPending, organizationPendingKey, type OrganizationGraph } from "../src/lib/employee-organization-setup-api";

const owner = "fixture-owner";
const before: OrganizationGraph = { success: true, edges: [], nodes: [{ agent: { id: "director", slug: "director", display_name: "本人が付けた統括名", state: "active" }, revision: { id: "revision", legacy: { agent_team_id: "team", execution_profile_id: "profile" }, capability_ceiling: ["workspace_read"] }, organization_profile: { agent_id: "director", job_title: "統括", responsibility_summary: "既存責務", primary_space_id: null, manager_agent_id: "old-manager", manager_user_id: null, autonomy_level: "supervised", employment_state: "active", company_permission_ceiling: {}, created_at: "original", updated_at: "before" } }] };
const manifest = { employees: [{ key: "director", agent_id: "director", display_name: "本人が付けた統括名", job_title: "新しい役職", manager_key: null }] };
const saved: OrganizationGraph = { ...before, nodes: [{ ...before.nodes[0], organization_profile: { ...before.nodes[0].organization_profile, job_title: "新しい役職", manager_agent_id: null, updated_at: "saved" } }] };
for (const width of [1440, 390]) {
  test(`canonical preview import and read-only complete reconciliation at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 }); await addAuthCookie(page); await mockAuthenticatedApis(page); await mockPersonaWorkspace(page);
    const proof = await createOrganizationPending(manifest, before, owner);
    await page.addInitScript(({ key, proof }) => { if (!sessionStorage.getItem("fixture-seeded")) { localStorage.setItem(key, JSON.stringify(proof)); sessionStorage.setItem("fixture-seeded", "1"); } }, { key: organizationPendingKey(owner), proof });
    await page.route("**/api/auth/status", route => route.fulfill({ json: { authenticated: true, user: { id: owner, role: "admin", password_reset_required: false } } }));
    await page.route("**/api/python-proxy/agents", route => route.fulfill({ json: { agents: saved.nodes.map(n => n.agent) } }));
    await page.route("**/api/python-proxy/agents/catalog", route => route.fulfill({ json: { can_manage: true, capabilities: [] } }));
    await page.route("**/api/python-proxy/agent-organization", route => route.fulfill({ json: saved }));
    await page.route("**/api/python-proxy/operations/command-center*", route => route.fulfill({ json: { summary: { working: 0, awaiting_approval: 0, uncertain: 0 }, agents: [], current_work: [] } }));
    const writes: string[] = []; const invalidSessionRequests: string[] = []; page.on("request", r => { if (r.method() !== "GET" && r.url().includes("python-proxy")) writes.push(r.url()); if (new URL(r.url()).pathname === "/api/auth/session") invalidSessionRequests.push(r.url()); });
    await page.goto("/operations?tab=agents&panel=organization"); const region = page.getByRole("region", { name: "Agent組織の設定" }); await expect(region).toBeVisible();
    await region.getByText("詳細・移行用").click();
    await region.getByLabel("設定ファイルを読み込む").setInputFiles({ name: "canonical-preview.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify({ applied: false, manifest })) });
    await expect(region.getByLabel("設定内容（JSON）")).toHaveValue(/新しい役職/);
    await region.getByRole("button", { name: "保存結果を確認" }).click();
    await expect(region.getByText("保存結果を確認しました。")).toBeVisible();
    expect(await page.evaluate(key => localStorage.getItem(key), organizationPendingKey(owner))).toBeNull();
    await page.reload(); await expect(region.getByText("詳細・移行用")).toBeVisible(); expect(writes).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(invalidSessionRequests).toEqual([]);
  });
}


test("structured organization form sends only explicit changes", async ({ page }) => {
  await addAuthCookie(page); await mockAuthenticatedApis(page); await mockPersonaWorkspace(page);
  let current = before; let submitted: unknown;
  await page.route("**/api/auth/status", route => route.fulfill({ json: { authenticated: true, user: { id: owner, role: "admin", password_reset_required: false } } }));
  await page.route("**/api/python-proxy/agents", route => route.fulfill({ json: { agents: before.nodes.map(n => n.agent) } }));
  await page.route("**/api/python-proxy/agents/catalog", route => route.fulfill({ json: { can_manage: true, capabilities: [] } }));
  await page.route("**/api/python-proxy/agent-organization", route => route.fulfill({ json: current }));
  await page.route("**/api/python-proxy/agent-organization/setup", async route => {
    submitted = route.request().postDataJSON(); current = saved;
    await route.fulfill({ json: { success: true, ready: true, blockers: [], employees: { "agent-director": { id: "director" } } } });
  });
  await page.route("**/api/python-proxy/operations/command-center*", route => route.fulfill({ json: { summary: { working: 0, awaiting_approval: 0, uncertain: 0 }, agents: [], current_work: [] } }));
  await page.goto("/operations?tab=agents&panel=organization");
  const region = page.getByRole("region", { name: "Agent組織の設定" });
  await expect(region.getByLabel("設定内容（JSON）")).toBeHidden();
  await region.getByLabel("編集するAgentを追加").click();
  await page.getByRole("option", { name: "本人が付けた統括名" }).click();
  await region.getByLabel("役職").fill("新しい役職");
  await region.getByLabel("直属上司").click();
  await page.getByRole("option", { name: "上司なしにする" }).click();
  await region.getByRole("button", { name: "保存前の内容を確認" }).click();
  await region.getByLabel("保存内容を確認しました").check();
  await region.getByRole("button", { name: "組織を保存", exact: true }).click();
  await expect.poll(() => submitted).toEqual({ employees: [{ key: "agent-director", agent_id: "director", display_name: "本人が付けた統括名", job_title: "新しい役職", manager_key: null }] });
  await expect(region.getByText(/Agent組織の保存記録を読戻しました/)).toBeVisible();
});
