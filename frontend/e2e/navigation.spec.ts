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

    const tabLabels = (await page.locator("header nav a").allTextContents()).map(
      (item) => item.trim(),
    );
    expect(tabLabels.slice(0, 5)).toEqual([
      "チャット",
      "タスク",
      "カレンダー",
      "Docs",
      "ファイラー",
    ]);

    // ヘッダー内のナビタブ（Link）で遷移
    const navTabs = [
      { label: "タスク", path: "/tasks" },
      { label: "カレンダー", path: "/calendar" },
      { label: "Docs", path: "/docs" },
      { label: "ファイラー", path: "/filer" },
      { label: "プロジェクト", path: "/projects" },
      { label: "シナリオ", path: "/scenarios" },
      { label: "TRPG", path: "/trpg" },
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

  test("Alt+4でDocsに遷移できる", async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.goto("/chat");

    await page.keyboard.press("Alt+4");
    await expect(page).toHaveURL(/\/docs/);
  });

  test("timer-changedイベントでヘッダータイマーが即時更新される", async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.goto("/chat");
    await page.getByRole("link", { name: "Docs" }).waitFor();
    await page.waitForTimeout(500);

    await page.evaluate(() => {
      const started = new Date(Date.now() - 3500);
      const pad = (value: number) => String(value).padStart(2, "0");
      const startedAt = `${started.getFullYear()}-${pad(started.getMonth() + 1)}-${pad(started.getDate())}T${pad(started.getHours())}:${pad(started.getMinutes())}:${pad(started.getSeconds())}`;
      window.dispatchEvent(
        new CustomEvent("timer-changed", {
          detail: {
            activeEntry: {
              id: "timer-e2e",
              task_id: "task-e2e",
              user_id: "user-1",
              source: "timer",
              started_at: startedAt,
              task_title: "E2E timer",
            },
          },
        }),
      );
    });

    await expect(page.locator("header")).toContainText("E2E timer");
    await expect(page.locator("header")).toContainText(/00:00:0[3-9]/);
  });

  test("サイドバーにナビリンクが表示される", async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.goto("/chat");

    // サイドバー内のナビリンク
    const sidebarLinks = ["チャット", "タスク"];
    for (const label of sidebarLinks) {
      const link = page
        .locator(`aside a, aside button, [data-sidebar] a, [data-sidebar] button`)
        .filter({ hasText: label });
      await expect(link.first()).toBeVisible();
    }
  });
});
