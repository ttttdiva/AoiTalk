import { expect, test } from "@playwright/test";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const task = {
  id: "task-1",
  project_id: "project-1",
  project_name: "Project One",
  title: "ファイル参照を追加するタスク",
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

test("タスク詳細モーダルへドロップしたファイルをReferencesへ追加する", async ({
  page,
}) => {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);

  let uploadBody = "";
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
      await route.fulfill({ json: [task] });
      return;
    }
    if (url.pathname === "/api/tasks/task-1" && method === "GET") {
      await route.fulfill({ json: task });
      return;
    }
    if (url.pathname === "/api/tasks/task-1/attachments" && method === "GET") {
      await route.fulfill({ json: [] });
      return;
    }
    if (url.pathname === "/api/tasks/task-1/attachments" && method === "POST") {
      uploadBody = route.request().postDataBuffer()?.toString("utf8") ?? "";
      await route.fulfill({
        status: 201,
        json: {
          id: "attachment-1",
          task_id: task.id,
          project_id: task.project_id,
          file_path: "attachments/tasks/task-1/dropped-reference.txt",
          display_name: "dropped-reference.txt",
          mime_type: "text/plain",
          size_bytes: 9,
          kind: "file",
          created_by: "user-1",
          created_at: "2026-07-31T00:00:00.000Z",
          metadata: {},
          url: "/api/tasks/task-1/attachments/attachment-1",
        },
      });
      return;
    }
    if (url.pathname === "/api/tasks/task-1/references" && method === "GET") {
      await route.fulfill({ json: [] });
      return;
    }
    if (url.pathname === "/api/tasks/task-1/recurrence") {
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
  await page.getByText(task.title, { exact: true }).click();

  const dialog = page.getByRole("dialog");
  await expect(dialog).toHaveCSS("position", "fixed");
  const dataTransfer = await page.evaluateHandle(() => {
    const transfer = new DataTransfer();
    transfer.items.add(
      new File(["reference"], "dropped-reference.txt", {
        type: "text/plain",
      }),
    );
    return transfer;
  });

  await dialog.dispatchEvent("dragenter", { dataTransfer });
  await expect(
    page.getByText("ファイルをドロップしてリファレンスに追加"),
  ).toBeVisible();
  await dialog.dispatchEvent("drop", { dataTransfer });

  await expect(page.getByText("dropped-reference.txt")).toBeVisible();
  expect(uploadBody).toContain('name="file"');
  expect(uploadBody).toContain('filename="dropped-reference.txt"');
});
