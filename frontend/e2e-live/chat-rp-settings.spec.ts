import { expect, test, type Page, type Response } from "@playwright/test";

import { loadLiveUserA, loginAsRegularLiveUser } from "./support/live-auth";
import {
  assertNoLiveObservabilityIssues,
  attachLiveObservability,
  type LiveObservability,
} from "./support/live-observability";

const liveUser = loadLiveUserA();
const DIRECT_RP_SETTINGS = /^\/api\/conversations\/[^/]+\/rp-settings$/;
const PROXY_RP_SETTINGS =
  /^\/api\/python-proxy\/conversations\/[^/]+\/rp-settings$/;
const observabilityByPage = new WeakMap<Page, LiveObservability>();

function pathnameOf(url: string) {
  try {
    return new URL(url).pathname;
  } catch {
    return url;
  }
}

type RpResponse = {
  method: string;
  status: number;
  requestBody: string | null;
  bodyPromise: Promise<Record<string, unknown> | null>;
};

async function readJson(
  response: Response,
): Promise<Record<string, unknown> | null> {
  try {
    const body = await response.json();
    return body && typeof body === "object" && !Array.isArray(body)
      ? (body as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function installRpResponseCollector(page: Page) {
  const direct: Array<{ method: string; status: number; path: string }> = [];
  const proxy: RpResponse[] = [];
  page.on("response", (response) => {
    const path = pathnameOf(response.url());
    const method = response.request().method();
    if (DIRECT_RP_SETTINGS.test(path)) {
      direct.push({ method, status: response.status(), path });
      return;
    }
    if (!PROXY_RP_SETTINGS.test(path)) return;
    const item: RpResponse = {
      method,
      status: response.status(),
      requestBody: response.request().postData() ?? null,
      bodyPromise: readJson(response),
    };
    proxy.push(item);
  });
  return { direct, proxy };
}

test.describe("chat rp-settings 経路", () => {
  test.beforeEach(async ({ page }) => {
    test.skip(!liveUser, "live user A credentials are not available");
    observabilityByPage.set(page, attachLiveObservability(page));
    await loginAsRegularLiveUser(page, liveUser!);
  });

  test("RP設定UIはproxyのGET/PUTを実行し、直パスや4xx/5xxを発生させない", async ({
    page,
  }) => {
    const runtimeIssues = observabilityByPage.get(page);
    expect(
      runtimeIssues,
      "live observability must be attached before login",
    ).toBeTruthy();
    const observed = installRpResponseCollector(page);

    await page.goto("/chat");
    await expect(page).not.toHaveURL(/\/login(?:\?|$)/);
    const newChat = page
      .getByRole("button", { name: "新規会話", exact: true })
      .first();
    await expect(newChat).toBeVisible({ timeout: 20_000 });
    await newChat.click();
    await expect(page).toHaveURL(/\?s=/, { timeout: 20_000 });

    const settingsControl = page.getByTestId("chat-model-control");
    await expect(settingsControl).toBeVisible({ timeout: 20_000 });
    await settingsControl.click();
    const steeringToggle = page.getByTitle("ステアリングパネル");
    // A freshly created session may already inherit the user's active RP
    // character. In that case the steering control is present and selecting a
    // second character would introduce an unnecessary race. Otherwise choose
    // one through the custom AppSelect (not a native <select>).
    if (!(await steeringToggle.isVisible().catch(() => false))) {
      const character = page.getByRole("combobox", { name: "キャラクター" });
      await expect(character).toBeVisible({ timeout: 20_000 });
      await character.click();
      const options = await page.getByRole("option").allTextContents();
      const rpCharacter = options.find(
        (option) => option.trim() && option.trim().toLowerCase() !== "aoi",
      );
      expect(
        rpCharacter,
        "live runtime must expose at least one non-Aoi character to enter RP mode",
      ).toBeTruthy();
      await page
        .getByRole("option")
        .filter({ hasText: rpCharacter!.trim() })
        .first()
        .click();
      await expect(steeringToggle).toBeVisible({ timeout: 20_000 });
    }
    await expect(steeringToggle).toBeVisible({ timeout: 20_000 });
    await steeringToggle.click();
    await expect
      .poll(() => observed.proxy.some((item) => item.method === "GET"), {
        timeout: 20_000,
        message: "RP settings GET was not issued by the UI",
      })
      .toBe(true);
    const getEvent = observed.proxy.find((item) => item.method === "GET");
    expect(getEvent).toBeTruthy();
    expect(getEvent!.status).toBeGreaterThanOrEqual(200);
    expect(getEvent!.status).toBeLessThan(300);
    const getBody = await getEvent!.bodyPromise;
    expect(getBody?.rp_settings).toBeTruthy();

    // SteeringPanel is mounted by the top control but starts collapsed; open
    // its own section before exercising the debounced PUT.
    await page
      .getByRole("button", { name: "ステアリング", exact: true })
      .click();
    const slider = page.locator('input[type="range"]').first();
    await expect(slider).toBeVisible();
    await slider.focus();
    await slider.press("ArrowRight");
    await expect
      .poll(() => observed.proxy.some((item) => item.method === "PUT"), {
        timeout: 20_000,
        message: "RP settings PUT was not issued by the UI",
      })
      .toBe(true);
    const putEvent = observed.proxy.find((item) => item.method === "PUT");
    expect(putEvent).toBeTruthy();
    expect(putEvent!.status, `RP PUT payload=${putEvent!.requestBody}`).toBeGreaterThanOrEqual(200);
    expect(putEvent!.status, `RP PUT payload=${putEvent!.requestBody}`).toBeLessThan(300);
    const putBody = await putEvent!.bodyPromise;
    expect(putBody?.rp_settings).toBeTruthy();

    await expect
      .poll(() => observed.proxy.length, {
        timeout: 5_000,
        message: "RP settings proxy response was not observed",
      })
      .toBeGreaterThanOrEqual(2);
    expect(
      observed.direct,
      "RP settings must never call the direct endpoint",
    ).toEqual([]);
    expect(
      observed.proxy.filter((item) => item.status >= 400),
      "RP settings proxy must not return an unexpected 4xx/5xx",
    ).toEqual([]);
    expect(
      (await Promise.all(observed.proxy.map((item) => item.bodyPromise))).every(
        (body) => Boolean(body?.rp_settings),
      ),
      "every RP settings proxy response must be JSON with rp_settings",
    ).toBe(true);
    assertNoLiveObservabilityIssues(runtimeIssues!, "RP settings live flow");
  });
});
