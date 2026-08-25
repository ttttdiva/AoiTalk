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
  space_id: "space-1",
  is_completed: false,
};

const space = {
  id: "space-1",
  name: "Filer Space",
  slug: "filer-space",
  description: null,
  color: "#2563eb",
};

const rootPath = `_projects/project_${project.id}`;
const paths = {
  folderA: `${rootPath}/folder-a`,
  folderB: `${rootPath}/folder-b`,
  fileC: `${rootPath}/file-c.txt`,
  fileD: `${rootPath}/file-d.txt`,
  fileE: `${rootPath}/file-e.txt`,
  nestedA: `${rootPath}/folder-a/alpha.txt`,
  nestedZ: `${rootPath}/folder-a/zebra.txt`,
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
      await route.fulfill({ json: { spaces: [space], total: 1 } });
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
      const requestedPath = url.searchParams.get("path") ?? rootPath;
      if (requestedPath === paths.folderA) {
        await new Promise((resolve) => setTimeout(resolve, 250));
        await route.fulfill({
          json: {
            success: true,
            current_path: paths.folderA,
            parent_path: rootPath,
            can_go_up: true,
            directories: [],
            files: [
              {
                name: "alpha.txt",
                path: paths.nestedA,
                type: "text/plain",
                size: 30,
                extension: ".txt",
              },
              {
                name: "zebra.txt",
                path: paths.nestedZ,
                type: "text/plain",
                size: 40,
                extension: ".txt",
              },
            ],
            total_items: 2,
          },
        });
        return;
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
            { name: "file-c.txt", path: paths.fileC, type: "text/plain", size: 10, extension: ".txt" },
            { name: "file-d.txt", path: paths.fileD, type: "text/plain", size: 20, extension: ".txt" },
            { name: "file-e.txt", path: paths.fileE, type: "text/plain", size: 30, extension: ".txt" },
          ],
          total_items: 5,
        },
      });
      return;
    }
    if (url.pathname === "/api/python-proxy/explorer/search") {
      await route.fulfill({
        json: {
          success: true,
          results: [
            {
              name: "alpha.txt",
              path: paths.nestedA,
              kind: "file",
              type: "text/plain",
              size_bytes: 30,
              extension: ".txt",
            },
          ],
          total: 1,
          total_returned: 1,
          root_path: rootPath,
          truncated: false,
          query: url.searchParams.get("q") ?? "",
        },
      });
      return;
    }
    if (url.pathname === "/api/python-proxy/explorer/content") {
      await route.fulfill({
        json: {
          success: true,
          content: "file c content",
          path: paths.fileC,
          name: "file-c.txt",
          extension: ".txt",
          size_bytes: 14,
          modified_at: "2026-08-01T00:00:00Z",
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
    await expect(item(page, paths.folderA)).toHaveClass(/border-primary/);

    // 一覧にフォーカスがある状態で t を押す
    await page.locator('[data-shell-workspace="files"]').focus();
    await page.keyboard.press("t");

    // 期待1: タスク作成ダイアログ(role=dialog)が開かない
    await expect(page.getByRole("dialog")).toHaveCount(0);

    // 期待2: インクリメンタル検索が動き、"t" を含む最初の項目 file-c.txt に選択が移る
    await expect(item(page, paths.fileC)).toHaveClass(/border-primary/);
    await expect(item(page, paths.folderA)).not.toHaveClass(/border-primary/);
  });

  test("/filer では l を押しても /tasks へ遷移せず、単発キー全体が抑止される", async ({
    page,
  }) => {
    await page.goto("/filer");
    await expect(item(page, paths.folderA)).toHaveClass(/border-primary/);

    await page.locator('[data-shell-workspace="files"]').focus();
    // グローバルでは l = /tasks へ遷移。/filer ガードで抑止されるはず。
    await page.keyboard.press("l");

    // ページ遷移せず /filer に留まる(ガードが t 以外の単発キーも抑止している証拠)
    await expect(page).toHaveURL(/\/filer/);
    await expect(page.getByRole("dialog")).toHaveCount(0);
    // 一覧が生きたまま(インクリメンタル検索側がキーを消費)であることを確認
    await expect(item(page, paths.fileD)).toBeVisible();
  });

  test("HomeとEndで表示中の先頭・末尾へ移動する", async ({ page }) => {
    await page.goto("/filer");
    await expect(item(page, paths.folderA)).toHaveClass(/border-primary/);
    const keyboardRoot = item(page, paths.folderA).locator(
      "xpath=ancestor::*[@tabindex='-1'][1]",
    );
    await expect(keyboardRoot).toBeFocused();

    await page.keyboard.press("End");
    await expect(item(page, paths.fileE)).toHaveClass(/border-primary/);

    await page.keyboard.press("Home");
    await expect(item(page, paths.folderA)).toHaveClass(/border-primary/);
  });

  test("Ctrl+Sで即席フィルターへフォーカスし、名前の部分一致で一覧を絞り込む", async ({
    page,
  }) => {
    await page.goto("/filer");
    await expect(item(page, paths.folderA)).toBeVisible();

    await page.getByTitle("リスト表示").click();
    await page.keyboard.press("Control+s");

    const filter = page.getByRole("textbox", {
      name: "ファイル名の即席フィルター",
    });
    await expect(filter).toBeFocused();
    await filter.fill("FILE-D");

    await expect(item(page, paths.fileD)).toBeVisible();
    await expect(item(page, paths.folderA)).toHaveCount(0);
    await expect(item(page, paths.fileC)).toHaveCount(0);
    await expect(page.getByText("1/5件")).toBeVisible();

    await filter.press("Escape");
    await expect(filter).toHaveCount(0);
    await expect(item(page, paths.folderA)).toBeVisible();
    await expect(item(page, paths.fileC)).toBeVisible();
  });

  test("Ctrl+Fで再帰検索を開いて実行し、Escapeで通常一覧へ戻る", async ({
    page,
  }) => {
    await page.goto("/filer");
    await expect(item(page, paths.folderA)).toBeVisible();

    await page.evaluate(() => {
      document.addEventListener("keydown", (event) => {
        if (event.ctrlKey && event.key.toLowerCase() === "f") {
          document.body.dataset.filerCtrlFDefaultPrevented = String(
            event.defaultPrevented,
          );
        }
      });
    });

    const panel = page.getByTestId("filer-search-panel");
    const searchInput = page.getByRole("textbox", {
      name: "ファイル名・フォルダ名の検索",
    });
    await page.keyboard.press("Control+Shift+f");
    await expect(panel).toHaveCount(0);
    await expect(page.locator("body")).toHaveAttribute(
      "data-filer-ctrl-f-default-prevented",
      "false",
    );

    await page.keyboard.press("Control+f");
    await expect(panel).toBeVisible();
    await expect(searchInput).toBeFocused();
    await expect(page.locator("body")).toHaveAttribute(
      "data-filer-ctrl-f-default-prevented",
      "true",
    );

    const searchRequest = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return (
        url.pathname === "/api/python-proxy/explorer/search" &&
        url.searchParams.get("q") === "alpha" &&
        url.searchParams.get("root") === rootPath
      );
    });
    await searchInput.fill("alpha");
    await searchInput.press("Enter");
    await searchRequest;

    await expect(item(page, paths.nestedA)).toBeVisible();
    await expect(item(page, paths.folderA)).toHaveCount(0);
    await expect(panel.getByText("1件", { exact: true })).toBeVisible();

    await searchInput.press("Escape");
    await expect(panel).toHaveCount(0);
    await expect(item(page, paths.folderA)).toBeVisible();
    await expect(item(page, paths.fileC)).toBeVisible();

    await page.keyboard.press("Meta+f");
    await expect(panel).toBeVisible();
    await expect(searchInput).toBeFocused();
  });

  test("エディタ内のCtrl+Fはファイラー検索に横取りされない", async ({
    page,
  }) => {
    await page.goto("/filer");
    const file = item(page, paths.fileC);
    await expect(file).toBeVisible();
    await file.dblclick();

    const editor = page.locator('.cm-content[contenteditable="true"]');
    await expect(editor).toBeVisible();
    await expect(editor).toBeFocused();

    await page.keyboard.press("Control+f");
    await expect(page.getByTestId("filer-search-panel")).toHaveCount(0);
    await expect(editor).toBeVisible();
  });

  test("ディレクトリ移動後はインクリメンタル検索を新しい文字列で開始する", async ({
    page,
  }) => {
    await page.goto("/filer");
    await expect(item(page, paths.folderA)).toHaveClass(/border-primary/);

    // 遷移前に検索文字列を残し、1秒の継続判定内に移動・次の検索を行う。
    await page.keyboard.press("a");
    const navigationRequest = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return (
        url.pathname === "/api/python-proxy/explorer/list" &&
        url.searchParams.get("path") === paths.folderA
      );
    });
    await page.keyboard.press("Enter");
    await navigationRequest;
    const searchRequest = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return (
        url.pathname === "/api/migemo" && url.searchParams.get("q") === "z"
      );
    });
    await page.keyboard.press("z");

    await expect(item(page, paths.nestedZ)).toBeVisible();
    await searchRequest;
    await expect(item(page, paths.nestedZ)).toHaveClass(/border-primary/);
    await expect(item(page, paths.nestedA)).not.toHaveClass(/border-primary/);
  });

  test("ディレクトリ読み込み中もボタンのSpace操作を妨げない", async ({
    page,
  }) => {
    await page.goto("/filer");
    await expect(item(page, paths.folderA)).toHaveClass(/border-primary/);

    const navigationRequest = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return (
        url.pathname === "/api/python-proxy/explorer/list" &&
        url.searchParams.get("path") === paths.folderA
      );
    });
    await page.keyboard.press("Enter");
    await navigationRequest;

    await page.getByTitle("リスト表示").focus();
    await page.keyboard.press("Space");
    await expect(page.getByTitle("グリッド表示")).toBeVisible();
  });

  test("Ctrl+Jで同じ検索条件の次の一致へ進み、末尾から先頭へwrapする", async ({
    page,
  }) => {
    await page.goto("/filer");
    await expect(item(page, paths.folderA)).toHaveClass(/border-primary/);

    await page.locator('[data-shell-workspace="files"]').focus();
    await page.keyboard.type("file");
    await expect(item(page, paths.fileC)).toHaveClass(/border-primary/);

    await page.keyboard.press("Control+j");
    await expect(item(page, paths.fileD)).toHaveClass(/border-primary/);

    await page.keyboard.press("Control+j");
    await expect(item(page, paths.fileE)).toHaveClass(/border-primary/);

    await page.keyboard.press("Control+j");
    await expect(item(page, paths.fileC)).toHaveClass(/border-primary/);
  });

  test("1秒の入力継続タイムアウト後もCtrl+Jは最後の検索条件を使える", async ({
    page,
  }) => {
    await page.goto("/filer");
    await expect(item(page, paths.folderA)).toHaveClass(/border-primary/);

    await page.locator('[data-shell-workspace="files"]').focus();
    await page.keyboard.type("file");
    await expect(item(page, paths.fileC)).toHaveClass(/border-primary/);

    await page.waitForTimeout(1300);
    await page.keyboard.press("Control+j");
    await expect(item(page, paths.fileD)).toHaveClass(/border-primary/);
  });

  test("継続時間を超えた通常文字入力は新しいインクリメンタル検索になる", async ({
    page,
  }) => {
    await page.goto("/filer");
    await expect(item(page, paths.folderA)).toHaveClass(/border-primary/);

    await page.locator('[data-shell-workspace="files"]').focus();
    await page.keyboard.type("file");
    await expect(item(page, paths.fileC)).toHaveClass(/border-primary/);

    await page.waitForTimeout(1300);
    await page.keyboard.press("b");
    await expect(item(page, paths.folderB)).toHaveClass(/border-primary/);
    await expect(item(page, paths.fileC)).not.toHaveClass(/border-primary/);
  });

  test("一致0件でもCtrl+Jでフォーカス状態が壊れない", async ({ page }) => {
    await page.goto("/filer");
    await expect(item(page, paths.folderA)).toHaveClass(/border-primary/);

    const keyboardRoot = page.locator('[data-shell-workspace="files"]');
    await keyboardRoot.focus();
    await page.keyboard.type("zzz");
    await expect(item(page, paths.folderA)).toHaveClass(/border-primary/);

    await page.keyboard.press("Control+j");
    await expect(item(page, paths.folderA)).toHaveClass(/border-primary/);
    await expect(keyboardRoot).toBeFocused();
  });

  test("Ctrl+Jがグローバルショートカットと二重発火しない", async ({
    page,
  }) => {
    await page.goto("/filer");
    await expect(item(page, paths.folderA)).toHaveClass(/border-primary/);

    await page.evaluate(() => {
      document.addEventListener("keydown", (event) => {
        if (event.ctrlKey && event.key.toLowerCase() === "j") {
          document.body.dataset.filerCtrlJClaimed = String(event.defaultPrevented);
        }
      });
    });

    await page.locator('[data-shell-workspace="files"]').focus();
    await page.keyboard.press("Control+j");
    await expect(page.locator("body")).toHaveAttribute(
      "data-filer-ctrl-j-claimed",
      "false",
    );
    await expect(item(page, paths.folderA)).toHaveClass(/border-primary/);

    await page.keyboard.type("file");
    await expect(item(page, paths.fileC)).toHaveClass(/border-primary/);

    await page.keyboard.press("Control+j");
    await expect(page.locator("body")).toHaveAttribute(
      "data-filer-ctrl-j-claimed",
      "true",
    );
    await expect(item(page, paths.fileD)).toHaveClass(/border-primary/);
    await expect
      .poll(() =>
        page.evaluate(() => document.activeElement?.tagName ?? ""),
      )
      .not.toBe("TEXTAREA");
  });
});
