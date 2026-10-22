import { expect, test, type Locator, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const PROJECT = {
  id: "timer-project",
  name: "Timer project",
  slug: "timer-project",
  space_id: "space-1",
  is_member: true,
  can_write: true,
  is_completed: false,
};

const TASK_ID = "timer-task";
const DEPLOYMENT_TIMEZONE = "Asia/Tokyo";
const PRIOR_COMPLETED_SECONDS = 3_661;

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

function makeTask(
  activeTimeEntry: Record<string, unknown> | null,
  totalSeconds: number,
) {
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

function formatClock(seconds: number): string {
  return [
    Math.floor(seconds / 3600),
    Math.floor((seconds % 3600) / 60),
    seconds % 60,
  ]
    .map((part) => String(part).padStart(2, "0"))
    .join(":");
}

function clockSeconds(text: string | null): number {
  expect(text).toMatch(/^\d{2,}:\d{2}:\d{2}$/);
  const [hours, minutes, seconds] = text!.split(":").map(Number);
  return hours * 3600 + minutes * 60 + seconds;
}

async function expectSingleClock(row: Locator): Promise<Locator> {
  const clock = row.locator('[title^="実績時間 "]');
  await expect(clock).toHaveCount(1);
  await expect(clock).toHaveText(/^\d{2,}:\d{2}:\d{2}$/);
  const { text, title } = await clock.evaluate((element) => ({
    text: element.textContent,
    title: element.getAttribute("title"),
  }));
  expect(title).toBe(`実績時間 ${text}`);
  await expect(row.locator('[title^="現在の経過時間"]')).toHaveCount(0);
  await expect(row.getByText(/^\+\d+[ms]$/)).toHaveCount(0);
  await expect(row.locator(".rounded-full.bg-green-500")).toHaveCount(0);
  return clock;
}

async function routeTimerApis(page: Page, initialTotalSeconds = 0) {
  let active: {
    id: string;
    started_at: string;
    ended_at: null;
    duration_seconds: null;
  } | null = null;
  let totalSeconds = initialTotalSeconds;
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
      await route.fulfill({ json: { spaces: [{ id: "space-1", name: "QA Space", is_member: true, can_write: true }], total: 1 } });
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
        await route.fulfill({
          status: 404,
          json: { detail: "No active timer" },
        });
        return;
      }
      const endedAt = explicitTimestamp(Date.now(), DEPLOYMENT_TIMEZONE);
      const durationSeconds = Math.max(
        0,
        Math.floor(
          (Date.parse(endedAt) - Date.parse(active.started_at)) / 1000,
        ),
      );
      totalSeconds += durationSeconds;
      const stopped = {
        id: active.id,
        task_id: TASK_ID,
        user_id: "user-1",
        source: "manual",
        started_at: active.started_at,
        ended_at: endedAt,
        duration_seconds: durationSeconds,
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
  test("shows an untracked task as a single zero clock", async ({ page }) => {
    await addAuthCookie(page);
    await routeTimerApis(page);
    await page.goto("/tasks");

    const row = page.getByTestId(`task-row-${TASK_ID}`);
    await expect(row).toBeVisible();
    const clock = await expectSingleClock(row);
    await expect(clock).toHaveText("00:00:00");
    await expect(clock).toHaveAttribute("title", "実績時間 00:00:00");
    await expect(
      row.getByRole("button", { name: "Time Tracked のタイマー開始" }),
    ).toBeVisible();
  });

  for (const timezone of ["Asia/Tokyo", "America/New_York"]) {
    test(`keeps the combined clock and stopped duration across reload (${timezone})`, async ({
      browser,
    }) => {
      const context = await browser.newContext({ timezoneId: timezone });
      const page = await context.newPage();
      try {
        await addAuthCookie(page);
        const timerState = await routeTimerApis(page, PRIOR_COMPLETED_SECONDS);
        await page.goto("/tasks");

        const row = page.getByTestId(`task-row-${TASK_ID}`);
        await expect(row).toBeVisible();
        const initialClock = await expectSingleClock(row);
        await expect(initialClock).toHaveText("01:01:01");
        await row
          .getByRole("button", { name: "Time Tracked のタイマー開始" })
          .click();
        expect(timerState.getStartedAt()).toMatch(/\+09:00$/);

        const activeClock = await expectSingleClock(row);
        // The first render can still hold the tick from just before start.
        await expect
          .poll(async () => clockSeconds(await activeClock.textContent()))
          .toBeGreaterThanOrEqual(PRIOR_COMPLETED_SECONDS + 3);
        const beforeDelay = clockSeconds(await activeClock.textContent());
        expect(beforeDelay).toBeLessThanOrEqual(PRIOR_COMPLETED_SECONDS + 30);
        await page.waitForTimeout(1_500);
        await expect
          .poll(async () => clockSeconds(await activeClock.textContent()))
          .toBeGreaterThan(beforeDelay);
        const beforeReload = clockSeconds(await activeClock.textContent());

        await page.reload();
        const reloadedRow = page.getByTestId(`task-row-${TASK_ID}`);
        await expect(reloadedRow).toBeVisible();
        const reloadedClock = await expectSingleClock(reloadedRow);
        expect(
          clockSeconds(await reloadedClock.textContent()),
        ).toBeGreaterThanOrEqual(beforeReload);
        expect(
          clockSeconds(await reloadedClock.textContent()),
        ).toBeLessThanOrEqual(PRIOR_COMPLETED_SECONDS + 30);

        await reloadedRow
          .getByRole("button", { name: "Time Tracked のタイマー停止" })
          .click();
        await expect(
          reloadedRow.getByRole("button", {
            name: "Time Tracked のタイマー開始",
          }),
        ).toBeVisible();
        const stoppedTotal = timerState.getTotalSeconds();
        const stoppedDuration = stoppedTotal - PRIOR_COMPLETED_SECONDS;
        expect(stoppedDuration).toBeGreaterThanOrEqual(4);
        expect(stoppedDuration).toBeLessThanOrEqual(30);
        const stoppedClock = await expectSingleClock(reloadedRow);
        await expect(stoppedClock).toHaveText(formatClock(stoppedTotal));
        await expect(stoppedClock).toHaveAttribute(
          "title",
          `実績時間 ${formatClock(stoppedTotal)}`,
        );
        // Stopping must retain the returned duration immediately and freeze
        // the clock rather than dropping back to the prior completed total.
        await page.waitForTimeout(1_500);
        await expect(stoppedClock).toHaveText(formatClock(stoppedTotal));

        await page.reload();
        const stoppedRow = page.getByTestId(`task-row-${TASK_ID}`);
        await expect(stoppedRow).toBeVisible();
        const reloadedStoppedClock = await expectSingleClock(stoppedRow);
        await expect(reloadedStoppedClock).toHaveText(
          formatClock(stoppedTotal),
        );
        await expect(reloadedStoppedClock).toHaveAttribute(
          "title",
          `実績時間 ${formatClock(stoppedTotal)}`,
        );
        await expect(
          stoppedRow.getByRole("button", {
            name: "Time Tracked のタイマー開始",
          }),
        ).toBeVisible();
      } finally {
        await context.close();
      }
    });
  }
});


for (const operation of ["start", "stop"] as const) {
  test(`late ${operation} completion preserves a different task detail`, async ({ page }) => {
    await addAuthCookie(page);
    await routeTimerApis(page);
    const oldEntry = {
      id: "old-entry", task_id: TASK_ID, user_id: "user-1", source: "manual",
      started_at: new Date().toISOString(), ended_at: null, duration_seconds: null,
    };
    let active = operation === "stop";
    const oldTask = () => ({ ...makeTask(active ? oldEntry : null, 0), title: "Pending timer task" });
    const nextTask = { ...makeTask(null, 0), id: "next-timer-task", title: "Current timer task" };
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let completed = false;
    const detailReads: string[] = [];
    await page.route("**/api/**", async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === `/api/time-entries/${operation}` && route.request().method() === "POST") {
        await gate;
        active = operation === "start";
        await route.fulfill({ json: { ...oldEntry, ended_at: active ? null : new Date().toISOString() } });
        completed = true;
        return;
      }
      if (url.pathname === "/api/tasks") {
        await route.fulfill({ json: [oldTask(), nextTask] });
        return;
      }
      if (url.pathname === `/api/tasks/${TASK_ID}` || url.pathname === `/api/tasks/${nextTask.id}`) {
        detailReads.push(url.pathname);
        await route.fulfill({ json: url.pathname.endsWith(nextTask.id) ? nextTask : oldTask() });
        return;
      }
      if (url.pathname === `/api/projects/${PROJECT.id}/assignee-candidates`) {
        await route.fulfill({ json: { members: [], total: 0 } });
        return;
      }
      if (/\/api\/tasks\/[^/]+\/recurrence$/.test(url.pathname)) {
        await route.fulfill({ json: null });
        return;
      }
      if (/\/api\/tasks\/[^/]+\/(attachments|references|dependencies)$/.test(url.pathname)) {
        await route.fulfill({ json: [] });
        return;
      }
      await route.fallback();
    });
    await page.goto("/tasks");
    await page.getByTestId(`task-row-${TASK_ID}`).getByText("Pending timer task", { exact: true }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByText("Pending timer task", { exact: true })).toBeVisible();
    const timerRequest = page.waitForRequest((request) => request.url().endsWith(`/api/time-entries/${operation}`));
    await dialog.getByRole("button", { name: operation === "start" ? "Start" : "Stop", exact: true }).click();
    await timerRequest;
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await page.getByTestId(`task-row-${nextTask.id}`).getByText(nextTask.title, { exact: true }).click();
    await expect(dialog.getByText(nextTask.title, { exact: true })).toBeVisible();
    detailReads.length = 0;
    release();
    await expect.poll(() => completed).toBe(true);
    await expect.poll(() => detailReads).toContain(`/api/tasks/${nextTask.id}`);
    await expect(dialog.getByText(nextTask.title, { exact: true })).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Start", exact: true })).toBeEnabled();
    expect(detailReads).not.toContain(`/api/tasks/${TASK_ID}`);
  });
}
