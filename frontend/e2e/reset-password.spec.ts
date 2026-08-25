import { expect, test } from "@playwright/test";

test.describe("パスワード再設定", () => {
  test("token付き画面からmock APIへ送信し成功状態を表示する", async ({ page }) => {
    let received: Record<string, unknown> | null = null;
    await page.route("**/api/auth/reset-password", async (route) => {
      received = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ success: true, session_version: 4 }),
      });
    });

    await page.goto("/reset-password?token=reset-token-smoke");
    await page.getByLabel("新しいパスワード").fill("new-password-123");
    await page.getByLabel("確認").fill("new-password-123");
    await page.getByRole("button", { name: "パスワードを更新", exact: true }).click();

    await expect(
      page.getByText("パスワードを更新しました。新しいパスワードでログインできます。"),
    ).toBeVisible();
    expect(received).toEqual({ token: "reset-token-smoke", password: "new-password-123" });
  });

  test("未認証の保護ページはログインへリダイレクトする", async ({ page }) => {
    await page.goto("/tasks");
    await expect(page).toHaveURL(/\/login(?:\?|$)/);
  });
});
