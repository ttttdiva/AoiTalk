import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const APP_ID = "responsive-app";
const APP_NAME = "QA222 Mobile Context";
const WIDTHS = [390, 1023, 1024, 1180, 1366, 1535, 1536, 1920];

async function mockApp(page: Page, name = APP_NAME, permission = "admin") {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  const target = {
    id: "responsive-target", app_id: APP_ID, target_key: "main",
    display_name: "Context helper", surface: "headless", runtime: "python",
    execution_host: "aoitalk", entrypoint: "main.py",
  };
  const app = {
    id: APP_ID, name, slug: APP_ID, visibility: "private", permission,
    description: "Appの名前と説明を読みながら、利用・ダウンロード・開発の操作を選べます。",
    default_target_key: "main", targets: [target], related_project_ids: [],
  };
  await page.route("**/api/python-proxy/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace(/^.*\/api\/python-proxy/, "");
    const payloads: Record<string, unknown> = {
      "/apps": { apps: [app] },
      [`/apps/${APP_ID}`]: { app },
      [`/apps/${APP_ID}/context`]: { app, permission, targets: [target], readme: "# Context helper\n\nローカルのresponsive回帰テスト。" },
      [`/apps/${APP_ID}/files`]: { files: [] },
      [`/apps/${APP_ID}/git/status`]: { revision: "fixture", clean: true, files: [] },
      [`/apps/${APP_ID}/git/history`]: { history: [] },
      [`/apps/${APP_ID}/jobs`]: { jobs: [] },
      [`/apps/${APP_ID}/releases`]: { releases: [] },
      "/conversations": { conversations: [] },
    };
    if (path in payloads) return route.fulfill({ json: payloads[path] });
    return route.fallback();
  });
}

async function expectReadableLayout(page: Page, railVisible: boolean) {
  const detail = page.locator(".apps-detail-page");
  await expect(detail.getByRole("heading", { level: 1 })).toBeVisible();
  const geometry = await detail.evaluate((element) => {
    const main = element.firstElementChild as HTMLElement;
    const rail = element.querySelector(":scope > aside");
    const header = main.querySelector("header")!;
    const identity = header.firstElementChild as HTMLElement;
    const actions = header.lastElementChild as HTMLElement;
    const title = header.querySelector("h1")!;
    const box = (node: Element) => {
      const rect = node.getBoundingClientRect();
      return { x: rect.x, y: rect.y, width: rect.width, right: rect.right, bottom: rect.bottom };
    };
    return {
      columns: getComputedStyle(element).gridTemplateColumns.split(/\s+/).length,
      railVisible: Boolean(rail && getComputedStyle(rail).display !== "none"),
      detail: box(element), main: box(main), header: box(header), identity: box(identity), actions: box(actions),
      titleClipped: title.scrollWidth > title.clientWidth + 1,
      headerOverflow: header.scrollWidth > header.clientWidth + 1,
      pageOverflow: document.documentElement.scrollWidth > innerWidth,
      mainOverflow: main.scrollWidth > main.clientWidth + 1,
    };
  });
  expect(geometry.railVisible).toBe(railVisible);
  expect(geometry.columns).toBe(railVisible ? 2 : 1);
  if (!railVisible) expect(geometry.main.width).toBeCloseTo(geometry.detail.width, 0);
  expect(geometry.identity.width).toBeGreaterThanOrEqual(Math.min(384, geometry.header.width) - 1);
  expect(geometry.titleClipped).toBe(false);
  expect(geometry.headerOverflow).toBe(false);
  expect(geometry.mainOverflow).toBe(false);
  expect(geometry.pageOverflow).toBe(false);
  const actionsOnNextRow = geometry.actions.y >= geometry.identity.bottom;
  expect(actionsOnNextRow || geometry.actions.x >= geometry.identity.right).toBe(true);
  for (const action of ["利用する", "ダウンロード", "Chatで開発"]) {
    const control = detail.locator("header").getByText(action, { exact: true });
    await expect(control).toBeVisible();
    const bounds = await control.boundingBox();
    expect(bounds!.x).toBeGreaterThanOrEqual(geometry.header.x - 1);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(geometry.header.right + 1);
  }
}

test("App詳細はrailの表示境界まで本文を全幅で表示し名前と操作を読みやすく保つ", async ({ page }, testInfo) => {
  await mockApp(page);
  await page.goto(`/apps/${APP_ID}`);
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: 1000 });
    await expectReadableLayout(page, width >= 1536);
    await testInfo.attach(`apps-detail-${width}`, { body: await page.screenshot(), contentType: "image/png" });
  }
});

test("狭いApp詳細でも長い連続文字の名前を折り返して全ての操作を表示する", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 1000 });
  const name = "LongAppNameWithoutSpaces".repeat(8);
  await mockApp(page, name);
  await page.goto(`/apps/${APP_ID}`);
  await expect(page.locator(".apps-detail-page h1")).toHaveText(name);
  await expectReadableLayout(page, false);
});

test("App詳細の利用操作と履歴タブはURLへ反映し戻る操作とviewer権限を維持する", async ({ page }) => {
  await page.setViewportSize({ width: 1180, height: 1000 });
  await mockApp(page);
  await page.goto(`/apps/${APP_ID}?tab=history&section=releases`);
  const detail = page.locator(".apps-detail-page");
  await expect(detail.getByRole("tab", { name: "履歴", exact: true })).toHaveAttribute("aria-selected", "true");
  await detail.getByRole("button", { name: "利用する", exact: true }).click();
  await expect(page).toHaveURL(/tab=use/);
  await expect(detail.getByRole("tab", { name: "利用", exact: true })).toHaveAttribute("aria-selected", "true");
  await page.goto(`/apps/${APP_ID}?tab=history&section=releases`);
  await page.goBack();
  await expect(detail.getByRole("tab", { name: "利用", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(detail.locator("header a").filter({ hasText: "Chatで開発" })).toHaveAttribute("href", `/chat?app_id=${APP_ID}&app_target_id=responsive-target`);
  await mockApp(page, APP_NAME, "viewer");
  await page.reload();
  await expect(detail.getByRole("heading", { name: APP_NAME })).toBeVisible();
  await expect(detail.locator("header").getByText("ダウンロード", { exact: true })).toHaveCount(0);
  await detail.getByRole("tab", { name: "設定", exact: true }).click();
  await expect(detail.getByLabel("説明", { exact: true })).toHaveAttribute("readonly", "");
  await expect(detail.getByRole("button", { name: "保存", exact: true })).toHaveCount(0);
});
