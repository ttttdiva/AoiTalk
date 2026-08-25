import { expect, test } from "@playwright/test";

import { addAuthCookie, E2E_USER_ID } from "./support/auth";

const project = {
  id: "project-1",
  name: "Sidebar Search Project",
  description: null,
  slug: "sidebar-search-project",
  color: "#2563eb",
  space_id: "space-1",
  is_completed: false,
};

const space = {
  id: "space-1",
  name: "Sidebar Search Space",
  slug: "sidebar-search-space",
  description: null,
  color: "#2563eb",
};

const rootPath = `_projects/project_${project.id}`;
const dailyPath = `${rootPath}/daily`;
const docsPath = `${rootPath}/docs`;
const downloadPath = `${rootPath}/download`;
const workPath = `${rootPath}/work`;
const docsReadmePath = `${docsPath}/readme.txt`;
const notesPath = `${rootPath}/notes.txt`;
const noteOldPath = `${rootPath}/note-old.txt`;
const readmePath = `${rootPath}/readme.txt`;

type Bookmark = {
  id: string;
  user_id: string;
  name: string;
  path: string;
  sort_order: number;
  kind?: "bookmark" | "folder";
  parent_id?: string | null;
};

type Launcher = {
  id: string;
  user_id: string;
  name: string;
  path: string;
  sort_order: number;
};

