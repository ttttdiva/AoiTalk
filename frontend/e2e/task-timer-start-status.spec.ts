import { expect, test } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const PROJECT = {
  id: "timer-status-project",
  name: "Timer status project",
  slug: "timer-status-project",
  space_id: "space-1",
  is_member: true,
  can_write: true,
  is_completed: false,
};
const TASK_ID = "timer-status-task";
const OCCURRENCE_START = "2026-10-21T01:00:00.000Z";

function makeTask(status: string, occurrenceStatus: string | null, active: unknown) {
  return {
    id: TASK_ID,
    project_id: PROJECT.id,
    project_name: PROJECT.name,
    title: "Recurring timer status task",
    description: null,
    status,
    priority: "medium",
    start_at: OCCURRENCE_START,
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
    active_time_entry: active,
    estimated_hours: null,
    sort_order: 0,
    total_time_seconds: 0,
    parent_task_id: null,
    subtasks: [],
    activities: [],
    has_recurrence: true,
    effective_occurrence_id: "occ-1",
    effective_occurrence_start_at: OCCURRENCE_START,
    effective_occurrence_status: occurrenceStatus,
  };
}

test("Alt+S starts the timer and saves the recurring occurrence as in progress", async ({ page }) => {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  let active: Record<string, unknown> | null = null;
  let occurrenceStatus = "open";
  const occurrenceBodies: Array<Record<string, unknown>> = [];
  const current = () => makeTask("open", occurrenceStatus, active);

  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    const path = url.pathname;
    if (path === "/api/projects") return route.fulfill({ json: { projects: [PROJECT], total: 1 } });
    if (path === "/api/spaces") {
      return route.fulfill({ json: { spaces: [{ id: "space-1", name: "QA Space", is_member: true, can_write: true }], total: 1 } });
    }
    if (path === "/api/tasks" && method === "GET") return route.fulfill({ json: [current()] });
    if (path === `/api/tasks/${TASK_ID}` && method === "GET") return route.fulfill({ json: current() });
    if (path === "/api/time-entries/start" && method === "POST") {
      active = {
        id: "entry-1", task_id: TASK_ID, user_id: "user-1", source: "manual",
        started_at: new Date().toISOString(), ended_at: null, duration_seconds: null,
      };
      return route.fulfill({ json: active });
    }
    if (path === `/api/tasks/${TASK_ID}/occurrence` && method === "PATCH") {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      occurrenceBodies.push(body);
      occurrenceStatus = String(body.status);
      return route.fulfill({ json: { success: true } });
    }
    if (path === `/api/projects/${PROJECT.id}/assignee-candidates`) return route.fulfill({ json: { members: [], total: 0 } });
    if (path === `/api/projects/${PROJECT.id}/tags`) return route.fulfill({ json: [] });
    if (/\/api\/tasks\/[^/]+\/recurrence$/.test(path)) return route.fulfill({ json: null });
    if (/\/api\/tasks\/[^/]+\/(attachments|references|dependencies)$/.test(path)) return route.fulfill({ json: [] });
    return route.fallback();
  });

  await page.goto("/tasks");
  await page.getByTestId(`task-row-${TASK_ID}`).getByText("Recurring timer status task", { exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Recurring timer status task", { exact: true })).toBeVisible();

  await page.keyboard.press("Alt+S");

  await expect.poll(() => occurrenceBodies.length).toBe(1);
  expect(occurrenceBodies[0]).toMatchObject({
    occurrence_id: "occ-1",
    occurrence_start_at: OCCURRENCE_START,
    status: "in_progress",
  });
  await expect(dialog.getByText("進行中").first()).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Stop", exact: true })).toBeVisible();
});
