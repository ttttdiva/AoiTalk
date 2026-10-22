import { test, expect, type Page } from "@playwright/test";
import { addAuthCookie, E2E_USER_ID, mockAuthenticatedApis } from "./support/auth";
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
  await page.route("**/api/conversations/session-e2e/resume**", (route) => route.fulfill({
    json: {
      session: {
        id: "session-e2e",
        user_id: E2E_USER_ID,
        title: "E2E conversation",
        character_name: "aoi",
        message_count: 0,
        is_active: true,
        is_group_chat: false,
        group_character_names: [],
      },
      messages: [],
    },
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
    await page.route("**/api/python-proxy/agent-team/config", (route) => route.fulfill({
      json: { teams: {} },
    }));
    await page.route("**/api/python-proxy/llm/execution-profiles", (route) => route.fulfill({
      json: { active_profile_id: "manual", profiles: [], effective_main: { provider: "mock", model: "mock-model" } },
    }));
    await page.route("**/api/python-proxy/llm/new-chat-defaults", (route) => route.fulfill({
      json: {
        last_used_main: { provider: "mock", model: "mock-model" },
        effective_main: { provider: "mock", model: "mock-model" },
      },
    }));
    await page.goto("/chat");
  });

  test("チャットUIが表示される", async ({ page }) => {
    // ヘッダーのナビタブで「チャット」がアクティブ
    const chatTab = page
      .getByRole("navigation", { name: "Workspace" })
      .getByRole("link", { name: "チャット", exact: true });
    await expect(chatTab).toBeVisible();
  });

  test("Context Railをヘッダーの同じアイコンで閉じて戻し、保存値と本文を保持する", async ({ page }) => {
    await openStableChatSession(page);

    const toggle = page.getByTestId("chat-context-rail-toggle");
    const rail = page.locator('[data-shell-region="context-rail"]');
    const composer = page.getByRole("textbox", { name: "メッセージ入力", exact: true });
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(rail).toBeVisible();
    await composer.fill("rail-toggle-draft");
    const originalRail = await rail.elementHandle();
    const initialMainWidth = (await page.locator('[data-shell-region="main-canvas"]').boundingBox())!.width;

    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(rail).toBeHidden();
    expect((await page.locator('[data-shell-region="main-canvas"]').boundingBox())!.width).toBeGreaterThan(initialMainWidth + 200);
    await expect(composer).toHaveValue("rail-toggle-draft");
    expect(await originalRail!.evaluate((element) => element.isConnected)).toBe(true);

    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(rail).toBeVisible();
    expect(await originalRail!.evaluate((element) => element.isConnected)).toBe(true);
    expect(await page.evaluate((key) => localStorage.getItem(key), `aoitalk:context-rail-visible:${E2E_USER_ID}`)).toBe("true");

    await toggle.click();
    await expect(rail).toBeHidden();
    await page.reload();
    await expect(page.getByTestId("chat-context-rail-toggle")).toHaveAttribute("aria-expanded", "false");
    await expect(page.locator('[data-shell-region="context-rail"]')).toBeHidden();
    await expect(page.getByRole("textbox", { name: "メッセージ入力", exact: true })).toHaveValue("rail-toggle-draft");
  });

  test("サイドバーに新規会話ボタンが表示される", async ({ page }) => {
    // アプリサイドバー内に新規会話ボタンがある
    const newChatBtn = page.getByRole("button", { name: "新規会話", exact: true });
    await expect(newChatBtn).toBeVisible();
  });

  test.describe("Context Rail 768px境界", () => {
    test.use({ viewport: { width: 768, height: 995 } });

    test("通常railのtoggleが狭幅Sheetの導線と重ならず本文をはみ出させない", async ({ page }) => {
      await openStableChatSession(page);
      const toggle = page.getByTestId("chat-context-rail-toggle");
      const rail = page.locator('[data-shell-region="context-rail"]');
      await expect(toggle).toBeVisible();
      await expect(page.getByRole("button", { name: "関連情報を開く", exact: true })).toHaveCount(0);
      await expect(rail).toBeVisible();
      const agentSelector = page.getByTestId("chat-agent-selector");
      const displayGroup = page.getByRole("group", { name: "チャットの表示", exact: true });
      await expect(agentSelector).toBeVisible();
      await expect(displayGroup).toBeVisible();
      const agentBox = await agentSelector.boundingBox();
      const displayBox = await displayGroup.boundingBox();
      const toggleBox = await toggle.boundingBox();
      if (!agentBox || !displayBox || !toggleBox) {
        throw new Error("Context Rail toolbar controls were not measurable at 768px");
      }
      expect(agentBox.width).toBeGreaterThan(0);
      expect(displayBox.width).toBeGreaterThan(0);
      const viewportWidth = page.viewportSize()?.width ?? 768;
      expect(toggleBox.x).toBeGreaterThanOrEqual(0);
      expect(toggleBox.x + toggleBox.width).toBeLessThanOrEqual(viewportWidth + 1);
      for (const [left, right] of [[agentBox, displayBox], [agentBox, toggleBox], [displayBox, toggleBox]] as const) {
        const separateX = left.x + left.width <= right.x + 1 || right.x + right.width <= left.x + 1;
        const separateY = left.y + left.height <= right.y + 1 || right.y + right.height <= left.y + 1;
        expect(separateX || separateY).toBe(true);
      }

      await toggle.click();
      await expect(rail).toBeHidden();
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(768);

      await toggle.click();
      await expect(rail).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(768);
    });

    test("動的な390/767/768リサイズでもmobile Sheetのcloseを1つに保つ", async ({ page }) => {
      await openStableChatSession(page);
      const toggle = page.getByTestId("chat-context-rail-toggle");
      const mobileTrigger = page.getByRole("button", { name: "関連情報を開く", exact: true });
      const sheet = page.locator('[data-slot="sheet-content"]');
      const sheetClose = page.getByRole("button", { name: "Context Railを閉じる", exact: true });

      for (const width of [390, 767]) {
        await page.setViewportSize({ width, height: 995 });
        await expect(toggle).toHaveCount(0);
        await expect(mobileTrigger).toHaveCount(1);
        await mobileTrigger.click();
        await expect(sheet).toBeVisible();
        await expect(sheetClose).toHaveCount(1);
        await expect(sheet.locator('[data-slot="sheet-close"]')).toHaveCount(0);
        await sheetClose.click();
        await expect(sheet).toBeHidden();
        expect(await page.evaluate((viewportWidth) => document.documentElement.scrollWidth <= viewportWidth, width)).toBe(true);
      }

      await page.setViewportSize({ width: 768, height: 995 });
      await expect(mobileTrigger).toHaveCount(0);
      await expect(toggle).toBeVisible();
    });
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
    const railToggle = page.getByTestId("chat-context-rail-toggle");
    await railToggle.click();
    await expect(page.locator('[data-shell-region="context-rail"]')).toBeHidden();
    await railToggle.click();
    await expect(page.locator('[data-shell-region="context-rail"]')).toBeVisible();
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
