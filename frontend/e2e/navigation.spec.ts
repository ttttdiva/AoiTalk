import { test, expect } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

test.describe("ナビゲーション", () => {
  test("ルート（/）にアクセスすると /chat にリダイレクトされる", async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.goto("/");
    await expect(page).toHaveURL(/\/chat/);
  });

  test("ヘッダーのナビタブで各ページに遷移できる", async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.goto("/chat");

    // ヘッダー内のナビタブ（Link）で遷移
    const navTabs = [
      { label: "タスク", path: "/tasks" },
      { label: "カレンダー", path: "/calendar" },
      { label: "レポート", path: "/reports" },
      { label: "ファイラー", path: "/filer" },
      { label: "設定", path: "/settings" },
      { label: "チャット", path: "/chat" },
    ];

    for (const { label, path } of navTabs) {
      const link = page.locator(`header nav a`).filter({ hasText: label });
      await expect(link).toBeVisible();
      await link.click();
      await expect(page).toHaveURL(new RegExp(path));
    }
  });

  test("サイドバーにナビリンクが表示される", async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.goto("/chat");

    // サイドバー内のナビリンク
    const sidebarLinks = ["チャット", "タスク"];
    for (const label of sidebarLinks) {
      const link = page.locator(`aside a, [data-sidebar] a`).filter({ hasText: label });
      await expect(link.first()).toBeVisible();
    }
  });
});
