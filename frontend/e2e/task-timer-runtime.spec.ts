import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const PROJECT = {
  id: "timer-project",
  name: "Timer project",
  slug: "timer-project",
  space_id: null,
  is_completed: false,
};

const TASK_ID = "timer-task";
const DEPLOYMENT_TIMEZONE = "Asia/Tokyo";

function explicitTimestamp(instantMs: number, timezone: string): string {
  const date = new Date(instantMs);
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
      timeZoneName: "longOffset",
    })
      .formatToParts(date)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  ) as Record<string, string>;
  const offsetName = parts.timeZoneName || "GMT";
  const offset = offsetName === "GMT" ? "+00:00" : offsetName.slice(3);
  const milliseconds = String(date.getUTCMilliseconds()).padStart(3, "0");
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}.${milliseconds}${offset}`;
}

function makeTask(activeTimeEntry: Record<string, unknown> | null, totalSeconds: number) {
  return {
    id: TASK_ID,
    project_id: PROJECT.id,
    project_name: PROJECT.name,
    title: "Explicit offset timer",
    description: null,
    status: activeTimeEntry ? "in_progress" : "open",
    priority: "medium",
    start_at: null,
    end_at: null,
    all_day: false,
    reminder_offsets: [],
    notifications_enabled: true,
    source: "manual",
    created_by: "user-1",
    completed_at: null,
    created_at: "2026-09-08T00:00:00.000Z",
    updated_at: "2026-09-08T00:00:00.000Z",
    metadata: {},
    assignees: [],
    tags: [],
    active_time_entry: activeTimeEntry,
    estimated_hours: null,
    sort_order: 0,
    total_time_seconds: totalSeconds,
    parent_task_id: null,
    subtasks: [],
    activities: [],
    has_recurrence: false,
  };
}

function elapsedSeconds(text: string | null): number {
  const match = text?.match(/\+(\d+)(m|s)/);
  if (!match) return 0;
  const value = Number(match[1]);
  return match[2] === "m" ? value * 60 : value;
}

async function routeTimerApis(page: Page) {
  let active: {
    id: string;
    started_at: string;
    ended_at: null;
    duration_seconds: null;
  } | null = null;
  let totalSeconds = 0;
  let startedAtForRun = "";

  await mockAuthenticatedApis(page);
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();

    if (url.pathname === "/api/projects") {
      await route.fulfill({ json: { projects: [PROJECT], total: 1 } });
      return;
    }
    if (url.pathname === "/api/spaces") {
      await route.fulfill({ json: { spaces: [], total: 0 } });
      return;
    }
    if (url.pathname === "/api/tasks" && method === "GET") {
      await route.fulfill({ json: [makeTask(active, totalSeconds)] });
      return;
    }
    if (url.pathname === `/api/projects/${PROJECT.id}/tags`) {
      await route.fulfill({ json: [] });
      return;
    }
    if (url.pathname === "/api/time-entries/start" && method === "POST") {
      // Enterprise deployment timezone is fixed; only the browser timezone
      // varies between the two contexts below.
      const startedAt = explicitTimestamp(
        Date.now() - 3_000,
        DEPLOYMENT_TIMEZONE,
      );
      startedAtForRun = startedAt;
      active = {
        id: "timer-entry",
        started_at: startedAt,
        ended_at: null,
        duration_seconds: null,
      };
      await route.fulfill({
        json: {
          id: active.id,
          task_id: TASK_ID,
          user_id: "user-1",
          source: "manual",
          started_at: active.started_at,
          ended_at: null,
          duration_seconds: null,
        },
      });
      return;
    }
    if (url.pathname === "/api/time-entries/stop" && method === "POST") {
      if (!active) {
        await route.fulfill({ status: 404, json: { detail: "No active timer" } });
        return;
      }
      const endedAt = explicitTimestamp(Date.now(), DEPLOYMENT_TIMEZONE);
      totalSeconds = Math.max(
        0,
        Math.floor((Date.parse(endedAt) - Date.parse(active.started_at)) / 1000),
      );
      const stopped = {
        id: active.id,
        task_id: TASK_ID,
        user_id: "user-1",
        source: "manual",
        started_at: active.started_at,
        ended_at: endedAt,
        duration_seconds: totalSeconds,
      };
      active = null;
      await route.fulfill({ json: stopped });
      return;
    }

    await route.fallback();
  });

  return {
    getStartedAt: () => startedAtForRun,
    getTotalSeconds: () => totalSeconds,
  };
}

test.describe("task timer explicit-offset runtime", () => {
  for (const timezone of ["Asia/Tokyo", "America/New_York"]) {
    test(`keeps active elapsed and stopped duration across reload (${timezone})`, async ({
      browser,
    }) => {
      const context = await browser.newContext({ timezoneId: timezone });
      const page = await context.newPage();
      try {
        await addAuthCookie(page);
        const timerState = await routeTimerApis(page);
        await page.goto("/tasks");

        const row = page.getByTestId(`task-row-${TASK_ID}`);
        await expect(row).toBeVisible();
        await row
          .getByRole("button", { name: "Time Tracked のタイマー開始" })
          .click();
        expect(timerState.getStartedAt()).toMatch(/\+09:00$/);

        const activeElapsed = row.locator('[title^="現在の経過時間"]');
        await expect(activeElapsed).toBeVisible();
        const beforeDelay = elapsedSeconds(await activeElapsed.textContent());
        await page.waitForTimeout(1_500);
        await expect
          .poll(async () => elapsedSeconds(await activeElapsed.textContent()))
          .toBeGreaterThan(beforeDelay);

        await page.reload();
        const reloadedRow = page.getByTestId(`task-row-${TASK_ID}`);
        await expect(reloadedRow).toBeVisible();
        const reloadedElapsed = reloadedRow.locator(
          '[title^="現在の経過時間"]',
        );
        await expect(reloadedElapsed).toBeVisible();
        expect(elapsedSeconds(await reloadedElapsed.textContent())).toBeGreaterThan(0);

        await reloadedRow
          .getByRole("button", { name: "Time Tracked のタイマー停止" })
          .click();
        await expect(
          reloadedRow.getByRole("button", { name: "Time Tracked のタイマー開始" }),
        ).toBeVisible();

        await page.reload();
        const stoppedRow = page.getByTestId(`task-row-${TASK_ID}`);
        await expect(stoppedRow).toBeVisible();
        await expect(
          stoppedRow.locator('[title^="実績時間 "]'),
        ).toBeVisible();
        const stoppedDuration = timerState.getTotalSeconds();
        expect(stoppedDuration).toBeGreaterThanOrEqual(3);
        expect(stoppedDuration).toBeLessThanOrEqual(30);
        await expect(
          stoppedRow.locator('[title^="実績時間 "]'),
        ).toHaveAttribute(
          "title",
          `実績時間 ${Math.floor(stoppedDuration / 60)}m`,
        );
      } finally {
        await context.close();
      }
    });
  }
});
