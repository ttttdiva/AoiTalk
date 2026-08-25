import { expect, test } from "@playwright/test";

import { addAuthCookie, E2E_USER_ID } from "./support/auth";

const project = {
  id: "project-1",
  name: "Sidebar Project",
  description: null,
  slug: "sidebar-project",
  color: "#2563eb",
  space_id: "space-1",
  is_completed: false,
};

const project2 = {
  id: "project-2",
  name: "Second Sidebar Project",
  description: null,
  slug: "second-sidebar-project",
  color: "#7c3aed",
  space_id: "space-1",
  is_completed: false,
};

const space = {
  id: "space-1",
  name: "Sidebar Space",
  slug: "sidebar-space",
  description: null,
  color: "#2563eb",
};

const rootPath = `_projects/project_${project.id}`;
const folderPath = `${rootPath}/docs`;
const textPath = `${rootPath}/notes.txt`;
const imagePath = `${rootPath}/cover.png`;
const outsideFolderTextPath = `${folderPath}/guide.md`;
const recordPath = `aoitalk-record-table:${project.id}:table-1`;
const root2Path = `_projects/project_${project2.id}`;
const userRootPath = `_users/user_${E2E_USER_ID}`;
const userFolderPath = `${userRootPath}/notes`;
const userTextPath = `${userFolderPath}/todo.txt`;

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

