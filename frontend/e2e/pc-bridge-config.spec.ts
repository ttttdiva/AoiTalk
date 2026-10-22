import { readFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const basePath = process.env.NEXT_PUBLIC_BASE_PATH || "";
const syntheticDevice = { id: "fixture-pc", name: "Fixture PC", online: false, edge_connected: false, capabilities: [] };

test.beforeEach(async ({ page }) => {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
});

test("Bridge設定保存はHTTPのBackend URLと通常browserを使い、操作先を変更しない", async ({ page }, testInfo) => {
  const methods: string[] = [];
  await page.route("**/api/python-proxy/pc-bridge/**", async (route) => {
    const method = route.request().method();
    methods.push(method);
    await route.fulfill({ json: { success: true, result: method === "POST"
      ? { id: "fixture-new", name: "New Fixture PC", token: "synthetic-pairing-code" }
      : { selected_device_id: syntheticDevice.id, devices: [syntheticDevice] } } });
  });
  await page.goto(`${basePath}/settings`);
  await page.locator('[data-settings-target="pc-bridge"] button[aria-expanded]').click();
  await page.getByLabel("追加するPCの名前").fill("New Fixture PC");
  await page.getByRole("button", { name: "PCを登録", exact: true }).click();
  await expect(page.getByLabel("接続コード（この画面でのみ表示）")).toHaveAttribute("type", "password");
  const downloaded = page.waitForEvent("download");
  await page.getByRole("button", { name: "Bridge設定ファイルを保存" }).click();
  const download = await downloaded;
  expect(download.suggestedFilename()).toBe("aoitalk-pc-connection.json");
  const path = testInfo.outputPath("synthetic-connection.json");
  await download.saveAs(path);
  const config = JSON.parse(await readFile(path, "utf8"));
  const location = new URL(page.url());
  expect(config).toEqual({
    server_url: `http://${location.hostname}:${process.env.NEXT_PUBLIC_AOITALK_WS_PORT?.trim() || "3000"}`,
    token: "synthetic-pairing-code",
    browser_backend: "agent_browser",
  });
  expect(methods).not.toContain("PUT");
  await expect(page.getByLabel("操作するPC")).toContainText("Fixture PC");
});

test("PC一覧の取得失敗から更新で復帰し、保存済みの選択先を保持する", async ({ page }) => {
  let failed = true;
  const methods: string[] = [];
  await page.route("**/api/python-proxy/pc-bridge/**", async (route) => {
    methods.push(route.request().method());
    await route.fulfill(failed ? { status: 503, json: { detail: "fixture unavailable" } }
      : { json: { success: true, result: { selected_device_id: syntheticDevice.id, devices: [syntheticDevice] } } });
  });
  await page.goto(`${basePath}/settings`);
  await page.locator('[data-settings-target="pc-bridge"] button[aria-expanded]').click();
  await expect(page.getByText("PC一覧を取得できませんでした。", { exact: true })).toBeVisible();
  failed = false;
  await page.getByRole("button", { name: "接続を更新", exact: true }).click();
  await expect(page.getByLabel("操作するPC")).toContainText("Fixture PC");
  await expect(page.getByText("PC一覧を取得できませんでした。", { exact: true })).not.toBeVisible();
  expect(methods.every(method => method === "GET")).toBe(true);
});
