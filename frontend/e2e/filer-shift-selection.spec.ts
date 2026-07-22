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
  archive: `${rootPath}/bundle.tar.gz`,
};

type ExplorerOperation = {
  method: string;
  pathname: string;
  payload: unknown;
};

async function mockFilerApis(
  page: import("@playwright/test").Page,
  operations: ExplorerOperation[] = [],
  options: { failDownloads?: boolean; listDelayAfterFirstMs?: number } = {},
) {
  let listRequestCount = 0;
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
      listRequestCount += 1;
      if (listRequestCount > 1 && options.listDelayAfterFirstMs) {
        await new Promise((resolve) =>
          setTimeout(resolve, options.listDelayAfterFirstMs),
        );
      }
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
            {
              name: "bundle.tar.gz",
              path: paths.archive,
              type: "application/gzip",
              size: 40,
              extension: ".gz",
            },
          ],
          total_items: 6,
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

    if (
      url.pathname === "/api/python-proxy/explorer/download" ||
      url.pathname === "/api/python-proxy/explorer/archive" ||
      url.pathname === "/api/python-proxy/explorer/extract"
    ) {
      const payload = route.request().postDataJSON();
      operations.push({
        method: route.request().method(),
        pathname: url.pathname,
        payload,
      });
      if (url.pathname.endsWith("/download")) {
        if (options.failDownloads) {
          await route.fulfill({
            status: 500,
            json: { detail: "テスト用ダウンロードエラー" },
          });
          return;
        }
        const requestedPaths = (payload as { paths?: string[] }).paths ?? [];
        const filename =
          requestedPaths.length > 1
            ? "archive.zip"
            : requestedPaths[0] === paths.folderA
              ? "folder-a.zip"
              : "file-c.txt";
        await route.fulfill({
          body: "mock archive",
          contentType: "application/zip",
          headers: {
            "Content-Disposition": `attachment; filename="${filename}"`,
          },
        });
      } else if (url.pathname.endsWith("/archive")) {
        await route.fulfill({
          json: { success: true, archive_name: "archive.zip" },
        });
      } else {
        await route.fulfill({
          json: {
            success: true,
            extracted: [{ archive_name: "bundle.tar.gz" }],
          },
        });
      }
      return;
    }

    await route.fulfill({ json: {} });
  });
}

function item(page: import("@playwright/test").Page, path: string) {
  return page.locator(`[data-explorer-item-path="${path}"]`);
}

