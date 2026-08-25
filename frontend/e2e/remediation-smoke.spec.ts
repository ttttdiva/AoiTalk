import { expect, test } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

test.describe("remediation acceptance smoke", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
  });

  test("Settings deep link keeps the requested category addressable", async ({
    page,
  }) => {
    await page.goto("/settings#account");
    await expect(page).toHaveURL(/\/settings#account$/);
    const category = page.locator('[data-settings-target="account"]');
    await expect(category).toBeVisible();
    await expect(category).toHaveAttribute("aria-current", "location");
  });

  test("Chat Composer opens from the new conversation action", async ({
    page,
  }) => {
    await page.goto("/chat");
    await page.getByRole("button", { name: "新規会話", exact: true }).click();
    const composer = page.locator("textarea, input[type='text']").last();
    await expect(composer).toBeVisible();
    await composer.fill("remediation smoke send");
    await composer.press("Enter");
    await expect(page.getByText("remediation smoke send", { exact: true })).toBeVisible({
      timeout: 15_000,
    });
  });

  test("Alt+P opens the global memo pad", async ({
    page,
  }) => {
    await page.goto("/chat");
    await page.keyboard.press("Alt+p");
    await expect(page.getByPlaceholder("メモを入力...")).toBeVisible();
  });

  test("Project switching and Files workspace operation are reachable", async ({
    page,
  }) => {
    const projects = [
      {
        id: "project-one",
        name: "Project One",
        space_id: "space-one",
        is_completed: false,
        description: null,
        aliases: [],
        color: "#2563eb",
      },
      {
        id: "project-two",
        name: "Project Two",
        space_id: "space-one",
        is_completed: false,
        description: null,
        aliases: [],
        color: "#16a34a",
      },
    ];
    await page.route("**/api/projects", async (route) => {
      if (new URL(route.request().url()).pathname === "/api/projects") {
        await route.fulfill({ json: { projects, total: projects.length } });
        return;
      }
      await route.fallback();
    });
    await page.route("**/api/spaces", async (route) => {
      await route.fulfill({
        json: {
          spaces: [
            {
              id: "space-one",
              name: "Workspace One",
              description: null,
              color: "#64748b",
            },
          ],
          total: 1,
        },
      });
    });
    await page.goto("/chat");
    const projectSelector = page.getByRole("combobox", { name: "プロジェクト選択" });
    await expect(projectSelector).toBeVisible({ timeout: 15_000 });
    await projectSelector.click();
    const projectTwoOption = page.getByRole("option", { name: "Project Two" });
    await expect(projectTwoOption).toBeVisible();
    await projectTwoOption.click();
    await expect(projectSelector).toContainText("Project Two");
    const navigation = page.getByRole("navigation", { name: "Workspace" });
    await navigation.getByRole("link", { name: "Files", exact: true }).click();
    await expect(page).toHaveURL(/\/filer/);
    await expect(page.locator('[data-workspace="files"]')).toBeVisible();
    const filesSource = page
      .getByRole("main")
      .getByRole("button", { name: "Project Files" });
    await expect(filesSource).toBeVisible();
    await filesSource.click();
  });

  test("Tasks column width changes survive a reload", async ({ page }) => {
    let settings: Record<string, unknown> = {
      tasks_view_preferences: {
        version: 3,
        viewMode: "list",
        columnWidths: { taskName: 220 },
      },
    };
    await page.unroute("**/api/**");
    await page.route("**/api/**", async (route) => {
      const url = new URL(route.request().url());
      const method = route.request().method();
      if (url.pathname === "/api/auth/status") {
        await route.fulfill({
          json: {
            authenticated: true,
            user: { id: "user-1", username: "tester", role: "admin" },
          },
        });
        return;
      }
      if (url.pathname === "/api/users/me/settings") {
        if (method === "PATCH") {
          const patch = route.request().postDataJSON() as Record<string, unknown>;
          settings = { ...settings, ...patch };
        }
        await route.fulfill({ json: { settings } });
        return;
      }
      if (url.pathname === "/api/tasks") {
        await route.fulfill({
          json: [{
            id: "task-smoke",
            project_id: "project-smoke",
            project_name: "Smoke Project",
            title: "Task smoke",
            description: null,
            status: "open",
            priority: "medium",
            start_at: null,
            end_at: null,
            all_day: false,
            reminder_offsets: [],
            notifications_enabled: true,
            source: "manual",
            created_by: "user-1",
            completed_at: null,
            created_at: "2026-08-12T00:00:00.000Z",
            updated_at: "2026-08-12T00:00:00.000Z",
            metadata: {},
            assignees: [],
            tags: [],
            active_time_entry: null,
            estimated_hours: null,
            sort_order: 0,
            total_time_seconds: 0,
            parent_task_id: null,
            subtasks: [],
            activities: [],
            has_recurrence: false,
          }],
        });
        return;
      }
      if (url.pathname === "/api/projects") {
        await route.fulfill({
          json: {
            projects: [
              {
                id: "project-smoke",
                name: "Smoke Project",
                space_id: "space-smoke",
                is_completed: false,
              },
            ],
            total: 1,
          },
        });
        return;
      }
      if (url.pathname === "/api/spaces") {
        await route.fulfill({
          json: {
            spaces: [
              {
                id: "space-smoke",
                name: "Smoke Workspace",
                slug: "smoke-workspace",
                description: null,
                color: "#64748b",
              },
            ],
            total: 1,
          },
        });
        return;
      }
      if (url.pathname === "/api/notifications") {
        await route.fulfill({ json: [] });
        return;
      }
      if (url.pathname === "/api/time-entries/active") {
        await route.fulfill({ json: null });
        return;
      }
      if (url.pathname.endsWith("/tags")) {
        await route.fulfill({ json: [] });
        return;
      }
      if (url.pathname === "/api/conversations") {
        await route.fulfill({ json: { conversations: [], total: 0 } });
        return;
      }
      if (url.pathname === "/api/python-proxy/health") {
        await route.fulfill({ status: 503, json: { ok: false } });
        return;
      }
      await route.fulfill({ json: {} });
    });
    await page.addInitScript(() => localStorage.clear());
    await page.goto("/tasks");
    await expect(page.getByTestId("task-list-toolbar")).toBeVisible({
      timeout: 15_000,
    });
    const listView = page.getByRole("main").getByTestId("task-view-list");
    if ((await listView.getAttribute("aria-pressed")) !== "true") {
      await listView.click();
    }
    const resizer = page.locator('[data-column-resizer="taskName"]');
    await expect(resizer).toBeVisible();
    const before = await page
      .locator("col")
      .nth(3)
      .getAttribute("data-column-width");
    const box = await resizer.boundingBox();
    expect(box).not.toBeNull();
    const patchRequest = page.waitForRequest(
      (request) =>
        request.url().includes("/api/users/me/settings") &&
        request.method() === "PATCH",
    );
    await page.mouse.move(box!.x + 2, box!.y + box!.height / 2);
    await page.mouse.down();
    await page.mouse.move(box!.x + 52, box!.y + box!.height / 2);
    await page.mouse.up();
    await patchRequest;
    await page.reload();
    const after = await page
      .locator("col")
      .nth(3)
      .getAttribute("data-column-width");
    expect(after).not.toBe(before);
  });
});
