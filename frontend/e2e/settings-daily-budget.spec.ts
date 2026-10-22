import { expect, test, type Page } from "@playwright/test";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

type UsageTier = "tier_1_2" | "tier_3_plus";

async function mockBudgetApis(page: Page, tier: UsageTier = "tier_3_plus") {
  const main = { provider: "gemini", model: "fixture-base" };
  const team = { schema_version: 3, delegation_enabled: false, teams: [], execution_profiles: [] };
  const profile = {
    enabled: true, main_pool_id: "coordinator", max_fallbacks: 2,
    pools: {}, llm_profiles: {},
  };
  const before = structuredClone({ main, team, profile });
  let quotas = [
    { id: "openai-sol-daily", limit: 1000000, safety_margin_ratio: 0, safety_margin_units: 40000 },
    { id: "openai-luna-daily", limit: 10000000, safety_margin_ratio: 0.01, safety_margin_units: 0 },
  ].map((quota) => ({
    ...quota, metric_type: "total_tokens", consumed: 100, reserved: 50,
    status: "active", window_end: "2026-10-08T00:00:00Z",
  }));
  function usage(quota: (typeof quotas)[number]) {
    const configured = quota.limit - Math.max(quota.safety_margin_units, quota.limit * quota.safety_margin_ratio);
    const tierCap = tier === "tier_1_2"
      ? quota.id === "openai-sol-daily" ? 220000 : 2470000
      : quota.id === "openai-sol-daily" ? 970000 : 9970000;
    const effective = Math.min(configured, tierCap);
    return {
      ...quota, configured_daily_cap: configured, effective_daily_cap: effective,
      daily_cap: effective, cap_restriction_reason: effective < configured ? "usage_tier" : "", usage_tier: tier,
      available: Math.max(0, effective - quota.consumed - quota.reserved),
    };
  }
  const writes: { path: string; method: string; body: Record<string, unknown> }[] = [];
  const freeTeamReads: string[] = [];
  await page.route("**/api/python-proxy/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (method !== "GET") {
      const body = request.postDataJSON() as Record<string, unknown>;
      writes.push({ path, method, body });
      if (method === "PATCH" && path.startsWith("/api/python-proxy/free-team/quota-pools/")) {
        const id = path.split("/").at(-1);
        quotas = quotas.map((quota) => quota.id === id ? { ...quota, ...body } : quota);
        const saved = quotas.find((quota) => quota.id === id);
        await route.fulfill({ json: { quota_pool: saved && usage(saved) } });
      } else {
        await route.fulfill({ status: 409, json: { detail: "Unexpected mutation in daily budget flow" } });
      }
      return;
    }
    if (path.startsWith("/api/python-proxy/free-team/")) freeTeamReads.push(path);
    if (path === "/api/python-proxy/llm/models") {
      await route.fulfill({ json: {
        current: main,
        providers: [{ id: "gemini", label: "Gemini", configured_model: main.model,
          source: "static", supports_custom_model: true,
          models: [{ id: main.model, label: "Fixture Base" }] }],
      } });
    } else if (path === "/api/python-proxy/settings") {
      await route.fulfill({ json: { settings: { agent_team: team } } });
    } else if (path === "/api/python-proxy/agent-team/config") {
      await route.fulfill({ json: { agent_team: team } });
    } else if (path === "/api/python-proxy/free-team/settings") {
      await route.fulfill({ json: { profile, credentials: [], candidates: [] } });
    } else if (path === "/api/python-proxy/free-team/usage") {
      await route.fulfill({ json: {
        quota_pools: quotas.map(usage),
        verification: { status: "verified", stop_reason: "", external_usage_visibility: "fixture" },
      } });
    } else {
      await route.fallback();
    }
  });
  return { writes, freeTeamReads, before, current: () => ({ main, team, profile }) };
}

async function expandModels(page: Page) {
  const section = page.locator('[data-settings-surface="language-models"]');
  await section.getByRole("button", { name: /言語モデル/ }).click();
  await expect(section.getByRole("combobox", { name: "LLMプロバイダー" })).toContainText("Gemini");
  await expect(section.getByRole("combobox", { name: "LLMモデル" })).toContainText("Fixture Base");
  return section;
}

