import { expect, test } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";
import { mockPersonaWorkspace } from "./support/persona-workspace-fixture";
import { createOrganizationPending, organizationPendingKey, type OrganizationGraph } from "../src/lib/employee-organization-setup-api";

const owner = "fixture-owner";
const before: OrganizationGraph = { success: true, edges: [], nodes: [{ agent: { id: "director", slug: "director", display_name: "本人が付けた統括名", state: "active" }, revision: { id: "revision", agent_team_id: "team", execution_profile_id: "profile", capability_ceiling: ["workspace_read"] }, organization_profile: { agent_id: "director", job_title: "統括", responsibility_summary: "既存責務", primary_space_id: null, manager_agent_id: "old-manager", manager_user_id: null, autonomy_level: "supervised", employment_state: "active", company_permission_ceiling: {}, created_at: "original", updated_at: "before" } }] };
const manifest = { employees: [{ key: "director", agent_id: "director", display_name: "本人が付けた統括名", job_title: "新しい役職", manager_key: null }] };
const saved: OrganizationGraph = { ...before, nodes: [{ ...before.nodes[0], organization_profile: { ...before.nodes[0].organization_profile, job_title: "新しい役職", manager_agent_id: null, updated_at: "saved" } }] };
for (const width of [1440, 390]) {
  test(`canonical preview import and read-only complete reconciliation at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 }); await addAuthCookie(page); await mockAuthenticatedApis(page); await mockPersonaWorkspace(page);
    const proof = await createOrganizationPending(manifest, before, owner);
    await page.addInitScript(({ key, proof }) => { if (!sessionStorage.getItem("fixture-seeded")) { localStorage.setItem(key, JSON.stringify(proof)); sessionStorage.setItem("fixture-seeded", "1"); } }, { key: organizationPendingKey(owner), proof });
    await page.route("**/api/auth/status", route => route.fulfill({ json: { authenticated: true, user: { id: owner, role: "admin", password_reset_required: false } } }));
    await page.route("**/api/python-proxy/agents", route => route.fulfill({ json: { agents: saved.nodes.map(n => n.agent) } }));
    await page.route("**/api/python-proxy/agents/catalog", route => route.fulfill({ json: { can_manage: true, teams: [{ team_id: "team", name: "既存Team", subagents: [] }], capabilities: [] } }));
    await page.route("**/api/python-proxy/agent-organization", route => route.fulfill({ json: saved }));
    await page.route("**/api/python-proxy/operations/command-center*", route => route.fulfill({ json: { summary: { working: 0, awaiting_approval: 0, uncertain: 0 }, agents: [], current_work: [] } }));
    const writes: string[] = []; const invalidSessionRequests: string[] = []; page.on("request", r => { if (r.method() !== "GET" && r.url().includes("python-proxy")) writes.push(r.url()); if (new URL(r.url()).pathname === "/api/auth/session") invalidSessionRequests.push(r.url()); });
    await page.goto("/operations?tab=agents&panel=organization"); const region = page.getByRole("region", { name: "Agent組織の設定" }); await expect(region).toBeVisible();
    await region.getByLabel("正本scriptのpreview JSONを読み込む").setInputFiles({ name: "canonical-preview.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify({ applied: false, manifest })) });
    await expect(region.getByLabel("Agent組織の設定内容（JSON）")).toHaveValue(/新しい役職/);
    await region.getByRole("button", { name: "同じ設定の保存完了を照合して送信記録を解除" }).click();
    await expect(region.getByText("同じ本人・内容hashの保存結果を全行確認しました。再送せず送信記録を解除しました。")).toBeVisible();
    expect(await page.evaluate(key => localStorage.getItem(key), organizationPendingKey(owner))).toBeNull();
    await page.reload(); await expect(region.getByLabel("正本scriptのpreview JSONを読み込む")).toBeEnabled(); expect(writes).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(invalidSessionRequests).toEqual([]);
  });
}
