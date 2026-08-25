import { expect, test } from "@playwright/test";

import { addAuthCookie, E2E_USER_ID } from "./support/auth";

const project = {
  id: "project-1",
  name: "Quick Launcher Project",
  description: null,
  slug: "quick-launcher-project",
  color: "#2563eb",
  space_id: "space-1",
  is_completed: false,
};

const space = {
  id: "space-1",
  name: "Quick Launcher Space",
  slug: "quick-launcher-space",
  description: null,
  color: "#2563eb",
};

const rootPath = `_projects/project_${project.id}`;
const workPath = `${rootPath}/work`;
const dailyPath = `${rootPath}/daily`;
const toolPath = `${rootPath}/tool`;
const wikiPath = `${rootPath}/wiki`;
const textPath = `${rootPath}/notes.txt`;

type Bookmark = {
  id: string;
  user_id: string;
  name: string;
  path: string;
  sort_order: number;
  kind?: "bookmark" | "folder";
  parent_id?: string | null;
};

async function mockQuickLauncherApis(page: import("@playwright/test").Page) {
  const state: { bookmarks: Bookmark[] } = {
    bookmarks: [
      {
        id: "f-ai",
        user_id: E2E_USER_ID,
        name: "AI",
        path: "aoitalk-bookmark-folder:ai",
        kind: "folder",
        parent_id: null,
        sort_order: 0,
      },
      {
        id: "b-tool",
        user_id: E2E_USER_ID,
        name: "tool",
        path: toolPath,
        parent_id: "f-ai",
        sort_order: 0,
      },
      {
        id: "b-work",
        user_id: E2E_USER_ID,
        name: "work",
        path: workPath,
        parent_id: null,
        sort_order: 1,
      },
      {
        id: "b-daily",
        user_id: E2E_USER_ID,
        name: "daily",
        path: dailyPath,
        parent_id: null,
        sort_order: 2,
      },
    ],
  };

  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();

    if (url.pathname === "/api/auth/status") {
      await route.fulfill({
        json: {
          authenticated: true,
          user: { id: E2E_USER_ID, username: "__playwright_e2e__", role: "admin" },
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
      const requestedPath = url.searchParams.get("path") || rootPath;
      const common = {
        success: true,
        current_path: requestedPath,
        parent_path: requestedPath === rootPath ? null : rootPath,
        can_go_up: requestedPath !== rootPath,
        directories: [],
        files: [],
        total_items: 0,
      };
      if (requestedPath === rootPath) {
        await route.fulfill({
          json: {
            ...common,
            files: [
              {
                name: "notes.txt",
                path: textPath,
                type: "text/plain",
                size: 12,
                extension: ".txt",
              },
            ],
            total_items: 1,
          },
        });
        return;
      }
      await route.fulfill({ json: common });
      return;
    }
    if (url.pathname === "/api/python-proxy/explorer/content") {
      expect(url.searchParams.get("path")).toBe(textPath);
      await route.fulfill({
        json: {
          success: true,
          content: "hello from quick launcher",
          path: textPath,
          name: "notes.txt",
          extension: ".txt",
          size_bytes: 28,
          modified_at: "2026-08-01T00:00:00Z",
        },
      });
      return;
    }
    if (url.pathname === "/api/python-proxy/explorer/bookmarks") {
      if (method === "GET") {
        await route.fulfill({ json: { success: true, bookmarks: state.bookmarks } });
        return;
      }
      if (method === "POST") {
        const body = JSON.parse(route.request().postData() ?? "{}");
        const bookmark: Bookmark = {
          id: `bookmark-${state.bookmarks.length + 1}`,
          user_id: E2E_USER_ID,
          name: body.name || body.path,
          path: body.path ?? `aoitalk-bookmark-folder:${state.bookmarks.length + 1}`,
          sort_order: state.bookmarks.length,
          kind: body.kind ?? "bookmark",
          parent_id: body.parent_id ?? null,
        };
        state.bookmarks.push(bookmark);
        await route.fulfill({ json: { success: true, bookmark } });
        return;
      }
    }
    if (url.pathname.startsWith("/api/python-proxy/explorer/bookmarks/")) {
      const id = url.pathname.split("/").pop();
      if (method === "PATCH") {
        const body = JSON.parse(route.request().postData() ?? "{}");
        const bookmark = state.bookmarks.find((item) => item.id === id);
        if (bookmark) Object.assign(bookmark, body);
        await route.fulfill({ json: { success: true, bookmark } });
        return;
      }
    }
    if (url.pathname === "/api/python-proxy/explorer/launchers" && method === "GET") {
      await route.fulfill({ json: { success: true, launchers: [] } });
      return;
    }
    if (url.pathname === "/api/python-proxy/storage/contexts") {
      await route.fulfill({
        json: {
          success: true,
          contexts: [],
          current_context: { type: "personal", id: E2E_USER_ID },
          is_admin: true,
        },
      });
      return;
    }
    if (url.pathname === "/api/python-proxy/health") {
      await route.fulfill({ status: 503, json: { ok: false } });
      return;
    }
    if (url.pathname === "/api/hydrus/settings") {
      await route.fulfill({ json: { success: true, settings: {} } });
      return;
    }
    if (url.pathname === "/api/migemo") {
      await route.fulfill({ json: { terms: [] } });
      return;
    }
    await route.fulfill({ json: {} });
  });
}

test.describe("Files bookmark quick launcher", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await page.addInitScript(() => {
      if (sessionStorage.getItem("__quick_launcher_fixture_initialized") === "1") return;
      localStorage.clear();
      localStorage.setItem("selectedProjectId", "project-1");
      localStorage.setItem("filer-tab", "workspace");
      localStorage.setItem("explorer-view-mode", "grid");
      sessionStorage.setItem("__quick_launcher_fixture_initialized", "1");
    });
    await mockQuickLauncherApis(page);
  });

  async function openQuickLauncher(page: import("@playwright/test").Page) {
    await page.locator('[data-shell-region="files-canvas"]').focus();
    await page.keyboard.press("Alt+A");
    await expect(page.getByTestId("files-bookmark-quick-launcher-trigger")).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    await expect(page.getByTestId("files-bookmark-quick-launcher-menu")).toBeVisible();
  }

  async function expectQuickLauncherCentered(
    page: import("@playwright/test").Page,
    viewport: { width: number; height: number },
  ) {
    const menu = page.getByTestId("files-bookmark-quick-launcher-menu");
    await expect(menu).toBeVisible();

    // The menu animates in and the positioner may need one layout pass after a
    // viewport resize. Poll the two axes independently so the assertion waits
    // for the final bounding box instead of racing that layout work.
    await expect
      .poll(async () => {
        const box = await menu.boundingBox();
        return box
          ? Math.abs(box.x + box.width / 2 - viewport.width / 2)
          : Number.POSITIVE_INFINITY;
      })
      .toBeLessThanOrEqual(8);
    await expect
      .poll(async () => {
        const box = await menu.boundingBox();
        return box
          ? Math.abs(box.y + box.height / 2 - viewport.height / 2)
          : Number.POSITIVE_INFINITY;
      })
      .toBeLessThanOrEqual(8);
  }

  test("centers the quick launcher and follows viewport resize", async ({ page }) => {
    const wideViewport = { width: 1200, height: 800 };
    const compactViewport = { width: 900, height: 600 };
    await page.setViewportSize(wideViewport);
    await page.goto("/filer");

    await openQuickLauncher(page);
    await expectQuickLauncherCentered(page, wideViewport);

    // Keep the launcher open while resizing to ensure its positioner responds
    // to the new viewport rather than only centering on initial open.
    await page.setViewportSize(compactViewport);
    await expectQuickLauncherCentered(page, compactViewport);
  });

  test("Alt+A navigates work, opens AI submenu for tool, and blocks duplicate mnemonics", async ({ page }) => {
    await page.goto("/filer");
    const sidebar = page.getByTestId("files-bookmark-launcher-sidebar");
    await expect(sidebar).toBeVisible();
    await expect(sidebar.getByRole("option", { name: /^work/ })).toBeVisible();

    const listWork = page.waitForRequest((request) => {
      return (
        new URL(request.url()).pathname === "/api/python-proxy/explorer/list" &&
        request.method() === "GET" &&
        new URL(request.url()).searchParams.get("path") === workPath
      );
    });
    await openQuickLauncher(page);
    await page.keyboard.press("W");
    await listWork;

    const listDaily = page.waitForRequest((request) => {
      return (
        new URL(request.url()).pathname === "/api/python-proxy/explorer/list" &&
        request.method() === "GET" &&
        new URL(request.url()).searchParams.get("path") === dailyPath
      );
    });
    await openQuickLauncher(page);
    await page.keyboard.press("D");
    await listDaily;

    const listTool = page.waitForRequest((request) => {
      return (
        new URL(request.url()).pathname === "/api/python-proxy/explorer/list" &&
        request.method() === "GET" &&
        new URL(request.url()).searchParams.get("path") === toolPath
      );
    });
    await openQuickLauncher(page);
    await page.keyboard.press("A");
    await expect(page.getByRole("menuitem", { name: /^tool/ })).toBeVisible();
    await page.keyboard.press("T");
    await listTool;
  });

  test("duplicate work/wiki mnemonics do not navigate and Esc closes the launcher", async ({ page }) => {
    await page.route("**/api/python-proxy/explorer/bookmarks", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        json: {
          success: true,
          bookmarks: [
            {
              id: "b-work",
              user_id: E2E_USER_ID,
              name: "work",
              path: workPath,
              sort_order: 0,
              kind: "bookmark",
              parent_id: null,
            },
            {
              id: "b-wiki",
              user_id: E2E_USER_ID,
              name: "wiki",
              path: wikiPath,
              sort_order: 1,
              kind: "bookmark",
              parent_id: null,
            },
          ],
        },
      });
    });

    await page.goto("/filer");
    await page.waitForResponse(
      (response) =>
        response.url().includes("/api/python-proxy/explorer/bookmarks") &&
        response.request().method() === "GET",
    );
    let listCalls = 0;
    page.on("request", (request) => {
      if (new URL(request.url()).pathname === "/api/python-proxy/explorer/list") listCalls += 1;
    });

    await openQuickLauncher(page);
    await page.keyboard.press("W");
    await page.waitForTimeout(200);
    expect(listCalls).toBe(0);

    await page.keyboard.press("Escape");
    await expect(page.getByTestId("files-bookmark-quick-launcher-trigger")).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  test("duplicate work/wiki items open the arrow-selected target with Enter", async ({ page }) => {
    await page.route("**/api/python-proxy/explorer/bookmarks", async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      await route.fulfill({
        json: {
          success: true,
          bookmarks: [
            {
              id: "b-work",
              user_id: E2E_USER_ID,
              name: "work",
              path: workPath,
              sort_order: 0,
              kind: "bookmark",
              parent_id: null,
            },
            {
              id: "b-wiki",
              user_id: E2E_USER_ID,
              name: "wiki",
              path: wikiPath,
              sort_order: 1,
              kind: "bookmark",
              parent_id: null,
            },
          ],
        },
      });
    });

    await page.goto("/filer");
    await page.waitForResponse(
      (response) =>
        response.url().includes("/api/python-proxy/explorer/bookmarks") &&
        response.request().method() === "GET",
    );

    const listWiki = page.waitForRequest((request) => {
      return (
        new URL(request.url()).pathname === "/api/python-proxy/explorer/list" &&
        request.method() === "GET" &&
        new URL(request.url()).searchParams.get("path") === wikiPath
      );
    });
    await openQuickLauncher(page);
    await expect(page.getByRole("menuitem", { name: /^work/ })).toBeFocused();
    await page.keyboard.press("ArrowDown");
    await expect(page.getByRole("menuitem", { name: /^wiki/ })).toBeFocused();
    await page.keyboard.press("Enter");
    await listWiki;
  });

  test("ArrowRight opens AI submenu and ArrowLeft returns to root menu", async ({ page }) => {
    await page.goto("/filer");
    await page.waitForResponse(
      (response) =>
        response.url().includes("/api/python-proxy/explorer/bookmarks") &&
        response.request().method() === "GET",
    );
    await openQuickLauncher(page);
    const menu = page.getByTestId("files-bookmark-quick-launcher-menu");
    await expect(menu).toHaveAttribute("data-open", "");
    const aiItem = page.getByRole("menuitem", { name: /^AI/ });
    await expect(aiItem).toBeVisible();
    await expect(aiItem).toBeFocused();
    await page.keyboard.press("ArrowRight");
    const toolItem = page.getByRole("menuitem", { name: /^tool/ });
    await expect(toolItem).toBeVisible();
    await page.keyboard.press("ArrowLeft");
    await expect(aiItem).toBeFocused();
    await expect(aiItem).toHaveAttribute("aria-expanded", "false");
    await expect(toolItem).not.toBeVisible();
  });

  test("keeps launcher open from editor focus and blocks incremental search while open", async ({ page }) => {
    await page.goto("/filer");
    const file = page.locator(`[data-explorer-item-path="${textPath}"]`);
    await file.dblclick();
    await expect(page.locator('[data-shell-region="files-editor"]')).toBeVisible();

    const editor = page.locator('.cm-content[contenteditable="true"]');
    await expect(editor).toBeVisible();
    await editor.click();
    await expect(editor).toBeFocused();
    const activeEditorState = await page.evaluate(() => {
      const active = document.activeElement;
      return {
        isCodeMirror: active instanceof HTMLElement && Boolean(active.closest(".cm-editor")),
      };
    });
    expect(activeEditorState.isCodeMirror).toBe(true);

    await page.keyboard.press("Alt+A");
    await expect(page.getByTestId("files-bookmark-quick-launcher-menu")).toBeVisible();

    let migemoCalls = 0;
    page.on("request", (request) => {
      if (new URL(request.url()).pathname === "/api/migemo") migemoCalls += 1;
    });
    await page.keyboard.press("x");
    await expect.poll(() => migemoCalls).toBe(0);
  });

  test("Alt+Q and Alt+E shortcuts still switch sidebar tabs", async ({ page }) => {
    await page.goto("/filer");
    const sidebar = page.getByTestId("files-bookmark-launcher-sidebar");
    const bookmarksTab = sidebar.getByRole("tab", { name: /ブックマーク/ });
    const launchersTab = sidebar.getByRole("tab", { name: /ランチャー/ });

    await page.keyboard.press("Alt+E");
    await expect(launchersTab).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("Alt+Q");
    await expect(bookmarksTab).toHaveAttribute("aria-selected", "true");
  });
});
