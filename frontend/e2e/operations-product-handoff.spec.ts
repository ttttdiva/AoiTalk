import { expect, test } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";
import { mockPersonaWorkspace, personaFixtureAccounts, personaFixtureVariants } from "./support/persona-workspace-fixture";

test("saved product history wraps at 390px and keeps preparation separate from sale", async ({ page }) => {
  test.setTimeout(90_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await addAuthCookie(page); await mockAuthenticatedApis(page);
  const sourceAccount = personaFixtureAccounts.find(a => a.persona_id === "persona-3")!;
  const account = { ...sourceAccount, platform: "fanza", account_ref: "synthetic-seller" };
  const original = personaFixtureVariants.find(v => v.current_revision.platform_account_id === account.id)!;
  const payload = { type: "fanza_release", title: "Synthetic landscape", description: "Non-sensitive fixture", category: "illustration", age_rating: "all_ages", price: 0, sales: { currency: "JPY", tax_included: true, distribution: "download" }, preview_assets: [], thumbnail_assets: [], deliverable_package_ref: "artifact:synthetic-product", rights_checklist: [{ code: "original", status: "passed", note: "fixture" }], storefront: "doujin", seller_ref: account.account_ref, ai_usage: "none", ai_generated: false, submission_state: "ready_for_review" };
  const revision = { ...original.current_revision, platform: "fanza", payload };
  const variant = { ...original, platform: "fanza", current_revision: revision };
  const actionId = "29cef6b9-c19d-4423-b957-71b02064627c";
  const action = { id: actionId, owner_user_id: account.owner_user_id, project_id: account.project_id, action_type: "media.release_product", execution_mode: "manual", platform: "fanza", status: "succeeded", action_version: 1, version: 4, payload_hash: "e".repeat(64), content_item_id: revision.content_item_id, content_variant_id: variant.id, content_variant_revision_id: revision.id, persona_revision_id: revision.persona_revision_id, platform_account_id: account.id, platform_account_revision_id: revision.platform_account_revision_id, connection_id: account.connection_id,
    payload: { content_item_id: revision.content_item_id, content_item_hash: revision.content_item_hash, content_variant_revision_hash: revision.content_hash, persona_revision_id: revision.persona_revision_id, persona_revision_hash: revision.persona_revision_hash, platform_account_id: account.id, platform_account_revision_id: revision.platform_account_revision_id, platform_account_revision_hash: revision.platform_account_revision_hash, payload },
    receipt: { id: "synthetic-receipt", action_id: actionId, action_version: 1, payload_hash: "e".repeat(64), remote_status: "pending_review", confirmation_level: "human_confirmed", evidence_note: "Synthetic review acceptance; sale has not begun" } };
  await mockPersonaWorkspace(page, { override: async (route, url) => {
    const path = url.pathname.replace("/api/python-proxy/operations/media", "");
    const json = path === "/platform-accounts" ? [account] : path === `/platform-accounts/${account.id}` ? account
      : path === "/content-variants" ? [variant] : path === `/content-variants/${variant.id}` ? variant
      : path === `/content-variants/${variant.id}/readiness` ? { content_variant_id: variant.id, revision_id: revision.id, revision_hash: revision.content_hash, ready: true, status: "publishable", blocking_reasons: [] }
      : path === "/actions" ? [action] : path === `/actions/${actionId}` ? action : path === "/revenue-events" ? [] : undefined;
    if (json === undefined) return false;
    await route.fulfill({ json }); return true;
  } });
  const writes: string[] = [];
  page.on("request", req => { if (req.method() !== "GET" && req.url().includes("/api/python-proxy/operations/")) writes.push(req.url()); });
  await page.goto(`/operations?tab=studio&persona=persona-3&stage=publish&variant=${variant.id}`);
  const handoff = page.getByRole("region", { name: "商品素材・手動結果への引継ぎ" });
  await expect(handoff.getByRole("button", { name: `succeeded · 商品Action ${actionId}` })).toBeVisible({ timeout: 30_000 });
  await handoff.getByRole("button", { name: `succeeded · 商品Action ${actionId}` }).click();
  await expect(handoff).toContainText("pending_review");
  await expect(handoff.getByRole("link", { name: "このActionの承認・Manual attempt・外部結果を開く" })).toHaveAttribute("href", `/operations?tab=actions&action=${actionId}`);
  const preview = page.getByRole("region", { name: "商品素材と販売接続先" });
  await expect(preview).toContainText("審査準備");
  await expect(preview).not.toContainText("本人が公開ボタンを押した時に送信");
  await expect(handoff).toContainText("該当する売上記録は未取得");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(writes).toEqual([]);
});
