import { mkdirSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test, type Page } from "@playwright/test";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const LONG_SNIPPET_PREFIX =
  "very-long-snippet-prefix-that-must-wrap-inside-the-standard-settings-card-instead-of-stretching-the-pane";
const LONG_SNIPPET_BODY =
  "body-token-without-spaces-".repeat(24);

async function mockSettingsWithLongSnippet(page: Page) {
  await page.route("**/api/users/me/settings", async (route) => {
    await route.fulfill({
      json: {
        settings: {
          snippets: [
            {
              prefix: LONG_SNIPPET_PREFIX,
              body: LONG_SNIPPET_BODY,
              description: "layout overflow probe",
              quickAccess: true,
            },
          ],
        },
      },
    });
  });
}

async function readLayout(page: Page) {
  return page.locator("[data-settings-page]").evaluate((element) => {
    const pane = element.closest<HTMLElement>(".ao-main-scroll")!;
    const paneRect = pane.getBoundingClientRect();
    const measure = (selector: string) => {
      const node = element.querySelector<HTMLElement>(selector)!;
      const rect = node.getBoundingClientRect();
      return { left: rect.left, right: rect.right, width: rect.width, center: rect.left + rect.width / 2 };
    };
    const snippetsFrame = measure('[data-settings-target-frame="snippets"]');
    const snippetsCard = measure('[data-settings-target-frame="snippets"] [data-slot="card"]');
    return {
      paneWidth: pane.clientWidth,
      paneCenter: paneRect.left + pane.clientWidth / 2,
      paneOverflow: pane.scrollWidth - pane.clientWidth,
      documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      header: measure(".settings-page-header"),
      search: measure("[data-settings-search]"),
      heading: measure(".settings-group-heading"),
      standard: measure("#custom-instructions-card"),
      snippets: snippetsFrame,
      snippetsCard,
    };
  });
}

async function readClosedHeaderHeights(page: Page) {
  return page.locator("[data-settings-page]").evaluate((element) => {
    const heightOf = (selector: string) => {
      const node = element.querySelector<HTMLElement>(selector);
      return node ? node.getBoundingClientRect().height : 0;
    };
    return {
      clip: heightOf('[data-settings-target-frame="clip-ingest"] [data-slot="card-header"]'),
      browser: heightOf('[data-settings-target="browser-agent"] button[aria-controls]'),
      mobile: heightOf('[data-settings-target="mobile-commands"] button[aria-controls]'),
    };
  });
}

test.describe("設定コンテンツの幅と余白", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await mockSettingsWithLongSnippet(page);
  });

  for (const width of [390, 1280, 1920, 2560]) {
    test(`${width}px: 通常欄を中央に揃え、スニペット開閉でも幅を変えない`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto("/settings");
      await expect(page.getByRole("combobox", { name: "設定を検索" })).toBeVisible();
      if (width >= 768) {
        await expect(page.getByRole("navigation", { name: "設定カテゴリ" })).toBeVisible();
      }
      await expect(page.locator('[data-settings-target="browser-agent"] button[aria-controls]')).toBeVisible();
      await expect(page.locator('[data-settings-target="mobile-commands"] button[aria-controls]')).toBeVisible();

      const collapsed = await readLayout(page);
      expect(collapsed.header.width).toBeCloseTo(Math.min(1040, collapsed.paneWidth - (width < 640 ? 24 : 48)), 0);
      for (const block of [collapsed.header, collapsed.search, collapsed.heading, collapsed.standard, collapsed.snippets]) {
        expect(Math.abs(block.center - collapsed.paneCenter)).toBeLessThan(2);
        expect(block.width).toBeCloseTo(collapsed.header.width, 0);
      }
      expect(collapsed.snippets.width).toBeCloseTo(collapsed.standard.width, 0);

      const closedHeaders = await readClosedHeaderHeights(page);
      expect(closedHeaders.clip).toBeGreaterThan(0);
      expect(closedHeaders.browser).toBeGreaterThan(0);
      expect(closedHeaders.mobile).toBeGreaterThan(0);
      expect(Math.abs(closedHeaders.clip - closedHeaders.browser)).toBeLessThanOrEqual(2);
      expect(Math.abs(closedHeaders.clip - closedHeaders.mobile)).toBeLessThanOrEqual(2);

      const standardTrigger = page.locator("#custom-instructions-card button[aria-controls='custom-instructions-card-content']");
      await standardTrigger.click();
      await expect(standardTrigger).toHaveAttribute("aria-expanded", "true");
      await expect(page.locator("#custom-instructions")).toBeVisible();
      expect((await readLayout(page)).standard.width).toBeCloseTo(collapsed.standard.width, 0);

      const snippetsTrigger = page.locator('[data-settings-target-activation="snippets"]');
      await snippetsTrigger.click();
      await expect(snippetsTrigger).toHaveAttribute("aria-expanded", "true");
      await expect(page.getByText(LONG_SNIPPET_PREFIX)).toBeVisible();
      const expanded = await readLayout(page);
      expect(expanded.snippets.width).toBeCloseTo(collapsed.snippets.width, 0);
      expect(expanded.snippets.width).toBeCloseTo(expanded.standard.width, 0);
      expect(Math.abs(expanded.snippets.center - expanded.paneCenter)).toBeLessThan(2);
      expect(expanded.header.width).toBeCloseTo(collapsed.header.width, 0);
      expect(expanded.snippetsCard.left).toBeGreaterThanOrEqual(expanded.snippets.left - 1);
      expect(expanded.snippetsCard.right).toBeLessThanOrEqual(expanded.snippets.right + 1);
      expect(expanded.paneOverflow).toBeLessThanOrEqual(1);
      expect(expanded.documentOverflow).toBeLessThanOrEqual(1);

      await snippetsTrigger.click();
      const recollapsed = await readLayout(page);
      expect(recollapsed.snippets.width).toBeCloseTo(collapsed.snippets.width, 0);
      expect(recollapsed.snippets.width).toBeCloseTo(recollapsed.standard.width, 0);

      const screenshotDir = process.env.SETTINGS_LAYOUT_SCREENSHOT_DIR?.trim();
      if (screenshotDir && width === 1280) {
        mkdirSync(screenshotDir, { recursive: true });
        await page.screenshot({
          path: resolve(screenshotDir, "snippets-closed-1280.png"),
          fullPage: true,
        });
        await snippetsTrigger.click();
        await expect(snippetsTrigger).toHaveAttribute("aria-expanded", "true");
        await page.screenshot({
          path: resolve(screenshotDir, "snippets-open-1280.png"),
          fullPage: true,
        });
        await page.locator('[data-settings-target-frame="clip-ingest"]').scrollIntoViewIfNeeded();
        await page.screenshot({
          path: resolve(screenshotDir, "clip-ingest-closed-header-1280.png"),
        });
      }
    });
  }
});