test.describe("Base Modelからの日次予算設定", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
  });

  for (const tier of ["tier_1_2", "tier_3_plus"] as const) {
    test(`${tier}でも設定capを保持し、Sol/Luna行だけを保存して再読込できる`, async ({ page }) => {
      const fixture = await mockBudgetApis(page, tier);
      await page.goto("/settings");
      const section = await expandModels(page);
      await section.getByRole("button", { name: "日次予算を設定", exact: true }).click();
      const dialog = page.getByRole("dialog", { name: "日次予算", exact: true });
      await expect(dialog.getByRole("button", { name: "ルーティングを保存" })).toHaveCount(0);
      await expect(dialog.getByRole("checkbox", { name: "無料Teamを有効化" })).toHaveCount(0);
      for (const [id, cap] of [["openai-sol-daily", 970000], ["openai-luna-daily", 9970000]] as const) {
        const input = dialog.getByLabel(`${id}の日次停止cap`, { exact: true });
        await input.fill(String(cap));
        const saved = page.waitForResponse((response) =>
          new URL(response.url()).pathname === `/api/python-proxy/free-team/quota-pools/${id}`
          && response.request().method() === "PATCH" && response.ok());
        await input.locator("xpath=../..").getByRole("button", { name: "予算を保存", exact: true }).click();
        await saved;
        await expect(dialog.getByLabel(`${id}の日次停止cap`, { exact: true })).toHaveValue(String(cap));
        await expect(dialog.getByLabel(`${id}の安全量`, { exact: true })).toHaveValue("30000");
        await expect(dialog.getByLabel(`${id}の安全率`, { exact: true })).toHaveValue("0");
      }
      await page.reload();
      const reloadedSection = await expandModels(page);
      await reloadedSection.getByRole("button", { name: "日次予算を設定", exact: true }).click();
      const reloaded = page.getByRole("dialog", { name: "日次予算", exact: true });
      await expect(reloaded.getByLabel("openai-sol-dailyの日次停止cap", { exact: true })).toHaveValue("970000");
      await expect(reloaded.getByLabel("openai-luna-dailyの日次停止cap", { exact: true })).toHaveValue("9970000");
      for (const [id, effective] of [
        ["openai-sol-daily", tier === "tier_1_2" ? 220000 : 970000],
        ["openai-luna-daily", tier === "tier_1_2" ? 2470000 : 9970000],
      ] as const) {
        const row = reloaded.getByLabel(`${id}の日次停止cap`, { exact: true }).locator("xpath=../..");
        await expect(row.getByLabel(`${id}の実効日次停止cap`, { exact: true })).toContainText(new Intl.NumberFormat("ja-JP").format(effective));
        await expect(row.getByText("残量", { exact: true }).locator("..")).toContainText(new Intl.NumberFormat("ja-JP").format(effective - 150));
        if (tier === "tier_1_2") {
          await expect(row).toContainText("利用Tier（tier_1_2）による上限制限");
        } else {
          await expect(row).not.toContainText("利用Tier");
        }
      }
      expect(fixture.writes).toEqual([
        { path: "/api/python-proxy/free-team/quota-pools/openai-sol-daily", method: "PATCH",
          body: { limit: 1000000, safety_margin_ratio: 0, safety_margin_units: 30000, daily_cap: 970000 } },
        { path: "/api/python-proxy/free-team/quota-pools/openai-luna-daily", method: "PATCH",
          body: { limit: 10000000, safety_margin_ratio: 0, safety_margin_units: 30000, daily_cap: 9970000 } },
      ]);
      expect(fixture.current()).toEqual(fixture.before);
    });
  }

  test("一般ユーザーには日次予算の入口を表示せずquotaを取得しない", async ({ page }) => {
    const fixture = await mockBudgetApis(page);
    await page.route("**/api/auth/status", async (route) => {
      await route.fulfill({ json: {
        authenticated: true,
        user: { id: "fixture-member", username: "fixture-member", role: "user" },
      } });
    });
    await page.goto("/settings");
    const section = await expandModels(page);
    await expect(section.getByRole("button", { name: "日次予算を設定", exact: true })).toHaveCount(0);
    expect(fixture.freeTeamReads).toEqual([]);
    expect(fixture.writes).toEqual([]);
    expect(fixture.current()).toEqual(fixture.before);
  });
});
