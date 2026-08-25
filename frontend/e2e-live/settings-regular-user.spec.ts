import { expect, test, type Page } from "@playwright/test";

import { loadLiveUserA, loginAsRegularLiveUser } from "./support/live-auth";
import {
  assertNoLiveObservabilityIssues,
  attachLiveObservability,
  type LiveObservability,
} from "./support/live-observability";

const liveUser = loadLiveUserA();
const MOBILE_COMMANDS_PATH = "/api/python-proxy/mobile/commands";
const observabilityByPage = new WeakMap<Page, LiveObservability>();

function pathnameOf(url: string) {
  try {
    return new URL(url).pathname;
  } catch {
    return url;
  }
}

function isMobileCommandsRequest(url: string) {
  const pathname = pathnameOf(url);
  return (
    pathname === MOBILE_COMMANDS_PATH ||
    pathname.startsWith(`${MOBILE_COMMANDS_PATH}/`)
  );
}

function collectMobileCommandRequests(page: Page) {
  const urls: string[] = [];
  page.on("request", (request) => {
    if (isMobileCommandsRequest(request.url())) {
      urls.push(pathnameOf(request.url()));
    }
  });
  return urls;
}

test.describe("一般ユーザー settings 境界", () => {
  test.beforeEach(async ({ page }) => {
    test.skip(!liveUser, "live user A credentials are not available");
    observabilityByPage.set(page, attachLiveObservability(page));
    await loginAsRegularLiveUser(page, liveUser!);
  });

  test("settings は mobile/commands を呼ばず、ログイン履歴カードも出さない", async ({
    page,
  }) => {
    const runtimeIssues = observabilityByPage.get(page);
    expect(
      runtimeIssues,
      "live observability must be attached before login",
    ).toBeTruthy();
    const mobileCommandRequests = collectMobileCommandRequests(page);

    await page.goto("/settings");
    await expect(page).not.toHaveURL(/\/login(?:\?|$)/);
    await expect(page.locator("[data-settings-page]")).toBeVisible({
      timeout: 20_000,
    });
    await expect(
      page.getByRole("heading", { name: "設定", exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Settings / Workspace")).toBeVisible({
      timeout: 20_000,
    });
    await page.waitForTimeout(1_500);

    await expect(
      page.getByText("モバイルコマンドを取得できませんでした。"),
    ).toHaveCount(0);
    await expect(
      page.locator('[data-settings-target="login-history"]'),
    ).toHaveCount(0);
    await expect(
      page.locator('[data-settings-target="mobile-commands"]'),
    ).toHaveCount(0);
    await expect(page.getByText("ログイン履歴", { exact: true })).toHaveCount(
      0,
    );
    expect(
      mobileCommandRequests,
      "role=user must not fetch mobile commands",
    ).toEqual([]);
    assertNoLiveObservabilityIssues(
      runtimeIssues!,
      "regular-user settings live flow",
    );
  });
});