test.describe("ファイラーの選択操作", () => {
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
    await expect(
      item(page, paths.fileD),
      runtimeErrors.join("\n"),
    ).toBeVisible();

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
    await expect(
      item(page, paths.fileD),
      runtimeErrors.join("\n"),
    ).toBeVisible();

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

  test("Ctrl+Xした項目をグリッドとリストで薄く表示する", async ({ page }) => {
    await page.goto("/filer");
    await expect(
      item(page, paths.fileC),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await item(page, paths.fileC).click();
    await page.keyboard.press("Control+x");

    await expect(item(page, paths.fileC)).toHaveCSS("opacity", "0.5");
    await expect(item(page, paths.fileD)).toHaveCSS("opacity", "1");

    await page.getByTitle("リスト表示").click();
    await expect(item(page, paths.fileC)).toHaveCSS("opacity", "0.5");

    await page.keyboard.press("Control+c");
    await expect(item(page, paths.fileC)).toHaveCSS("opacity", "1");
  });

  test("Ctrl+Shift+Lで単一ファイル・単一フォルダ・複数選択をダウンロードする", async ({
    page,
  }) => {
    const operations: ExplorerOperation[] = [];
    await page.unroute("**/api/**");
    await mockFilerApis(page, operations);
    await page.goto("/filer");
    await expect(
      item(page, paths.fileC),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await item(page, paths.fileC).click();
    let downloadPromise = page.waitForEvent("download");
    await page.keyboard.press("Control+Shift+l");
    await expect.poll(() => operations.length).toBe(1);
    let download = await downloadPromise;
    expect(download.suggestedFilename()).toBe("file-c.txt");

    await item(page, paths.folderA).click();
    downloadPromise = page.waitForEvent("download");
    await page.keyboard.press("Control+Shift+l");
    download = await downloadPromise;
    expect(download.suggestedFilename()).toBe("folder-a.zip");

    await item(page, paths.fileC).click({ modifiers: ["Control"] });
    downloadPromise = page.waitForEvent("download");
    await page.keyboard.press("Control+Shift+l");
    download = await downloadPromise;

    expect(download.suggestedFilename()).toBe("archive.zip");
    expect(operations.slice(0, 2)).toEqual([
      {
        method: "POST",
        pathname: "/api/python-proxy/explorer/download",
        payload: { paths: [paths.fileC] },
      },
      {
        method: "POST",
        pathname: "/api/python-proxy/explorer/download",
        payload: { paths: [paths.folderA] },
      },
    ]);
    expect(operations).toContainEqual({
      method: "POST",
      pathname: "/api/python-proxy/explorer/download",
      payload: { paths: [paths.folderA, paths.fileC] },
    });
  });

  test("Ctrl+IとCtrl+Uで選択対象を圧縮・展開APIへ渡す", async ({ page }) => {
    const operations: ExplorerOperation[] = [];
    await page.unroute("**/api/**");
    await mockFilerApis(page, operations);
    await page.goto("/filer");
    await expect(
      item(page, paths.fileC),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await item(page, paths.folderA).click();
    await item(page, paths.fileC).click({ modifiers: ["Control"] });
    await page.keyboard.press("Control+i");
    await expect.poll(() => operations.length).toBe(1);
    expect(operations[0]).toEqual({
      method: "POST",
      pathname: "/api/python-proxy/explorer/archive",
      payload: { paths: [paths.folderA, paths.fileC], dest: rootPath },
    });

    await item(page, paths.archive).click();
    await page.keyboard.press("Control+u");
    await expect.poll(() => operations.length).toBe(2);
    expect(operations[1]).toEqual({
      method: "POST",
      pathname: "/api/python-proxy/explorer/extract",
      payload: { paths: [paths.archive], dest: rootPath },
    });
  });

  test("入力欄ではCtrl+Shift+Lをダウンロード操作として処理しない", async ({
    page,
  }) => {
    const operations: ExplorerOperation[] = [];
    await page.unroute("**/api/**");
    await mockFilerApis(page, operations);
    await page.goto("/filer");
    await expect(
      item(page, paths.fileC),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await page.getByPlaceholder("現在のフォルダ内を検索...").focus();
    await page.keyboard.press("Control+Shift+l");
    await page.waitForTimeout(100);
    expect(operations).toEqual([]);
  });

  test("Ctrl+Shift+Lのダウンロード失敗を日本語で通知する", async ({ page }) => {
    const operations: ExplorerOperation[] = [];
    await page.unroute("**/api/**");
    await mockFilerApis(page, operations, { failDownloads: true });
    await page.goto("/filer");
    await expect(
      item(page, paths.fileC),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await item(page, paths.fileC).click();
    await page.keyboard.press("Control+Shift+l");
    await expect(page.getByText(/ダウンロードに失敗しました/)).toBeVisible();
    expect(operations).toContainEqual({
      method: "POST",
      pathname: "/api/python-proxy/explorer/download",
      payload: { paths: [paths.fileC] },
    });
  });

  test("フォルダ読み込み中は旧選択へのファイル操作を発火しない", async ({
    page,
  }) => {
    const operations: ExplorerOperation[] = [];
    await page.unroute("**/api/**");
    await mockFilerApis(page, operations, { listDelayAfterFirstMs: 500 });
    await page.goto("/filer");
    await expect(
      item(page, paths.folderA),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await item(page, paths.folderA).click();
    const navigationRequest = page.waitForRequest((request) =>
      request.url().includes("/api/python-proxy/explorer/list"),
    );
    await page.keyboard.press("Enter");
    await navigationRequest;
    await page.keyboard.press("Control+Shift+l");
    await page.keyboard.press("Control+i");
    await page.keyboard.press("Control+u");
    await page.waitForTimeout(100);
    expect(operations).toEqual([]);
  });

  test("ショートカットヘルプに圧縮・展開・ダウンロードを表示する", async ({
    page,
  }) => {
    await page.goto("/filer");
    await expect(
      item(page, paths.fileC),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await page.getByTitle("ショートカット一覧 (?)").click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("選択中の項目をZIP圧縮");
    await expect(dialog).toContainText("選択中の圧縮ファイルを展開");
    await expect(dialog).toContainText("選択中の項目をダウンロード");
    await expect(dialog).toContainText("Ctrl+Shift+L");
  });
});
