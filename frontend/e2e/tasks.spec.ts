import { test, expect } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

test.describe("タスクページ", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.goto("/tasks");
  });

  test("タスクページが表示される", async ({ page }) => {
    // ヘッダーにタイトル「タスク」が表示される
    const heading = page.locator("header h1");
    await expect(heading).toHaveText("タスク");
  });

  test("フィルタタブが表示される", async ({ page }) => {
    // exact: true で「完了」と「未完了」を区別
    await expect(page.getByRole("tab", { name: "全て" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "未完了" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "今日" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "期限超過" })).toBeVisible();
    await expect(
      page.getByRole("tab", { name: "完了", exact: true })
    ).toBeVisible();
  });

  test("検索バーが存在する", async ({ page }) => {
    // API未接続でもプロジェクト非依存の検索バーは表示される
    await expect(page.getByPlaceholder("タスクを検索")).toBeVisible();
  });
});
