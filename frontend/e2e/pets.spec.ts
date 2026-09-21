import { randomUUID } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";
import type { ServerPet } from "../src/lib/pets/pet-server-contract";

/** Test-owned shared server. Browser contexts have independent local storage. */
function petServer() {
  let pet: ServerPet | null = null;
  let image: Buffer | null = null;
  const server = {
    failNextRegistration: false,
    async attach(page: Page) {
      await page.route("**/api/pets", async (route) => {
        const request = route.request();
        if (request.method() === "PUT") {
          if (server.failNextRegistration) {
            server.failNextRegistration = false;
            return route.fulfill({ status: 503, json: { detail: "検証用の保存失敗" } });
          }
          if (request.headers()["if-none-match"] === "*" && pet) return route.fulfill({ status: 409, json: { detail: "既に登録されています" } });
          const form = await new Response(new Uint8Array(request.postDataBuffer()!), {
            headers: { "content-type": request.headers()["content-type"] },
          }).formData();
          pet = { revision: randomUUID(), updatedAt: new Date().toISOString(), manifest: JSON.parse(String(form.get("manifest"))) };
          image = Buffer.from(await (form.get("image") as Blob).arrayBuffer());
        } else if (request.method() === "DELETE") { pet = null; image = null; }
        return route.fulfill({ json: { pet }, headers: { "cache-control": "no-store" } });
      });
      await page.route("**/api/pets/image?*", async (route) => {
        if (!pet || !image) return route.fulfill({ status: 404, json: { detail: "未登録" } });
        if (new URL(route.request().url()).searchParams.get("revision") !== pet.revision) return route.fulfill({ status: 409, json: { detail: "更新されました" } });
        return route.fulfill({ body: image, contentType: "image/png", headers: { "cache-control": "no-store" } });
      });
    },
  };
  return server;
}
async function openSettings(page: Page, entertainment = true, server = petServer()) {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  await server.attach(page);
  await page.route("**/api/python-proxy/runtime/features", (route) => route.fulfill({
    json: { features: {}, application_features: { entertainment } },
  }));
  await page.goto("/settings#pet");
  return server;
}

// Synthetic atlas; no personal artwork is committed or downloaded.
async function importFixture(page: Page, name = "E2E pet", expectSuccess = true) {
  const png = await page.evaluate(() => {
    const canvas = document.createElement("canvas");
    canvas.width = 1536; canvas.height = 2288;
    const context = canvas.getContext("2d")!;
    for (let row = 0; row < 11; row++) {
      for (let column = 0; column < 8; column++) {
        context.fillStyle = `rgb(${row * 20}, ${column * 30}, 120)`;
        context.fillRect(column * 192 + 32, row * 208 + 32, 128, 144);
      }
    }
    return canvas.toDataURL("image/png").split(",")[1];
  });
  await expect(page.getByLabel("Codexペットのファイル")).toBeEnabled();
  await page.getByLabel("Codexペットのファイル").setInputFiles([
    { name: "pet.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify({ displayName: name, spriteVersionNumber: 2, spritesheetPath: "spritesheet.png" })) },
    { name: "spritesheet.png", mimeType: "image/png", buffer: Buffer.from(png, "base64") },
  ]);
  if (expectSuccess) {
    await expect(page.getByTestId("pet-registration-status")).toContainText(`このサーバーにペットが登録済み：${name}`);
    await expect(page.getByTestId("pet-overlay").getByRole("img")).toHaveAttribute("data-pet-frame", /\d+/);
  }
}

test("ペットを登録し、非表示・サイズ変更・再読み込みができる", async ({ page }) => {
  await openSettings(page);
  await expect(page.getByTestId("pet-registration-status")).toContainText("ペット未登録");
  await importFixture(page);
  const overlay = page.getByTestId("pet-overlay");
  await page.getByLabel("ペットの表示サイズ").click();
  await page.getByRole("option", { name: "96 px", exact: true }).click();
  await expect(overlay.getByRole("img")).toHaveCSS("width", "96px");
  await page.reload();
  await expect(overlay.getByRole("img")).toHaveCSS("width", "96px");
  await page.getByRole("button", { name: "ペットを非表示", exact: true }).click();
  await expect(overlay).toHaveCount(0);
  await page.getByRole("checkbox", { name: "ペットを表示する", exact: true }).check();
  await expect(overlay).toBeVisible();
  await page.getByLabel("プレビューモーション").click();
  await page.getByRole("option", { name: "ジャンプ", exact: true }).click();
  await expect(page.getByRole("img", { name: "E2E petのプレビュー", exact: true })).toHaveAttribute("data-pet-motion", "jumping");
});

test("読み込み失敗でペットを失わず、狭い画面でも移動できる", async ({ page }) => {
  await openSettings(page);
  await importFixture(page);
  await page.getByLabel("Codexペットのファイル").setInputFiles({ name: "broken.zip", mimeType: "application/zip", buffer: Buffer.from("not a zip") });
  await expect(page.getByRole("region", { name: "ペットの設定", exact: true }).getByRole("alert")).toBeVisible();
  const overlay = page.getByTestId("pet-overlay");
  await expect(overlay.getByRole("img")).toBeVisible();
  await page.getByRole("checkbox", { name: "アニメーションを抑える", exact: true }).check();
  await page.setViewportSize({ width: 375, height: 640 });
  await expect(overlay.getByRole("img")).toHaveAttribute("data-pet-frame", "0");
  const petButton = overlay.getByRole("button", { name: /ドラッグで移動/ });
  await petButton.focus();
  await petButton.press("ArrowLeft");
  await expect.poll(async () => (await overlay.boundingBox())?.x).toBeLessThan(230);
  const box = await overlay.boundingBox();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.y).toBeGreaterThanOrEqual(40);
  expect(box!.x + box!.width).toBeLessThanOrEqual(375);
  expect(box!.y + box!.height).toBeLessThanOrEqual(640);
});

