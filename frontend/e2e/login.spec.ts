import { test, expect } from "@playwright/test";

test.describe("ログインページ", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/login");
  });

  test("ログインフォームが表示される", async ({ page }) => {
    // カードタイトル「AoiTalk」が表示される
    await expect(page.getByText("AoiTalk")).toBeVisible();
  });

  test("ユーザー名とパスワードのフィールドがある", async ({ page }) => {
    const usernameInput = page.getByLabel("ユーザー名");
    const passwordInput = page.getByLabel("パスワード");

    await expect(usernameInput).toBeVisible();
    await expect(passwordInput).toBeVisible();

    // type属性の確認
    await expect(usernameInput).toHaveAttribute("type", "text");
    await expect(passwordInput).toHaveAttribute("type", "password");
  });

  test("ログインボタンが存在する", async ({ page }) => {
    const loginButton = page.getByRole("button", { name: "ログイン" });
    await expect(loginButton).toBeVisible();
    await expect(loginButton).toBeEnabled();
  });
});
