import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie } from "./support/auth";

const spaces = [
  { id: "space-a", name: "Space A", slug: "space-a", color: "#2563eb" },
  { id: "space-b", name: "Space B", slug: "space-b", color: "#16a34a" },
  { id: "space-c", name: "Space C", slug: "space-c", color: "#dc2626" },
];

const projects = [
  {
    id: "project-a1",
    name: "Project A1",
    slug: "project-a1",
    space_id: "space-a",
    is_completed: false,
  },
  {
    id: "project-a2",
    name: "Project A2",
    slug: "project-a2",
    space_id: "space-a",
    is_completed: false,
  },
  {
    id: "project-b1",
    name: "Project B1",
    slug: "project-b1",
    space_id: "space-b",
    is_completed: false,
  },
  {
    id: "project-b2",
    name: "Project B2",
    slug: "project-b2",
    space_id: "space-b",
    is_completed: false,
  },
  {
    id: "project-c1",
    name: "Project C1",
    slug: "project-c1",
    space_id: "space-c",
    is_completed: false,
  },
  {
    id: "project-c2",
    name: "Project C2",
    slug: "project-c2",
    space_id: "space-c",
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

const allTasks = [
  makeTask("task-a1", "project-a1", "Task A1", 0),
  makeTask("task-a2", "project-a2", "Task A2", 0),
  makeTask("task-b1", "project-b1", "Task B1", 0),
  makeTask("task-b2", "project-b2", "Task B2", 0),
  makeTask("task-c1", "project-c1", "Task C1", 0),
  makeTask("task-c2", "project-c2", "Task C2", 0),
];

async function routeTasksPage(page: Page) {
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
      await route.fulfill({ json: { spaces, total: spaces.length } });
      return;
    }
    if (url.pathname === "/api/tasks") {
      const spaceId = url.searchParams.get("space_id");
      const tasks = spaceId
        ? allTasks.filter((task) => {
            const project = projects.find((item) => item.id === task.project_id);
            return project?.space_id === spaceId;
          })
        : allTasks;
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

async function openTasksAtSpaceA(page: Page) {
  await page.goto("/tasks");
  await expect(page.getByTestId("task-space-space-a")).toBeVisible();
  await expect(page.getByTestId("task-row-task-a1")).toBeVisible();
  await expect(page.getByTestId("task-row-task-a2")).toBeVisible();
}

async function switchSpaceByShortcut(page: Page, digit: 1 | 2 | 3) {
  await page.keyboard.press(`Alt+Shift+Digit${digit}`);
}

async function expectSpaceWide(
  page: Page,
  spaceId: "space-a" | "space-b" | "space-c",
) {
  const projectIds =
    spaceId === "space-a"
      ? (["project-a1", "project-a2"] as const)
      : spaceId === "space-b"
        ? (["project-b1", "project-b2"] as const)
        : (["project-c1", "project-c2"] as const);
  const visibleTaskIds =
    spaceId === "space-a"
      ? (["task-a1", "task-a2"] as const)
      : spaceId === "space-b"
        ? (["task-b1", "task-b2"] as const)
        : (["task-c1", "task-c2"] as const);
  const hiddenTaskIds = allTasks
    .map((task) => task.id)
    .filter((id) => !visibleTaskIds.includes(id as (typeof visibleTaskIds)[number]));

  await expect(page.getByTestId(`task-space-${spaceId}`)).toHaveAttribute(
    "aria-selected",
    "true",
  );
  for (const projectId of projectIds) {
    await expect(page.getByTestId(`task-project-${projectId}`)).toHaveAttribute(
      "aria-selected",
      "false",
    );
  }
  for (const taskId of visibleTaskIds) {
    await expect(page.getByTestId(`task-row-${taskId}`)).toBeVisible();
  }
  for (const taskId of hiddenTaskIds) {
    await expect(page.getByTestId(`task-row-${taskId}`)).toHaveCount(0);
  }
}

test.describe("タスクのスペース切替ショートカット", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await page.addInitScript(() => {
      localStorage.clear();
      localStorage.setItem("selectedSpaceId", "space-a");
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

  test("スペースA全体からAlt+Shift+2でスペースB全体へ切り替える", async ({
    page,
  }) => {
    await openTasksAtSpaceA(page);
    await expect(page.getByTestId("task-space-space-a")).toHaveAttribute(
      "aria-selected",
      "true",
    );

    await switchSpaceByShortcut(page, 2);
    await expectSpaceWide(page, "space-b");
  });

  test("プロジェクト限定表示からAlt+Shift+2でもスペースB全体になる", async ({
    page,
  }) => {
    await openTasksAtSpaceA(page);
    await page.getByTestId("task-project-project-a1").click();
    await expect(page.getByTestId("task-project-project-a1")).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(page.getByTestId("task-row-task-a1")).toBeVisible();
    await expect(page.getByTestId("task-row-task-a2")).toHaveCount(0);

    await switchSpaceByShortcut(page, 2);
    await expectSpaceWide(page, "space-b");
  });

  test("別スペースのプロジェクト選択後にAlt+Shift+3でスペースC全体になる", async ({
    page,
  }) => {
    await openTasksAtSpaceA(page);
    await page.getByTestId("task-project-project-a1").click();
    await expect(page.getByTestId("task-project-project-a1")).toHaveAttribute(
      "aria-selected",
      "true",
    );

    await page.getByTestId("task-project-project-b1").click();
    await expect(page.getByTestId("task-project-project-b1")).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(page.getByTestId("task-row-task-b1")).toBeVisible();
    await expect(page.getByTestId("task-row-task-b2")).toHaveCount(0);

    await switchSpaceByShortcut(page, 3);
    await expectSpaceWide(page, "space-c");
  });

  test("スペース別の最終プロジェクトをヘッダーへ復元してもTasksはSpace全体のまま", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await openTasksAtSpaceA(page);

    const spaceSelect = page.getByRole("combobox", { name: "スペース選択" });
    const projectSelect = page.getByRole("combobox", { name: "プロジェクト選択" });
    await expect(projectSelect).toBeVisible();

    await page.getByTestId("task-project-project-a2").click();
    await expect(page.getByTestId("task-project-project-a2")).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(projectSelect).toContainText("Project A2");

    await page.getByTestId("task-project-project-b2").click();
    await expect(page.getByTestId("task-project-project-b2")).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(spaceSelect).toContainText("Space B");
    await expect(projectSelect).toContainText("Project B2");
    await expect
      .poll(() => page.evaluate(() => localStorage.getItem("lastProjectIdBySpace")))
      .toContain("project-b2");

    await page.getByTestId("task-project-project-a2").click();
    await expect(page.getByTestId("task-project-project-a2")).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(page.getByTestId("task-row-task-a2")).toBeVisible();
    await expect(page.getByTestId("task-row-task-a1")).toHaveCount(0);
    await expect
      .poll(() => page.evaluate(() => localStorage.getItem("lastProjectIdBySpace")))
      .toContain("project-b2");

    await switchSpaceByShortcut(page, 2);
    await expect(spaceSelect).toContainText("Space B");
    await expect(projectSelect).toContainText("Project B2");
    await expectSpaceWide(page, "space-b");

    await switchSpaceByShortcut(page, 1);
    await expect(spaceSelect).toContainText("Space A");
    await expect(projectSelect).toContainText("Project A2");
    await expectSpaceWide(page, "space-a");
  });

  test("ショートカット連続切替でも毎回切替先スペース全体になる", async ({
    page,
  }) => {
    await openTasksAtSpaceA(page);
    await page.getByTestId("task-project-project-a1").click();
    await expect(page.getByTestId("task-project-project-a1")).toHaveAttribute(
      "aria-selected",
      "true",
    );

    await switchSpaceByShortcut(page, 2);
    await expectSpaceWide(page, "space-b");

    await switchSpaceByShortcut(page, 3);
    await expectSpaceWide(page, "space-c");

    await switchSpaceByShortcut(page, 1);
    await expectSpaceWide(page, "space-a");
  });
});