async function mockFilesApis(page: import("@playwright/test").Page) {
  // The rename regression exercises the server-confirmed path rather than
  // assuming that the requested name is echoed back.  Keep this item mutable
  // so the refresh performed by the real rename operation exposes the new
  // sort position to both Grid and List renderers.
  let projectImage = {
    name: "cover.png",
    path: imagePath,
    type: "image/png",
    size: 24,
    extension: ".png",
  };
  const state: { bookmarks: Bookmark[]; launchers: Launcher[] } = {
    bookmarks: [
      {
        id: "bookmark-1",
        user_id: E2E_USER_ID,
        name: "Project root",
        path: rootPath,
        sort_order: 0,
      },
      {
        id: "bookmark-2",
        user_id: E2E_USER_ID,
        name: "Second project root",
        path: root2Path,
        sort_order: 1,
      },
    ],
    launchers: [
      {
        id: "launcher-1",
        user_id: E2E_USER_ID,
        name: "Notes",
        path: textPath,
        sort_order: 0,
      },
      {
        id: "launcher-2",
        user_id: E2E_USER_ID,
        name: "Guide",
        path: outsideFolderTextPath,
        sort_order: 1,
      },
      {
        id: "launcher-3",
        user_id: E2E_USER_ID,
        name: "Records table",
        path: recordPath,
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
      await route.fulfill({ json: { projects: [project, project2], total: 2 } });
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
    if (url.pathname === `/api/projects/${project2.id}/records`) {
      await route.fulfill({ json: { tables: [] } });
      return;
    }
    if (url.pathname === `/api/projects/${project.id}/records/table-1`) {
      await route.fulfill({
        json: {
          table: {
            id: "table-1",
            name: "Records table",
            description: null,
            row_count: 0,
          },
          fields: [],
          rows: [],
        },
      });
      return;
    }
    if (url.pathname === "/api/python-proxy/explorer/list") {
      const requestedPath = url.searchParams.get("path") || rootPath;
      if (requestedPath === folderPath) {
        await route.fulfill({
          json: {
            success: true,
            current_path: folderPath,
            parent_path: rootPath,
            can_go_up: true,
            directories: [],
            files: [
              {
                name: "guide.md",
                path: outsideFolderTextPath,
                type: "text/markdown",
                size: 18,
                extension: ".md",
              },
            ],
            total_items: 1,
          },
        });
        return;
      }
      if (requestedPath === userFolderPath) {
        await route.fulfill({
          json: {
            success: true,
            current_path: userFolderPath,
            parent_path: userRootPath,
            can_go_up: true,
            directories: [],
            files: [
              {
                name: "todo.txt",
                path: userTextPath,
                type: "text/plain",
                size: 18,
                extension: ".txt",
              },
            ],
            total_items: 1,
          },
        });
        return;
      }
      if (requestedPath === userRootPath) {
        await route.fulfill({
          json: {
            success: true,
            current_path: userRootPath,
            parent_path: null,
            can_go_up: false,
            directories: [{ name: "notes", path: userFolderPath, item_count: 1 }],
            files: [],
            total_items: 1,
          },
        });
        return;
      }
      if (requestedPath === root2Path) {
        await route.fulfill({
          json: {
            success: true,
            current_path: root2Path,
            parent_path: null,
            can_go_up: false,
            directories: [],
            files: [],
            total_items: 0,
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
          directories: [{ name: "docs", path: folderPath, item_count: 1 }],
          files: [
            {
              name: "notes.txt",
              path: textPath,
              type: "text/plain",
              size: 12,
              extension: ".txt",
            },
            projectImage,
            {
              name: "Records table.dbtable",
              path: recordPath,
              type: "application/x-aoitalk-record-table",
              size: 0,
              extension: ".dbtable",
              virtual_kind: "record_table",
              project_id: project.id,
              record_table_id: "table-1",
            },
          ],
          total_items: 2,
        },
      });
      return;
    }
    if (
      (url.pathname === "/api/python-proxy/explorer/rename" ||
        url.pathname === `/api/python-proxy/projects/${project.id}/files/rename`) &&
      method === "POST"
    ) {
      const body = JSON.parse(route.request().postData() ?? "{}");
      const projectRelative = url.pathname.endsWith("/files/rename");
      expect(body.path).toBe(projectRelative ? "cover.png" : imagePath);
      const newName = String(body.new_name || "").trim();
      projectImage = {
        ...projectImage,
        name: newName,
        path: `${rootPath}/${newName}`,
      };
      await route.fulfill({
        json: {
          success: true,
          new_name: newName,
          // The project endpoint returns a relative path, while the generic
          // explorer endpoint returns the canonical workspace path.
          new_path: projectRelative ? newName : `${rootPath}/${newName}`,
        },
      });
      return;
    }
    if (url.pathname === "/api/python-proxy/explorer/content") {
      await route.fulfill({
        json: {
          success: true,
          content: "sidebar runtime evidence",
          path: textPath,
          name: "notes.txt",
          extension: ".txt",
          size_bytes: 24,
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
          path: body.path,
          sort_order: state.bookmarks.length,
          kind: body.kind ?? "bookmark",
          parent_id: body.parent_id ?? null,
        };
        state.bookmarks.push(bookmark);
        await route.fulfill({ json: { success: true, bookmark } });
        return;
      }
      if (method === "DELETE") {
        const body = JSON.parse(route.request().postData() ?? "{}");
        state.bookmarks = state.bookmarks.filter((item) => item.path !== body.path);
        await route.fulfill({ json: { success: true } });
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
      if (method === "DELETE") {
        state.bookmarks = state.bookmarks.filter((item) => item.id !== id);
        await route.fulfill({ json: { success: true } });
        return;
      }
    }
    if (url.pathname === "/api/python-proxy/explorer/launchers") {
      if (method === "GET") {
        await route.fulfill({ json: { success: true, launchers: state.launchers } });
        return;
      }
      if (method === "POST") {
        const body = JSON.parse(route.request().postData() ?? "{}");
        const launcher: Launcher = {
          id: `launcher-${state.launchers.length + 1}`,
          user_id: E2E_USER_ID,
          name: body.name || body.path,
          path: body.path,
          sort_order: state.launchers.length,
        };
        state.launchers.push(launcher);
        await route.fulfill({ json: { success: true, launcher } });
        return;
      }
    }
    if (url.pathname.startsWith("/api/python-proxy/explorer/launchers/")) {
      const id = url.pathname.split("/").pop();
      if (method === "PATCH") {
        const body = JSON.parse(route.request().postData() ?? "{}");
        const launcher = state.launchers.find((item) => item.id === id);
        if (launcher) Object.assign(launcher, body);
        await route.fulfill({ json: { success: true, launcher } });
        return;
      }
      if (method === "DELETE") {
        state.launchers = state.launchers.filter((item) => item.id !== id);
        await route.fulfill({ json: { success: true } });
        return;
      }
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

    // HF root/accounts and unrelated shell requests are intentionally empty in
    // this stateful smoke fixture; the Files tab still renders its bookmark UI.
    await route.fulfill({ json: {} });
  });
}

test.describe("Files bookmark/launcher sidebar", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await page.addInitScript(() => {
      // Keep the selected Files tab/path across page.reload() so the
      // persistence checks below exercise the same principal/scope after a
      // reload instead of resetting to Project Files on every document.
      if (sessionStorage.getItem("__sidebar_fixture_initialized") === "1") return;
      localStorage.clear();
      localStorage.setItem("selectedProjectId", "project-1");
      localStorage.setItem("filer-tab", "workspace");
      localStorage.setItem("explorer-view-mode", "grid");
      sessionStorage.setItem("__sidebar_fixture_initialized", "1");
    });
    await mockFilesApis(page);
  });

  test("renders the new rail, keeps it while an editor is open, and returns focus to Files", async ({ page }) => {
    await page.goto("/filer");

    const sidebar = page.getByTestId("files-bookmark-launcher-sidebar");
    const file = page.locator(`[data-explorer-item-path="${textPath}"]`);
    await expect(sidebar).toBeVisible();
    await expect(page.getByTestId("files-quick-panel-navigation")).toHaveCount(0);
    await expect(file).toBeVisible();

    const bookmarksTab = sidebar.getByRole("tab", { name: /ブックマーク/ });
    const launchersTab = sidebar.getByRole("tab", { name: /ランチャー/ });
    await expect(bookmarksTab).toHaveAttribute("aria-selected", "true");
    await expect(launchersTab).toBeVisible();

    await page.keyboard.press("Alt+E");
    await expect(launchersTab).toHaveAttribute("aria-selected", "true");
    await expect(sidebar.getByRole("option", { name: "Notes" })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.locator('[data-shell-region="files-canvas"]')).toBeFocused();
    await page.keyboard.press("Alt+Q");
    await expect(bookmarksTab).toHaveAttribute("aria-selected", "true");
    await expect(sidebar.getByRole("option", { name: "Project root" })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.locator('[data-shell-region="files-canvas"]')).toBeFocused();

    await file.dblclick();
    await expect(page.locator('[data-shell-region="files-editor"]')).toBeVisible();
    await expect(sidebar).toBeVisible();

    await page.evaluate(() => {
      (window as Window & { __ctrlDPrevented?: boolean }).__ctrlDPrevented = false;
      window.addEventListener(
        "keydown",
        (event) => {
          if (event.key.toLowerCase() !== "d") return;
          (window as Window & { __ctrlDPrevented?: boolean }).__ctrlDPrevented =
            event.defaultPrevented;
        },
        // CodeMirror stops bubbling editor key events; observe the Files
        // capture-phase shortcut handler before that propagation boundary.
        { capture: true },
      );
    });
    await page.keyboard.press("Control+d");
    await expect
      .poll(() =>
        page.evaluate(
          () => (window as Window & { __ctrlDPrevented?: boolean }).__ctrlDPrevented,
        ),
      )
      .toBe(true);

    await page.keyboard.press("Alt+Q");
    await expect(sidebar.getByRole("option", { name: "Project root" })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.locator('[data-shell-region="files-canvas"]')).toBeFocused();
  });

  test("hides launchers for HF and Hydrus while keeping bookmarks available", async ({ page }) => {
    await page.goto("/filer");
    const sidebar = page.getByTestId("files-bookmark-launcher-sidebar");
    await expect(sidebar).toBeVisible();

    await page.getByRole("button", { name: "HF", exact: true }).click();
    await expect(sidebar.getByRole("tab", { name: /ブックマーク/ })).toBeVisible();
    await expect(sidebar.getByRole("tab", { name: /ランチャー/ })).toHaveCount(0);

    await page.getByRole("button", { name: "Hydrus", exact: true }).click();
    await expect(sidebar.getByRole("tab", { name: /ブックマーク/ })).toBeVisible();
    await expect(sidebar.getByRole("tab", { name: /ランチャー/ })).toHaveCount(0);

    await page.getByRole("button", { name: "Project Files", exact: true }).click();
    await expect(sidebar.getByRole("tab", { name: /ランチャー/ })).toBeVisible();
  });

  test("accepts folder drops into bookmarks and file drops into launchers", async ({ page }) => {
    await page.goto("/filer");
    const sidebar = page.getByTestId("files-bookmark-launcher-sidebar");
    await expect(sidebar).toBeVisible();
    const sourceMutationRequests: string[] = [];
    page.on("request", (request) => {
      const pathname = new URL(request.url()).pathname;
      if (/(?:\/explorer|\/files)\/(?:move|copy|delete)$/.test(pathname)) {
        sourceMutationRequests.push(pathname);
      }
    });

    await sidebar.evaluate((element, path) => {
      const dataTransfer = new DataTransfer();
      dataTransfer.setData("application/x-explorer-paths", JSON.stringify([path]));
      element.dispatchEvent(new DragEvent("drop", { bubbles: true, dataTransfer }));
    }, folderPath);
    await expect(sidebar.getByRole("option", { name: "docs" })).toBeVisible();
    await expect(page.locator(`[data-explorer-item-path="${folderPath}"]`)).toBeVisible();

    await page.getByRole("tab", { name: /ランチャー/ }).click();
    await sidebar.evaluate((element, path) => {
      const dataTransfer = new DataTransfer();
      dataTransfer.setData("application/x-explorer-paths", JSON.stringify([path]));
      element.dispatchEvent(new DragEvent("drop", { bubbles: true, dataTransfer }));
    }, imagePath);
    await expect(sidebar.getByRole("option", { name: "cover.png" })).toBeVisible();
    await expect(page.locator(`[data-explorer-item-path="${imagePath}"]`)).toBeVisible();
    expect(sourceMutationRequests).toEqual([]);
  });

  test("persists a non-duplicate Project Files folder registered with Ctrl+D across reload", async ({ page }) => {
    await page.goto("/filer");
    const sidebar = page.getByTestId("files-bookmark-launcher-sidebar");
    const folder = page.locator(`[data-explorer-item-path="${folderPath}"]`);
    await expect(folder).toBeVisible();
    await folder.dblclick();
    await expect(page.locator(`[data-explorer-item-path="${outsideFolderTextPath}"]`)).toBeVisible();

    const bookmarkPost = page.waitForRequest((request) => {
      const pathname = new URL(request.url()).pathname;
      return pathname === "/api/python-proxy/explorer/bookmarks" && request.method() === "POST";
    });
    await page.keyboard.press("Control+d");
    const request = await bookmarkPost;
    expect(JSON.parse(request.postData() ?? "{}").path).toBe(folderPath);
    await expect(sidebar.getByRole("option", { name: "docs" })).toBeVisible();

    await page.reload();
    await expect(sidebar).toBeVisible();
    await expect(sidebar.getByRole("option", { name: "docs" })).toBeVisible();
  });

  test("persists a non-duplicate User Files folder registered with Ctrl+D across reload", async ({ page }) => {
    await page.goto("/filer");
    await expect(page.locator(`[data-explorer-item-path="${textPath}"]`)).toBeVisible();
    await page.getByRole("button", { name: "User Files", exact: true }).click();
    const sidebar = page.getByTestId("files-bookmark-launcher-sidebar");
    const folder = page.locator(`[data-explorer-item-path="${userFolderPath}"]`);
    await expect(folder).toBeVisible();
    await folder.dblclick();
    await expect(page.locator(`[data-explorer-item-path="${userTextPath}"]`)).toBeVisible();

    const bookmarkPost = page.waitForRequest((request) => {
      const pathname = new URL(request.url()).pathname;
      return pathname === "/api/python-proxy/explorer/bookmarks" && request.method() === "POST";
    });
    await page.keyboard.press("Control+d");
    const request = await bookmarkPost;
    expect(JSON.parse(request.postData() ?? "{}").path).toBe(userFolderPath);
    await expect(sidebar.getByRole("option", { name: "notes" })).toBeVisible();

    await page.reload();
    await expect(sidebar).toBeVisible();
    await expect(sidebar.getByRole("option", { name: "notes" })).toBeVisible();
  });

  test("filters stale project state and opens a launcher outside the current folder", async ({ page }) => {
    await page.goto("/filer");
    const sidebar = page.getByTestId("files-bookmark-launcher-sidebar");
    await expect(sidebar.getByRole("option", { name: "Project root" })).toBeVisible();
    await expect(sidebar.getByRole("option", { name: "Second project root" })).toHaveCount(0);

    const projectSelect = page.getByRole("combobox", { name: "プロジェクト選択" });
    await projectSelect.click();
    await page.getByRole("option", { name: "Second Sidebar Project", exact: true }).click();
    await expect(sidebar.getByRole("option", { name: "Second project root" })).toBeVisible();
    await expect(sidebar.getByRole("option", { name: "Project root", exact: true })).toHaveCount(0);

    await projectSelect.click();
    await page.getByRole("option", { name: "Sidebar Project", exact: true }).click();
    await expect(sidebar.getByRole("option", { name: "Project root" })).toBeVisible();
    await sidebar.getByRole("tab", { name: /ランチャー/ }).click();
    await expect(sidebar.getByRole("option", { name: "Guide" })).toBeVisible();
    await sidebar.getByRole("option", { name: "Guide" }).click();
    await expect(page.locator('[data-shell-region="files-editor"]')).toBeVisible();
  });

  test("keeps a renamed item selected after server path and sort changes in Grid and List", async ({ page }) => {
    await page.goto("/filer");

    const renamedPath = `${rootPath}/zzz.png`;
    const source = page.locator(`[data-explorer-item-path="${imagePath}"]`);
    await expect(source).toBeVisible();

    // Select a non-leading item, invoke the existing F2 flow, and let the
    // mocked API return the canonical new_path.  The list refresh then sorts
    // zzz.png after the other files; selection must follow that path rather
    // than falling back to the first rendered entry.
    await source.click();
    await page.keyboard.press("F2");
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    const input = dialog.getByRole("textbox");
    await input.fill("zzz.png");
    const renameRequest = page.waitForRequest((request) => {
      return (
        new URL(request.url()).pathname === "/api/python-proxy/explorer/rename" &&
        request.method() === "POST"
      );
    });
    await dialog.getByRole("button", { name: "変更", exact: true }).click();
    const request = await renameRequest;
    expect(JSON.parse(request.postData() ?? "{}")).toMatchObject({
      path: imagePath,
      new_name: "zzz.png",
    });

    const renamed = page.locator(`[data-explorer-item-path="${renamedPath}"]`);
    await expect(renamed).toBeVisible();
    await expect(dialog).toHaveCount(0);
    await expect(renamed).toHaveClass(/outline-primary/);
    const gridOrder = await page.locator("[data-explorer-item-path]").evaluateAll((items) =>
      items.map((item) => item.getAttribute("data-explorer-item-path")),
    );
    expect(gridOrder.indexOf(renamedPath)).toBeGreaterThan(0);
    expect(gridOrder.at(-1)).toBe(renamedPath);

    // The same focused/selected path must survive the view-mode switch.  An
    // Arrow key afterwards proves the normal keyboard navigation remains
    // active instead of leaving the canvas with a stale pre-rename path.
    await page.locator('[data-shell-region="files-canvas"]').focus();
    await page.keyboard.press(";");
    await expect(renamed).toHaveClass(/outline-primary/);
    await page.keyboard.press("ArrowUp");
    await expect(renamed).not.toHaveClass(/outline-primary/);
  });

  test("opens a persisted record-table launcher through the existing record editor", async ({ page }) => {
    await page.goto("/filer");
    const sidebar = page.getByTestId("files-bookmark-launcher-sidebar");
    await expect(sidebar).toBeVisible();
    await sidebar.getByRole("tab", { name: /ランチャー/ }).click();
    await expect(sidebar.getByRole("option", { name: "Records table" })).toBeVisible();
    await sidebar.getByRole("option", { name: "Records table" }).click();
    await expect(sidebar).toBeVisible();
    await expect(page.locator('input[value="Records table"]')).toBeVisible();
  });
});
