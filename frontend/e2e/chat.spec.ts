import { test, expect, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";
import type { ConversationMessage } from "../src/lib/chat-api";

async function openStableChatSession(page: Page) {
  await page.route("**/api/python-proxy/characters", (route) => route.fulfill({
    json: { characters: ["aoi"], current: "aoi" },
  }));
  await page.route("**/api/python-proxy/llm/session-settings*", (route) => route.fulfill({
    json: {
      settings: { main_route: { provider: "mock", model: "mock-model" }, special_routing: {} },
      effective_main: { provider: "mock", model: "mock-model" },
    },
  }));
  await page.route("**/api/python-proxy/conversations/session-e2e/generation/status", (route) => route.fulfill({
    json: { session_id: "session-e2e", running: false, status: "idle" },
  }));
  // Socket delivery is outside this fixture; exercise deterministic REST persistence.
  await page.routeWebSocket(/\/ws(?:\?|$)/, (socket) => socket.close());
  await page.goto("/chat?s=session-e2e");
  await expect(page.getByText("この会話にはまだメッセージがありません。", { exact: true })).toBeVisible();
}

test.describe("チャットページ", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.goto("/chat");
  });

  test("チャットUIが表示される", async ({ page }) => {
    // ヘッダーのナビタブで「チャット」がアクティブ
    const chatTab = page
      .getByRole("navigation", { name: "Workspace" })
      .getByRole("link", { name: "チャット", exact: true });
    await expect(chatTab).toBeVisible();
  });

  test("サイドバーに新規会話ボタンが表示される", async ({ page }) => {
    // アプリサイドバー内に新規会話ボタンがある
    const newChatBtn = page.getByRole("button", { name: "新規会話", exact: true });
    await expect(newChatBtn).toBeVisible();
  });

  test("初期状態でローディングまたは案内が表示される", async ({ page }) => {
    // API未接続時は「読み込み中...」、接続時は「会話を選択してください」
    const loading = page.getByText("読み込み中...");
    const guide = page.getByText("会話を選択してください");
    const empty = page.getByText("メッセージを送信して会話を開始しましょう。");
    // いずれかが表示されていればOK
    await expect(loading.or(guide).or(empty)).toBeVisible({ timeout: 10000 });
  });

  test("セッション一覧がサイドバーに統合されている", async ({ page }) => {
    // サイドバー内に「会話履歴」セクションがある
    await expect(page.getByText("History", { exact: true })).toBeVisible({
      timeout: 5000,
    });
  });

  test("送信行は保存確定後も同じDOMを維持する", async ({ page }) => {
    let releaseSave: () => void = () => {};
    const saveGate = new Promise<void>((resolve) => { releaseSave = resolve; });
    let saved: ConversationMessage | null = null;
    await page.route("**/api/conversations/session-e2e/messages", async (route) => {
      await route.fulfill({ json: { messages: saved ? [saved] : [] } });
    });
    await page.route("**/api/python-proxy/conversations/session-e2e/dispatch", async (route) => {
      const body = route.request().postDataJSON();
      await saveGate;
      saved = {
        id: "saved-flicker-check", session_id: "session-e2e",
        role: "user", content: body.message,
        metadata: { client_message_id: body.client_message_id },
        branch_index: 0, is_active_branch: true,
      };
      await route.fulfill({ json: { success: true, queued: false, session_id: "session-e2e" } });
    });
    await openStableChatSession(page);
    const composer = page.getByRole("textbox", { name: "メッセージ入力", exact: true });
    await composer.fill("保存時の描画確認");
    await page.getByRole("button", { name: "送信", exact: true }).click();
    const pending = page.locator('[data-chat-message-id^="temp-user-"]');
    await expect(pending).toBeVisible();
    await pending.evaluate((element) => element.setAttribute("data-stability-probe", "retained"));
    releaseSave();
    await expect(page.locator('[data-chat-message-id="saved-flicker-check"]'))
      .toHaveAttribute("data-stability-probe", "retained");
  });

  test("右側の関連タスクは定期更新中も空状態と位置を維持する", async ({ page }) => {
    let holdRefresh = false;
    let releaseRefresh: () => void = () => {};
    const refreshGate = new Promise<void>((resolve) => { releaseRefresh = resolve; });
    await page.route("**/api/conversations/session-e2e/related-tasks", async (route) => {
      if (holdRefresh) await refreshGate;
      await route.fulfill({ json: { tasks: [] } });
    });
    await openStableChatSession(page);
    const rail = page.getByTestId("chat-context-rail");
    const empty = rail.getByText("このチャットに関連するタスクはありません", { exact: true });
    await expect(empty).toBeVisible();
    const before = await empty.boundingBox();
    holdRefresh = true;
    await page.waitForRequest((request) => request.url().endsWith("/related-tasks"));
    try {
      await expect(empty).toBeVisible();
      await expect(rail.getByText("関連タスクを読み込み中…", { exact: true })).toBeHidden();
      expect(await empty.boundingBox()).toEqual(before);
    } finally {
      releaseRefresh();
    }
  });
});

test.describe("チャット送受信（Python API起動時）", () => {
  // Python APIが起動している場合のみ実行されるテスト
  test.beforeEach(async ({ page }) => {
    // Python APIのヘルスチェック
    const healthRes = await page.request.get("http://127.0.0.1:3000/health");
    test.skip(!healthRes.ok(), "Python APIが起動していないためスキップ");
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.goto("/chat");
  });

  test("新規会話を作成してメッセージを送信できる", async ({ page }) => {
    // 新規会話ボタンをクリック
    const newChatBtn = page.getByRole("button", { name: "新規会話", exact: true });
    await newChatBtn.click();

    // URLに ?s= パラメータが付くのを待つ
    await expect(page).toHaveURL(/\?s=/, { timeout: 5000 });

    // メッセージ入力欄が表示される
    const input = page.locator("textarea, input[type='text']").last();
    await expect(input).toBeVisible({ timeout: 5000 });

    // メッセージを送信
    await input.fill("テスト送信");
    await input.press("Enter");

    // 送信したメッセージが表示される（ユーザーメッセージとして）
    await expect(page.getByText("テスト送信")).toBeVisible({ timeout: 10000 });
  });
});
