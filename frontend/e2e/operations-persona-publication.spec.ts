import { createHash } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";
import { mockPersonaWorkspace, personaFixtureAccounts, personaFixtureVariants } from "./support/persona-workspace-fixture";
import type { ContentVariant } from "../src/lib/media-operations-content-api";

// Tiny local media bytes. Every API and external request is isolated below.
const imageBytes = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN5kAAAAASUVORK5CYII=", "base64");
const videoBytes = Buffer.from("GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAALaEU2bdLtNu4tTq4QVSalmU6yB5U27jFOrhBZUrmtTrIIBHE27jFOrhBJUw2dTrIIBXE27jFOrhBxTu2tTrIICxOwBAAAAAAAAnAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABVJqWayKtexgw9CQE2AjUxhdmY1OC4yOS4xMDBXQY1MYXZmNTguMjkuMTAwRImIQGkAAAAAAAAWVK5ru64BAAAAAAAAMteBAXPFgQGcgQAitZyDdW5khoVWX1ZQOIOBASPjg4QCYloA4AEAAAAAAAAGsIEQuoEQElTDZ0C/c3MBAAAAAAAALmPAAQAAAAAAAABnyAEAAAAAAAAaRaOHRU5DT0RFUkSHjUxhdmY1OC4yOS4xMDBzcwEAAAAAAAA5Y8ABAAAAAAAABGPFgQFnyAEAAAAAAAAhRaOHRU5DT0RFUkSHlExhdmM1OC41NC4xMDAgbGlidnB4c3MBAAAAAAAAOmPAAQAAAAAAAARjxYEBZ8gBAAAAAAAAIkWjiERVUkFUSU9ORIeUMDA6MDA6MDAuMjAwMDAwMDAwAAAfQ7Z1QJ3ngQCjvIEAAICwAgCdASoQABAAAEcIhYWIhYSIAgICdaoD+AIM/SgA/v9NEv/8WFfxYV/FhX/xYV/8/M7txfzmAKOVgQAoALEBAAEQOAAYABhYL/QACAAAo5WBAFAAsQEAARAQABgAGFgv9AAIAACjlYEAeACxAQABEBAAGAAYWC/0AAgAAKOVgQCgALEBAAEQEAAYABhYL/QACAAAHFO7a5G7j7OBALeK94EB8YICIfCBAw==", "base64");

