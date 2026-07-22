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

async function mockTimelineApis(
  page: Page,
  options: { liveTransition?: boolean; richOperations?: boolean } = {},
) {
  let liveRunFetchCount = 0;
  const messages = options.liveTransition
    ? timelineMessages.slice(0, 1)
    : timelineMessages;
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
          messages,
        },
      });
      return;
    }

    if (url.pathname === `/api/conversations/${SESSION_ID}/messages`) {
      await route.fulfill({
        json: {
          messages,
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
          running: Boolean(options.liveTransition),
          status: options.liveTransition ? "generating" : "idle",
          message: null,
          active_tool: null,
          agent_run_id: options.liveTransition ? AGENT_RUN_ID : null,
        },
      });
      return;
    }

    if (url.pathname === `/api/python-proxy/agent-runs/${AGENT_RUN_ID}`) {
      if (options.richOperations) {
        await route.fulfill({
          json: {
            success: true,
            agent_run: {
              id: AGENT_RUN_ID,
              status: "succeeded",
              timeline: [
                {
                  id: "weather-simple",
                  source: "tool_call",
                  run_id: AGENT_RUN_ID,
                  event_type: "tool_call",
                  status: "succeeded",
                  display_status: "succeeded",
                  tool_name: "get_weather",
                  action: "天気を確認",
                  arguments: { location: "東京" },
                  result: "晴れ 30℃",
                  success: true,
                },
                {
                  id: "files-edit",
                  source: "tool_call",
                  run_id: AGENT_RUN_ID,
                  event_type: "tool_call",
                  status: "succeeded",
                  display_status: "succeeded",
                  tool_name: "write_file",
                  action: "ファイルを編集",
                  arguments: { paths: ["src/a.py", "tests/test_a.py"] },
                  result: "+ updated",
                  success: true,
                },
              ],
            },
          },
        });
        return;
      }
      if (options.liveTransition) {
        const completed = liveRunFetchCount > 1;
        liveRunFetchCount += 1;
        await route.fulfill({
          json: {
            success: true,
            agent_run: {
              id: AGENT_RUN_ID,
              status: completed ? "succeeded" : "running",
              error: null,
              timeline: [
                {
                  id: "operation:tool:stable-start-id",
                  source: completed ? "tool_call" : "event",
                  run_id: AGENT_RUN_ID,
                  event_type: completed ? "tool_call" : "tool_operation",
                  status: completed ? "succeeded" : "running",
                  display_status: completed ? "succeeded" : "started",
                  tool_name: "web_search",
                  action: "Webを検索",
                  arguments: { query: "ライブ更新" },
                  result: completed ? "検索完了" : null,
                  success: completed ? true : null,
                  duration_ms: completed ? 1250 : null,
                  model: "gpt-5.6-sol",
                },
              ],
            },
          },
        });
        return;
      }
      await route.fulfill({
        json: {
          success: true,
          agent_run: {
            id: AGENT_RUN_ID,
            status: "succeeded",
            error: null,
            started_at: "2026-06-29T07:00:00.000Z",
            ended_at: "2026-06-29T07:00:35.000Z",
            timeline: [
              {
                id: "tool-1",
                source: "tool_call",
                run_id: AGENT_RUN_ID,
                tool_name: "web_search",
                action: "検索を実行",
                arguments: { query: "AoiTalk 使い方" },
                result: "検索結果を3件取得",
                success: true,
                duration_ms: 4000,
                model: "gpt-5.6-sol",
                created_at: "2026-06-29T07:00:10.000Z",
                started_at: "2026-06-29T07:00:10.000Z",
                ended_at: "2026-06-29T07:00:14.000Z",
              },
              {
                // ライフサイクル行（新 UI では非表示）
                id: "event-1",
                source: "event",
                run_id: AGENT_RUN_ID,
                event_type: "run.succeeded",
                status: "succeeded",
                display_status: "succeeded",
                actor_type: "assistant",
                actor_label: "メインエージェント",
                action: "応答生成を完了",
                created_at: "2026-06-29T07:00:35.000Z",
              },
            ],
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
            error: "エージェント実行に失敗しました",
            started_at: "2026-06-29T07:00:00.000Z",
            ended_at: "2026-06-29T07:00:05.000Z",
            timeline: [
              {
                id: "tool-failed",
                source: "tool_call",
                run_id: "run-failed",
                tool_name: "web_fetch",
                action: "URL を取得",
                arguments: { url: "https://example.com/broken" },
                success: false,
                created_at: "2026-06-29T07:00:02.000Z",
              },
              {
                // ライフサイクル行（新 UI では非表示）
                id: "event-failed",
                source: "event",
                run_id: "run-failed",
                event_type: "run.failed",
                status: "failed",
                display_status: "failed",
                actor_type: "assistant",
                actor_label: "メインエージェント",
                action: "応答生成に失敗",
                created_at: "2026-06-29T07:00:05.000Z",
              },
            ],
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

  // 合計時間を重複表示せず、操作件数と失敗状態をヘッダにする
  const successToggle = page.getByRole("button", {
    name: /実行済み 1件の操作/,
  });
  const failedToggle = page.getByRole("button", { name: /実行に失敗/ });
  await expect(successToggle).toHaveCount(1);
  await expect(failedToggle).toHaveCount(1);
  await expect(page.getByText(/作業しました/)).toHaveCount(0);

  // 履歴（保存済み）は初期折りたたみ
  await expect(successToggle).toHaveAttribute("aria-expanded", "false");
  await expect(failedToggle).toHaveAttribute("aria-expanded", "false");

  // ライフサイクル行（run.succeeded / run.failed の action）は一切表示しない
  await expect(page.getByText("応答生成を完了")).toHaveCount(0);
  await expect(page.getByText("応答生成に失敗")).toHaveCount(0);
  // 折りたたみ中はログ本体も非表示
  await expect(page.getByText("AoiTalk 使い方", { exact: true })).toHaveCount(
    0,
  );

  // 成功 run を展開するとツール実行行（1 列）が見え、ライフサイクル行は出ない
  await successToggle.click();
  await expect(successToggle).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText("AoiTalk 使い方", { exact: true })).toBeVisible();
  await expect(page.getByText("4.0秒 · gpt-5.6-sol")).toBeVisible();
  await expect(page.getByText("応答生成を完了")).toHaveCount(0);

  // ログは最終回答の角丸カードの外側にある
  const answerCard = page
    .getByText("Agent finished")
    .locator("xpath=ancestor::div[contains(@class,'rounded-2xl')][1]");
  await expect(
    answerCard.getByRole("button", { name: /実行済み/ }),
  ).toHaveCount(0);

  // 失敗 run を展開するとエラーメッセージが見える
  await failedToggle.click();
  await expect(failedToggle).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText("エージェント実行に失敗しました")).toBeVisible();
  await expect(page.getByText("応答生成に失敗")).toHaveCount(0);
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
  const scrollTopBefore = await messageList.evaluate(
    (element) => element.scrollTop,
  );

  await page.evaluate(() => {
    const button = Array.from(document.querySelectorAll("button")).find(
      (item) => item.textContent?.includes("実行済み"),
    );
    button?.click();
  });
  await expect(
    page.getByText("AoiTalk 使い方", { exact: true }),
  ).toBeAttached();
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
    const button = Array.from(document.querySelectorAll("button")).find(
      (item) => item.textContent?.includes("実行済み"),
    );
    button?.click();
  });
  await expect(
    page.getByText("AoiTalk 使い方", { exact: true }),
  ).toBeAttached();
  await expect
    .poll(() =>
      messageList.evaluate(
        (element) =>
          element.scrollHeight - element.scrollTop - element.clientHeight,
      ),
    )
    .toBeLessThanOrEqual(48);
});

