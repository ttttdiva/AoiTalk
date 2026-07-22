import { expect, test } from "@playwright/test";

import { addAuthCookie } from "./support/auth";

// /filer のインクリメンタル検索(一覧上で入力欄なしに単発キーを拾う)が、
// グローバル単発キーショートカット(t=タスク作成 等)と衝突しないことを検証する。
// keyboard-shortcuts.tsx に追加した `pathname.startsWith("/filer")` ガードの回帰テスト。

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

async function mockApis(page: import("@playwright/test").Page) {
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
    if (url.pathname === "/api/notifications") {
      await route.fulfill({ json: [] });
      return;
    }
    if (url.pathname === "/api/tasks") {
      await route.fulfill({ json: [] });
      return;
    }
    if (url.pathname === "/api/time-entries/active") {
      await route.fulfill({ json: null });
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
            { name: "file-c.txt", path: paths.fileC, type: "text/plain", size: 10, extension: ".txt" },
            { name: "file-d.txt", path: paths.fileD, type: "text/plain", size: 20, extension: ".txt" },
            { name: "file-e.txt", path: paths.fileE, type: "text/plain", size: 30, extension: ".txt" },
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

test.describe("ファイラーの単発キーとグローバルショートカット衝突", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await page.addInitScript((projectId) => {
      localStorage.clear();
      localStorage.setItem("selectedProjectId", projectId);
      localStorage.setItem("filer-tab", "workspace");
      localStorage.setItem("explorer-view-mode", "grid");
    }, project.id);
    await mockApis(page);
  });

  test("/filer で t を押してもタスク作成ダイアログが開かず、インクリメンタル検索が動く", async ({
    page,
  }) => {
    await page.goto("/filer");
    // 初期表示: 先頭 folder-a が自動フォーカス(選択)される
    await expect(item(page, paths.folderA)).toHaveClass(/bg-accent/);

    // 一覧にフォーカスがある状態で t を押す
    await page.locator("body").click({ position: { x: 5, y: 5 } });
    await page.keyboard.press("t");

    // 期待1: タスク作成ダイアログ(role=dialog)が開かない
    await expect(page.getByRole("dialog")).toHaveCount(0);

    // 期待2: インクリメンタル検索が動き、"t" を含む最初の項目 file-c.txt に選択が移る
    await expect(item(page, paths.fileC)).toHaveClass(/bg-accent/);
    await expect(item(page, paths.folderA)).not.toHaveClass(/bg-accent/);
  });

  test("/filer では l を押しても /tasks へ遷移せず、単発キー全体が抑止される", async ({
    page,
  }) => {
    await page.goto("/filer");
    await expect(item(page, paths.folderA)).toHaveClass(/bg-accent/);

    await page.locator("body").click({ position: { x: 5, y: 5 } });
    // グローバルでは l = /tasks へ遷移。/filer ガードで抑止されるはず。
    await page.keyboard.press("l");

    // ページ遷移せず /filer に留まる(ガードが t 以外の単発キーも抑止している証拠)
    await expect(page).toHaveURL(/\/filer/);
    await expect(page.getByRole("dialog")).toHaveCount(0);
    // 一覧が生きたまま(インクリメンタル検索側がキーを消費)であることを確認
    await expect(item(page, paths.fileD)).toBeVisible();
  });
});
