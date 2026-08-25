import { test, expect } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

test.describe("タスクページ", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.goto("/tasks");
  });

  test("タスク一覧とデスクトップツールバーが表示される", async ({ page }) => {
    await expect(page.getByTestId("task-list-toolbar")).toBeVisible();
    await expect(page.getByText("タスクがありません")).toBeVisible();
  });

  test("デスクトップのFilterと表示列コントロールが表示される", async ({ page }) => {
    const toolbar = page.getByTestId("task-list-toolbar");
    await expect(toolbar.getByRole("button", { name: /Filter/ })).toBeVisible();
    await expect(page.getByTitle("表示列")).toBeVisible();
    await expect(page.getByTitle("その他")).toBeVisible();
    await expect(toolbar.getByRole("button", { name: "New Task" })).toBeVisible();
  });

  test("検索バーが存在する", async ({ page }) => {
    await expect(page.getByPlaceholder("Search tasks...")).toBeVisible();
  });
});
