import { expect, test } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

test("Runtime popup does not collapse the chat sidebar at compact desktop width", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  await page.goto("/chat");

  const navigation = page.getByTestId("chat-workspace-navigation");
  await expect(navigation).toBeVisible({ timeout: 15_000 });
  const frame = page.getByTestId("workspace-navigation-frame");
  await expect(frame).toHaveAttribute("data-sidebar-state", "expanded");

  await page.getByRole("button", { name: "Runtime設定を開く" }).click();
  await expect(page.getByRole("dialog", { name: "ランタイム設定" })).toBeVisible();
  await expect(navigation).toBeVisible();
  await expect(frame).toHaveAttribute("data-sidebar-state", "expanded");
});
