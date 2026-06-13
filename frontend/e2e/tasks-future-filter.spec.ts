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

function dateTimeAfterDays(days: number): string {
  const date = new Date();
  date.setDate(date.getDate() + days);
  date.setHours(10, 0, 0, 0);
  return date.toISOString();
}

const project = {
  id: "project-1",
  name: "Filter Project",
  description: null,
  slug: "filter-project",
  color: "#2563eb",
};

function task(
  id: string,
  title: string,
  startAt: string,
  hasRecurrence = false,
) {
  return {
    id,
    project_id: project.id,
    project_name: project.name,
    title,
    description: null,
    status: "open",
    priority: "medium",
    start_at: startAt,
    end_at: null,
    all_day: false,
    reminder_offsets: [],
    notifications_enabled: true,
    source: "manual",
    created_by: "user-1",
    completed_at: null,
    created_at: startAt,
    updated_at: startAt,
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
    has_recurrence: hasRecurrence,
  };
}

test.describe("タスク一覧の未来タスクフィルタ", () => {
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
          showFuture: false,
          customFilter: { logic: "and", rules: [] },
        }),
      );
    });

    const tasks = [
      task("today", "今日のタスク", dateTimeAfterDays(0)),
      task("future-recurring", "未来の繰り返しタスク", dateTimeAfterDays(2), true),
      task("future-normal", "未来の通常タスク", dateTimeAfterDays(3)),
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
  });

  test("チェック off では繰り返し設定のある未来タスクも隠す", async ({
    page,
  }) => {
    const runtimeErrors: string[] = [];
    page.on("pageerror", (error) => runtimeErrors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") runtimeErrors.push(message.text());
    });
    await page.goto("/tasks");

    await expect(
      page.getByText("今日のタスク"),
      runtimeErrors.join("\n"),
    ).toBeVisible();
    await expect(page.getByText("未来の繰り返しタスク")).toBeHidden();
    await expect(page.getByText("未来の通常タスク")).toBeHidden();

    await page.getByRole("checkbox", { name: "未来のタスクを表示" }).click();

    await expect(page.getByText("未来の繰り返しタスク")).toBeVisible();
    await expect(page.getByText("未来の通常タスク")).toBeVisible();
  });
});
