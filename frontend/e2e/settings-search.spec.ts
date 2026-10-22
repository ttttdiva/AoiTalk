import { expect, test, type Locator, type Page } from "@playwright/test";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

async function mockSettingsFeatures(page: Page) {
  await page.route("**/api/python-proxy/runtime/features*", (route) =>
    route.fulfill({
      json: {
        features: {},
        application_features: { entertainment: true },
      },
    }),
  );
  await page.route("**/api/pets", (route) =>
    route.fulfill({ json: { pet: null } }),
  );
}

async function expectOnlySettingsScrollerMoved(page: Page, target: Locator) {
  const readMetrics = () =>
    target.evaluate((element) => {
      const measuredElement = element.classList.contains("contents")
        ? ((element.firstElementChild as HTMLElement | null) ?? element)
        : element;
      const container = measuredElement.closest<HTMLElement>(".ao-main-scroll");
      const shell = document.querySelector<HTMLElement>(
        '[data-shell="contextual-workspace"]',
      );
      const containerRect = container?.getBoundingClientRect();
      const targetRect = measuredElement.getBoundingClientRect();
      return {
        innerScrollTop: container?.scrollTop ?? 0,
        targetTop:
          containerRect === undefined
            ? null
            : targetRect.top - containerRect.top,
        viewportHeight: containerRect?.height ?? 0,
        shellScrollTop: shell?.scrollTop ?? null,
        documentScrollTop: document.documentElement.scrollTop,
        bodyScrollTop: document.body.scrollTop,
        windowScrollY: window.scrollY,
      };
    });

  await expect
    .poll(
      async () => {
        const metrics = await readMetrics();
        return (
          metrics.innerScrollTop > 0 &&
          metrics.targetTop !== null &&
          metrics.targetTop >= -2 &&
          metrics.targetTop < metrics.viewportHeight
        );
      },
      { timeout: 20_000, intervals: [100, 250, 500] },
    )
    .toBe(true);

  const metrics = await readMetrics();

  expect(metrics.innerScrollTop).toBeGreaterThan(0);
  expect(metrics.targetTop).not.toBeNull();
  expect(metrics.targetTop!).toBeGreaterThanOrEqual(-2);
  expect(metrics.targetTop!).toBeLessThan(metrics.viewportHeight);
  expect(metrics.shellScrollTop).toBe(0);
  expect(metrics.documentScrollTop).toBe(0);
  expect(metrics.bodyScrollTop).toBe(0);
  expect(metrics.windowScrollY).toBe(0);
}

test.describe("設定検索とカテゴリナビゲーション", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await mockSettingsFeatures(page);
  });

  test("上部は検索を主導線にし、重複した概要UIを表示しない", async ({
    page,
  }) => {
    await page.goto("/settings");

    await expect(
      page.getByRole("heading", { name: "設定", level: 1 }),
    ).toBeVisible();
    await expect(
      page.getByRole("combobox", { name: "設定を検索" }),
    ).toBeVisible();
    await expect(page.getByText("よく使う設定", { exact: true })).toHaveCount(
      0,
    );
    await expect(page.locator("[data-settings-overview]")).toHaveCount(0);
    await expect(page.locator('[aria-label="設定カテゴリ一覧"]')).toHaveCount(
      0,
    );
    await expect(page.getByText(/^Settings$/i)).toHaveCount(0);
    await expect(
      page.getByText("Settings / Admin", { exact: true }),
    ).toHaveCount(0);

    const groups = page.locator("[data-settings-page] > [data-settings-group]");
    await expect(groups.first()).toHaveAttribute(
      "data-settings-group",
      "conversation",
    );
    const petDisclosure = page.locator(
      '[data-settings-disclosure="true"][data-settings-target="pet"]',
    );
    await expect(petDisclosure).toBeVisible();
    await expect(
      petDisclosure.locator("xpath=ancestor::*[@data-settings-group][1]"),
    ).toHaveAttribute("data-settings-group", "appearance");
    await expect(petDisclosure.getByRole("button")).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  test("ペット検索は表示・外観へ移動してDisclosureを展開する", async ({
    page,
  }) => {
    await page.goto("/settings");

    const petDisclosure = page.locator(
      '[data-settings-disclosure="true"][data-settings-target="pet"]',
    );
    await expect(petDisclosure).toBeVisible();
    const search = page.getByRole("combobox", { name: "設定を検索" });
    await search.fill("ペット");
    const option = page
      .locator('[data-settings-search-result="target"]')
      .filter({ hasText: "ペット" })
      .first();
    await expect(option).toContainText("表示・外観");
    await search.press("Enter");

    await expect(page).toHaveURL(/\/settings#pet$/);
    const trigger = petDisclosure.locator("button[aria-expanded]");
    await expect(trigger).toHaveAttribute("aria-expanded", "true");
    await expect(trigger).toBeFocused();
    await expect(
      page.getByRole("region", { name: "ペットの設定", exact: true }),
    ).toBeVisible();
    await expect(
      page
        .getByRole("navigation", { name: "設定カテゴリ" })
        .locator('[data-settings-target="appearance"]'),
    ).toHaveAttribute("aria-current", "location");
    await expectOnlySettingsScrollerMoved(page, petDisclosure);
  });

  test("検索プロバイダ検索と左ナビのカテゴリ移動を維持する", async ({
    page,
  }) => {
    await page.goto("/settings");

    await expect(
      page
        .getByRole("navigation", { name: "設定カテゴリ" })
        .locator('[data-settings-target="admin"]'),
    ).toBeVisible();
    const search = page.getByRole("combobox", { name: "設定を検索" });
    await search.fill("検索プロバイダ");
    const option = page
      .locator('[data-settings-search-result="target"]')
      .filter({ hasText: "検索プロバイダ" })
      .first();
    await expect(option).toContainText("ナレッジ・検索");
    await search.press("Enter");

    await expect(page).toHaveURL(/\/settings#search-provider$/);
    const providerTarget = page.locator(
      '[data-settings-page] [data-settings-target="search-provider"]',
    );
    const providerTrigger = providerTarget.locator(
      '[role="button"][aria-expanded]',
    );
    await expect(providerTrigger).toHaveAttribute("aria-expanded", "true");
    await expect(providerTrigger).toBeFocused();
    await expect(page.locator("#search-provider-content")).toBeVisible();
    await expect(
      page
        .getByRole("navigation", { name: "設定カテゴリ" })
        .locator('[data-settings-target="knowledge"]'),
    ).toHaveAttribute("aria-current", "location");
    await expectOnlySettingsScrollerMoved(page, providerTarget);

    const navigation = page.getByRole("navigation", { name: "設定カテゴリ" });
    const account = navigation.locator('[data-settings-target="account"]');
    await account.click({ force: true });
    await expect(page).toHaveURL(/\/settings#account$/);
    await expect(account).toHaveAttribute("aria-current", "location");
    await expect(
      page.locator('[data-settings-group="account"] h2'),
    ).toBeFocused();
  });
});
