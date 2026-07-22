import { expect, test } from "@playwright/test";
import { SignJWT } from "jose";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

function readEnvValue(key: string) {
  const envPath = resolve(process.cwd(), ".env");
  try {
    const line = readFileSync(envPath, "utf8")
      .split(/\r?\n/)
      .find((item) => item.startsWith(`${key}=`));
    return line?.slice(key.length + 1).trim();
  } catch {
    return undefined;
  }
}

const SECRET = new TextEncoder().encode(
  process.env.NEXTAUTH_SECRET ||
    readEnvValue("NEXTAUTH_SECRET") ||
    "fallback-secret",
);

async function createSessionToken(userId: string) {
  return await new SignJWT({ sub: userId })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setJti(randomUUID())
    .setExpirationTime("7d")
    .sign(SECRET);
}

const project = {
  id: "project-1",
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
    parent_task_id: null,
    subtasks: [],
    activities: [],
    has_recurrence: false,
  };
}

test.describe("タスク一覧の複数選択D&D", () => {
  test.beforeEach(async ({ page }) => {
    const token = await createSessionToken("user-1");
    await page.context().addCookies([
      {
        name: "aoitalk_session",
        value: token,
        domain: "127.0.0.1",
        path: "/",
      },
    ]);
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

  test("選択した複数行を表示順のままドロップ先へまとめて並び替える", async ({
    page,
  }) => {
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
    const reorderRequests: string[][] = [];

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
        await route.fulfill({ json: { spaces: [], total: 0 } });
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
        await route.fulfill({ json: { success: true } });
        return;
      }
      if (url.pathname === `/api/projects/${project.id}/tags`) {
        await route.fulfill({ json: [] });
        return;
      }
      if (url.pathname === "/api/conversations") {
        await route.fulfill({ json: { conversations: [] } });
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
    await expect(
      page.getByText("A task"),
      runtimeErrors.join("\n"),
    ).toBeVisible();

    await page.getByTestId("task-row-task-a").getByRole("checkbox").click();
    await page.getByTestId("task-row-task-c").getByRole("checkbox").click();

    const dropTarget = page.getByTestId("task-row-task-d");
    const box = await dropTarget.boundingBox();
    if (!box) throw new Error("Drop target row was not measurable");

    await page.getByTestId("task-row-task-a").dragTo(dropTarget, {
      targetPosition: { x: 12, y: Math.max(4, box.height - 4) },
    });

    await expect
      .poll(() => reorderRequests.at(-1))
      .toEqual(["task-b", "task-d", "task-a", "task-c"]);
  });

  test("closes the row status menu after a status shortcut", async ({
    page,
  }) => {
    const tasks = [task("task-a", "A task", 0)];
    const statusUpdates: string[] = [];

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
        await route.fulfill({ json: { spaces: [], total: 0 } });
        return;
      }
      if (url.pathname === "/api/tasks") {
        await route.fulfill({ json: tasks });
        return;
      }
      if (
        url.pathname === "/api/tasks/task-a" &&
        route.request().method() === "PATCH"
      ) {
        const body = route.request().postDataJSON() as { status?: string };
        if (body.status) {
          statusUpdates.push(body.status);
          tasks[0] = { ...tasks[0], status: body.status };
        }
        await route.fulfill({ json: tasks[0] });
        return;
      }
      if (url.pathname === `/api/projects/${project.id}/tags`) {
        await route.fulfill({ json: [] });
        return;
      }
      if (url.pathname === "/api/conversations") {
        await route.fulfill({ json: { conversations: [] } });
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
    await expect(page.getByText("A task")).toBeVisible();

    await page
      .getByTestId("task-row-task-a")
      .locator('[data-slot="dropdown-menu-trigger"]')
      .click();
    const menu = page.locator('[data-slot="dropdown-menu-content"]').first();
    await expect(menu).toBeVisible();

    await menu.press("S");

    await expect.poll(() => statusUpdates.at(-1)).toBe("in_progress");
    await expect(menu).toBeHidden();
    await expect
      .poll(() =>
        page.evaluate(() => document.activeElement?.getAttribute("data-testid")),
      )
      .toBe("task-row-task-a");

    await page
      .getByTestId("task-row-task-a")
      .locator('[data-slot="dropdown-menu-trigger"]')
      .click();
    const openStatusMenu = page
      .locator('[data-slot="dropdown-menu-content"]')
      .first();
    await expect(openStatusMenu).toBeVisible();

    await openStatusMenu.press("O");

    await expect.poll(() => statusUpdates.at(-1)).toBe("open");
    await expect(openStatusMenu).toBeHidden();
    await expect
      .poll(() =>
        page.evaluate(() => document.activeElement?.getAttribute("data-testid")),
      )
      .toBe("task-row-task-a");

    await page
      .getByTestId("task-row-task-a")
      .locator('[data-slot="dropdown-menu-trigger"]')
      .click();
    const sameStatusMenu = page
      .locator('[data-slot="dropdown-menu-content"]')
      .first();
    await expect(sameStatusMenu).toBeVisible();

    const updateCount = statusUpdates.length;
    await sameStatusMenu.press("O");

    await expect.poll(() => statusUpdates.length).toBe(updateCount);
    await expect(sameStatusMenu).toBeHidden();
    await expect
      .poll(() =>
        page.evaluate(() => document.activeElement?.getAttribute("data-testid")),
      )
      .toBe("task-row-task-a");

    await page.keyboard.press("S");
    await expect.poll(() => statusUpdates.length).toBe(updateCount);
  });

  test("closes the sidebar status menu after a status shortcut", async ({
    page,
  }) => {
    const tasks = [task("task-a", "A task", 0)];
    const statusUpdates: string[] = [];

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
        await route.fulfill({ json: { spaces: [], total: 0 } });
        return;
      }
      if (url.pathname === "/api/tasks") {
        await route.fulfill({ json: tasks });
        return;
      }
      if (
        url.pathname === "/api/tasks/task-a" &&
        route.request().method() === "PATCH"
      ) {
        const body = route.request().postDataJSON() as { status?: string };
        if (body.status) {
          statusUpdates.push(body.status);
          tasks[0] = { ...tasks[0], status: body.status };
        }
        await route.fulfill({ json: tasks[0] });
        return;
      }
      if (url.pathname === `/api/projects/${project.id}/tags`) {
        await route.fulfill({ json: [] });
        return;
      }
      if (url.pathname === "/api/conversations") {
        await route.fulfill({ json: { conversations: [] } });
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
    await expect(page.getByText("A task").first()).toBeVisible();

    await page
      .locator(
        '[data-slot="sidebar-container"] [data-slot="sidebar-menu-button"]',
      )
      .nth(1)
      .click();
    await expect(
      page.locator('[data-slot="sidebar-container"]').getByText("A task"),
    ).toBeVisible();

    await page
      .locator('[data-slot="sidebar-container"]')
      .locator('[data-slot="dropdown-menu-trigger"][title]')
      .first()
      .click();
    const menu = page.locator('[data-slot="dropdown-menu-content"]').first();
    await expect(menu).toBeVisible();

    await menu.press("S");

    await expect.poll(() => statusUpdates.at(-1)).toBe("in_progress");
    await expect(menu).toBeHidden();
  });
});
