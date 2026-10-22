import { expect, test } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

for (const width of [390, 1366, 1920]) {
  test(`Operations navigation retains updated same-section URL scope at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    await page.route("**/api/python-proxy/operations/connections*", route => route.fulfill({ json: [] }));
    await page.route("**/api/python-proxy/operations/actions*", route => route.fulfill({ json: [] }));
    await page.goto("/operations?tab=connections&persona=previous-persona&content=previous-brief");
    const navigation = page.getByRole("navigation", { name: "Operationsワークスペース" });
    if (width < 768) await page.locator('[data-sidebar="trigger"]').click();
    await expect(navigation).toBeVisible();
    await expect(page.getByTestId("workspace-navigation-frame")).toHaveAttribute("data-sidebar-persistent", "false");
    await expect(page.locator('[data-shell-region="main-canvas"]').getByRole("navigation", { name: "Operationsワークスペース" })).toHaveCount(0);
    await page.evaluate(() => {
      window.history.pushState({}, "", "/operations?tab=connections&persona=latest-persona&content=latest-brief&read_only=1");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await expect(page).toHaveURL(/persona=latest-persona/);
    await navigation.locator('[data-operations-section="actions"]').click();
    await expect(page).toHaveURL(/tab=actions&persona=latest-persona&content=latest-brief&read_only=1/);
    await expect(navigation.locator('[data-operations-section="actions"]')).toHaveAttribute("aria-current", "page");
  });
}
