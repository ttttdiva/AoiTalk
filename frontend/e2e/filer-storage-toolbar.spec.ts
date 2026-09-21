import { expect, test } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const mountedRoot = {
  id: "nas",
  name: "Studio NAS",
  configuration_revision: "rev-1",
  root_path: "D:/Mounted/StudioNAS",
  read_only: false,
  enabled: true,
  external: true,
  shared: false,
  project_ids: [],
  user_ids: [],
  online: true,
  status: "online" as const,
  can_write: true,
  identity: "storage-nas",
  marker_name: ".aoitalk-storage-id",
};

async function installStorageAndFilerMocks(
  page: import("@playwright/test").Page,
  options: { catalogFailure?: boolean } = {},
) {
  let catalogRequests = 0;
  await mockAuthenticatedApis(page);
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/python-proxy/storage/roots") {
      catalogRequests += 1;
      if (options.catalogFailure) {
        await route.fulfill({
          status: 503,
          json: { detail: "storage unavailable" },
        });
      } else {
        await route.fulfill({
          json: { is_admin: true, revision: "catalog-1", roots: [mountedRoot] },
        });
      }
      return;
    }
    if (url.pathname === "/api/python-proxy/storage/roots/nas/status") {
      await route.fulfill({ json: mountedRoot });
      return;
    }
    if (url.pathname === "/api/python-proxy/storage/roots/nas/files") {
      await route.fulfill({
        json: {
          entries: [
            {
              name: "notes.txt",
              path: "notes.txt",
              is_directory: false,
              size_bytes: 42,
              etag: "etag-1",
              mime_type: "text/plain",
              modified_at: "2026-09-17T00:00:00Z",
            },
          ],
          path: "",
          parent_path: null,
          skipped_entries: 0,
          truncated: false,
        },
      });
      return;
    }
    if (url.pathname === "/api/python-proxy/explorer/list") {
      await route.fulfill({
        json: {
          success: true,
          current_path: "",
          parent_path: null,
          can_go_up: false,
          directories: [],
          files: Array.from({ length: 80 }, (_, index) => ({
            name: `file-${index.toString().padStart(2, "0")}.txt`,
            path: `file-${index.toString().padStart(2, "0")}.txt`,
            type: "text/plain",
            size: 100 + index,
            extension: ".txt",
          })),
          total_items: 80,
        },
      });
      return;
    }
    await route.fallback();
  });
  return { catalogRequests: () => catalogRequests };
}

test.describe("Files storage toolbar integration", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await page.addInitScript(() => {
      localStorage.clear();
      localStorage.setItem("filer-tab", "user");
      localStorage.setItem("explorer-view-mode", "grid");
    });
  });

  test("keeps source tabs and storage controls on one toolbar and preserves internal scrolling", async ({
    page,
  }) => {
    const requests = await installStorageAndFilerMocks(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/filer");

    const toolbar = page.locator('[data-files-toolbar="source-storage"]');
    const storageControls = page.getByLabel("ファイルストレージ");
    const selector = page.getByRole("combobox", { name: "ストレージ選択" });
    await expect(toolbar).toBeVisible();
    await expect(
      toolbar.getByRole("button", { name: "Project Files" }),
    ).toBeVisible();
    await expect(
      toolbar.getByRole("button", { name: "User Files" }),
    ).toBeVisible();
    await expect(toolbar.getByRole("button", { name: "HF" })).toBeVisible();
    await expect(toolbar.getByRole("button", { name: "Hydrus" })).toBeVisible();
    await expect(selector).toHaveValue("default");
    expect(
      await storageControls.evaluate(
        (element) =>
          element.closest('[data-files-toolbar="source-storage"]') !== null,
      ),
    ).toBe(true);

    const initialCatalogRequests = requests.catalogRequests();
    await page.getByRole("button", { name: "接続状態を更新" }).click();
    await expect
      .poll(requests.catalogRequests)
      .toBeGreaterThan(initialCatalogRequests);

    await selector.click();
    await page.getByRole("option", { name: /Studio NAS/ }).click();
    await expect(
      page.getByRole("region", { name: "Studio NASのファイル" }),
    ).toBeVisible();
    await expect(page.getByText("notes.txt", { exact: true })).toBeVisible();
    await expect(
      page.getByRole("button", { name: "新規フォルダ" }),
    ).toHaveCount(0);

    await toolbar.getByRole("button", { name: "Project Files" }).click();
    await expect(selector).toHaveValue("default");
    await expect(
      page.getByRole("region", { name: "Studio NASのファイル" }),
    ).toHaveCount(0);

    await page.getByRole("button", { name: "ストレージ設定" }).click();
    await expect(
      page.getByRole("region", { name: "ストレージ設定" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "閉じる", exact: true }).click();
    await expect(
      page.getByRole("region", { name: "ストレージ設定" }),
    ).toHaveCount(0);

    const layout = await page.evaluate(() => {
      const scroller = document.querySelector<HTMLElement>(
        '[data-files-scroll-region="browser"]',
      );
      return {
        documentOverflow:
          document.documentElement.scrollHeight -
          document.documentElement.clientHeight,
        scrollerOverflow: scroller
          ? scroller.scrollHeight - scroller.clientHeight
          : -1,
      };
    });
    expect(layout.documentOverflow).toBeLessThanOrEqual(1);
    expect(layout.scrollerOverflow).toBeGreaterThan(0);

    await page.setViewportSize({ width: 900, height: 700 });
    await expect(toolbar).toBeVisible();
    await expect(selector).toBeVisible();
    const aligned = await toolbar.evaluate((element) => {
      const buttons = Array.from(element.querySelectorAll("button"));
      const projectTab = buttons.find(
        (button) => button.textContent?.trim() === "Project Files",
      );
      const storage = element.querySelector('[aria-label="ストレージ選択"]');
      if (
        !(projectTab instanceof HTMLElement) ||
        !(storage instanceof HTMLElement)
      ) {
        return false;
      }
      return (
        Math.abs(
          projectTab.getBoundingClientRect().y -
            storage.getBoundingClientRect().y,
        ) < 16
      );
    });
    expect(aligned).toBe(true);
  });

  test("leaves normal Files usable when the optional storage catalog is unavailable", async ({
    page,
  }) => {
    await installStorageAndFilerMocks(page, { catalogFailure: true });
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.goto("/filer");

    const toolbar = page.locator('[data-files-toolbar="source-storage"]');
    await expect(
      toolbar.getByRole("button", { name: "Project Files" }),
    ).toBeVisible();
    await expect(
      toolbar.getByRole("button", { name: "User Files" }),
    ).toBeVisible();
    await expect(
      page.locator('[data-files-scroll-region="browser"]'),
    ).toBeVisible();
    await expect(
      page.getByRole("combobox", { name: "ストレージ選択" }),
    ).toHaveCount(0);
    expect(pageErrors).toEqual([]);
  });
});