test("work log remains readable on a narrow screen", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await addAuthCookie(page);
  await mockTimelineApis(page);

  await page.goto(`/chat?s=${SESSION_ID}`);
  const log = page.getByTestId("agent-run-work-log").first();
  await expect(log).toBeVisible();
  await log.getByRole("button", { name: /実行済み/ }).click();

  const operation = log.locator("summary").first();
  await expect(operation).toContainText("検索を実行");
  await expect(operation).toContainText("4.0秒 · gpt-5.6-sol");
  await operation.click();
  await expect(log.getByText("入力")).toBeVisible();

  const box = await log.boundingBox();
  expect(box).not.toBeNull();
  expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(390);
});

test("live operation updates the same row when it completes", async ({
  page,
}) => {
  await addAuthCookie(page);
  await mockTimelineApis(page, { liveTransition: true });

  await page.goto(`/chat?s=${SESSION_ID}`);
  const log = page.getByTestId("agent-run-work-log");
  await expect(log).toHaveCount(1);
  await expect(
    log.getByRole("button", { name: /実行中 1件の操作/ }),
  ).toBeVisible();
  await expect(log.getByText("Webを検索", { exact: true })).toHaveCount(1);

  const completedToggle = log.getByRole("button", {
    name: /実行済み 1件の操作/,
  });
  await expect(completedToggle).toBeVisible({ timeout: 7_000 });
  await expect(log.getByText("Webを検索", { exact: true })).toHaveCount(0);
  await completedToggle.click();
  await expect(log.getByText("Webを検索", { exact: true })).toHaveCount(1);
  await expect(log.getByText("1.3秒 · gpt-5.6-sol")).toBeVisible();
  await expect(page.getByText(/実行開始|実行完了/)).toHaveCount(0);
});

test("simple results and changed file counts stay visible", async ({
  page,
}) => {
  await addAuthCookie(page);
  await mockTimelineApis(page, { richOperations: true });

  await page.goto(`/chat?s=${SESSION_ID}`);
  const log = page.getByTestId("agent-run-work-log").first();
  const toggle = log.getByRole("button", {
    name: /実行済み 2個のファイル、1件の操作/,
  });
  await expect(toggle).toBeVisible();
  await toggle.click();

  await expect(log.getByText("東京 → 晴れ 30℃", { exact: true })).toBeVisible();
  await expect(log.getByText("成功", { exact: true })).toHaveCount(2);
  await log.locator("summary").click();
  await expect(
    log.getByText("src/a.py\ntests/test_a.py", { exact: true }),
  ).toBeVisible();
});
