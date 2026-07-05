import { expect, test, type Page } from "@playwright/test";

import { addAuthCookie } from "./support/auth";

const SESSION_ID = "session-timeline";
const AGENT_RUN_ID = "run-timeline";

const fillerMessages = Array.from({ length: 28 }, (_, index) => ({
  id: `msg-filler-${index}`,
  session_id: SESSION_ID,
  role: index % 2 === 0 ? "user" : "assistant",
  content: `スクロール確認用メッセージ ${index + 1}\n${"本文 ".repeat(24)}`,
  metadata: {},
  parent_message_id: null,
  branch_index: 0,
  is_active_branch: true,
}));

const timelineMessages = [
  {
    id: "msg-user",
    session_id: SESSION_ID,
    role: "user",
    content: "Run an agent",
    metadata: {},
    parent_message_id: null,
    branch_index: 0,
    is_active_branch: true,
  },
  {
    id: "msg-assistant",
    session_id: SESSION_ID,
    role: "assistant",
    content: "Agent finished",
    metadata: { agent_run_id: AGENT_RUN_ID },
    parent_message_id: null,
    branch_index: 0,
    is_active_branch: true,
  },
  ...fillerMessages,
  {
    id: "msg-failed-assistant",
    session_id: SESSION_ID,
    role: "assistant",
    content: "Agent failed",
    metadata: { agent_run_id: "run-failed" },
    parent_message_id: null,
    branch_index: 0,
    is_active_branch: true,
  },
];

async function mockTimelineApis(page: Page) {
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
      await route.fulfill({ json: { projects: [], total: 0 } });
      return;
    }

    if (url.pathname === "/api/spaces") {
      await route.fulfill({ json: { spaces: [], total: 0 } });
      return;
    }

    if (url.pathname === "/api/python-proxy/health") {
      await route.fulfill({ json: { status: "ok" } });
      return;
    }

    if (url.pathname === "/api/python-proxy/characters") {
      await route.fulfill({ json: { characters: [], current: "" } });
      return;
    }

    if (url.pathname === "/api/python-proxy/llm/engine") {
      await route.fulfill({ json: { available: [], provider: "", model: "" } });
      return;
    }

    if (url.pathname === "/api/python-proxy/llm/mode") {
      await route.fulfill({ json: { mode: "", available_modes: [] } });
      return;
    }

    if (url.pathname === "/api/python-proxy/llm/models") {
      await route.fulfill({
        json: {
          current: { provider: "local", model: "default" },
          providers: [
            {
              id: "local",
              label: "Local",
              configured_model: "default",
              models: [{ id: "default", label: "Default" }],
            },
          ],
        },
      });
      return;
    }

    if (url.pathname === "/api/python-proxy/runtime/features") {
      await route.fulfill({ json: { features: {} } });
      return;
    }

    if (url.pathname === "/api/conversations") {
      await route.fulfill({
        json: {
          conversations: [
            {
              id: SESSION_ID,
              user_id: "user-1",
              title: "Timeline session",
              character_name: "aoi",
              message_count: 2,
              is_active: true,
              is_group_chat: false,
              group_character_names: [],
            },
          ],
          total: 1,
        },
      });
      return;
    }

    if (url.pathname === `/api/conversations/${SESSION_ID}/resume`) {
      await route.fulfill({
        json: {
          session: {
            id: SESSION_ID,
            user_id: "user-1",
            title: "Timeline session",
            character_name: "aoi",
            message_count: 2,
            is_active: true,
            is_group_chat: false,
            group_character_names: [],
          },
          messages: timelineMessages,
        },
      });
      return;
    }

    if (url.pathname === `/api/conversations/${SESSION_ID}/messages`) {
      await route.fulfill({
        json: {
          messages: timelineMessages,
        },
      });
      return;
    }

    if (
      url.pathname ===
      `/api/python-proxy/conversations/${SESSION_ID}/generation/status`
    ) {
      await route.fulfill({
        json: {
          session_id: SESSION_ID,
          running: false,
          status: "idle",
          message: null,
          active_tool: null,
          agent_run_id: null,
        },
      });
      return;
    }

    if (url.pathname === `/api/python-proxy/agent-runs/${AGENT_RUN_ID}`) {
      await route.fulfill({
        json: {
          success: true,
          agent_run: {
            id: AGENT_RUN_ID,
            status: "succeeded",
            error: null,
            timeline: [
              {
                id: "event-1",
                source: "event",
                run_id: AGENT_RUN_ID,
                event_type: "run.succeeded",
                status: "succeeded",
                display_status: "succeeded",
                actor_type: "assistant",
                actor_label: "メインエージェント",
                action: "応答生成を完了",
                created_at: "2026-06-29T07:00:00.000Z",
              },
            ],
            timeline_columns: [],
          },
        },
      });
      return;
    }

    if (url.pathname === "/api/python-proxy/agent-runs/run-failed") {
      await route.fulfill({
        json: {
          success: true,
          agent_run: {
            id: "run-failed",
            status: "failed",
            error: "failed",
            timeline: [
              {
                id: "event-failed",
                source: "event",
                run_id: "run-failed",
                event_type: "run.failed",
                status: "failed",
                display_status: "failed",
                actor_type: "assistant",
                actor_label: "メインエージェント",
                action: "応答生成に失敗",
                created_at: "2026-06-29T07:00:00.000Z",
              },
            ],
            timeline_columns: [],
          },
        },
      });
      return;
    }

    if (url.pathname.includes("/scenarios/logs/by-conversation/")) {
      await route.fulfill({ json: null });
      return;
    }

    await route.fulfill({ json: {} });
  });
}