async function fixture(page: Page, platform: "x" | "youtube", mode: "ready" | "unknown" | "foreign-receipt" | "media-error" | "approval-unknown" = "ready") {
  await page.route("**/*", route => {
    const url = new URL(route.request().url());
    if (!["127.0.0.1", "localhost"].includes(url.hostname)) return route.abort("blockedbyclient");
    if (url.pathname.startsWith("/api/")) return route.fulfill({ status: 503, json: { detail: "unhandled isolated API" } });
    return route.continue();
  });
  await addAuthCookie(page); await mockAuthenticatedApis(page);
  const index = platform === "x" ? 6 : 7;
  const account = personaFixtureAccounts[index]; const variant = structuredClone(personaFixtureVariants[index]) as ContentVariant;
  const revision = variant.current_revision; const bytes = platform === "x" ? imageBytes : videoBytes;
  if (!revision) throw new Error("The isolated publication fixture requires a saved revision");
  const sha = createHash("sha256").update(bytes).digest("hex");
  revision.payload = platform === "x" ? { type: "x_post", text: "本人の保存済み画像投稿", media: ["publication-asset"] }
    : { type: "youtube_video", title: "本人の保存済み動画", description: "公開要求と実際のprivate結果を別々に確認", tags: [], media_asset: ["publication-asset"], visibility: "public", audience: "not_made_for_kids" };
  revision.generation_output_refs = [{ generation_output_id: "publication-asset", sha256: sha }];
  const asset = { asset_revision_id: "asset-revision", generation_output_id: "publication-asset", persona_id: account.persona_id, owner_user_id: account.owner_user_id, project_id: account.project_id, sha256: sha, mime_type: platform === "x" ? "image/png" : "video/webm", size_bytes: bytes.length };
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  let action: Record<string, unknown> | null = null; let packageUnavailable = false;
  await page.route("**/api/python-proxy/operations/actions/publication-action/package*", route => packageUnavailable
    ? route.fulfill({ status: 503, json: { detail: "isolated package unavailable" } })
    : route.fulfill({ json: { manifest: { status: "partial", action: { id: action?.id, action_version: action?.action_version, payload_hash: action?.payload_hash }, source: { content_variant_revision_id: revision.id, content_variant_revision_hash: revision.content_hash }, payload: revision.payload,
      assets: [{ ordinal: 1, role: "media", reference_type: "generation_output", reference_id: asset.generation_output_id, sha256: sha, status: "unavailable" }], reasons: [{ code: "generation_output_artifact_binding_unavailable" }] } } }));
  await mockPersonaWorkspace(page, { override: async (route, url) => {
    const path = url.pathname.replace("/api/python-proxy/operations/media", ""); const method = route.request().method();
    if (method !== "GET") writes.push({ path, body: route.request().postDataJSON() });
    const reply = async (json: unknown) => { await route.fulfill({ json }); return true; };
    if (path === "/platform-accounts") return reply([account]);
    if (path === `/platform-accounts/${account.id}`) return reply(account);
    if (path === `/platform-accounts/${account.id}/oauth`) return reply({ platform_account_id: account.id, platform, configured: false, blockers: [], required_scopes: [], external_checks: [] });
    if (path === `/platform-accounts/${account.id}/provider-readiness`) return reply({ platform_account_id: account.id, platform, account_revision_id: account.current_revision.id, account_status: "active", verified: true, credential_status: "verified", credential_revision: 1, write_capable: true, read_only: false,
      operations: ["text", "image", "video", "schedule"].map(operation => ({ id: operation, owner_user_id: account.owner_user_id, project_id: account.project_id, platform_account_id: account.id, account_revision_id: account.current_revision.id, operation, status: platform === "x" && operation === "schedule" || platform === "youtube" && operation === "text" ? "unsupported" : "automatable", account_eligibility: "eligible" })) });
    if (path === "/content-variants") return reply([variant]);
    if (path === `/content-variants/${variant.id}`) return reply(variant);
    if (path === "/assets/publication-asset") return reply(asset);
    if (path === "/assets/publication-asset/preview") { expect(url.searchParams.get("expected_sha256")).toBe(sha); await route.fulfill(mode === "media-error" ? { status: 403 } : { contentType: asset.mime_type, body: bytes }); return true; }
    if (path === "/actions") {
      if (method === "GET") return reply(action ? [action] : []);
      const body = route.request().postDataJSON();
      expect(body).toMatchObject({ project_id: "project-3", persona_revision_id: "persona-revision-3", platform_account_id: account.id, content_variant_revision_id: revision.id, artifact_hashes: [sha], payload: revision.payload });
      action = { ...body, id: "publication-action", owner_user_id: account.owner_user_id, project_id: account.project_id, status: "proposed", version: 1, action_version: 1, payload_hash: "e".repeat(64), attempts: [], receipt: null, payload: body };
      return reply(action);
    }
    if (path === "/actions/publication-action") return reply(action);
    if (path === "/actions/publication-action/approve") {
      expect(route.request().postDataJSON()).toEqual({ expected_version: 1, action_version: 1, payload_hash: "e".repeat(64), artifact_hashes: [sha] });
      action = { ...action, status: "approved", version: 2 };
      if (mode === "approval-unknown") { await route.fulfill({ status: 503 }); return true; }
      return reply(action);
    }
    if (path === "/actions/publication-action/attempts") {
      expect(route.request().postDataJSON()).toEqual({ expected_version: 2, executor_type: "provider", execution_mode: "provider", execution_key: action?.execution_key });
      const attempt = { id: "publication-attempt", action_id: "publication-action", action_version: 1, execution_key: action?.execution_key, provider_recovery_available: platform === "youtube" };
      action = { ...action, status: mode === "unknown" ? "receipt_unknown" : "succeeded", version: 3, attempts: [attempt], receipt: mode === "unknown" ? null : { id: "publication-receipt", action_id: "publication-action", action_version: mode === "foreign-receipt" ? 2 : 1, attempt_id: attempt.id, confirmation_level: "provider", remote_status: platform === "youtube" ? "private" : "published", remote_resource_id: "fixture-resource" } };
      if (mode === "unknown") { await route.fulfill({ status: 503 }); return true; }
      return reply(attempt);
    }
    if (path === "/actions/publication-action/provider-recovery") {
      expect(route.request().postDataJSON()).toMatchObject({ expected_version: 3, resume: false }); return reply(action);
    }
    return false;
  } });
  await page.goto(`/operations?tab=studio&persona=persona-3&stage=publish&variant=${variant.id}`);
  return { writes, packageFails: () => { packageUnavailable = true; }, panel: page.getByRole("region", { name: "本人による公開操作" }) };
}

async function prepare(page: Page, panel: ReturnType<Page["getByRole"]>, platform: "x" | "youtube") {
  await panel.getByRole("button", { name: "この保存版の公開準備", exact: true }).click();
  const preview = panel.getByLabel("凍結した公開preview"); await expect(preview).toBeVisible();
  if (platform === "x") await expect.poll(() => preview.getByRole("img").evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);
  else await expect.poll(() => preview.locator("video").evaluate((video: HTMLVideoElement) => video.readyState)).toBeGreaterThan(0);
  const submit = panel.getByRole("button", { name: platform === "x" ? "本人が X に投稿" : "本人が YouTube にupload" });
  await expect(submit).toBeDisabled(); await preview.getByRole("checkbox").click(); await expect(submit).toBeEnabled(); return submit;
}

