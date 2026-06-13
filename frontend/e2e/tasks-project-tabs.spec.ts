import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie } from "./support/auth";

const projects = [
  { id: "project-1", name: "Project One", space_id: null, is_completed: false },
  { id: "project-2", name: "Project Two", space_id: null, is_completed: false },
  {
    id: "project-3",
    name: "Project Three",
    space_id: null,
    is_completed: false,
  },
  {
    id: "project-4",
    name: "Project Four",
    space_id: null,
    is_completed: false,
  },
];

function makeTask(
  id: string,
  projectId: string,
  title: string,
  sortOrder: number,
) {
  const now = "2026-04-28T00:00:00.000Z";
  const project = projects.find((item) => item.id === projectId) ?? projects[0];
  return {
    id,
    project_id: project.id,
    project_name: project.name,
    title,
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
    created_at: now,
    updated_at: now,
    metadata: {},
    assignees: [],
    tags: [],
    active_time_entry: null,
    estimated_hours: null,
    sort_order: sortOrder,
    total_time_seconds: 0,
    parent_task_id: null,
    subtasks: [],
    activities: [],
    has_recurrence: false,
  };
}

async function routeTasksPage(page: Page) {
  const tasks = [
    makeTask("task-1", "project-1", "One", 0),
    makeTask("task-2", "project-2", "Two", 0),
    makeTask("task-3", "project-3", "Three", 0),
    makeTask("task-4", "project-4", "Four", 0),
    ...Array.from({ length: 40 }, (_, index) =>
      makeTask(`task-many-${index}`, "project-2", `Many ${index}`, index + 1),
    ),
  ];

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
      await route.fulfill({ json: { projects, total: projects.length } });
      return;
    }
    if (url.pathname === "/api/spaces") {
      await route.fulfill({ json: { spaces: [], total: 0 } });
      return;
    }
    if (url.pathname === "/api/tasks") {
      await route.fulfill({ json: tasks });
      return;
    }
    if (
      url.pathname.startsWith("/api/projects/") &&
      url.pathname.endsWith("/tags")
    ) {
      await route.fulfill({ json: [] });
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
    if (url.pathname === "/api/python-proxy/health") {
      await route.fulfill({ status: 503, json: { ok: false } });
      return;
    }
    if (url.pathname.endsWith("/explorer/list")) {
      await route.fulfill({
        json: {
          success: true,
          current_path: "",
          parent_path: null,
          can_go_up: false,
          directories: [],
          files: [],
          total_items: 0,
        },
      });
      return;
    }

    await route.fulfill({ json: {} });
  });
}

test.describe("task project tabs", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await page.addInitScript(() => {
      if (sessionStorage.getItem("task-project-tabs-test-init")) return;
      sessionStorage.setItem("task-project-tabs-test-init", "1");
      localStorage.clear();
      localStorage.setItem(
        "tasks-sidebar-view-state",
        JSON.stringify({
          filter: "all",
          projectTab: "all",
          projectTabStateVersion: 2,
          showClosed: false,
          showFuture: true,
          customFilter: { logic: "and", rules: [] },
        }),
      );
    });
    await routeTasksPage(page);
  });

  test("keeps the project tab row visible while cycling projects", async ({
    page,
  }) => {
    await page.goto("/tasks");
    await expect(page.getByTestId("task-row-task-1")).toBeVisible();
    await expect(page.getByTestId("task-project-tabs")).toBeVisible();

    for (let i = 0; i < 5; i += 1) {
      await page.keyboard.press("Control+Shift+ArrowRight");
      await expect(page.getByTestId("task-project-tabs")).toBeVisible();
      await expect(page.getByTestId("task-project-tabs-toggle")).toBeVisible();
    }
  });

  test("can collapse, persist, and expand the project tab row", async ({
    page,
  }) => {
    await page.goto("/tasks");
    await expect(page.getByTestId("task-row-task-1")).toBeVisible();
    await expect(page.getByTestId("task-project-tabs")).toBeVisible();

    await page.getByTestId("task-project-tabs-toggle").click();
    await expect(page.getByTestId("task-project-tabs")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "全て" })).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "プロジェクトタブを表示" }),
    ).toBeVisible();

    await page.reload();
    await expect(page.getByTestId("task-row-task-1")).toBeVisible();
    await expect(page.getByTestId("task-project-tabs")).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "プロジェクトタブを表示" }),
    ).toBeVisible();

    await page.keyboard.press("Control+Shift+ArrowRight");
    await expect(page.getByTestId("task-project-tabs")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "全て" })).toHaveCount(0);

    await page.getByTestId("task-project-tabs-toggle").click();
    await expect(page.getByTestId("task-project-tabs")).toBeVisible();
    await expect(
      page.getByRole("button", { name: "プロジェクトタブを隠す" }),
    ).toBeVisible();
  });
});
