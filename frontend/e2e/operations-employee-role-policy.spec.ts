import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";
import { mockPersonaWorkspace } from "./support/persona-workspace-fixture";
import type { EmployeeRevision } from "../src/lib/agent-employees-api";

const agent = { id: "role-employee", display_name: "広報社員", slug: "role-employee", state: "draft" };
const endpoint = `/api/python-proxy/agents/${agent.id}/revisions`;
async function select(page: Page, label: string, value: string) {
  await page.getByRole("combobox", { name: label }).click();
  await page.getByRole("option", { name: value, exact: true }).click();
}
async function save(page: Page, version: number) {
  await page.getByRole("checkbox", { name: "変更内容を確認しました" }).check();
  await page.getByRole("button", { name: `新しい職務 revision v${version} を保存` }).click();
  await expect(page.getByRole("heading", { name: `職務・モデル — 現在 v${version}` })).toBeVisible();
}

for (const width of [1440, 390]) {
  test(`role revisions preserve unset policies until explicitly edited at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await addAuthCookie(page); await mockAuthenticatedApis(page); await mockPersonaWorkspace(page);
    let revisions: EmployeeRevision[] = [{ id: "rev1", agent_id: agent.id, version: 1, display_name: agent.display_name,
      mission: "提供資料の整理", responsibility_summary: "資料読取", operational_instructions: "根拠を確認",
      agent_team_id: "employee", execution_profile_id: "manual", allowed_subagent_ids: ["operator"], capability_ceiling: [],
      budget_policy: {}, concurrency_policy: {}, wake_policy: { mode: "manual" } }];
    const writes: Record<string, unknown>[] = []; const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", async route => {
      const request = route.request(); const path = new URL(request.url()).pathname;
      if (path === endpoint) {
        if (request.method() === "POST") {
          const body = request.postDataJSON(); writes.push(body);
          const saved = { ...body, id: `rev${body.version}`, agent_id: agent.id } as EmployeeRevision;
          revisions = [...revisions, saved];
          return route.fulfill({ status: 201, json: { revision: saved } });
        }
        return route.fulfill({ json: { revisions } });
      }
      if (path === "/api/python-proxy/agents/catalog") return route.fulfill({ json: { can_manage: true,
        teams: [{ team_id: "employee", name: "社員チーム", subagents: [{ subagent_id: "operator", name: "運用担当", capability_ids: ["workspace_read"] }], execution_profiles: [{ profile_id: "manual", name: "手動Profile" }] }],
        capabilities: [{ id: "workspace_read", family: "workspace", access: "read", native: true }] } });
      if (path === "/api/python-proxy/agents") return route.fulfill({ json: { agents: [agent] } });
      if (path === `/api/python-proxy/agents/${agent.id}`) return route.fulfill({ json: { agent } });
      if (path.endsWith("/command-center")) return route.fulfill({ json: { summary: { working: 0, awaiting_approval: 0, uncertain: 0 }, agents: [], current_work: [] } });
      if (path.endsWith("/organization-profile")) return route.fulfill({ json: { organization_profile: {} } });
      return route.fallback();
    });
    await page.goto(`/operations?tab=agents&agent=${agent.id}&panel=role`);
    await expect(page.getByRole("combobox", { name: "予算通貨" })).toContainText("選択してください");
    await expect(page.getByLabel("最大同時実行数")).toHaveValue("");
    await page.getByRole("checkbox", { name: "workspace_read (workspace / read)" }).check();
    expect(writes).toEqual([]);
    await save(page, 2);
    expect(writes[0]).toMatchObject({ capability_ceiling: ["workspace_read"], budget_policy: {}, concurrency_policy: {}, wake_policy: { mode: "manual" } });
    await page.reload();
    await expect(page.getByRole("heading", { name: "職務・モデル — 現在 v2" })).toBeVisible();
    await expect(page.getByRole("combobox", { name: "予算通貨" })).toContainText("選択してください");
    await expect(page.getByLabel("最大同時実行数")).toHaveValue("");
    await page.getByLabel("1実行の予算上限（百万分の1通貨単位）").fill("0");
    await page.getByLabel("1日の予算上限（百万分の1通貨単位）").fill("2000");
    await select(page, "予算通貨", "JPY");
    await page.getByLabel("最大同時実行数").fill("1");
    await save(page, 3);
    expect(writes[1]).toMatchObject({ budget_policy: { currency: "JPY", max_run_cost_micros: 0, max_daily_cost_micros: 2000 }, concurrency_policy: { max_parallel_runs: 1 } });
    await page.reload();
    await expect(page.getByRole("combobox", { name: "予算通貨" })).toContainText("JPY");
    await expect(page.getByLabel("最大同時実行数")).toHaveValue("1");
    await expect(page.getByLabel("1実行の予算上限（百万分の1通貨単位）")).toHaveValue("0");
    await page.getByLabel("1日の予算上限（百万分の1通貨単位）").fill("3000");
    await save(page, 4);
    expect(writes[2]).toMatchObject({ budget_policy: { currency: "JPY", max_run_cost_micros: 0, max_daily_cost_micros: 3000 }, concurrency_policy: { max_parallel_runs: 1 } });
    await select(page, "予算通貨", "選択してください");
    await page.getByLabel("最大同時実行数").clear();
    await save(page, 5);
    expect(writes[3]).toMatchObject({ budget_policy: { max_run_cost_micros: 0, max_daily_cost_micros: 3000 }, concurrency_policy: {} });
    expect(revisions[0].budget_policy).toEqual({}); expect(revisions[0].concurrency_policy).toEqual({});
    expect(errors).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}
