import { expect, test, type Page } from "@playwright/test";

import { addAuthCookie, E2E_USER_ID, mockAuthenticatedApis } from "./support/auth";

type Reference = "openai_api_key" | "free-team:openai-complimentary";

async function mockConfirmationApis(page: Page, options: { otherOwner?: boolean } = {}) {
  const main = { provider: "gemini", model: "fixture-base" };
  const team = { schema_version: 3, delegation_enabled: false, teams: [], execution_profiles: [] };
  const profile = { enabled: true, main_pool_id: "coordinator", max_fallbacks: 2, pools: {}, llm_profiles: {} };
  const before = structuredClone({ main, team, profile });
  const credentials = [
    { reference: "openai_api_key", configured: true, fingerprint: "a".repeat(64),
      project_id: "", sdk_organization_id: "", base_url: "https://api.openai.com/v1" },
    { reference: "free-team:openai-complimentary", configured: true, fingerprint: "b".repeat(64),
      project_id: "fixture-project", sdk_organization_id: "", base_url: "https://api.openai.com/v1" },
  ];
  let record: Record<string, unknown> = {};
  let scope = "org-fixture";
  let tier = "tier_1_2";
  let stopReason = "complimentary_unverified";
  let nextPutStatus = 0;
  let nextRecord = 0;
  const reads: string[] = [];
  const writes: { path: string; method: string; body: Record<string, unknown> }[] = [];
  const response = () => ({ credentials, record, stop_reason: stopReason,
    billing_scope_id: scope, usage_tier: tier, provider_attestation: false });
  await page.route("**/api/python-proxy/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/python-proxy", "");
    const method = request.method();
    if (method === "GET") reads.push(path);
    else writes.push({ path, method, body: request.postDataJSON() as Record<string, unknown> });
    if (path === "/free-team/openai-confirmation") {
      if (options.otherOwner) {
        await route.fulfill({ status: 403, json: { detail: "confirmation_owner_mismatch" } });
      } else if (method === "GET") {
        await route.fulfill({ json: response() });
      } else if (method === "PUT" && nextPutStatus) {
        const status = nextPutStatus;
        nextPutStatus = 0;
        await route.fulfill({ status, json: { detail: "fixture_invalid_scope" } });
      } else if (method === "PUT") {
        const body = request.postDataJSON() as Record<string, unknown>;
        scope = String(body.billing_scope_id);
        tier = String(body.usage_tier);
        stopReason = "";
        record = { record_id: `fixture-record-${++nextRecord}`, owner_user_id: E2E_USER_ID,
          actor_user_id: E2E_USER_ID, recorded_at: "2026-10-07T12:00:00Z",
          source_kind: "owner_declaration", source_reference: body.source_reference,
          eligible: true, enrolled: true, organization_id: scope, usage_tier: tier,
          credential_reference: body.credential_reference as Reference, revoked_at: null };
        await route.fulfill({ json: response() });
      } else if (method === "DELETE") {
        record = { ...record, revoked_at: "2026-10-07T12:05:00Z" };
        stopReason = "confirmation_revoked";
        await route.fulfill({ json: response() });
      } else {
        await route.fulfill({ status: 409, json: { detail: "Unexpected confirmation method" } });
      }
    } else if (method !== "GET") {
      await route.fulfill({ status: 409, json: { detail: "Unexpected mutation in confirmation flow" } });
    } else if (path === "/llm/models") {
      await route.fulfill({ json: { current: main,
        providers: [{ id: "gemini", label: "Gemini", configured_model: main.model,
          source: "static", supports_custom_model: true,
          models: [{ id: main.model, label: "Fixture Base" }] }],
      } });
    } else if (path === "/settings") {
      await route.fulfill({ json: { settings: { agent_team: team } } });
    } else if (path === "/agent-team/config") {
      await route.fulfill({ json: { agent_team: team } });
    } else if (path === "/free-team/settings") {
      await route.fulfill({ json: { profile, credentials: [], candidates: [] } });
    } else if (path === "/free-team/usage") {
      await route.fulfill({ json: { quota_pools: [], verification: {
        status: stopReason ? "unverified" : "verified", stop_reason: stopReason,
        external_usage_visibility: "fixture external consumption unknown",
      } } });
    } else {
      await route.fallback();
    }
  });
  return { reads, writes, before, current: () => ({ main, team, profile }),
    rejectNextPut: (status: number) => { nextPutStatus = status; } };
}

async function models(page: Page) {
  const section = page.locator('[data-settings-surface="language-models"]');
  await section.getByRole("button", { name: /言語モデル/ }).click();
  await expect(section.getByRole("combobox", { name: "LLMプロバイダー" })).toContainText("Gemini");
  await expect(section.getByRole("combobox", { name: "LLMモデル" })).toContainText("Fixture Base");
  return section;
}

async function openRecord(page: Page) {
  const section = await models(page);
  await section.getByRole("button", { name: "日次予算を設定", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "日次予算", exact: true });
  const panel = dialog.getByTestId("openai-confirmation-panel");
  await panel.getByRole("button", { name: "確認済み無料枠の記録", exact: true }).click();
  return { section, dialog, panel };
}

