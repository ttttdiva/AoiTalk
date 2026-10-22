import { createHash } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";
import { mockPersonaWorkspace, personaFixtureAccounts, personaFixtureVariants } from "./support/persona-workspace-fixture";

// 隔離DTOの準備記録だけを返す。本物の投稿・OAuth・PublicationReceiptは作らない。
async function fixture(page: Page, mode: "ready" | "unknown" | "unavailable" | "foreign" | "wrong-readiness" | "legacy") {
  // APIは全て隔離fixture。実SNS・外部final click・認証/DBへの通信を許さない。
  await page.route("**/*", route => {
    const url = new URL(route.request().url());
    if (!["127.0.0.1", "localhost"].includes(url.hostname)) return route.abort("blockedbyclient");
    if (url.pathname.startsWith("/api/")) return route.fulfill({ status: 503, json: { detail: "unhandled isolated API" } });
    return route.continue();
  });
  await addAuthCookie(page); await mockAuthenticatedApis(page);
  const account = { ...personaFixtureAccounts[6], account_ref: "@Own_Aoi" };
  const variant = structuredClone(personaFixtureVariants[6]);
  const revision = variant.current_revision;
  const digest = createHash("sha256").update(revision.payload.type === "x_post" ? revision.payload.text ?? "" : "").digest("hex");
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  const readinessQueries: URLSearchParams[] = [];
  let action: Record<string, unknown> | null = null;
  let preparation: Record<string, unknown> | null = null;
  await page.route("**/api/python-proxy/pc-bridge/devices", route => route.fulfill({ json: { success: true, result: { devices: [{ id: "own-pc", name: "本人の長いPC名".repeat(12), online: true, edge_connected: true, browser_backend: "managed_profile", capabilities: ["browser", "media_asset"] }] } } }));
  await page.route("**/api/python-proxy/operations/actions/bridge-action/package*", route => route.fulfill({ json: { manifest: { status: "verified", action: { id: action?.id, action_version: action?.action_version, payload_hash: action?.payload_hash }, source: { content_variant_revision_id: revision.id, content_variant_revision_hash: revision.content_hash }, payload: revision.payload, assets: [], reasons: [] } } }));
  await mockPersonaWorkspace(page, { override: async (route, url) => {
    const path = url.pathname.replace("/api/python-proxy/operations/media", "");
    const method = route.request().method();
    if (method !== "GET") writes.push({ path, body: route.request().postDataJSON() });
    const reply = async (json: unknown) => { await route.fulfill({ json }); return true; };
    if (path === "/platform-accounts") return reply([account]);
    if (path === `/platform-accounts/${account.id}`) return reply(account);
    if (path === `/platform-accounts/${account.id}/provider-readiness`) return reply({ platform_account_id: account.id, platform: "x", account_revision_id: account.current_revision.id, account_status: "active", verified: false, credential_status: "missing", write_capable: false, read_only: false, operations: [] });
    if (path === `/platform-accounts/${account.id}/oauth`) return reply({ platform_account_id: account.id, platform: "x", enabled: false, blockers: [], required_scopes: [], external_checks: [], credential_status: "missing", authorization_url: null });
    if (path === "/content-variants") return reply([variant]);
    if (path === `/content-variants/${variant.id}`) return reply(variant);
    if (path === "/adapter-status") {
      const query = url.searchParams; readinessQueries.push(new URLSearchParams(query));
      const matching = query.get("platform_account_id") === account.id && query.get("device_id") === "own-pc" && query.get("tab_id") === "7";
      const target = query.has("platform_account_id") ? { owner_user_id: account.owner_user_id, project_id: account.project_id, persona_id: account.persona_id, platform_account_id: mode === "wrong-readiness" ? "foreign-account" : account.id, platform_account_revision_id: account.current_revision.id, platform: "x", device_id: query.get("device_id"), tab_id: Number(query.get("tab_id")), checked_at: new Date().toISOString() } : null;
      const blockers = !matching ? ["browser_explicit_owner_device_tab_required"] : mode === "legacy" ? ["browser_preparation_legacy_revision_unverified"] : mode === "unavailable" || preparation ? ["browser_preparation_new_document_required"] : [];
      return reply({ platforms: { x: { browser_publication: { publication_available: false, automatic_fallback: false, preparation: { available: matching && blockers.length === 0, source_available: true, execution_mode: "manual", publication_confirmation_allowed: false, required_terminal_stage: "awaiting_final_confirmation", prepare_operation_id: "media_operations_prepare_browser_action", get_operation_id: "media_operations_get_browser_preparation", inspect_operation_id: "media_operations_inspect_browser_preparation", blockers, readiness_target: target } } } } });
    }
    if (path === "/actions") {
      if (method === "GET") return reply(action ? [action] : []);
      const body = route.request().postDataJSON();
      action = { ...body, id: "bridge-action", owner_user_id: "fixture-owner", project_id: "project-3", platform: "x", action_type: "media.publish_content", execution_mode: "manual", status: "proposed", version: 1, action_version: 1, payload_hash: "e".repeat(64), artifact_hashes: [], attempts: [], receipt: null, payload: { ...body, connection: { id: account.connection_id, provider_key: "x", remote_account_ref: account.account_ref } } };
      return reply(action);
    }
    if (path === "/actions/bridge-action") return reply(action);
    if (path === "/actions/bridge-action/approve") {
      expect(route.request().postDataJSON()).toMatchObject({ expected_version: 1, action_version: 1, payload_hash: "e".repeat(64) });
      action = { ...action, status: "approved", version: 2 }; return reply(action);
    }
    if (path === "/actions/bridge-action/browser-preparation") {
      if (method === "GET") return reply({ preparation, publication_confirmed: false });
      const body = route.request().postDataJSON();
      expect(Object.keys(body).sort()).toEqual(["expected_version", "action_version", "payload_hash", "device_id", "tab_id", "idempotency_key"].sort());
      expect(body).toMatchObject({ expected_version: 2, action_version: 1, payload_hash: "e".repeat(64), device_id: "own-pc", tab_id: 7 });
      // 実backend同様、claimでActionの状態版が1つ増える。
      action = { ...action, version: 3 };
      preparation = { id: "bridge-preparation", action_id: "bridge-action", action_state_version: 3, action_version: 1, payload_hash: "e".repeat(64), owner_user_id: "fixture-owner", project_id: "project-3", platform: "x", platform_account_id: mode === "foreign" ? "foreign-account" : account.id, platform_account_revision_id: account.current_revision.id, device_id: "own-pc", tab_id: 7, content_sha256: digest, text_sha256: digest, metadata_sha256: null, status: mode === "unknown" ? "uncertain" : "awaiting_final_confirmation", phase: mode === "unknown" ? "transport_unknown" : "final_checkpoint", version: 1, current_action_matches: true, current_approval_valid: true, publication_confirmed: false, automatic_retry_allowed: false };
      if (mode === "unknown") { await route.fulfill({ status: 503, json: { detail: "isolated transport unknown" } }); return true; }
      return reply(preparation);
    }
    if (path === "/actions/bridge-action/browser-preparation/inspect") {
      expect(route.request().postDataJSON()).toEqual({ preparation_id: "bridge-preparation", expected_version: 1 });
      preparation = { ...preparation, version: 2, phase: "inspect_only" }; return reply(preparation);
    }
    return false;
  } });
  await page.goto(`/operations?tab=studio&persona=persona-3&stage=publish&variant=${variant.id}`);
  await expect(page.getByRole("button", { name: "公式APIで公開", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("button", { name: "この保存版の公開準備", exact: true })).toBeDisabled();
  expect(writes).toEqual([]);
  await page.getByRole("button", { name: "Bridgeで準備のみ", exact: true }).click();
  return { writes, readinessQueries, panel: page.getByRole("region", { name: "Bridgeで準備のみ", exact: true }) };
}

async function selectTarget(page: Page, panel: ReturnType<Page["getByRole"]>) {
  await expect(panel.getByRole("button", { name: "この保存版の準備Actionを作成" })).toBeDisabled();
  await expect(page.getByLabel("本人が開いた既存タブID")).toHaveValue("");
  await page.getByLabel("本人の接続済みPC").click(); await page.getByRole("option", { name: /本人の長いPC名/ }).click();
  await page.getByLabel("本人が開いた既存タブID").fill("7");
}
async function prepare(page: Page, panel: ReturnType<Page["getByRole"]>) {
  await selectTarget(page, panel);
  const create = panel.getByRole("button", { name: "この保存版の準備Actionを作成" }); await expect(create).toBeEnabled();
  await create.evaluate((button: HTMLButtonElement) => { button.click(); button.click(); });
  await expect(panel.getByLabel("Bridgeの凍結preview")).toBeVisible();
  const submit = panel.getByRole("button", { name: "本人が承認して下書きだけ準備" }); await expect(submit).toBeDisabled();
  await panel.getByRole("checkbox").check();
  await expect(submit).toBeEnabled(); await submit.evaluate((button: HTMLButtonElement) => { button.click(); button.click(); });
  return submit;
}

for (const width of [1440, 390]) {
  test(`Bridge text preparation keeps human final waiting and does not replay at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 }); const { writes, readinessQueries, panel } = await fixture(page, "ready");
    await expect(panel.getByRole("alert")).toContainText("Action作成前に明示選択");
    expect(readinessQueries.filter(query => query.has("platform_account_id"))).toHaveLength(0);
    const submit = await prepare(page, panel);
    expect(readinessQueries.filter(query => query.has("platform_account_id")).every(query => query.get("platform_account_id") === "account-x-3" && query.get("device_id") === "own-pc" && query.get("tab_id") === "7")).toBe(true);
    const evidence = panel.getByLabel("Bridge準備の保存証跡"); await expect(evidence).toContainText("本人の最終操作待ち"); await expect(evidence).toContainText("公開結果は未確認");
    await expect(submit).toBeDisabled(); await expect(page.getByTestId("operations-action-receipt")).toHaveCount(0);
    await panel.getByRole("button", { name: "Bridgeの利用状態を更新" }).click();
    await expect(panel.getByRole("alert")).toContainText("新しい制作タブを手動で開いて");
    await expect(panel.getByRole("button", { name: "同じ下書きを読み取り専用で再照合" })).toBeEnabled();
    await panel.getByRole("button", { name: "同じ下書きを読み取り専用で再照合" }).click(); await expect(evidence).toContainText("inspect_only");
    expect(writes.map(write => write.path)).toEqual(["/actions", "/actions/bridge-action/approve", "/actions/bridge-action/browser-preparation", "/actions/bridge-action/browser-preparation/inspect"]);
    await page.reload(); await page.getByRole("button", { name: "Bridgeで準備のみ", exact: true }).click();
    await panel.getByRole("button", { name: "準備記録を読み取って照合" }).click(); await expect(evidence).toContainText("公開結果は未確認"); expect(writes).toHaveLength(4);
    const device = await page.getByLabel("本人の接続済みPC").boundingBox(); const tab = await page.getByLabel("本人が開いた既存タブID").boundingBox();
    expect(device && tab && (width < 640 ? device.y + device.height <= tab.y : device.x + device.width <= tab.x)).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test("Bridge actual availability false exposes one concrete wait without API fallback", async ({ page }) => {
  const { writes, panel } = await fixture(page, "unavailable"); await selectTarget(page, panel); await expect(panel.getByRole("alert")).toHaveCount(1); await expect(panel.getByRole("alert")).toContainText("新しい制作タブを手動で開いて");
  await expect(panel.getByRole("button", { name: "この保存版の準備Actionを作成" })).toBeDisabled(); expect(writes).toEqual([]);
});

test("Bridge unknown submission reload reads the same evidence without input replay", async ({ page }) => {
  const { writes, panel } = await fixture(page, "unknown"); const submit = await prepare(page, panel); await expect(panel.getByRole("alert")).toBeVisible(); await expect(submit).toBeDisabled();
  await page.reload(); await page.getByRole("button", { name: "Bridgeで準備のみ", exact: true }).click();
  await panel.getByRole("button", { name: "準備記録を読み取って照合" }).click(); await expect(panel.getByLabel("Bridge準備の保存証跡")).toContainText("準備結果が不明・再入力を停止");
  expect(writes.map(write => write.path)).toEqual(["/actions", "/actions/bridge-action/approve", "/actions/bridge-action/browser-preparation"]);
});

test("Bridge foreign evidence stops before any receipt and remains blocked after reload", async ({ page }) => {
  const { writes, panel } = await fixture(page, "foreign"); await prepare(page, panel); await expect(panel.getByRole("alert")).toContainText("準備証跡");
  await expect(panel.getByLabel("Bridge準備の保存証跡")).toHaveCount(0); await expect(page.getByTestId("operations-action-receipt")).toHaveCount(0);
  await page.reload(); await page.getByRole("button", { name: "Bridgeで準備のみ", exact: true }).click(); await panel.getByRole("button", { name: "準備記録を読み取って照合" }).click();
  await expect(panel.getByRole("alert")).toContainText("準備証跡"); expect(writes).toHaveLength(3);
});

test("Bridge target change rechecks availability before any Action write", async ({ page }) => {
  const { writes, readinessQueries, panel } = await fixture(page, "ready"); await selectTarget(page, panel);
  const create = panel.getByRole("button", { name: "この保存版の準備Actionを作成" }); await expect(create).toBeEnabled();
  await page.getByLabel("本人が開いた既存タブID").fill("8");
  await expect(panel.getByRole("alert")).toContainText("Action作成前に明示選択"); await expect(create).toBeDisabled();
  expect(readinessQueries.some(query => query.get("tab_id") === "8")).toBe(true); expect(writes).toEqual([]);
});

for (const mode of ["wrong-readiness", "legacy"] as const) {
  test(`Bridge ${mode} readiness cannot create or replay an Action`, async ({ page }) => {
    const { writes, panel } = await fixture(page, mode); await selectTarget(page, panel);
    await expect(panel.getByRole("alert")).toContainText(mode === "legacy" ? "旧拡張機能による準備証跡" : "人格・account・PC・タブが一致しません");
    await expect(panel.getByRole("button", { name: "この保存版の準備Actionを作成" })).toBeDisabled(); expect(writes).toEqual([]);
  });
}
