import { test, expect } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

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