async function mockSidebarSearchApis(page: import("@playwright/test").Page) {
  const state: { bookmarks: Bookmark[]; launchers: Launcher[] } = {
    bookmarks: [
      {
        id: "b-daily",
        user_id: E2E_USER_ID,
        name: "daily",
        path: dailyPath,
        parent_id: null,
        sort_order: 0,
      },
      {
        id: "b-docs",
        user_id: E2E_USER_ID,
        name: "docs",
        path: docsPath,
        parent_id: null,
        sort_order: 1,
      },
      {
        id: "b-download",
        user_id: E2E_USER_ID,
        name: "download",
        path: downloadPath,
        parent_id: null,
        sort_order: 2,
      },
      {
        id: "b-work",
        user_id: E2E_USER_ID,
        name: "work",
        path: workPath,
        parent_id: null,
        sort_order: 3,
      },
    ],
    launchers: [
      {
        id: "l-notes",
        user_id: E2E_USER_ID,
        name: "notes.txt",
        path: notesPath,
        sort_order: 0,
      },
      {
        id: "l-note-old",
        user_id: E2E_USER_ID,
        name: "note-old.txt",
        path: noteOldPath,
        sort_order: 1,
      },
      {
        id: "l-readme",
        user_id: E2E_USER_ID,
        name: "readme.txt",
        path: docsReadmePath,
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
            directories: [
              { name: "daily", path: dailyPath, item_count: 0 },
              { name: "docs", path: docsPath, item_count: 0 },
              { name: "download", path: downloadPath, item_count: 0 },
              { name: "work", path: workPath, item_count: 0 },
            ],
            files: [
              {
                name: "notes.txt",
                path: notesPath,
                type: "text/plain",
                size: 12,
                extension: ".txt",
              },
              {
                name: "note-old.txt",
                path: noteOldPath,
                type: "text/plain",
                size: 12,
                extension: ".txt",
              },
              {
                name: "readme.txt",
                path: readmePath,
                type: "text/plain",
                size: 12,
                extension: ".txt",
              },
            ],
            total_items: 7,
          },
        });
        return;
      }
      if (requestedPath === docsPath) {
        await route.fulfill({
          json: {
            ...common,
            parent_path: rootPath,
            can_go_up: true,
            files: [
              {
                name: "readme.txt",
                path: docsReadmePath,
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
    if (url.pathname === "/api/python-proxy/explorer/bookmarks") {
      if (method === "GET") {
        await route.fulfill({ json: { success: true, bookmarks: state.bookmarks } });
        return;
      }
    }
    if (url.pathname === "/api/python-proxy/explorer/launchers" && method === "GET") {
      await route.fulfill({ json: { success: true, launchers: state.launchers } });
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

test.describe("Files sidebar incremental search", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await page.addInitScript(() => {
      if (sessionStorage.getItem("__sidebar_search_fixture_initialized") === "1") return;
      localStorage.clear();
      localStorage.setItem("selectedProjectId", "project-1");
      localStorage.setItem("filer-tab", "workspace");
      localStorage.setItem("explorer-view-mode", "grid");
      sessionStorage.setItem("__sidebar_search_fixture_initialized", "1");
    });
    await mockSidebarSearchApis(page);
  });

  test("Alt+Q focuses bookmarks and character input moves to a matching item", async ({ page }) => {
    await page.goto("/filer");
    const sidebar = page.getByTestId("files-bookmark-launcher-sidebar");
    await expect(sidebar).toBeVisible();
    const daily = sidebar.getByRole("option", { name: /^daily/ });
    await expect(daily).toBeVisible();

    await page.keyboard.press("Alt+Q");
    await expect(daily).toHaveAttribute("aria-selected", "true");
    await daily.focus();

    await page.keyboard.press("d");
    await page.keyboard.press("o");
    const docs = sidebar.getByRole("option", { name: /^docs/ });
    await expect(docs).toHaveAttribute("aria-selected", "true");
    await expect(docs).toBeFocused();
  });

  test("Ctrl+J advances bookmark matches and wraps", async ({ page }) => {
    await page.goto("/filer");
    const sidebar = page.getByTestId("files-bookmark-launcher-sidebar");
    await page.waitForResponse(
      (response) =>
        response.url().includes("/api/python-proxy/explorer/bookmarks") &&
        response.request().method() === "GET",
    );
    await page.keyboard.press("Alt+Q");
    const daily = sidebar.getByRole("option", { name: /^daily/ });
    await expect(daily).toHaveAttribute("aria-selected", "true");
    await daily.focus();

    await page.keyboard.press("d");
    await page.keyboard.press("o");
    const docs = sidebar.getByRole("option", { name: /^docs/ });
    await expect(docs).toHaveAttribute("aria-selected", "true");

    await page.keyboard.press("Control+j");
    const download = sidebar.getByRole("option", { name: /^download/ });
    await expect(download).toHaveAttribute("aria-selected", "true");

    await page.keyboard.press("Control+j");
    await expect(docs).toHaveAttribute("aria-selected", "true");
  });

  test("Enter navigates to the bookmark selected by incremental search", async ({ page }) => {
    await page.goto("/filer");
    const sidebar = page.getByTestId("files-bookmark-launcher-sidebar");
    await page.waitForResponse(
      (response) =>
        response.url().includes("/api/python-proxy/explorer/bookmarks") &&
        response.request().method() === "GET",
    );
    await page.keyboard.press("Alt+Q");
    const daily = sidebar.getByRole("option", { name: /^daily/ });
    await expect(daily).toHaveAttribute("aria-selected", "true");
    await daily.focus();

    await page.keyboard.press("d");
    await page.keyboard.press("o");
    const docs = sidebar.getByRole("option", { name: /^docs/ });
    await expect(docs).toHaveAttribute("aria-selected", "true");
    await docs.focus();

    const listDocs = page.waitForRequest((request) => {
      return (
        new URL(request.url()).pathname === "/api/python-proxy/explorer/list" &&
        request.method() === "GET" &&
        new URL(request.url()).searchParams.get("path") === docsPath
      );
    });
    await page.keyboard.press("Enter");
    await listDocs;
  });

  test("Alt+E focuses launchers with the same incremental search and Ctrl+J behavior", async ({ page }) => {
    await page.goto("/filer");
    const sidebar = page.getByTestId("files-bookmark-launcher-sidebar");
    await page.waitForResponse(
      (response) =>
        response.url().includes("/api/python-proxy/explorer/launchers") &&
        response.request().method() === "GET",
    );
    await page.keyboard.press("Alt+E");
    const notes = sidebar.getByRole("option", { name: /^notes\.txt/ });
    await expect(notes).toHaveAttribute("aria-selected", "true");
    await notes.focus();

    await page.keyboard.press("n");
    await page.keyboard.press("o");
    await expect(notes).toHaveAttribute("aria-selected", "true");

    await page.keyboard.press("Control+j");
    const noteOld = sidebar.getByRole("option", { name: /^note-old\.txt/ });
    await expect(noteOld).toHaveAttribute("aria-selected", "true");

    await page.keyboard.press("Control+j");
    await expect(notes).toHaveAttribute("aria-selected", "true");
  });

  test("Alt+J still navigates to the launcher parent folder", async ({ page }) => {
    await page.goto("/filer");
    const sidebar = page.getByTestId("files-bookmark-launcher-sidebar");
    await page.waitForResponse(
      (response) =>
        response.url().includes("/api/python-proxy/explorer/launchers") &&
        response.request().method() === "GET",
    );
    await page.keyboard.press("Alt+E");
    const readme = sidebar.getByRole("option", { name: /^readme\.txt/ });
    await readme.focus();
    await expect(readme).toHaveAttribute("aria-selected", "true");

    const listDocs = page.waitForRequest((request) => {
      return (
        new URL(request.url()).pathname === "/api/python-proxy/explorer/list" &&
        request.method() === "GET" &&
        new URL(request.url()).searchParams.get("path") === docsPath
      );
    });
    await page.keyboard.press("Alt+J");
    await listDocs;
  });

  test("Files canvas search does not carry over to launcher Ctrl+J", async ({ page }) => {
    await page.goto("/filer");
    const sidebar = page.getByTestId("files-bookmark-launcher-sidebar");
    await expect(sidebar).toBeVisible();
    await expect(page.locator(`[data-explorer-item-path="${dailyPath}"]`).first()).toHaveClass(
      /border-primary/,
      { timeout: 15000 },
    );

    await page.locator('[data-shell-workspace="files"]').focus();
    await page.keyboard.type("wo");
    await expect(
      page
        .locator('[data-explorer-grid="true"]')
        .locator(`[data-explorer-item-path="${workPath}"]`),
    ).toHaveClass(/border-primary/);

    await page.keyboard.press("Alt+E");
    const notes = sidebar.getByRole("option", { name: /^notes\.txt/ });
    await expect(notes).toHaveAttribute("aria-selected", "true");
    await notes.focus();

    await page.keyboard.press("Control+j");
    await expect(notes).toHaveAttribute("aria-selected", "true");
    await expect(sidebar.getByRole("option", { name: /^note-old\.txt/ })).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });
});