test("独立ブラウザでも再登録不要で、置換と削除を取得する", async ({ page, browser }) => {
  const server = await openSettings(page);
  await importFixture(page);
  const second = await browser.newContext({ baseURL: new URL(page.url()).origin });
  try {
    const other = await second.newPage();
    await openSettings(other, true, server);
    await expect(other.getByTestId("pet-overlay").getByRole("img")).toHaveAccessibleName("E2E pet");
    await other.evaluate(() => localStorage.clear());
    await other.reload();
    await expect(other.getByTestId("pet-registration-status")).toContainText("登録済み");
    await importFixture(page, "Replacement pet");
    await other.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect(other.getByTestId("pet-overlay").getByRole("img")).toHaveAccessibleName("Replacement pet");
    await page.getByRole("button", { name: "サーバーのペット登録を削除", exact: true }).click();
    await page.getByRole("button", { name: "サーバーから削除する", exact: true }).click();
    await expect(page.getByTestId("pet-registration-status")).toContainText("ペット未登録");
    await other.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect(other.getByTestId("pet-registration-status")).toContainText("ペット未登録");
    await expect(other.getByTestId("pet-overlay")).toHaveCount(0);
  } finally { await second.close(); }
});

test("サーバー保存失敗を表示し、正常登録を保持する", async ({ page }) => {
  const server = await openSettings(page);
  await importFixture(page);
  server.failNextRegistration = true;
  await importFixture(page, "失敗するペット", false);
  await expect(page.getByRole("region", { name: "ペットの設定", exact: true }).getByRole("alert")).toContainText("保存失敗");
  await expect(page.getByTestId("pet-registration-status")).toContainText("E2E pet");
  await expect(page.getByTestId("pet-overlay").getByRole("img")).toHaveAccessibleName("E2E pet");
});

test("ペットのブラウザ保存領域が無効でもサーバーの登録を取得する", async ({ page, browser }) => {
  const server = await openSettings(page);
  await importFixture(page);
  const second = await browser.newContext({ baseURL: new URL(page.url()).origin });
  try {
    const other = await second.newPage();
    await other.addInitScript(() => {
      const getItem = Storage.prototype.getItem;
      const setItem = Storage.prototype.setItem;
      Storage.prototype.getItem = function (key) {
        if (key.startsWith("aoitalk-pet-view-")) throw new Error("検証用ストレージ無効");
        return getItem.call(this, key);
      };
      Storage.prototype.setItem = function (key, value) {
        if (key.startsWith("aoitalk-pet-view-")) throw new Error("検証用ストレージ無効");
        return setItem.call(this, key, value);
      };
    });
    await openSettings(other, true, server);
    await expect(other.getByTestId("pet-overlay").getByRole("img")).toHaveAccessibleName("E2E pet");
  } finally { await second.close(); }
});

test("entertainmentを許可しないプロファイルでは表示しない", async ({ page }) => {
  await openSettings(page);
  await importFixture(page);
  await page.route("**/api/python-proxy/runtime/features", (route) => route.fulfill({
    json: { features: {}, application_features: { entertainment: false } },
  }));
  await page.reload();
  await expect(page.getByTestId("pet-overlay")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /画面のペット/ })).toHaveCount(0);
});
