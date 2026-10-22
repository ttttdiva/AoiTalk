import { expect, test } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const sessions = [
  {
    id: "history-ready",
    user_id: "user-1",
    title: "準備完了の通常Chat",
    character_name: "aoi",
    project_id: "project-a",
    history_status: "ready",
    history_completed_at: "2026-10-17T08:00:00.000Z",
    is_unread: true,
    message_count: 2,
    is_active: true,
  },
  {
    id: "history-working",
    user_id: "user-1",
    title: "処理中のApp",
    character_name: "aoi",
    app_id: "app-1",
    app_target_id: "target-1",
    history_status: "working",
    message_count: 2,
    is_active: true,
  },
  {
    id: "history-idle",
    user_id: "user-1",
    title: "待機中のChat",
    character_name: "aoi",
    history_status: "idle",
    message_count: 1,
    is_active: true,
  },
];

async function installHistoryFixture(page: import("@playwright/test").Page) {
  await addAuthCookie(page, "user-1");
  await mockAuthenticatedApis(page);

  // Route handlers are evaluated newest first, so these scoped fixtures take
  // precedence over the generic authenticated fixture above.
  await page.route("**/api/projects**", async (route) => {
    await route.fulfill({
      json: {
        projects: [{ id: "project-a", name: "QAプロジェクト", slug: "qa-project" }],
        total: 1,
      },
    });
  });
  await page.route("**/api/conversations/search**", async (route) => {
    await route.fulfill({
      json: {
        results: [
          {
            id: "message:history-ready",
            match_type: "message",
            session_id: "history-ready",
            title: "検索の準備完了",
            character_name: "aoi",
            snippet: "検索語",
            history_status: "ready",
          },
          {
            id: "message:history-working",
            match_type: "message",
            session_id: "history-working",
            title: "検索の処理中",
            character_name: "aoi",
            snippet: "検索語",
            history_status: "working",
          },
        ],
        total: 2,
      },
    });
  });
  await page.route("**/api/conversations", async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    await route.fulfill({ json: { conversations: sessions, total: sessions.length } });
  });
}

test.describe("Chat history status filter", () => {
  test.beforeEach(async ({ page }) => {
    await installHistoryFixture(page);
    await page.goto("/chat");
    await expect(page.getByText("準備完了の通常Chat")).toBeVisible();
  });

  test("filters timeline/project rows, recovers from zero results, and reuses the filter in search", async ({ page }) => {
    const filter = page.getByTestId("chat-history-status-filter");
    await filter.click();
    await expect(page.getByRole("menuitemradio")).toHaveCount(4);
    await expect(page.getByRole("menuitemradio", { name: "準備完了" })).toBeVisible();
    await expect(page.getByRole("menuitemradio", { name: "要確認" })).toBeVisible();
    await expect(page.getByRole("menuitemradio", { name: "処理中" })).toBeVisible();

    await page.getByRole("menuitemradio", { name: "処理中" }).click();
    await expect(page.getByText("処理中のApp")).toBeVisible();
    await expect(page.getByText("準備完了の通常Chat")).toHaveCount(0);

    await page.getByRole("button", { name: "表示方法" }).click();
    await page.getByRole("menuitemradio", { name: "プロジェクト別" }).click();
    await expect(page.getByText("処理中のApp")).toBeVisible();

    await filter.click();
    await page.getByRole("menuitemradio", { name: "要確認" }).click();
    await expect(page.getByText("この状態の会話はありません")).toBeVisible();
    await filter.click();
    await expect(page.getByRole("menuitemradio", { name: "要確認" })).toHaveAttribute("aria-checked", "true");
    await page.getByRole("menuitemradio", { name: "処理中" }).click();
    await expect(page.getByText("処理中のApp")).toBeVisible();

    await page.keyboard.press("Control+f");
    const searchInput = page.getByPlaceholder("会話履歴を検索...");
    await expect(searchInput).toBeVisible();
    await searchInput.fill("検索語");
    await expect(page.getByText("検索の処理中")).toBeVisible();
    await expect(page.getByText("検索の準備完了")).toHaveCount(0);
  });
});
