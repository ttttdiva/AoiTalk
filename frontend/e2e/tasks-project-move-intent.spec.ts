import { expect, test } from "@playwright/test";
import { addAuthCookie } from "./support/auth";

const projects = [
  {
    id: "project-1",
    name: "Project One",
    description: null,
    slug: "project-one",
    color: "#2563eb",
  },
  {
    id: "project-2",
    name: "Project Two",
    description: null,
    slug: "project-two",
    color: "#16a34a",
  },
];

function task(id: string, projectId: string, title: string, sortOrder: number) {
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

test.describe("タスクのプロジェクト移動", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await page.addInitScript(() => {
      localStorage.clear();
      localStorage.setItem(
        "tasks-sidebar-view-state",
        JSON.stringify({
          filter: "all",
          projectTab: "all",
          showClosed: false,
          showFuture: true,
          customFilter: { logic: "and", rules: [] },
        }),
      );
    });
  });

  test("タスクコマンドから別スペースの同名プロジェクトへ移動できる", async ({
    page,
  }) => {
    const crossSpaceProjects = [
      {
        ...projects[0],
        id: "project-source",
        name: "Shared Project",
        slug: "source-shared-project",
        space_id: "space-source",
      },
      {
        ...projects[1],
        id: "project-target",
        name: "Shared Project",
        slug: "target-shared-project",
        space_id: "space-target",
      },
      {
        ...projects[1],
        id: "project-read-only",
        name: "Read Only Project",
        slug: "read-only-project",
        space_id: "space-target",
        can_write: false,
      },
    ];
    const spaces = [
      { id: "space-source", name: "Source Space", color: "#2563eb" },
      { id: "space-target", name: "Target Space", color: "#16a34a" },
    ];
    let currentTasks = [task("task-a", "project-1", "Cross-space move", 0)].map(
      (item) => ({
        ...item,
        project_id: "project-source",
        project_name: "Shared Project",
      }),
    );
    const patchPayloads: Record<string, unknown>[] = [];

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
      if (url.pathname === "/api/projects") {
        await route.fulfill({
          json: {
            projects: crossSpaceProjects,
            total: crossSpaceProjects.length,
          },
        });
        return;
      }
      if (url.pathname === "/api/spaces") {
        await route.fulfill({ json: { spaces, total: spaces.length } });
        return;
      }
      if (url.pathname === "/api/tasks/task-a" && method === "PATCH") {
        const body = route.request().postDataJSON() as Record<string, unknown>;
        patchPayloads.push(body);
        const moved = {
          ...currentTasks[0],
          project_id: body.project_id,
          sort_order: 0,
        };
        currentTasks = [];
        await route.fulfill({ json: moved });
        return;
      }
      if (url.pathname === "/api/tasks") {
        await route.fulfill({ json: currentTasks });
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

    await page.goto("/tasks");
    await page.evaluate(() => {
      localStorage.setItem("selectedSpaceId", "space-source");
      localStorage.setItem("selectedProjectId", "project-source");
    });
    await page.reload();
    const taskRow = page.getByTestId("task-row-task-a");
    await expect(taskRow).toBeVisible();
    await taskRow.focus();
    await page.keyboard.press("/");
    await expect(page.getByText("タスクコマンド")).toBeVisible();

    const commandInput = page.getByPlaceholder("/ でコマンド一覧");
    await commandInput.fill("/m ");
    await expect(page.getByText("Source Space / Shared Project")).toBeVisible();
    await expect(page.getByText("Target Space / Shared Project")).toBeVisible();
    await expect(page.getByText("Target Space / Read Only Project")).toHaveCount(
      0,
    );
    await page.getByText("Target Space / Shared Project").click();

    await expect.poll(() => patchPayloads).toHaveLength(1);
    expect(patchPayloads[0]).toMatchObject({
      project_id: "project-target",
      project_move_intent: true,
    });
    await expect(page.getByText("Cross-space move")).toHaveCount(0);
    await expect
      .poll(() => page.evaluate(() => localStorage.getItem("selectedSpaceId")))
      .toBe("space-source");
  });

  test("一覧の明示的なプロジェクト変更だけ移動意図を送る", async ({ page }) => {
    let tasks = [
      task("task-a", "project-1", "Move me", 0),
      task("task-b", "project-2", "Existing B", 0),
      task("task-c", "project-2", "Existing C", 1),
    ];
    const patchPayloads: unknown[] = [];

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
      if (url.pathname === "/api/tasks/task-a" && method === "PATCH") {
        const body = route.request().postDataJSON();
        patchPayloads.push(body);
        const moved = {
          ...tasks.find((item) => item.id === "task-a")!,
          project_id: body.project_id,
          project_name:
            projects.find((project) => project.id === body.project_id)?.name ??
            "Project Two",
          sort_order: 2,
        };
        tasks = [...tasks.filter((item) => item.id !== "task-a"), moved].sort(
          (a, b) =>
            a.project_id === b.project_id
              ? a.sort_order - b.sort_order
              : a.project_id.localeCompare(b.project_id),
        );
        await route.fulfill({ json: moved });
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

    await page.goto("/tasks");
    await expect(page.getByText("Move me")).toBeVisible();

    await page
      .getByTestId("task-row-task-a")
      .locator("select")
      .selectOption("project-2");

    await expect.poll(() => patchPayloads).toHaveLength(1);
    expect(patchPayloads[0]).toMatchObject({
      project_id: "project-2",
      project_move_intent: true,
    });

    await expect
      .poll(async () =>
        page.locator("[data-testid^='task-row-']").evaluateAll((rows) =>
          rows
            .map((row) => {
              const text = row.textContent || "";
              if (text.includes("Existing B")) return "task-b";
              if (text.includes("Existing C")) return "task-c";
              if (text.includes("Move me")) return "task-a";
              return null;
            })
            .filter((id) => id !== null),
        ),
      )
      .toEqual(["task-a", "task-b", "task-c"]);
  });

  test("別プロジェクトの行へD&Dしても所属プロジェクトを変更しない", async ({
    page,
  }) => {
    const tasks = [
      task("task-a", "project-1", "Drag source", 0),
      task("task-b", "project-2", "Other project B", 0),
      task("task-c", "project-2", "Other project C", 1),
    ];
    const patchPayloads: unknown[] = [];
    const reorderRequests: unknown[] = [];

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
      if (url.pathname === "/api/tasks/task-a" && method === "PATCH") {
        patchPayloads.push(route.request().postDataJSON());
        await route.fulfill({ json: tasks[0] });
        return;
      }
      if (url.pathname === "/api/tasks/reorder") {
        reorderRequests.push(route.request().postDataJSON());
        await route.fulfill({ json: { success: true } });
        return;
      }
      if (
        url.pathname.startsWith("/api/projects/") &&
        url.pathname.endsWith("/tasks/reorder")
      ) {
        reorderRequests.push(route.request().postDataJSON());
        await route.fulfill({ json: { success: true } });
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

    await page.goto("/tasks");
    await expect(page.getByText("Drag source")).toBeVisible();

    const dropTarget = page.getByTestId("task-row-task-b");
    const box = await dropTarget.boundingBox();
    if (!box) throw new Error("Drop target row was not measurable");

    await page.getByTestId("task-row-task-a").dragTo(dropTarget, {
      targetPosition: { x: 12, y: Math.max(4, box.height - 4) },
    });
    expect(patchPayloads).toEqual([]);
    await expect
      .poll(() => reorderRequests.at(-1))
      .toEqual({ task_ids: ["task-b", "task-a", "task-c"] });
    await expect
      .poll(async () =>
        page.locator("[data-testid^='task-row-']").evaluateAll((rows) =>
          rows
            .map((row) => {
              const text = row.textContent || "";
              if (text.includes("Drag source")) return "task-a";
              if (text.includes("Other project B")) return "task-b";
              if (text.includes("Other project C")) return "task-c";
              return null;
            })
            .filter((id) => id !== null),
        ),
      )
      .toEqual(["task-a", "task-b", "task-c"]);
  });
  test("D&D over another project reorders the ALL order without moving projects", async ({
    page,
  }) => {
    const tasks = [
      task("task-a", "project-1", "Drag source", 0),
      task("task-d", "project-1", "Same project anchor", 1),
      task("task-b", "project-2", "Other project B", 0),
      task("task-c", "project-2", "Other project C", 1),
    ];
    const patchPayloads: unknown[] = [];
    const reorderRequests: { task_ids: string[] }[] = [];

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
      if (url.pathname.startsWith("/api/tasks/") && method === "PATCH") {
        patchPayloads.push(route.request().postDataJSON());
        await route.fulfill({ json: tasks[0] });
        return;
      }
      if (url.pathname === "/api/tasks/reorder") {
        reorderRequests.push(
          route.request().postDataJSON() as { task_ids: string[] },
        );
        await route.fulfill({ json: { success: true } });
        return;
      }
      if (
        url.pathname.startsWith("/api/projects/") &&
        url.pathname.endsWith("/tasks/reorder")
      ) {
        await route.fulfill({ json: { success: true } });
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

    await page.goto("/tasks");
    await expect(page.getByText("Drag source")).toBeVisible();

    const dropTarget = page.getByTestId("task-row-task-c");
    const box = await dropTarget.boundingBox();
    if (!box) throw new Error("Drop target row was not measurable");

    await page.getByTestId("task-row-task-a").dragTo(dropTarget, {
      targetPosition: { x: 12, y: Math.max(4, box.height - 4) },
    });

    await expect
      .poll(() => reorderRequests.at(-1))
      .toEqual({ task_ids: ["task-d", "task-b", "task-c", "task-a"] });
    expect(patchPayloads).toEqual([]);
  });
  test("Project column move keeps the current visible row position after refetch", async ({
    page,
  }) => {
    let tasks = [
      task("task-a", "project-2", "Keep my row", 0),
      task("task-b", "project-2", "Neighbor B", 1),
      task("task-c", "project-2", "Neighbor C", 2),
    ];
    const patchPayloads: unknown[] = [];

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
      if (url.pathname === "/api/projects") {
        await route.fulfill({ json: { projects, total: projects.length } });
        return;
      }
      if (url.pathname === "/api/spaces") {
        await route.fulfill({ json: { spaces: [], total: 0 } });
        return;
      }
      if (url.pathname === "/api/tasks") {
        await route.fulfill({
          json: [...tasks].sort(
            (a, b) =>
              a.sort_order - b.sort_order ||
              a.project_id.localeCompare(b.project_id),
          ),
        });
        return;
      }
      if (url.pathname === "/api/tasks/task-b" && method === "PATCH") {
        const body = route.request().postDataJSON();
        patchPayloads.push(body);
        const moved = {
          ...tasks.find((item) => item.id === "task-b")!,
          project_id: body.project_id,
          project_name:
            projects.find((project) => project.id === body.project_id)?.name ??
            "Project One",
        };
        tasks = [moved, ...tasks.filter((item) => item.id !== "task-b")];
        await route.fulfill({ json: moved });
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

    await page.goto("/tasks");
    await expect(page.getByText("Keep my row")).toBeVisible();

    await page
      .getByTestId("task-row-task-b")
      .locator("select")
      .selectOption("project-1");

    await expect.poll(() => patchPayloads).toHaveLength(1);
    expect(patchPayloads[0]).toMatchObject({
      project_id: "project-1",
      project_move_intent: true,
    });
    await expect
      .poll(async () =>
        page.locator("[data-testid^='task-row-']").evaluateAll((rows) =>
          rows
            .map((row) => {
              const text = row.textContent || "";
              if (text.includes("Keep my row")) return "task-a";
              if (text.includes("Neighbor B")) return "task-b";
              if (text.includes("Neighbor C")) return "task-c";
              return null;
            })
            .filter((id) => id !== null),
        ),
      )
      .toEqual(["task-a", "task-b", "task-c"]);

    await page.reload();
    await expect(page.getByText("Keep my row")).toBeVisible();
    await expect
      .poll(async () =>
        page.locator("[data-testid^='task-row-']").evaluateAll((rows) =>
          rows
            .map((row) => {
              const text = row.textContent || "";
              if (text.includes("Keep my row")) return "task-a";
              if (text.includes("Neighbor B")) return "task-b";
              if (text.includes("Neighbor C")) return "task-c";
              return null;
            })
            .filter((id) => id !== null),
        ),
      )
      .toEqual(["task-a", "task-b", "task-c"]);
  });
});
