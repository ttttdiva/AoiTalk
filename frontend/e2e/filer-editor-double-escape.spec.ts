import { expect, test } from "@playwright/test";

import { addAuthCookie } from "./support/auth";

const project = {
  id: "project-1",
  name: "Filer Escape Project",
  description: null,
  slug: "filer-escape-project",
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
const textPath = `${rootPath}/notes.txt`;

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
      await route.fulfill({
        json: {
          success: true,
          current_path: rootPath,
          parent_path: null,
          can_go_up: false,
          directories: [],
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
    if (url.pathname === "/api/python-proxy/explorer/content") {
      expect(url.searchParams.get("path")).toBe(textPath);
      await route.fulfill({
        json: {
          success: true,
          content: "hello from filer",
          path: textPath,
          name: "notes.txt",
          extension: ".txt",
          size_bytes: 15,
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

test.describe("Filer editor double Escape", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await page.addInitScript((projectId) => {
      localStorage.clear();
      localStorage.setItem("selectedProjectId", projectId);
      localStorage.setItem("filer-tab", "workspace");
      localStorage.setItem("explorer-view-mode", "grid");
    }, project.id);
    await mockFilerApis(page);
  });

  test("closes the real CodeMirror editor after Quick Filter then two independent Escapes", async ({
    page,
  }) => {
    await page.goto("/filer");

    const root = page.locator('[data-shell-workspace="files"]');
    const file = page.locator(`[data-explorer-item-path="${textPath}"]`);
    await expect(root).toBeVisible();
    await expect(file).toBeVisible();

    // Open the real Filer quick filter and leave it armed with a query.  The
    // text-file click below must clear this state before mounting the editor.
    await root.focus();
    await page.keyboard.press("Control+s");
    const quickFilter = page.getByRole("textbox", {
      name: "ファイル名の即席フィルター",
    });
    await expect(quickFilter).toBeVisible();
    await quickFilter.fill("notes");
    await file.dblclick();

    const editor = page.locator('.cm-content[contenteditable="true"]');
    await expect(editor).toBeVisible();
    await expect(page.locator('[data-shell-region="files-editor"]')).toBeVisible();
    const activeEditorState = await page.evaluate(() => {
      const active = document.activeElement;
      return {
        tagName: active?.tagName ?? null,
        className: active instanceof HTMLElement ? active.className : null,
        isCodeMirror: active instanceof HTMLElement && Boolean(active.closest(".cm-editor")),
      };
    });
    expect(activeEditorState.isCodeMirror).toBe(true);
    await expect(editor).toBeFocused();

    await page.keyboard.press("Escape");
    await expect(page.locator('[data-shell-region="files-editor"]')).toHaveCount(1);
    await expect(editor).toBeVisible();
    await page.keyboard.press("Escape");

    await expect(page.locator('[data-shell-region="files-editor"]')).toHaveCount(0);
    await expect(file).toBeVisible();
    await expect(
      page.getByRole("textbox", { name: "ファイル名の即席フィルター" }),
    ).toHaveCount(0);
  });

  test("keeps the double-Escape sequence when focus moves to the Files sidebar", async ({
    page,
  }) => {
    await page.goto("/filer");

    const file = page.locator(`[data-explorer-item-path="${textPath}"]`);
    await expect(file).toBeVisible();
    await file.dblclick();
    const editor = page.locator('.cm-content[contenteditable="true"]');
    await expect(editor).toBeVisible();

    // Arm the state in CodeMirror, then make a valid Files-workspace focus
    // move into the persistent sidebar.  The second Escape there must close
    // the editor instead of requiring a third press.
    await page.keyboard.press("Escape");
    const sidebarList = page.locator('[data-files-sidebar-list]');
    await sidebarList.focus();
    await expect(sidebarList).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(page.locator('[data-shell-region="files-editor"]')).toHaveCount(0);
  });

  test("resets the armed Escape after leaving Files and returning from the sidebar", async ({
    page,
  }) => {
    await page.goto("/filer");

    const file = page.locator(`[data-explorer-item-path="${textPath}"]`);
    await expect(file).toBeVisible();
    await file.dblclick();
    const editor = page.locator('.cm-content[contenteditable="true"]');
    await expect(editor).toBeVisible();

    // Arm once in CodeMirror, move inside Files, then leave the Files
    // workspace entirely.  Returning to the sidebar must not retain the
    // stale first Escape from before the external focus transition.
    await page.keyboard.press("Escape");
    const sidebarList = page.locator("[data-files-sidebar-list]");
    await sidebarList.focus();
    await expect(sidebarList).toBeFocused();
    const outsideFiles = page.locator('a[href="/chat"]').first();
    await outsideFiles.focus();
    await expect(outsideFiles).toBeFocused();
    await sidebarList.focus();
    await expect(sidebarList).toBeFocused();

    await page.keyboard.press("Escape");
    await expect(page.locator('[data-shell-region="files-editor"]')).toHaveCount(1);
    await page.keyboard.press("Escape");
    await expect(page.locator('[data-shell-region="files-editor"]')).toHaveCount(0);
  });
});
