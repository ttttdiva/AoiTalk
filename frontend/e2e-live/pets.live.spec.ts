import { chromium, expect, test, type Page } from "@playwright/test";
import { readFileSync, writeFileSync } from "node:fs";
import { basename, join, resolve } from "node:path";

// Real Next + FastAPI + PostgreSQL only. The isolated harness injects the
// canonical .env.qa-login credentials; never record login traces or videos.
const baseURL = process.env.PET_QA_BASE_URL;
const directory = process.env.PET_QA_DIRECTORY;
const phase = process.env.PET_QA_PHASE;
test.use({ baseURL, trace: "off", video: "off", screenshot: "off", actionTimeout: 15_000, navigationTimeout: 60_000 });

function assertIsolatedRuntime() {
  if (!directory || !baseURL) throw new Error("An isolated pet QA runtime is required");
  const manifest = JSON.parse(readFileSync(join(directory, "manifest.json"), "utf8"));
  expect(manifest.harness).toBe("ai_employee_qa");
  expect(basename(resolve(directory))).toMatch(/^aoitalk_test_employee_[a-f0-9]{32}$/);
  expect(resolve(manifest.output_dir)).toBe(resolve(directory));
  expect(manifest.frontend_url).toBe(baseURL);
  expect(resolve(process.env.AOITALK_PET_DATA_DIR!)).toBe(resolve(directory, "data/web-pets"));
}

async function login(page: Page) {
  await page.goto("/login");
  await page.getByLabel("ユーザー名", { exact: true }).fill(process.env.PET_QA_USERNAME!);
  await page.getByLabel("パスワード", { exact: true }).fill(process.env.PET_QA_PASSWORD!);
  await page.getByRole("button", { name: "ログイン", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login(?:\?|$)/, { timeout: 60_000 });
}

async function metadata(page: Page) {
  const response = await page.request.get("/api/pets");
  expect(response.status()).toBe(200);
  expect(response.headers()["cache-control"]).toContain("no-store");
  return (await response.json()).pet;
}

async function upload(page: Page, name: string) {
  const image = await page.evaluate(() => {
    const canvas = document.createElement("canvas");
    canvas.width = 1536; canvas.height = 2288;
    const context = canvas.getContext("2d")!;
    for (let row = 0; row < 11; row++) for (let col = 0; col < 8; col++) {
      context.fillStyle = `rgb(${row * 20},${col * 30},120)`;
      context.fillRect(col * 192 + 32, row * 208 + 32, 128, 144);
    }
    return canvas.toDataURL("image/png").split(",")[1];
  });
  await page.getByLabel("Codexペットのファイル").setInputFiles([
    { name: "pet.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify({
      displayName: name, spriteVersionNumber: 2, spritesheetPath: "sprite.png",
    })) },
    { name: "sprite.png", mimeType: "image/png", buffer: Buffer.from(image, "base64") },
  ]);
  await expect(page.getByTestId("pet-registration-status")).toContainText(`このサーバーにペットが登録済み：${name}`);
  await visiblePet(page, name);
}

async function visiblePet(page: Page, name: string) {
  const image = page.getByTestId("pet-overlay").getByRole("img");
  await expect(image).toHaveAccessibleName(name, { timeout: 30_000 });
  await expect(image).toHaveAttribute("data-pet-frame", /\d+/);
}