test.describe("既存OpenAI無料枠の本人確認記録", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
  });

  test("Mainを保ちLaunch枠を記録し、再読込・422復帰・Teamキー参照復元・失効ができる", async ({ page }) => {
    const fixture = await mockConfirmationApis(page);
    await page.goto("/settings");
    const section = await models(page);
    await section.getByRole("button", { name: "日次予算を設定", exact: true }).click();
    const initialDialog = page.getByRole("dialog", { name: "日次予算", exact: true });
    const initialPanel = initialDialog.getByTestId("openai-confirmation-panel");
    await expect(initialPanel).toBeVisible();
    expect(fixture.reads).not.toContain("/free-team/openai-confirmation");
    await initialPanel.getByRole("button", { name: "確認済み無料枠の記録", exact: true }).click();
    await expect(initialPanel).toContainText("Providerの自動attestationではありません");
    await initialPanel.getByRole("combobox").nth(1).click();
    await page.getByRole("option", { name: "Sol 1M / Luna 10M token/日", exact: true }).click();
    await initialPanel.getByLabel("本人申告の出典メモ").fill("本人確認済みLaunch 1M/10Mの記録");
    await initialPanel.getByRole("button", { name: "確認済みの無料枠を記録", exact: true }).click();
    await expect(initialPanel).toContainText("本人申告の確認記録を保存しました");
    await expect(initialDialog).toContainText("OpenAI無料枠: 確認済み");
    expect(fixture.writes[0]).toEqual({ path: "/free-team/openai-confirmation", method: "PUT", body: {
      credential_reference: "openai_api_key", fingerprint: "a".repeat(64), expected_record_id: null,
      billing_scope_id: "org-fixture", usage_tier: "tier_3_plus", eligible: true, enrolled: true,
      source_reference: "本人確認済みLaunch 1M/10Mの記録",
    } });

    await page.reload();
    let { dialog, panel } = await openRecord(page);
    await expect(panel.getByRole("combobox").first()).toContainText("現在のOpenAIキー");
    await expect(panel.getByRole("combobox").nth(1)).toContainText("1M / Luna 10M");
    await expect(panel.getByLabel("本人申告の出典メモ")).toHaveValue("本人確認済みLaunch 1M/10Mの記録");
    await expect(panel).toContainText(`操作actor: ${E2E_USER_ID}`);
    await expect(panel).toContainText("owner_declaration");
    fixture.rejectNextPut(422);
    await panel.getByRole("button", { name: "確認済みの無料枠を記録", exact: true }).click();
    await expect(panel).toContainText("fixture_invalid_scope");
    await expect(panel.getByRole("button", { name: "確認済みの無料枠を記録", exact: true })).toBeEnabled();
    await panel.getByRole("button", { name: "確認済みの無料枠を記録", exact: true }).click();
    await expect(panel).toContainText("本人申告の確認記録を保存しました");

    await panel.getByRole("combobox").first().click();
    await page.getByRole("option", { name: "既存無料Teamキー", exact: true }).click();
    await panel.getByRole("button", { name: "確認済みの無料枠を記録", exact: true }).click();
    await expect(panel).toContainText("本人申告の確認記録を保存しました");
    expect(fixture.writes.at(-1)?.body).toMatchObject({
      credential_reference: "free-team:openai-complimentary", fingerprint: "b".repeat(64),
      expected_record_id: "fixture-record-2",
    });
    await page.reload();
    ({ dialog, panel } = await openRecord(page));
    await expect(panel.getByRole("combobox").first()).toContainText("既存無料Teamキー");
    await panel.getByRole("button", { name: "記録を失効", exact: true }).click();
    await expect(panel).toContainText("確認記録を失効させました");
    await expect(dialog).toContainText("確認記録が失効済みです");
    expect(fixture.writes.at(-1)).toEqual({ path: "/free-team/openai-confirmation", method: "DELETE", body: { expected_record_id: "fixture-record-3" } });
    await page.keyboard.press("Escape");
    const reloadedSection = page.locator('[data-settings-surface="language-models"]');
    const routing = reloadedSection.getByRole("tab", { name: /Routing/ });
    await routing.click();
    await expect(routing).toHaveAttribute("aria-selected", "true");
    await reloadedSection.getByRole("tab", { name: /Base Model/ }).click();
    await expect(reloadedSection.getByRole("combobox", { name: "LLMプロバイダー" })).toContainText("Gemini");
    await expect(reloadedSection.getByRole("combobox", { name: "LLMモデル" })).toContainText("Fixture Base");
    expect(fixture.writes).toHaveLength(5);
    expect(fixture.writes.every(({ path }) => path === "/free-team/openai-confirmation")).toBe(true);
    expect(fixture.current()).toEqual(fixture.before);
  });

  test("別ownerの403では記録フォームを表示せず書き込みを行わない", async ({ page }) => {
    const fixture = await mockConfirmationApis(page, { otherOwner: true });
    await page.goto("/settings");
    const { panel } = await openRecord(page);
    await expect(panel).toContainText("confirmation_owner_mismatch");
    await expect(panel.getByRole("button", { name: "確認済みの無料枠を記録", exact: true })).toHaveCount(0);
    await expect(panel.getByLabel("本人申告の出典メモ")).toHaveCount(0);
    expect(fixture.writes).toEqual([]);
    expect(fixture.current()).toEqual(fixture.before);
  });

  test("一般ユーザーには入口を表示せず確認記録を取得しない", async ({ page }) => {
    const fixture = await mockConfirmationApis(page);
    await page.route("**/api/auth/status", async (route) => {
      await route.fulfill({ json: { authenticated: true,
        user: { id: "fixture-member", username: "fixture-member", role: "user" },
      } });
    });
    await page.goto("/settings");
    const section = await models(page);
    await expect(section.getByRole("button", { name: "日次予算を設定", exact: true })).toHaveCount(0);
    expect(fixture.reads.filter((path) => path.startsWith("/free-team/"))).toEqual([]);
    expect(fixture.writes).toEqual([]);
    expect(fixture.current()).toEqual(fixture.before);
  });
});
