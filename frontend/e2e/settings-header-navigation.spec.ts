import { expect, test, type Page } from "@playwright/test";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

async function readSettingsScrollMetrics(page: Page) {
  return page.locator("[data-shell-region='main-canvas'] .ao-main-scroll").evaluate((element) => {
    const container = element as HTMLElement;
    const shell = document.querySelector<HTMLElement>('[data-shell="contextual-workspace"]');
    const settingsPage = container.querySelector<HTMLElement>("[data-settings-page]");
    const account = container.querySelector<HTMLElement>('[data-settings-group="account"]');
    const containerRect = container.getBoundingClientRect();
    const accountRect = account?.getBoundingClientRect();
    const settingsPageRect = settingsPage?.getBoundingClientRect();
    const contentBottom = settingsPageRect
      ? settingsPageRect.bottom - containerRect.top + container.scrollTop
      : null;

    return {
      innerScrollTop: container.scrollTop,
      innerScrollHeight: container.scrollHeight,
      innerClientHeight: container.clientHeight,
      shellScrollTop: shell?.scrollTop ?? null,
      documentScrollTop: document.documentElement.scrollTop,
      bodyScrollTop: document.body.scrollTop,
      windowScrollY: window.scrollY,
      accountTop: accountRect ? accountRect.top - containerRect.top : null,
      accountBottom: accountRect ? accountRect.bottom - containerRect.top : null,
      viewportHeight: containerRect.height,
      contentTail: contentBottom === null ? null : container.scrollHeight - contentBottom,
    };
  });
}

test.describe("ヘッダーからユーザー設定へ遷移", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
  });

  test("実際のアバターメニュー遷移はSettings専用スクロールを維持する", async ({ page }) => {
    test.setTimeout(120_000);
    await page.goto("/chat");

    const avatar = page.getByRole("button", { name: "ユーザーメニューを開く" });
    await expect(avatar).toBeVisible({ timeout: 15_000 });
    await avatar.click();
    await page.getByRole("menuitem", { name: /ユーザー設定/ }).click();

    await expect(page).toHaveURL(/\/settings#account$/);
    await expect(
      page.locator('[data-shell-region="global-context"]'),
    ).toBeVisible();
    await expect(page.locator('[data-workspace="settings"]')).toBeVisible();
    await expect(page.locator("[data-settings-page]")).toBeVisible();

    const accountCategory = page.locator('[data-settings-target="account"]');
    await expect
      .poll(() => accountCategory.getAttribute("aria-current"), {
        timeout: 20_000,
        intervals: [100, 250, 500],
      })
      .toBe("location");

    const scrollContainer = page.locator(
      "[data-shell-region='main-canvas'] .ao-main-scroll",
    );
    await expect(scrollContainer).toBeVisible();
    await expect
      .poll(
        async () => {
          const metrics = await readSettingsScrollMetrics(page);
          return (
            metrics.innerScrollTop > 0 &&
            metrics.accountTop !== null &&
            metrics.accountBottom !== null &&
            metrics.accountTop >= -2 &&
            metrics.accountBottom > 0 &&
            metrics.accountBottom <= metrics.viewportHeight + 2
          );
        },
        { timeout: 20_000, intervals: [100, 250, 500] },
      )
      .toBe(true);

    const initialMetrics = await readSettingsScrollMetrics(page);
    expect(initialMetrics.innerScrollTop).toBeGreaterThan(0);
    expect(initialMetrics.accountTop).not.toBeNull();
    expect(initialMetrics.accountTop!).toBeGreaterThanOrEqual(-2);
    expect(initialMetrics.accountBottom).not.toBeNull();
    expect(initialMetrics.accountBottom!).toBeGreaterThan(0);
    expect(initialMetrics.accountBottom!).toBeLessThanOrEqual(
      initialMetrics.viewportHeight + 2,
    );
    expect(initialMetrics.shellScrollTop).toBe(0);
    expect(initialMetrics.documentScrollTop).toBe(0);
    expect(initialMetrics.bodyScrollTop).toBe(0);
    expect(initialMetrics.windowScrollY).toBe(0);
    expect(initialMetrics.contentTail).not.toBeNull();
    // The scroll container's extent must end at the Settings page, not at a
    // detached blank tail left by native hash scrolling.
    expect(initialMetrics.contentTail!).toBeGreaterThanOrEqual(-8);
    expect(initialMetrics.contentTail!).toBeLessThan(64);

    const settingsNavigation = page.getByRole("navigation", { name: "設定カテゴリ" });
    const knowledgeCategory = settingsNavigation.getByRole("link", {
      name: "ナレッジ・検索",
      exact: true,
    });
    await knowledgeCategory.click();
    await expect(page).toHaveURL(/\/settings#knowledge$/);
    await expect(knowledgeCategory).toHaveAttribute("aria-current", "location");
    const knowledgeMetrics = await readSettingsScrollMetrics(page);
    expect(knowledgeMetrics.shellScrollTop).toBe(0);
    expect(knowledgeMetrics.documentScrollTop).toBe(0);
    expect(knowledgeMetrics.bodyScrollTop).toBe(0);
    expect(knowledgeMetrics.windowScrollY).toBe(0);
    expect(knowledgeMetrics.innerScrollTop).toBeGreaterThan(0);

    await page.goBack();
    await expect(page).toHaveURL(/\/settings#account$/);
    await expect(accountCategory).toHaveAttribute("aria-current", "location");
    await expect
      .poll(async () => (await readSettingsScrollMetrics(page)).innerScrollTop, {
        timeout: 20_000,
        intervals: [100, 250, 500],
      })
      .toBeGreaterThan(0);

    await page.goForward();
    await expect(page).toHaveURL(/\/settings#knowledge$/);
    await expect(knowledgeCategory).toHaveAttribute("aria-current", "location");
    const finalMetrics = await readSettingsScrollMetrics(page);
    expect(finalMetrics.shellScrollTop).toBe(0);
    expect(finalMetrics.documentScrollTop).toBe(0);
    expect(finalMetrics.bodyScrollTop).toBe(0);
    expect(finalMetrics.windowScrollY).toBe(0);

    // The header transition itself must remain a normal history entry: going
    // back twice leaves Settings for the originating shell route, and going
    // forward twice restores the hash navigation sequence.
    await page.goBack();
    await expect(page).toHaveURL(/\/settings#account$/);
    await expect(accountCategory).toHaveAttribute("aria-current", "location");
    await page.goBack();
    await expect(page).toHaveURL(/\/chat(?:$|[?#])/);
    await expect(
      page.locator('[data-shell-region="global-context"]'),
    ).toBeVisible();

    await page.goForward();
    await expect(page).toHaveURL(/\/settings#account$/);
    await expect(accountCategory).toHaveAttribute("aria-current", "location");
    await page.goForward();
    await expect(page).toHaveURL(/\/settings#knowledge$/);
    await expect(knowledgeCategory).toHaveAttribute("aria-current", "location");
    await expect(page.locator('[data-workspace="settings"]')).toBeVisible();
    await expect(
      page.locator('[data-shell-region="global-context"]'),
    ).toBeVisible();
  });
});