test("実サーバー: 登録・独立クライアント・サイトデータ削除・既存操作・置換・失敗時保持", async ({ page, browser }) => {
  test.skip(phase !== "register", "Run register, restart both services, then run restarted");
  test.setTimeout(240_000);
  assertIsolatedRuntime();
  expect((await page.request.get("/api/pets")).status()).toBe(401);
  await login(page);
  expect(await metadata(page)).toBeNull(); // Never overwrite an existing asset.
  await page.goto("/settings#pet");
  await expect(page.getByTestId("pet-registration-status")).toContainText("ペット未登録", { timeout: 60_000 });
  await upload(page, "Server QA pet");
  const original = await metadata(page);
  await expect(page.getByRole("region", { name: "ペットの設定", exact: true })).toContainText("他のブラウザ・端末でも再登録せず利用できます");

  const secondBrowser = process.env.PET_QA_SECOND_BROWSER_CHANNEL
    ? await chromium.launch({ channel: process.env.PET_QA_SECOND_BROWSER_CHANNEL }) : null;
  const otherContext = await (secondBrowser ?? browser).newContext({ baseURL });
  const phoneContext = await browser.newContext({ baseURL, viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  try {
    const other = await otherContext.newPage();
    await login(other);
    await other.goto("/chat");
    await visiblePet(other, "Server QA pet");
    const phone = await phoneContext.newPage();
    await login(phone);
    await phone.goto("/chat");
    await visiblePet(phone, "Server QA pet");
    expect((await metadata(phone)).revision).toBe(original.revision);

    // Delete all origin storage, including cookies and IndexedDB, then log in.
    const cdp = await otherContext.newCDPSession(other);
    await cdp.send("Storage.clearDataForOrigin", { origin: baseURL!, storageTypes: "all" });
    await cdp.detach();
    await login(other);
    await other.goto("/chat");
    await visiblePet(other, "Server QA pet");
    expect((await metadata(other)).revision).toBe(original.revision);

    const sprite = page.getByTestId("pet-overlay").getByRole("img");
    await page.getByLabel("ペットの表示サイズ").click();
    await page.getByRole("option", { name: "96 px", exact: true }).click();
    await expect(sprite).toHaveCSS("width", "96px");
    await expect(other.getByTestId("pet-overlay").getByRole("img")).toHaveCSS("width", "128px");
    await page.getByLabel("プレビューモーション").click();
    await page.getByRole("option", { name: "ジャンプ", exact: true }).click();
    await expect(page.getByRole("img", { name: "Server QA petのプレビュー" })).toHaveAttribute("data-pet-motion", "jumping");
    const button = page.getByRole("button", { name: /Server QA pet：ドラッグ/ });
    await button.click();
    await expect(sprite).toHaveAttribute("data-pet-motion", "waving");
    const before = (await button.boundingBox())!;
    await page.mouse.move(before.x + 30, before.y + 30);
    await page.mouse.down();
    await page.mouse.move(before.x - 80, before.y - 30, { steps: 5 });
    await expect(sprite).toHaveAttribute("data-pet-motion", "running-left");
    await page.mouse.up();
    await expect.poll(async () => (await button.boundingBox())!.x).toBeLessThan(before.x - 50);
    await page.getByRole("checkbox", { name: "アニメーションを抑える", exact: true }).check();
    await expect(sprite).toHaveAttribute("data-pet-frame", "0");
    await page.getByRole("button", { name: "ペットを非表示", exact: true }).click();
    await expect(page.getByTestId("pet-overlay")).toHaveCount(0);
    await page.getByRole("checkbox", { name: "ペットを表示する", exact: true }).check();
    await visiblePet(page, "Server QA pet");

    await page.getByLabel("Codexペットのファイル").setInputFiles({ name: "broken.zip", mimeType: "application/zip", buffer: Buffer.from("invalid zip") });
    await expect(page.getByRole("region", { name: "ペットの設定", exact: true }).getByRole("alert")).toBeVisible();
    expect((await metadata(page)).revision).toBe(original.revision);
    // Bypass client checks to exercise actual server image validation as well.
    const invalid = await page.request.put("/api/pets", { headers: { "x-aoitalk-pet": "1" }, multipart: {
      manifest: JSON.stringify(original.manifest), image: { name: "bad.png", mimeType: "image/png", buffer: Buffer.from("invalid image") },
    } });
    expect(invalid.status()).toBe(400);
    expect((await metadata(page)).revision).toBe(original.revision);
    await visiblePet(other, "Server QA pet");

    await upload(page, "Replacement QA pet");
    const replacement = await metadata(page);
    expect(replacement.revision).not.toBe(original.revision);
    // No shared browser storage and no manual reload: wait for normal polling.
    await visiblePet(other, "Replacement QA pet");
    await visiblePet(phone, "Replacement QA pet");
    expect((await page.request.get(`/api/pets/image?revision=${original.revision}`)).status()).toBe(409);
    await page.screenshot({ path: join(directory!, "pet-settings.png"), fullPage: true });
    await other.screenshot({ path: join(directory!, "pet-chat.png"), fullPage: true });
    await phone.screenshot({ path: join(directory!, "pet-phone.png"), fullPage: true });
    writeFileSync(join(directory!, "pet-before-restart.json"), JSON.stringify(replacement));
    const manifest = JSON.parse(readFileSync(join(directory!, "manifest.json"), "utf8"));
    writeFileSync(join(directory!, "pet-processes-before-restart.json"), JSON.stringify(manifest.processes));
  } finally { await otherContext.close(); await phoneContext.close(); await secondBrowser?.close(); }
});

test("実サーバー: 両サービス再起動後の保持と全クライアントからの削除", async ({ page, browser }) => {
  test.skip(phase !== "restarted", "Requires the register phase and a service restart");
  test.setTimeout(120_000);
  assertIsolatedRuntime();
  const before = JSON.parse(readFileSync(join(directory!, "pet-processes-before-restart.json"), "utf8"));
  const current = JSON.parse(readFileSync(join(directory!, "manifest.json"), "utf8")).processes;
  for (const service of ["frontend", "backend"]) {
    expect(current[service].created_at).toBeGreaterThan(before[service].created_at);
  }
  const expected = JSON.parse(readFileSync(join(directory!, "pet-before-restart.json"), "utf8"));
  await login(page);
  await page.goto("/settings#pet");
  await visiblePet(page, expected.manifest.displayName);
  expect(await metadata(page)).toEqual(expected);
  const otherContext = await browser.newContext({ baseURL });
  try {
    const other = await otherContext.newPage();
    await login(other);
    await other.goto("/chat");
    await visiblePet(other, expected.manifest.displayName);
    await page.getByRole("button", { name: "サーバーのペット登録を削除", exact: true }).click();
    await page.getByRole("button", { name: "サーバーから削除する", exact: true }).click();
    await expect(page.getByTestId("pet-registration-status")).toContainText("ペット未登録");
    await expect(other.getByTestId("pet-overlay")).toHaveCount(0, { timeout: 30_000 });
    expect(await metadata(other)).toBeNull();
    expect((await other.request.get(`/api/pets/image?revision=${expected.revision}`)).status()).toBe(404);
  } finally { await otherContext.close(); }
});
