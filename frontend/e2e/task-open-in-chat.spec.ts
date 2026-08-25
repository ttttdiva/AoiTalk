import { expect, test } from "@playwright/test";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const task = {
  id: "task-1",
  project_id: "project-1",
  project_name: "Project One",
  title: "送信前に確認するタスク",
  description: "チャット入力欄へ引き継ぐ説明",
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
  created_at: "2026-07-30T00:00:00.000Z",
  updated_at: "2026-07-30T00:00:00.000Z",
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

test("Open in Chat links the task and sends the handed-off draft", async ({
  page,
}) => {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);

  let dispatchCount = 0;
  let dispatchPayload: Record<string, unknown> | null = null;
  let referencePayload: Record<string, unknown> | null = null;
  let persistedMessages: Array<Record<string, unknown>> = [];
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();

    if (url.pathname === "/api/spaces") {
      await route.fulfill({
        json: {
          spaces: [
            {
              id: "space-1",
              name: "Workspace One",
              slug: "workspace-one",
              description: null,
              color: "#64748b",
            },
          ],
          total: 1,
        },
      });
      return;
    }
    if (url.pathname === "/api/projects") {
      await route.fulfill({
        json: {
          projects: [
            {
              id: "project-1",
              name: "Project One",
              slug: "project-one",
              space_id: "space-1",
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
    if (
      url.pathname === "/api/tasks/task-1/attachments" &&
      method === "GET"
    ) {
      await route.fulfill({ json: [] });
      return;
    }
    if (
      url.pathname === "/api/tasks/task-1/references" &&
      method === "GET"
    ) {
      await route.fulfill({ json: [] });
      return;
    }
    if (
      url.pathname === "/api/tasks/task-1/references" &&
      method === "POST"
    ) {
      referencePayload = route.request().postDataJSON() as Record<
        string,
        unknown
      >;
      await route.fulfill({
        status: 201,
        json: {
          id: "reference-1",
          ...referencePayload,
          relation_type: "related",
          can_remove: true,
          exists: true,
          open: { path: "/chat?s=session-e2e" },
        },
      });
      return;
    }
    if (
      url.pathname === "/api/conversations/session-e2e/messages" &&
      method === "GET"
    ) {
      await route.fulfill({
        json: {
          messages: persistedMessages,
          server_time: "2026-08-13T00:00:00Z",
        },
      });
      return;
    }
    if (
      url.pathname ===
        "/api/python-proxy/conversations/session-e2e/generation/status" &&
      method === "GET"
    ) {
      await route.fulfill({
        json: {
          session_id: "session-e2e",
          running: false,
          status: "idle",
          message: null,
        },
      });
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
    if (url.pathname === "/api/projects/project-1/assignee-candidates") {
      await route.fulfill({ json: { members: [], total: 0 } });
      return;
    }
    if (url.pathname === "/api/python-proxy/characters") {
      await route.fulfill({
        json: { current: "aoi", characters: ["aoi"] },
      });
      return;
    }
    if (
      url.pathname ===
      "/api/python-proxy/conversations/session-e2e/dispatch"
    ) {
      dispatchPayload = route.request().postDataJSON() as Record<
        string,
        unknown
      >;
      dispatchCount += 1;
      const content = String(dispatchPayload.message ?? "");
      const clientMessageId = String(
        dispatchPayload.client_message_id ?? "client-e2e",
      );
      persistedMessages = [
        {
          id: "message-user-e2e",
          session_id: "session-e2e",
          role: "user",
          content,
          metadata: { client_message_id: clientMessageId },
          branch_index: 0,
          is_active_branch: true,
        },
        {
          id: "message-assistant-e2e",
          session_id: "session-e2e",
          role: "assistant",
          content: "タスクの確認が完了しました。",
          metadata: {
            agent_run_id: "run-e2e",
            generation_status: "completed",
          },
          branch_index: 0,
          is_active_branch: true,
        },
      ];
      await route.fulfill({
        json: {
          success: true,
          queued: true,
          session_id: "session-e2e",
          user_message_id: "message-user-e2e",
          agent_run_id: "run-e2e",
        },
      });
      return;
    }

    await route.fallback();
  });

  await page.goto("/tasks");
  await page
    .getByTestId("task-row-task-1")
    .getByText(task.title, { exact: true })
    .click();
  await page.getByRole("button", { name: "Open in Chat" }).click();

  await expect(page).toHaveURL(/\/chat\?s=session-e2e$/);
  const composerTextBlock = page.locator(
    'textarea[aria-label="メッセージ入力"]',
  ).first();
  await expect(composerTextBlock).toHaveValue(
    /送信前に確認するタスク/,
  );
  expect(referencePayload).toMatchObject({
    reference_type: "conversation_session",
    relation_type: "related",
    target_id: "session-e2e",
  });
  await expect(page.getByRole("button", { name: "送信", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "送信", exact: true }).click();
  await expect.poll(() => dispatchCount).toBe(1);
  expect(dispatchPayload).toMatchObject({
    message: expect.stringContaining("送信前に確認するタスク"),
    generation_profile: "assisted_work",
  });
  await expect(page.getByText("タスクの確認が完了しました。")).toBeVisible();
});
