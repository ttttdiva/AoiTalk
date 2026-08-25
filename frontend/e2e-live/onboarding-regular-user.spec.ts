import { expect, test, type Page } from "@playwright/test";

import {
  assertRegularSession,
  loadLiveAdmin,
  loginThroughUi,
  logoutThroughUi,
  type LiveUserCredentials,
} from "./support/live-auth";
import {
  assertNoLiveObservabilityIssues,
  attachLiveObservability,
  type LiveObservability,
} from "./support/live-observability";

const liveAdmin = loadLiveAdmin();

async function fillDialogField(page: Page, label: string, value: string) {
  const dialog = page.getByRole("dialog");
  const field = dialog
    .locator("div.space-y-1\\.5")
    .filter({ hasText: label })
    .first();
  await expect(field, `${label} field must be present`).toBeVisible();
  await field.locator("input").first().fill(value);
}

async function openUserManagement(page: Page) {
  await page.goto("/settings");
  const navigation = page.getByRole("navigation", { name: "設定カテゴリ" });
  await expect(navigation).toBeVisible({ timeout: 20_000 });
  await navigation.getByText("管理・運用", { exact: true }).click();
  await page.getByText("ユーザー管理", { exact: true }).first().click();
  await expect(
    page.getByRole("button", { name: "ユーザー追加", exact: true }),
  ).toBeVisible({
    timeout: 20_000,
  });
}

async function createRegularUserViaUi(
  page: Page,
  username: string,
  password: string,
): Promise<string> {
  await page.getByRole("button", { name: "ユーザー追加", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await fillDialogField(page, "ユーザー名", username);
  await fillDialogField(page, "初期ログインパスワード", password);
  const roleSelect = dialog.getByRole("combobox").first();
  await expect(roleSelect).toContainText("user");
  await roleSelect.click();
  await page.getByRole("option", { name: "user", exact: true }).click();

  const responsePromise = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/users" &&
      response.request().method() === "POST",
    { timeout: 20_000 },
  );
  await dialog.getByRole("button", { name: "追加", exact: true }).click();
  const response = await responsePromise;
  expect(
    response.status(),
    "creating the onboarding user must succeed",
  ).toBeGreaterThanOrEqual(200);
  expect(response.status()).toBeLessThan(300);
  const payload = (await response.json()) as Record<string, unknown>;
  const user =
    payload.user && typeof payload.user === "object"
      ? (payload.user as Record<string, unknown>)
      : payload;
  expect(user.role, "the UI-created onboarding account must be role=user").toBe(
    "user",
  );
  expect(
    user.password_reset_required,
    "the UI-created onboarding account must require an initial password change",
  ).toBe(true);
  const id = typeof user.id === "string" ? user.id : "";
  expect(id, "the create response must include a user id for cleanup").not.toBe(
    "",
  );
  await expect(dialog).toBeHidden({ timeout: 10_000 });
  await expect(page.getByText(`@${username}`, { exact: true })).toBeVisible({
    timeout: 10_000,
  });
  return id;
}

async function cleanupUser(page: Page, userId: string) {
  const purge = await page.request.delete(
    `/api/users/${encodeURIComponent(userId)}/purge`,
    {
      failOnStatusCode: false,
    },
  );
  if (purge.ok() || purge.status() === 404) return;

  // Purge may be blocked by a server-side relation (for example, login audit
  // retention). Soft-delete the unique test identity instead of touching any
  // shared account.
  const softDelete = await page.request.delete(
    `/api/users/${encodeURIComponent(userId)}`,
    {
      failOnStatusCode: false,
    },
  );
  if (!softDelete.ok() && softDelete.status() !== 404) {
    throw new Error(
      `onboarding cleanup failed: purge HTTP ${purge.status()}, soft-delete HTTP ${softDelete.status()}`,
    );
  }
}

async function ensureAdminSession(page: Page) {
  const statusResponse = await page.request.get("/api/auth/status", {
    failOnStatusCode: false,
  });
  const status = statusResponse.ok()
    ? ((await statusResponse.json().catch(() => null)) as {
        authenticated?: boolean;
        user?: { role?: string | null } | null;
      } | null)
    : null;
  if (status?.authenticated === true && status.user?.role === "admin") return;
  await loginThroughUi(page, liveAdmin!, { expectedRole: "admin" });
}

test.describe("regular user onboarding live flow", () => {
  test("admin creates role=user, initial password changes in UI, and the new session relogins", async ({
    browser,
    page,
  }) => {
    test.skip(!liveAdmin, "live admin credentials are not available");

    const suffix = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const username = `e2e_onboarding_${suffix}`;
    const initialPassword = `Init_${suffix}!x`;
    const newPassword = `Next_${suffix}!y`;
    const createdCredentials: LiveUserCredentials = {
      username,
      password: initialPassword,
    };
    let userId = "";
    const adminIssues = attachLiveObservability(page);
    const userContext = await browser.newContext();
    const userPage = await userContext.newPage();
    let userIssues: LiveObservability | null = null;

    try {
      await loginThroughUi(page, liveAdmin!, { expectedRole: "admin" });
      await openUserManagement(page);
      userId = await createRegularUserViaUi(page, username, initialPassword);
      await logoutThroughUi(page);

      const firstLogin = await loginThroughUi(userPage, createdCredentials, {
        expectedRole: "user",
        allowPasswordReset: true,
      });
      expect(firstLogin.user?.password_reset_required).toBe(true);
      await expect(userPage).toHaveURL(/\/change-password(?:\?|$)/);
      await userPage.locator("#new-password").fill(newPassword);
      await userPage.locator("#confirm-password").fill(newPassword);
      await userPage
        .getByRole("button", { name: "パスワードを設定", exact: true })
        .click();
      await userPage.waitForURL(/\/chat(?:\?|$)/, { timeout: 20_000 });
      // The reset flow replaces the session cookie while the old /chat tree
      // is still unmounting; observe only the stable authenticated phase.
      userIssues = attachLiveObservability(userPage);
      await assertRegularSession(userPage, username);
      await expect(userPage).toHaveURL(/\/chat(?:\?|$)/);
      await userPage.reload({ waitUntil: "domcontentloaded" });
      await expect(userPage).toHaveURL(/\/chat(?:\?|$)/);
      await assertRegularSession(userPage, username);

      assertNoLiveObservabilityIssues(
        userIssues,
        "regular-user onboarding initial authenticated session",
      );
      await logoutThroughUi(userPage);
      const reloginIssues = attachLiveObservability(userPage);
      const relogin = await loginThroughUi(
        userPage,
        { username, password: newPassword },
        { expectedRole: "user" },
      );
      expect(relogin.user?.password_reset_required).not.toBe(true);
      await expect(userPage).toHaveURL(/\/chat(?:\?|$)/);
      await assertRegularSession(userPage, username);
      assertNoLiveObservabilityIssues(
        reloginIssues,
        "regular-user onboarding relogin session",
      );
      await logoutThroughUi(userPage);
    } finally {
      await userContext.close();
      if (userId) {
        await ensureAdminSession(page);
        await cleanupUser(page, userId);
        await logoutThroughUi(page);
      }
      assertNoLiveObservabilityIssues(adminIssues, "admin onboarding session");
    }
  });
});
