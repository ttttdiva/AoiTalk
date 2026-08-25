import { expect, test } from "@playwright/test";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const baseTask = {
  project_id: "project-1",
  project_name: "Project One",
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
  created_at: "2026-07-31T00:00:00.000Z",
  updated_at: "2026-07-31T00:00:00.000Z",
  metadata: {},
  assignees: [],
  tags: [],
  active_time_entry: null,
  estimated_hours: null,
  sort_order: 0,
  total_time_seconds: 0,
  parent_task_id: null,
  subtasks: [],
  comments: [],
  activities: [],
  has_recurrence: false,
};

const taskA = { ...baseTask, id: "task-1", title: "関連元タスク" };
const taskB = { ...baseTask, id: "task-2", title: "関連先タスク" };

test("Referencesで関連付けたタスクを双方から開ける", async ({ page }) => {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);

  const references = new Map<string, Array<Record<string, unknown>>>([
    [taskA.id, []],
    [taskB.id, []],
  ]);
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();

    if (url.pathname === "/api/projects") {
      await route.fulfill({
        json: {
          projects: [
            {
              id: "project-1",
              name: "Project One",
              slug: "project-one",
              space_id: null,
              is_completed: false,
            },
          ],
          total: 1,
        },
      });
      return;
    }
    if (url.pathname === "/api/tasks" && method === "GET") {
      await route.fulfill({ json: [taskA, taskB] });
      return;
    }
    const taskMatch = url.pathname.match(/^\/api\/tasks\/(task-[12])$/);
    if (taskMatch && method === "GET") {
      await route.fulfill({
        json: taskMatch[1] === taskA.id ? taskA : taskB,
      });
      return;
    }
    const referenceMatch = url.pathname.match(
      /^\/api\/tasks\/(task-[12])\/references$/,
    );
    if (referenceMatch && method === "GET") {
      await route.fulfill({ json: references.get(referenceMatch[1]) ?? [] });
      return;
    }
    if (referenceMatch && method === "POST") {
      const sourceId = referenceMatch[1];
      const body = route.request().postDataJSON() as { target_id: string };
      const targetId = body.target_id;
      const sourceTask = sourceId === taskA.id ? taskA : taskB;
      const targetTask = targetId === taskA.id ? taskA : taskB;
      const relationId = "task-relation:relation-1";
      const referenceFor = (
        target: typeof taskA,
      ): Record<string, unknown> => ({
        id: relationId,
        reference_type: "task",
        relation_type: "related",
        display_name: target.title,
        subtitle: `${target.project_name} · ${target.status}`,
        target_id: target.id,
        target_path: null,
        target_url: null,
        metadata: {},
        created_by: "user-1",
        created_at: "2026-07-31T00:00:00.000Z",
        can_remove: true,
        exists: true,
        open: {
          id: target.id,
          path: `/tasks?detail=${target.id}`,
          url: null,
        },
      });
      references.set(sourceId, [referenceFor(targetTask)]);
      references.set(targetId, [referenceFor(sourceTask)]);
      await route.fulfill({ status: 201, json: referenceFor(targetTask) });
      return;
    }
    if (/^\/api\/tasks\/task-[12]\/attachments$/.test(url.pathname)) {
      await route.fulfill({ json: [] });
      return;
    }
    if (/^\/api\/tasks\/task-[12]\/recurrence$/.test(url.pathname)) {
      await route.fulfill({ json: null });
      return;
    }
    if (url.pathname === "/api/projects/project-1/tags") {
      await route.fulfill({ json: [] });
      return;
    }

    await route.fallback();
  });

  await page.goto("/tasks");
  await page.getByText(taskA.title, { exact: true }).click();

  await page.getByRole("button", { name: "参照を追加" }).click();
  await page.getByRole("menuitem", { name: "タスクを関連付け" }).click();
  await page
    .getByPlaceholder("タスク名・プロジェクト名で検索")
    .fill("関連先");
  await page.getByRole("button", { name: /関連先タスク/ }).click();

  const relatedTaskButton = page
    .getByRole("dialog")
    .getByRole("button", { name: /関連先タスク/ });
  await expect(relatedTaskButton).toBeVisible();
  await relatedTaskButton.click();
  await expect(page.getByRole("dialog")).toContainText(taskB.title);
  await expect(
    page.getByRole("dialog").getByText(taskA.title, { exact: true }),
  ).toBeVisible();
});