test("saved agent run timelines start collapsed and expand on demand", async ({
  page,
}) => {
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (
      message.type() === "error" &&
      !message.text().includes("WebSocket connection to")
    ) {
      consoleErrors.push(message.text());
    }
  });

  await addAuthCookie(page);
  await mockTimelineApis(page);

  await page.goto(`/chat?s=${SESSION_ID}`);
  await expect(page.getByText("Agent finished")).toBeVisible();
  await expect(page.getByText("Agent failed")).toBeVisible();

  const timelineToggles = page.getByRole("button", {
    name: /実行タイムライン/,
  });
  await expect(timelineToggles).toHaveCount(2);
  await expect(timelineToggles.nth(0)).toHaveAttribute(
    "aria-expanded",
    "false",
  );
  await expect(timelineToggles.nth(1)).toHaveAttribute(
    "aria-expanded",
    "false",
  );
  await expect(page.getByText("応答生成を完了")).toHaveCount(0);
  await expect(page.getByText("応答生成に失敗")).toHaveCount(0);

  await timelineToggles.nth(0).click();
  await expect(timelineToggles.nth(0)).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText("応答生成を完了")).toBeVisible();

  await timelineToggles.nth(1).click();
  await expect(timelineToggles.nth(1)).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText("応答生成に失敗")).toBeVisible();
  expect(consoleErrors).toEqual([]);
});

test("timeline expansion follows only when the message list is pinned to the bottom", async ({
  page,
}) => {
  await addAuthCookie(page);
  await mockTimelineApis(page);

  await page.goto(`/chat?s=${SESSION_ID}`);
  const messageList = page.getByTestId("chat-message-list");
  await expect(page.getByText("Agent finished")).toBeVisible();
  await expect(messageList).toBeVisible();

  await messageList.evaluate((element) => {
    element.scrollTop = 0;
    element.dispatchEvent(new Event("scroll", { bubbles: true }));
  });
  const scrollTopBefore = await messageList.evaluate((element) => element.scrollTop);

  await page.evaluate(() => {
    const button = Array.from(document.querySelectorAll("button")).find((item) =>
      item.textContent?.includes("実行タイムライン"),
    );
    button?.click();
  });
  await expect(page.getByText("応答生成を完了")).toBeAttached();
  await expect
    .poll(() => messageList.evaluate((element) => element.scrollTop))
    .toBe(scrollTopBefore);

  await page.reload();
  await expect(page.getByText("Agent finished")).toBeVisible();
  await messageList.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
    element.dispatchEvent(new Event("scroll", { bubbles: true }));
  });

  await page.evaluate(() => {
    const button = Array.from(document.querySelectorAll("button")).find((item) =>
      item.textContent?.includes("実行タイムライン"),
    );
    button?.click();
  });
  await expect(page.getByText("応答生成を完了")).toBeAttached();
  await expect
    .poll(() =>
      messageList.evaluate(
        (element) =>
          element.scrollHeight - element.scrollTop - element.clientHeight,
      ),
    )
    .toBeLessThanOrEqual(48);
});