for (const platform of ["x", "youtube"] as const) {
  for (const width of [1440, 390]) test(`${platform} saved media preview, approval and receipt stay bound at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 }); const { writes, panel } = await fixture(page, platform);
    await expect(panel.getByLabel("公式APIの対応範囲")).toContainText(platform === "x" ? "予約未対応" : "動画のthumbnail利用可能");
    const submit = await prepare(page, panel, platform); await submit.evaluate((button: HTMLButtonElement) => { button.click(); button.click(); });
    await expect(panel.getByTestId("operations-action-receipt")).toContainText(platform === "youtube" ? "private" : "published");
    if (platform === "youtube") await expect(panel.getByLabel("凍結した公開preview")).toContainText("公開設定: public");
    expect(writes.map(write => write.path)).toEqual(["/actions", "/actions/publication-action/approve", "/actions/publication-action/attempts"]);
    await page.reload(); await panel.getByRole("button", { name: "保存記録・receiptを照合" }).click();
    await expect(panel.getByTestId("operations-action-receipt")).toContainText(platform === "youtube" ? "private" : "published");
    expect(writes).toHaveLength(3); await expect(submit).toBeDisabled();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
  test(`${platform} unknown reload keeps same-execution recovery when the asset package is unavailable`, async ({ page }) => {
    const { writes, packageFails, panel } = await fixture(page, platform, "unknown"); const submit = await prepare(page, panel, platform); await submit.click();
    await expect(panel.getByRole("alert")).toContainText("接続先または実行サービス"); packageFails(); await page.reload();
    await panel.getByRole("button", { name: "保存記録・receiptを照合" }).click(); await expect(panel.getByRole("alert")).toContainText("接続先または実行サービス");
    await expect(panel.getByLabel("凍結した公開preview")).toHaveCount(0);
    await panel.getByRole("button", { name: "同じ実行の外部状態を再照合" }).click();
    await expect.poll(() => writes.filter(write => write.path.endsWith("/provider-recovery")).length).toBe(1);
    expect(writes.filter(write => write.path.endsWith("/attempts"))).toHaveLength(1); expect(writes.filter(write => write.path === "/actions")).toHaveLength(1);
    if (platform === "youtube") await expect(panel.getByRole("button", { name: "保存済みの同じ動画uploadを再開" })).toBeEnabled();
  });
}

test("a foreign receipt revision never becomes a confirmed publication", async ({ page }) => {
  const { panel } = await fixture(page, "x", "foreign-receipt"); await (await prepare(page, panel, "x")).click();
  await expect(panel.getByRole("alert")).toContainText("外部結果のAction版・実行記録");
  await expect(panel.getByTestId("operations-action-receipt")).not.toContainText("published");
  await expect(panel.getByRole("button", { name: "本人が X に投稿" })).toBeDisabled();
});

test("private media permission failure prevents submission of the saved image", async ({ page }) => {
  const { panel, writes } = await fixture(page, "x", "media-error"); await panel.getByRole("button", { name: "この保存版の公開準備" }).click();
  await expect(panel.getByLabel("凍結した公開preview").getByRole("alert")).toContainText("実体を表示できません");
  await panel.getByRole("checkbox").click(); await expect(panel.getByRole("button", { name: "本人が X に投稿" })).toBeDisabled();
  expect(writes.map(write => write.path)).toEqual(["/actions"]);
});

test("unknown approval stops both controls until saved approval is inspected and explicitly reviewed again", async ({ page }) => {
  const { panel, writes } = await fixture(page, "x", "approval-unknown"); const submit = await prepare(page, panel, "x"); await submit.click();
  await expect(panel).toContainText("保存記録を照合するまで公開操作を停止"); await expect(submit).toBeDisabled();
  await expect(panel.getByRole("checkbox")).toHaveAttribute("aria-disabled", "true");
  expect(writes.map(write => write.path)).toEqual(["/actions", "/actions/publication-action/approve"]);
  await page.reload(); await panel.getByRole("button", { name: "保存記録・receiptを照合" }).click();
  await expect(panel.getByLabel("凍結した公開preview")).toBeVisible(); await expect(submit).toBeDisabled();
  await panel.getByRole("checkbox").click(); await expect(submit).toBeEnabled(); await submit.click();
  await expect(panel.getByTestId("operations-action-receipt")).toContainText("published");
  expect(writes.filter(write => write.path.endsWith("/approve"))).toHaveLength(1); expect(writes.filter(write => write.path.endsWith("/attempts"))).toHaveLength(1);
});
