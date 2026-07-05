import { expect, test } from "@playwright/test";

import { addAuthCookie } from "./support/auth";

const project = {
  id: "project-1",
  name: "Filer Project",
  description: null,
  slug: "filer-project",
  color: "#2563eb",
  space_id: null,
  is_completed: false,
};

const rootPath = `_projects/project_${project.id}`;
const paths = {
  folderA: `${rootPath}/folder-a`,
  folderB: `${rootPath}/folder-b`,
  fileC: `${rootPath}/file-c.txt`,
  fileD: `${rootPath}/file-d.txt`,
  fileE: `${rootPath}/file-e.txt`,
};

async function mockFilerApis(page: import("@playwright/test").Page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());

    if (url.pathname === "/api/auth/status") {
      await route.fulfill({
        json: {
          authenticated: true,
          user: { id: "user-1", username: "tester", role: "admin" },
        },
      });
      return;
    }

    if (url.pathname === "/api/projects") {
      await route.fulfill({ json: { projects: [project], total: 1 } });
      return;
    }

    if (url.pathname === "/api/spaces") {
      await route.fulfill({ json: { spaces: [], total: 0 } });
      return;
    }

    if (url.pathname === "/api/conversations") {
      await route.fulfill({ json: { conversations: [], total: 0 } });
      return;
    }

    if (url.pathname === `/api/projects/${project.id}/records`) {
      await route.fulfill({ json: { tables: [] } });
      return;
    }

    if (url.pathname === "/api/python-proxy/explorer/list") {
      await route.fulfill({
        json: {
          success: true,
          current_path: rootPath,
          parent_path: null,
          can_go_up: false,
          directories: [
            { name: "folder-a", path: paths.folderA, item_count: 0 },
            { name: "folder-b", path: paths.folderB, item_count: 0 },
          ],
          files: [
            {
              name: "file-c.txt",
              path: paths.fileC,
              type: "text/plain",
              size: 10,
              extension: ".txt",
            },
            {
              name: "file-d.txt",
              path: paths.fileD,
              type: "text/plain",
              size: 20,
              extension: ".txt",
            },
            {
              name: "file-e.txt",
              path: paths.fileE,
              type: "text/plain",
              size: 30,
              extension: ".txt",
            },
          ],
          total_items: 5,
        },
      });
      return;
    }

    if (url.pathname === "/api/python-proxy/explorer/bookmarks") {
      await route.fulfill({ json: { success: true, bookmarks: [] } });
      return;
    }

    if (url.pathname === "/api/python-proxy/storage/contexts") {
      await route.fulfill({
        json: {
          success: true,
          contexts: [],
          current_context: { type: "personal", id: "user-1" },
          is_admin: true,
        },
      });
      return;
    }

    if (url.pathname === "/api/python-proxy/health") {
      await route.fulfill({ status: 503, json: { ok: false } });
      return;
    }

    await route.fulfill({ json: {} });
  });
}

function item(page: import("@playwright/test").Page, path: string) {
  return page.locator(`[data-explorer-item-path="${path}"]`);
}

test.describe("ファイラーのShift範囲選択", () => {
  let runtimeErrors: string[];

  test.beforeEach(async ({ page }) => {
    runtimeErrors = [];
    page.on("pageerror", (error) => runtimeErrors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") runtimeErrors.push(message.text());
    });
    page.on("response", (response) => {
      if (response.status() >= 500) {
        runtimeErrors.push(`${response.status()} ${response.url()}`);
      }
    });
    await addAuthCookie(page);
    await page.addInitScript((projectId) => {
      localStorage.clear();
      localStorage.setItem("selectedProjectId", projectId);
      localStorage.setItem("filer-tab", "workspace");
      localStorage.setItem("explorer-view-mode", "grid");
    }, project.id);
    await mockFilerApis(page);
  });

  test("グリッド表示でアンカーからShiftクリック対象まで選択する", async ({
    page,
  }) => {
    await page.goto("/filer");
    await expect(item(page, paths.fileD), runtimeErrors.join("\n")).toBeVisible();

    await item(page, paths.folderB).click();
    await item(page, paths.fileD).click({ modifiers: ["Shift"] });

    await expect(item(page, paths.folderA)).not.toHaveClass(/bg-accent/);
    await expect(item(page, paths.folderB)).toHaveClass(/bg-accent/);
    await expect(item(page, paths.fileC)).toHaveClass(/bg-accent/);
    await expect(item(page, paths.fileD)).toHaveClass(/bg-accent/);
    await expect(item(page, paths.fileE)).not.toHaveClass(/bg-accent/);
  });

  test("リスト表示ではソート後の表示順でShift範囲選択する", async ({
    page,
  }) => {
    await page.goto("/filer");
    await expect(item(page, paths.fileD), runtimeErrors.join("\n")).toBeVisible();

    await page.getByTitle("リスト表示").click();
    await expect(page.getByTitle("グリッド表示")).toBeVisible();

    await item(page, paths.folderB).click();
    await item(page, paths.fileD).click({ modifiers: ["Shift"] });

    await expect(item(page, paths.folderA)).not.toHaveClass(/bg-accent/);
    await expect(item(page, paths.folderB)).toHaveClass(/bg-accent/);
    await expect(item(page, paths.fileC)).toHaveClass(/bg-accent/);
    await expect(item(page, paths.fileD)).toHaveClass(/bg-accent/);
    await expect(item(page, paths.fileE)).not.toHaveClass(/bg-accent/);
  });
});
