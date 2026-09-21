import { expect, test } from "@playwright/test";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

// Use real browser gestures: synthetic drag events cannot expose a menu
// backdrop intercepting the pointer before the native drag starts.
test.use({ viewport: { width: 1920, height: 1080 } });

const project = {
  id: "project-1",
  space_id: "space-1",
  is_member: true,
  can_write: true,
  name: "DND Project",
  description: null,
  slug: "dnd-project",
  color: "#2563eb",
};

function task(id: string, title: string, sortOrder: number) {
  const now = "2026-04-28T00:00:00.000Z";
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
    parent_task_id: null as string | null,
    subtasks: [],
    activities: [],
    has_recurrence: false,
  };
}

test.describe("タスク行のステータスメニューとD&D", () => {
  test.beforeEach(async ({ page }) => {
    const qaPath = resolve(process.cwd(), "../.env.qa-login");
    if (existsSync(qaPath)) {
      const qa = Object.fromEntries(
        readFileSync(qaPath, "utf8")
          .split(/\r?\n/)
          .filter((line) => line.includes("="))
          .map((line) => [
            line.slice(0, line.indexOf("=")),
            line.slice(line.indexOf("=") + 1),
          ]),
      );
      const login = await page.request.post("/api/auth/login", {
        data: {
          username: qa.AOITALK_QA_ADMIN_USERNAME,
          password: qa.AOITALK_QA_ADMIN_PASSWORD,
        },
      });
      expect(login.ok()).toBeTruthy();
    } else {
      await addAuthCookie(page);
    }
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

  for (const mode of [
    "single",
    "escape",
    "status",
    "multi",
    "subtask-status",
    "subtask-escape",
  ]) {
    test(mode, async ({ page }) => {
      const runtimeErrors: string[] = [];
      page.on("pageerror", (error) => runtimeErrors.push(error.message));
      page.on("console", (message) => {
        if (message.type() === "error") runtimeErrors.push(message.text());
      });
      const tasks = [
        task("task-a", "A task", 0),
        task("task-b", "B task", 1),
        task("task-c", "C task", 2),
        task("task-d", "D task", 3),
      ];
      if (mode.startsWith("subtask")) tasks[2].parent_task_id = "task-a";
      const reorderRequests: string[][] = [];

      await mockAuthenticatedApis(page);
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
          await route.fulfill({
            json: {
              spaces: [{ id: "space-1", name: "DND Space", is_member: true }],
              total: 1,
            },
          });
          return;
        }
        if (
          url.pathname === "/api/tasks/task-c" &&
          route.request().method() === "PATCH"
        ) {
          Object.assign(tasks[2], route.request().postDataJSON());
          await route.fulfill({ json: tasks[2] });
          return;
        }
        if (url.pathname === "/api/tasks") {
          await route.fulfill({ json: tasks });
          return;
        }
        if (
          url.pathname === "/api/tasks/reorder" ||
          url.pathname === `/api/projects/${project.id}/tasks/reorder`
        ) {
          const body = route.request().postDataJSON() as { task_ids: string[] };
          reorderRequests.push(body.task_ids);
          for (const [index, id] of body.task_ids.entries()) {
            const moved = tasks.find((item) => item.id === id);
            if (moved) moved.sort_order = index;
          }
          tasks.sort((left, right) => left.sort_order - right.sort_order);
          await route.fulfill({ json: { success: true } });
          return;
        }
        if (url.pathname === `/api/projects/${project.id}/tags`) {
          await route.fulfill({ json: [] });
          return;
        }
        await route.fallback();
      });

      await page.goto("/tasks");
      await expect(
        page.getByTestId("task-row-task-a"),
        runtimeErrors.join("\n"),
      ).toBeVisible();

      if (mode.startsWith("subtask"))
        await page
          .getByTestId("task-row-task-a")
          .getByRole("button", { name: "サブタスクを展開", exact: true })
          .click();
      const row = page.getByTestId(
        mode.startsWith("subtask") ? "task-row-task-c" : "task-row-task-a",
      );
      if (mode === "multi") {
        await row.getByRole("checkbox").click();
        await page.getByTestId("task-row-task-c").getByRole("checkbox").click();
      }
      if (mode.includes("escape")) {
        await row
          .locator('[data-slot="dropdown-menu-trigger"]')
          .first()
          .click();
        await page.getByRole("menu").press("Escape");
        await expect(row).toBeFocused();
      }
      const source = mode.includes("status")
        ? row.locator('[data-slot="dropdown-menu-trigger"]').first()
        : row.getByText(mode.startsWith("subtask") ? "C task" : "A task", {
            exact: true,
          });
      const sourceBox = await source.boundingBox();
      if (!sourceBox) throw new Error("Missing drag source");
      await page.mouse.move(
        sourceBox.x + sourceBox.width / 2,
        sourceBox.y + sourceBox.height / 2,
      );
      await page.mouse.down();
      await page.mouse.move(
        sourceBox.x + sourceBox.width / 2 + 12,
        sourceBox.y + sourceBox.height / 2,
        { steps: 5 },
      );
      const dropTarget = page.getByTestId("task-row-task-d");
      const box = await dropTarget.boundingBox();
      if (!box) throw new Error("Missing drop target");
      await page.mouse.move(box.x + 12, box.y + box.height - 4, { steps: 5 });
      await page.mouse.move(box.x + 12, box.y + box.height - 4);
      await page.mouse.up();

      await expect
        .poll(() => reorderRequests.at(-1))
        .toEqual(
          mode === "multi"
            ? ["task-b", "task-d", "task-a", "task-c"]
            : mode.startsWith("subtask")
              ? ["task-a", "task-b", "task-d", "task-c"]
              : ["task-b", "task-c", "task-d", "task-a"],
        );
      // Ordinary clicks must still open the menu, and Escape must return to the row.
      const movedRow = page.getByTestId(
        mode.startsWith("subtask") ? "task-row-task-c" : "task-row-task-a",
      );
      const trigger = movedRow
        .locator('[data-slot="dropdown-menu-trigger"]')
        .first();
      await trigger.click();
      await expect(page.getByRole("menu")).toBeVisible();
      await page.getByRole("menu").press("Escape");
      await expect(movedRow).toBeFocused();
    });
  }
});

