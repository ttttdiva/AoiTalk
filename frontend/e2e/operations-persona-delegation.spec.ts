import { expect, test } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";
import { mockPersonaWorkspace, personaFixtureBriefs } from "./support/persona-workspace-fixture";
import { delegationContentHash, type PersonaDelegationRequest } from "../src/lib/persona-delegation-request";

for (const mismatch of [false, true]) {
  test(`saved brief creates one bounded manager request and ${mismatch ? "rejects a different GET ID" : "reads its exact receipt"}`, async ({ page }) => {
    await addAuthCookie(page); await mockAuthenticatedApis(page); const brief = personaFixtureBriefs[0];
    await mockPersonaWorkspace(page, { override: async (route, url) => {
      if (!url.pathname.endsWith(`/content-items/${brief.id}`)) return false;
      await route.fulfill({ json: brief }); return true;
    } });
    const agents = ["manager", "worker", "reviewer"].map(id => ({ id, slug: id, display_name: id + "社員", state: "active" }));
    await page.route("**/api/auth/status", route => route.fulfill({ json: { authenticated: true, user: { id: "fixture-owner", role: "admin", password_reset_required: false } } }));
    const invalidSessionRequests: string[] = []; page.on("request", r => { if (new URL(r.url()).pathname === "/api/auth/session") invalidSessionRequests.push(r.url()); });
    await page.route("**/api/python-proxy/agent-organization", route => route.fulfill({ json: { success: true, edges: [], nodes: agents.map(agent => ({ agent, revision: { id: agent.id + "-rev" }, organization_profile: null })) } }));
    await page.route("**/api/python-proxy/operations/command-center*", route => route.fulfill({ json: { agents: agents.map(a => ({ ...a, persona_assignments: [{ persona_id: "persona-0", state: "active" }], project_grants: [{ project_id: "project-0", state: "active", role: "viewer" }] })), current_work: [] } }));
    let writes = 0; let result: Record<string, unknown> | undefined;
    await page.route("**/api/python-proxy/agent-delegations**", async route => {
      if (route.request().method() === "POST") {
        writes++; const body = route.request().postDataJSON() as PersonaDelegationRequest;
        expect(body.project_id).toBe("project-0"); expect(body.persona_id).toBe("persona-0"); expect(body.instruction).toContain(brief.content_hash); expect(body.planning?.brand_keys).toEqual([]); expect(body.planning?.roster).toHaveLength(2);
        result = { ...body, id: "saved-delegation", created_by: "fixture-owner", content_hash: await delegationContentHash(body, "fixture-owner"), round: 1, state: "awaiting_plan", phases: { plan: { state: "pending" } }, artifacts: {}, children: [], readiness: { ready: true }, planning: { enabled: true, max_total_output_tokens: body.planning?.max_total_output_tokens } };
        await route.fulfill({ json: { success: true, delegation: result } });
      } else if (new URL(route.request().url()).pathname.endsWith("/saved-delegation")) await route.fulfill({ json: { success: true, delegation: { ...result, id: mismatch ? "foreign-delegation" : "saved-delegation" } } });
      else await route.fulfill({ json: { success: true, delegations: result ? [result] : [] } });
    });
    await page.goto("/operations?tab=studio&persona=persona-0&stage=brief&content=brief-0");
    const panel = page.getByRole("region", { name: "企画を統括Agentへ依頼" }); await expect(panel).toBeVisible();
    for (const [label, id] of [["統括Agent", "manager"], ["制作担当Agent", "worker"], ["review担当Agent", "reviewer"]]) { await panel.getByLabel(label, { exact: true }).click(); await page.locator('[data-slot="select-content"][data-open]').getByRole("option", { name: id + "社員", exact: true }).click(); await expect(panel.getByRole("combobox", { name: label, exact: true })).toHaveAttribute("aria-expanded", "false"); await expect(page.locator('[data-slot="select-content"][data-open]')).toHaveCount(0); }
    await panel.getByRole("button", { name: "Agentへの依頼内容を確認" }).click(); expect(writes).toBe(0);
    await panel.getByRole("checkbox").check(); await panel.getByRole("button", { name: "確認した企画を統括Agentへ依頼" }).evaluate((b: HTMLButtonElement) => { b.click(); b.click(); });
    if (mismatch) { await expect(panel.getByRole("alert")).toContainText("保存結果を照合できません"); await expect(panel).not.toContainText("保存結果を読戻しました"); }
    else await expect(panel).toContainText("依頼の保存結果を読戻しました");
    expect(writes).toBe(1); await page.reload(); await expect(panel.getByRole("status")).toBeVisible(); expect(writes).toBe(1); await expect(panel.getByRole("button", { name: "Agentへの依頼内容を確認" })).toHaveCount(0);
    expect(invalidSessionRequests).toEqual([]);
  });
}
